import datetime as dt

import numpy as np
from perfis_teste import WIN

from robowin.cli import main
from robowin.core.tipos import LivroSnapshot, NivelBook, Tick
from robowin.dados import (
    ROTULO_SINTETICO,
    ArmazenamentoTicks,
    ConfigQualidade,
    ConfigSintetico,
    gerar,
    verificar,
    verificar_livros,
    verificar_ticks,
)
from robowin.dados.sintetico import gerar_dias
from robowin.dados.ticks import dia_ns

T0 = dia_ns(dt.date(2026, 3, 10)) + 10 * 3600 * 10**9
S = 10**9


def tk(seg, bid=130000.0, ask=None, last=None, ms=None, vol=1.0):
    t = T0 + int(seg * S)
    ask = bid + 5 if ask is None else ask
    return Tick(t_servidor_ms=t // 10**6 if ms is None else ms, t_utc_ns=t, t_recebido_ns=t, bid=bid, ask=ask,
                last=bid + 5 if last is None else last, volume=vol, flags=0)


def base(n=20):
    return [tk(i, vol=i + 1) for i in range(n)]


def test_limpo():
    r = verificar_ticks(base())
    assert r.ok and not r.bloqueia_entradas


def test_nao_monotonico():
    t = base()
    t[5], t[6] = t[6], t[5]
    r = verificar_ticks(t)
    assert "nao_monotonico" in r.tipos() and r.bloqueia_entradas


def test_duplicata():
    t = base() + [base()[3]]
    t = sorted(t, key=lambda x: x.t_utc_ns)
    r = verificar_ticks(t)
    assert "duplicata" in r.tipos() and not r.bloqueia_entradas


def test_lacunas_aviso_e_critico():
    cfg = ConfigQualidade()
    assert [a.severidade for a in verificar_ticks([tk(0), tk(90)], cfg).achados] == ["aviso"]
    r = verificar_ticks([tk(0), tk(400)], cfg)
    assert r.achados[0].tipo == "lacuna" and r.bloqueia_entradas
    # lacuna entre dias ou fora do pregao e ignorada
    assert verificar_ticks([tk(0), tk(86400)], cfg).ok
    assert verificar_ticks([tk(-3600 * 5), tk(0)], cfg).ok


def test_feed_parado():
    t = base()
    assert verificar_ticks(t, agora_ns=t[-1].t_utc_ns + 5 * S).ok
    r = verificar_ticks(t, agora_ns=t[-1].t_utc_ns + 120 * S)
    assert "feed_parado" in r.tipos() and r.bloqueia_entradas
    assert "feed_parado" in verificar_ticks([], agora_ns=1).tipos()


def test_book_cruzado_ticks_e_livro():
    r = verificar_ticks([tk(0), tk(1, bid=130000, ask=130000)])
    assert "book_cruzado" in r.tipos() and r.bloqueia_entradas
    liv = LivroSnapshot(t_utc_ns=T0, niveis=(NivelBook(preco=130005, volume=1, lado="compra"),
                                             NivelBook(preco=130000, volume=1, lado="venda")))
    rl = verificar_livros([liv])
    assert "book_cruzado" in rl.tipos() and rl.bloqueia_entradas
    uni = LivroSnapshot(t_utc_ns=T0, niveis=(NivelBook(preco=1, volume=1, lado="compra"),))
    assert verificar_livros([uni]).tipos() == {"livro_unilateral"}
    ok = LivroSnapshot(t_utc_ns=T0, niveis=(NivelBook(preco=1, volume=1, lado="compra"),
                                            NivelBook(preco=2, volume=1, lado="venda")))
    assert verificar_livros([ok, ok.model_copy(update={"t_utc_ns": T0 + 1})]).ok
    assert "nao_monotonico" in verificar_livros([ok.model_copy(update={"t_utc_ns": T0 + 5}), ok]).tipos()


