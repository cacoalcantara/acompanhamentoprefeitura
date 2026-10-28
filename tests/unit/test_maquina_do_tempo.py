"""Maquina do Tempo: bootstrap vetorizado das sessoes anteriores (SIMULACAO, nao e previsao)."""
from __future__ import annotations

import datetime as dt
import json
import time

import numpy as np
import pytest
from perfis_teste import WIN

from robowin.avaliacao import cli_maquina
from robowin.avaliacao import maquina_do_tempo as mt
from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.tipos import Lado, Tick

TICK = WIN.tick_pontos
DIA_PLANO = dt.date(2026, 6, 10)  # quarta-feira
GRADE = mt.grade_da_sessao(WIN, DIA_PLANO)
NB = GRADE.n_barras


def _dias_uteis_antes(d: dt.date, n: int) -> list[dt.date]:
    out: list[dt.date] = []
    x = d
    while len(out) < n:
        x -= dt.timedelta(days=1)
        if x.weekday() < 5:
            out.append(x)
    return sorted(out)


def _sessao(d: dt.date, rng: np.random.Generator, base: float = 130_000.0, vol_ticks: float = 3.0,
            deriva_ticks: float = 0.0, amplitude_ticks: float = 1.0, fixa: bool = False) -> mt.SessaoBarras:
    passos = np.round(rng.normal(deriva_ticks, vol_ticks, NB)) * TICK
    c = base + np.cumsum(passos)
    o = np.concatenate([[base], c[:-1]])
    ext = (np.full((2, NB), amplitude_ticks) if fixa else np.abs(np.round(rng.normal(0.0, amplitude_ticks, (2, NB))))) * TICK
    return mt.SessaoBarras(d, o, np.maximum(o, c) + ext[0], np.minimum(o, c) - ext[1], c, GRADE)


def piscina(n: int = 30, seed: int = 1, rotulo: bool | None = True, **kw) -> mt.PiscinaSessoes:
    rng = np.random.default_rng(seed)
    ss = tuple(_sessao(d, rng, **kw) for d in _dias_uteis_antes(DIA_PLANO, n))
    return mt.PiscinaSessoes(ss, {}, GRADE.intervalo_s, rotulo, "teste")


def plano(lado: Lado = Lado.COMPRA, stop: float = 30.0, alvo: float = 60.0, **kw) -> mt.PlanoMT:
    base = dict(data=DIA_PLANO, entrada="10:00", zeragem="17:45", preco_entrada=130_000.0)
    return mt.PlanoMT(lado, stop, alvo, **{**base, **kw})


# ------------------------------------------------------------------------------------------------ barras
def _tk(d: dt.date, h: int, m: int, preco: float, s: int = 0) -> Tick:
    t = dt_utc_ns(dt.datetime(d.year, d.month, d.day, h, m, s))
    return Tick(t_servidor_ms=t // 1_000_000, t_utc_ns=t, t_recebido_ns=t, bid=preco, ask=preco + TICK, last=preco,
                volume=1.0, flags=8)


def _ticks_dia(d: dt.date, *, a_partir_de_barra: int = 0, sem_barra: int | None = None) -> list[Tick]:
    out = []
    for i in range(a_partir_de_barra, NB):
        if i == sem_barra:
            continue
        seg = GRADE.abertura_s + i * GRADE.intervalo_s
        h, m = seg // 3600, (seg % 3600) // 60
        out += [_tk(d, h, m, 130_000.0 + i * TICK, 1), _tk(d, h, m, 130_000.0 + i * TICK + 10 * TICK, 20),
                _tk(d, h, m, 130_000.0 + i * TICK - 5 * TICK, 40), _tk(d, h, m, 130_000.0 + i * TICK + TICK, 59)]
    return out


