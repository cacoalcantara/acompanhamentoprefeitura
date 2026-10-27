"""Calibragem do relogio do servidor MT5 contra o UTC local.

O MT5 entrega horarios de ticks/deals em "hora do servidor" (epoch deslocado por um offset de fuso,
tipicamente multiplo de 15 min). Gravamos SEMPRE o ms bruto do servidor e o UTC normalizado.

Estimativa: para um tick com idade `a >= 0`, `est = t_tick_servidor - t_utc_local = offset - a`.
O offset verdadeiro e entao o menor multiplo da grade (900 s) que seja >= est (com tolerancia para
desvio do relogio local). A calibragem so e confiavel se a idade implicita for pequena; fora disso
(mercado fechado, tick velho) use `offset_fixo_s` da configuracao.

Atencao: um tick MUITO velho (horas) e indistinguivel de um offset menor com tick fresco. Por isso a
calibragem so e marcada `confiavel` com evidencia de feed vivo: ticks DIFERENTES observados entre as
amostras (use `amostras>=3` e `intervalo_s>0` com o pregao aberto). Sem isso, exija `offset_fixo_s`.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from robowin.core.relogio import Relogio
from robowin.core.tipos import Tick

GRADE_S = 900
TOLERANCIA_RELOGIO_S = 60.0
IDADE_MAX_CONFIAVEL_S = 600.0


@dataclass(frozen=True)
class RelogioServidor:
    offset_ms: int = 0
    calibrado_em_ns: int = 0
    confiavel: bool = False
    amostras: int = 0
    idade_implicita_s: float = 0.0
    origem: str = "nao_calibrado"  # nao_calibrado | calibrado | fixo

    @property
    def offset_s(self) -> float:
        return self.offset_ms / 1000.0

    def servidor_ms_para_utc_ns(self, ms: int) -> int:
        return (int(ms) - self.offset_ms) * 1_000_000

    def utc_ns_para_servidor_ms(self, ns: int) -> int:
        return int(ns) // 1_000_000 + self.offset_ms

    def utc_ns_para_dt_servidor(self, ns: int) -> dt.datetime:
        """Datetime no formato que a API do MT5 espera: hora de parede do servidor rotulada como UTC."""
        return dt.datetime.fromtimestamp(self.utc_ns_para_servidor_ms(ns) / 1000.0, dt.UTC)

    def servidor_s_para_utc_ns(self, s: int) -> int:
        return self.servidor_ms_para_utc_ns(int(s) * 1000)

    def tick_de_linha(self, linha: dict[str, Any], recebido_ns: int) -> Tick:
        """Converte uma linha bruta (time_msc, bid, ask, last, volume, flags) em `Tick`."""
        ms = int(linha["time_msc"])
        vol = linha.get("volume_real") or linha.get("volume") or 0.0
        return Tick(
            t_servidor_ms=ms,
            t_utc_ns=self.servidor_ms_para_utc_ns(ms),
            t_recebido_ns=recebido_ns,
            bid=float(linha["bid"]),
            ask=float(linha["ask"]),
            last=float(linha["last"]),
            volume=float(vol),
            flags=int(linha.get("flags", 0)),
        )


def estimar_offset(est_s: float, grade_s: int = GRADE_S) -> tuple[int, float]:
    """Retorna (offset_s alinhado a grade, idade implicita em s) a partir de `est = offset - idade`."""
    off = math.ceil((est_s - TOLERANCIA_RELOGIO_S) / grade_s) * grade_s
    return int(off), float(off - est_s)


def calibrar(
    terminal: Any,
    simbolo: str,
    relogio: Relogio,
    amostras: int = 5,
    intervalo_s: float = 0.0,
    offset_fixo_s: int | None = None,
    dormir: Callable[[float], None] = time.sleep,
) -> RelogioServidor:
    """Calibra o offset usando `symbol_info_tick`. Com `offset_fixo_s`, apenas registra o valor fixo."""
    if offset_fixo_s is not None:
        return RelogioServidor(int(offset_fixo_s) * 1000, relogio.agora_ns(), True, 0, 0.0, "fixo")
    ests: list[float] = []
    distintos: set[int] = set()
    for i in range(max(1, amostras)):
        tk = terminal.symbol_info_tick(simbolo)
        if tk is not None and int(getattr(tk, "time_msc", 0)) > 0:
            ests.append(tk.time_msc / 1000.0 - relogio.agora_ns() / 1e9)
            distintos.add(int(tk.time_msc))
        if i + 1 < amostras and intervalo_s > 0:
            dormir(intervalo_s)
    if not ests:
        return RelogioServidor(0, relogio.agora_ns(), False, 0, 0.0, "nao_calibrado")
    off_s, idade = estimar_offset(max(ests))  # maior est = tick mais novo
    vivo = len(distintos) >= 2
    confiavel = -TOLERANCIA_RELOGIO_S <= idade <= IDADE_MAX_CONFIAVEL_S and vivo
    return RelogioServidor(off_s * 1000, relogio.agora_ns(), confiavel, len(ests), idade, "calibrado")
