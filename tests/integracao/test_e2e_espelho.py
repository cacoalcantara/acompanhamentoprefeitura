"""E2E MULTIPROCESSO: gateway (DubleMT5 SINTETICO) em outro processo + nucleo ao vivo + painel + MM.

Valida a INFRAESTRUTURA do espelhamento na conta DEMO e da ponte do market making (nada aqui mede lucro; o
DubleMT5 nao e o mercado). O relogio dos dois lados e deslocado para um pregao da B3 (ver ``gateway_proc``).
"""
from __future__ import annotations

import time

import pytest
from gateway_proc import (
    INS,
    SIMB,
    GatewayProc,
    NucleoE2E,
    braco_entra_uma_vez,
    subir_nucleo_proc,
)
from perfis_teste import WIN

from robowin.core.tipos import ConfigRisco
from robowin.decisao.espelho import (
    ConfigEspelho,
    EspelhoDemo,
    EspelhoRecusado,
    validar_config_espelhamento,
)
from robowin.execucao.ponte_gateway import TravaEspelho
from robowin.mm.avellaneda_stoikov import ParamsAS
from robowin.mm.config import ConfigMM
from robowin.mm.cotacao import CotadorMM
from robowin.mm.ponte_demo import ConfigPonteMM, MMDemoRecusado, PonteMMDemo, validar_config_mm
from robowin.mt5 import constantes as C
from robowin.painel.app import criar_app

NS = 1_000_000_000


def deals_de_entrada(g: GatewayProc) -> list[dict]:
    return [e for e in g.envios() if e["acao"] == C.ACAO_DEAL]


def posicoes(g: GatewayProc) -> list[dict]:
    return g.snapshot()["positions"]


@pytest.fixture
def gw(tmp_path):
    g = GatewayProc(tmp_path)
    try:
        g.pronto()
        yield g
    finally:
        g.fechar()


@pytest.fixture
def nucleo(gw, tmp_path):
    n = NucleoE2E(tmp_path, gw.pub, gw.cmd, gw.deslocamento_ns)
    try:
        yield n
    finally:
        n.fechar()