def test_barras_de_ticks_usa_a_grade_e_descarta_meio_pregao_e_gaps():
    d1, d2, d3 = dt.date(2026, 6, 1), dt.date(2026, 6, 2), dt.date(2026, 6, 3)
    ticks = _ticks_dia(d1) + _ticks_dia(d2, a_partir_de_barra=48) + _ticks_dia(d3, sem_barra=50)
    p = mt.barras_de_ticks(ticks, WIN, sintetico=True)
    assert [s.data for s in p.sessoes] == [d1]
    assert p.descartadas[d2].startswith("meio_pregao") and p.descartadas[d3].startswith("gap")
    s = p.sessoes[0]
    assert s.n == NB == 113 and s.grade.intervalo_s == 300
    # OHLC da barra 3: abertura = 1o tick (meio do book), maxima/minima/fechamento dos ticks da barra
    i = 3
    meio = lambda x: x + TICK / 2  # noqa: E731
    assert s.o[i] == meio(130_000.0 + i * TICK) and s.h[i] == meio(130_000.0 + i * TICK + 10 * TICK)
    assert s.l[i] == meio(130_000.0 + i * TICK - 5 * TICK) and s.c[i] == meio(130_000.0 + i * TICK + TICK)
    assert (s.l <= np.minimum(s.o, s.c)).all() and (s.h >= np.maximum(s.o, s.c)).all()


def test_visao_causal_da_sessao_so_tem_barras_anteriores():
    s = piscina(21).sessoes[0]
    v = s.ate(12)
    assert v.n == 12 and np.array_equal(v.c, s.c[:12])


# ------------------------------------------------------------------------------------------------ determinismo / velocidade
def test_determinismo_por_seed_e_seeds_diferentes_divergem():
    p = piscina()
    a = mt.simular(p, WIN, plano(), seed=19, replays=3000).a_dict()
    b = mt.simular(p, WIN, plano(), seed=19, replays=3000).a_dict()
    c = mt.simular(p, WIN, plano(), seed=20, replays=3000).a_dict()
    assert a == b and a != c
    assert mt.simular(p, WIN, plano(), replays=3000).seed == mt.SEED_PADRAO == 19


def test_dez_mil_replays_vetorizados_em_menos_de_1s():
    p = piscina()
    mt.simular(p, WIN, plano(), replays=500)  # aquece numpy
    t0 = time.perf_counter()
    r = mt.simular(p, WIN, plano(), replays=10_000)
    dur = time.perf_counter() - t0
    assert r.replays == 10_000 and dur < 1.0, dur
    assert abs(r.p_alvo + r.p_stop + r.p_nenhum - 1.0) < 1e-9


# ------------------------------------------------------------------------------------------------ causalidade
def test_causal_perturbar_sessoes_futuras_nao_muda_o_resultado():
    base = piscina(30)
    rng = np.random.default_rng(99)
    futuras = tuple(_sessao(DIA_PLANO + dt.timedelta(days=k), rng, base=250_000.0, vol_ticks=40.0) for k in range(0, 8))
    perturbada = mt.PiscinaSessoes(base.sessoes + futuras, {}, base.intervalo_s, True, "teste")
    a = mt.planejar(base, WIN, plano(), replays=2000)
    b = mt.planejar(perturbada, WIN, plano(), replays=2000)
    assert a.a_dict() == b.a_dict()  # o proprio dia do plano e o futuro ficam de fora
    assert b.simulacao.n_sessoes == 30
    # e mexer no PASSADO muda (o teste de perturbacao nao e trivial)
    outra = piscina(30, seed=2)
    assert mt.simular(outra, WIN, plano(), replays=2000).a_dict() != a.simulacao.a_dict()


# ------------------------------------------------------------------------------------------------ stop antes do alvo
def test_barra_que_toca_stop_e_alvo_conta_como_stop():
    rp = mt.resolver_plano(plano(stop=30.0, alvo=60.0), WIN)
    e = np.full((1, 1), 130_000.0)
    o = np.array([[130_000.0]])
    # a mesma barra vai ao alvo (+60) e ao stop (-30): STOP
    s, r, _ = mt._resolver(o, np.array([[130_060.0]]), np.array([[129_970.0]]), np.array([[130_000.0]]), e, rp, 0.0)
    assert s[0] == mt.STOP and r[0] == pytest.approx(-1.0)
    # so o alvo
    s, r, _ = mt._resolver(o, np.array([[130_060.0]]), np.array([[129_995.0]]), np.array([[130_000.0]]), e, rp, 0.0)
    assert s[0] == mt.ALVO and r[0] == pytest.approx(2.0)
    # alvo numa barra e stop na seguinte: ALVO; o inverso: STOP
    o2, h2, l2 = np.full((1, 2), 130_000.0), np.array([[130_060.0, 130_000.0]]), np.array([[129_995.0, 129_970.0]])
    assert mt._resolver(o2, h2, l2, o2, e, rp, 0.0)[0][0] == mt.ALVO
    assert mt._resolver(o2, h2[:, ::-1], l2[:, ::-1], o2, e, rp, 0.0)[0][0] == mt.STOP
    # venda: espelhado
    rv = mt.resolver_plano(plano(Lado.VENDA, 30.0, 60.0), WIN)
    s, r, _ = mt._resolver(o, np.array([[130_030.0]]), np.array([[129_940.0]]), np.array([[130_000.0]]), e, rv, 0.0)
    assert s[0] == mt.STOP


