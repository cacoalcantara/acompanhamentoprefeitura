"""Ponte nucleo -> gateway (conta DEMO): emissor de ordens, terminal somente-leitura e trava de exclusividade.

Este modulo NAO importa MetaTrader5 e NAO chama ``order_send``: so conversa com o gateway (unico dono do
terminal) pelo canal de comandos (ROUTER/DEALER ZeroMQ, ou o canal local nos testes).

* ``EmissorGateway``: callback do ``Roteador(modo="gateway_demo")``. Devolve o ``AckOrdem`` SINCRONO do gateway;
  sem resposta (timeout), resposta de erro ou formato inesperado => ``None`` (o OMS marca INCERTA, bloqueia e
  NUNCA reenvia; a reconciliacao resolve).
* ``TerminalViaGateway``: finge ser o ``TerminalMT5`` (positions_get / orders_get / history_deals_get) servindo
  um snapshot ATOMICO pedido ao gateway; permite reusar o ``Reconciliador`` sem tocar o MT5 neste processo.
  Volumes em contratos e instantes em UTC (ver ``mt5.consultas``).
* ``TravaEspelho``: trava de arquivo; garante ESPELHAMENTO EXCLUSIVO (um unico braco/estrategia espelhado na
  conta demo por vez, mesmo entre processos).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from ..core.tipos import AckOrdem, ComandoOrdem


class CanalComandos(Protocol):
    """Cliente do canal de comandos (``ClienteComandosZMQ``; nos testes, ``ClienteLocal`` com ``bombear``)."""

    def enviar(self, msg: dict[str, Any], timeout_s: float = ...) -> dict[str, Any] | None: ...
    def enviar_sem_resposta(self, msg: dict[str, Any]) -> None: ...


def ack_de_resposta(resp: dict[str, Any] | None) -> AckOrdem | None:
    """Resposta do gateway -> AckOrdem; ``None`` quando o resultado e DESCONHECIDO (timeout/erro): vira INCERTA."""
    if resp is None or resp.get("tipo") != "ack" or not isinstance(resp.get("ack"), dict):
        return None
    try:
        return AckOrdem.model_validate(resp["ack"])
    except Exception:  # noqa: BLE001 - formato inesperado: nao sabemos o que aconteceu
        return None


class EmissorGateway:
    def __init__(self, canal: CanalComandos, *, dono: str = "nucleo", timeout_s: float = 2.0) -> None:
        self.canal, self.dono, self.timeout_s = canal, dono, timeout_s
        self.enviados = 0
        self.sem_resposta = 0
        self.ultimo_ack: AckOrdem | None = None

    def __call__(self, cmd: ComandoOrdem) -> AckOrdem | None:
        self.enviados += 1
        resp = self.canal.enviar({"tipo": "ordem", "cmd": cmd.model_dump(mode="json"), "dono": self.dono},
                                 timeout_s=self.timeout_s)
        ack = ack_de_resposta(resp)
        if ack is None:
            self.sem_resposta += 1
        self.ultimo_ack = ack
        return ack


class TerminalViaGateway:
    """Somente leitura. ``atualizar()`` pede o snapshot; as tres leituras servem o MESMO snapshot."""

    eh_proxy_do_gateway = True

    def __init__(self, canal: CanalComandos, *, desde_ns: int = 0, timeout_s: float = 2.0) -> None:
        self.canal, self.desde_ns, self.timeout_s = canal, desde_ns, timeout_s
        self._snap: dict[str, list[SimpleNamespace]] | None = None
        self.bruto: dict[str, Any] = {}

    def atualizar(self) -> bool:
        r = self.canal.enviar({"tipo": "snapshot", "desde_ns": self.desde_ns}, timeout_s=self.timeout_s)
        if r is None or r.get("tipo") != "snapshot" or not r.get("ok"):
            self._snap, self.bruto = None, {}
            return False
        self.bruto = r
        self._snap = {k: [SimpleNamespace(**x) for x in r.get(k, [])] for k in ("positions", "orders", "deals")}
        return True

    def _ler(self, chave: str, symbol: str | None) -> tuple[SimpleNamespace, ...] | None:
        if self._snap is None:
            return None
        return tuple(x for x in self._snap[chave] if symbol is None or x.symbol == symbol)

    def positions_get(self, symbol: str | None = None, **_: Any) -> tuple[SimpleNamespace, ...] | None:
        return self._ler("positions", symbol)

    def orders_get(self, symbol: str | None = None, **_: Any) -> tuple[SimpleNamespace, ...] | None:
        return self._ler("orders", symbol)

    def history_deals_get(self, *_: Any, **__: Any) -> tuple[SimpleNamespace, ...] | None:
        return self._ler("deals", None)  # a janela ja foi aplicada no gateway (``desde_ns``)


class EspelhamentoNaoExclusivo(Exception):
    """Outro braco/estrategia ja espelha na conta demo: so UM por vez."""


def _pid_vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


IDADE_ILEGIVEL_S = 5.0  # trava vazia/ilegivel mais nova que isto e tratada como OCUPADA (criacao em andamento)


class TravaEspelho:
    """Trava de arquivo (O_EXCL) com dono e pid. Trava de processo morto e retomada; de processo vivo, recusada."""

    def __init__(self, caminho: str | Path) -> None:
        self.caminho = Path(caminho)
        self.dono: str | None = None

    def atual(self) -> dict[str, Any] | None:
        try:
            return dict(json.loads(self.caminho.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def _idade_s(self) -> float:
        try:
            return max(0.0, time.time() - self.caminho.stat().st_mtime)
        except OSError:
            return float("inf")

    def _criar_atomico(self, dono: str) -> None:
        """Escreve num temporario e publica com `os.link` (falha com FileExistsError se a trava ja existe)."""
        tmp = self.caminho.with_name(f"{self.caminho.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"dono": dono, "pid": os.getpid(), "t": time.time()}, f)
        try:
            os.link(tmp, self.caminho)
        finally:
            tmp.unlink(missing_ok=True)

    def adquirir(self, dono: str) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                self._criar_atomico(dono)  # arquivo ja COMPLETO quando aparece: ninguem le uma trava vazia
            except FileExistsError:
                a = self.atual()
                if a is not None and int(a.get("pid", 0)) == os.getpid() and a.get("dono") == dono:
                    self.dono = dono
                    return
                if a is not None and _pid_vivo(int(a.get("pid", 0))):
                    raise EspelhamentoNaoExclusivo(
                        f"'{a.get('dono')}' (pid {a.get('pid')}) ja espelha na conta demo: so um por vez") from None
                if a is None and self._idade_s() < IDADE_ILEGIVEL_S:  # vazia/ilegivel e RECENTE: outro esta criando
                    raise EspelhamentoNaoExclusivo(f"trava {self.caminho} em criacao por outro processo: tente de novo") from None
                self.caminho.unlink(missing_ok=True)  # trava orfa (processo morto) ou ilegivel ha tempo
                continue
            self.dono = dono
            return
        raise EspelhamentoNaoExclusivo(f"nao foi possivel adquirir {self.caminho}")

    def liberar(self) -> None:
        a = self.atual()
        if self.dono is not None and a is not None and int(a.get("pid", 0)) == os.getpid():
            self.caminho.unlink(missing_ok=True)
        self.dono = None
