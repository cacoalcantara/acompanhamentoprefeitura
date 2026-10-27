"""Controles anti-manipulacao (spoofing/layering): limites internos BEM abaixo dos padroes CVM/CRSFN.

Mede por braco: taxa de cancelamento, tempo minimo no livro, mensagens por minuto e ordens canceladas
numa janela de 10 min no lado oposto a execucoes. Nao replica um limite regulatorio especifico; os
limites internos sao propositalmente conservadores (revisar com a corretora/compliance antes de real).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..core.tipos import Lado

JANELA_OPOSTO_S = 600.0  # 10 min


@dataclass(frozen=True)
class LimitesAntimanipulacao:
    mensagens_min_max: int = 20  # novas + alteracoes + cancelamentos por minuto e por braco
    tempo_min_livro_s: float = 2.0
    taxa_cancelamento_max: float = 0.5  # cancelamentos / ordens novas na janela (min. de amostras abaixo)
    amostra_min_taxa: int = 6
    janela_taxa_s: float = 3600.0
    cancel_oposto_10min_max: int = 2  # ordens canceladas no lado oposto a execucoes em 10 min
    ordens_dia_max: int = 400  # franquia diaria de mensagens (demo)


@dataclass
class Alerta:
    tipo: str
    braco: str
    detalhe: str
    t_ns: int


@dataclass
class _Ordem:
    id: str
    braco: str
    lado: Lado
    t_ns: int


@dataclass
class MonitorAntimanipulacao:
    limites: LimitesAntimanipulacao = field(default_factory=LimitesAntimanipulacao)
    alertas: list[Alerta] = field(default_factory=list)
    _abertas: dict[str, _Ordem] = field(default_factory=dict)
    _msgs: dict[str, deque[int]] = field(default_factory=dict)
    _novas: dict[str, deque[int]] = field(default_factory=dict)
    _cancels: dict[str, deque[int]] = field(default_factory=dict)
    _cancels_lado: dict[str, deque[tuple[int, Lado]]] = field(default_factory=dict)
    _execs: dict[str, deque[tuple[int, Lado]]] = field(default_factory=dict)
    _dia_msgs: dict[tuple[str, int], int] = field(default_factory=dict)

    @staticmethod
    def _podar(q: deque, t_ns: int, janela_s: float, chave=lambda x: x) -> None:
        lim = t_ns - int(janela_s * 1e9)
        while q and chave(q[0]) < lim:
            q.popleft()

    # ---- consultas previas (permitir ou nao) -------------------------------------------------
    def pode_enviar(self, braco: str, t_ns: int) -> tuple[bool, str]:
        q = self._msgs.setdefault(braco, deque())
        self._podar(q, t_ns, 60.0)
        if len(q) >= self.limites.mensagens_min_max:
            return False, "mensagens_por_minuto"
        dia = t_ns // int(86_400e9)
        if self._dia_msgs.get((braco, dia), 0) >= self.limites.ordens_dia_max:
            return False, "franquia_diaria_de_mensagens"
        return True, ""

    def pode_cancelar(self, ordem_id: str, t_ns: int) -> tuple[bool, str]:
        """Cancelamento (ou alteracao) antes do tempo minimo no livro e proibido, exceto por seguranca."""
        o = self._abertas.get(ordem_id)
        if o is None:
            return True, ""
        if t_ns - o.t_ns < int(self.limites.tempo_min_livro_s * 1e9):
            return False, "tempo_minimo_no_livro"
        return self.pode_enviar(o.braco, t_ns)

    # ---- registros ----------------------------------------------------------------------------------
    def _msg(self, braco: str, t_ns: int) -> None:
        self._msgs.setdefault(braco, deque()).append(t_ns)
        k = (braco, t_ns // int(86_400e9))
        self._dia_msgs[k] = self._dia_msgs.get(k, 0) + 1

    def registrar_nova(self, ordem_id: str, braco: str, lado: Lado, t_ns: int) -> None:
        self._msg(braco, t_ns)
        self._novas.setdefault(braco, deque()).append(t_ns)
        self._abertas[ordem_id] = _Ordem(ordem_id, braco, lado, t_ns)

    def registrar_alteracao(self, ordem_id: str, t_ns: int) -> None:
        o = self._abertas.get(ordem_id)
        if o is not None:
            self._msg(o.braco, t_ns)
            if t_ns - o.t_ns < int(self.limites.tempo_min_livro_s * 1e9):
                self._alertar("tempo_minimo_no_livro", o.braco, f"ordem {ordem_id} alterada antes do minimo", t_ns)

    def registrar_cancelamento(self, ordem_id: str, t_ns: int) -> None:
        o = self._abertas.pop(ordem_id, None)
        if o is None:
            return
        self._msg(o.braco, t_ns)
        self._cancels.setdefault(o.braco, deque()).append(t_ns)
        self._cancels_lado.setdefault(o.braco, deque()).append((t_ns, o.lado))
        if t_ns - o.t_ns < int(self.limites.tempo_min_livro_s * 1e9):
            self._alertar("tempo_minimo_no_livro", o.braco, f"ordem {ordem_id} cancelada em "
                          f"{(t_ns - o.t_ns) / 1e9:.2f}s", t_ns)
        self.verificar(o.braco, t_ns)

    def registrar_execucao(self, ordem_id: str, braco: str, lado: Lado, t_ns: int) -> None:
        self._abertas.pop(ordem_id, None)
        self._execs.setdefault(braco, deque()).append((t_ns, lado))

    # ---- metricas e alertas --------------------------------------------------------------------------
    def taxa_cancelamento(self, braco: str, t_ns: int) -> float:
        n = self._novas.setdefault(braco, deque())
        c = self._cancels.setdefault(braco, deque())
        self._podar(n, t_ns, self.limites.janela_taxa_s)
        self._podar(c, t_ns, self.limites.janela_taxa_s)
        return len(c) / len(n) if n else 0.0

    def cancelamentos_lado_oposto(self, braco: str, t_ns: int) -> int:
        """Ordens canceladas na janela de 10 min no lado oposto ao de execucoes na mesma janela."""
        cl = self._cancels_lado.setdefault(braco, deque())
        ex = self._execs.setdefault(braco, deque())
        self._podar(cl, t_ns, JANELA_OPOSTO_S, chave=lambda x: x[0])
        self._podar(ex, t_ns, JANELA_OPOSTO_S, chave=lambda x: x[0])
        lados_exec = {lado for _, lado in ex}
        if not lados_exec:
            return 0
        return sum(1 for _, lado in cl if all(lado != le for le in lados_exec))

    def _alertar(self, tipo: str, braco: str, detalhe: str, t_ns: int) -> None:
        self.alertas.append(Alerta(tipo, braco, detalhe, t_ns))

    def verificar(self, braco: str, t_ns: int) -> list[Alerta]:
        novos: list[Alerta] = []
        n = len(self._novas.get(braco, ()))
        if n >= self.limites.amostra_min_taxa:
            tx = self.taxa_cancelamento(braco, t_ns)
            if tx > self.limites.taxa_cancelamento_max:
                novos.append(Alerta("taxa_de_cancelamento", braco, f"{tx:.2f} > {self.limites.taxa_cancelamento_max}", t_ns))
        co = self.cancelamentos_lado_oposto(braco, t_ns)
        if co > self.limites.cancel_oposto_10min_max:
            novos.append(Alerta("cancelamentos_lado_oposto", braco, f"{co} em 10 min", t_ns))
        self.alertas.extend(novos)
        return novos

    def bloqueado(self, braco: str, t_ns: int) -> bool:
        """True se algum limite de taxa/oposto estiver violado agora (bloqueia novas ordens passivas)."""
        return bool(self.verificar_sem_registrar(braco, t_ns))

    def verificar_sem_registrar(self, braco: str, t_ns: int) -> list[str]:
        out: list[str] = []
        n = len(self._novas.get(braco, ()))
        if n >= self.limites.amostra_min_taxa and self.taxa_cancelamento(braco, t_ns) > self.limites.taxa_cancelamento_max:
            out.append("taxa_de_cancelamento")
        if self.cancelamentos_lado_oposto(braco, t_ns) > self.limites.cancel_oposto_10min_max:
            out.append("cancelamentos_lado_oposto")
        return out
