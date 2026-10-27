"""Configuracao do market making (experimental).

`demo_mm` esta DESLIGADA por padrao. Com ela desligada o MM so roda no SIMULADOR interno (fila conservadora).
Ligar `demo_mm` significaria espelhar as cotacoes na conta DEMO do MT5 (so para medir confirmacao e
cancelamento; a demo preenche limitada sem fila e NAO e evidencia de lucro), com teto de mensagens dentro da
franquia e espelhamento exclusivo. Essa ponte NAO esta implementada em src/robowin/mm: `exigir_simulador_apenas`
levanta NotImplementedError se a flag vier ligada.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigMM:
    demo_mm: bool = False          # DESLIGADA por padrao (ver docstring do modulo)
    mensagens_min_max: int = 10    # orcamento proprio, < limite do monitor antimanipulacao (20/min)
    mensagens_sessao_max: int = 300


def exigir_simulador_apenas(cfg: ConfigMM) -> None:
    if cfg.demo_mm:
        raise NotImplementedError("demo_mm: espelhamento na conta demo nao implementado neste modulo; use o simulador")
