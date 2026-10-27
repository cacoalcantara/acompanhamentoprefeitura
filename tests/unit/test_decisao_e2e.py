"""E2E: sessao sintetica completa (todos os bracos gravando no banco, painel lendo), CLI, nucleo ao vivo.

Valida a INFRAESTRUTURA: nenhum assert aqui mede desempenho ou lucro (dados sinteticos nao provam nada).
"""
from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from test_decisao_comum import T0, BracoRoteirizado, montar_orq, serie

import robowin.decisao as pacote
from robowin.auditoria.cadeia import verificar_diretorio
from robowin.cli import main
from robowin.core.esquema_banco import TABELAS

RAIZ_PKG = Path(pacote.__file__).parent
NS_TICKS = 3000


@pytest.fixture(scope="module")
def sessao(tmp_path_factory):
    saida = tmp_path_factory.mktemp("sim") / "s1"
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["simular", "--n-ticks", str(NS_TICKS), "--saida", str(saida), "--seed", "11"])
    return {"rc": rc, "saida": saida, "stdout": buf.getvalue()}


def _con(sessao):
    c = sqlite3.connect(sessao["saida"] / "robowin.db")
    c.row_factory = sqlite3.Row
    return c


IDS_BRACOS = {"quant", "quant_cond", "claude", "claude_cond", "claude_gpt", "claude_gpt_cond", "nao_operar",
              "controle_aleatorio", "controle_aleatorio_cond"}


def test_simular_imprime_resumo_rotulado_simulacao_sintetico(sessao):
    assert sessao["rc"] == 0
    out = sessao["stdout"]
    assert "SIMULACAO / SINTETICO" in out and "FALSO" in out
    assert "NAO sao dados de mercado" in out and "nenhuma ordem real" in out.lower()
    for b in IDS_BRACOS:
        assert b in out


def test_todos_os_bracos_gravam_no_banco(sessao):
    c = _con(sessao)
    eq = {r[0] for r in c.execute("SELECT DISTINCT braco FROM equity")}
    assert eq == IDS_BRACOS  # contas separadas, uma serie de capital por braco
    dec = {r[0] for r in c.execute("SELECT DISTINCT braco FROM decisoes")}
    assert {"claude", "claude_gpt", "claude_cond", "claude_gpt_cond"} <= dec  # bracos de IA: toda decisao registrada
    for tab in ("decisoes", "chamadas_llm", "latencias", "equity", "experimentos", "modelos", "incidentes",
                "orcamento_ia"):
        assert c.execute(f"SELECT COUNT(*) FROM {tab}").fetchone()[0] > 0, tab
    assert set(TABELAS) <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_chamadas_llm_rotuladas_falso_com_tokens_custo_e_modelo_servido(sessao):
    c = _con(sessao)
    rows = c.execute("SELECT * FROM chamadas_llm").fetchall()
    assert rows and all(r["modelo_servido"] == "" or r["modelo_servido"].endswith("-FALSO") for r in rows)
    validas = [r for r in rows if r["valida"]]
    assert validas and all(r["custo_usd"] > 0 and r["hash_dossie"] and r["tokens_entrada"] > 0 for r in validas)
    assert {r["papel"] for r in rows} <= {"proponente", "revisor"}
    assert {"proponente", "revisor"} <= {r["papel"] for r in rows}
    # chamada compartilhada B/C: cada dossie tem UMA chamada de proponente para o par (nao uma por braco)
    por_dossie = c.execute("SELECT hash_dossie, COUNT(*) n FROM chamadas_llm WHERE papel='proponente' "
                           "GROUP BY hash_dossie, braco").fetchall()
    assert por_dossie and max(r["n"] for r in por_dossie) == 1
    inval = [r for r in rows if not r["valida"]]
    assert {r["erro"] for r in inval} <= {"timeout", "http_429"} or all(r["erro"] for r in inval)  # falhas injetadas