# ===================================================================================== espelha + reconcilia ====
async def test_espelha_reconcilia_kill_zera_e_painel_confirma(gw, nucleo, tmp_path):
    from fastapi.testclient import TestClient
    esp = nucleo.espelho
    assert esp is not None and esp.ativo and esp.simbolo_gw == SIMB
    # 1) o braco entra no SIMULADOR; quando a posicao simulada abre, a demo e espelhada com SL
    assert await nucleo.ate(lambda: len(posicoes(gw)) == 1), esp.eventos
    p = posicoes(gw)[0]
    assert p["volume"] == 1.0 and p["type"] == C.POS_COMPRA and p["magic"] == esp.cfg.magic
    assert p["sl"] > 0 and p["sl"] < p["price_open"]  # SL do servidor, abaixo do preco (compra)
    assert nucleo.orq.sim.posicao("a").quantidade == 1  # o stop continua tambem no simulador
    entradas = [e for e in deals_de_entrada(gw) if e["position"] in (None, 0)]
    assert len(entradas) == 1 and entradas[0]["sl"] == p["sl"]  # exatamente UMA ordem de entrada, com SL
    assert esp.crenca.quantidade == 1
    ordens = nucleo.consulta("SELECT braco, simulada, estado, quantidade FROM ordens WHERE braco LIKE 'demo:%'")
    assert ordens and all(r["simulada"] == 0 and r["estado"] == "EXECUTADA" for r in ordens)  # ordem DEMO, nao simulada
    # 2) a reconciliacao (positions/orders/deals do gateway) fecha o laco sem divergencia
    assert await nucleo.ate(lambda: esp.deals_vistos["robo"] >= 1 and esp.crenca.ticket is not None)
    res = esp.ultimo_reconc
    assert res.ok and not res.divergencias and esp.deals_vistos["robo"] >= 1 and esp.deals_vistos["manual"] == 0, res
    assert esp.crenca.ticket == p["ticket"]
    # 3) heartbeat/estado do gateway gravados no banco (o painel sabe que HA gateway reportando)
    assert nucleo.consulta("SELECT 1 FROM incidentes WHERE origem='gateway_estado' LIMIT 1")
    assert nucleo.consulta("SELECT 1 FROM eventos_risco WHERE tipo='gateway_estado' AND braco='gateway' LIMIT 1")
    # 4) kill switch pelo PAINEL: a flag so e "confirmada" quando o GATEWAY reportar posicao zerada
    cli = TestClient(criar_app(banco=nucleo.banco_path, dir_dados=tmp_path, token="t"))
    r = cli.post("/acoes/emergencia", data={"token": "t", "confirmacao": "EMERGENCIA"})
    assert r.status_code == 200 and (tmp_path / "EMERGENCIA").exists()
    pagina = cli.get("/?msg=emergencia").text
    assert "aguardando confirmação do gateway" in pagina and "gateway/núcleo confirmou" not in pagina
    assert await nucleo.ate(lambda: bool(nucleo.consulta(
        "SELECT 1 FROM incidentes WHERE origem='gateway' AND detalhe LIKE 'emergencia_confirmada%'")), timeout_s=20)
    assert posicoes(gw) == [] and gw.snapshot()["orders"] == []
    est = gw.estado()
    assert est["guardiao"]["travado_emergencia"] is True and est["posicoes"] == [] and est["ordens"] == 0
    assert await nucleo.ate(lambda: nucleo.orq.sim.posicao("a").quantidade == 0)  # o simulador tambem zerou
    assert nucleo.orq.contas["a"].kill.ativo and nucleo.consulta("SELECT 1 FROM eventos_risco WHERE tipo='kill_switch'")
    pagina = cli.get("/").text
    assert "gateway/núcleo confirmou" in pagina and "posicao zerada" in pagina
    # travado: nenhuma entrada nova (nem no simulador, nem na demo)
    assert esp._bloqueio() != "" and len(deals_de_entrada(gw)) <= 2  # entrada + (no maximo) uma saida


async def test_painel_nao_confirma_so_com_kill_do_nucleo_quando_ha_gateway(gw, nucleo, tmp_path):
    """Com gateway reportando, o kill switch do NUCLEO (evento de risco) nao basta: so a posicao zerada no gateway."""
    from fastapi.testclient import TestClient
    await nucleo.rodar(1.0)  # garante pelo menos um estado do gateway gravado
    nucleo.registro.evento_risco(t_ns=nucleo.rel.agora_ns(), braco="a", tipo="kill_switch", detalhe="x")
    nucleo.registro.incidente(t_ns=nucleo.rel.agora_ns(), origem="gateway", severidade="alerta", detalhe="bloqueio_emergencia")
    (tmp_path / "EMERGENCIA").write_text("x")
    cli = TestClient(criar_app(banco=nucleo.banco_path, dir_dados=tmp_path, token="t"))
    pagina = cli.get("/").text
    assert "aguardando confirmação do gateway" in pagina and "gateway/núcleo confirmou" not in pagina


# ===================================================================================== SL do servidor =========
async def test_stop_do_servidor_fecha_a_demo_sem_divergencia_nem_saida_duplicada(gw, nucleo):
    esp = nucleo.espelho
    assert await nucleo.ate(lambda: len(posicoes(gw)) == 1)
    n_antes = len(deals_de_entrada(gw))
    gw.saltar_preco(-120.0)  # derruba o preco ate muito alem do stop de 60 pontos
    assert await nucleo.ate(lambda: posicoes(gw) == [] and nucleo.orq.sim.posicao("a").quantidade == 0, timeout_s=20)
    assert await nucleo.ate(lambda: esp.crenca.quantidade == 0 and esp.ultimo_reconc is not None)
    deals = gw.snapshot()["deals"]
    assert any(d["reason"] == C.RAZAO_SL for d in deals)  # o SL vive no servidor
    await nucleo.rodar(2.5)
    assert esp.ultimo_reconc.ok and esp.deals_vistos["servidor"] >= 1  # deal do SL classificado como esperado
    assert not [e for e in esp.eventos if "divergencia" in e[0] or "manual" in e[0]], esp.eventos
    assert not nucleo.orq.contas["a"].kill.ativo
    assert len(deals_de_entrada(gw)) == n_antes  # nenhuma saida por ordem: o SL ja tinha fechado (UM dono)
    assert [e for e in esp.eventos if e[0] == "espelho_posicao_encerrada"]


