"""Maquina do Tempo no ``robowin nucleo``: piscina CAUSAL (so pregoes anteriores ao corrente, mesmo ativo), sem
dados => sem avaliador (comportamento anterior) com motivo informativo, mistura sintetico/real => recusa."""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from perfis_teste import INS_WIN, WIN

from robowin.avaliacao import maquina_do_tempo as mt
from robowin.core.calendario_b3 import SP
from robowin.core.tipos import Lado, Tick
from robowin.dados.ticks import ArmazenamentoTicks, dia_ns
from robowin.decisao import nucleo as nu
from robowin.decisao.odds import AvaliadorOdds, ConfigOdds

NS = 1_000_000_000
HOJE = dt.date(2026, 6, 10)  # quarta-feira
SIMBOLO = INS_WIN.simbolo  # simbolo "real" do perfil de teste (o literal vive em perfis_teste)


def _dias_uteis(antes: dt.date, n: int) -> list[dt.date]:
    out, x = [], antes
    while len(out) < n:
        x -= dt.timedelta(days=1)
        if x.weekday() < 5:
            out.append(x)
    return sorted(out)


def _ticks_do_dia(d: dt.date, base: float, passo: int) -> list[Tick]:
    """Um tick por barra de 5 min (sessao completa na grade do perfil); preco inventado, so para a piscina."""
    try:
        g = mt.grade_da_sessao(WIN, d)
    except ValueError:
        return []
    out, preco, tk = [], base, WIN.tick_pontos
    for j in range(g.n_barras):
        for k in (5, 150):
            t = dia_ns(d) + (g.abertura_s + j * g.intervalo_s + k) * NS
            preco += ((j * 7 + k + passo) % 5 - 2) * tk
            out.append(Tick(t_servidor_ms=t // 1_000_000, t_utc_ns=t, t_recebido_ns=t, bid=preco - tk, ask=preco,
                            last=preco, volume=1.0, flags=0))
    return out


def armazem(raiz, dias: list[dt.date], simbolo: str = SIMBOLO, sintetico: bool = False) -> ArmazenamentoTicks:
    arm = ArmazenamentoTicks(raiz)
    for i, d in enumerate(dias):
        arm.gravar(simbolo, _ticks_do_dia(d, 130_000.0 + 10 * i * WIN.tick_pontos, i))
    if sintetico:
        arm.marcar_sintetico()
    return arm


# ================================================================================ configuracao ==
def test_config_padrao_ligada_so_como_evidencia_e_veto_continua_desligado():
    from robowin.core.config import carregar_toml
    from robowin.decisao.odds import config_odds
    cfg = carregar_toml("config/robowin.toml")
    cn = nu.config_odds_nucleo(cfg)
    assert cn.ativo is True and cn.min_sessoes >= mt.MIN_SESSOES and cn.dados == ""
    assert config_odds(cfg).veto_por_odds is False
    assert nu.config_odds_nucleo({}).ativo is True  # chave ausente: padrao
    assert nu.ConfigOddsNucleo(min_sessoes=3).min_efetivo == mt.MIN_SESSOES  # nunca abaixo do minimo da simulacao


# ================================================================================ causalidade ==
def test_piscina_so_tem_sessoes_anteriores_ao_pregao_corrente(tmp_path, monkeypatch):
    antes = _dias_uteis(HOJE, 26)
    depois = [HOJE, HOJE + dt.timedelta(days=1), HOJE + dt.timedelta(days=2)]  # o dia corrente e o "futuro"
    armazem(tmp_path / "t", antes + depois)
    lidas: list[dt.date] = []
    orig = ArmazenamentoTicks.ler_dia

    def espia(self, simbolo, d):
        lidas.append(d)
        return orig(self, simbolo, d)

    monkeypatch.setattr(ArmazenamentoTicks, "ler_dia", espia)
    pn = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "t")
    assert pn.ligada, pn.motivo
    assert pn.piscina is not None and pn.piscina.sintetico is False
    assert all(s.data < HOJE for s in pn.piscina.sessoes)
    assert lidas and max(lidas) < HOJE  # nenhum tick do pregao corrente (nem posterior) e sequer lido
    assert len(pn.piscina.sessoes) >= mt.MIN_SESSOES
    # e a piscina serve ao avaliador no pregao corrente (evidencia disponivel ao revisor)
    t0 = int(dt.datetime(HOJE.year, HOJE.month, HOJE.day, 11, 0, tzinfo=SP).timestamp()) * NS
    av = AvaliadorOdds(WIN, pn.piscina, ConfigOdds(replays=500)).avaliar(
        t0_ns=t0, lado=Lado.COMPRA, stop_pontos=100.0, alvo_pontos=200.0, preco_ref=130_000.0)
    assert av.disponivel and av.campo["n_sessoes"] == len(pn.piscina.sessoes) and not av.veto


def test_limite_de_sessoes_pega_as_mais_recentes_anteriores(tmp_path):
    armazem(tmp_path / "t", _dias_uteis(HOJE, 30))
    pn = nu.piscina_odds_nucleo({"decisao": {"odds_nucleo_sessoes": 22}}, WIN, HOJE, raiz=tmp_path / "t")
    assert pn.ligada and pn.piscina is not None
    datas = pn.piscina.datas() + list(pn.piscina.descartadas)
    assert len(datas) == 22 and min(datas) >= _dias_uteis(HOJE, 22)[0]