def test_saltos_configuraveis():
    t = [tk(0, 130000), tk(1, 130150), tk(2, 130160)]
    r = verificar_ticks(t)  # tunel padrao 100
    assert r.tipos() == {"salto"} and not r.bloqueia_entradas
    assert verificar_ticks(t, ConfigQualidade(tunel_pontos=200)).ok
    assert verificar_ticks([tk(0, 130000), tk(1, 131000)]).bloqueia_entradas


def test_relatorio_sintetico_propaga():
    r = verificar(base(), [], sintetico=True)
    assert r.sintetico and "SINTETICO" in r.texto()


# ---------------- sintetico ----------------
def test_determinismo_e_rotulo():
    a = gerar(ConfigSintetico.do_perfil(WIN, seed=7, n_ticks=3000))
    b = gerar(ConfigSintetico.do_perfil(WIN, seed=7, n_ticks=3000))
    c = gerar(ConfigSintetico.do_perfil(WIN, seed=8, n_ticks=3000))
    assert a.hash() == b.hash() != c.hash()
    assert a.sintetico is True and a.rotulo == ROTULO_SINTETICO
    rel = a.relatorio()
    assert rel["sintetico"] is True and rel["rotulo"] == ROTULO_SINTETICO


def test_propriedades_do_sintetico():
    d = gerar(ConfigSintetico.do_perfil(WIN, seed=1, n_ticks=20000))
    assert len(d.ticks) == 20000 and d.livros
    assert all(t.bid % 5 == 0 and t.ask > t.bid and t.flags & (32 | 64) for t in d.ticks)
    ms = [t.t_servidor_ms for t in d.ticks]
    assert ms == sorted(ms) and len(set(ms)) == len(ms)
    assert verificar(d.ticks, d.livros, sintetico=True).ok


def test_sinal_injetado_detectavel():
    def corr(forca):
        d = gerar(ConfigSintetico.do_perfil(WIN, seed=3, n_ticks=20000, sinal_forca=forca, livro_a_cada_ticks=0))
        s = np.array([1.0 if t.flags & 32 else -1.0 for t in d.ticks])
        return float(np.corrcoef(s[:-1], s[1:])[0, 1])
    assert abs(corr(0.0)) < 0.03
    assert 0.12 < corr(0.1) < 0.28  # esperado 2*0.1 = 0.2


def test_gaps_saltos_lacunas():
    d = gerar(ConfigSintetico.do_perfil(WIN, seed=2, n_ticks=8000, n_saltos=2, salto_pts=1500, n_lacunas=2, lacuna_s=400,
                              gap_abertura_pts=2000, livro_a_cada_ticks=0))
    assert len(d.ticks) < 8000
    assert d.ticks[0].bid > 131000
    tipos = verificar_ticks(d.ticks).tipos()
    assert {"salto", "lacuna"} <= tipos


def test_gerar_dias_pula_fim_de_semana():
    ds = gerar_dias(ConfigSintetico.do_perfil(WIN, data=dt.date(2026, 3, 13), n_ticks=200), 3)  # sexta
    datas = [x.config.data for x in ds]
    assert datas == [dt.date(2026, 3, 13), dt.date(2026, 3, 16), dt.date(2026, 3, 17)]


# ---------------- CLI ----------------
def test_cli_resumo_verificar(tmp_path, capsys):
    arm = ArmazenamentoTicks(tmp_path)
    gerar(ConfigSintetico.do_perfil(WIN, seed=5, n_ticks=2000)).gravar(arm)
    assert main(["dados", "resumo", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "SINTETICO" in out and "SINT_WIN" in out and "ticks=2000" in out
    assert main(["dados", "verificar", "--dir", str(tmp_path)]) == 0
    arm.gravar("RUIM", [tk(0), tk(1, bid=130000, ask=129990)])
    assert main(["dados", "verificar", "--dir", str(tmp_path)]) == 1
    assert "CRITICO" in capsys.readouterr().out
    assert main(["dados", "sintetico", "--dir", str(tmp_path / "s"), "--n-ticks", "100"]) == 0