def test_slippage_de_stop_e_gap_pioram_o_r_e_nunca_melhoram():
    rp = mt.resolver_plano(plano(stop=30.0, alvo=60.0), WIN)
    e = np.full((1, 1), 130_000.0)
    barra = (np.array([[130_000.0]]), np.array([[130_000.0]]), np.array([[129_960.0]]), np.array([[129_970.0]]))
    r0 = mt._resolver(*barra, e, rp, 0.0)[1][0]
    r2 = mt._resolver(*barra, e, rp, 2.0)[1][0]
    assert r0 == pytest.approx(-1.0) and r2 == pytest.approx(-1.0 - 2 * TICK / 30.0)
    gap = (np.array([[129_950.0]]), np.array([[129_955.0]]), np.array([[129_940.0]]), np.array([[129_950.0]]))
    assert mt._resolver(*gap, e, rp, 0.0)[1][0] == pytest.approx(-50.0 / 30.0)  # abertura alem do stop


def test_barras_que_sempre_tocam_os_dois_lados_dao_100_por_cento_stop():
    p = piscina(25, amplitude_ticks=200.0, vol_ticks=1.0, fixa=True)  # toda barra cobre +-1000 pts
    r = mt.simular(p, WIN, plano(stop=30.0, alvo=60.0), replays=2000)
    assert r.p_stop == 1.0 and r.p_alvo == 0.0 and r.veredito.codigo == mt.V_FORTE_STOP


# ------------------------------------------------------------------------------------------------ equilibrio e veredito
def test_equilibrio_bruto_1_sobre_1_mais_r_e_liquido_com_custos():
    bruto, liq = mt._equilibrio(2.0, 1.0, 0.0)
    assert bruto == pytest.approx(1 / 3) and liq == pytest.approx(1 / 3)
    for r_alvo, c in ((1.0, 0.05), (2.0, 0.1), (3.0, 0.2)):
        b, n = mt._equilibrio(r_alvo, 1.0, c)
        assert b == pytest.approx(1 / (1 + r_alvo)) and n > b and n == pytest.approx((1 + c) / (1 + r_alvo))
    # perda media maior que 1R (slippage) sobe o equilibrio
    assert mt._equilibrio(2.0, 1.2, 0.0)[1] > mt._equilibrio(2.0, 1.0, 0.0)[1]


def test_equilibrio_do_resultado_usa_os_custos_do_perfil_na_moeda_do_instrumento():
    r = mt.simular(piscina(), WIN, plano(stop=30.0, alvo=60.0), replays=3000)
    custo_brl = 2 * WIN.tarifa_em(DIA_PLANO).tarifa_lado_brl
    assert r.custo_ida_volta == pytest.approx(custo_brl) and r.moeda == WIN.moeda
    assert r.custo_r == pytest.approx(custo_brl / (30.0 * WIN.ponto_valor_brl))
    assert r.r_alvo == pytest.approx(2.0) and r.equilibrio_bruto == pytest.approx(1 / 3)
    assert r.equilibrio_liquido > r.equilibrio_bruto
    assert r.r_liquido_medio < r.r_bruto_medio
    assert r.ev_liquido == pytest.approx(r.r_liquido_medio * 30.0 * WIN.ponto_valor_brl)


def test_veredito_pelos_limiares_e_pela_deriva():
    v = lambda p: mt.montar_veredito(p, 0.40, 1.5, 0.02, 1.0, "BRL", "x").codigo  # noqa: E731
    assert v(None) == mt.V_INDETERMINADO and v(0.42) == mt.V_PERTO and v(0.38) == mt.V_PERTO
    assert v(0.50) == mt.V_INCLINA_ALVO and v(0.30) == mt.V_INCLINA_STOP
    assert v(0.60) == mt.V_FORTE_ALVO and v(0.20) == mt.V_FORTE_STOP
    texto = mt.montar_veredito(0.5, 0.4, 1.5, 0.02, 3.5, "BRL", "30 sessoes").texto
    assert "BRL" in texto and "NAO e previsao".lower() in texto.lower() and "equilibrio" in texto
    # dados com deriva a favor => alvo quase sempre primeiro; contra => stop
    a = mt.simular(piscina(25, deriva_ticks=2.0, vol_ticks=1.0), WIN, plano(stop=30.0, alvo=60.0), replays=3000)
    c = mt.simular(piscina(25, deriva_ticks=-2.0, vol_ticks=1.0), WIN, plano(stop=30.0, alvo=60.0), replays=3000)
    assert a.p_alvo > 0.9 and a.veredito.codigo == mt.V_FORTE_ALVO
    assert c.p_stop > 0.9 and c.veredito.codigo == mt.V_FORTE_STOP


