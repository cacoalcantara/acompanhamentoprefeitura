"""Apoio dos testes E2E multiprocesso: gateway (e, se preciso, nucleo) em OUTRO processo com o DubleMT5 SINTETICO.

*** Tudo aqui e SINTETICO: o DubleMT5 nao e o mercado nem o terminal real. ***

* ``RelogioDeslocado``: relogio real deslocado por uma constante (a MESMA nos dois processos). Leva o "agora" para um
  pregao B3 (terca, 12:00 em Sao Paulo) para que o motor de risco do nucleo (calendario real da B3) aceite entradas
  em qualquer horario em que a suite rode.
* ``DubleControlado``: DubleMT5 que (1) grava cada ``order_send`` num arquivo (observabilidade entre processos) e
  (2) executa uma operacao MANUAL (deal com reason CLIENT) quando o teste cria ``manual.json``.
* ``GatewayProc``: sobe ``rodar_gateway_zmq`` com ``spawn`` e expoe um cliente de comandos para o teste.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import multiprocessing as mp
import socket
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from perfis_teste import WIN  # noqa: E402

from robowin.mt5 import constantes as C  # noqa: E402
from robowin.mt5.canais import ClienteComandosZMQ  # noqa: E402
from robowin.mt5.duble import DubleMT5, Falha, _ts_venc, especificacao, gerar_livros_sinteticos  # noqa: E402
from robowin.mt5.gateway import ConfigGateway, rodar_gateway_zmq  # noqa: E402
from robowin.mt5.guardiao import ConfigGuardiao  # noqa: E402

SP = dt.timezone(dt.timedelta(hours=-3))
ALVO_NS = int(dt.datetime(2026, 11, 10, 12, 0, tzinfo=SP).timestamp() * 1e9)  # terca, pregao B3, 12:00 em SP
LOGIN, SERVIDOR = 900001, "XPMT5-DEMO"
HOJE = dt.date(2026, 11, 10)
SIMB = "WINZ26"
PRECO = 130000.0


class RelogioDeslocado:
    def __init__(self, deslocamento_ns: int) -> None:
        self.deslocamento_ns = deslocamento_ns

    def agora_ns(self) -> int:
        return time.time_ns() + self.deslocamento_ns

    def monotonico_ns(self) -> int:
        return time.monotonic_ns()


def novo_deslocamento() -> int:
    return ALVO_NS - time.time_ns()


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


TickSint = tuple[int, float, float, float, float, int]


def ticks_trechos(inicio_ms: int, trechos: tuple[tuple[int, float], ...], total_ms: int, passo_ms: int = 100,
                  spread: float = 5.0) -> list[TickSint]:
    """Serie SINTETICA em trechos de preco constante: (duracao_ms, bid). O ultimo trecho se repete ate ``total_ms``."""
    out: list[TickSint] = []
    t, i, acabou = inicio_ms, 0, inicio_ms + trechos[0][0]
    bid = trechos[0][1]
    while t < inicio_ms + total_ms:
        while t >= acabou and i + 1 < len(trechos):
            i += 1
            bid = trechos[i][1]
            acabou += trechos[i][0]
        # cada tick e um NEGOCIO (o stop do simulador dispara pelo ultimo negocio)
        out.append((t, bid, bid + spread, bid, 1.0, C.FLAG_BID | C.FLAG_ASK | C.FLAG_LAST | C.FLAG_VOLUME | C.FLAG_SELL))
        t += passo_ms
    return out


class DubleControlado(DubleMT5):
    """DubleMT5 observavel/comandavel de fora do processo (ver docstring do modulo)."""

    def __init__(self, *a: Any, dir_ctl: str | Path = ".", **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.dir_ctl = Path(dir_ctl)
        self._manual_em_curso = False

    def order_send(self, request: dict[str, Any]) -> Any:
        res = super().order_send(request)
        with open(self.dir_ctl / "envios.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t_ns": self.relogio.agora_ns(), "acao": request.get("action"), "tipo": request.get("type"),
                                "position": request.get("position"), "order": request.get("order"),
                                "preco": request.get("price"), "sl": request.get("sl"), "magic": request.get("magic"),
                                "res_order": None if res is None else int(res.order),
                                "retcode": None if res is None else int(res.retcode)}) + "\n")
        return res

    def _salto(self, delta: float) -> None:
        """Substitui o RESTO da serie por um novo patamar de preco (``salto.json``: {"delta": pontos})."""
        agora, idx = self._agora_ms(), self._idx[SIMB]
        ult = self._ticks[SIMB][idx - 1] if idx else self._ticks[SIMB][0]
        novos = ticks_trechos(agora + 100, ((10**9, ult[1] + delta),), 600_000, 100)
        self._ticks[SIMB] = self._ticks[SIMB][:idx] + novos
        self._tms[SIMB] = [x[0] for x in self._ticks[SIMB]]

    def _sincronizar(self) -> None:
        super()._sincronizar()
        sal = self.dir_ctl / "salto.json"
        if not self._manual_em_curso and sal.exists():
            self._manual_em_curso = True
            try:
                delta = float(json.loads(sal.read_text())["delta"])
                sal.unlink()
                self._salto(delta)
            finally:
                self._manual_em_curso = False
        arq = self.dir_ctl / "manual.json"
        if not self._manual_em_curso and arq.exists():
            self._manual_em_curso = True
            try:
                cmd = json.loads(arq.read_text())
                arq.unlink()
                self.operar_manual(int(cmd["lado"]), float(cmd["volume"]), SIMB, int(cmd.get("razao", C.RAZAO_CLIENT)))
            finally:
                self._manual_em_curso = False


def fabrica_duble(deslocamento_ns: int, dir_ctl: str, trechos: tuple[tuple[int, float], ...] = ((10**9, PRECO),),
                  falhas: tuple[tuple[int | None, bool], ...] = (), trade_mode: str = "demo",
                  total_ms: int = 600_000, passo_ms: int = 100) -> DubleControlado:
    rel = RelogioDeslocado(deslocamento_ns)
    d = DubleControlado(rel, login=LOGIN, servidor=SERVIDOR, trade_mode=trade_mode, offset_servidor_s=10800, dir_ctl=dir_ctl)
    ini = rel.agora_ns() // 1_000_000 - 1000
    ticks = ticks_trechos(ini, trechos, total_ms, passo_ms)
    d.adicionar_simbolo(especificacao(WIN, SIMB, _ts_venc(2026, 12, 16)), ticks,
                        gerar_livros_sinteticos(ticks, 10, 5, WIN.tick_pontos))
    d.adicionar_simbolo(especificacao(WIN, "WING27", _ts_venc(2027, 2, 17)), ticks_trechos(ini, ((10**9, PRECO + 500),), 50_000, 1000))
    d.initialize()
    for rc, executa in falhas:
        d.injetar_falhas(Falha(rc, executa=executa))
    return d


def cfg_gateway(tmp_path: Path, *, hb_bloqueio_s: float = 15.0, hb_zera_s: float = 60.0, modo_mm: bool = False,
                msgs_por_min: int = 20, espera_incerta_s: float = 2.0) -> ConfigGateway:
    guard = ConfigGuardiao(arquivo_emergencia=tmp_path / "EMERGENCIA", heartbeat_bloqueio_s=hb_bloqueio_s,
                           heartbeat_zera_s=hb_zera_s, reconciliar_a_cada_s=1.0)
    return ConfigGateway(perfil=WIN, login=LOGIN, servidor=SERVIDOR, hoje=HOJE, dir_log=tmp_path / "bruto", guardiao=guard,
                         offset_fixo_s=10800, intervalo_guardiao_s=0.2, intervalo_hb_s=0.2, intervalo_estado_s=0.3,
                         espera_emergencia_s=0.05, espera_incerta_s=espera_incerta_s, msgs_por_min=msgs_por_min,
                         modo_mm=modo_mm)


class GatewayProc:
    def __init__(self, tmp_path: Path, *, deslocamento_ns: int | None = None, duracao_s: float = 90.0,
                 trechos: tuple[tuple[int, float], ...] = ((10**9, PRECO),), falhas: tuple[tuple[int | None, bool], ...] = (),
                 trade_mode: str = "demo", **cfg: Any) -> None:
        self.tmp_path = tmp_path
        self.deslocamento_ns = novo_deslocamento() if deslocamento_ns is None else deslocamento_ns
        self.pub = f"tcp://127.0.0.1:{porta_livre()}"
        self.cmd = f"tcp://127.0.0.1:{porta_livre()}"
        ctx = mp.get_context("spawn")
        fab = functools.partial(fabrica_duble, self.deslocamento_ns, str(tmp_path), trechos, falhas, trade_mode)
        self.p = ctx.Process(target=rodar_gateway_zmq, args=(fab, cfg_gateway(tmp_path, **cfg), self.pub, self.cmd, duracao_s,
                                                              RelogioDeslocado(self.deslocamento_ns)))
        self.p.start()
        self.cli = ClienteComandosZMQ(self.cmd)

    @property
    def relogio(self) -> RelogioDeslocado:
        return RelogioDeslocado(self.deslocamento_ns)

    def pronto(self, timeout_s: float = 30.0) -> None:
        fim = time.monotonic() + timeout_s
        while time.monotonic() < fim:
            if not self.p.is_alive():
                raise AssertionError(f"gateway morreu (exitcode={self.p.exitcode})")
            r = self.cli.enviar({"tipo": "estado"}, timeout_s=0.5)
            if r and r.get("simbolo"):
                return
        raise AssertionError("gateway nao ficou pronto")

    def estado(self) -> dict[str, Any]:
        r = self.cli.enviar({"tipo": "estado"}, timeout_s=3)
        assert r is not None
        return r

    def snapshot(self) -> dict[str, Any]:
        r = self.cli.enviar({"tipo": "snapshot", "desde_ns": 0}, timeout_s=3)
        assert r is not None and r["ok"]
        return r

    def heartbeat(self) -> None:
        self.cli.enviar_sem_resposta({"tipo": "heartbeat", "origem": "teste"})

    def envios(self) -> list[dict[str, Any]]:
        arq = self.tmp_path / "envios.jsonl"
        if not arq.exists():
            return []
        return [json.loads(x) for x in arq.read_text().splitlines() if x.strip()]

    def saltar_preco(self, delta: float) -> None:
        (self.tmp_path / "salto.json").write_text(json.dumps({"delta": delta}))

    def operar_manual(self, lado: int, volume: float = 1.0, razao: int = C.RAZAO_CLIENT) -> None:
        (self.tmp_path / "manual.json").write_text(json.dumps({"lado": lado, "volume": volume, "razao": razao}))

    def fechar(self) -> None:
        try:
            self.cli.enviar({"tipo": "parar"}, timeout_s=2)
        except Exception:  # noqa: BLE001
            pass
        self.p.join(5)
        if self.p.is_alive():
            self.p.terminate()
            self.p.join(2)
        self.cli.fechar()


# ================================================================================================ nucleo ====
import asyncio  # noqa: E402

from test_decisao_comum import INS, BracoRoteirizado  # noqa: E402

from robowin.core.tipos import ConfigRisco  # noqa: E402
from robowin.dados.banco import BancoSQLite  # noqa: E402
from robowin.decisao.espelho import ConfigEspelho, EspelhoDemo, MonitorGateway  # noqa: E402
from robowin.decisao.gatilhos import ConfigGatilhos  # noqa: E402
from robowin.decisao.nucleo import FonteZMQ, Nucleo  # noqa: E402
from robowin.decisao.orquestrador import ConfigOrquestrador, Orquestrador  # noqa: E402
from robowin.decisao.registro import Registro  # noqa: E402
from robowin.execucao.ponte_gateway import TravaEspelho  # noqa: E402


def braco_entra_uma_vez(id: str = "a", stop: float = 60.0, alvo: float | None = None) -> BracoRoteirizado:
    return BracoRoteirizado(id, [lambda s, c: s.entrada(c, stop=stop, alvo=alvo, tempo=3600), lambda s, c: s.nada(c)])


class NucleoE2E:
    """Nucleo ao vivo (orquestrador + FonteZMQ + espelho) montado como o CLI faz, com o relogio deslocado."""

    def __init__(self, tmp_path: Path, pub: str, cmd: str, deslocamento_ns: int, *, bracos: list[Any] | None = None,
                 espelhar: str | None = "a", reconciliar_a_cada_s: float = 1.0, espera_incerta_s: float = 2.0,
                 iniciar_espelho: bool = True) -> None:
        self.rel = RelogioDeslocado(deslocamento_ns)
        self.flag = tmp_path / "EMERGENCIA"
        self.banco_path = tmp_path / "robowin.db"
        self.banco = BancoSQLite(self.banco_path)
        self.registro = Registro(self.banco, None, "e2e")
        cfg = ConfigOrquestrador(
            instrumento=INS, risco=ConfigRisco(), sessao_id="e2e", modo_tempo="real", passo_s=1.0, checagem_s=1.0,
            exigir_aquecimento=False, latencia_exec_ns=0, arquivo_emergencia=self.flag,
            gatilhos=ConfigGatilhos(periodo_s=1.0, intervalo_min_s=1.0, e_brl_min=1e18, z_statarb_min=1e18,
                                    evento_posicao=False, max_chamadas_dia=None))
        self.orq = Orquestrador(cfg, relogio=self.rel, registro=self.registro, bracos=bracos or [braco_entra_uma_vez()])
        self.cliente = ClienteComandosZMQ(cmd)
        self.fonte = FonteZMQ(pub)
        self.monitor = MonitorGateway(self.registro, arquivo_flag=self.flag)
        self.espelho: EspelhoDemo | None = None
        if espelhar:
            self.espelho = EspelhoDemo(
                self.orq, ConfigEspelho(braco=espelhar, reconciliar_a_cada_s=reconciliar_a_cada_s,
                                        espera_incerta_s=espera_incerta_s),
                self.cliente, trava=TravaEspelho(tmp_path / "ESPELHO.lock"))
            if iniciar_espelho:
                self.espelho.iniciar()
        self.nuc = Nucleo(self.orq, self.fonte, heartbeat=lambda: self.cliente.enviar_sem_resposta({"tipo": "heartbeat"}),
                          intervalo_hb_s=0.3, cooperar_s=0.002, espelho=self.espelho, monitor=self.monitor)

    async def rodar(self, s: float) -> None:
        await self.nuc.executar(duracao_s=s)
        await self.orq.aguardar_tarefas()

    async def ate(self, cond: Any, timeout_s: float = 25.0, passo_s: float = 0.25) -> bool:
        fim = time.monotonic() + timeout_s
        while time.monotonic() < fim:
            await self.rodar(passo_s)
            if cond():
                return True
        return False

    def consulta(self, sql: str, *args: Any) -> list[Any]:
        return self.banco.consultar(sql, args)

    def fechar(self) -> None:
        if self.espelho is not None and self.espelho.trava is not None:
            self.espelho.trava.liberar()
        self.fonte.fechar()
        self.cliente.fechar()
        self.banco.fechar()


def nucleo_proc_main(tmp_path: str, pub: str, cmd: str, deslocamento_ns: int, duracao_s: float) -> None:
    """Entrada de PROCESSO do nucleo (para matar com SIGKILL e provar que o guardiao zera sozinho)."""
    n = NucleoE2E(Path(tmp_path), pub, cmd, deslocamento_ns)
    asyncio.run(n.rodar(duracao_s))


def subir_nucleo_proc(g: GatewayProc, duracao_s: float = 120.0) -> mp.Process:
    (g.tmp_path / "nucleo").mkdir(exist_ok=True)
    p = mp.get_context("spawn").Process(target=nucleo_proc_main, args=(str(g.tmp_path / "nucleo"), g.pub, g.cmd,
                                                                      g.deslocamento_ns, duracao_s))
    p.start()
    return p
