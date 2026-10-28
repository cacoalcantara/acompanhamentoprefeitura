"""Memoria REFLEXIVA com liquidacao tardia (inspirada, so no conceito, em TradingAgents ``memory/reflection`` e
``memory/settlement``, Apache-2.0, e no journal do Aurum, MIT).

Ideia: depois que uma operacao TERMINA e o resultado esta liquidado, um papel de IA (``reflexao``) escreve uma
licao curta. A licao so entra no dossie de ciclos POSTERIORES ao instante em que ficou disponivel
(``t_disponivel_ns``), nunca antes: o backtest/simulacao nao aprende com um resultado que ainda nao existia.

Garantias (todas testadas em ``tests/unit/test_ia_reflexao.py``):

* sem vazamento: ``reflexoes_ate(t)`` / ``enriquecer(.., t)`` so devolvem reflexoes com ``t_disponivel_ns <= t``;
  ``liquidar`` recusa uma liquidacao antes de ``t_saida_ns + atraso`` (``ErroVazamentoReflexao``) e grava
  ``t_disponivel_ns = t + latencia da chamada`` (a reflexao nao existe enquanto a IA ainda escreve);
* o texto do LLM e DADO, nunca instrucao nem identificador: sem chaves/crases, sem indicador tecnico, sem data,
  nome de ativo ou nivel absoluto de preco (mesma checagem de anonimizacao do dossie), tamanho limitado; texto
  reprovado e DESCARTADO (a operacao fica na memoria so com o resultado numerico);
* falha segura: erro, prazo ou orcamento esgotado da IA nao bloqueia nada (a reflexao e opcional); apos
  ``max_tentativas`` falhas a entrada e descartada (``reflexao_falhou``);
* e so INFORMACAO para o proponente: nao altera risco, stops nem limites, e uma unica operacao e amostra minima
  (o prompt diz isso). Nada aqui prova que reflexoes melhoram o resultado: e hipotese a medir (braco com e sem).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from robowin.core.tipos import ChamadaLLM
from robowin.ia.dossie import canonico, verificar_anonimizacao
from robowin.ia.esquemas import ReflexaoIA
from robowin.ia.validadores import validar_textos_livres

NS = 1_000_000_000
MAX_CHARS_LICAO = 240
_RE_PERIGOSO = re.compile(r"[{}\[\]`<>\\]|ignore|desconsidere|system|instruc", re.IGNORECASE)

_RE_NUMERO_GRANDE = re.compile(r"\d{4,}|\d{1,3}(?:[.,]\d{3})+")  # nivel absoluto, ano ou codigo: so numeros pequenos

Estado = Literal["pendente", "em_liquidacao", "liquidada", "descartada"]


class ErroVazamentoReflexao(Exception):
    """Tentativa de liquidar/ler uma reflexao antes do instante permitido (usaria o futuro)."""


@dataclass
class EntradaReflexiva:
    trade_id: str
    braco: str
    t_saida_ns: int
    acao: str
    resultado_pontos: float
    duracao_s: int
    motivo_saida: str
    resumo_decisao: dict[str, Any] = field(default_factory=dict)  # hipoteses/justificativa da proposta (anonimizadas)
    estado: Estado = "pendente"
    tentativas: int = 0
    licao: str | None = None
    aspecto: str | None = None
    vies: str | None = None
    t_liquidacao_ns: int | None = None  # instante em que a liquidacao foi pedida
    t_disponivel_ns: int | None = None  # instante a partir do qual o dossie pode usar a licao
    motivo_descarte: str = ""

    def auditavel(self) -> dict[str, Any]:
        return {"trade_id": self.trade_id, "braco": self.braco, "estado": self.estado, "t_saida_ns": self.t_saida_ns,
                "t_liquidacao_ns": self.t_liquidacao_ns, "t_disponivel_ns": self.t_disponivel_ns,
                "resultado_pontos": self.resultado_pontos, "licao": self.licao, "aspecto": self.aspecto,
                "vies": self.vies, "tentativas": self.tentativas, "motivo_descarte": self.motivo_descarte}


def sanitizar_licao(texto: str, niveis_absolutos: Iterable[float] = (), max_chars: int = MAX_CHARS_LICAO) -> str:
    """Devolve o texto limpo ou levanta ``ValueError`` com o motivo (a entrada vira ``descartada``)."""
    t = " ".join(texto.split())  # sem quebras de linha nem espacos repetidos
    if not t:
        raise ValueError("licao vazia")
    if len(t) > max_chars:
        raise ValueError(f"licao com mais de {max_chars} caracteres")
    if _RE_PERIGOSO.search(t):
        raise ValueError("licao com caractere/termo de instrucao ou marcacao")
    if _RE_NUMERO_GRANDE.search(t):
        raise ValueError("licao com numero grande (possivel nivel absoluto de preco, ano ou codigo)")
    viol = validar_textos_livres((t,), max_chars=max_chars)
    viol += verificar_anonimizacao({"memoria": [{"reflexao": t}]}, niveis_absolutos)
    if viol:
        raise ValueError("; ".join(viol)[:200])
    return t


class MemoriaReflexiva:
    """Pendentes -> liquidadas, por braco, com o instante de disponibilidade de cada reflexao."""

    def __init__(self, atraso_liquidacao_s: float = 300.0, max_reflexoes: int = 3, max_tentativas: int = 3) -> None:
        if atraso_liquidacao_s < 0 or max_reflexoes < 0 or max_tentativas < 1:
            raise ValueError("parametros invalidos da memoria reflexiva")
        self.atraso_ns = int(atraso_liquidacao_s * NS)
        self.max_reflexoes = max_reflexoes
        self.max_tentativas = max_tentativas
        self._entradas: dict[str, EntradaReflexiva] = {}

    # --- escrita -------------------------------------------------------------------------------------------------
    def registrar_trade(self, *, trade_id: str, braco: str, t_saida_ns: int, acao: str, resultado_pontos: float,
                        duracao_s: int, motivo_saida: str,
                        resumo_decisao: dict[str, Any] | None = None) -> EntradaReflexiva:
        """Grava a operacao como PENDENTE (idempotente por ``trade_id``). Nenhuma reflexao existe ainda."""
        if trade_id in self._entradas:
            return self._entradas[trade_id]
        e = EntradaReflexiva(trade_id, braco, t_saida_ns, acao, float(resultado_pontos), int(duracao_s), motivo_saida,
                             dict(resumo_decisao or {}))
        self._entradas[trade_id] = e
        return e

    def prontas(self, t_ns: int) -> list[EntradaReflexiva]:
        """Pendentes cujo atraso de liquidacao ja passou em ``t_ns``; marca-as ``em_liquidacao`` (sem duplicar)."""
        out = [e for e in self._entradas.values()
               if e.estado == "pendente" and t_ns >= e.t_saida_ns + self.atraso_ns]
        for e in out:
            e.estado = "em_liquidacao"
        return sorted(out, key=lambda e: (e.t_saida_ns, e.trade_id))

    def liquidar(self, trade_id: str, reflexao: ReflexaoIA | None, *, t_ns: int, latencia_ns: int = 0,
                 niveis_absolutos: Iterable[float] = ()) -> EntradaReflexiva:
        """Registra o resultado do pedido de reflexao feito em ``t_ns``. ``reflexao=None`` = falha da IA."""
        e = self._entradas[trade_id]
        if t_ns < e.t_saida_ns + self.atraso_ns:
            raise ErroVazamentoReflexao(
                f"liquidacao de {trade_id} em t={t_ns} antes de t_saida+atraso={e.t_saida_ns + self.atraso_ns}")
        e.tentativas += 1
        e.t_liquidacao_ns = t_ns
        if reflexao is None:
            if e.tentativas >= self.max_tentativas:
                e.estado, e.motivo_descarte = "descartada", "reflexao_falhou"
            else:
                e.estado = "pendente"  # tenta de novo em um ciclo posterior
            return e
        try:
            e.licao = sanitizar_licao(reflexao.licao, niveis_absolutos)
        except ValueError as exc:
            e.licao, e.estado, e.motivo_descarte = None, "descartada", f"texto_reprovado: {exc}"
            return e
        e.aspecto, e.vies = reflexao.aspecto, reflexao.vies
        e.estado = "liquidada"
        e.t_disponivel_ns = t_ns + max(int(latencia_ns), 0)
        return e

    # --- leitura (SEM vazamento) -------------------------------------------------------------------------------------
    def reflexoes_ate(self, braco: str, t_ns: int, n: int | None = None) -> list[EntradaReflexiva]:
        """Reflexoes liquidadas do braco JA DISPONIVEIS em ``t_ns`` (mais recentes por ultimo)."""
        out = sorted((e for e in self._entradas.values()
                      if e.braco == braco and e.estado == "liquidada" and e.t_disponivel_ns is not None
                      and e.t_disponivel_ns <= t_ns), key=lambda e: (e.t_saida_ns, e.trade_id))
        lim = self.max_reflexoes if n is None else n
        return out[-lim:] if lim > 0 else []

    def enriquecer(self, memoria: Sequence[dict[str, Any]], braco: str, t_ns: int) -> list[dict[str, Any]]:
        """Copia da memoria de operacoes com ``reflexao`` nas entradas (por ``trade_id``) cuja licao ja existe em
        ``t_ns``. As demais passam intactas (o dossie so ganha o campo quando ha reflexao disponivel)."""
        disp = {e.trade_id: e.licao for e in self.reflexoes_ate(braco, t_ns)}
        out: list[dict[str, Any]] = []
        for m in memoria:
            m2 = dict(m)
            lic = disp.get(str(m.get("trade_id", "")))
            if lic:
                m2["reflexao"] = lic
            out.append(m2)
        return out

    def exportar(self) -> list[dict[str, Any]]:
        return [e.auditavel() for e in sorted(self._entradas.values(), key=lambda e: (e.t_saida_ns, e.trade_id))]

    def __len__(self) -> int:
        return len(self._entradas)


def mensagem_reflexao(e: EntradaReflexiva) -> str:
    """Resumo ANONIMIZADO da operacao liquidada (sem datas, sem niveis absolutos, sem nome de ativo)."""
    res = {"acao": e.acao, "resultado_pontos": round(e.resultado_pontos, 2), "duracao_s": e.duracao_s,
           "motivo_saida": e.motivo_saida}
    return ("OPERACAO LIQUIDADA (JSON anonimizado):\n" + canonico({"decisao": e.resumo_decisao, "resultado": res}))


async def liquidar_pendentes(mem: MemoriaReflexiva, t_ns: int, servico: Any, *, prazo_s: float = 30.0,
                             niveis_absolutos: Iterable[float] = ()) -> list[tuple[str, ChamadaLLM]]:
    """Pede a reflexao das operacoes prontas (uma chamada por operacao) e a liquida. Devolve (braco, chamada)
    para custeio. ``servico``: ``ServicoLLM`` com esquema ``ReflexaoIA`` (``chamar(mensagem, prazo_s, hash)``)."""
    chamadas: list[tuple[str, ChamadaLLM]] = []
    for e in mem.prontas(t_ns):
        try:
            obj, ch, motivo = await servico.chamar(mensagem_reflexao(e), prazo_s, "")
        except Exception:  # a reflexao e opcional: nunca derruba o ciclo
            mem.liquidar(e.trade_id, None, t_ns=t_ns)
            continue
        chamadas.append((e.braco, ch))
        ok = motivo is None and isinstance(obj, ReflexaoIA)
        mem.liquidar(e.trade_id, obj if ok else None, t_ns=t_ns, latencia_ns=int(ch.total_ms * 1e6),
                     niveis_absolutos=niveis_absolutos)
    return chamadas
