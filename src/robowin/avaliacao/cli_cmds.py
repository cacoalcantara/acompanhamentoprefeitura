"""`robowin avaliar --banco ... --saida ...`"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from robowin.core.tipos import Lado, Trade

from . import relatorio

_SQL = ("SELECT id, braco, lado, entrada, saida, t_entrada_ns, t_saida_ns, bruto_brl, tarifas_brl, corretagem_brl, "
        "slippage_brl, motivo_saida FROM trades ORDER BY t_saida_ns, id")


def carregar_trades(banco: str | Path) -> dict[str, list[Trade]]:
    con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
    try:
        out: dict[str, list[Trade]] = {}
        for r in con.execute(_SQL):
            t = Trade(id=r[0], braco=r[1], lado=Lado(r[2]), entrada=r[3], saida=r[4], t_entrada_ns=r[5],
                      t_saida_ns=r[6], bruto_brl=r[7], tarifas_brl=r[8] or 0.0, corretagem_brl=r[9] or 0.0,
                      slippage_brl=r[10] or 0.0, motivo_saida=r[11] or "")
            out.setdefault(t.braco, []).append(t)
        return out
    finally:
        con.close()


def banco_sintetico(banco: str | Path) -> bool:
    """Marca como sintetico se algum manifesto de experimento mencionar dados sinteticos."""
    con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
    try:
        for (m,) in con.execute("SELECT manifesto_json FROM experimentos"):
            if m and "sinteti" in m.lower():
                return True
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return False


def _avaliar(a) -> int:
    trades = carregar_trades(a.banco)
    sint = bool(a.sintetico) or banco_sintetico(a.banco)
    rel = relatorio.gerar_relatorio(trades, controle=a.controle, sintetico=sint, contaminado=bool(a.contaminado),
                                    n_alvo=a.n_alvo, tentativas=a.tentativas, n_boot=a.n_boot, seed=a.seed)
    saida = Path(a.saida)
    if saida.suffix:
        md, js = saida, saida.with_suffix(".json")
    else:
        saida.mkdir(parents=True, exist_ok=True)
        md, js = saida / "relatorio.md", saida / "relatorio.json"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(relatorio.renderizar_markdown(rel), encoding="utf-8")
    js.write_text(json.dumps(relatorio.para_json_seguro(rel), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"relatorio escrito em {md} e {js}")
    return 0


def registrar(sub):
    p = sub.add_parser("avaliar", help="gera o relatorio de avaliacao (Markdown e JSON) a partir do banco")
    p.add_argument("--banco", required=True)
    p.add_argument("--saida", required=True, help="arquivo .md ou diretorio")
    p.add_argument("--controle", default=None, help="nome do braco de controle aleatorio")
    p.add_argument("--n-alvo", type=int, default=None, help="n pre-registrado")
    p.add_argument("--tentativas", type=int, default=1, help="numero de tentativas (DSR)")
    p.add_argument("--sintetico", action="store_true")
    p.add_argument("--contaminado", action="store_true")
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=_avaliar)
