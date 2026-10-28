"""Backtest (somente simulacao): replay por disponibilidade, latencia injetada, teste de vazamento."""
from .bracos_referencia import BracoSonda, ControleAleatorio, NaoOperar
from .executor import ExecucaoBacktest
from .latencia_injetada import AmostradorLatencia, DistribuicaoLatencia, carimbo_injetado
from .motor import (
    ConfigBacktest,
    ConfigReflexaoBacktest,
    MotorBacktest,
    ResultadoBacktest,
    executar_cenarios,
    reflexao_de_config,
)
from .replay import (
    EventoReplay,
    FonteArmazem,
    FonteMemoria,
    ordenar_por_disponibilidade,
    ordenar_por_evento,
    tick_no_instante,
)
from .vazamento import (
    RelatorioVazamento,
    dossie_sem_datas,
    perturbar_futuro,
    verificar_causal,
    verificar_perturbacao_futura,
)

__all__ = [
    "AmostradorLatencia", "BracoSonda", "ConfigBacktest", "ConfigReflexaoBacktest", "ControleAleatorio", "DistribuicaoLatencia",
    "EventoReplay", "ExecucaoBacktest", "FonteArmazem", "FonteMemoria", "MotorBacktest", "NaoOperar",
    "RelatorioVazamento", "ResultadoBacktest", "carimbo_injetado", "dossie_sem_datas", "executar_cenarios",
    "ordenar_por_disponibilidade", "ordenar_por_evento", "perturbar_futuro", "verificar_perturbacao_futura",
    "reflexao_de_config", "tick_no_instante", "verificar_causal",
]
