"""`robowin rl demo`: execucao por RL em dados SINTETICOS rotulados (SIMULACAO; nada e dado de mercado)."""
from __future__ import annotations

import numpy as np

from ..core.instrumentos import argumentos_ativo, perfil_do_cli
from ..dados.sintetico import ROTULO_SINTETICO, ConfigSintetico, gerar
from .ambiente import AGREDIR, N_ACOES, AmbienteExecucao, ConfigAmbiente
from .bandit import BanditThompsonLinear
from .ips import estimar_ips, razao_trajetoria
from .politica import PoliticaBandit, PoliticaQ, contexto
from .q_tabular import Discretizador, QTabularFinito


def aviso(tick_valor_brl: float) -> str:
    return (f"*** {ROTULO_SINTETICO} ***\n"
            f"Ganho maximo plausivel da execucao otima ~1 tick (R$ {tick_valor_brl:.2f}/contrato): e um LIMITE teorico, "
            "NAO uma medicao.\n"
            "Resultados em dados sinteticos demonstram o pipeline; nao provam ganho em mercado real.")


def coletar(env: AmbienteExecucao, n: int, seed: int) -> list[dict]:
    """Episodios com politica UNIFORME aleatoria (propensao 1/3 por passo, registrada)."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        obs, _ = env.reset()
        passos, fim, r = [], False, 0.0
        while not fim:
            a = int(rng.integers(N_ACOES))
            novo, r, term, trunc, info = env.step(a)
            passos.append({"obs": obs, "a": a, "p": 1.0 / N_ACOES, "obs2": novo})
            obs, fim = novo, term or trunc
        if not info.get("truncado"):
            out.append({"passos": passos, "r": r})
    return out


def _treinar(eps: list[dict], q_h: int, seed: int) -> tuple[PoliticaBandit, PoliticaQ]:
    bandit = BanditThompsonLinear(N_ACOES, 5, seed=seed, sigma=1.0)
    disc = Discretizador([[1.5], [-1.5, -0.5, 0.5, 1.5]])
    q = QTabularFinito(disc.n_estados, N_ACOES, q_h, alfa=0.1, seed=seed)
    pq = PoliticaQ(q, disc, epsilon=0.1)
    for passada in range(5):  # varias passadas pelos mesmos dados logados (off-policy)
        for ep in eps:
            n = len(ep["passos"])
            for i, p in enumerate(ep["passos"]):
                if passada == 0:
                    bandit.atualizar(contexto(p["obs"]), p["a"], ep["r"])  # credito Monte Carlo: retorno do episodio
                last = i == n - 1
                h = min(q_h - 1, int((1 - p["obs"]["tempo_restante"]) * q_h))
                s = disc([p["obs"][k] for k in pq.chaves])
                s2 = disc([p["obs2"][k] for k in pq.chaves])
                q.atualizar(h, s, p["a"], ep["r"] if last else 0.0, s2, last)
    return PoliticaBandit(bandit), pq


def _avaliar(nome: str, eps: list[dict], prob, clip: float) -> str:
    rs, plog, palvo = [], [], []
    for ep in eps:
        pl = [p["p"] for p in ep["passos"]]
        pa = [float(prob(p["obs"])[p["a"]]) for p in ep["passos"]]
        rs.append(ep["r"])
        plog.append(float(np.prod(pl)))
        palvo.append(float(np.prod(pa)))
        _ = razao_trajetoria(pl, pa)
    r = estimar_ips(rs, plog, palvo, clip=clip)
    return (f"{nome:<22} IPS={r.estimativa:+.3f} R$ (ep {r.erro_padrao:.3f})  SNIPS={r.snips:+.3f}  "
            f"ESS={r.ess:.1f}/{r.n}  clipadas={r.frac_clipada:.0%}")


def _demo(a) -> int:
    perfil = perfil_do_cli(a)
    print(aviso(perfil.tick_valor_brl))
    dados = gerar(ConfigSintetico.do_perfil(perfil, seed=a.seed, n_ticks=a.ticks, sinal_forca=0.0))
    cfg = ConfigAmbiente()
    env = AmbienteExecucao(dados.ticks, dados.livros, cfg, instrumento=perfil.instrumento(dados.config.simbolo), seed=a.seed)
    eps = coletar(env, a.episodios, a.seed + 1)
    corte = int(len(eps) * 0.7)
    treino, teste = eps[:corte], eps[corte:]
    H = int(cfg.horizonte_s / cfg.passo_s)
    pb, pq = _treinar(treino, H, a.seed)
    print(f"episodios validos: {len(eps)} (treino {len(treino)} / teste {len(teste)}); horizonte {H} passos")
    custo = -np.mean([e["r"] for e in teste])
    print(f"custo medio sob politica uniforme (log): {custo:+.3f} R$/ordem")
    sempre = np.zeros(N_ACOES)
    sempre[AGREDIR] = 1.0
    print("avaliacao off-policy (IPS por trajetoria, clip=20) no conjunto de TESTE; recompensa = -custo vs chegada:")
    print("  " + _avaliar("sempre agredir (padrao)", teste, lambda o: sempre, 20.0))
    print("  " + _avaliar("bandit Thompson linear", teste, pb.probabilidades, 20.0))
    print("  " + _avaliar("Q tabular horizonte fin.", teste, pq.probabilidades, 20.0))
    print("Leitura: ESS baixo => estimativa pouco confiavel. Sem validacao em tempo real, RL NAO e promovido.")
    return 0


def registrar(sub):
    p = sub.add_parser("rl", help="execucao por RL (SIMULACAO, dados sinteticos)").add_subparsers(dest="rl_cmd")
    d = p.add_parser("demo", help="demonstracao em dados sinteticos rotulados")
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--ticks", type=int, default=30_000)
    d.add_argument("--episodios", type=int, default=300)
    argumentos_ativo(d)
    d.set_defaults(func=_demo)
