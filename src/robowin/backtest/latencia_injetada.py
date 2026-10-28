"""Latencia INJETADA no backtest, amostrada de uma distribuicao medida (empirica).

O backtest nao mede latencia: ele a injeta, a partir de amostras fornecidas (por exemplo, as medidas pelo
gateway na demo e gravadas em `latencias` com injetado=0). Todo carimbo produzido aqui e marcado
`injetado=True`, para que relatorios e painel nunca confundam latencia simulada com medida.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from robowin.core.protocolos import Banco
from robowin.core.tipos import CarimboLatencia

MS_NS = 1_000_000


class ErroLatencia(ValueError):
    pass


@dataclass(frozen=True)
class DistribuicaoLatencia:
    """Distribuicao empirica de latencia (ms). `origem` documenta de onde vieram as amostras."""
    amostras_ms: tuple[float, ...]
    origem: str = "empirica"
    suave: bool = False  # True: interpola entre estatisticas de ordem (distribuicao continua)

    def __post_init__(self) -> None:
        if not self.amostras_ms:
            raise ErroLatencia("distribuicao de latencia sem amostras")
        if any((not math.isfinite(x)) or x < 0 for x in self.amostras_ms):
            raise ErroLatencia("latencias devem ser finitas e nao negativas")

    @classmethod
    def de_amostras(cls, amostras_ms: Sequence[float], origem: str = "empirica", suave: bool = False) -> DistribuicaoLatencia:
        return cls(tuple(float(x) for x in amostras_ms), origem, suave)

    @classmethod
    def fixa(cls, ms: float) -> DistribuicaoLatencia:
        return cls((float(ms),), origem=f"fixa_{ms}ms")

    @classmethod
    def de_banco(cls, banco: Banco, etapa: str, *, modelo: str | None = None, suave: bool = False) -> DistribuicaoLatencia:
        """Amostras MEDIDAS (injetado=0) da tabela `latencias` para uma etapa."""
        sql = "SELECT dur_ms FROM latencias WHERE etapa=? AND injetado=0"
        params: tuple[str, ...] = (etapa,)
        if modelo is not None:
            sql, params = sql + " AND modelo=?", (etapa, modelo)
        linhas = banco.consultar(sql + " ORDER BY id", params)
        return cls.de_amostras([float(r["dur_ms"]) for r in linhas], origem=f"banco:{etapa}", suave=suave)

    def quantis(self, qs: Sequence[float] = (0.5, 0.9, 0.99)) -> dict[str, float]:
        a = np.asarray(self.amostras_ms, dtype=float)
        return {f"p{round(q * 100)}": float(np.quantile(a, q)) for q in qs}

    def amostrador(self, seed: int) -> AmostradorLatencia:
        return AmostradorLatencia(self, seed)


@dataclass
class AmostradorLatencia:
    """Chamavel sem argumentos que devolve latencia em ns (formato esperado por SimuladorExecucao)."""
    dist: DistribuicaoLatencia
    seed: int
    _rng: np.random.Generator = field(init=False, repr=False)
    _ordenadas: np.ndarray = field(init=False, repr=False)
    _pendentes: list[float] = field(default_factory=list, init=False, repr=False)
    n_amostras: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        self._ordenadas = np.sort(np.asarray(self.dist.amostras_ms, dtype=float))

    def amostrar_ms(self) -> float:
        a = self._ordenadas
        if self.dist.suave and len(a) > 1:
            ms = float(np.interp(self._rng.random(), np.linspace(0.0, 1.0, len(a)), a))
        else:
            ms = float(a[int(self._rng.integers(0, len(a)))])
        self._pendentes.append(ms)
        self.n_amostras += 1
        return ms

    def __call__(self) -> int:
        return int(round(self.amostrar_ms() * MS_NS))

    def drenar(self) -> list[float]:
        """Amostras sorteadas desde a ultima drenagem (ms), na ordem do sorteio."""
        out, self._pendentes = self._pendentes, []
        return out


def carimbo_injetado(etapa: str, braco: str, t_ini_ns: int, dur_ms: float) -> CarimboLatencia:
    return CarimboLatencia(etapa=etapa, braco=braco, t_ini_ns=t_ini_ns,
                           t_fim_ns=t_ini_ns + int(round(dur_ms * MS_NS)), injetado=True)


def linha_latencia(c: CarimboLatencia, modelo: str = "", effort: str = "") -> dict[str, Any]:
    """Linha da tabela `latencias` a partir de um carimbo."""
    return {"t_ns": c.t_ini_ns, "braco": c.braco, "etapa": c.etapa, "dur_ms": c.dur_ms,
            "injetado": int(c.injetado), "modelo": modelo, "effort": effort}
