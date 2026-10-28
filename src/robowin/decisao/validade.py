"""Validade e falha segura da decisao (plano 5.2, passos 4 e 6).

Regra unica: qualquer falha da IA ou do dado BLOQUEIA NOVAS ENTRADAS; saidas (ENCERRAR, tempo, stop, alvo,
zeragem) e protecao continuam. Este modulo nao conhece o motor de risco: so classifica e bloqueia.

* ``classificar_chamada``: ChamadaLLM invalida -> motivo normalizado (resposta tardia, invalida, recusa,
  erro HTTP, modelo servido fora da lista, orcamento esgotado, ...);
* ``Prazos``: prazos pre-registrados (imediato: Claude 40 s, GPT 12 s, validade 60 s, margem >= 8 s, 20 pts);
* ``validar_frescor_entrada``: ``agora - t0 <= validade`` e desvio de preco (delegando a ia.validadores);
* ``BloqueioDados``: lacuna de sequencia e dado velho (bloqueiam entradas por uma retencao).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robowin.core.tipos import ChamadaLLM
from robowin.ia.validadores import ResultadoValidacao, validar_frescor

NS = 1_000_000_000

# motivos normalizados (aparecem em decisoes.motivos como "ia:<motivo>")
RESPOSTA_TARDIA = "resposta_tardia"
RESPOSTA_INVALIDA = "resposta_invalida"
RECUSA = "recusa"
MAX_TOKENS = "max_tokens"
ERRO_HTTP = "erro_http"
MODELO_FORA_DA_LISTA = "modelo_fora_da_lista"
ORCAMENTO_ESGOTADO = "orcamento_esgotado"
CACHE_MISS = "cache_miss"
ERRO_PROVEDOR = "erro_provedor"
DIVERGENCIA = "divergencia_revisor"
SEM_TEMPO_REVISAO = "sem_tempo_para_revisao"


def classificar_chamada(ch: ChamadaLLM) -> str | None:
    """None se a chamada e valida; senao o motivo normalizado que bloqueia a nova entrada."""
    if ch.valida:
        return None
    erro = (ch.erro or "").lower()
    if erro.startswith("orcamento_esgotado"):
        return ORCAMENTO_ESGOTADO
    if erro == "timeout":
        return RESPOSTA_TARDIA
    if erro == "recusa" or ch.categoria_recusa:
        return RECUSA
    if erro == "max_tokens":
        return MAX_TOKENS
    if erro.startswith("http_") or "429" in erro or "529" in erro:
        return ERRO_HTTP
    if erro.startswith("modelo_servido_fora_da_lista"):
        return MODELO_FORA_DA_LISTA
    if erro == "cache_miss":
        return CACHE_MISS
    if erro in ("json_invalido", "") or "json" in erro or "validation" in erro or "parse" in erro:
        return RESPOSTA_INVALIDA
    return ERRO_PROVEDOR


# --------------------------------------------------------------------------------------------------
# prazos pre-registrados
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Prazos:
    """Prazos de um modo. Mudar qualquer valor gera NOVA variante (analisada separadamente)."""
    claude_s: float
    gpt_s: float
    validade_s: float
    desvio_max_ticks: float
    margem_s: float = 0.0  # reservada ao motor de risco e ao OMS

    def prazo_proponente(self) -> float:
        return self.claude_s

    def prazo_revisor(self, decorrido_s: float) -> float:
        """O revisor so pode usar o que sobra da validade menos a margem do risco/OMS."""
        return min(self.gpt_s, self.validade_s - self.margem_s - decorrido_s)


PRAZOS_IMEDIATO = Prazos(claude_s=40.0, gpt_s=12.0, validade_s=60.0, desvio_max_ticks=4.0, margem_s=8.0)
# condicional: o plano e gerado a cada ~4 min ou por evento e revisado ANTES de armar; expira em 3-10 min.
PRAZOS_CONDICIONAL = Prazos(claude_s=90.0, gpt_s=30.0, validade_s=600.0, desvio_max_ticks=float("inf"),
                            margem_s=0.0)


def prazos_de_config(cfg: dict[str, Any] | None) -> dict[str, Prazos]:
    """Le ``[prazos]`` de config/robowin.toml; chaves ausentes mantem os valores pre-registrados."""
    cfg = cfg or {}
    p = cfg.get("prazos", {})
    im = p.get("imediato", {})
    co = p.get("condicional", {})
    imediato = Prazos(
        claude_s=float(im.get("claude_s", PRAZOS_IMEDIATO.claude_s)),
        gpt_s=float(im.get("gpt_s", PRAZOS_IMEDIATO.gpt_s)),
        validade_s=float(im.get("validade_s", PRAZOS_IMEDIATO.validade_s)),
        desvio_max_ticks=float(im.get("desvio_max_ticks", PRAZOS_IMEDIATO.desvio_max_ticks)),
        margem_s=float(im.get("margem_s", PRAZOS_IMEDIATO.margem_s)))
    condicional = Prazos(
        claude_s=float(co.get("claude_s", PRAZOS_CONDICIONAL.claude_s)),
        gpt_s=float(co.get("gpt_s", PRAZOS_CONDICIONAL.gpt_s)),
        validade_s=float(co.get("expira_min_max", PRAZOS_CONDICIONAL.validade_s / 60)) * 60.0,
        desvio_max_ticks=PRAZOS_CONDICIONAL.desvio_max_ticks, margem_s=0.0)
    return {"imediato": imediato, "condicional": condicional}


# --------------------------------------------------------------------------------------------------
# frescor
# --------------------------------------------------------------------------------------------------
def validar_frescor_entrada(*, t0_ns: int, agora_ns: int, validade_s: float, preco_ref: float,
                            preco_atual: float, desvio_max_pts: float) -> ResultadoValidacao:
    """``agora - t0 <= validade`` e desvio de preco dentro da tolerancia (ia.validadores.validar_frescor)."""
    return validar_frescor(t0_ns=t0_ns, agora_ns=agora_ns, validade_s=int(validade_s), preco_ref=preco_ref,
                           preco_atual=preco_atual, desvio_max_pts=desvio_max_pts)


# --------------------------------------------------------------------------------------------------
# bloqueios de dado
# --------------------------------------------------------------------------------------------------
class BloqueioDados:
    """Lacuna de sequencia e dado velho: bloqueiam NOVAS entradas. Saidas e protecao nao dependem disto.

    A lacuna segura as entradas por ``retencao_lacuna_s`` apos a ULTIMA lacuna observada (o book/ticks
    podem estar inconsistentes ate o backfill); ``dado_velho`` e recalculado a cada consulta.
    """

    def __init__(self, retencao_lacuna_s: float = 30.0, idade_max_dado_s: float = 15.0) -> None:
        self.retencao_ns = int(retencao_lacuna_s * NS)
        self.idade_max_ns = int(idade_max_dado_s * NS)
        self._bloqueado_ate_ns = 0
        self._ultimo_dado_ns: int | None = None
        self._tipo = "lacuna_sequencia"
        self.lacunas = 0
        self.historico: list[tuple[int, str, str]] = []

    def dado_recebido(self, t_ns: int) -> None:
        """Registra o instante do ultimo dado. Se o intervalo desde o anterior estourou a idade maxima,
        o buraco e tratado como lacuna (feed parado + backfill)."""
        if self._ultimo_dado_ns is not None and t_ns - self._ultimo_dado_ns > self.idade_max_ns:
            self.marcar("dado_velho", t_ns, f"intervalo de {(t_ns - self._ultimo_dado_ns) / NS:.1f}s sem dados")
        self._ultimo_dado_ns = t_ns

    def lacuna_sequencia(self, t_ns: int, detalhe: str = "") -> None:
        self.lacunas += 1
        self.marcar("lacuna_sequencia", t_ns, detalhe)

    def marcar(self, tipo: str, t_ns: int, detalhe: str = "") -> None:
        self._bloqueado_ate_ns = max(self._bloqueado_ate_ns, t_ns + self.retencao_ns)
        self.historico.append((t_ns, tipo, detalhe))
        self._tipo = tipo

    def motivo(self, t_ns: int) -> str:
        """'' se livre; senao o motivo que bloqueia entradas agora."""
        if t_ns < self._bloqueado_ate_ns:
            return self._tipo
        if self._ultimo_dado_ns is not None and t_ns - self._ultimo_dado_ns > self.idade_max_ns:
            return "dado_velho"
        return ""

    def bloqueado(self, t_ns: int) -> bool:
        return bool(self.motivo(t_ns))
