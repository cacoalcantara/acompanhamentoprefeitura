"""Canal de comandos: bind fora do loopback exige token; com token, todo comando (incl. emergencia/rearmar) vai assinado."""
from __future__ import annotations

import pytest
from test_mt4_gateway_capacidades import montar_gw4

from robowin.mt5.canais import (
    BindInseguro,
    CanalComandosZMQ,
    assinar,
    endereco_local,
    token_valido,
)
from robowin.mt5.gateway import rodar_gateway_zmq


@pytest.mark.parametrize("end,local", [
    ("tcp://127.0.0.1:5557", True), ("tcp://localhost:5557", True), ("ipc:///tmp/x.sock", True),
    ("tcp://0.0.0.0:5557", False), ("tcp://*:5557", False), ("tcp://192.168.0.5:5557", False)])
def test_endereco_local(end, local):
    assert endereco_local(end) is local


def test_bind_exposto_sem_token_e_recusado(monkeypatch):
    monkeypatch.delenv("ROBOWIN_CMD_TOKEN", raising=False)
    with pytest.raises(BindInseguro):
        CanalComandosZMQ("tcp://0.0.0.0:5599")
    with pytest.raises(BindInseguro):
        rodar_gateway_zmq(lambda: None, None, "tcp://127.0.0.1:5598", "tcp://*:5599")  # recusa antes de criar o terminal


def test_assinatura_hmac():
    m = assinar({"tipo": "emergencia", "id": "a"}, "segredo")
    assert token_valido(m, "segredo") and not token_valido(m, "outro")
    assert not token_valido({**m, "tipo": "rearmar"}, "segredo")  # conteudo adulterado


def test_gateway_com_token_recusa_comando_sem_token_e_aceita_assinado(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOWIN_CMD_TOKEN", "segredo")
    g = montar_gw4(tmp_path)
    monkeypatch.delenv("ROBOWIN_CMD_TOKEN")  # cliente SEM token
    r = g.cli.enviar({"tipo": "emergencia", "origem": "intruso"}, bombear=g.gw.passo)
    assert r["tipo"] == "erro" and "token" in r["erro"]
    assert not g.gw.guardiao.travado_emergencia
    r = g.cli.enviar({"tipo": "rearmar", "motivo": "intruso rearmando"}, bombear=g.gw.passo)
    assert r["tipo"] == "erro"
    monkeypatch.setenv("ROBOWIN_CMD_TOKEN", "segredo")  # cliente com token
    r = g.cli.enviar({"tipo": "emergencia", "origem": "operador"}, bombear=g.gw.passo)
    assert r["tipo"] == "ack_emergencia" and g.gw.guardiao.travado_emergencia
    assert any(t == "comando_sem_token" for t, _ in g.gw.incidentes)
