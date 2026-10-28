"""Latencia da reflexao no backtest: distribuicao MEDIDA (chamadas_llm do papel 'reflexao' / latencias da etapa
'reflexao_ia' com injetado=0) quando houver amostras, sorteada por seed e conservadora (max(medida na chamada,
injetada)); sem amostras, a fixa de 5000 ms."""
from __future__ import annotations

import json
import sqlite3

import pytest
from perfis_teste import INS_WIN, WIN

from robowin.backtest import (
    BracoSonda,
    ConfigBacktest,
    ConfigReflexaoBacktest,
    DistribuicaoLatencia,
    FonteMemoria,
    MotorBacktest,
    latencia_reflexao,
    reflexao_de_config,
)
from robowin.backtest.motor import LATENCIA_REFLEXAO_PADRAO_MS, MIN_AMOSTRAS_LATENCIA_REFLEXAO
from robowin.core.tipos import ChamadaLLM
from robowin.dados.banco import BancoSQLite
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.ia.esquemas import ReflexaoIA

NS = 1_000_000_000
DADOS = gerar(ConfigSintetico.do_perfil(WIN, seed=11, n_ticks=3000, livro_a_cada_ticks=10))
MEDIDAS = [6100.0, 7200.0, 8300.0, 9400.0, 10500.0, 11600.0]


def _chamada(banco: BancoSQLite, i: int, papel: str, ms: float, *, id_: str | None = None) -> None:
    banco.inserir("chamadas_llm", {"id": id_ or f"s-llm-{papel}-{i}", "t_ns": 1000 + i, "braco": "b", "papel": papel,
                                   "modelo_pedido": "falso", "total_ms": ms, "valida": 1, "custo_usd": 0.0})


def _banco_medido(amostras=MEDIDAS) -> BancoSQLite:
    b = BancoSQLite(":memory:")
    for i, ms in enumerate(amostras):
        _chamada(b, i, "reflexao", ms)
    _chamada(b, 900, "proposta", 99_999.0)  # outro papel: fora
    _chamada(b, 901, "reflexao", 0.0)  # sem medida: fora
    _chamada(b, 902, "reflexao", 77_777.0, id_="llm-ref-000001")  # gravada por um backtest (injetada): fora
    b.inserir("latencias", {"t_ns": 5, "braco": "", "etapa": "reflexao_ia", "dur_ms": 55_555.0, "injetado": 1,
                            "modelo": "", "effort": ""})  # injetada: fora
    return b


class SondaIA(BracoSonda):
    ia = True


class ServicoDuble:
    def __init__(self, total_ms: float = 1.0) -> None:
        self.total_ms = total_ms

    async def chamar(self, mensagem, prazo_s, hash_dossie):
        ch = ChamadaLLM(papel="reflexao", modelo_pedido="falso:reflexao", modelo_servido="reflexao-FALSO",
                        valida=True, custo_usd=0.001, total_ms=self.total_ms)
        return ReflexaoIA(licao="Amostra minima; manter a cautela."), ch, None


def _rodar(lat: DistribuicaoLatencia, *, seed: int = 3, total_ms: float = 1.0, banco=None):
    ref = ConfigReflexaoBacktest(ativo=True, atraso_liquidacao_s=60.0, latencia=lat)
    cfg = ConfigBacktest(instrumento=INS_WIN, seed=seed, intervalo_decisao_s=30.0, reflexao=ref)
    return MotorBacktest(FonteMemoria(DADOS.ticks, DADOS.livros, sintetico=True), [SondaIA(p_operar=0.4)], cfg, banco,
                         servico_reflexao=ServicoDuble(total_ms)).executar()


