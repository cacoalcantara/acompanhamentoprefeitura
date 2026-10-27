"""`robowin mm demo`: market making EXPERIMENTAL no simulador, em dados SINTETICOS rotulados."""
from __future__ import annotations

from ..dados.sintetico import ROTULO_SINTETICO, ConfigSintetico, gerar
from .config import ConfigMM
from .cotacao import CotadorMM
from .markouts import markouts, serie_meio
from .simulacao import simular_mm

AVISO = (f"*** {ROTULO_SINTETICO} ***\n"
         "Market making de varejo via MT5 e EXPERIMENTAL: fila no fim, spread de 1 tick, tarifa ~0,5 tick por ida e\n"
         "volta e selecao adversa provavel. Roda SO no simulador (fila conservadora); demo_mm desligada por padrao.")


def _demo(a) -> int:
    print(AVISO)
    cfg = ConfigMM(demo_mm=a.demo_mm)
    dados = gerar(ConfigSintetico(seed=a.seed, n_ticks=a.ticks, sinal_forca=a.sinal))
    cot = CotadorMM(cfg=cfg)
    r = simular_mm(dados.ticks, dados.livros, cot, seed=a.seed, cfg=cfg)
    t, m = serie_meio(dados.ticks)
    _, resumos, capt = markouts(r.fills, t, m)
    print(f"decisoes: {r.n_decisoes}; fills: {len(r.fills)}; mensagens: {r.mensagens}")
    print(f"estoque maximo |q|: {r.max_abs_estoque} (limite 1); estoque final: {r.estoque_final}")
    print(f"resultado liquido realizado (R$, apos tarifas): {r.liquido_brl:+.2f}")
    print(f"captura inicial media: {capt:+.2f} pts")
    for s in resumos:
        print(f"markout {s.horizonte_s:>4.0f}s: media {s.media_pts:+.2f} pts (n={s.n}, sem dados={s.n_sem_dados})")
    print("Markout medio negativo = selecao adversa. Nada acima e evidencia de lucro.")
    return 0


def registrar(sub):
    p = sub.add_parser("mm", help="market making experimental (SIMULACAO)").add_subparsers(dest="mm_cmd")
    d = p.add_parser("demo", help="demonstracao em dados sinteticos rotulados")
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--ticks", type=int, default=30_000)
    d.add_argument("--sinal", type=float, default=0.0)
    d.add_argument("--demo-mm", action="store_true", help="flag demo_mm (nao implementada: aborta)")
    d.set_defaults(func=_demo)