def test_percentis_e_verde():
    r = mt.simular(piscina(), WIN, plano(), replays=3000)
    vals = list(r.percentis_pontos.values())
    assert vals == sorted(vals) and set(r.percentis_pontos) == {"5", "25", "50", "75", "95"}
    assert r.p_alvo <= r.p_verde <= r.p_alvo + r.p_nenhum + 1e-9
    assert r.percentis_preco["50"] == pytest.approx(r.preco_entrada + r.percentis_pontos["50"])


# ------------------------------------------------------------------------------------------------ recusas
def test_recusa_com_menos_de_20_sessoes_completas():
    with pytest.raises(mt.ErroSessoesInsuficientes, match="19"):
        mt.simular(piscina(19), WIN, plano(), replays=100)
    assert mt.simular(piscina(20), WIN, plano(), replays=100).n_sessoes == 20
    # sessoes do proprio dia ou posteriores nao contam
    base = piscina(19)
    extra = _sessao(DIA_PLANO, np.random.default_rng(3))
    with pytest.raises(mt.ErroSessoesInsuficientes):
        mt.simular(mt.PiscinaSessoes(base.sessoes + (extra,), {}, 300, True), WIN, plano(), replays=100)


def test_recusa_plano_incoerente_stop_do_lado_errado():
    with pytest.raises(mt.ErroPlanoIncoerente, match="stop do lado errado"):
        mt.PlanoMT.de_niveis(Lado.COMPRA, 130_000.0, 130_030.0, 130_060.0, data=DIA_PLANO, entrada="10:00", zeragem="17:45")
    with pytest.raises(mt.ErroPlanoIncoerente, match="stop do lado errado"):
        mt.PlanoMT.de_niveis(Lado.VENDA, 130_000.0, 129_970.0, 129_940.0, data=DIA_PLANO, entrada="10:00", zeragem="17:45")
    with pytest.raises(mt.ErroPlanoIncoerente, match="alvo do lado errado"):
        mt.PlanoMT.de_niveis(Lado.COMPRA, 130_000.0, 129_970.0, 129_990.0 - 100.0, data=DIA_PLANO, entrada="10:00", zeragem="17:45")
    ok = mt.PlanoMT.de_niveis(Lado.VENDA, 130_000.0, 130_030.0, 129_940.0, data=DIA_PLANO, entrada="10:00", zeragem="17:45")
    assert ok.stop_pontos == 30.0 and ok.alvo_pontos == 60.0 and ok.preco_entrada == 130_000.0
    for kw in ({"stop": 0.0}, {"alvo": -5.0}, {"stop": 2.0}, {"alvo": float("nan")}):
        with pytest.raises(mt.ErroPlanoIncoerente):
            mt.simular(piscina(), WIN, plano(**kw), replays=100)


def test_recusa_entrada_ou_zeragem_fora_da_grade():
    for kw in ({"entrada": "10:02"}, {"entrada": "08:55"}, {"zeragem": "17:47"}, {"entrada": "xx"}):
        with pytest.raises(mt.ErroEntradaForaDaGrade):
            mt.simular(piscina(), WIN, plano(**kw), replays=100)
    with pytest.raises(mt.ErroPlanoIncoerente, match="posterior"):
        mt.simular(piscina(), WIN, plano(entrada="12:00", zeragem="11:00"), replays=100)
    with pytest.raises(mt.ErroPlanoIncoerente, match="limite de entradas"):
        mt.simular(piscina(), WIN, plano(entrada="17:35", zeragem="17:45"), replays=100)
    with pytest.raises(mt.ErroPlanoIncoerente, match="zeragem da sessao"):
        mt.simular(piscina(), WIN, plano(zeragem="18:20"), replays=100)


