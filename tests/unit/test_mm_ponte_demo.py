"""Ponte do MM com a demo (unidade): flag, cotador, CLI e recusas. O fluxo com gateway esta em tests/integracao."""
from __future__ import annotations

import pytest
from perfis_teste import INS_WIN, WIN
from test_espelho_demo import SNAP_VAZIO, CanalFalso, estado_gw

from robowin.cli import main
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import ConfigRisco
from robowin.mm.avellaneda_stoikov import ParamsAS
from robowin.mm.config import ConfigMM
from robowin.mm.cotacao import CotadorMM
from robowin.mm.ponte_demo import ConfigPonteMM, MMDemoRecusado, PonteMMDemo, validar_config_mm

NS = 1_000_000_000
P = ParamsAS(tick=WIN.tick_pontos)


def estado(mp=130002.5, q=0):
    return {"microprice": mp, "melhor_bid": 130000.0, "melhor_ask": 130005.0, "estoque": q, "sigma": 3.0}


def ponte(canal, **kw):
    return PonteMMDemo(CotadorMM(params=P, cfg=ConfigMM(demo_mm=True)), INS_WIN, WIN, canal, RelogioSimulado(10**18),
                       ConfigRisco(), ConfigPonteMM(stop_pontos=30.0, **kw))


def test_descartar_esquece_a_cotacao_sem_contar_mensagem():
    cot = CotadorMM(params=P)
    t0 = 1_700_000_000 * NS
    cot.cotar(t0, estado())
    n = len(cot.historico)
    cot.descartar("bid")
    assert "bid" not in cot.vivas and len(cot.historico) == n
    assert set(cot.monitor._abertas) == {cot.vivas["ask"].ordem_id}
    cot.descartar("bid")  # idempotente


def test_ponte_exige_stop_e_recusa_conta_real_e_sem_livro_ou_sem_modo_mm():
    with pytest.raises(MMDemoRecusado, match="stop_pontos"):
        PonteMMDemo(CotadorMM(params=P), INS_WIN, WIN, CanalFalso(), RelogioSimulado(10**18), ConfigRisco(),
                    ConfigPonteMM(stop_pontos=0.0))
    with pytest.raises(MMDemoRecusado, match="so em conta DEMO"):
        ponte(CanalFalso(estado=estado_gw("real"), snapshot=SNAP_VAZIO)).iniciar()
    with pytest.raises(MMDemoRecusado, match="livro"):
        ponte(CanalFalso(estado=estado_gw(capacidades={"livro": False}), snapshot=SNAP_VAZIO)).iniciar()
    with pytest.raises(MMDemoRecusado, match="modo_mm"):
        ponte(CanalFalso(estado=estado_gw(modo_mm=False), snapshot=SNAP_VAZIO)).iniciar()
    p = ponte(CanalFalso(estado=estado_gw(), snapshot=SNAP_VAZIO))
    p.iniciar()
    assert p.ativo and p.motor.cfg.operacoes_dia_max >= 300  # cada cotacao conta como operacao: teto = mensagens da sessao


def test_cotacao_recusada_pela_demo_realinha_o_cotador():
    canal = CanalFalso(estado=estado_gw(), snapshot=SNAP_VAZIO,
                       ordem={"tipo": "ack", "ack": {"retcode": -1, "comentario": "recusado localmente: x"}})
    p = ponte(canal)
    p.iniciar()
    p.ao_estado_gateway(estado_gw())
    t = p.rel.agora_ns()
    p.cotador.cotar(t, estado())
    res = p.aplicar(t)  # o relogio fixo cai fora do pregao (veto de risco) ou o gateway recusa: nunca fica viva
    assert res and all(r.startswith("recusada") for r in res)
    assert not p.cotador.vivas and not p.vivas


def test_validar_config_mm_exige_demo_e_simulacao_e_e_exclusiva():
    validar_config_mm({"geral": {"conta": "demo_mt5", "modo": "simulacao"}})
    with pytest.raises(MMDemoRecusado):
        validar_config_mm({"geral": {"conta": "interno"}})
    with pytest.raises(MMDemoRecusado, match="EXCLUSIVO"):
        validar_config_mm({"geral": {"conta": "demo_mt5", "espelhar": "claude_gpt_cond"}})


def test_cli_mm_demo_recusa_demo_mm_e_mm_ao_vivo_exige_flag(tmp_path, capsys):
    assert main(["mm", "demo", "--demo-mm", "--ticks", "100"]) == 2
    cfg = tmp_path / "robowin.toml"
    cfg.write_text('[geral]\nconta = "demo_mt5"\nsimbolo_base = "WIN"\n[mm]\ndemo_mm = false\n')
    assert main(["mm", "ao-vivo", "--config", str(cfg), "--saida", str(tmp_path / "s")]) == 2
    assert "demo_mm desligada" in capsys.readouterr().out
    cfg.write_text('[geral]\nconta = "demo_mt5"\nsimbolo_base = "WIN"\nespelhar = "a"\n[mm]\ndemo_mm = true\n')
    assert main(["mm", "ao-vivo", "--config", str(cfg), "--saida", str(tmp_path / "s2")]) == 2
    assert "EXCLUSIVO" in capsys.readouterr().out
