from perfis_teste import WIN

from robowin.core.tipos import ConfigRisco, Lado
from robowin.risco.antimanipulacao import LimitesAntimanipulacao, MonitorAntimanipulacao
from robowin.risco.estresse import imprimir_estresse, rodar_estresse

S = int(1e9)


# ---- estresse -------------------------------------------------------------------------------------
def test_estresse_gap_10pct_perda_em_reais_e_limites(capsys):
    cfg, ins = ConfigRisco(), WIN.instrumento("WINZ26")
    res = {r.cenario: r for r in rodar_estresse(cfg, ins, preco=130_000.0)}
    r = res["gap_ate_limite_10pct"]
    assert r.pontos == 13_000 and abs(r.perda_brl - 2600.0) < 1e-6  # 13000 pts * R$0,20
    assert r.x_perda_operacao > 40 and set(r.excede) == {"perda_max_operacao", "perda_max_dia", "perda_max_semana", "dd_max"}
    cb = res["circuit_breaker_10pct_halt_30min_reabre_-2pct"]
    assert abs(cb.perda_brl - 3120.0) < 1e-6 and cb.perda_brl > r.perda_brl
    assert res["gap_2pct"].perda_brl > cfg.perda_max_operacao_brl  # mesmo 2% estoura o limite por operacao
    assert res["gap_2pct"].perda_com_stop_brl == 70.0  # referencia: stop+folga sem gap
    txt = imprimir_estresse(cfg, ins)
    out = capsys.readouterr().out
    assert "2600.00" in out and "gap_ate_limite_10pct" in out and "R$" in out and txt in out


def test_estresse_escala_com_quantidade():
    cfg, ins = ConfigRisco(), WIN.instrumento("WINZ26")
    r1 = rodar_estresse(cfg, ins, quantidade=1)[2]
    r2 = rodar_estresse(cfg, ins, quantidade=2)[2]
    assert abs(r2.perda_brl - 2 * r1.perda_brl) < 1e-9


# ---- antimanipulacao ----------------------------------------------------------------------------------
def test_tempo_minimo_no_livro():
    m = MonitorAntimanipulacao()
    m.registrar_nova("o1", "b", Lado.COMPRA, 10 * S)
    assert m.pode_cancelar("o1", 11 * S) == (False, "tempo_minimo_no_livro")
    assert m.pode_cancelar("o1", 12 * S)[0]
    m.registrar_cancelamento("o1", int(10.5 * S))  # cancelou cedo demais mesmo assim: gera alerta
    assert any(a.tipo == "tempo_minimo_no_livro" for a in m.alertas)


def test_mensagens_por_minuto():
    m = MonitorAntimanipulacao(LimitesAntimanipulacao(mensagens_min_max=5))
    for k in range(5):
        assert m.pode_enviar("b", k * S)[0]
        m.registrar_nova(f"o{k}", "b", Lado.COMPRA, k * S)
    assert m.pode_enviar("b", 10 * S) == (False, "mensagens_por_minuto")
    assert m.pode_enviar("outro", 10 * S)[0]  # por braco
    assert m.pode_enviar("b", 70 * S)[0]  # janela de 1 min passou


def test_taxa_de_cancelamento():
    m = MonitorAntimanipulacao(LimitesAntimanipulacao(taxa_cancelamento_max=0.5, amostra_min_taxa=4,
                                                      mensagens_min_max=1000))
    t = 0
    for k in range(6):
        m.registrar_nova(f"o{k}", "b", Lado.COMPRA, t)
        t += 3 * S
    for k in range(3):
        m.registrar_cancelamento(f"o{k}", t)
    assert m.taxa_cancelamento("b", t) == 0.5 and not m.bloqueado("b", t)
    m.registrar_cancelamento("o3", t)
    assert m.taxa_cancelamento("b", t) > 0.5 and m.bloqueado("b", t)
    assert any(a.tipo == "taxa_de_cancelamento" for a in m.alertas)


def test_cancelamentos_no_lado_oposto_a_execucoes_em_10_min():
    m = MonitorAntimanipulacao(LimitesAntimanipulacao(cancel_oposto_10min_max=2, mensagens_min_max=1000))
    m.registrar_nova("x", "b", Lado.COMPRA, 0)
    m.registrar_execucao("x", "b", Lado.COMPRA, 5 * S)
    for k in range(3):  # vendas passivas repetidamente canceladas apos compra executada
        m.registrar_nova(f"v{k}", "b", Lado.VENDA, (10 + 5 * k) * S)
        m.registrar_cancelamento(f"v{k}", (13 + 5 * k) * S)
    assert m.cancelamentos_lado_oposto("b", 30 * S) == 3
    assert any(a.tipo == "cancelamentos_lado_oposto" for a in m.alertas)
    assert m.bloqueado("b", 30 * S)
    assert m.cancelamentos_lado_oposto("b", 30 * 60 * S) == 0  # fora da janela de 10 min


def test_limites_internos_conservadores():
    L = LimitesAntimanipulacao()
    assert L.mensagens_min_max <= 20 and L.tempo_min_livro_s >= 2.0 and L.taxa_cancelamento_max <= 0.5
