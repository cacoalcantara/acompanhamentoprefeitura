"""Integracao: gateway com duble SINTETICO em OUTRO PROCESSO + assinante ZeroMQ local; diagnostico e CLI.

Valida a infraestrutura (mensageria, guardiao, emergencia), nunca desempenho de estrategia.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import multiprocessing as mp
import socket
import time
from pathlib import Path

import pytest

from robowin.cli import main as cli_main
from robowin.core.barramento import AssinanteZMQ
from robowin.core.relogio import RelogioReal
from robowin.core.tipos import ComandoOrdem, Lado
from robowin.mt5 import constantes as C
from robowin.mt5.canais import ClienteComandosZMQ
from robowin.mt5.cli_cmds import executar_diagnostico, texto_relatorio
from robowin.mt5.duble import criar_duble_padrao
from robowin.mt5.gateway import ConfigGateway, rodar_gateway_zmq
from robowin.mt5.guardiao import ConfigGuardiao

LOGIN, SERVIDOR = 900001, "XPMT5-DEMO"
HOJE = dt.date(2026, 10, 27)


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cfg_gateway(tmp_path: Path, **g) -> ConfigGateway:
    # janela de entradas aberta o dia todo: o relogio e o real e o duble nao tem pregao
    guard = ConfigGuardiao(entradas_de="00:00", entradas_ate="23:58", zeragem="23:59", exigir_dia_util=False, exigir_sl=False,
                           arquivo_emergencia=tmp_path / "EMERGENCIA", **g)
    return ConfigGateway(login=LOGIN, servidor=SERVIDOR, hoje=HOJE, dir_log=tmp_path / "bruto", guardiao=guard,
                         intervalo_guardiao_s=0.2, intervalo_hb_s=0.2, intervalo_estado_s=0.3, espera_emergencia_s=0.05)


class GatewayProc:
    def __init__(self, tmp_path: Path, duracao_s: float = 25.0, **g) -> None:
        self.pub = f"tcp://127.0.0.1:{porta_livre()}"
        self.cmd = f"tcp://127.0.0.1:{porta_livre()}"
        ctx = mp.get_context("spawn")
        self.p = ctx.Process(target=rodar_gateway_zmq, args=(
            functools.partial(criar_duble_padrao, n_ticks=20000, passo_ms=50), cfg_gateway(tmp_path, **g),
            self.pub, self.cmd, duracao_s))
        self.p.start()
        self.cli = ClienteComandosZMQ(self.cmd)
        self.sub = AssinanteZMQ(self.pub, [""])

    def pronto(self, timeout_s: float = 25.0) -> None:
        fim = time.monotonic() + timeout_s
        while time.monotonic() < fim:
            r = self.cli.enviar({"tipo": "estado"}, timeout_s=0.5)
            if r and r.get("simbolo"):
                return
        raise AssertionError("gateway nao ficou pronto")

    def ordem(self, lado=Lado.COMPRA, iid="x1", **kw) -> dict:
        c = ComandoOrdem(intencao_id=iid, magic=7, lado=lado, tipo=kw.pop("tipo", "mercado"), **kw)
        return self.cli.enviar({"tipo": "ordem", "cmd": c.model_dump(mode="json")}, timeout_s=5)

    def heartbeat(self) -> None:
        self.cli.enviar_sem_resposta({"tipo": "heartbeat", "origem": "teste"})

    def estado(self) -> dict:
        return self.cli.enviar({"tipo": "estado"}, timeout_s=3)

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
        self.sub.fechar()


@pytest.fixture
def gw(tmp_path):
    g = GatewayProc(tmp_path)
    try:
        g.pronto()
        yield g
    finally:
        g.fechar()


def ler_ate(sub, topicos, timeout_s=5.0, minimo=1):
    vistos: dict[str, list[dict]] = {t: [] for t in topicos}
    fim = time.monotonic() + timeout_s
    while time.monotonic() < fim and any(len(v) < minimo for v in vistos.values()):
        m = sub.receber(100)
        if m and m[0] in vistos:
            vistos[m[0]].append(m[1])
    return vistos


def test_assinante_recebe_ticks_livro_estado_heartbeat_sem_lacunas(gw):
    v = ler_ate(gw.sub, ["tick", "livro", "estado", "heartbeat"], 8, minimo=3)
    assert all(len(x) >= 3 for x in v.values()), {k: len(x) for k, x in v.items()}
    assert v["tick"][0]["simbolo"] == "WINZ26" and "t_utc_ns" in v["tick"][0]
    assert v["estado"][-1]["duble"] is True  # o consumidor SABE que e dubles
    ler_ate(gw.sub, ["tick"], 2, minimo=50)
    assert gw.sub.lacunas == 0


def test_ordem_zmq_e_nucleo_morto_zera_pelo_ticket(tmp_path):
    g = GatewayProc(tmp_path, heartbeat_bloqueio_s=1.0, heartbeat_zera_s=3.0)
    try:
        g.pronto()
        g.heartbeat()
        r = g.ordem()
        assert r["estado"] == "EXECUTADA" and r["ack"]["retcode"] == C.RET_EXECUTADA
        assert g.estado()["posicao"]["volume"] == 1.0
        t0 = time.monotonic()  # "mata" o nucleo: nenhum heartbeat a partir daqui
        time.sleep(1.6)
        assert g.ordem(Lado.VENDA, iid="x2")["ack"]["retcode"] == C.RET_LOCAL_RECUSADO  # bloqueio de 15 s (escalado)
        while time.monotonic() - t0 < 12:
            if g.estado()["posicao"] is None:
                break
            time.sleep(0.3)
        assert g.estado()["posicao"] is None
        assert time.monotonic() - t0 < 8  # 3 s escalados de 60 s + folga
    finally:
        g.fechar()


def test_emergencia_por_comando_confirma_ponta_a_ponta(gw):
    gw.heartbeat()
    assert gw.ordem()["estado"] == "EXECUTADA"
    rc = cli_main(["emergencia", "--sem-arquivo", "--comando", gw.cmd, "--motivo", "teste", "--timeout-s", "5"])
    assert rc == 0
    est = gw.estado()
    assert est["posicao"] is None and est["guardiao"]["travado_emergencia"] is True
    gw.heartbeat()
    assert gw.ordem(iid="x9")["ack"]["retcode"] == C.RET_LOCAL_RECUSADO


def test_emergencia_por_arquivo_zera_e_trava(gw, tmp_path):
    gw.heartbeat()
    assert gw.ordem()["estado"] == "EXECUTADA"
    rc = cli_main(["emergencia", "--arquivo", str(tmp_path / "EMERGENCIA")])
    assert rc == 0 and (tmp_path / "EMERGENCIA").exists()
    fim = time.monotonic() + 8
    while time.monotonic() < fim and gw.estado()["posicao"] is not None:
        gw.heartbeat()
        time.sleep(0.3)
    est = gw.estado()
    assert est["posicao"] is None and est["guardiao"]["travado_emergencia"] is True


def test_log_bruto_gravado_pelo_processo_do_gateway(gw, tmp_path):
    ler_ate(gw.sub, ["tick"], 5, minimo=20)
    time.sleep(0.5)
    arqs = list((tmp_path / "bruto").rglob("WINZ26.ticks.jsonl"))
    assert arqs
    linhas = [json.loads(x) for x in arqs[0].read_text().splitlines() if x.strip()]
    assert len(linhas) >= 20 and {"time_msc", "bid", "ask", "t_rec_ns"} <= set(linhas[0])


# --- diagnostico ---------------------------------------------------------------------------------------
def test_diagnostico_contra_duble_nao_envia_ordem_e_grava_relatorio(tmp_path):
    d = criar_duble_padrao(RelogioReal(), n_ticks=20000, passo_ms=50)
    rel = executar_diagnostico(d, login_esperado=LOGIN, servidor_esperado=SERVIDOR, hoje=HOJE)
    assert rel["ok"], rel["recusas"]
    assert d.envios == [] and len(d.checks) == 1  # order_check SEM envio
    assert "DUBLE" in rel["origem"]
    assert rel["conta"]["trade_mode"] == "demo" and rel["conta"]["margin_mode"] == "EXCHANGE"
    assert rel["contrato"]["simbolo_corretora"] == "WINZ26"
    assert rel["especificacoes"]["tick_pontos"] == 5.0 and rel["especificacoes"]["ponto_valor_brl"] == pytest.approx(0.2)
    assert rel["livro"]["profundidade_declarada"] == 10 and rel["livro"]["profundidade_observada"] > 0
    assert {"WINZ26", "WING27", "INDZ26", "WDOX26", "BOVA11"} <= set(rel["ativos"])
    assert rel["offset_servidor"]["offset_s"] == 10800.0 and rel["offset_servidor"]["confiavel"]
    assert rel["order_check"]["ok"] is True
    txt = texto_relatorio(rel)
    assert "DUBLE" in txt and "APTO" in txt and "RECUSAS" not in txt


def test_diagnostico_cli_com_duble_grava_json_e_txt(tmp_path, capsys):
    rc = cli_main(["diagnostico", "--duble", "--saida", str(tmp_path / "diag")])
    out = capsys.readouterr().out
    assert rc == 0 and "DIAGNOSTICO MT5" in out and "DUBLE" in out and "Relatorio gravado" in out
    assert list((tmp_path / "diag").glob("diagnostico-*.json")) and list((tmp_path / "diag").glob("diagnostico-*.txt"))


@pytest.mark.parametrize("modo", ["real", "contest"])
def test_diagnostico_recusa_conta_nao_demo(modo):
    d = criar_duble_padrao(RelogioReal(), n_ticks=2000, trade_mode=modo)
    rel = executar_diagnostico(d, login_esperado=LOGIN, servidor_esperado=SERVIDOR, hoje=HOJE)
    assert not rel["ok"] and any(modo.upper() in r for r in rel["recusas"])
    assert d.envios == [] and d.checks == []
    assert "NAO APTO" in texto_relatorio(rel)


def test_diagnostico_recusa_login_e_especificacao_divergentes():
    d = criar_duble_padrao(RelogioReal(), n_ticks=2000)
    d.especificacoes["WINZ26"].trade_tick_size = 10.0
    rel = executar_diagnostico(d, login_esperado=123, servidor_esperado=SERVIDOR, hoje=HOJE)
    assert not rel["ok"]
    assert any("login" in r for r in rel["recusas"]) and any("tick_size" in r for r in rel["recusas"])
    assert d.envios == []


def test_cli_coletar_com_duble_grava_log(tmp_path, capsys):
    rc = cli_main(["coletar", "--duble", "--duracao-s", "1.2", "--recuo-s", "3", "--dir-log", str(tmp_path / "c")])
    assert rc == 0 and "ticks" in capsys.readouterr().out
    assert list((tmp_path / "c").rglob("WINZ26.ticks.jsonl"))