# ===================================================================================== intervencao manual =====
async def test_intervencao_manual_alerta_e_bloqueia_entradas(gw, nucleo):
    esp = nucleo.espelho
    assert await nucleo.ate(lambda: len(posicoes(gw)) == 1)
    gw.operar_manual(C.DEAL_VENDA, 1.0)  # o usuario fecha a posicao na mao (reason CLIENT)
    assert await nucleo.ate(lambda: any(e[0] == "espelho_intervencao_manual" for e in esp.eventos), timeout_s=20)
    assert esp._bloqueio() == "reconciliacao_manual"
    assert nucleo.consulta("SELECT 1 FROM incidentes WHERE origem='espelho' AND detalhe LIKE '%intervencao_manual%'")
    assert not nucleo.orq.contas["a"].kill.ativo  # alerta + bloqueio, sem kill
    assert await nucleo.ate(lambda: "manual" in " ".join(gw.estado()["guardiao"]["bloqueios"]), timeout_s=8)  # o gateway tambem bloqueou
    esp.reconhecer_intervencao_manual("operador", "fechei na mao para teste", nucleo.rel.agora_ns())
    await nucleo.rodar(2.0)
    assert esp._bloqueio() != "reconciliacao_manual"


# ===================================================================================== queda do nucleo ========
def test_queda_do_nucleo_zera_a_demo_em_ate_60s_pelo_guardiao(tmp_path):
    from robowin.mt5.guardiao import ConfigGuardiao
    assert ConfigGuardiao().heartbeat_zera_s <= 60.0 and ConfigGuardiao().heartbeat_bloqueio_s <= 15.0
    g = GatewayProc(tmp_path, hb_bloqueio_s=1.5, hb_zera_s=4.0)  # 4 s escalados de 60 s (a regra e a mesma)
    proc = None
    try:
        g.pronto()
        proc = subir_nucleo_proc(g)
        fim = time.monotonic() + 40
        while time.monotonic() < fim and not posicoes(g):
            assert proc.is_alive(), "nucleo morreu antes de espelhar"
            time.sleep(0.3)
        assert len(posicoes(g)) == 1
        t0 = time.monotonic()
        proc.kill()  # queda abrupta do nucleo (SIGKILL): nenhum heartbeat, nenhuma saida
        proc.join(5)
        while time.monotonic() - t0 < 30 and posicoes(g):
            time.sleep(0.3)
        dt = time.monotonic() - t0
        assert posicoes(g) == [] and dt < 12  # 4 s de heartbeat + ciclo do guardiao + folga (<< 60 s)
        zeragens = [e for e in deals_de_entrada(g) if e["position"]]
        assert len(zeragens) == 1 and zeragens[0]["magic"] == 990001  # saida pelo ticket, magic do guardiao
    finally:
        if proc is not None and proc.is_alive():
            proc.kill()
        g.fechar()


# ===================================================================================== conta real =============
def test_recusa_espelhar_com_conta_real(tmp_path):
    g = GatewayProc(tmp_path, trade_mode="real", duracao_s=20)
    n = None
    try:
        g.p.join(25)
        assert g.p.exitcode not in (None, 0)  # o gateway nem inicia em conta real
        n = NucleoE2E(tmp_path, g.pub, g.cmd, g.deslocamento_ns, iniciar_espelho=False)
        with pytest.raises(EspelhoRecusado):
            n.espelho.iniciar()
        assert not n.espelho.ativo and g.envios() == []  # nenhuma ordem
        assert not (tmp_path / "ESPELHO.lock").exists()  # a trava nao ficou presa
    finally:
        if n is not None:
            n.fechar()
        g.fechar()


