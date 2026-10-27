from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from robowin.core.protocolos import ClassificadorNoticias
from robowin.core.tipos import ClassificacaoNoticia, EventoNoticia
from robowin.ia.provedores.falso import ProvedorFalso
from robowin.noticias.calendario import CalendarioEventos, EventoAgendado, bls_emprego, carregar_calendario
from robowin.noticias.classificador import NEUTRA, ClassificadorLLM

S = 1_000_000_000
MIN = 60 * S
BSB = ZoneInfo("America/Sao_Paulo")


def _brasilia(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).astimezone(BSB)


def test_bls_08h30_et_muda_de_0930_para_1030_em_brasilia_no_fim_do_horario_de_verao_dos_eua():
    antes = EventoAgendado("BLS", "payroll", date(2026, 10, 30), time(8, 30), "America/New_York")
    depois = EventoAgendado("BLS", "payroll", date(2026, 11, 6), time(8, 30), "America/New_York")
    assert _brasilia(antes.t_ns).strftime("%H:%M") == "09:30"
    assert _brasilia(depois.t_ns).strftime("%H:%M") == "10:30"
    assert depois.horario_em("America/Sao_Paulo").strftime("%H:%M") == "10:30"
    # o dia da virada (domingo 01/11/2026): 02:00 EDT volta para 01:00 EST
    assert datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("America/New_York")).utcoffset().total_seconds() == -5 * 3600
    assert datetime(2026, 10, 31, 12, 0, tzinfo=ZoneInfo("America/New_York")).utcoffset().total_seconds() == -4 * 3600


def test_bls_emprego_primeira_sexta():
    ev = bls_emprego(2026, 11)
    assert ev.data_local == date(2026, 11, 6) and ev.hora_local == time(8, 30) and not ev.verificado
    assert bls_emprego(2026, 10).data_local == date(2026, 10, 2)


def test_janela_de_bloqueio_antes_e_depois():
    ev = EventoAgendado("BCB", "Copom", date(2026, 11, 4), time(18, 30), "America/Sao_Paulo", antes_min=10, depois_min=15)
    cal = CalendarioEventos([ev])
    t = ev.t_ns
    assert cal.em_bloqueio(t - 11 * MIN) is None
    assert cal.em_bloqueio(t - 10 * MIN) is ev
    assert cal.em_bloqueio(t) is ev
    assert cal.em_bloqueio(t + 15 * MIN) is ev
    assert cal.em_bloqueio(t + 15 * MIN + 1) is None
    assert ev.janela_bloqueio_ns == (t - 10 * MIN, t + 15 * MIN)


def test_bloqueio_respeita_fuso_de_origem_do_evento():
    # FOMC 14:00 ET em 28/10/2026 (EDT) = 15:00 de Brasilia
    ev = EventoAgendado("FOMC", "decisao", date(2026, 10, 28), time(14, 0), "America/New_York")
    assert _brasilia(ev.t_ns).strftime("%H:%M") == "15:00"
    cal = CalendarioEventos([ev])
    t_bsb_1500 = int(datetime(2026, 10, 28, 15, 0, tzinfo=BSB).timestamp()) * S
    assert cal.em_bloqueio(t_bsb_1500) is ev
    t_bsb_1400 = int(datetime(2026, 10, 28, 14, 0, tzinfo=BSB).timestamp()) * S
    assert cal.em_bloqueio(t_bsb_1400) is None  # 14:00 de Brasilia nao e 14:00 ET


def test_calendario_real():
    cal = carregar_calendario("config/fontes.toml")
    assert len(cal) >= 5
    assert {e.orgao for e in cal.todos()} >= {"FOMC", "BCB", "BLS", "IBGE"}
    assert all(e.verificado is False for e in cal.todos())  # tudo a confirmar pelo usuario
    nfp = next(e for e in cal.todos() if e.orgao == "BLS")
    assert _brasilia(nfp.t_ns).strftime("%Y-%m-%d %H:%M") == "2026-11-06 10:30"
    # evento NAO verificado continua bloqueando (falha segura)
    assert cal.em_bloqueio(nfp.t_ns) is nfp
    ts = [e.t_ns for e in cal.todos()]
    assert ts == sorted(ts)
    assert cal.proximos(nfp.t_ns, 2)[0] is nfp


def test_orgao_desconhecido_recusado():
    with pytest.raises(ValueError):
        carregar_calendario({"calendario": [{"orgao": "XYZ", "nome": "n", "data": "2026-11-04", "hora": "10:00",
                                              "fuso": "UTC"}]})


# ---------------- classificador ----------------
def _ev(titulo="Selic cai 50 pontos-base", id_="e1"):
    return EventoNoticia(id=id_, fonte="f", titulo=titulo, t_publicado_ns=S, t_disponivel_ns=S, t_recebido_ns=S)


CLASSE = ClassificacaoNoticia(relevancia=0.9, direcao="alta", intensidade=0.6, horizonte_min=60, tipo="juros", confianca=0.8)


def test_classificador_implementa_o_protocolo_e_usa_o_provedor():
    prov = ProvedorFalso(roteiro=[CLASSE])
    c = ClassificadorLLM(prov, "claude-haiku-5-5")
    x: ClassificadorNoticias = c  # noqa: F841 - conformidade estrutural (mypy)
    assert callable(c.classificar)
    r = c.classificar(_ev())
    assert r == CLASSE and c.ultima_chamada.papel == "classificador_nlp" and c.ultima_chamada.valida
    assert c.classificar(_ev()) == CLASSE and prov.chamadas == 1  # cache por id de evento


async def test_classificador_funciona_dentro_de_um_loop_em_execucao():
    c = ClassificadorLLM(ProvedorFalso(roteiro=[CLASSE]), "m")
    assert c.classificar(_ev()) == CLASSE
    assert await c.aclassificar(_ev("e2")) == CLASSE


@pytest.mark.parametrize("roteiro", [["lixo"], [RuntimeError("429")]])
def test_classificador_falha_segura_vira_neutra(roteiro):
    c = ClassificadorLLM(ProvedorFalso(roteiro=roteiro), "m")
    r = c.classificar(_ev())
    assert r == NEUTRA and r.relevancia == 0 and r.confianca == 0 and r.direcao == "neutra"
    assert not c.ultima_chamada.valida


def test_classificador_timeout():
    c = ClassificadorLLM(ProvedorFalso(atraso_s=5.0), "m", prazo_s=0.05)
    assert c.classificar(_ev()) == NEUTRA and c.ultima_chamada.erro == "timeout"


def test_classificador_chama_callback_de_auditoria_e_o_texto_vai_ao_modelo():
    visto = []
    prov = ProvedorFalso(roteiro=[CLASSE])
    c = ClassificadorLLM(prov, "m", ao_chamar=lambda ev, ch: visto.append((ev.id, ch.papel)))
    c.classificar(_ev(id_="zzz"))
    assert visto == [("zzz", "classificador_nlp")]
