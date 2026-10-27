"""Kill switch: acionamento manual (flag EMERGENCIA, CLI, token) e automatico; rearme so manual.

Efeito: cancela ordens, zera posicoes e bloqueia novas entradas. O kill switch nao envia ordens: ele
notifica os `efeitos` registrados (o Roteador cancela/zera; o gateway reforca).
"""
from __future__ import annotations

import hashlib
import hmac
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.protocolos import RegistroAuditoria


class ErroRearme(Exception):
    pass


class ErroToken(Exception):
    pass


@dataclass(frozen=True)
class EventoKill:
    motivo: str
    origem: str  # manual_flag | manual_cli | manual_token | perda_diaria | drawdown | dado_critico | ...
    t_ns: int
    cancelar: bool = True
    zerar: bool = True
    bloquear: bool = True


ORIGENS_AUTOMATICAS = (
    "perda_diaria", "drawdown", "dado_critico", "divergencia_reconciliacao", "erros_repetidos", "heartbeat",
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class KillSwitch:
    arquivo_flag: Path | None = None  # arquivo EMERGENCIA
    token_hash: str | None = None  # sha256 do token do painel/CLI
    auditoria: RegistroAuditoria | None = None
    erros_max: int = 5
    janela_erros_s: float = 60.0
    heartbeat_timeout_s: float = 5.0
    origens_heartbeat: tuple[str, ...] = ()
    ativo: bool = False
    ultimo: EventoKill | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    _efeitos: list[Callable[[EventoKill], None]] = field(default_factory=list)
    _erros: deque[int] = field(default_factory=deque)
    _hb: dict[str, int] = field(default_factory=dict)

    # --- infraestrutura -------------------------------------------------------------------------
    def registrar_efeito(self, cb: Callable[[EventoKill], None]) -> None:
        self._efeitos.append(cb)

    def _audit(self, tipo: str, dados: dict[str, Any]) -> None:
        self.historico.append({"tipo": tipo, **dados})
        if self.auditoria is not None:
            self.auditoria.registrar(tipo, dados)

    # --- acionamento ----------------------------------------------------------------------------
    def acionar(self, motivo: str, origem: str, t_ns: int) -> bool:
        """Aciona (idempotente). Retorna True se esta chamada ativou; False se ja estava ativo."""
        if self.ativo:
            self._audit("kill_switch_reacionado", {"motivo": motivo, "origem": origem, "t_ns": t_ns})
            return False
        self.ativo = True
        ev = EventoKill(motivo=motivo, origem=origem, t_ns=t_ns)
        self.ultimo = ev
        self._audit("kill_switch_acionado", {"motivo": motivo, "origem": origem, "t_ns": t_ns})
        for cb in list(self._efeitos):
            try:
                cb(ev)
            except Exception as e:  # um efeito com defeito nao pode impedir os demais
                self._audit("kill_switch_efeito_falhou", {"erro": repr(e), "t_ns": t_ns})
        return True

    def acionar_manual(self, motivo: str, t_ns: int, origem: str = "manual_cli") -> bool:
        return self.acionar(motivo, origem, t_ns)

    def acionar_com_token(self, token: str, motivo: str, t_ns: int) -> bool:
        if self.token_hash is None or not hmac.compare_digest(hash_token(token), self.token_hash):
            self._audit("kill_switch_token_invalido", {"t_ns": t_ns})
            raise ErroToken("token invalido")
        return self.acionar(motivo, "manual_token", t_ns)

    def verificar_flag(self, t_ns: int) -> bool:
        """Consulta o arquivo EMERGENCIA; se existir, aciona. Retorna o estado ativo."""
        if self.arquivo_flag is not None and Path(self.arquivo_flag).exists():
            self.acionar("arquivo EMERGENCIA presente", "manual_flag", t_ns)
        return self.ativo

    @staticmethod
    def criar_flag(caminho: str | Path, motivo: str = "") -> Path:
        p = Path(caminho)
        p.write_text(motivo or "emergencia", encoding="utf-8")
        return p

    # --- gatilhos automaticos ---------------------------------------------------------------------
    def checar_perda_diaria(self, pnl_dia_brl: float, limite_brl: float, t_ns: int) -> bool:
        if pnl_dia_brl <= -abs(limite_brl):
            return self.acionar(f"perda diaria {pnl_dia_brl:.2f} <= -{abs(limite_brl):.2f}", "perda_diaria", t_ns)
        return False

    def checar_drawdown(self, dd_brl: float, dd_max_brl: float, t_ns: int) -> bool:
        if dd_brl >= dd_max_brl:
            return self.acionar(f"drawdown {dd_brl:.2f} >= {dd_max_brl:.2f}", "drawdown", t_ns)
        return False

    def dado_critico(self, detalhe: str, t_ns: int) -> bool:
        return self.acionar(f"dado critico: {detalhe}", "dado_critico", t_ns)

    def divergencia_reconciliacao(self, detalhe: str, t_ns: int) -> bool:
        return self.acionar(f"divergencia de reconciliacao: {detalhe}", "divergencia_reconciliacao", t_ns)

    def registrar_erro(self, origem: str, t_ns: int) -> bool:
        """Erros repetidos dentro da janela acionam o kill switch."""
        self._erros.append(t_ns)
        lim = t_ns - int(self.janela_erros_s * 1e9)
        while self._erros and self._erros[0] < lim:
            self._erros.popleft()
        if len(self._erros) >= self.erros_max:
            return self.acionar(f"{len(self._erros)} erros em {self.janela_erros_s:.0f}s (ultimo: {origem})",
                                "erros_repetidos", t_ns)
        return False

    def registrar_heartbeat(self, origem: str, t_ns: int) -> None:
        self._hb[origem] = t_ns

    def checar_heartbeat(self, t_ns: int) -> bool:
        lim = int(self.heartbeat_timeout_s * 1e9)
        for o in self.origens_heartbeat:
            ultimo = self._hb.get(o)
            if ultimo is None or t_ns - ultimo > lim:
                return self.acionar(f"heartbeat de {o} ausente", "heartbeat", t_ns)
        return False

    # --- rearme -----------------------------------------------------------------------------------
    def rearmar(self, motivo: str, responsavel: str, t_ns: int, token: str | None = None) -> None:
        """Rearme SO manual, com motivo e responsavel gravados na auditoria. Nunca automatico."""
        if not self.ativo:
            raise ErroRearme("kill switch nao esta ativo")
        if len(motivo.strip()) < 5 or not responsavel.strip():
            raise ErroRearme("rearme exige motivo (>= 5 caracteres) e responsavel")
        if self.token_hash is not None and (token is None or not hmac.compare_digest(hash_token(token), self.token_hash)):
            self._audit("kill_switch_rearme_negado", {"responsavel": responsavel, "t_ns": t_ns})
            raise ErroToken("token invalido para rearme")
        if self.arquivo_flag is not None and Path(self.arquivo_flag).exists():
            raise ErroRearme("arquivo EMERGENCIA ainda presente; remova-o antes de rearmar")
        self._audit("kill_switch_rearmado", {"motivo": motivo, "responsavel": responsavel, "t_ns": t_ns,
                                             "acionado_por": self.ultimo.origem if self.ultimo else ""})
        self.ativo = False
        self._erros.clear()
