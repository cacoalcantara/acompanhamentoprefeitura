"""Politica de risco: carregamento travado por hash e coerencia interna."""
from __future__ import annotations

import math
from pathlib import Path

from ..core.config import ErroConfig, aprovacao_pendente, carregar_risco, carregar_toml, sha256_arquivo
from ..core.instrumentos import PerfilInstrumento
from ..core.tipos import ConfigRisco
from .tempo import hhmm_para_s

TETO_CONTRATOS_V1 = 1  # teto duro da v1 (simulacao): nem a politica aprovada pode exceder
# teto duro da v1 para `volume_max_lotes` (lote fracionario em forex/CFD): 1,0 lote do terminal. Nos perfis B3 a
# unidade ja e >= 1 lote, entao o padrao (0 = desligado) mantem 1 contrato no WIN.
TETO_VOLUME_LOTES_V1 = 1.0
# protecoes do motor em TICKS do instrumento (inteiros: nenhum valor de ativo no codigo). O stop de entrada nunca e
# menor que STOP_MIN_TICKS ticks; qualquer stop/alvo ajustado fica a pelo menos DIST_MIN_STOP_TICKS ticks (+ spread)
# do preco executavel de saida.
STOP_MIN_TICKS = 2
DIST_MIN_STOP_TICKS = 1


def unidades_maximas(cfg: ConfigRisco, volume_por_unidade: float) -> int:
    """Quantidade maxima (unidades do robo) por posicao/exposicao: `contratos_max` ou, com `volume_max_lotes` > 0,
    quantos lotes minimos cabem nele (ex.: 0.05 lote com unidade 0.01 => 5). Nunca acima do teto da v1."""
    if cfg.volume_max_lotes <= 0:
        return cfg.contratos_max
    if volume_por_unidade <= 0:
        return 0
    teto = min(cfg.volume_max_lotes, TETO_VOLUME_LOTES_V1)
    return max(0, int(teto / volume_por_unidade + 1e-9))


def validar_coerencia(cfg: ConfigRisco) -> ConfigRisco:
    erros: list[str] = []
    if not 1 <= cfg.contratos_max <= TETO_CONTRATOS_V1:
        erros.append(f"contratos_max deve estar entre 1 e {TETO_CONTRATOS_V1}")
    if not 0 <= cfg.volume_max_lotes <= TETO_VOLUME_LOTES_V1:
        erros.append(f"volume_max_lotes deve estar entre 0 (desligado) e {TETO_VOLUME_LOTES_V1} lote")
    if cfg.nocional_max_conta < 0:
        erros.append("nocional_max_conta nao pode ser negativo (0 = desligado)")
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
    janela_total_s = hhmm_para_s(cfg.zeragem) - hhmm_para_s(cfg.entradas_de)
    if not 1 <= cfg.tempo_max_posicao_s <= janela_total_s:
        erros.append(f"tempo_max_posicao_s deve estar entre 1 e {janela_total_s} s (de entradas_de ate zeragem)")
    if erros:
        raise ErroConfig("politica de risco incoerente: " + "; ".join(erros))
    return cfg


def carregar_politica(caminho: str | Path, aprovacao: str | Path | None, *, ao_vivo: bool = False) -> ConfigRisco:
    """Recusa iniciar se o hash divergir da aprovacao (core.config.carregar_risco) ou se for incoerente.
    Ao vivo, tambem recusa aprovacao com responsavel pendente."""
    return validar_coerencia(carregar_risco(caminho, aprovacao, ao_vivo=ao_vivo))


ERRO_APROVACAO_PENDENTE = ("aprovacao com responsavel pendente (risco_nao_aprovado): aprove com "
                           "`robowin risco aprovar --responsavel \"<seu nome>\"`")


def hash_politica(caminho: str | Path) -> str:
    return sha256_arquivo(caminho)


def perda_pior_caso_brl(cfg: ConfigRisco, stop_pontos: float, quantidade: int, ponto_valor_brl: float) -> float:
    """Perda maxima de uma operacao: stop + folga de slippage. `ponto_valor_brl` ja NA MOEDA DA CONTA (o chamador
    multiplica pelo fator de conversao do instante quando a moeda de lucro do simbolo difere da conta)."""
    return (stop_pontos + cfg.slippage_folga_pontos) * quantidade * ponto_valor_brl


def stop_maximo_efetivo_pontos(cfg: ConfigRisco, ponto_valor_brl: float, quantidade: int | None = None) -> float:
    """Menor entre stop_max_pontos e o stop que, somado a folga, cabe em perda_max_operacao_brl com `quantidade`
    unidades (None = `contratos_max`, o comportamento de antes)."""
    q = cfg.contratos_max if quantidade is None else quantidade
    por_perda = cfg.perda_max_operacao_brl / (q * ponto_valor_brl) - cfg.slippage_folga_pontos
    return min(cfg.stop_max_pontos, por_perda)


def stop_max_por_quantidade(cfg: ConfigRisco, pv_conta: float, umax: int, restante_dia: float,
                            restante_semana: float, *, tick: float = 0.0) -> tuple[float, ...]:
    """Stop maximo (pontos) por quantidade q = 1..umax (indice q-1), ja descontados perda por operacao, orcamento
    restante do dia e da semana (moeda da conta), folga de slippage e fator de conversao (em `pv_conta`):

        q -> min(stop_max_pontos, min(perda_max_op, restante_dia, restante_semana) / (q * pv_conta) - folga)

    Com `tick` > 0 o valor e levado para BAIXO na grade do tick (o stop anunciado sempre passa no veto). Nunca
    negativo; `pv_conta` invalido => zeros (nenhum stop cabe)."""
    if umax <= 0:
        return ()
    if not (math.isfinite(pv_conta) and pv_conta > 0):
        return tuple(0.0 for _ in range(umax))
    orcamento = min(cfg.perda_max_operacao_brl, restante_dia, restante_semana)
    saida: list[float] = []
    for q in range(1, umax + 1):
        v = min(cfg.stop_max_pontos, orcamento / (q * pv_conta) - cfg.slippage_folga_pontos)
        if tick > 0:
            v = math.floor(v / tick + 1e-9) * tick
        saida.append(max(0.0, round(v, 10)))
    return tuple(saida)


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
            cfg = carregar_politica(caminho, aprovacao, ao_vivo=ao_vivo)
            return cfg, (ERRO_APROVACAO_PENDENTE if aprovacao_pendente(aprovacao, caminho) else None)
        except Exception as e:
            erro = f"{type(e).__name__}: {e}"
            fallback = ConfigRisco()
    else:
        arq, apr = arquivos_de_risco_do_ativo(perfil, caminho, aprovacao)
        if arq.is_file():
            try:
                apr_ok = apr if apr is not None and apr.is_file() else None
                cfg = carregar_politica(arq, apr_ok, ao_vivo=ao_vivo)
                return cfg, (ERRO_APROVACAO_PENDENTE if aprovacao_pendente(apr_ok, arq) else None)
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
