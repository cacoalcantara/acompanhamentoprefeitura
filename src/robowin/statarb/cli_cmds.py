"""`robowin statarb demo`: demonstracao com dados SINTETICOS rotulados (nao e dado de mercado)."""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np

from .cointegracao import engle_granger
from .kalman import filtrar
from .leadlag import lead_lag
from .sinais import SinalStatArbPadrao
from .sintetico import par_cointegrado, passeios_independentes, ticks_assincronos


def demo(args: argparse.Namespace) -> int:
    print("[SINTETICO] statarb demo - simulacao, sem dados de mercado")
    y, x = par_cointegrado(args.n, beta=1.5, phi=0.95, seed=args.seed)
    eg = engle_granger(y, x)
    print(f"[SINTETICO] par cointegrado (beta real 1.5): p={eg.pvalor:.4f} coint={eg.cointegrado} "
          f"beta={eg.betas[0]:.3f} meia-vida={eg.meia_vida:.1f} (real {-np.log(2) / np.log(0.95):.1f})")
    a, b = passeios_independentes(args.n, seed=args.seed)
    eg2 = engle_granger(a, b)
    print(f"[SINTETICO] passeios independentes: p={eg2.pvalor:.4f} coint={eg2.cointegrado}")
    beta, _, _ = filtrar(y, x, delta=1e-5, var_obs=1.0)
    print(f"[SINTETICO] Kalman: beta final={beta[-1]:.3f}")
    tx, xx, ty, yy = ticks_assincronos(2000.0, lag=3.0, seed=args.seed)
    ll = lead_lag(tx, np.asarray(xx), ty, np.asarray(yy), np.arange(-8.0, 8.5, 0.5))
    print(f"[SINTETICO] Hayashi-Yoshida: lag estimado={ll.lag_otimo:+.1f} (real +3.0) corr={ll.correlacao:.2f}")
    s = SinalStatArbPadrao(alvo="WIN", referencias=("REF",))
    h = args.n // 2
    s.treinar({"WIN": y[:h], "REF": x[:h]})
    s.validar_oos({"WIN": y[h:], "REF": x[h:]})
    out = [s.sinal(i, {"WIN": float(y[i]), "REF": float(x[i])}) for i in range(h, args.n)]
    ativos = sum(1 for o in out if o["direcional_win"] != 0.0)
    print(f"[SINTETICO] sinal: status={s.status} instantes com direcional != 0: {ativos}/{len(out)}")
    return 0


def registrar(sub: Any) -> None:
    p = sub.add_parser("statarb", help="Sinais de valor relativo (StatArb)")
    ssub = p.add_subparsers(dest="statarb_cmd")
    d = ssub.add_parser("demo", help="Demonstracao com dados sinteticos rotulados")
    d.add_argument("--n", type=int, default=2000)
    d.add_argument("--seed", type=int, default=0)
    d.set_defaults(func=demo)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])