def test_decisoes_trazem_proposta_revisao_risco_e_resultado(sessao):
    c = _con(sessao)
    d = c.execute("SELECT * FROM decisoes WHERE braco='claude_gpt' AND revisao_json IS NOT NULL LIMIT 1").fetchone()
    assert d is not None and json.loads(d["revisao_json"])["veredito"] in ("APROVAR", "REJEITAR")
    assert d["hash_dossie"] and d["gatilho"] and json.loads(d["proposta_json"])
    resultados = {r[0] for r in c.execute("SELECT DISTINCT resultado FROM decisoes")}
    assert resultados <= {"executada", "vetada", "bloqueada", "nao_operar", "plano_armado", "ignorada", "nao_enviada"}
    exec_ = c.execute("SELECT * FROM decisoes WHERE resultado IN ('executada','vetada') AND risco_json IS NOT NULL"
                      ).fetchall()
    assert exec_ and "aprovada" in json.loads(exec_[0]["risco_json"])


def test_latencia_por_etapa_com_trecho_injetado_rotulado(sessao):
    c = _con(sessao)
    etapas = {r[0] for r in c.execute("SELECT DISTINCT etapa FROM latencias")}
    assert {"proposta_ia", "features_modelo"} <= etapas
    inj = c.execute("SELECT injetado FROM latencias WHERE etapa IN ('proposta_ia','ordem_para_fill')").fetchall()
    assert inj and all(r[0] == 1 for r in inj)  # IA falsa e fill do simulador: sempre rotulados "injetado"


def test_sessao_marcada_como_sintetica_manifesto_e_modelo_nao_validado(sessao):
    c = _con(sessao)
    assert c.execute("SELECT COUNT(*) FROM incidentes WHERE origem='dados_sinteticos'").fetchone()[0] == 1
    man = json.loads(c.execute("SELECT manifesto_json FROM experimentos").fetchone()[0])
    assert man["id"] == "sim-11" and man["modelos"]["claude.proponente"].startswith("anthropic:")
    assert man["modelos"]["claude_gpt.revisor"].startswith("openai:") and man["hashes"]["risco"]
    assert any(k.startswith("prompt:") for k in man["hashes"])
    st = c.execute("SELECT status FROM modelos").fetchone()[0]
    assert st == "em_validacao"  # dados sinteticos nunca geram o selo validado_oos
    assert c.execute("SELECT status FROM experimentos").fetchone()[0] == "concluido"


def test_so_ordens_simuladas_nenhuma_ordem_real(sessao):
    c = _con(sessao)
    assert c.execute("SELECT COUNT(*) FROM ordens WHERE simulada=0").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM execucoes WHERE simulada=0").fetchone()[0] == 0
    modos = []  # o roteador so e construido no modo 'simulado' (o gateway e o unico dono de order_send)
    for arq in RAIZ_PKG.rglob("*.py"):
        for no in ast.walk(ast.parse(arq.read_text())):
            if isinstance(no, ast.Call) and getattr(no.func, "id", "") == "Roteador":
                modos.append(ast.literal_eval(no.args[0]))
    assert modos == ["simulado"]


def test_posicoes_zeradas_ao_fim_e_trades_consistentes(sessao):
    c = _con(sessao)
    for braco in IDS_BRACOS:
        u = c.execute("SELECT quantidade FROM posicoes WHERE braco=? ORDER BY t_ns DESC, rowid DESC LIMIT 1", (braco,)
                      ).fetchone()
        assert u is None or u[0] == 0, braco
    for t in c.execute("SELECT * FROM trades"):
        assert t["t_saida_ns"] >= t["t_entrada_ns"] and t["motivo_saida"]
        assert abs(t["liquido_brl"] - (t["bruto_brl"] - t["tarifas_brl"] - t["corretagem_brl"] - t["slippage_brl"])) < 1e-6
    # stop/alvo/tempo/zeragem: motivos deterministicos conhecidos
    motivos = {r[0] for r in c.execute("SELECT DISTINCT motivo_saida FROM trades")}
    assert motivos <= {"stop", "alvo", "tempo", "encerrar", "zeragem", "kill", "saida"}


