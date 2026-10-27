"""Politica de risco: carregamento travado por hash e coerencia interna."""
from __future__ import annotations

from pathlib import Path

from ..core.config import ErroConfig, carregar_risco, sha256_arquivo
from ..core.tipos import ConfigRisco
from .tempo import hhmm_para_s

TETO_CONTRATOS_V1 = 1  # teto duro da v1 (simulacao): nem a politica aprovada pode exceder


def validar_coerencia(cfg: ConfigRisco) -> ConfigRisco:
    erros: list[str] = []
    if not 1 <= cfg.contratos_max <= TETO_CONTRATOS_V1:
        erros.append(f"contratos_max deve estar entre 1 e {TETO_CONTRATOS_V1}")
    for nome in ("perda_max_operacao_brl", "perda_max_dia_brl", "perda_max_semana_brl", "dd_max_brl",
                 "stop_max_pontos", "tempo_min_livro_s"):
        if getattr(cfg, nome) <= 0:
            erros.append(f"{nome} deve ser positivo")
    if cfg.slippage_folga_pontos < 0:
        erros.append("slippage_folga_pontos nao pode ser negativo")
    if not cfg.perda_max_operacao_brl <= cfg.perda_max_dia_brl <= cfg.perda_max_semana_brl:
        erros.append("esperado perda_operacao <= perda_dia <= perda_semana")
    if cfg.dd_alerta_brl > cfg.dd_max_brl:
        erros.append("dd_alerta_brl nao pode exceder dd_max_brl")
    if not hhmm_para_s(cfg.entradas_de) < hhmm_para_s(cfg.entradas_ate) <= hhmm_para_s(cfg.zeragem):
        erros.append("esperado entradas_de < entradas_ate <= zeragem")
    if cfg.operacoes_dia_max < 1 or cfg.perdas_seguidas_max < 1 or cfg.pausa_apos_perda_s < 0:
        erros.append("limites de operacoes/perdas/pausa invalidos")
    if cfg.bloqueio_evento_min < 0 or cfg.ordens_min_max < 1:
        erros.append("bloqueio_evento_min/ordens_min_max invalidos")
    if erros:
        raise ErroConfig("politica de risco incoerente: " + "; ".join(erros))
    return cfg


def carregar_politica(caminho: str | Path, aprovacao: str | Path | None) -> ConfigRisco:
    """Recusa iniciar se o hash divergir da aprovacao (core.config.carregar_risco) ou se for incoerente."""
    return validar_coerencia(carregar_risco(caminho, aprovacao))


def hash_politica(caminho: str | Path) -> str:
    return sha256_arquivo(caminho)


def perda_pior_caso_brl(cfg: ConfigRisco, stop_pontos: float, quantidade: int, ponto_valor_brl: float) -> float:
    """Perda maxima de uma operacao: stop + folga de slippage."""
    return (stop_pontos + cfg.slippage_folga_pontos) * quantidade * ponto_valor_brl


def stop_maximo_efetivo_pontos(cfg: ConfigRisco, ponto_valor_brl: float) -> float:
    """Menor entre stop_max_pontos e o stop que, somado a folga, cabe em perda_max_operacao_brl."""
    por_perda = cfg.perda_max_operacao_brl / (cfg.contratos_max * ponto_valor_brl) - cfg.slippage_folga_pontos
    return min(cfg.stop_max_pontos, por_perda)
