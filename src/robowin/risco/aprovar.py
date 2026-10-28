"""Aprovacao GUIADA da politica de risco (`robowin risco aprovar`): decisao que SO o usuario pode tomar.

Fluxo: mostra os limites de config/risco.toml em pt-BR, calcula o sha256 EXATAMENTE como a verificacao
(`core.config.sha256_arquivo` sobre os bytes do arquivo), exige a palavra APROVO e grava um NOVO registro
`config/aprovacoes/risco-v<N+1>.toml` (nunca sobrescreve um registro existente), registrando o evento na cadeia de
auditoria. O carregamento (`core.config.resolver_aprovacao`) passa a usar a versao mais recente valida.
"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import (
    ErroConfig,
    carregar_toml,
    resolver_aprovacao,
    responsavel_pendente,
    sha256_arquivo,
    versoes_aprovacao,
)
from ..core.tipos import ConfigRisco
from .politica import validar_coerencia

PALAVRA_APROVACAO = "APROVO"
PREFIXO_REGISTRO = "risco"


class AprovacaoRecusada(ErroConfig):
    """A aprovacao nao foi gravada (responsavel invalido, confirmacao errada, politica incoerente...)."""


def _num(v: float, casas: int = 2) -> str:
    """Numero no formato brasileiro (1.500,00)."""
    return f"{v:,.{casas}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def reais(v: float) -> str:
    return f"R$ {_num(v)}"


def descrever_limites(cfg: ConfigRisco) -> list[str]:
    """Limites da politica em pt-BR legivel (o que o usuario esta aprovando)."""
    pausa_min = cfg.pausa_apos_perda_s / 60
    return [
        f"Contratos por operacao (maximo) .......... {cfg.contratos_max}",
        f"Perda maxima por operacao ................ {reais(cfg.perda_max_operacao_brl)}",
        f"Perda maxima no dia ...................... {reais(cfg.perda_max_dia_brl)}",
        f"Perda maxima na semana ................... {reais(cfg.perda_max_semana_brl)}",
        f"Drawdown: alerta / maximo ................ {reais(cfg.dd_alerta_brl)} / {reais(cfg.dd_max_brl)}",
        f"Operacoes por dia (maximo) ............... {cfg.operacoes_dia_max}",
        f"Perdas seguidas antes de parar ........... {cfg.perdas_seguidas_max}",
        f"Pausa apos uma perda ..................... {cfg.pausa_apos_perda_s} s ({_num(pausa_min, 1)} min)",
        f"Stop maximo / folga de slippage .......... {_num(cfg.stop_max_pontos, 1)} / "
        f"{_num(cfg.slippage_folga_pontos, 1)} pontos (o perfil do ativo pode reduzir)",
        f"Janela de novas entradas ................. {cfg.entradas_de} ate {cfg.entradas_ate}",
        f"Zeragem obrigatoria ...................... {cfg.zeragem}",
        f"Bloqueio em torno de evento de agenda .... {cfg.bloqueio_evento_min} min",
        f"Ordens por minuto (maximo) ............... {cfg.ordens_min_max}",
        f"Tempo minimo de livro antes de entrar .... {_num(cfg.tempo_min_livro_s, 1)} s",
    ]


def proximo_registro(dir_aprovacoes: str | Path) -> Path:
    """Caminho do proximo `risco-vN.toml` (N = maior existente + 1). Nunca aponta para arquivo existente."""
    d = Path(dir_aprovacoes)
    versoes = versoes_aprovacao(d / f"{PREFIXO_REGISTRO}-v1.toml")
    n = (versoes[0][0] if versoes else 0) + 1
    return d / f"{PREFIXO_REGISTRO}-v{n}.toml"


def validar_responsavel(responsavel: str) -> str:
    r = (responsavel or "").strip()
    if responsavel_pendente({"responsavel": r}):
        raise AprovacaoRecusada("responsavel vazio ou 'pendente...': a aprovacao precisa do nome de quem assume o risco")
    if "\n" in r or len(r) > 200:
        raise AprovacaoRecusada("responsavel invalido (uma linha, ate 200 caracteres)")
    return r


@dataclass(frozen=True)
class ResultadoAprovacao:
    arquivo: Path
    sha256: str
    versao_anterior: Path | None


def _toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)  # string basica TOML (aspas e escapes compativeis)


def aprovar_politica(risco: str | Path, dir_aprovacoes: str | Path, responsavel: str, motivo: str, *,
                     confirmar: Callable[[list[str]], str], auditoria: Any | None = None,
                     hoje: dt.date | None = None, agora_utc: dt.datetime | None = None) -> ResultadoAprovacao:
    """Aprova a politica de `risco`: valida, mostra o resumo a `confirmar` (que devolve o texto digitado) e, so com
    'APROVO', grava o novo registro e o evento de auditoria. Qualquer recusa levanta `AprovacaoRecusada` sem
    criar nem alterar arquivo algum."""
    responsavel = validar_responsavel(responsavel)
    motivo = (motivo or "").strip() or "aprovacao pelo responsavel via robowin risco aprovar"
    risco = Path(risco)
    if not risco.is_file():
        raise AprovacaoRecusada(f"arquivo de risco nao encontrado: {risco}")
    try:
        cfg = validar_coerencia(ConfigRisco(**carregar_toml(risco)))
    except ErroConfig as e:
        raise AprovacaoRecusada(f"politica nao pode ser aprovada: {e}") from e
    except Exception as e:  # noqa: BLE001 - TOML/campo invalido
        raise AprovacaoRecusada(f"config de risco invalida: {e}") from e
    sha = sha256_arquivo(risco)  # o MESMO calculo de core.config.carregar_risco
    d = Path(dir_aprovacoes)
    anterior = resolver_aprovacao(d / f"{PREFIXO_REGISTRO}-v1.toml", risco)
    destino = proximo_registro(d)
    resumo = [f"Arquivo: {risco}", f"sha256:  {sha}", "", *descrever_limites(cfg), "",
              f"Responsavel: {responsavel}", f"Motivo: {motivo}", f"Registro a gravar: {destino}"]
    digitado = confirmar(resumo)
    if (digitado or "").strip() != PALAVRA_APROVACAO:
        raise AprovacaoRecusada(f"confirmacao nao recebida (era preciso digitar {PALAVRA_APROVACAO}): nada foi alterado")
    if sha256_arquivo(risco) != sha:  # alterado durante a confirmacao
        raise AprovacaoRecusada("config de risco mudou durante a confirmacao: nada foi gravado; rode de novo")
    hoje = hoje or dt.date.today()
    agora_utc = agora_utc or dt.datetime.now(dt.UTC)
    texto = (
        "# Registro de aprovacao da politica de risco gerado por `robowin risco aprovar`. NAO edite: para mudar,\n"
        "# altere config/risco.toml e aprove de novo (gera a proxima versao; as anteriores ficam como historico).\n"
        f"sha256 = {_toml_str(sha)}\n"
        f"data = {_toml_str(hoje.isoformat())}\n"
        f"responsavel = {_toml_str(responsavel)}\n"
        f"motivo = {_toml_str(motivo)}\n"
        f"arquivo = {_toml_str(risco.name)}\n"
        f"aprovado_em_utc = {_toml_str(agora_utc.replace(microsecond=0).isoformat())}\n"
    )
    d.mkdir(parents=True, exist_ok=True)
    try:
        with open(destino, "x", encoding="utf-8", newline="\n") as f:  # "x": falha se ja existir (nunca sobrescreve)
            f.write(texto)
    except FileExistsError as e:
        raise AprovacaoRecusada(f"{destino.name} ja existe (aprovacao concorrente?): nada foi sobrescrito") from e
    if auditoria is not None:
        auditoria.registrar("risco_aprovado", {
            "arquivo_risco": str(risco), "sha256": sha, "registro": destino.name, "responsavel": responsavel,
            "motivo": motivo, "data": hoje.isoformat(),
            "registro_anterior": anterior.name if anterior is not None and anterior.is_file() else None})
    return ResultadoAprovacao(destino, sha, anterior if anterior is not None and anterior.is_file() else None)


def estado_aprovacao(risco: str | Path, aprovacao: str | Path) -> tuple[str, str]:
    """('APROVADO' | 'PENDENTE' | 'INVALIDO', detalhe) da aprovacao efetiva de `risco`."""
    from .politica import carregar_politica
    efetiva = resolver_aprovacao(aprovacao, risco)
    try:
        carregar_politica(risco, efetiva)
    except Exception as e:  # noqa: BLE001
        return "INVALIDO", f"{e}"
    assert efetiva is not None
    reg = carregar_toml(efetiva)
    if responsavel_pendente(reg):
        return "PENDENTE", f"{efetiva.name}: hash confere, responsavel '{reg.get('responsavel', '')}'"
    return "APROVADO", f"{efetiva.name}: {reg.get('responsavel')} em {reg.get('data', '?')}"
