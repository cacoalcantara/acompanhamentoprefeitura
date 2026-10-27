"""Gateway com BarramentoLocal e canal local (relogio simulado, duble sintetico)."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from test_mt5_comum import LOGIN, SERVIDOR, SIMB, T0_MS, T0_NS, cmd, montar_duble, ticks_planos

from robowin.core.barramento import BarramentoLocal
from robowin.core.tipos import Lado
from robowin.mt5 import constantes as C
from robowin.mt5.canais import CanalComandosLocal
from robowin.mt5.execucao_demo import ExecucaoRealNaoAutorizada
from robowin.mt5.gateway import ConfigGateway, Gateway


def montar_gw(tmp_path, ticks=None, duble_kw=None, **cfg_kw):
    d, rel = montar_duble(ticks if ticks is not None else ticks_planos(3000), **(duble_kw or {}))
    bus = BarramentoLocal()
    eventos: list[dict] = []
    bus.assinar("*", eventos.append)
    canal = CanalComandosLocal()
    cfg = ConfigGateway(login=LOGIN, servidor=SERVIDOR, hoje=dt.date(2026, 10, 27), dir_log=tmp_path / "log",
                        inicio_coleta_ns=T0_NS - 1_000_000_000, espera_emergencia_s=0.0, **cfg_kw)
    gw = Gateway(d, cfg, rel, bus, canal, dormir=lambda s: rel.avancar(int(s * 1e9)))
    gw.iniciar()
    cli = canal.cliente("nucleo")
    return SimpleNamespace(d=d, rel=rel, bus=bus, ev=eventos, canal=canal, gw=gw, cli=cli)


def ordem(g, lado=Lado.COMPRA, **kw):
    if kw.get("tipo", "mercado") != "saida":
        kw.setdefault("sl", 99900.0 if lado == Lado.COMPRA else 100100.0)  # o guardiao exige stop na entrada
    return g.cli.enviar({"tipo": "ordem", "cmd": cmd(lado, **kw).model_dump(mode="json")}, bombear=g.gw.passo)


def passos(g, n, dt_s=0.2):
    for _ in range(n):
        g.rel.avancar(int(dt_s * 1e9))
        g.gw.passo()


def topicos(g):
    return {e["topico"] for e in g.ev}


def test_publica_ticks_livro_estado_e_heartbeat_com_sequencia(tmp_path):
    g = montar_gw(tmp_path)
    passos(g, 15)
    assert {"tick", "livro", "estado", "heartbeat"} <= topicos(g)
    seqs = [e["seq"] for e in g.ev]
    assert seqs == list(range(1, len(seqs) + 1))  # sem lacunas
    est = [e for e in g.ev if e["topico"] == "estado"][-1]
    assert est["duble"] is True and est["simbolo"] == SIMB and est["conta"]["login"] == LOGIN
    assert est["livro_profundidade"]["declarada"] == 10 and est["offset_s"] == 10800.0 and est["offset_confiavel"]
    hb = [e for e in g.ev if e["topico"] == "heartbeat"]
    assert hb and hb[0]["origem"] == "gateway" and "t_ns" in hb[0]


def test_ordem_via_canal_retorna_ack_e_guardiao_veta_sem_heartbeat(tmp_path):
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat", "origem": "nucleo"})
    r = ordem(g)
    assert r["tipo"] == "ack" and r["estado"] == "EXECUTADA" and r["ack"]["retcode"] == C.RET_EXECUTADA
    assert g.d.posicao_liquida(SIMB) == 1.0
    g.rel.avancar(20_000_000_000)  # 20 s sem heartbeat do nucleo
    r2 = ordem(g, Lado.VENDA)
    assert r2["ack"]["retcode"] == C.RET_LOCAL_RECUSADO and "heartbeat" in r2["ack"]["comentario"]
    # saidas continuam permitidas
    tk = int(g.d.positions_get()[0].ticket)
    r3 = ordem(g, Lado.VENDA, tipo="saida", ticket=tk)
    assert r3["estado"] == "EXECUTADA" and g.d.envios[-1]["position"] == tk


def test_nucleo_morto_com_posicao_aberta_e_zerado_em_ate_60s(tmp_path):
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    assert ordem(g)["estado"] == "EXECUTADA"
    tk = int(g.d.positions_get()[0].ticket)
    decorrido = 0.0
    while g.d.posicao_liquida(SIMB) != 0.0 and decorrido < 70:  # o nucleo "morreu": nenhum heartbeat
        passos(g, 1, 1.0)
        decorrido += 1.0
    assert g.d.posicao_liquida(SIMB) == 0.0 and 55 <= decorrido <= 62
    assert g.d.envios[-1]["position"] == tk


def test_emergencia_por_comando_confirma_ponta_a_ponta(tmp_path):
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    ordem(g)
    r = g.cli.enviar({"tipo": "emergencia", "origem": "teste"}, bombear=g.gw.passo)
    assert r["tipo"] == "ack_emergencia" and r["confirmado"] is True and r["zerado"] and r["travado"]
    assert r["posicoes"] == 0 and r["ordens"] == 0 and g.d.posicao_liquida(SIMB) == 0.0
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    assert "emergencia" in ordem(g)["ack"]["comentario"]
    # rearme manual exige motivo
    assert g.cli.enviar({"tipo": "rearmar", "motivo": ""}, bombear=g.gw.passo)["ok"] is False
    assert g.cli.enviar({"tipo": "rearmar", "motivo": "ok"}, bombear=g.gw.passo)["ok"] is True


def test_emergencia_sem_confirmacao_quando_nao_consegue_zerar(tmp_path):
    g = montar_gw(tmp_path, tentativas_emergencia=2)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    ordem(g)
    g.d.definir_conta(trade_allowed=False)  # algotrading desligado: nao ha como enviar a saida
    r = g.cli.enviar({"tipo": "emergencia"}, bombear=g.gw.passo)
    assert r["confirmado"] is False and r["posicoes"] == 1
    assert g.gw.parado is None  # permissao transitoria nao derruba o gateway


def test_troca_de_conta_para_o_gateway_e_nada_mais_e_enviado(tmp_path):
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    ordem(g)
    n = len(g.d.envios)
    g.d.trocar_conta(4242, SERVIDOR, "demo")
    passos(g, 3, 1.0)
    assert g.gw.parado and "guarda demo violada" in g.gw.parado
    assert len(g.d.envios) == n


def test_ordem_apos_troca_para_conta_real_responde_recusa_e_para(tmp_path):
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    g.d.trocar_conta(4242, SERVIDOR, "real")
    r = ordem(g)
    assert r["ack"]["retcode"] == C.RET_LOCAL_RECUSADO and g.gw.parado
    assert g.d.envios == []


def test_inicio_recusa_conta_real_e_contest(tmp_path):
    for modo in ("real", "contest"):
        d, rel = montar_duble(ticks_planos(), trade_mode=modo)
        gw = Gateway(d, ConfigGateway(login=LOGIN, servidor=SERVIDOR, hoje=dt.date(2026, 10, 27)), rel,
                     BarramentoLocal(), CanalComandosLocal())
        with pytest.raises(ExecucaoRealNaoAutorizada):
            gw.iniciar()
        assert d.envios == []


def test_feed_parado_e_backfill_pelo_gateway(tmp_path):
    g = montar_gw(tmp_path)
    passos(g, 5)
    n0 = sum(1 for e in g.ev if e["topico"] == "tick")
    g.d.desconectar()
    passos(g, 25)  # 5 s de queda
    est = g.gw.estado.__self__  # noqa: F841 - garante que o metodo existe
    assert g.gw.ticks.lacuna_pendente
    g.d.reconectar()
    passos(g, 3)
    bf = [e for e in g.ev if e["topico"] == "tick" and e["backfill"]]
    assert len(bf) > 30 and not g.gw.ticks.lacuna_pendente
    ms = [e["t_servidor_ms"] for e in g.ev if e["topico"] == "tick"]
    assert ms == sorted(ms) and len(ms) > n0


def test_log_bruto_sobrevive_em_disco(tmp_path):
    g = montar_gw(tmp_path)
    passos(g, 10)
    g.gw.encerrar()
    arq = list((tmp_path / "log").rglob(f"{SIMB}.ticks.jsonl"))
    n_ticks = sum(1 for e in g.ev if e["topico"] == "tick")
    assert arq and len(arq[0].read_text().splitlines()) == n_ticks > 0
    assert json.loads(arq[0].read_text().splitlines()[0])["time_msc"] >= T0_MS


def test_offset_nao_confiavel_bloqueia_entradas_ate_calibrar_ou_fixar(tmp_path):
    velhos = ticks_planos(50, inicio_ms=T0_MS - 3_600_000)  # feed parado ha ~1 h (idade multipla da grade!)
    g = montar_gw(tmp_path, ticks=velhos)
    assert g.gw.rs.confiavel is False
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    r = ordem(g)
    assert r["ack"]["retcode"] == C.RET_LOCAL_RECUSADO and "offset" in r["ack"]["comentario"]
    g2 = montar_gw(tmp_path, ticks=velhos, offset_fixo_s=10800)
    g2.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    assert ordem(g2)["estado"] == "EXECUTADA"


def test_estado_e_comando_desconhecido_e_parar(tmp_path):
    g = montar_gw(tmp_path)
    e = g.cli.enviar({"tipo": "estado"}, bombear=g.gw.passo)
    assert e["tipo"] == "estado" and e["guardiao"]["heartbeat_idade_s"] >= 0 and e["posicao"] is None
    assert g.cli.enviar({"tipo": "xyz"}, bombear=g.gw.passo)["tipo"] == "erro"
    assert g.cli.enviar({"tipo": "parar"}, bombear=g.gw.passo)["tipo"] == "parando"
    g.gw.passo()
    assert g.gw.parado == "comando parar"


def test_ordem_incerta_via_canal_e_sem_reenvio(tmp_path):
    from robowin.mt5.duble import Falha
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    g.d.injetar_falhas(Falha(None, executa=True))
    r = ordem(g, iid="u1")
    assert r["estado"] == "INCERTA" and r["ack"]["retcode"] is None
    r2 = ordem(g, iid="u1")
    assert r2["ack"]["retcode"] == C.RET_LOCAL_RECUSADO and len(g.d.envios) == 1
    g.rel.avancar(2_000_000_000)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    passos(g, 6, 1.0)  # o guardiao reconcilia: encontra o deal EXPERT da ordem que "se perdeu"
    assert not g.gw.executor.bloqueada_incerta and g.d.posicao_liquida(SIMB) == 1.0
