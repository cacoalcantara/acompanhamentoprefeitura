"""Validade e falha segura da decisao (plano 5.2, passos 4 e 6).

Regra unica: qualquer falha da IA ou do dado BLOQUEIA NOVAS ENTRADAS; saidas (ENCERRAR, tempo, stop, alvo,
zeragem) e protecao continuam. Este modulo nao conhece o motor de risco: so classifica e bloqueia.

* ``classificar_chamada``: ChamadaLLM invalida -> motivo normalizado (resposta tardia, invalida, recusa,
  erro HTTP, modelo servido fora da lista, orcamento esgotado, ...);
* ``Prazos``: prazos pre-registrados (imediato: Claude 40 s, GPT 12 s, validade 60 s, margem >= 8 s, 20 pts);
* ``validar_frescor_entrada``: ``agora - t0 <= validade`` e desvio de preco (delegando a ia.validadores);
* ``ajustar_entrada_ancorada`` (braco planejador, docs/16 C20): niveis ancorados no preco de t0; no disparo o stop e
  o alvo restantes sao recalculados a partir do preco executavel, sem perseguir o preco alem da faixa a favor;
* ``BloqueioDados``: lacuna de sequencia e dado velho (bloqueiam entradas por uma retencao).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from robowin.core.tipos import ChamadaLLM, Intencao
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

    # nomes neutros (docs/16 C32): os campos guardados mantem o nome antigo para nao mudar o manifesto de B/C
    @property
    def proponente_s(self) -> float:
        return self.claude_s

    @property
    def revisor_s(self) -> float:
        return self.gpt_s

    def prazo_proponente(self) -> float:
        return self.claude_s

    def prazo_revisor(self, decorrido_s: float) -> float:
        """O revisor so pode usar o que sobra da validade menos a margem do risco/OMS."""
        return min(self.gpt_s, self.validade_s - self.margem_s - decorrido_s)


PRAZOS_IMEDIATO = Prazos(claude_s=40.0, gpt_s=12.0, validade_s=60.0, desvio_max_ticks=4.0, margem_s=8.0)
# condicional: o plano e gerado a cada ~4 min ou por evento e revisado ANTES de armar; expira em 3-10 min.
PRAZOS_CONDICIONAL = Prazos(claude_s=90.0, gpt_s=30.0, validade_s=600.0, desvio_max_ticks=float("inf"),
                            margem_s=0.0)


# planejador (docs/16): proponente 60 s, revisor 20 s; o plano fica armado no maximo 10 min. A ancoragem em t0 substitui
# o desvio simetrico de preco (desvio infinito): o que vale e a faixa assimetrica do proprio plano.
PRAZOS_PLANEJADOR = Prazos(claude_s=60.0, gpt_s=20.0, validade_s=600.0, desvio_max_ticks=float("inf"), margem_s=0.0)


def _valor(d: Mapping[str, Any], nomes: tuple[str, ...], padrao: float) -> float:
    """Primeiro nome presente (o nome neutro tem precedencia sobre o apelido antigo)."""
    for n in nomes:
        if n in d:
            return float(d[n])
    return padrao


def prazos_de_config(cfg: dict[str, Any] | None) -> dict[str, Prazos]:
    """Le ``[prazos]`` de config/robowin.toml; chaves ausentes mantem os valores pre-registrados.

    Aceita os nomes neutros ``proponente_s``/``revisor_s`` como apelidos de ``claude_s``/``gpt_s`` (C32)."""
    cfg = cfg or {}
    p = cfg.get("prazos", {})
    im = p.get("imediato", {})
    co = p.get("condicional", {})
    pl = p.get("planejador", {})
    prop, rev = ("proponente_s", "claude_s"), ("revisor_s", "gpt_s")
    imediato = Prazos(
        claude_s=_valor(im, prop, PRAZOS_IMEDIATO.claude_s),
        gpt_s=_valor(im, rev, PRAZOS_IMEDIATO.gpt_s),
        validade_s=float(im.get("validade_s", PRAZOS_IMEDIATO.validade_s)),
        desvio_max_ticks=float(im.get("desvio_max_ticks", PRAZOS_IMEDIATO.desvio_max_ticks)),
        margem_s=float(im.get("margem_s", PRAZOS_IMEDIATO.margem_s)))
    condicional = Prazos(
        claude_s=_valor(co, prop, PRAZOS_CONDICIONAL.claude_s),
        gpt_s=_valor(co, rev, PRAZOS_CONDICIONAL.gpt_s),
        validade_s=float(co.get("expira_min_max", PRAZOS_CONDICIONAL.validade_s / 60)) * 60.0,
        desvio_max_ticks=PRAZOS_CONDICIONAL.desvio_max_ticks, margem_s=0.0)
    planejador = Prazos(
        claude_s=_valor(pl, prop, PRAZOS_PLANEJADOR.claude_s),
        gpt_s=_valor(pl, rev, PRAZOS_PLANEJADOR.gpt_s),
        validade_s=float(pl.get("expira_min_max", PRAZOS_PLANEJADOR.validade_s / 60)) * 60.0,
        desvio_max_ticks=PRAZOS_PLANEJADOR.desvio_max_ticks, margem_s=0.0)
    return {"imediato": imediato, "condicional": condicional, "planejador": planejador}


# --------------------------------------------------------------------------------------------------
# frescor
# --------------------------------------------------------------------------------------------------
def validar_frescor_entrada(*, t0_ns: int, agora_ns: int, validade_s: float, preco_ref: float,
                            preco_atual: float, desvio_max_pts: float) -> ResultadoValidacao:
    """``agora - t0 <= validade`` e desvio de preco dentro da tolerancia (ia.validadores.validar_frescor)."""
    return validar_frescor(t0_ns=t0_ns, agora_ns=agora_ns, validade_s=int(validade_s), preco_ref=preco_ref,
                           preco_atual=preco_atual, desvio_max_pts=desvio_max_pts)


# --------------------------------------------------------------------------------------------------
# ancoragem em t0 (braco planejador)
# --------------------------------------------------------------------------------------------------
STOP_RESTANTE_INSUFICIENTE = "stop_restante_insuficiente"
ALVO_RESTANTE_INSUFICIENTE = "alvo_restante_insuficiente"
PERSEGUICAO_ACIMA_DO_ACEITO = "perseguicao_acima_do_aceito"
_EPS = 1e-9


def ajustar_entrada_ancorada(it: Intencao, *, preco_exec: float, compra: bool, stop_min: float, alvo_min: float,
                             favor_max: float) -> tuple[Intencao | None, tuple[str, ...]]:
    """Recalcula stop e alvo a partir do preco EXECUTAVEL mantendo FIXOS os niveis absolutos vistos em t0.

    ``d = preco_exec - ancora`` (compra) ou ``ancora - preco_exec`` (venda): ``d > 0`` e o preco que andou a favor
    da tese desde t0. ``stop' = stop + d`` e ``alvo' = alvo - d`` (os niveis nao andam junto com o preco).
    Violacoes: ``stop_restante_insuficiente`` (stop' < stop_min), ``alvo_restante_insuficiente`` (alvo' < alvo_min)
    e ``perseguicao_acima_do_aceito`` (d > favor_max). Com violacao devolve ``(None, motivos)``; senao a Intencao
    com ``preco_referencia=preco_exec``. Nao conhece limites: o motor de risco veta se ``stop'`` estourar a perda.
    Sem ``ancora_t0`` (bracos legados) so troca o preco de referencia."""
    if it.ancora_t0 is None:
        return it.model_copy(update={"preco_referencia": preco_exec}), ()
    vals = (preco_exec, it.ancora_t0, stop_min, alvo_min, favor_max)
    if not all(math.isfinite(v) for v in vals) or preco_exec <= 0:
        return None, ("preco_invalido",)
    d = (preco_exec - it.ancora_t0) if compra else (it.ancora_t0 - preco_exec)
    motivos: list[str] = []
    if d > favor_max + _EPS:
        motivos.append(PERSEGUICAO_ACIMA_DO_ACEITO)
    stop = None if it.stop_pontos is None else round(it.stop_pontos + d, 10)
    alvo = None if it.alvo_pontos is None else round(it.alvo_pontos - d, 10)
    if stop is None or not math.isfinite(stop) or stop < stop_min - _EPS or stop <= 0:
        motivos.append(STOP_RESTANTE_INSUFICIENTE)
    if alvo is not None and (not math.isfinite(alvo) or alvo < alvo_min - _EPS or alvo <= 0):
        motivos.append(ALVO_RESTANTE_INSUFICIENTE)
    if motivos:
        return None, tuple(motivos)
    return it.model_copy(update={"stop_pontos": stop, "alvo_pontos": alvo, "preco_referencia": preco_exec}), ()


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
