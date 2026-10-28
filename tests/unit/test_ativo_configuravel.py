"""Troca de ativo (e de bolsa) so por configuracao: perfis, vencimento, tarifas, ticks, corretora, falha segura."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from perfis_teste import ACAO, PERFIS, WDO, WIN

from robowin.core import calendario_b3 as cal
from robowin.core.bolsas import SemTaxaDeCambio, carregar_bolsa, converter
from robowin.core.config import ErroConfig
from robowin.core.instrumentos import (
    AtivoDivergente,
    PerfilNaoConfirmado,
    carregar_perfil,
    exigir_mesmo_ativo,
)
from robowin.core.tipos import ConfigRisco
from robowin.execucao.custos import Custos
from robowin.ia.orcamento import OrcamentoIA
from robowin.mt5.contrato import ContratoRecusado, resolver_contrato
from robowin.mt5.duble import criar_duble_padrao
from robowin.mt5.gateway import ConfigGateway, Gateway
from robowin.risco.politica import limites_conservadores, risco_do_ativo, stop_maximo_efetivo_pontos

HOJE = dt.date(2026, 10, 27)
ESPERADO = {  # ativo -> (tick, valor do ponto por lote, lote, contrato vigente em HOJE, vencimento)
    "WIN": (5.0, 0.2, 1, "WINZ26", dt.date(2026, 12, 16)),
    "WDO": (0.5, 10.0, 1, "WDOX26", dt.date(2026, 11, 3)),
    "PETR4": (0.01, 100.0, 100, "PETR4", None),
}


@pytest.mark.parametrize("nome", list(PERFIS))
def test_perfil_tick_ponto_vencimento_tarifa_e_stop_em_ticks(nome):
    p = PERFIS[nome]
    tick, pv, lote, contrato, venc = ESPERADO[nome]
    assert (p.tick_pontos, p.ponto_valor_brl, p.lote) == (tick, pv, lote)
    assert p.tick_valor_brl == pytest.approx(tick * pv)
    assert cal.contrato_vigente(p, HOJE) == contrato
    assert cal.vencimento_do_codigo(p, contrato) == venc
    assert p.tarifa_em(HOJE).tarifa_lado_brl > 0
    assert Custos.do_perfil(p).ida_e_volta(HOJE, 1) == pytest.approx(2 * p.tarifa_em(HOJE).tarifa_lado_brl)
    # stop/folga declarados em TICKS e convertidos pelo perfil
    assert p.stop_max_pontos == pytest.approx(p.stop_max_ticks * tick)
    cfg = limites_conservadores(p, ConfigRisco())
    assert cfg.stop_max_pontos == pytest.approx(p.stop_max_pontos)
    assert stop_maximo_efetivo_pontos(cfg, pv) >= tick  # cabe ao menos 1 tick dentro da perda por operacao
    assert p.ticks(p.pontos(7)) == pytest.approx(7)


def test_wdo_vence_no_primeiro_dia_util_e_rola():
    assert cal.contrato_vigente(WDO, dt.date(2026, 11, 3)) == "WDOX26"   # dia do vencimento: ainda vigente
    assert cal.contrato_vigente(WDO, dt.date(2026, 11, 4)) == "WDOZ26"   # rolou
    assert cal.contrato_vigente(WIN, dt.date(2026, 12, 17)) == "WING27"


def test_a_confirmar_bloqueia_a_operacao_mas_nao_a_simulacao():
    with pytest.raises(PerfilNaoConfirmado):
        carregar_perfil("WDO")
    with pytest.raises(PerfilNaoConfirmado):
        carregar_perfil("PETR4")
    assert carregar_perfil("WIN").a_confirmar is False
    assert WDO.a_confirmar and "ativo_a_confirmar" in WDO.rotulos()
    d = criar_duble_padrao()
    d.initialize()
    with pytest.raises(PerfilNaoConfirmado):
        resolver_contrato(d, HOJE, WDO)


def test_perfil_inexistente_e_recusado():
    with pytest.raises(ErroConfig):
        carregar_perfil("XXXX3")


@pytest.mark.parametrize("nome", ["WDO", "PETR4"])
def test_corretora_confere_com_o_perfil_confirmado_pelo_usuario(nome):
    p = PERFIS[nome].model_copy(update={"a_confirmar": False, "a_confirmar_itens": ()})  # o usuario confirmou
    d = criar_duble_padrao()
    d.initialize()
    r = resolver_contrato(d, HOJE, p)
    assert r.instrumento.ativo == nome and r.instrumento.tick_pontos == p.tick_pontos
    assert r.instrumento.ponto_valor_brl == pytest.approx(p.ponto_valor_brl) and r.instrumento.lote == p.lote


@pytest.mark.parametrize("campo,valor", [("tick_pontos", 1.0), ("ponto_valor_brl", 99.0), ("lote", 7)])
def test_corretora_divergente_do_perfil_e_recusada(campo, valor):
    p = WIN.model_copy(update={campo: valor})
    d = criar_duble_padrao()
    d.initialize()
    with pytest.raises(ContratoRecusado):
        resolver_contrato(d, HOJE, p)


def test_gateway_recusa_perfil_a_confirmar_e_ausente():
    from test_mt5_comum import LOGIN, SERVIDOR, montar_duble, ticks_planos
    d, rel = montar_duble(ticks_planos(50))
    from robowin.mt5.execucao_demo import GuardaDemoViolada
    for perfil in (None, WDO):
        with pytest.raises(GuardaDemoViolada):
            Gateway(d, ConfigGateway(perfil=perfil, login=LOGIN, servidor=SERVIDOR, hoje=HOJE), rel).iniciar()


def test_modelo_e_orcamento_de_outro_ativo_nunca_sao_reutilizados(tmp_path):
    with pytest.raises(AtivoDivergente):
        exigir_mesmo_ativo("WIN", "WDO", "modelo x")
    o = OrcamentoIA(100.0)
    o.vincular_ativo("WIN")
    o.vincular_ativo("WIN")
    with pytest.raises(AtivoDivergente):
        o.vincular_ativo("WDO")
    from robowin.core.tipos import EntradaRegistroModelo
    from robowin.modelos.registro import RegistroModelos
    reg = RegistroModelos()
    reg.registrar(EntradaRegistroModelo(nome="m", versao="v1", ativo="WDO", hash_dados="a", hash_codigo="b",
                                        status="em_validacao"), origem="sintetico")
    assert reg.exigir_ativo("m", "v1", "WDO").ativo == "WDO"
    with pytest.raises(AtivoDivergente):
        reg.exigir_ativo("m", "v1", "WIN")


def test_orquestrador_recusa_modelo_treinado_em_outro_ativo():
    from test_decisao_comum import INS, T0

    from robowin.core.relogio import RelogioSimulado
    from robowin.dados.banco import BancoSQLite
    from robowin.decisao.orquestrador import ConfigOrquestrador, Orquestrador
    from robowin.decisao.registro import Registro

    class Modelo:
        nome, status, ativo = "m", "em_validacao", "WDO"

        def prever(self, *_):
            return {}

    from robowin.decisao.bracos.controles import BracoNaoOperar
    with pytest.raises(AtivoDivergente):
        Orquestrador(ConfigOrquestrador(instrumento=INS), relogio=RelogioSimulado(T0),
                     registro=Registro(BancoSQLite(":memory:"), None, "t"), bracos=[BracoNaoOperar()],
                     modelos={"m": Modelo()})


def test_ativo_diferente_do_aprovado_usa_limites_minimos_e_rotula(tmp_path):
    # copia isolada com SO a aprovacao 'pendente' (v1): o repositorio real ja tem a v2 aprovada pelo titular
    (tmp_path / "aprov").mkdir()
    risco = tmp_path / "risco.toml"
    risco.write_bytes(Path("config/risco.toml").read_bytes())
    v1 = tmp_path / "aprov" / "risco-v1.toml"
    v1.write_bytes(Path("config/aprovacoes/risco-v1.toml").read_bytes())
    cfg, erro = risco_do_ativo(WDO, risco, v1)
    assert erro and "risco-wdo" in erro
    assert cfg.stop_max_pontos == pytest.approx(WDO.stop_max_pontos) and cfg.operacoes_dia_max <= 5
    with pytest.raises(ErroConfig):
        risco_do_ativo(WDO, risco, v1, ao_vivo=True)
    cfg_win, erro_win = risco_do_ativo(WIN, risco, v1)
    # aprovacao com responsavel 'pendente': carrega (simulacao) mas fica rotulado risco_nao_aprovado
    assert erro_win and "pendente" in erro_win and cfg_win.stop_max_pontos == 300.0
    with pytest.raises(ErroConfig, match="pendente"):
        risco_do_ativo(WIN, risco, v1, ao_vivo=True)


def test_repositorio_win_aprovado_pelo_titular_para_demo():
    cfg_win, erro_win = risco_do_ativo(WIN, "config/risco.toml", "config/aprovacoes/risco-v1.toml")
    assert erro_win is None and cfg_win.contratos_max == 1


def test_manifesto_inclui_ativo_e_hash_do_perfil():
    from robowin.decisao.simulacao import montar_manifesto
    from robowin.ia.catalogo import Catalogo
    m = montar_manifesto("s", [], Catalogo.carregar("config/modelos.toml"), {"perfil": WDO.hash()}, ativo="WDO")
    assert m.ativo == "WDO" and m.hashes["perfil"] != WIN.hash()


# --------------------------------------------------------------------------------------------- bolsas e moeda
def test_bolsa_b3_confirmada_e_demais_sao_modelos_a_confirmar():
    b3 = carregar_bolsa("B3")
    assert not b3.a_confirmar and b3.moeda == "BRL" and b3.calendario == "BVMF" and b3.calcula_tributo
    for outra in ("NYSE", "NASDAQ", "LSE", "CME"):
        assert carregar_bolsa(outra).a_confirmar
    assert carregar_bolsa("NYSE").sessao_em(HOJE).abertura == "09:30"
    with pytest.raises(ErroConfig):
        carregar_bolsa("XXXX")


def test_ativo_em_bolsa_a_confirmar_herda_bloqueio_e_moeda(tmp_path, monkeypatch):
    p = carregar_perfil("WIN", permitir_a_confirmar=True)
    q = p.model_copy(update={"bolsa": "NYSE"})
    from robowin.core.instrumentos import _aplicar_bolsa
    q = _aplicar_bolsa(q)
    assert q.moeda == "USD" and q.a_confirmar and q.instrumento().moeda == "USD"
    with pytest.raises(PerfilNaoConfirmado):
        q.exigir_confirmado()


def test_conversao_de_moeda_so_com_taxa_fornecida():
    assert converter(10.0, "USD", "USD") == 10.0
    assert converter(10.0, "USD", "BRL", 5.5) == pytest.approx(55.0)
    with pytest.raises(SemTaxaDeCambio):
        converter(10.0, "USD", "BRL")


def test_relatorio_de_bolsa_estrangeira_diz_tributo_nao_calculado():
    from robowin.avaliacao import relatorio
    from robowin.core.instrumentos import _aplicar_bolsa
    from robowin.core.tipos import Lado, Trade
    ts = [Trade(id=f"t{i}", braco="B", lado=Lado.COMPRA, entrada=100.0, saida=101.0, t_entrada_ns=i * 10**9,
                t_saida_ns=i * 10**9 + 5 * 10**8, bruto_brl=5.0, tarifas_brl=0.5, corretagem_brl=0.0,
                slippage_brl=0.0, motivo_saida="alvo") for i in range(5)]
    q = _aplicar_bolsa(WIN.model_copy(update={"bolsa": "NYSE"}))
    rel = relatorio.gerar_relatorio({"B": ts}, tentativas=1, perfil=q, n_boot=50)
    assert rel["bracos"]["B"]["impostos"] == [] and rel["bracos"]["B"]["tributo"] == "tributo nao calculado"
    md = relatorio.renderizar_markdown(rel)
    assert "tributo nao calculado" in md and "USD" in md and "R$" not in md.split("Taxa de acerto")[0]


def test_acao_e_etf_sao_perfis_validos():
    assert ACAO.classe == "acao" and carregar_perfil("BOVA11", permitir_a_confirmar=True).classe == "etf"


# --------------------------------------------------------------------------------------------- CLI sobre sinteticos
@pytest.mark.parametrize("ativo", ["WDO", "PETR4"])
def test_simular_com_outro_ativo_roda_e_rotula(ativo, tmp_path, capsys):
    from robowin.cli import main
    rc = main(["simular", "--ativo", ativo, "--n-ticks", "2500", "--saida", str(tmp_path / ativo)])
    out = capsys.readouterr().out
    assert rc == 0 and f"Ativo: {ativo}" in out and "ativo_a_confirmar" in out and "risco_nao_aprovado" in out
