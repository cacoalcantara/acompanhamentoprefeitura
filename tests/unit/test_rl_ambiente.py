import datetime as dt

import numpy as np
import pytest
from perfis_teste import INS_WIN, WIN

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.tipos import Lado, Tick
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.rl.ambiente import AGREDIR, AGUARDAR, APREGOAR, ConfigAmbiente
from robowin.rl.ambiente import AmbienteExecucao as _AmbienteExecucao

T0 = dt_utc_ns(dt.datetime(2026, 11, 10, 13, 0))
S = 1_000_000_000


def AmbienteExecucao(*a, **k):
    k.setdefault("instrumento", INS_WIN)
    return _AmbienteExecucao(*a, **k)


def tk(seg, bid=130000.0, ask=130005.0, last=None, vol=1.0):
    t = T0 + int(seg * S)
    return Tick(t_servidor_ms=t // 1_000_000, t_utc_ns=t, t_recebido_ns=t, bid=bid, ask=ask,
                last=bid if last is None else last, volume=vol)


def estavel(n=200, trade_apos=None):
    out = []
    for i in range(n):
        atravessa = trade_apos is not None and i >= trade_apos
        out.append(tk(i, last=129995.0 if atravessa else None, vol=1.0))
    return out


def rodar(env, acoes, **opt):
    obs, info = env.reset(options=opt or None)
    traj = []
    for a in acoes:
        obs, r, term, trunc, info = env.step(a)
        traj.append((a, round(r, 9), term, trunc))
        if term or trunc:
            break
    return traj, info


def test_agredir_custo_meio_spread_mais_tarifa():
    env = AmbienteExecucao(estavel(), config=ConfigAmbiente(lado=Lado.COMPRA))
    traj, info = rodar(env, [AGREDIR], inicio=5)
    # compra a mercado no ask: (130005 - meio 130002.5) * 0.20 = 0.50 + tarifa 0.25
    assert traj[-1][1] == pytest.approx(-0.75)
    assert info["custo_ticks"] == pytest.approx(0.75)  # 0.75 R$ / (R$ 1,00 por tick)


def test_apregoar_executa_no_melhor_preco_e_ganha_meio_spread():
    env = AmbienteExecucao(estavel(trade_apos=8), config=ConfigAmbiente(lado=Lado.COMPRA))
    traj, info = rodar(env, [APREGOAR], inicio=5)
    # compra no bid 130000 (meio 130002.5): ganho 0.50 - tarifa 0.25
    assert traj[-1][1] == pytest.approx(0.25)
    assert info["quantidade_executada"] == 1


def test_apregoar_sem_negocio_nao_executa_e_prazo_forca_agressao():
    env = AmbienteExecucao(estavel(), config=ConfigAmbiente(lado=Lado.COMPRA, horizonte_s=20.0))
    traj, info = rodar(env, [APREGOAR] * 10, inicio=5)
    assert traj[-1][2] is True and traj[-1][1] == pytest.approx(-0.75)
    assert info["quantidade_executada"] == 1


def test_saida_protecao_desliga_rl():
    env = AmbienteExecucao(estavel(), config=ConfigAmbiente())
    traj, info = rodar(env, [APREGOAR], inicio=5, saida_protecao=True)
    assert traj[-1][2] is True and "acao_convertida" in info
    assert traj[-1][1] == pytest.approx(-0.75)


def test_apregoar_abaixo_do_tempo_minimo_recusado():
    with pytest.raises(ValueError):
        ConfigAmbiente(apregoar_s=1.0)


def test_step_apos_termino_exige_reset():
    env = AmbienteExecucao(estavel())
    rodar(env, [AGREDIR], inicio=5)
    with pytest.raises(RuntimeError):
        env.step(AGUARDAR)


def _dados(seed=3, n=6000):
    d = gerar(ConfigSintetico.do_perfil(WIN, seed=seed, n_ticks=n))
    return d.ticks, d.livros


def _politica_aleatoria(env, seed, n_ep):
    rng = np.random.default_rng(seed)
    saidas = []
    for _ in range(n_ep):
        env.reset()
        fim = False
        while not fim:
            _, r, term, trunc, info = env.step(int(rng.integers(3)))
            assert info["quantidade_executada"] <= 1
            fim = term or trunc
        saidas.append((round(r, 9), info.get("preco_exec"), info["passos"]))
    return saidas


def test_determinismo_por_seed():
    ticks, livros = _dados()
    a = _politica_aleatoria(AmbienteExecucao(ticks, livros, seed=11), 5, 15)
    b = _politica_aleatoria(AmbienteExecucao(ticks, livros, seed=11), 5, 15)
    c = _politica_aleatoria(AmbienteExecucao(ticks, livros, seed=12), 5, 15)
    assert a == b
    assert a != c


def test_nunca_viola_quantidade_1_em_muitos_episodios():
    ticks, livros = _dados(seed=5)
    for lado in (Lado.COMPRA, Lado.VENDA):
        env = AmbienteExecucao(ticks, livros, ConfigAmbiente(lado=lado), seed=1)
        _politica_aleatoria(env, 9, 25)  # asserts internas (qtd <= 1) e AssertionError do ambiente