def test_recusa_dados_sem_rotulo_de_origem():
    with pytest.raises(mt.ErroSemRotulo, match="sem rotulo"):
        mt.simular(piscina(rotulo=None), WIN, plano(), replays=100)
    with pytest.raises(mt.ErroSemRotulo):
        mt.historico(piscina(rotulo=None), WIN, plano())


def test_rotulo_sintetico_e_simulacao_no_resultado():
    r = mt.simular(piscina(rotulo=True), WIN, plano(), replays=200)
    assert r.sintetico and mt.ROTULO_SINTETICO in r.rotulos and mt.ROTULO_SIMULACAO in r.rotulos
    assert mt.AVISO in r.aviso and "NAO e previsao" in r.aviso
    real = mt.simular(piscina(rotulo=False), WIN, plano(), replays=200)
    assert not real.sintetico and mt.ROTULO_SINTETICO not in real.rotulos and mt.ROTULO_SIMULACAO in real.rotulos


# ------------------------------------------------------------------------------------------------ historico
def test_historico_reaplica_regra_callable_nas_sessoes_passadas():
    p = piscina(30)
    vistas: list[int] = []

    def regra(visao: mt.SessaoBarras) -> bool:
        vistas.append(visao.n)
        return visao.data.toordinal() % 2 == 0

    h = mt.historico(p, WIN, plano(stop=30.0, alvo=60.0), regra)
    esperadas = sum(1 for s in p.sessoes if s.data.toordinal() % 2 == 0)
    assert h.n_sessoes == 30 and h.n_disparos == esperadas and 0 < esperadas < 30
    assert set(vistas) == {12} and len(vistas) == 30  # a regra so ve as 12 barras anteriores a entrada das 10:00
    assert h.n_alvo + h.n_stop + h.n_nenhum == h.n_disparos
    assert all(d.toordinal() % 2 == 0 for d in h.datas_disparo)
    todas = mt.historico(p, WIN, plano())
    assert todas.n_disparos == 30
    nenhuma = mt.historico(p, WIN, plano(), lambda v: False)
    assert nenhuma.n_disparos == 0 and nenhuma.r_liquido_medio is None and nenhuma.p_alvo_entre_resolvidos is None


def test_historico_usa_barras_reais_stop_antes_do_alvo_e_custos():
    p = piscina(21, deriva_ticks=2.0, vol_ticks=1.0)
    h = mt.historico(p, WIN, plano(stop=30.0, alvo=60.0))
    assert h.n_alvo == 21 and h.p_alvo_entre_resolvidos == 1.0
    assert h.r_bruto_medio == pytest.approx(2.0) and h.r_liquido_medio < h.r_bruto_medio
    pior = mt.historico(piscina(21, amplitude_ticks=200.0, vol_ticks=1.0, fixa=True), WIN, plano(stop=30.0, alvo=60.0))
    assert pior.n_stop == 21 and pior.n_alvo == 0


def test_planejar_junta_simulacao_e_historico():
    rel = mt.planejar(piscina(), WIN, plano(), lambda v: True, replays=500)
    d = rel.a_dict()
    json.dumps(d)  # serializavel
    assert d["simulacao"]["replays"] == 500 and d["historico"]["n_disparos"] == 30


# ------------------------------------------------------------------------------------------------ dados e CLI
def test_pipeline_com_dados_sinteticos_do_gerador_e_armazem(tmp_path):
    from robowin.dados.sintetico import ConfigSintetico, gerar_dias
    from robowin.dados.ticks import ArmazenamentoTicks
    dias = gerar_dias(ConfigSintetico.do_perfil(WIN, seed=3, n_ticks=12_000, n_saltos=0), 3)
    arm = ArmazenamentoTicks(tmp_path / "t")
    for d in dias:
        d.gravar(arm)
    p = cli_maquina.piscina_de_armazem(tmp_path / "t", WIN)
    assert p.sintetico is True and len(p.sessoes) == 3 and not p.descartadas
    assert all(s.n == 113 for s in p.sessoes)
    with pytest.raises(mt.ErroMaquinaDoTempo, match="nao encontrado"):
        cli_maquina.piscina_de_armazem(tmp_path / "nada", WIN)
    # contaminacao: simbolos sinteticos e reais misturados no mesmo ativo
    arm.gravar("WINZ26", dias[0].ticks[:50])
    with pytest.raises(mt.ErroMaquinaDoTempo, match="contaminados"):
        cli_maquina.piscina_de_armazem(tmp_path / "t", WIN)


