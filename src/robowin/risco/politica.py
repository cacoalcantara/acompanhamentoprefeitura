"""Politica de risco: carregamento travado por hash e coerencia interna."""
from __future__ import annotations

from pathlib import Path

from ..core.config import ErroConfig, carregar_risco, carregar_toml, sha256_arquivo
from ..core.instrumentos import PerfilInstrumento
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


ROTULO_RISCO_NAO_APROVADO = "risco_nao_aprovado"
OPERACOES_DIA_MAX_CONSERVADOR = 5
PERDAS_SEGUIDAS_MAX_CONSERVADOR = 2


def limites_conservadores(perfil: PerfilInstrumento, base: ConfigRisco) -> ConfigRisco:
    """Limites MINIMOS para um ativo sem politica propria aprovada: parte do config/risco.toml (limites em R$ e
    horarios) e troca stop/folga pelos do PERFIL em ticks (convertidos em pontos do ativo), com menos operacoes."""
    cfg = base.model_copy(update={
        "contratos_max": 1, "stop_max_pontos": perfil.stop_max_pontos,
        "slippage_folga_pontos": perfil.slippage_folga_pontos,
        "operacoes_dia_max": min(base.operacoes_dia_max, OPERACOES_DIA_MAX_CONSERVADOR),
        "perdas_seguidas_max": min(base.perdas_seguidas_max, PERDAS_SEGUIDAS_MAX_CONSERVADOR)})
    validar_coerencia(cfg)
    if stop_maximo_efetivo_pontos(cfg, perfil.ponto_valor_brl) < perfil.tick_pontos:
        raise ErroConfig(f"perfil {perfil.simbolo_base}: perda por operacao de R$ {cfg.perda_max_operacao_brl:.2f} nao "
                         "comporta nem 1 tick de stop mais a folga; ajuste o perfil ou aprove risco proprio")
    return cfg


def arquivos_de_risco_do_ativo(perfil: PerfilInstrumento, caminho: str | Path, aprovacao: str | Path | None
                               ) -> tuple[Path, Path | None]:
    """`risco-<ativo>.toml` (ao lado de risco.toml) e seu registro de aprovacao (ao lado do de risco.toml)."""
    nome = f"risco-{perfil.simbolo_base.lower()}.toml"
    return Path(caminho).with_name(nome), (Path(aprovacao).with_name(nome) if aprovacao else None)


def risco_do_ativo(perfil: PerfilInstrumento, caminho: str | Path, aprovacao: str | Path | None, *,
                   ao_vivo: bool = False) -> tuple[ConfigRisco, str | None]:
    """Politica de risco do ATIVO. Devolve (config, erro); `erro` nao nulo => rotular `risco_nao_aprovado`.

    * ativo cujo config/risco.toml foi aprovado (`risco_aprovado_padrao`): politica travada por hash;
    * outro ativo: exige `risco-<ativo>.toml` com aprovacao de hash; sem isso usa `limites_conservadores`
      (ao vivo, recusa: `ErroConfig`). Limites de um ativo NUNCA valem para outro."""
    erro: str | None = None
    if perfil.risco_aprovado_padrao:
        try:
            return carregar_politica(caminho, aprovacao), None
        except Exception as e:
            erro = f"{type(e).__name__}: {e}"
            fallback = ConfigRisco()
    else:
        arq, apr = arquivos_de_risco_do_ativo(perfil, caminho, aprovacao)
        if arq.is_file():
            try:
                return carregar_politica(arq, apr if apr is not None and apr.is_file() else None), None
            except Exception as e:
                erro = f"{type(e).__name__}: {e}"
        else:
            erro = f"sem {arq.name} aprovado para o ativo {perfil.simbolo_base}"
        try:
            base = ConfigRisco(**carregar_toml(caminho)) if Path(caminho).is_file() else ConfigRisco()
        except Exception:
            base = ConfigRisco()
        fallback = limites_conservadores(perfil, base)
    if ao_vivo:
        raise ErroConfig(f"politica de risco nao aprovada: {erro}")
    return fallback, erro
