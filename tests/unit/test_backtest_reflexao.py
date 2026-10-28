"""Memoria reflexiva no MotorBacktest: desligada por padrao, mesma semantica de t_disponivel do Orquestrador
(licao so visivel apos liquidacao + atraso + latencia da IA), deterministica e sem vazamento do futuro."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from perfis_teste import INS_WIN, WIN

from robowin.backtest import (
    BracoSonda,
    ConfigBacktest,
    ConfigReflexaoBacktest,
    ControleAleatorio,
    DistribuicaoLatencia,
    FonteMemoria,
    MotorBacktest,
    executar_cenarios,
    reflexao_de_config,
    verificar_perturbacao_futura,
)
from robowin.backtest.vazamento import assinatura_decisoes
from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.config import carregar_toml
from robowin.core.tipos import ChamadaLLM
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.ia.esquemas import ReflexaoIA

NS = 1_000_000_000
RAIZ = Path(__file__).resolve().parents[2]
DADOS = gerar(ConfigSintetico.do_perfil(WIN, seed=11, n_ticks=3000, livro_a_cada_ticks=10))
T_CORTE = dt_utc_ns(dt.datetime(2026, 3, 10, 13, 30))
ATRASO_S = 60.0
LAT_MS = 3000.0
REF = ConfigReflexaoBacktest(ativo=True, atraso_liquidacao_s=ATRASO_S, latencia=DistribuicaoLatencia.fixa(LAT_MS))


class SondaIA(BracoSonda):
    """Sonda de vazamento marcada como braco de IA (so bracos com IA ganham reflexao, como no Orquestrador)."""

    ia = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self.dossies: list[tuple[int, dict]] = []

    def decidir(self, t_ns, dossie):
        self.dossies.append((t_ns, dossie))
        return super().decidir(t_ns, dossie)


class ServicoDuble:
    """Duble de ``ServicoLLM`` (papel reflexao): a licao pode depender da mensagem (resultado liquidado)."""

    def __init__(self, licao=lambda msg: "Uma unica operacao e amostra minima; manter a cautela."):
        self.licao, self.mensagens = licao, []

    async def chamar(self, mensagem, prazo_s, hash_dossie):
        self.mensagens.append(mensagem)
        ch = ChamadaLLM(papel="reflexao", modelo_pedido="falso:reflexao", modelo_servido="reflexao-FALSO",
                        valida=True, custo_usd=0.001, total_ms=1.0)
        return ReflexaoIA(licao=self.licao(mensagem)), ch, None


def cfg(reflexao: ConfigReflexaoBacktest | None = None) -> ConfigBacktest:
    return ConfigBacktest(instrumento=INS_WIN, seed=3, intervalo_decisao_s=30.0,
                          **({"reflexao": reflexao} if reflexao is not None else {}))


def rodar(ticks=DADOS.ticks, livros=DADOS.livros, *, reflexao=REF, servico=None, bracos=None):
    bracos = bracos or [SondaIA(p_operar=0.4), ControleAleatorio(4, p_entrada=0.1)]
    motor = MotorBacktest(FonteMemoria(ticks, livros, sintetico=True), bracos, cfg(reflexao),
                          servico_reflexao=servico if servico is not None else ServicoDuble())
    return motor.executar(), bracos


# ================================================================================ desligada por padrao ==
def test_desligada_por_padrao_e_o_resultado_nao_muda():
    assert ConfigBacktest(instrumento=INS_WIN).reflexao.ativo is False
    assert reflexao_de_config(carregar_toml(RAIZ / "config" / "robowin.toml")).ativo is False
    sem = MotorBacktest(FonteMemoria(DADOS.ticks, DADOS.livros, sintetico=True),
                        [BracoSonda(p_operar=0.4), ControleAleatorio(4, p_entrada=0.1)], cfg()).executar()
    serv = ServicoDuble()
    com_servico, _ = rodar(reflexao=ConfigReflexaoBacktest(), servico=serv)  # servico presente, [reflexao] desligada
    assert com_servico.reflexoes == [] and serv.mensagens == []
    assert com_servico.hash_resultado() == sem.hash_resultado()


def test_reflexao_de_config_le_a_secao():
    c = reflexao_de_config({"reflexao": {"ativo": True, "atraso_liquidacao_s": 30, "max_reflexoes": 2}})
    assert c.ativo and c.atraso_liquidacao_s == 30.0 and c.max_reflexoes == 2
    assert c.latencia.amostras_ms  # latencia injetada padrao (conservadora)


# ================================================================================ semantica de t_disponivel ==
def test_licao_so_aparece_no_dossie_depois_de_liquidacao_atraso_e_latencia():
    res, (sonda, _ctrl) = rodar()
    liq = [r for r in res.reflexoes if r["estado"] == "liquidada"]
    assert liq, "o teste precisa de reflexoes liquidadas"
    assert {r["braco"] for r in res.reflexoes} == {"sonda"}  # o controle (sem IA) nao ganha reflexao
    for r in liq:
        assert r["t_liquidacao_ns"] >= r["t_saida_ns"] + int(ATRASO_S * NS)
        assert r["t_disponivel_ns"] >= r["t_liquidacao_ns"] + int(LAT_MS * 1e6)
    disp = {r["t_disponivel_ns"] for r in liq}
    com_licao = [t for t, d in sonda.dossies if any("reflexao" in m for m in d["memoria"])]
    assert com_licao, "nenhum dossie recebeu a licao: teste vazio"
    assert min(com_licao) >= min(disp)
    for t, d in sonda.dossies:  # cada licao no dossie de t ja estava disponivel em t
        n_licoes = sum(1 for m in d["memoria"] if "reflexao" in m)
        assert n_licoes <= sum(1 for x in disp if x <= t)
    # custo e latencia injetada registrados (rotulados como injetados)
    assert res.custo_reflexao_usd.get("sonda", 0.0) > 0
    lat = [x for x in res.latencias if x["etapa"] == "reflexao_ia"]
    assert lat and all(x["injetado"] for x in lat) and all(x["dur_ms"] >= LAT_MS for x in lat)


def test_mensagem_da_reflexao_e_anonimizada_e_sem_trade_id():
    serv = ServicoDuble()
    rodar(servico=serv)
    assert serv.mensagens and all("OPERACAO LIQUIDADA" in m and "sonda:" not in m for m in serv.mensagens)


def test_deterministico_com_a_mesma_seed():
    a, _ = rodar()
    b, _ = rodar()
    assert a.reflexoes and a.hash_resultado() == b.hash_resultado()


def test_falha_do_servico_nunca_derruba_o_backtest():
    class Quebrado:
        async def chamar(self, *a):
            raise RuntimeError("rede")

    res, _ = rodar(servico=Quebrado())
    assert res.reflexoes and all(r["licao"] is None for r in res.reflexoes)
    assert all(r["estado"] in ("pendente", "em_liquidacao", "descartada") for r in res.reflexoes)


def test_ativo_sem_servico_desliga_e_nao_quebra():
    motor = MotorBacktest(FonteMemoria(DADOS.ticks, DADOS.livros, sintetico=True), [SondaIA(p_operar=0.4)], cfg(REF))
    res = motor.executar()
    assert res.reflexoes == []


def test_servico_real_com_provedor_falso_e_dentro_de_loop_assincrono():
    """Paridade com a sessao: o mesmo ServicoLLM do Orquestrador; o motor sincrono roda mesmo com um loop ativo."""
    import asyncio

    from robowin.decisao.bracos.llm import ServicoLLM
    from robowin.ia.provedores.falso import ProvedorFalso

    def montar():
        return ServicoLLM(ProvedorFalso(), "falso:claude-haiku-5-5", "reflexao", ReflexaoIA, max_tokens=300)

    sinc, _ = rodar(servico=montar())

    async def dentro_do_loop():
        return rodar(servico=montar())[0]

    assinc = asyncio.run(dentro_do_loop())
    assert any(r["estado"] == "liquidada" for r in sinc.reflexoes)
    assert sinc.hash_resultado() == assinc.hash_resultado()


def test_cenarios_usam_memoria_nova_por_cenario():
    serv = ServicoDuble()
    out = executar_cenarios(FonteMemoria(DADOS.ticks, DADOS.livros, sintetico=True),
                            lambda: [SondaIA(p_operar=0.4)], cfg(REF), servico_reflexao=serv)
    base, est = out["base"], out["estresse"]
    assert base.reflexoes and est.reflexoes
    for r_ in (base, est):  # um registro por trade do proprio cenario (memoria nao vaza entre cenarios)
        ids = [r["trade_id"] for r in r_.reflexoes]
        assert len(ids) == len(set(ids)) == len(r_.trades["sonda"])


# ================================================================================ vazamento ==
def test_perturbacao_futura_com_reflexao_ligada_nao_muda_o_passado():
    """O teste de perturbacao do futuro do backtest, agora com a licao dependente do RESULTADO do trade."""
    def licao(msg: str) -> str:
        return "Resultado favoravel; manter a tese." if '"resultado_pontos":-' not in msg else \
            "Resultado contra; mais cautela na geometria."

    def executar(t, lv):
        return rodar(t, lv, servico=ServicoDuble(licao))[0]

    rel = verificar_perturbacao_futura(executar, DADOS.ticks, DADOS.livros, T_CORTE, semente=1,
                                       tick_pontos=WIN.tick_pontos)
    assert rel.ok, rel.texto()
    assert rel.n_comparados > 100 and rel.perturbacao_efetiva


def test_trocar_so_a_licao_nao_muda_nenhuma_decisao_antes_de_ela_estar_disponivel():
    a, _ = rodar(servico=ServicoDuble(lambda m: "Licao do caso A; manter a cautela."))
    b, _ = rodar(servico=ServicoDuble(lambda m: "Licao do caso B; reduzir a pressa."))
    disp = [r["t_disponivel_ns"] for r in a.reflexoes if r["estado"] == "liquidada"]
    assert disp
    primeira = min(disp)
    # tudo ANTES da primeira licao disponivel e identico (mesmo instante de liquidacao, so o texto mudou)
    assert assinatura_decisoes(a, primeira - 1) == assinatura_decisoes(b, primeira - 1)
    # e o texto da licao chega de fato ao dossie depois (o teste nao e vazio)
    assert assinatura_decisoes(a) != assinatura_decisoes(b)
