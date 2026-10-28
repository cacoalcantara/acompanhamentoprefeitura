"""Roda o CotadorMM no SimuladorExecucao (fila conservadora, latencia, tarifas B3). SOMENTE simulacao."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..core.relogio import RelogioSimulado
from ..core.tipos import ComandoOrdem, Instrumento, Lado, LivroSnapshot, Tick
from ..execucao.simulador import SimuladorExecucao
from .config import ConfigMM, exigir_simulador_apenas
from .cotacao import CotadorMM
from .markouts import Preenchimento

NS = 1_000_000_000
MAGIC = 11
BRACO = "mm"


@dataclass
class ResultadoMM:
    fills: list[Preenchimento] = field(default_factory=list)
    mensagens: dict[str, int] = field(default_factory=dict)
    max_abs_estoque: int = 0
    liquido_brl: float = 0.0
    estoque_final: int = 0
    n_decisoes: int = 0


def _microprice(t: Tick, livro: LivroSnapshot | None) -> float:
    bid, ask = (t.bid if t.bid > 0 else t.last), (t.ask if t.ask > 0 else t.last)
    if livro is not None:
        vb = sum(n.volume for n in livro.niveis if n.lado == "compra" and abs(n.preco - bid) < 1e-9)
        va = sum(n.volume for n in livro.niveis if n.lado == "venda" and abs(n.preco - ask) < 1e-9)
        if vb + va > 0:
            return (bid * va + ask * vb) / (vb + va)
    return (bid + ask) / 2


def simular_mm(ticks: list[Tick] | tuple[Tick, ...], livros: list[LivroSnapshot] | tuple[LivroSnapshot, ...],
               cotador: CotadorMM, instrumento: Instrumento | None = None, intervalo_s: float = 1.0,
               seed: int = 0, cfg: ConfigMM | None = None, latencia_ms: float = 50.0) -> ResultadoMM:
    exigir_simulador_apenas(cfg or cotador.cfg)
    if instrumento is None:
        raise ValueError("simular_mm exige o Instrumento do ativo (perfil)")
    ins = instrumento
    rel = RelogioSimulado(ticks[0].t_utc_ns)
    sim = SimuladorExecucao(ins, rel, latencia_ns=int(latencia_ms * 1e6), seed=seed)
    sim.registrar_braco(BRACO, MAGIC)
    res = ResultadoMM()
    ticket_lado: dict[int, str] = {}
    tickets: dict[str, int] = {}
    mids: deque[float] = deque(maxlen=60)
    fluxo: deque[float] = deque(maxlen=10)
    il, livro, prox = 0, None, ticks[0].t_utc_ns

    def estoque() -> int:
        p = sim.posicao(BRACO)
        return 0 if p.lado is None else (p.quantidade if p.lado == Lado.COMPRA else -p.quantidade)

    def enviar(lado: str, preco: float) -> None:
        ack = sim.enviar(ComandoOrdem(intencao_id="mm", magic=MAGIC, lado=Lado.COMPRA if lado == "bid" else Lado.VENDA,
                                      tipo="limitada", quantidade=1, preco=preco))
        if ack.retcode == 10008:
            ticket_lado[ack.order] = lado
            tickets[lado] = ack.order

    for tk in ticks:
        t = tk.t_utc_ns
        rel.avancar_para(max(rel.agora_ns(), t))
        while il < len(livros) and livros[il].t_utc_ns <= t:
            livro = livros[il]
            sim.on_livro(livro)
            il += 1
        for ev in sim.on_tick(tk):
            if ev.tipo == "execucao" and ev.execucao is not None and ev.ticket in ticket_lado:
                lado = ticket_lado.pop(ev.ticket)
                tickets.pop(lado, None)
                cotador.registrar_execucao(lado, ev.execucao.t_ns)
                res.fills.append(Preenchimento(ev.execucao.t_ns, Lado.COMPRA if lado == "bid" else Lado.VENDA,
                                               ev.execucao.preco))
        res.max_abs_estoque = max(res.max_abs_estoque, abs(estoque()))
        if tk.flags & 32:
            fluxo.append(1.0)
        elif tk.flags & 64:
            fluxo.append(-1.0)
        if t < prox:
            continue
        prox = t + int(intervalo_s * NS)
        bid, ask = (tk.bid if tk.bid > 0 else tk.last), (tk.ask if tk.ask > 0 else tk.last)
        mids.append((bid + ask) / 2)
        if len(mids) < 10 or len(fluxo) < 5:
            continue
        sigma = max(float(np.std(np.diff(np.array(mids)))) / np.sqrt(intervalo_s), 1.0)
        tox = abs(float(np.mean(fluxo)))  # proxy: desequilibrio de fluxo agressor nos ultimos 10 negocios
        estado = {"microprice": _microprice(tk, livro), "melhor_bid": bid, "melhor_ask": ask, "estoque": estoque(),
                  "sigma": sigma, "toxicidade": tox}
        cotador.cotar(t, estado)
        res.n_decisoes += 1
        for m in cotador.ultimas_mensagens:
            if m.tipo in ("cancelamento", "modificacao") and m.lado in tickets:
                sim.cancelar(tickets.pop(m.lado))
            if m.tipo in ("nova", "modificacao") and m.preco is not None:
                enviar(m.lado, m.preco)
    res.mensagens = cotador.contagem()
    res.liquido_brl = sim.carteira(BRACO).liquido_brl
    res.estoque_final = estoque()
    return res
