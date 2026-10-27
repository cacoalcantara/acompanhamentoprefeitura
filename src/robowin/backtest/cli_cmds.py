"""CLI: `robowin backtest --dados sinteticos --saida DIR [--dias N] [--seed S] [--estresse]`.

Roda o backtest de infraestrutura com os bracos de REFERENCIA (controle aleatorio e nao operar) e escreve
o relatorio via `robowin.avaliacao.relatorio`, que recusa o selo 'validado' em dados sinteticos.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from robowin.avaliacao import relatorio
from robowin.core.tipos import Instrumento
from robowin.dados.banco import BancoSQLite
from robowin.dados.sintetico import ROTULO_SINTETICO, ConfigSintetico, gerar_dias
from robowin.dados.ticks import ArmazenamentoTicks

from .bracos_referencia import ControleAleatorio, NaoOperar
from .latencia_injetada import DistribuicaoLatencia
from .motor import ConfigBacktest, MotorBacktest, ResultadoBacktest
from .replay import FonteArmazem

CONTROLE = "controle_aleatorio"


def _limpar_banco(caminho: Path) -> None:
    for suf in ("", "-wal", "-shm"):
        p = Path(str(caminho) + suf)
        if p.exists():
            p.unlink()


def _escrever_relatorio(res: ResultadoBacktest, saida: Path, sufixo: str, *, contaminado: bool, n_boot: int,
                        seed: int) -> dict[str, Any]:
    rel = relatorio.gerar_relatorio(
        res.trades_por_braco(), controle=CONTROLE if CONTROLE in res.bracos else None, sintetico=res.sintetico,
        contaminado=contaminado, tentativas=max(1, len(res.bracos)), n_boot=n_boot, seed=seed,
        titulo=f"Backtest de infraestrutura ({res.cenario})")
    md = relatorio.renderizar_markdown(rel)
    if res.sintetico:
        md = f"> **{ROTULO_SINTETICO}**\n\n" + md
    (saida / f"relatorio{sufixo}.md").write_text(md, encoding="utf-8")
    (saida / f"relatorio{sufixo}.json").write_text(
        json.dumps(relatorio.para_json_seguro(rel), ensure_ascii=False, indent=2), encoding="utf-8")
    (saida / f"resultado{sufixo}.json").write_text(
        json.dumps(res.resumo(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return rel


def _backtest(a: argparse.Namespace) -> int:
    saida = Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    if a.dados == "sinteticos":
        arm = ArmazenamentoTicks(saida / "ticks")
        cfg_s = ConfigSintetico(seed=a.seed, n_ticks=a.n_ticks, sinal_forca=a.sinal_forca)
        for d in gerar_dias(cfg_s, a.dias):
            d.gravar(arm)
        simbolo = cfg_s.simbolo
        print(f"*** {ROTULO_SINTETICO} ***")
    else:
        if not a.dir or not a.simbolo:
            print("--dados armazenado exige --dir e --simbolo")
            return 2
        arm, simbolo = ArmazenamentoTicks(a.dir), a.simbolo
        if arm.eh_sintetico:
            print(f"*** {ROTULO_SINTETICO} ***")
    fonte = FonteArmazem(arm, simbolo)
    cfg = ConfigBacktest(
        instrumento=Instrumento(simbolo=simbolo), seed=a.seed, intervalo_decisao_s=a.intervalo_s,
        latencia=DistribuicaoLatencia.fixa(a.latencia_ms), id_experimento=f"backtest-{a.seed}")
    cenarios = [("", cfg)] + ([("_estresse", cfg.estresse())] if a.estresse else [])
    for sufixo, c in cenarios:
        db = saida / f"backtest{sufixo}.db"
        _limpar_banco(db)
        with BancoSQLite(db) as banco:
            bracos = [ControleAleatorio(a.seed, id=CONTROLE, p_entrada=a.p_entrada), NaoOperar()]
            res = MotorBacktest(fonte, bracos, c, banco).executar()
        rel = _escrever_relatorio(res, saida, sufixo, contaminado=a.contaminado, n_boot=a.n_boot, seed=a.seed)
        selos = {n: b["selo"]["selo"] for n, b in rel["bracos"].items()}
        print(f"[{res.cenario}] eventos={res.n_eventos} hash={res.hash_resultado()} selos={selos}")
        for b in res.bracos:
            print(f"  {b}: trades={len(res.trades[b])} liquido=R$ {res.liquido_brl(b):.2f}")
    print(f"relatorios e bancos em {saida}")
    return 0


def registrar(sub: Any) -> None:
    p = sub.add_parser("backtest", help="backtest de infraestrutura (bracos de referencia) e relatorio")
    p.add_argument("--dados", choices=["sinteticos", "armazenado"], default="sinteticos")
    p.add_argument("--saida", required=True, help="diretorio de saida (bancos SQLite, relatorios, ticks sinteticos)")
    p.add_argument("--dir", default=None, help="armazem de ticks (com --dados armazenado)")
    p.add_argument("--simbolo", default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--dias", type=int, default=1)
    p.add_argument("--n-ticks", type=int, default=20_000)
    p.add_argument("--sinal-forca", type=float, default=0.0)
    p.add_argument("--intervalo-s", type=float, default=15.0, help="intervalo entre decisoes")
    p.add_argument("--p-entrada", type=float, default=0.02, help="probabilidade de entrada do controle aleatorio")
    p.add_argument("--latencia-ms", type=float, default=150.0, help="latencia injetada (fixa) do simulador")
    p.add_argument("--estresse", action="store_true", help="roda tambem o cenario de estresse")
    p.add_argument("--contaminado", action="store_true")
    p.add_argument("--n-boot", type=int, default=500)
    p.set_defaults(func=_backtest)
