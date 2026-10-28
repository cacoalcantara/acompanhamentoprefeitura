"""`robowin aceitacao`: roda inteira contra o DubleMT5 e o DubleMT4, sem a flag nada e enviado, conta real e recusada,
perfil a_confirmar bloqueia as ordens, nenhum segredo vaza e REPROVADO muda o codigo de saida. Dados 100% SINTETICOS."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pytest

from robowin.aceitacao import cli_cmds as K
from robowin.aceitacao import etapas as E
from robowin.aceitacao import matriz as M
from robowin.cli import main
from robowin.core.relogio import RelogioSimulado

SP = dt.timezone(dt.timedelta(hours=-3))
SEGREDO_IA = "sk-ant-SEGREDO-DA-ACEITACAO-123456"
SEGREDO_MT5 = "SenhaSuperSecreta#9981"


def _args(*extra: str, saida: Path) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    K.registrar(p.add_subparsers(dest="cmd"))
    return p.parse_args(["aceitacao", "--saida", str(saida), *extra])


def _relogio(inicio: dt.datetime = K.INICIO_DUBLE) -> RelogioSimulado:
    return RelogioSimulado(int(inicio.timestamp() * 1e9))


def _rodar(tmp_path, *extra: str, plataforma: str = "mt5", kw_duble: dict | None = None, inicio: dt.datetime = K.INICIO_DUBLE):
    """Roda com um duble que o teste controla. Devolve (codigo, duble_ou_conector, relatorio_json)."""
    a = _args("--duble", "--plataforma", plataforma, "--minutos-coleta", "1", "--repeticoes", "4", *extra, saida=tmp_path)
    rel = _relogio(inicio)
    term = K.criar_duble_sim(plataforma, rel, K._n_ticks_duble(1, 4, 3.0), **(kw_duble or {}))
    rc = K.rodar(a, terminal=term, relogio=rel)
    js = sorted(tmp_path.glob("aceitacao-*.json"))
    assert js, "relatorio .json nao foi gravado"
    return rc, term, json.loads(js[-1].read_text(encoding="utf-8"))


def _estados(rel: dict) -> dict[str, str]:
    return {e["etapa"]: e["estado"] for e in rel["etapas"]}


def _envios(term) -> int:
    srv = getattr(getattr(term, "_tr", None), "servidor", None)  # ConectorMT4 -> DubleMT4
    return len(srv.envios) if srv is not None else len(term.envios)


@pytest.fixture(autouse=True)
def _sem_chaves(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "ROBOWIN_MT5_SENHA"):
        monkeypatch.delenv(v, raising=False)


# ---------------------------------------------------------------------------------------------- completo
def test_aceitacao_completa_contra_duble_mt5_aprova(tmp_path):
    rc, d, rel = _rodar(tmp_path, "--confirmo-conta-demo")
    est = _estados(rel)
    assert rc == 0 and "REPROVADO" not in est.values(), [e for e in rel["etapas"] if e["estado"] == "REPROVADO"]
    for cod in ("A1.1", "A1.3", "A2.1", "A2.2", "A2.3", "A2.4", "A3.1", "A3.2", "A4.1", "A4.2", "A4.3", "A4.4", "A5.1", "A5.2",
                "A5.3", "A5.4", "A6", "A7", "A8.1", "A8.2", "A8.3", "A9"):
        assert est[cod] == "APROVADO", (cod, est)
    assert est["A10"] == "NAO VERIFICAVEL AQUI" and est["A11"] == "NAO VERIFICAVEL AQUI"  # sem chave / nao e MT4
    assert rel["duble"] is True and "NAO vale como aceitacao" in rel["veredito"]
    assert rel["ordens_enviadas"] == len(d.envios) > 0
    assert d.posicao_liquida("WINZ26") == 0 and d.volume_pendente("WINZ26") == 0 and not d._conta.pos and not d._conta.ordens
    a6 = next(e for e in rel["etapas"] if e["etapa"] == "A6")
    assert a6["dados"]["n"] == 8 and "maximo de mensagens no minuto" in a6["detalhe"]  # 4 repeticoes x (colocar + cancelar)
    assert set(rel["latencias_ms"]) >= {"colocar_limitada", "cancelar", "envio_ate_confirmacao"}
    for c in ("p50", "p95", "p99"):
        assert c in rel["latencias_ms"]["envio_ate_confirmacao"]
    assert list(tmp_path.glob("aceitacao-*.md"))


def test_aceitacao_completa_contra_duble_mt4_aprova(tmp_path):
    rc, con, rel = _rodar(tmp_path, "--confirmo-conta-demo", plataforma="mt4")
    est = _estados(rel)
    assert rc == 0 and "REPROVADO" not in est.values(), [e for e in rel["etapas"] if e["estado"] == "REPROVADO"]
    for cod in ("A2.3", "A4.1", "A5.2", "A5.3", "A5.4", "A6", "A7", "A8.1", "A8.2", "A8.3", "A9", "A11.1", "A11.2"):
        assert est[cod] == "APROVADO", (cod, est)
    assert est["A4.3"] == "NAO VERIFICAVEL AQUI"  # MT4 nao tem livro
    assert "hedging" in next(e for e in rel["etapas"] if e["etapa"] == "A5.2")["detalhe"]
    assert _envios(con) > 0 and rel["plataforma"] == "mt4"


def test_cli_main_duble_roda_e_sai_com_zero(tmp_path, capsys):
    rc = main(["aceitacao", "--duble", "--confirmo-conta-demo", "--minutos-coleta", "1", "--repeticoes", "3",
               "--saida", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "VEREDITO" in out and "aceitacao-" in out
    rc = main(["aceitacao", "--duble", "--json", "--minutos-coleta", "1", "--saida", str(tmp_path)])
    corpo = json.loads(capsys.readouterr().out.split("\n", 1)[1])  # 1a linha = aviso de que nao ha ordens
    assert rc == 0 and corpo["ordens_enviadas"] == 0


# ---------------------------------------------------------------------------------------------- seguranca
@pytest.mark.parametrize("plataforma", ["mt5", "mt4"])
def test_sem_a_flag_nada_e_enviado(tmp_path, plataforma):
    rc, term, rel = _rodar(tmp_path, plataforma=plataforma)
    est = _estados(rel)
    assert rc == 0 and _envios(term) == 0 and rel["ordens_enviadas"] == 0 and rel["confirmou_conta_demo"] is False
    for cod in ("A5", "A6", "A8", "A9"):
        assert est[cod] == "NAO VERIFICAVEL AQUI"
        assert "--confirmo-conta-demo" in next(e for e in rel["etapas"] if e["etapa"] == cod)["detalhe"]
    assert est["A7"] == "NAO VERIFICAVEL AQUI"
    assert est["A4.1"] == "APROVADO"  # as etapas de leitura rodam
    assert E.ETAPAS_DE_ORDEM == {"A5", "A6", "A8", "A9"}


def test_executor_nao_pode_ser_criado_sem_a_flag(tmp_path):
    a = _args("--duble", saida=tmp_path)
    rel = _relogio()
    ctx = E.Contexto(terminal=K.criar_duble_sim("mt5", rel, 100), plataforma="mt5", caps=E.CapacidadesTerminal("mt5", True, True, "netting", 1, True),
                     perfil=None, risco=None, cfg={}, relogio=rel, dormir=lambda s: None, duble=True, login=1, servidor="X-DEMO",
                     confirmo=False, saida=tmp_path)  # type: ignore[arg-type]
    assert a.confirmo_conta_demo is False
    with pytest.raises(RuntimeError, match="sem --confirmo-conta-demo"):
        E._executor(ctx)


@pytest.mark.parametrize("modo", ["real", "contest"])
def test_conta_real_ou_contest_e_recusada_e_aborta_sem_envio(tmp_path, modo):
    rc, d, rel = _rodar(tmp_path, "--confirmo-conta-demo", kw_duble={"trade_mode": modo})
    est = _estados(rel)
    assert rc != 0 and est["A2.1"] == "REPROVADO" and _envios(d) == 0 and rel["ordens_enviadas"] == 0
    assert rel["abortada"] and "so DEMO" in rel["abortada"]
    assert rel["veredito"] == "REPROVADO"
    for cod in ("A3", "A4", "A5", "A6", "A8", "A9"):  # nada alem da identidade roda
        assert est[cod] == "NAO VERIFICAVEL AQUI"
        assert "abortada" in next(e for e in rel["etapas"] if e["etapa"] == cod)["detalhe"]


def test_conta_mt4_real_e_recusada_sem_envio(tmp_path):
    rc, con, rel = _rodar(tmp_path, "--confirmo-conta-demo", plataforma="mt4", kw_duble={"demo": False})
    assert rc != 0 and _estados(rel)["A2.1"] == "REPROVADO" and _envios(con) == 0


def test_servidor_sem_demo_e_login_diferente_sao_recusados(tmp_path):
    rc, d, rel = _rodar(tmp_path, "--confirmo-conta-demo", "--servidor", "XPMT5-PRODUCAO")  # esperado != observado e sem DEMO
    assert rc != 0 and _estados(rel)["A2.1"] == "REPROVADO" and len(d.envios) == 0
    rc, d, rel = _rodar(tmp_path / "b", "--confirmo-conta-demo", "--login", "123456")
    assert rc != 0 and _estados(rel)["A2.1"] == "REPROVADO" and len(d.envios) == 0 and "difere do esperado" in rel["abortada"]
    rc, d, rel = _rodar(tmp_path / "c", "--confirmo-conta-demo", kw_duble={"servidor": "XPMT5-REAL"})  # observado sem DEMO
    assert rc != 0 and _estados(rel)["A2.1"] == "REPROVADO" and len(d.envios) == 0


def test_perfil_a_confirmar_nao_roda_etapas_de_ordem(tmp_path):
    rc, d, rel = _rodar(tmp_path, "--confirmo-conta-demo", "--ativo", "WDO")
    est = _estados(rel)
    assert rc == 0 and rel["perfil_a_confirmar"] is True and len(d.envios) == 0
    for cod in ("A5", "A6", "A8", "A9"):
        assert est[cod] == "NAO VERIFICAVEL AQUI", (cod, est)
    assert any("a_confirmar" in e["detalhe"] for e in rel["etapas"] if e["etapa"] in ("A5", "A6", "A8", "A9"))
    a32 = next(e for e in rel["etapas"] if e["etapa"] == "A3.2")
    assert a32["estado"] == "NAO VERIFICAVEL AQUI" and "OBSERVADAS" in a32["detalhe"]


def test_fora_do_pregao_etapas_de_mercado_e_ordem_sao_nao_verificaveis(tmp_path):
    sabado = dt.datetime(2026, 10, 31, 11, 0, tzinfo=SP)
    rc, d, rel = _rodar(tmp_path, "--confirmo-conta-demo", inicio=sabado)
    est = _estados(rel)
    assert rc == 0 and len(d.envios) == 0
    for cod in ("A4", "A5", "A6", "A8", "A9"):
        assert est[cod] == "NAO VERIFICAVEL AQUI", (cod, est)
    assert "pregao" in next(e for e in rel["etapas"] if e["etapa"] == "A5")["detalhe"]


def test_conta_com_posicao_previa_nao_e_tocada(tmp_path):
    rel = _relogio()
    a = _args("--duble", "--confirmo-conta-demo", "--minutos-coleta", "1", "--repeticoes", "2", saida=tmp_path)
    d = K.criar_duble_sim("mt5", rel, K._n_ticks_duble(1, 2, 3.0))
    d.operar_manual(0, 1.0, "WINZ26")  # posicao do usuario (magic 0)
    K.rodar(a, terminal=d, relogio=rel)
    est = _estados(json.loads(next(tmp_path.glob("aceitacao-*.json")).read_text(encoding="utf-8")))
    assert est["A5"] == "NAO VERIFICAVEL AQUI" and est["A8"] == "NAO VERIFICAVEL AQUI"
    assert len(d.envios) == 0 and d.posicao_liquida("WINZ26") == 1.0


# ---------------------------------------------------------------------------------------------- codigo de saida
def test_reprovado_muda_o_codigo_de_saida(tmp_path):
    rel = _relogio()
    a = _args("--duble", "--confirmo-conta-demo", "--minutos-coleta", "1", "--repeticoes", "2", saida=tmp_path)
    d = K.criar_duble_sim("mt5", rel, K._n_ticks_duble(1, 2, 3.0))
    d.definir_conta(trade_expert=False)  # Algo Trading desligado
    rc = K.rodar(a, terminal=d, relogio=rel)
    est = _estados(json.loads(next(tmp_path.glob("aceitacao-*.json")).read_text(encoding="utf-8")))
    assert rc == 1 and est["A2.2"] == "REPROVADO" and len(d.envios) == 0
    assert est["A5"] == "NAO VERIFICAVEL AQUI"  # A2 reprovada: nenhuma ordem
    assert M.codigo_de_saida([M.aprovada("X", "n", "c"), M.nao_verificavel("Y", "n", "c", "m")]) == 0
    assert M.codigo_de_saida([M.aprovada("X", "n", "c"), M.reprovada("Y", "n", "c", "m")]) == 1


def test_falha_no_meio_das_ordens_reprova_e_deixa_a_conta_zerada(tmp_path):
    rel = _relogio()
    a = _args("--duble", "--confirmo-conta-demo", "--minutos-coleta", "1", "--repeticoes", "2", saida=tmp_path)
    d = K.criar_duble_sim("mt5", rel, K._n_ticks_duble(1, 2, 3.0))
    from robowin.mt5.duble import Falha
    d.injetar_falhas(Falha(10006))  # o terminal rejeita o 1o envio (a limitada longe)
    rc = K.rodar(a, terminal=d, relogio=rel)
    j = json.loads(next(tmp_path.glob("aceitacao-*.json")).read_text(encoding="utf-8"))
    assert rc != 0 and _estados(j)["A5.1"] == "REPROVADO"
    assert d.posicao_liquida("WINZ26") == 0 and d.volume_pendente("WINZ26") == 0


def test_terminal_indisponivel_gera_relatorio_e_codigo_3(tmp_path, monkeypatch):
    def quebra(*a, **k):
        raise ImportError("MetaTrader5 indisponivel")
    monkeypatch.setattr(K, "_terminal", quebra)
    rc = main(["aceitacao", "--confirmo-conta-demo", "--login", "123456", "--servidor", "X-DEMO", "--saida", str(tmp_path)])
    j = json.loads(next(tmp_path.glob("aceitacao-*.json")).read_text(encoding="utf-8"))
    assert rc == 3 and _estados(j)["A1.3"] == "REPROVADO" and j["ordens_enviadas"] == 0
    assert _estados(j)["A5"] == "NAO VERIFICAVEL AQUI"


# ---------------------------------------------------------------------------------------------- segredos e A10
def test_nenhum_segredo_nem_login_completo_no_relatorio_e_a10_mede_ia(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SEGREDO_IA)
    monkeypatch.setenv("ROBOWIN_MT5_SENHA", SEGREDO_MT5)
    from robowin.ia.provedores import criar_provedor
    monkeypatch.setattr(E, "provedor_para_cli", lambda prov, cat: (criar_provedor("falso", catalogo=None), True))
    rc, d, rel = _rodar(tmp_path, "--confirmo-conta-demo", "--login", "900001", "--json")
    capsys.readouterr()
    assert rc == 0
    a10 = [e for e in rel["etapas"] if e["etapa"].startswith("A10")]
    assert a10 and all(e["estado"] == "APROVADO" for e in a10), a10
    assert any(k.startswith("ia:") and "ttft_ms" in v and "p99" in v["total_ms"] for k, v in rel["latencias_ms"].items())
    texto = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    for proibido in (SEGREDO_IA, SEGREDO_MT5, "900001"):
        assert proibido not in texto, proibido
    assert "****01" in texto and "XPMT5-DEMO" in texto
    assert "sk-ant" not in texto


def test_a10_sem_chave_e_nao_verificavel_e_pular_ia_nao_se_aplica(tmp_path):
    rc, _, rel = _rodar(tmp_path)
    a10 = next(e for e in rel["etapas"] if e["etapa"] == "A10")
    assert a10["estado"] == "NAO VERIFICAVEL AQUI" and a10["aplicavel"] is True and "chave" in a10["detalhe"]
    rc, _, rel = _rodar(tmp_path / "x", "--pular-ia")
    a10 = next(e for e in rel["etapas"] if e["etapa"] == "A10")
    assert a10["aplicavel"] is False


# ---------------------------------------------------------------------------------------------- matriz
def test_veredito_nunca_aceita_por_omissao():
    ok = M.aprovada("A1", "n", "c")
    nv = M.nao_verificavel("A5", "n", "c", "sem flag")
    nao_aplica = M.nao_verificavel("A11", "n", "c", "mt5", aplicavel=False)
    assert M.veredito([ok, nv]).startswith("INCOMPLETO") and "A5" in M.veredito([ok, nv])
    assert M.veredito([ok, nao_aplica]).startswith("ACEITO")
    assert M.veredito([ok, M.reprovada("A2", "n", "c", "x"), nv]) == "REPROVADO"
    assert M.veredito([ok], duble=True).startswith("SEM REPROVACOES NO DUBLE") and "NAO vale" in M.veredito([ok], duble=True)
    assert M.veredito([ok], abortada=True).startswith("INCOMPLETO")


def test_higienizar_mascara_login_usuario_e_chaves(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-ABCDEFGHIJ123456")
    t = M.higienizar(r"login 7654321 em C:\Users\Maria Silva\AppData\x e /home/joao/robowin; sk-proj-ABCDEFGHIJ123456", (7654321,))
    assert "7654321" not in t and "****21" in t and "Maria" not in t and "joao" not in t and "sk-proj" not in t
    assert M.mascarar_login(None) == "(nao informado)" and M.mascarar_login(900001) == "****01"


# ---------------------------------------------------------------------------------------------- scripts e docs
RAIZ = Path(__file__).resolve().parents[2]


def test_scripts_windows_existem_sao_ascii_e_avisam_que_nao_foram_executados():
    comp = (RAIZ / "scripts" / "windows" / "compilar_ea.ps1")
    tudo = (RAIZ / "scripts" / "windows" / "validar_tudo.ps1")
    for p in (comp, tudo):
        txt = p.read_bytes().decode("ascii")  # ASCII puro: PowerShell 5.1 sem BOM nao corrompe acentos
        assert "SEM PODER SER EXECUTADO" in txt.upper()
    c = comp.read_text(encoding="ascii")
    assert "/compile:" in c and "/log:" in c and "metaeditor.exe" in c and "RobowinBridge.mq4" in c and ".ex4" in c
    t = tudo.read_text(encoding="ascii")
    for trecho in ("pytest", "conferir_constantes_mt5.py", "lint_mql4.py", "robowin verificar", "--confirmo-conta-demo",
                   "ConfirmoContaDemo", "Compress-Archive", ".env", "ROBOWIN_MT5_SENHA", "MANIFESTO"):
        assert trecho in t, trecho
    assert "compilar_ea.ps1" in t


def test_todos_os_ps1_sao_ascii_e_os_da_ci_tem_modo_seco():
    pasta = RAIZ / "scripts" / "windows"
    todos = sorted(pasta.glob("*.ps1"))
    assert len(todos) >= 7
    for p in todos:
        p.read_bytes().decode("ascii")  # ASCII puro (PowerShell 5.1 le arquivo sem BOM como ANSI)
    for nome in ("analisar_scripts.ps1", "ci_preparar_metaeditor.ps1", "instalar.ps1"):
        assert "SEM PODER SER EXECUTADO" in (pasta / nome).read_text(encoding="ascii").upper(), nome
    assert "ParseFile" in (pasta / "analisar_scripts.ps1").read_text(encoding="ascii")
    for nome in ("validar_tudo.ps1", "instalar.ps1"):
        assert "[switch]$SemTerminal" in (pasta / nome).read_text(encoding="ascii"), nome
    ci = (RAIZ / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for trecho in ("analisar_scripts.ps1", "validar_tudo.ps1 -SemTerminal", "instalar.ps1 -SemTerminal",
                   "ci_preparar_metaeditor.ps1", "compilar_ea.ps1"):
        assert trecho in ci, trecho


def test_docs_definem_o_cem_por_cento_como_aceitacao_no_windows():
    doc = (RAIZ / "docs" / "12-aceitacao-no-windows.md").read_text(encoding="utf-8")
    assert "sem nenhuma etapa REPROVADA" in doc and "NUNCA" in doc and "--confirmo-conta-demo" in doc
    assert "aceitação rodada no Windows" in (RAIZ / "docs" / "ENTREGA.md").read_text(encoding="utf-8")
    assert "docs/12-aceitacao-no-windows.md" in (RAIZ / "README.md").read_text(encoding="utf-8")