# ===================================================================================== INCERTA ================
async def test_ack_incerta_nao_reenvia_e_a_reconciliacao_adota_a_posicao(tmp_path):
    g = GatewayProc(tmp_path, falhas=((None, True),))  # o 1o order_send EXECUTA no servidor mas a resposta se perde
    n = None
    try:
        g.pronto()
        n = NucleoE2E(tmp_path, g.pub, g.cmd, g.deslocamento_ns)
        esp = n.espelho
        assert await n.ate(lambda: any(e[0] == "espelho_entrada_incerta" for e in esp.eventos), timeout_s=30), esp.eventos
        assert esp.emissor.enviados == 1 and esp.oms.incertas()  # INCERTA no OMS do nucleo
        assert esp.oms.bloqueado(esp.braco_demo) == "ordem_incerta_pendente"  # entradas bloqueadas ate reconciliar
        # a reconciliacao encontra o deal EXPERT, resolve a INCERTA e ADOTA a posicao real (sem reenviar)
        assert await n.ate(lambda: esp.crenca.quantidade == 1 and not esp.oms.incertas(), timeout_s=30), esp.eventos
        assert [e for e in esp.eventos if e[0] == "espelho_posicao_adotada"]
        await n.rodar(4.0)
        assert len(deals_de_entrada(g)) == 1  # NUNCA reenviou
        assert len(posicoes(g)) == 1 and posicoes(g)[0]["ticket"] == esp.crenca.ticket
        # e a posicao adotada pode ser encerrada com position=<ticket> (um dono)
        ticket = esp.crenca.ticket
        r = esp.saida("teste", n.rel.agora_ns())
        assert r.enviado and posicoes(g) == []
        assert deals_de_entrada(g)[-1]["position"] == ticket and len(deals_de_entrada(g)) == 2
    finally:
        if n is not None:
            n.fechar()
        g.fechar()


# ===================================================================================== conta nao zerada =======
def test_recusa_ligar_com_posicao_aberta_na_demo(gw, tmp_path):
    from robowin.core.tipos import ComandoOrdem, Lado
    gw.heartbeat()
    c = ComandoOrdem(intencao_id="manual-1", magic=7, lado=Lado.COMPRA, tipo="mercado", sl=129900.0)
    assert gw.cli.enviar({"tipo": "ordem", "cmd": c.model_dump(mode="json")}, timeout_s=5)["estado"] == "EXECUTADA"
    n = NucleoE2E(tmp_path, gw.pub, gw.cmd, gw.deslocamento_ns, iniciar_espelho=False)
    try:
        with pytest.raises(EspelhoRecusado, match="posicao ou ordem aberta"):
            n.espelho.iniciar()
    finally:
        n.fechar()


# ===================================================================================== exclusividade ==========
def test_espelhamento_exclusivo_entre_bracos_e_mm(tmp_path):
    # configuracao: espelhar + demo_mm juntos sao recusados nos dois sentidos
    cfg = {"geral": {"conta": "demo_mt5", "modo": "simulacao", "espelhar": "a"}, "mm": {"demo_mm": True}}
    with pytest.raises(EspelhoRecusado, match="EXCLUSIVO"):
        validar_config_espelhamento(cfg, "a", canal_ok=True)
    with pytest.raises(MMDemoRecusado, match="EXCLUSIVO"):
        validar_config_mm(cfg)
    with pytest.raises(EspelhoRecusado, match="demo_mt5"):
        validar_config_espelhamento({"geral": {"conta": "interno"}}, "a", canal_ok=True)
    with pytest.raises(EspelhoRecusado, match="canal"):
        validar_config_espelhamento({"geral": {"conta": "demo_mt5"}}, "a", canal_ok=False)
    validar_config_espelhamento({"geral": {"conta": "demo_mt5", "modo": "simulacao"}}, "a", canal_ok=True)


