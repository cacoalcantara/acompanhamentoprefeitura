"""Regressoes da revisao: orcamento de IA no nucleo multi-dia, risco nao aprovado, n_alvo coerente com o MDE."""
from __future__ import annotations

import pytest
from perfis_teste import WIN
from test_decisao_comum import NS, T0, BracoRoteirizado, alimentar, montar_orq, serie

from robowin.avaliacao.estatistica import n_expectativa
from robowin.core.config import ErroConfig
from robowin.core.tipos import ChamadaLLM
from robowin.decisao.bracos.base import RegistroChamada, ResultadoDecisao
from robowin.decisao.simulacao import (
    ParamsSimulacao,
    carregar_risco_simulacao,
    construir_orquestrador,
    montar_sessao,
)
from robowin.ia.orcamento import OrcamentoIA


# --- 9: orcamento rola o dia, carrega o gasto do mes e grava a cada confirmacao ------------------------------
async def test_orcamento_rola_o_dia_carrega_gasto_do_mes_e_grava_a_cada_chamada():
    orc = OrcamentoIA(100.0)
    orq, banco, _ = montar_orq([BracoRoteirizado("a", [lambda s, c: s.nada(c)])], orcamento=orc)
    banco.inserir("orcamento_ia", {"dia": "2026-11-02", "braco": "a", "gasto_usd": 30.0, "reservado_usd": 0.0})
    banco.inserir("orcamento_ia", {"dia": "2026-10-30", "braco": "a", "gasto_usd": 999.0, "reservado_usd": 0.0})
    orq.iniciar(None, t_ns=T0)
    assert orc.gasto_mes == pytest.approx(30.0)  # so o mes corrente; persiste entre reinicios
    assert orc.dia == "2026-11-10"
    r = orc.reservar(1.0)
    orc.confirmar(r, 1.0)
    assert orc.gasto_hoje == 1.0
    cota_d1 = orc.cota_dia
    await alimentar(orq, serie([130000.0] * 3))
    assert orc.dia == "2026-11-10"
    # outro pregao (America/Sao_Paulo): o dia rola, gasto_hoje zera e a cota e redistribuida
    await alimentar(orq, serie([130000.0] * 3, inicio_ms=86_400_000))
    assert orc.dia == "2026-11-11" and orc.gasto_hoje == 0.0 and orc.gasto_mes == pytest.approx(31.0)
    assert orc.cota_dia > cota_d1  # menos pregoes restantes para a mesma verba
    # virada de mes: o gasto do mes volta a ser o do novo mes (sem linhas => 0)
    await alimentar(orq, serie([130000.0] * 3, inicio_ms=25 * 86_400_000))
    assert orc.dia == "2026-12-05" and orc.gasto_mes == 0.0


def test_gasto_de_ia_e_gravado_a_cada_chamada_por_dia():
    orc = OrcamentoIA(100.0)
    orq, banco, _ = montar_orq([BracoRoteirizado("a", [lambda s, c: s.nada(c)])], orcamento=orc)
    ch = ChamadaLLM(papel="proponente", modelo_pedido="x:y", custo_usd=0.25, valida=True)
    res = ResultadoDecisao(braco="a", modo="imediato", t0_ns=T0, ciclo_id="c", acao=None, resultado="nao_operar",
                           chamadas=[RegistroChamada(ch, "a", "proponente")])
    orq._registrar_ia(res)
    orq._registrar_ia(res)
    linhas = banco.consultar("SELECT dia, braco, gasto_usd FROM orcamento_ia")
    assert linhas == [{"dia": "2026-11-10", "braco": "a", "gasto_usd": 0.5}]  # gravado antes do fim da sessao
    res2 = ResultadoDecisao(braco="a", modo="imediato", t0_ns=T0 + 86_400 * NS, ciclo_id="c", acao=None,
                            resultado="nao_operar", chamadas=[RegistroChamada(ch, "a", "proponente")])
    orq._registrar_ia(res2)
    assert {r["dia"]: r["gasto_usd"] for r in banco.consultar("SELECT * FROM orcamento_ia")} == {
        "2026-11-10": 0.5, "2026-11-11": 0.25}


# --- 10: risco nao aprovado --------------------------------------------------------------------------------
def test_risco_nao_aprovado_simulacao_registra_incidente_critico_e_rotula(tmp_path):
    p = ParamsSimulacao(n_ticks=1500, saida=tmp_path / "s", risco=tmp_path / "inexistente.toml",
                        aprovacao=tmp_path / "nao-existe.toml")
    risco, erro = carregar_risco_simulacao(p, WIN)
    assert erro and risco.contratos_max == 1
    s = montar_sessao(p)
    try:
        inc = s.banco.consultar("SELECT * FROM incidentes WHERE severidade='critico'")
        assert inc and "risco_nao_aprovado" in inc[0]["detalhe"]
        assert all("risco_nao_aprovado" in c.rotulos for c in s.orq.contas.values())
        assert s.manifesto.hashes["risco"] == "" and s.manifesto.hashes["risco_aplicado"] == "padrao_nao_aprovado"
    finally:
        s.banco.fechar()


def test_risco_nao_aprovado_ao_vivo_propaga_erro(tmp_path):
    p = ParamsSimulacao(saida=tmp_path / "n", risco=tmp_path / "inexistente.toml", aprovacao=tmp_path / "x.toml")
    with pytest.raises(ErroConfig):
        carregar_risco_simulacao(p, WIN, ao_vivo=True)
    from robowin.core.relogio import RelogioReal
    from robowin.decisao.simulacao import abrir_banco
    from robowin.modelos.microestrutura import ModeloMicroestrutura
    rel = RelogioReal()
    banco, registro, _, _ = abrir_banco(p.saida, rel, "n1")
    try:
        with pytest.raises(ErroConfig):
            construir_orquestrador(p, rel=rel, registro=registro, ins=WIN.instrumento("WINZ26"),
                                   modelo=ModeloMicroestrutura("logistica"), statarb=None, precos_aux=None,
                                   sintetico=False, modo_tempo="real", sessao_id="n1", t_ref_ns=rel.agora_ns())
    finally:
        banco.fechar()


# --- 11: n_alvo derivado do MDE -----------------------------------------------------------------------------
def test_n_alvo_do_manifesto_vem_de_n_expectativa(tmp_path):
    p = ParamsSimulacao(n_ticks=1500, saida=tmp_path / "s")
    s = montar_sessao(p)
    try:
        assert s.manifesto.n_alvo == n_expectativa(35.0, 5.0) == 385 and s.manifesto.mde_brl == 5.0
    finally:
        s.banco.fechar()
    p2 = ParamsSimulacao(n_ticks=1500, saida=tmp_path / "s2", sigma_brl=20.0, mde_brl=10.0)
    s2 = montar_sessao(p2)
    try:
        assert s2.manifesto.n_alvo == n_expectativa(20.0, 10.0)
        assert s2.manifesto.origem_dados == "sinteticos"
    finally:
        s2.banco.fechar()

