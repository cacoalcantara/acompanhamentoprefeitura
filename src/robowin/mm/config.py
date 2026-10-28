"""Configuracao do market making (experimental).

`demo_mm` esta DESLIGADA por padrao. Com ela desligada o MM so roda no SIMULADOR interno (fila conservadora).
Ligar `demo_mm` significaria espelhar as cotacoes na conta DEMO do MT5 (so para medir confirmacao e
cancelamento; a demo preenche limitada sem fila e NAO e evidencia de lucro), com teto de mensagens dentro da
franquia e espelhamento exclusivo. Essa ponte NAO esta implementada em src/robowin/mm: `exigir_simulador_apenas`
levanta NotImplementedError se a flag vier ligada.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robowin.mt5.capacidades import CapacidadesTerminal


@dataclass(frozen=True)
class ConfigMM:
    demo_mm: bool = False          # DESLIGADA por padrao (ver docstring do modulo)
    mensagens_min_max: int = 10    # orcamento proprio, < limite do monitor antimanipulacao (20/min)
    mensagens_sessao_max: int = 300


def exigir_livro(caps: CapacidadesTerminal) -> None:
    """MM precisa do livro de ofertas: em plataforma sem livro (MT4) o MM fica BLOQUEADO (CapacidadeAusente)."""
    caps.exigir("livro", "market making")


def exigir_simulador_apenas(cfg: ConfigMM, caps: CapacidadesTerminal | None = None) -> None:
    if caps is not None:  # terminal informado: sem livro (MT4) o MM fica bloqueado
        exigir_livro(caps)
    if cfg.demo_mm:
        raise NotImplementedError("demo_mm: espelhamento na conta demo nao implementado neste modulo; use o simulador")
