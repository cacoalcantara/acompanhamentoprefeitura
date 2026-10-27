from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import EventoNoticia
from robowin.noticias.carregador import carregar_fontes
from robowin.noticias.dedup import Deduplicador, normalizar
from robowin.noticias.fontes.http_json import FonteHttpJson
from robowin.noticias.fontes.manual import FonteManual
from robowin.noticias.fontes.rss import FonteRSS
from robowin.noticias.tempo import para_ns_utc

S = 1_000_000_000


def _ns(y, mo, d, h, mi, tz):
    return int(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC).timestamp()) * S


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Copom mantem a Selic</title><pubDate>Wed, 04 Nov 2026 18:30:00 -0300</pubDate><guid>a1</guid></item>
<item><title>Payroll surpreende</title><pubDate>Fri, 06 Nov 2026 08:30:00 -0500</pubDate><guid>a2</guid></item>
<item><title>Sem data</title></item>
<item><title>Data quebrada</title><pubDate>ontem</pubDate></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Fed comunica</title>
<updated>2026-10-28T14:00:00-04:00</updated><id>urn:x1</id></entry></feed>"""


def _ev(id_, titulo, t_disp):
    return EventoNoticia(id=id_, fonte="f", titulo=titulo, t_publicado_ns=t_disp, t_disponivel_ns=t_disp, t_recebido_ns=t_disp)


# ---------- fusos ----------
def test_para_ns_utc_respeita_fuso_de_origem():
    # 08:30 em Nova York em 06/11/2026 (EST, UTC-5) = 13:30 UTC = 10:30 em Brasilia
    ns = para_ns_utc("2026-11-06T08:30:00", "America/New_York")
    assert ns == _ns(2026, 11, 6, 13, 30, "UTC")
    # a mesma hora local, antes do fim do horario de verao (EDT, UTC-4), cai em 09:30 de Brasilia
    ns2 = para_ns_utc("2026-10-30T08:30:00", "America/New_York")
    assert ns2 == _ns(2026, 10, 30, 9, 30, "America/Sao_Paulo")
    assert ns == _ns(2026, 11, 6, 10, 30, "America/Sao_Paulo")


def test_offset_explicito_vence_o_fuso_de_origem():
    assert para_ns_utc("2026-11-04T18:30:00-03:00", "America/New_York") == _ns(2026, 11, 4, 18, 30, "America/Sao_Paulo")
    assert para_ns_utc("2026-11-04T21:30:00Z", "America/New_York") == _ns(2026, 11, 4, 18, 30, "America/Sao_Paulo")


def test_epoch_s_ms_ns():
    assert para_ns_utc(1_700_000_000) == 1_700_000_000 * S
    assert para_ns_utc(1_700_000_000_000) == 1_700_000_000 * S
    assert para_ns_utc(1_700_000_000 * S) == 1_700_000_000 * S


# ---------- RSS ----------
def test_rss_converte_datas_e_ignora_itens_sem_data():
    rel = RelogioSimulado(_ns(2026, 11, 4, 18, 31, "America/Sao_Paulo"))
    f = FonteRSS("bcb", "http://x", verificado=True, transporte=lambda u: RSS, relogio=rel)
    evs = f.coletar()
    assert [e.titulo for e in evs] == ["Copom mantem a Selic", "Payroll surpreende"]
    a, b = evs
    assert a.t_publicado_ns == _ns(2026, 11, 4, 18, 30, "America/Sao_Paulo")
    assert b.t_publicado_ns == _ns(2026, 11, 6, 10, 30, "America/Sao_Paulo")
    assert a.t_recebido_ns == rel.agora_ns() and a.t_disponivel_ns == rel.agora_ns()  # publicado < recebido
    assert b.t_disponivel_ns == b.t_publicado_ns  # "publicado no futuro" nao fica disponivel antes de existir
    assert a.verificado and a.fonte == "bcb" and a.id != b.id


def test_rss_atom_com_fuso_de_origem_na_data_sem_offset():
    atom_sem_offset = ATOM.replace("-04:00", "")
    f = FonteRSS("fed", "http://x", fuso_origem="America/New_York", verificado=True,
                 transporte=lambda u: atom_sem_offset, relogio=RelogioSimulado(0))
    (e,) = f.coletar()
    assert e.t_publicado_ns == _ns(2026, 10, 28, 14, 0, "America/New_York")


def test_rss_xml_quebrado_nao_levanta():
    f = FonteRSS("x", "http://x", verificado=True, transporte=lambda u: "<rss><item>", relogio=RelogioSimulado(0))
    assert f.coletar() == []


# ---------- fontes nao verificadas desligadas ----------
def test_fonte_nao_verificada_fica_desligada_e_nao_faz_requisicao():
    chamadas = []
    f = FonteRSS("x", "http://x", transporte=lambda u: chamadas.append(u) or RSS, relogio=RelogioSimulado(0))
    assert f.verificado is False and f.ativa is False
    assert f.coletar() == [] and chamadas == []
    f2 = FonteRSS("x", "http://x", permitir_nao_verificada=True, transporte=lambda u: chamadas.append(u) or RSS,
                  relogio=RelogioSimulado(0))
    evs = f2.coletar()
    assert len(evs) == 2 and chamadas == ["http://x"] and all(not e.verificado for e in evs)


def test_config_real_tem_todas_as_fontes_nao_verificadas_e_nada_e_carregado():
    assert carregar_fontes("config/fontes.toml") == []
    todas = carregar_fontes("config/fontes.toml", permitir_nao_verificadas=True, relogio=RelogioSimulado(0),
                            transporte=lambda u: "")
    assert len(todas) >= 3 and all(f.verificado is False and f.ativa for f in todas)
    assert all(f.coletar() == [] for f in todas)  # transporte vazio => nada, sem erro


def test_carregador_so_liga_as_verificadas():
    cfg = {"fonte": [
        {"tipo": "rss", "nome": "a", "url": "u", "verificado": True},
        {"tipo": "rss", "nome": "b", "url": "u", "verificado": False},
        {"tipo": "http_json", "nome": "c", "url": "u", "verificado": True, "campo_lista": "x"},
        {"tipo": "rss", "nome": "d", "url": "u"},
    ]}
    assert [f.nome for f in carregar_fontes(cfg)] == ["a", "c"]
    with pytest.raises(ValueError):
        carregar_fontes({"fonte": [{"tipo": "ftp", "nome": "z", "url": "u", "verificado": True}]})


# ---------- http_json ----------
def test_http_json_mapeia_campos_e_fuso():
    corpo = '{"dados": {"itens": [{"id": 7, "t": {"texto": "IPCA sobe"}, "quando": "09/11/2026 09:00"}, {"id": 8}]}}'
    f = FonteHttpJson("ibge", "http://x", campo_lista="dados.itens", campo_titulo="t.texto", campo_data="quando",
                      campo_id="id", formato_data="%d/%m/%Y %H:%M", verificado=True, transporte=lambda u: corpo,
                      relogio=RelogioSimulado(0))
    (e,) = f.coletar()
    assert e.titulo == "IPCA sobe" and e.t_publicado_ns == _ns(2026, 11, 9, 9, 0, "America/Sao_Paulo")


def test_http_json_invalido():
    f = FonteHttpJson("x", "u", verificado=True, transporte=lambda u: "nao e json", relogio=RelogioSimulado(0))
    assert f.coletar() == []
    f2 = FonteHttpJson("x", "u", campo_lista="a", verificado=True, transporte=lambda u: '{"a": 3}', relogio=RelogioSimulado(0))
    assert f2.coletar() == []


# ---------- manual ----------
def test_fonte_manual_sempre_ativa_e_nao_verificada_por_padrao():
    rel = RelogioSimulado(_ns(2026, 11, 4, 20, 0, "America/Sao_Paulo"))
    m = FonteManual(relogio=rel)
    assert m.verificado is False and m.ativa
    e = m.adicionar("Selic mantida", "2026-11-04 18:30:00" .replace(" ", "T"))
    assert e.t_publicado_ns == _ns(2026, 11, 4, 18, 30, "America/Sao_Paulo") and not e.verificado
    assert e.t_disponivel_ns == rel.agora_ns()
    e2 = m.adicionar("Historica", "2026-10-01T10:00:00", disponivel="2026-10-01T10:00:30", verificado=True)
    assert e2.t_disponivel_ns - e2.t_publicado_ns == 30 * S and e2.verificado
    assert len(m.coletar()) == 2 and m.coletar() == []


# ---------- dedup ----------
def test_dedup_por_id_titulo_normalizado_e_quase_igual():
    d = Deduplicador(janela_s=3600, limiar=0.8)
    a = _ev("1", "Copom mantém a Selic em 10,50%", 100 * S)
    b = _ev("2", "COPOM MANTEM A SELIC EM 10,50%!", 200 * S)         # mesmo titulo normalizado
    c = _ev("3", "Copom mantem a Selic em 10,50% diz BC", 300 * S)    # quase igual (Jaccard alto)
    e = _ev("1", "Outro texto totalmente diferente", 400 * S)         # mesmo id
    f = _ev("4", "Dolar fecha em alta apos payroll", 500 * S)
    out = d.filtrar([f, e, c, b, a])  # fora de ordem de proposito
    assert [x.id for x in out] == ["1", "4"]
    assert out[0] is a  # fica o primeiro a ficar disponivel


def test_dedup_janela_expira():
    d = Deduplicador(janela_s=60)
    assert d.filtrar([_ev("1", "Mesma manchete", 0)])
    assert d.filtrar([_ev("2", "Mesma manchete", 30 * S)]) == []
    assert len(d.filtrar([_ev("3", "Mesma manchete", 500 * S)])) == 1  # fora da janela: noticia nova


def test_dedup_entre_fontes_e_estado_persistente():
    d = Deduplicador()
    a = EventoNoticia(id="x", fonte="rss1", titulo="Fed mantem juros", t_publicado_ns=S, t_disponivel_ns=S, t_recebido_ns=S)
    b = EventoNoticia(id="y", fonte="rss2", titulo="Fed mantém juros", t_publicado_ns=2 * S, t_disponivel_ns=2 * S, t_recebido_ns=2 * S)
    assert d.filtrar([a]) == [a] and d.filtrar([b]) == []


def test_normalizar():
    assert normalizar("Fed MANTÉM os juros!") == normalizar("fed mantem juros")