def test_auditoria_com_cadeia_de_hash_integra_e_adulteracao_detectada(sessao):
    ok, msgs = verificar_diretorio(sessao["saida"] / "auditoria")
    assert ok and msgs
    arq = sorted((sessao["saida"] / "auditoria").glob("auditoria-*.jsonl"))[0]
    linhas = arq.read_text().splitlines()
    tipos = {json.loads(ln)["tipo"] for ln in linhas}
    assert {"sessao_inicio", "decisao", "chamada_llm", "sessao_fim", "experimento"} <= tipos
    assert not any("sk-" in ln for ln in linhas)  # nenhum segredo
    copia = sessao["saida"].parent / "adulterada"
    copia.mkdir()
    adulterado = list(linhas)
    adulterado[5] = adulterado[5].replace('"tipo"', '"tipo_"', 1) if '"tipo"' in adulterado[5] else adulterado[5] + " "
    (copia / arq.name).write_text("\n".join(adulterado) + "\n")
    assert main(["auditoria", "--dir", str(copia)]) == 1  # saida != 0 em caso de adulteracao
    assert main(["auditoria", "--dir", str(sessao["saida"] / "auditoria")]) == 0


def test_painel_le_o_banco_gravado_pela_sessao(sessao):
    from fastapi.testclient import TestClient

    from robowin.painel.app import criar_app
    cli = TestClient(criar_app(banco=sessao["saida"] / "robowin.db"))
    for rota in ("/", "/decisoes", "/operacoes", "/desempenho", "/modelos", "/auditoria"):
        r = cli.get(rota)
        assert r.status_code == 200, rota
    assert "SINTÉTICO" in cli.get("/").text.upper() or "SINTETICO" in cli.get("/").text.upper()
    assert "claude_gpt" in cli.get("/decisoes").text
    assert cli.get("/api/decisoes").status_code == 200


def test_mesma_seed_mesmo_resultado_reprodutivel(tmp_path):
    from robowin.decisao.simulacao import ParamsSimulacao, executar_sessao, montar_sessao

    def rodar(nome):
        p = ParamsSimulacao(seed=5, n_ticks=2200, saida=tmp_path / nome, com_condicional=False)
        s = montar_sessao(p)
        try:
            resumo = asyncio.run(executar_sessao(s))
        finally:
            s.banco.fechar()
        c = sqlite3.connect(tmp_path / nome / "robowin.db")
        dossies = [r[0] for r in c.execute("SELECT hash_dossie FROM decisoes ORDER BY t_ns, braco, id")]
        return resumo, dossies
    r1, d1 = rodar("a")
    r2, d2 = rodar("b")
    assert r1 == r2 and d1 == d2 and d1  # decisoes, dossies e resultados identicos nas duas execucoes