# ================================================================================ sem dados => sem avaliador ==
def test_sem_diretorio_sem_simbolo_ou_poucas_sessoes_nao_liga(tmp_path):
    a = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "nao_existe")
    assert not a.ligada and a.piscina is None and "sem dados" in a.motivo and a.severidade == "info"
    (tmp_path / "vazio").mkdir()
    b = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "vazio")
    assert not b.ligada and "sem dados" in b.motivo and b.severidade == "info"
    armazem(tmp_path / "poucas", _dias_uteis(HOJE, 5))
    c = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "poucas")
    assert not c.ligada and "insuficientes" in c.motivo and c.severidade == "info"
    # so ha dados do proprio dia/futuro: tambem nao liga (nada anterior)
    armazem(tmp_path / "futuro", [HOJE + dt.timedelta(days=i) for i in range(0, 40) if
                                  (HOJE + dt.timedelta(days=i)).weekday() < 5])
    d = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "futuro")
    assert not d.ligada and d.piscina is None


def test_minimo_configuravel_e_desligar_por_config(tmp_path):
    armazem(tmp_path / "t", _dias_uteis(HOJE, 26))
    alto = nu.piscina_odds_nucleo({"decisao": {"odds_nucleo_min_sessoes": 40}}, WIN, HOJE, raiz=tmp_path / "t")
    assert not alto.ligada and "minimo 40" in alto.motivo
    off = nu.piscina_odds_nucleo({"decisao": {"odds_nucleo": False}}, WIN, HOJE, raiz=tmp_path / "t")
    assert not off.ligada and "desligada" in off.motivo


def test_outro_ativo_nao_entra_na_piscina(tmp_path):
    from perfis_teste import WDO
    armazem(tmp_path / "t", _dias_uteis(HOJE, 26), simbolo=WDO.instrumento(WDO.simbolo_base).simbolo)
    pn = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "t")
    assert not pn.ligada and "sem dados" in pn.motivo


# ================================================================================ mistura => recusa ==
def test_mistura_de_sintetico_e_real_e_recusada(tmp_path):
    dias = _dias_uteis(HOJE, 26)
    armazem(tmp_path / "t", dias)
    armazem(tmp_path / "t", dias[:3], simbolo=f"SINT_{WIN.simbolo_base}")
    pn = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "t")
    assert not pn.ligada and pn.motivo.startswith("RECUSADO") and "contaminados" in pn.motivo
    assert pn.severidade == "alerta"


def test_piscina_sintetica_com_nucleo_em_dados_reais_e_recusada(tmp_path):
    armazem(tmp_path / "s", _dias_uteis(HOJE, 26), sintetico=True)  # marcador SINTETICO.txt
    pn = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "s")
    assert not pn.ligada and pn.motivo.startswith("RECUSADO") and "mistura" in pn.motivo and pn.severidade == "alerta"
    ok = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "s", dados_sinteticos=True)
    assert ok.ligada and ok.piscina is not None and ok.piscina.sintetico is True
    # o inverso tambem: piscina real com nucleo sintetico
    armazem(tmp_path / "r", _dias_uteis(HOJE, 26))
    inv = nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "r", dados_sinteticos=True)
    assert not inv.ligada and "mistura" in inv.motivo


# ================================================================================ registro e DUBLE ==
class RegistroFalso:
    def __init__(self):
        self.incidentes: list[dict] = []

    def incidente(self, **kw):
        self.incidentes.append(kw)


def test_motivo_vai_para_incidentes(tmp_path):
    reg = RegistroFalso()
    nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "nada").registrar(reg, 123)
    (inc,) = reg.incidentes
    assert inc["origem"] == nu.ORIGEM_ODDS and inc["severidade"] == "info" and "NAO ligada" in inc["detalhe"]
    armazem(tmp_path / "t", _dias_uteis(HOJE, 26))
    nu.piscina_odds_nucleo({}, WIN, HOJE, raiz=tmp_path / "t").registrar(reg, 124)
    assert reg.incidentes[-1]["severidade"] == "info" and "ligada no nucleo" in reg.incidentes[-1]["detalhe"]


@pytest.mark.parametrize("piscina_sintetica", [False, True])
def test_gateway_duble_desliga_avaliador_com_piscina_real(piscina_sintetica):
    reg = RegistroFalso()
    pool = mt.PiscinaSessoes((), {}, mt.INTERVALO_PADRAO_S, piscina_sintetica, "teste")
    orq = SimpleNamespace(registro=reg, avaliador_odds=AvaliadorOdds(WIN, pool), _agora=lambda: 1,
                          bloqueio=SimpleNamespace(marcar=lambda *a: None))
    n = nu.Nucleo(orq, nu.FonteLista([]))  # type: ignore[arg-type]
    asyncio.run(n.tratar("estado", {"duble": True}))
    if piscina_sintetica:
        assert orq.avaliador_odds is not None  # sintetico com sintetico: pode seguir
    else:
        assert orq.avaliador_odds is None
        assert any(i["origem"] == nu.ORIGEM_ODDS and "DUBLE" in i["detalhe"] for i in reg.incidentes)
