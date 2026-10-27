"""Ambiente de execucao (interface Gymnasium-like propria; sem dependencia de gymnasium) sobre REPLAY.

Problema: uma ordem-mae JA APROVADA pelo motor de risco (lado e quantidade fixos; quantidade = 1) precisa ser
executada. A politica escolhe, a cada passo de decisao, entre
  0 = agredir   : ordem a mercado (ponta contraria; executa pelo simulador, com latencia e tarifas);
  1 = apregoar  : limitada no MELHOR preco da propria ponta por `apregoar_s` segundos; se nao executar, e
                  cancelada e o episodio segue (fila CONSERVADORA do SimuladorExecucao);
  2 = aguardar  : nao faz nada ate o proximo passo.
Ao fim do horizonte sem execucao, o ambiente AGRIDE (a ordem-mae aprovada tem de ser executada).

Recompensa (so no passo terminal), em R$: -(custo liquido vs. preco de chegada) =
  -(sinal * (preco_exec - meio_na_chegada) * ponto_valor * qtd + tarifas),   sinal = +1 compra / -1 venda.
Ou seja, inclui meio-spread pago, slippage do preenchimento simulado e tarifas B3. Compra passiva no bid pode
ter custo NEGATIVO (ganho de ~meio-spread) -- exatamente o ganho maximo plausivel.

LIMITE, NAO MEDICAO: o ganho maximo plausivel da execucao otima e da ordem de ~1 tick (R$ 1,00 por contrato)
por operacao. E um teto teorico para dimensionar expectativas; nenhum numero produzido aqui sobre dados
sinteticos e uma medicao de ganho real.

Saidas de protecao (stop, zeragem, kill switch): o ambiente e DESLIGADO -- com `saida_protecao=True` a unica
acao aceita e agredir (as demais sao convertidas em agredir e sinalizadas em `info`).

Determinismo: tudo vem de `numpy.random.default_rng(seed)`; mesma seed + mesmos dados => mesma trajetoria.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core.relogio import RelogioSimulado
from ..core.tipos import ComandoOrdem, Instrumento, Lado, LivroSnapshot, Tick
from ..execucao.custos import Custos
from ..execucao.simulador import SimuladorExecucao
from ..risco.antimanipulacao import LimitesAntimanipulacao

NS = 1_000_000_000
AGREDIR, APREGOAR, AGUARDAR = 0, 1, 2
NOMES_ACOES = ("agredir", "apregoar", "aguardar")
N_ACOES = 3
QTD = 1  # quantidade da ordem-mae: fixa em 1 (invariante verificada a cada execucao)
CHAVES_OBS = ("spread_ticks", "desequilibrio", "tempo_restante", "deriva_ticks", "passiva_ativa", "saida_protecao")
MAGIC = 9
BRACO = "rl"


@dataclass(frozen=True)
class ConfigAmbiente:
    lado: Lado = Lado.COMPRA
    passo_s: float = 5.0              # intervalo entre decisoes
    apregoar_s: float = 5.0           # N: tempo de apregoacao; >= tempo minimo no livro (2 s) da antimanipulacao
    horizonte_s: float = 30.0         # prazo maximo da ordem-mae; depois disso, agride
    latencia_ms: float = 50.0
    fator_fila: float = 1.0
    custos: Custos = field(default_factory=Custos)

    def __post_init__(self) -> None:
        if self.apregoar_s < LimitesAntimanipulacao().tempo_min_livro_s:
            raise ValueError("apregoar_s abaixo do tempo minimo no livro (antimanipulacao)")
        if self.passo_s <= 0 or self.horizonte_s < self.passo_s:
            raise ValueError("passo_s/horizonte_s invalidos")


class AmbienteExecucao:
    """reset(seed, options) -> (obs, info); step(acao) -> (obs, recompensa, terminado, truncado, info)."""

    n_acoes = N_ACOES
    nomes_acoes = NOMES_ACOES

    def __init__(self, ticks: tuple[Tick, ...] | list[Tick], livros: tuple[LivroSnapshot, ...] | list[LivroSnapshot] = (),
                 config: ConfigAmbiente | None = None, instrumento: Instrumento | None = None, seed: int = 0) -> None:
        if len(ticks) < 10:
            raise ValueError("ticks insuficientes")
        self.cfg = config or ConfigAmbiente()
        self.ins = instrumento or Instrumento(simbolo="SINT_WIN")
        self.ticks = list(ticks)
        self.livros = list(livros)
        self._t = np.array([t.t_utc_ns for t in self.ticks], dtype=np.int64)
        self._tl = np.array([lv.t_utc_ns for lv in self.livros], dtype=np.int64)
        self._seed = seed
        self._rng = np.random.default_rng(seed)
        self._sim: SimuladorExecucao | None = None
        self._rel = RelogioSimulado(0)
        self._i = self._il = self._passos = self._qtd_exec = 0
        self._chegada_ns = 0
        self._meio_chegada = 0.0
        self._exec: list[tuple[float, float]] = []
        self._ordem: int | None = None
        self._protecao = False
        self._truncado = False
        self.terminado = True

    # ------------------------------------------------------------------ API ---------------------------
    def inicios_validos(self) -> np.ndarray:
        """Indices de tick onde uma ordem-mae pode chegar (ha pelo menos `horizonte_s` de dados adiante)."""
        lim = self._t[-1] - int(self.cfg.horizonte_s * NS)
        n = int(np.searchsorted(self._t, lim, side="right"))
        if n < 2:
            raise ValueError("dados mais curtos que o horizonte")
        return np.arange(1, n)

    def reset(self, seed: int | None = None, options: dict[str, Any] | None = None) -> tuple[dict[str, float], dict[str, Any]]:
        options = options or {}
        if seed is not None:
            self._seed = seed
            self._rng = np.random.default_rng(seed)
        validos = self.inicios_validos()
        i0 = int(options["inicio"]) if "inicio" in options else int(self._rng.choice(validos))
        if not 1 <= i0 < len(self.ticks):
            raise ValueError("inicio fora da faixa")
        self._protecao = bool(options.get("saida_protecao", False))
        self._rel = RelogioSimulado(int(self._t[i0]))
        self._sim = SimuladorExecucao(
            self.ins, self._rel, custos=self.cfg.custos, latencia_ns=int(self.cfg.latencia_ms * 1e6),
            seed=int(self._rng.integers(0, 2**31 - 1)), fator_fila=self.cfg.fator_fila)
        self._sim.registrar_braco(BRACO, MAGIC)
        self._i = 0
        self._il = 0
        self._alimentar_ate(int(self._t[i0]))  # estado do mercado ate a chegada (inclusive)
        bid, ask = self._cotacao()
        self._chegada_ns = int(self._t[i0])
        self._meio_chegada = (bid + ask) / 2
        self._exec = []
        self._ordem = None
        self._qtd_exec = 0
        self._passos = 0
        self.terminado = False
        self._truncado = False
        return self._obs(), {"inicio": i0, "meio_chegada": self._meio_chegada, "saida_protecao": self._protecao}

    def step(self, acao: int) -> tuple[dict[str, float], float, bool, bool, dict[str, Any]]:
        if self._sim is None or self.terminado:
            raise RuntimeError("episodio terminado; chame reset()")
        if acao not in (AGREDIR, APREGOAR, AGUARDAR):
            raise ValueError(f"acao invalida: {acao}")
        info: dict[str, Any] = {"acao": NOMES_ACOES[acao]}
        t_dec = self._rel.agora_ns()
        if self._protecao and acao != AGREDIR:
            info["acao_convertida"] = f"{NOMES_ACOES[acao]}->agredir (saida de protecao: RL desligado)"
            acao = AGREDIR
        prazo_ns = self._chegada_ns + int(self.cfg.horizonte_s * NS)
        if acao != AGREDIR and prazo_ns - t_dec <= int(self.cfg.passo_s * NS) // 2:
            acao = AGREDIR  # sem tempo: a ordem-mae aprovada tem de ser executada
            info["agressao_forcada_por_prazo"] = True
        if acao == AGREDIR:
            self._agredir(t_dec)
        elif acao == APREGOAR:
            self._apregoar(t_dec)
        else:
            self._avancar_ate(t_dec + int(self.cfg.passo_s * NS))
        self._passos += 1
        if not self.terminado and self._rel.agora_ns() >= prazo_ns:
            self._agredir(self._rel.agora_ns())
            info["agressao_forcada_por_prazo"] = True
        r = 0.0
        if self.terminado:
            r = self._recompensa()
            info.update(self._resumo())
        info["quantidade_executada"] = self._qtd_exec
        return self._obs(), r, self.terminado and not self._truncado, self._truncado, info

    # ------------------------------------------------------------------ mecanica ----------------------
    def _cotacao(self) -> tuple[float, float]:
        t = self.ticks[self._i - 1]
        bid = t.bid if t.bid > 0 else t.last
        ask = t.ask if t.ask > 0 else t.last
        return bid, ask

    def _alimentar_ate(self, t_ns: int) -> None:
        """Alimenta simulador (e relogio) com livros/ticks ate t_ns, inclusive."""
        assert self._sim is not None
        while self._il < len(self._tl) and self._tl[self._il] <= t_ns:
            self._sim.on_livro(self.livros[self._il])
            self._il += 1
        while self._i < len(self.ticks) and self._t[self._i] <= t_ns:
            tk = self.ticks[self._i]
            self._rel.avancar_para(max(self._rel.agora_ns(), tk.t_utc_ns))
            self._i += 1
            for ev in self._sim.on_tick(tk):
                if ev.tipo == "execucao" and ev.execucao is not None:
                    self._registrar_exec(ev.execucao.preco, ev.execucao.quantidade, ev.execucao.taxas_brl)
        self._rel.avancar_para(max(self._rel.agora_ns(), t_ns))

    def _registrar_exec(self, preco: float, qtd: int, taxas: float) -> None:
        self._qtd_exec += qtd
        self._exec.append((preco, taxas))
        if self._qtd_exec > QTD:
            raise AssertionError("ambiente violou quantidade 1")
        if self._qtd_exec == QTD:
            self.terminado = True
            self._ordem = None

    def _avancar_ate(self, t_ns: int) -> None:
        """Avanca ate t_ns, parando quando a ordem-mae executa; sem dados ate t_ns => truncado."""
        while not self.terminado and self._rel.agora_ns() < t_ns:
            if self._i >= len(self.ticks):
                self._truncado = True
                self.terminado = True
                return
            self._alimentar_ate(min(int(self._t[self._i]), t_ns))

    def _enviar(self, tipo: str, preco: float | None) -> int:
        assert self._sim is not None
        ack = self._sim.enviar(ComandoOrdem(intencao_id="rl-mae", magic=MAGIC, lado=self.cfg.lado, tipo=tipo,  # type: ignore[arg-type]
                                            quantidade=QTD, preco=preco))
        if ack.retcode != 10008:
            raise RuntimeError(f"simulador recusou ordem: {ack.retcode} {ack.comentario}")
        return ack.order

    def _agredir(self, t_dec: int) -> None:
        assert self._sim is not None
        if self._ordem is not None:
            self._sim.cancelar(self._ordem)
            self._ordem = None
        self._enviar("mercado", None)
        self._avancar_ate(t_dec + int(self.cfg.latencia_ms * 1e6) + 5 * NS)
        if not self.terminado:  # sem tick posterior a latencia
            self._truncado = True
            self.terminado = True

    def _apregoar(self, t_dec: int) -> None:
        assert self._sim is not None
        bid, ask = self._cotacao()
        preco = bid if self.cfg.lado == Lado.COMPRA else ask
        self._ordem = self._enviar("limitada", preco)
        self._avancar_ate(t_dec + int(self.cfg.apregoar_s * NS))
        if not self.terminado and self._ordem is not None:
            self._sim.cancelar(self._ordem)
            self._ordem = None

    # ------------------------------------------------------------------ observacao e recompensa --------
    def _obs(self) -> dict[str, float]:
        bid, ask = self._cotacao()
        tk = self.ins.tick_pontos
        deseq = 0.0
        if self._il > 0:
            lv = self.livros[self._il - 1]
            vb = sum(n.volume for n in lv.niveis if n.lado == "compra" and abs(n.preco - bid) < 1e-9)
            va = sum(n.volume for n in lv.niveis if n.lado == "venda" and abs(n.preco - ask) < 1e-9)
            deseq = (vb - va) / (vb + va) if vb + va > 0 else 0.0
        sinal = 1.0 if self.cfg.lado == Lado.COMPRA else -1.0
        deriva = sinal * ((bid + ask) / 2 - self._meio_chegada) / tk  # >0 = mercado andando CONTRA a ordem-mae
        resto = max(0.0, (self._chegada_ns + self.cfg.horizonte_s * NS - self._rel.agora_ns()) / (self.cfg.horizonte_s * NS))
        return {"spread_ticks": (ask - bid) / tk, "desequilibrio": float(deseq), "tempo_restante": float(resto),
                "deriva_ticks": float(deriva), "passiva_ativa": 1.0 if self._ordem is not None else 0.0,
                "saida_protecao": 1.0 if self._protecao else 0.0}

    def _custo_brl(self) -> float:
        sinal = 1.0 if self.cfg.lado == Lado.COMPRA else -1.0
        return sum(sinal * (p - self._meio_chegada) * self.ins.ponto_valor_brl + taxas for p, taxas in self._exec)

    def _recompensa(self) -> float:
        if self._qtd_exec != QTD:
            return 0.0  # truncado por fim dos dados: sem recompensa valida
        return -self._custo_brl()

    def _resumo(self) -> dict[str, Any]:
        out: dict[str, Any] = {"truncado": self._truncado, "passos": self._passos}
        if self._qtd_exec == QTD:
            c = self._custo_brl()
            out.update(custo_brl=c, custo_ticks=c / (self.ins.tick_pontos * self.ins.ponto_valor_brl),
                       preco_exec=self._exec[0][0], meio_chegada=self._meio_chegada)
        return out

    @staticmethod
    def vetor(obs: dict[str, float]) -> np.ndarray:
        return np.array([obs[k] for k in CHAVES_OBS], dtype=float)