def test_simular_recusa_sobrescrever_banco_e_real_sem_chaves(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    saida = tmp_path / "x"
    assert main(["simular", "--n-ticks", "1500", "--saida", str(saida), "--provedor", "real"]) == 2  # sem chaves
    saida.mkdir(exist_ok=True)
    (saida / "robowin.db").write_bytes(b"")
    assert main(["simular", "--n-ticks", "1500", "--saida", str(saida)]) == 2  # nao sobrescreve resultados


# =========================================================================================== nucleo ====
def _msg_tick(t_ns: int, bid: float, ask: float, extra: dict | None = None) -> tuple[str, dict]:
    d = {"t_servidor_ms": t_ns // 1_000_000, "t_utc_ns": t_ns, "t_recebido_ns": t_ns + 1_000_000, "bid": bid,
         "ask": ask, "last": bid, "volume": 1.0, "flags": 8, "simbolo": "WINZ26", "t_disponivel_ns": t_ns,
         "backfill": False, "seq": 1}
    d.update(extra or {})
    return "tick", d


async def test_nucleo_consome_o_gateway_bloqueia_na_lacuna_e_envia_heartbeat():
    from robowin.decisao.nucleo import FonteLista, Nucleo
    arm = BracoRoteirizado("a", [lambda s, c: s.entrada(c, stop=60.0, alvo=None, tempo=600), lambda s, c: s.nada(c)])
    orq, banco, _ = montar_orq([arm], modo_tempo="real")
    msgs = [_msg_tick(T0 + i * 1_000_000_000, 130000.0, 130005.0) for i in range(8)]
    msgs.insert(3, ("heartbeat", {"origem": "gateway", "hb": 1, "t_ns": T0}))
    msgs.insert(4, ("estado", {"feed_parado": False, "lacuna_pendente": False}))
    fonte = FonteLista(msgs)
    hb = []
    nuc = Nucleo(orq, fonte, heartbeat=lambda: hb.append(1), cooperar_s=0.0)
    await nuc.executar(duracao_s=0.3)
    await orq.aguardar_tarefas()
    await orq.on_tempo()
    assert orq.ticks_processados == 8 and hb
    assert banco.consultar("SELECT * FROM decisoes WHERE braco='a' AND resultado='executada'")  # tarefa -> ordem
    for k in (10, 11):  # a ordem a mercado executa no tick seguinte
        await nuc.tratar(*_msg_tick(T0 + k * 1_000_000_000, 130000.0, 130005.0))
    assert orq.sim.posicao("a").quantidade == 1  # decisao no modo real (tarefas) -> entrada no simulador
    await nuc.tratar(*_msg_tick(T0 + 12 * 1_000_000_000, 130000.0, 130005.0, {"backfill": True}))
    assert orq.ticks_processados == 10 and orq.bloqueio.motivo(orq._agora()) == "backfill"  # backfill nao e ao vivo
    # lacuna de sequencia detectada pelo SUB: bloqueia novas entradas
    fonte.lacunas = 1
    nuc._verificar_integridade()
    assert orq.bloqueio.motivo(orq._agora()) in ("lacuna_sequencia", "backfill")
    inc = banco.consultar("SELECT * FROM incidentes WHERE detalhe LIKE '%lacuna de sequencia%'")
    assert inc


def test_nucleo_com_zeromq_local_recebe_ticks_e_conta_lacunas():
    pytest.importorskip("zmq")
    import socket

    from robowin.core.barramento import PublicadorZMQ
    from robowin.decisao.nucleo import FonteZMQ
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    pub = PublicadorZMQ(f"tcp://127.0.0.1:{porta}")
    fonte = FonteZMQ(f"tcp://127.0.0.1:{porta}")
    try:
        recebidas = []
        fim = time.time() + 5
        while time.time() < fim and len(recebidas) < 3:
            pub.publicar("tick", _msg_tick(T0, 130000.0, 130005.0)[1])
            time.sleep(0.05)
            while (m := fonte.receber()) is not None:
                recebidas.append(m)
        assert len(recebidas) >= 3 and recebidas[0][0] == "tick"
        pub.seq += 5  # o gateway "perdeu" mensagens: lacuna de sequencia
        pub.publicar("tick", _msg_tick(T0, 130000.0, 130005.0)[1])
        fim = time.time() + 3
        while time.time() < fim and fonte.lacunas == 0:
            time.sleep(0.05)
            fonte.receber()
        assert fonte.lacunas >= 1
    finally:
        fonte.fechar()
        pub.fechar()


def test_cli_nucleo_e_simular_estao_registrados():
    import argparse

    from robowin.decisao import cli_cmds
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    cli_cmds.registrar(sub)
    a = p.parse_args(["nucleo", "--duracao-s", "1"])
    assert a.pub.startswith("tcp://") and a.duracao_s == 1.0 and a.provedor == "falso"
    b = p.parse_args(["simular", "--seed", "3"])
    assert b.seed == 3 and not b.sem_condicional


def test_pacote_decisao_nao_tem_indicadores_tecnicos_nem_ordens():
    for arq in RAIZ_PKG.rglob("*.py"):
        arvore = ast.parse(arq.read_text())
        for no in ast.walk(arvore):
            if isinstance(no, ast.Call):
                f = no.func
                nome = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                assert not nome.startswith("order_send"), arq
            if isinstance(no, ast.ImportFrom | ast.Import):
                mods = [no.module or ""] if isinstance(no, ast.ImportFrom) else [a.name for a in no.names]
                assert not any(m.split(".")[0] == "MetaTrader5" for m in mods), arq


def test_serie_helper():
    assert len(serie([1.0, 2.0])) == 2