def test_trava_de_espelho_recusa_segundo_dono_mesmo_entre_processos(gw, tmp_path):
    n = NucleoE2E(tmp_path, gw.pub, gw.cmd, gw.deslocamento_ns)  # braco 'a' espelhado: trava adquirida
    try:
        assert (tmp_path / "ESPELHO.lock").exists()
        # outro braco no mesmo nucleo/processo? dono diferente => recusado
        t2 = TravaEspelho(tmp_path / "ESPELHO.lock")
        with pytest.raises(Exception, match="ja espelha"):
            t2.adquirir("nucleo:b")
        # a ponte do MM tambem e recusada enquanto o braco espelha
        from robowin.core.relogio import RelogioReal
        cot = CotadorMM(params=ParamsAS(tick=WIN.tick_pontos), cfg=ConfigMM(demo_mm=True))
        ponte = PonteMMDemo(cot, INS, WIN, n.cliente, RelogioReal(), ConfigRisco(), ConfigPonteMM(stop_pontos=30.0),
                            trava=TravaEspelho(tmp_path / "ESPELHO.lock"))
        with pytest.raises(MMDemoRecusado, match="ja espelha"):
            ponte.iniciar()
        assert not ponte.ativo
    finally:
        n.fechar()  # libera a trava
    assert not (tmp_path / "ESPELHO.lock").exists()


# ===================================================================================== market making ==========
def _estado_mm(mp: float, bb: float = 130000.0, ba: float = 130005.0) -> dict[str, float]:
    return {"microprice": mp, "melhor_bid": bb, "melhor_ask": ba, "estoque": 0, "sigma": 3.0}


def _montar_mm(tmp_path, g, *, msgs_min: int = 4, trava: bool = True):
    from gateway_proc import RelogioDeslocado

    from robowin.dados.banco import BancoSQLite
    from robowin.decisao.registro import Registro
    from robowin.mt5.canais import ClienteComandosZMQ
    cli = ClienteComandosZMQ(g.cmd)
    rel = RelogioDeslocado(g.deslocamento_ns)
    banco = BancoSQLite(tmp_path / "mm.db")
    cot = CotadorMM(params=ParamsAS(tick=WIN.tick_pontos), cfg=ConfigMM(demo_mm=True, mensagens_min_max=msgs_min,
                                                                       mensagens_sessao_max=200))
    ponte = PonteMMDemo(cot, INS, WIN, cli, rel, ConfigRisco(), ConfigPonteMM(stop_pontos=30.0), registro=Registro(banco, None, "mm"),
                        trava=TravaEspelho(tmp_path / "ESPELHO.lock") if trava else None)
    return cli, rel, banco, cot, ponte


def _ciclo_mm(g, rel, cot, ponte, mp, bb=130000.0, ba=130005.0):
    g.heartbeat()
    ponte.ao_estado_gateway(g.estado())
    t = rel.agora_ns()
    ponte.sincronizar(t)
    est = _estado_mm(mp, bb, ba)
    est["estoque"] = ponte.estoque
    cot.cotar(t, est)
    return ponte.aplicar(t)


