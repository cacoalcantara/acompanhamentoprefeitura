"""Livro L2: desequilibrio, microprice, spread e OFI de Cont-Kukanov-Stoikov (2014)."""
from __future__ import annotations

import math

from robowin.core.tipos import LivroSnapshot

from ._janela import JanelaSoma

NS = 1_000_000_000
NAN = float("nan")
IDADE_MAX_NS = 10 * NS  # livro mais velho que isso e tratado como ausente


def niveis_ordenados(livro: LivroSnapshot) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(compras da melhor para a pior, vendas da melhor para a pior), como (preco, volume)."""
    c = sorted(((n.preco, n.volume) for n in livro.niveis if n.lado == "compra"), key=lambda x: -x[0])
    v = sorted(((n.preco, n.volume) for n in livro.niveis if n.lado == "venda"), key=lambda x: x[0])
    return c, v


def topo(livro: LivroSnapshot) -> tuple[float, float, float, float] | None:
    """(pb, qb, pa, qa) ou None se o livro for vazio de um lado ou cruzado."""
    c, v = niveis_ordenados(livro)
    if not c or not v or c[0][0] >= v[0][0]:
        return None
    return c[0][0], c[0][1], v[0][0], v[0][1]


def desequilibrio_l2(livro: LivroSnapshot, n: int) -> float:
    """(Vb - Va)/(Vb + Va) nos n melhores niveis de cada lado; NaN se sem livro."""
    c, v = niveis_ordenados(livro)
    if not c or not v:
        return NAN
    vb = sum(q for _, q in c[:n])
    va = sum(q for _, q in v[:n])
    return (vb - va) / (vb + va) if vb + va > 0 else 0.0


def microprice(pb: float, qb: float, pa: float, qa: float) -> float:
    """Microprice de Stoikov: (pb*qa + pa*qb)/(qb+qa)."""
    tot = qb + qa
    return (pb * qa + pa * qb) / tot if tot > 0 else (pb + pa) / 2


def contribuicao_ofi(ant: tuple[float, float, float, float], atu: tuple[float, float, float, float]) -> float:
    """e_n = I{Pb>=Pb'}qb - I{Pb<=Pb'}qb' - I{Pa<=Pa'}qa + I{Pa>=Pa'}qa' (Cont-Kukanov-Stoikov)."""
    pb0, qb0, pa0, qa0 = ant
    pb1, qb1, pa1, qa1 = atu
    e = 0.0
    if pb1 >= pb0:
        e += qb1
    if pb1 <= pb0:
        e -= qb0
    if pa1 <= pa0:
        e -= qa1
    if pa1 >= pa0:
        e += qa0
    return e


class FeaturesLivro:
    def __init__(self) -> None:
        self._ultimo_topo: tuple[float, float, float, float] | None = None
        self._ultimo_livro: LivroSnapshot | None = None
        self._t_livro: int | None = None
        self._ofi5 = JanelaSoma(5 * NS, 1)
        self._ofi30 = JanelaSoma(30 * NS, 2)  # (ofi, profundidade) ; n via len

    def atualizar(self, t_ns: int, livro: LivroSnapshot) -> None:
        tp = topo(livro)
        self._ultimo_livro, self._t_livro = livro, t_ns
        if tp is None:
            self._ultimo_topo = None  # nao calcula OFI atravessando livro invalido
            return
        if self._ultimo_topo is not None:
            e = contribuicao_ofi(self._ultimo_topo, tp)
            self._ofi5.adicionar(t_ns, e)
            self._ofi30.adicionar(t_ns, e, tp[1] + tp[3])
        self._ultimo_topo = tp

    def valores(self, t_ns: int) -> dict[str, float]:
        out = {k: NAN for k in ("livro_imb_1", "livro_imb_5", "microprice_desvio_pts", "livro_spread_pts")}
        o5 = self._ofi5.somas(t_ns)[0]
        o30, prof = self._ofi30.somas(t_ns)
        n30 = len(self._ofi30)
        out["ofi_5s"] = o5
        out["ofi_30s"] = o30
        out["ofi_30s_norm"] = o30 / (prof / n30) if n30 and prof > 0 else 0.0
        if self._ultimo_livro is None or self._t_livro is None or t_ns - self._t_livro > IDADE_MAX_NS:
            return out
        tp = topo(self._ultimo_livro)
        if tp is None:
            return out
        pb, qb, pa, qa = tp
        out["livro_imb_1"] = desequilibrio_l2(self._ultimo_livro, 1)
        out["livro_imb_5"] = desequilibrio_l2(self._ultimo_livro, 5)
        out["microprice_desvio_pts"] = microprice(pb, qb, pa, qa) - (pb + pa) / 2
        out["livro_spread_pts"] = pa - pb
        return out


def eh_nan(x: float) -> bool:
    return isinstance(x, float) and math.isnan(x)
