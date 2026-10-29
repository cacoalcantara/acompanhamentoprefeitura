"""Gatilhos do braco planejador (docs/16 WP5): eventos de gestao, reavaliacao programada, plano terminado,
eventos de posicao permitidos e a reserva de chamadas para quem tem posicao."""
from __future__ import annotations

from robowin.decisao.gatilhos import ConfigGatilhos, EstadoGatilho, GerenciadorGatilhos

NS = 1_000_000_000
T0 = 1_800_000_000 * NS  # dia qualquer; o teto diario e por dia de Sao Paulo


def _g(**kw) -> GerenciadorGatilhos:
    base = {"periodo_s": 240.0, "intervalo_min_s": 10.0, "max_chamadas_dia": None}
    base.update(kw)
    return GerenciadorGatilhos(ConfigGatilhos(**base))


def _nomes(g, **kw):
    # segundos_restantes=0: o periodo nao e espalhado (so periodo_s e intervalo_min_s valem)
    return [d.nome for d in g.avaliar(EstadoGatilho(**{"t_ns": T0 + 30 * NS, "segundos_restantes": 0.0, **kw}))]


def test_padroes_reproduzem_o_comportamento_atual():
    c = ConfigGatilhos()
    assert c.eventos_posicao_permitidos == ("abriu", "fechou") and c.reserva_gestao_frac == 0.0
    assert c.perto_frac == 0.3
    e = EstadoGatilho(t_ns=0)
    assert e.eventos_gestao == () and e.reavaliacao_ns is None and not e.com_posicao and not e.plano_terminou


def test_eventos_de_gestao_viram_disparos_sem_repetir():
    g = _g()
    g.marcar_chamada(T0)
    assert _nomes(g, eventos_gestao=("perto_do_stop", "regra_disparou", "perto_do_stop")) == [
        "gestao:perto_do_stop", "gestao:regra_disparou"]


def test_reavaliacao_programada_e_plano_terminou():
    g = _g()
    g.marcar_chamada(T0)
    assert _nomes(g, reavaliacao_ns=T0 + 40 * NS) == []  # ainda nao chegou a hora
    assert _nomes(g, reavaliacao_ns=T0 + 30 * NS) == ["reavaliacao_programada"]
    assert _nomes(g, plano_terminou=True) == ["plano_terminou"]


def test_reavaliacao_nunca_fura_o_intervalo_minimo_nem_o_teto():
    g = _g(intervalo_min_s=20.0)
    g.marcar_chamada(T0)
    assert g.avaliar(EstadoGatilho(t_ns=T0 + 5 * NS, reavaliacao_ns=T0, eventos_gestao=("perto_do_alvo",))) == ()
    g2 = _g(max_chamadas_dia=1)
    g2.marcar_chamada(T0)
    assert g2.avaliar(EstadoGatilho(t_ns=T0 + 60 * NS, reavaliacao_ns=T0, com_posicao=True)) == ()
    assert g2.bloqueios_orcamento == 1


def test_evento_abriu_desligado_pela_config():
    g = _g(eventos_posicao_permitidos=("fechou",))
    g.marcar_chamada(T0)
    assert _nomes(g, eventos_posicao=("abriu",)) == []
    assert _nomes(g, eventos_posicao=("abriu", "fechou")) == ["evento_posicao"]
    d = g.avaliar(EstadoGatilho(t_ns=T0 + 30 * NS, eventos_posicao=("abriu", "fechou")))
    assert d[0].detalhe == "fechou"


def test_reserva_de_gestao_so_vale_sem_posicao():
    g = _g(max_chamadas_dia=10, reserva_gestao_frac=0.3, intervalo_min_s=1.0, periodo_s=1.0)
    for i in range(7):  # sem posicao: so 70% das restantes a cada instante
        t = T0 + (i + 1) * 10 * NS
        assert _nomes(g, t_ns=t) == ["periodico"]
        g.marcar_chamada(t)
    assert g.chamadas_restantes(T0 + 80 * NS) == 3
    assert g.chamadas_utilizaveis(T0 + 80 * NS, com_posicao=False) == 2
    # restam 3: floor(0.7 x 3) = 2 sem posicao; com posicao, todas
    for i in range(2):
        t = T0 + (8 + i) * 10 * NS
        assert _nomes(g, t_ns=t) == ["periodico"]
        g.marcar_chamada(t)
    t = T0 + 100 * NS
    assert _nomes(g, t_ns=t) == []  # 1 restante: floor(0.7) = 0 -> reservada
    assert _nomes(g, t_ns=t, com_posicao=True, eventos_gestao=("perto_do_stop",)) == ["gestao:perto_do_stop"]


def test_reserva_espalha_o_periodo_sem_posicao():
    g = _g(max_chamadas_dia=100, reserva_gestao_frac=0.5, periodo_s=1.0, intervalo_min_s=1.0)
    sem = g.intervalo_periodico_s(T0, 1000.0, com_posicao=False)
    com = g.intervalo_periodico_s(T0, 1000.0, com_posicao=True)
    assert sem == 1000.0 / 50 and com == 1000.0 / 100
