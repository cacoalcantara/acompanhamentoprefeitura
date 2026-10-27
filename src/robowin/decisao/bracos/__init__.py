"""Bracos experimentais (plano 5.4): A-quant, B (proponente), C (proponente + revisor), controles e extras."""
from .base import (
    MODOS,
    BracoBase,
    ContextoDecisao,
    Geometria,
    Modo,
    RegistroChamada,
    ResultadoDecisao,
    executar_sync,
)
from .claude import BracoClaude, BracoProponente
from .claude_gpt import BracoClaudeGPT, BracoProponenteRevisor
from .controles import BracoAleatorio, BracoNaoOperar
from .fabrica import ConfigBracoExtra, ErroConfigBraco, FabricaBracos, extras_de_config
from .llm import BracoLLM, PropostaObtida, RevisaoObtida, ServicoProposta, ServicoRevisao
from .quant import BracoQuant

__all__ = [
    "MODOS", "BracoAleatorio", "BracoBase", "BracoClaude", "BracoClaudeGPT", "BracoLLM", "BracoNaoOperar",
    "BracoProponente", "BracoProponenteRevisor", "BracoQuant", "ConfigBracoExtra", "ContextoDecisao",
    "ErroConfigBraco", "FabricaBracos", "Geometria", "Modo", "PropostaObtida", "RegistroChamada",
    "ResultadoDecisao", "RevisaoObtida", "ServicoProposta", "ServicoRevisao", "executar_sync",
    "extras_de_config",
]