def _main(capsys, monkeypatch, *args, pool=None):
    from robowin.cli import main
    monkeypatch.setattr(cli_maquina, "piscina_sintetica", lambda *a, **k: pool or piscina(25))
    codigo = main(["maquina-do-tempo", "--ativo", "WIN", *args])
    return codigo, capsys.readouterr().out


def test_cli_saida_rotulada_com_aviso(capsys, monkeypatch):
    cod, out = _main(capsys, monkeypatch, "--sintetico", "--lado", "compra", "--stop-pontos", "30", "--alvo-pontos", "60",
                     "--replays", "1000", "--data", DIA_PLANO.isoformat())
    assert cod == 0
    assert "SIMULACAO / SINTETICO" in out and mt.AVISO in out
    assert "alvo primeiro" in out and "stop primeiro" in out and "Veredito:" in out and "R medio liquido" in out
    assert "seed 19" in out and "1000 replays" in out and "BRL" in out


def test_cli_json_e_opcoes(capsys, monkeypatch):
    cod, out = _main(capsys, monkeypatch, "--sintetico", "--lado", "venda", "--stop-pontos", "30", "--alvo-pontos", "90",
                     "--entrada", "11:00", "--zeragem", "17:30", "--seed", "5", "--replays", "800", "--json",
                     "--data", DIA_PLANO.isoformat(), "--slippage-stop-ticks", "1")
    assert cod == 0
    d = json.loads(out)
    s = d["simulacao"]
    assert d["entrada"] == "11:00" and d["zeragem"] == "17:30" and s["seed"] == 5 and s["replays"] == 800
    assert s["lado"] == "venda" and s["sintetico"] is True and "SINTETICO" in s["rotulos"]
    assert s["aviso"] == mt.AVISO and s["slippage_stop_ticks"] == 1.0


def test_cli_recusa_com_mensagem_clara(capsys, monkeypatch):
    cod, out = _main(capsys, monkeypatch, "--sintetico", "--lado", "compra", "--stop-pontos", "30", "--alvo-pontos", "60",
                     "--data", DIA_PLANO.isoformat(), pool=piscina(10))
    assert cod == 3 and "RECUSADO" in out and "dados insuficientes" in out
    cod, out = _main(capsys, monkeypatch, "--sintetico", "--lado", "compra", "--stop-pontos", "30", "--alvo-pontos", "60",
                     "--entrada", "10:03", "--data", DIA_PLANO.isoformat())
    assert cod == 3 and "fora da grade" in out
    from robowin.cli import main
    assert main(["maquina-do-tempo", "--ativo", "WIN", "--lado", "compra", "--stop-pontos", "30", "--alvo-pontos", "60"]) == 2


def test_cli_pipeline_sintetico_real_completo(capsys):
    from robowin.cli import main
    cod = main(["maquina-do-tempo", "--ativo", "WIN", "--sintetico", "--sessoes", "22", "--lado", "compra",
                "--stop-pontos", "100", "--alvo-pontos", "200", "--replays", "2000"])
    out = capsys.readouterr().out
    assert cod == 0 and "SIMULACAO / SINTETICO" in out and "22 sessoes completas anteriores" in out


# ------------------------------------------------------------------------------------------------ estatico
def test_modulo_sem_indicadores_tecnicos_e_com_atribuicao():
    import re
    from pathlib import Path
    raiz = Path(mt.__file__).parent
    for arq in (raiz / "maquina_do_tempo.py", raiz / "cli_maquina.py", raiz.parent / "decisao" / "odds.py"):
        txt = arq.read_text(encoding="utf-8")
        assert "Jake Schincariol" in txt and "MIT" in txt
        rx = re.compile(r"(?<![a-z0-9])(rsi|macd|sma|ema|media_movel|bollinger|cruzamento)(?![a-z0-9])")
        assert not [ln for ln in txt.lower().splitlines() if "proib" not in ln and rx.search(ln)]
    notices = (raiz.parents[2] / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    licenca = Path("/home/user/jakeschincariol/day-trader-skill/LICENSE")
    assert "Copyright (c) 2026 Jake Schincariol" in notices and "MIT License" in notices
    assert "reimplementação independente" in " ".join(notices.split())
    if licenca.exists():
        assert licenca.read_text(encoding="utf-8").strip() in notices
