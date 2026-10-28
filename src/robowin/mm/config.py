"""Configuracao do market making (experimental).

`demo_mm` esta DESLIGADA por padrao. Com ela desligada o MM so roda no SIMULADOR interno (fila conservadora).
Ligada, ``mm/ponte_demo.py`` espelha as cotacoes na conta DEMO do MT5 pelo gateway (so para medir confirmacao,
modificacao e cancelamento; a demo preenche limitada sem fila e NAO e evidencia de lucro), com:

* teto de mensagens dentro da franquia (``mensagens_min_max`` e o monitor ``risco/antimanipulacao``) e tempo
  minimo no livro antes de alterar/cancelar (2 s);
* espelhamento EXCLUSIVO: ou um braco do nucleo (``[geral].espelhar``) ou o MM, nunca os dois (trava de arquivo
  ``execucao.ponte_gateway.TravaEspelho`` + validacao de configuracao);
* a capacidade ``livro`` do terminal (sem livro de ofertas nao ha MM).
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
    """Valida a plataforma para o MM: sem livro (MT4) o MM fica bloqueado. demo_mm e permitido (ponte na demo)."""
    if caps is not None:
        exigir_livro(caps)