# ============================================================================ origem da distribuicao ==
def test_sem_amostras_mantem_a_fixa_de_5000ms():
    assert latencia_reflexao(None).amostras_ms == (LATENCIA_REFLEXAO_PADRAO_MS,) == (5000.0,)
    assert latencia_reflexao(BancoSQLite(":memory:")).origem == "fixa_5000.0ms"
    poucas = _banco_medido(MEDIDAS[: MIN_AMOSTRAS_LATENCIA_REFLEXAO - 1])
    assert latencia_reflexao(poucas).amostras_ms == (5000.0,)
    assert ConfigReflexaoBacktest().latencia.amostras_ms == (5000.0,)
    assert reflexao_de_config({"reflexao": {"ativo": True}}).latencia.amostras_ms == (5000.0,)


def test_usa_so_as_amostras_medidas_do_papel_reflexao():
    d = latencia_reflexao(_banco_medido())
    assert list(d.amostras_ms) == MEDIDAS and d.origem == "banco:papel=reflexao"
    b = _banco_medido(MEDIDAS[:4])
    b.inserir("latencias", {"t_ns": 9, "braco": "", "etapa": "reflexao_ia", "dur_ms": 4321.0, "injetado": 0,
                            "modelo": "", "effort": ""})  # medida (etapa da reflexao): entra
    assert sorted(latencia_reflexao(b).amostras_ms) == sorted([*MEDIDAS[:4], 4321.0])


def test_banco_antigo_sem_tabelas_nao_quebra(tmp_path):
    p = tmp_path / "antigo.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE coisa(a)")
    con.commit()
    con.close()

    class SoLeitura:  # Banco minimo (protocolo) sobre um arquivo sem chamadas_llm/latencias
        def consultar(self, sql, params=()):
            c = sqlite3.connect(p)
            c.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in c.execute(sql, params).fetchall()]  # levanta: tabela inexistente
            finally:
                c.close()

        def inserir(self, tabela, linha):
            raise AssertionError("somente leitura")

    assert latencia_reflexao(SoLeitura()).amostras_ms == (5000.0,)


def test_reflexao_de_config_com_banco_medido_e_latencia_explicita_tem_prioridade():
    b = _banco_medido()
    assert list(reflexao_de_config({"reflexao": {"ativo": True}}, banco_medido=b).latencia.amostras_ms) == MEDIDAS
    fixa = DistribuicaoLatencia.fixa(1234.0)
    assert reflexao_de_config({}, fixa, banco_medido=b).latencia is fixa


# ============================================================================ uso no motor ==
def _lat_reflexao(res):
    return [x["dur_ms"] for x in res.latencias if x["etapa"] == "reflexao_ia"]


def test_motor_sorteia_da_distribuicao_medida_de_forma_deterministica():
    med = latencia_reflexao(_banco_medido())
    banco = BancoSQLite(":memory:")
    a, b = _rodar(med, banco=banco), _rodar(med)
    lat = _lat_reflexao(a)
    assert lat, "o teste precisa de reflexoes liquidadas"
    assert all(any(x == pytest.approx(m) for m in MEDIDAS) for x in lat)  # cada latencia e uma amostra medida
    assert a.hash_resultado() == b.hash_resultado() and _lat_reflexao(b) == lat  # mesma seed => mesmo sorteio
    liq = [r for r in a.reflexoes if r["estado"] == "liquidada"]
    assert liq and all(r["t_disponivel_ns"] - r["t_liquidacao_ns"] >= int(min(MEDIDAS) * 1e6) for r in liq)
    fixa = _rodar(DistribuicaoLatencia.fixa(LATENCIA_REFLEXAO_PADRAO_MS))
    assert set(_lat_reflexao(fixa)) == {5000.0}
    man = json.loads(banco.consultar("SELECT manifesto_json FROM experimentos")[0]["manifesto_json"])
    assert man["reflexao"]["latencia"]["origem"] == "banco:papel=reflexao"  # a origem fica registrada


def test_motor_e_conservador_medida_na_chamada_maior_que_a_injetada_prevalece():
    med = latencia_reflexao(_banco_medido())
    res = _rodar(med, total_ms=50_000.0)
    lat = _lat_reflexao(res)
    assert lat and set(lat) == {50_000.0}  # max(medida na chamada, injetada)