def test_mm_demo_respeita_orcamento_de_mensagens_e_tempo_minimo_no_livro(tmp_path):
    g = GatewayProc(tmp_path, modo_mm=True)
    cli = banco = None
    try:
        g.pronto()
        cli, rel, banco, cot, ponte = _montar_mm(tmp_path, g, msgs_min=4)
        est = ponte.iniciar()
        assert est["modo_mm"] is True and ponte.ativo
        inicio = time.monotonic()
        k = 0
        while time.monotonic() - inicio < 13.0:  # o estado empurra o preco de reserva para cima e para baixo
            _ciclo_mm(g, rel, cot, ponte, 130002.5 + (15.0 if k % 2 else -15.0))
            k += 1
            time.sleep(0.5)
        envios = [e for e in g.envios() if e["acao"] in (C.ACAO_PENDING, C.ACAO_MODIFY, C.ACAO_REMOVE)]
        # ORCAMENTO: nunca mais que 4 mensagens na janela de 60 s (novas + modificacoes + cancelamentos)
        assert 2 <= len(envios) <= 4, envios
        assert len(ponte.historico) == len(envios) == len(cot.historico)
        # TEMPO MINIMO: nenhuma alteracao/cancelamento antes de 2 s da colocacao/alteracao anterior da MESMA ordem
        ultimo: dict[int, int] = {}
        mexidas = 0
        for e in envios:
            if e["acao"] == C.ACAO_PENDING:
                ultimo[e["res_order"]] = e["t_ns"]
            else:
                mexidas += 1
                assert e["t_ns"] - ultimo[e["order"]] >= 2 * NS - 50_000_000, (e, ultimo)  # folga de 50 ms de relogio
                ultimo[e["order"]] = e["t_ns"]
        assert mexidas >= 1  # houve de fato modificacao (o teste exercita a regra)
        # as cotacoes da demo tem SL e ficam fora do topo do livro (nunca cruzam)
        ordens = g.snapshot()["orders"]
        assert ordens and all(o["sl"] > 0 for o in ordens)
        for o in ordens:
            assert o["price_open"] <= 130000.0 if o["type"] == C.ORDEM_BUY_LIMIT else o["price_open"] >= 130005.0
        # encerrar cancela tudo (por seguranca, mesmo antes do tempo minimo) e solta a trava
        ponte.encerrar(rel.agora_ns())
        assert g.snapshot()["orders"] == [] and not (tmp_path / "ESPELHO.lock").exists()
    finally:
        if banco is not None:
            banco.fechar()
        if cli is not None:
            cli.fechar()
        g.fechar()


def test_mm_demo_detecta_fill_atualiza_estoque_e_nao_aumenta_posicao(tmp_path):
    g = GatewayProc(tmp_path, modo_mm=True)
    cli = banco = None
    try:
        g.pronto()
        cli, rel, banco, cot, ponte = _montar_mm(tmp_path, g, msgs_min=10)
        ponte.iniciar()
        _ciclo_mm(g, rel, cot, ponte, 130002.5)
        assert sorted(ponte.vivas) == ["ask", "bid"]
        assert len(g.snapshot()["orders"]) == 2  # UMA limitada por lado (modo MM)
        g.saltar_preco(-20.0)  # bid 129980 / ask 129985: o ask cai abaixo do bid cotado (129990) e a compra executa
        fim = time.monotonic() + 15
        while time.monotonic() < fim and not ponte.fills:
            _ciclo_mm(g, rel, cot, ponte, 129982.5, 129980.0, 129985.0)
            time.sleep(0.4)
        assert ponte.fills and ponte.fills[0][0] == "bid"
        assert ponte.estoque == 1 and "bid" not in cot.vivas  # estoque 1: sem nova compra
        pos = posicoes(g)
        assert len(pos) == 1 and pos[0]["volume"] == 1.0 and pos[0]["sl"] > 0
        for _ in range(4):  # com estoque 1 o cotador so cota a venda (que REDUZ a posicao)
            _ciclo_mm(g, rel, cot, ponte, 129982.5, 129980.0, 129985.0)
            time.sleep(0.3)
        compras = [e for e in g.envios() if e["acao"] == C.ACAO_PENDING and e["tipo"] == C.ORDEM_BUY_LIMIT]
        assert len(compras) == 1
        assert len(posicoes(g)) == 1 and abs(g.snapshot()["positions"][0]["volume"]) == 1.0
    finally:
        if banco is not None:
            banco.fechar()
        if cli is not None:
            cli.fechar()
        g.fechar()


def test_mm_demo_recusa_sem_modo_mm_no_gateway_e_sem_livro(tmp_path):
    g = GatewayProc(tmp_path, modo_mm=False)
    cli = banco = None
    try:
        g.pronto()
        cli, rel, banco, cot, ponte = _montar_mm(tmp_path, g)
        with pytest.raises(MMDemoRecusado, match="modo_mm"):
            ponte.iniciar()
        assert not (tmp_path / "ESPELHO.lock").exists()  # recusa libera a trava
    finally:
        if banco is not None:
            banco.fechar()
        if cli is not None:
            cli.fechar()
        g.fechar()


def test_espelho_e_classe_exportada():
    assert EspelhoDemo and ConfigEspelho and braco_entra_uma_vez
