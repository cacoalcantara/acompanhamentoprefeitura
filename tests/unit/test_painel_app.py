import sqlite3

import pytest
from fastapi.testclient import TestClient

from robowin.painel.app import criar_app
from robowin.painel.demo import popular
from robowin.painel.metricas import metricas_braco, wilson

PAGINAS = ["/", "/decisoes", "/operacoes", "/desempenho", "/modelos", "/auditoria"]


@pytest.fixture
def cli(tmp_path):
    banco = popular(tmp_path / "demo.sqlite")
    app = criar_app(banco=banco, dir_dados=tmp_path / "dados", token="segredo")
    return TestClient(app), tmp_path


@pytest.fixture
def vazio(tmp_path):
    app = criar_app(banco=tmp_path / "nao_existe.sqlite", dir_dados=tmp_path / "d", token="segredo")
    return TestClient(app)


@pytest.mark.parametrize("rota", PAGINAS)
def test_paginas_pt_br_e_faixas(cli, rota):
    c, _ = cli
    r = c.get(rota)
    assert r.status_code == 200
    assert '<html lang="pt-BR">' in r.text
    assert "SIMULAÇÃO — sem ordens reais" in r.text
    assert "DADOS SINTÉTICOS" in r.text
    assert "/static/htmx.min.js" in r.text and "http" not in r.text.split("<body>")[0].replace("http-equiv", "")


@pytest.mark.parametrize("rota", PAGINAS)
def test_banco_vazio_nao_quebra(vazio, rota):
    r = vazio.get(rota)
    assert r.status_code == 200
    assert "SIMULAÇÃO — sem ordens reais" in r.text
    assert "DADOS SINTÉTICOS" not in r.text


def test_apis_json(cli, vazio):
    c, _ = cli
    for rota in ["/api/resumo", "/api/capital", "/api/desempenho", "/api/latencia", "/api/decisoes"]:
        assert c.get(rota).status_code == 200
        assert vazio.get(rota).status_code == 200
    assert "claude" in c.get("/api/capital").json()


def test_acao_sem_token_negada(cli):
    c, tmp = cli
    r = c.post("/acoes/emergencia", data={"confirmacao": "EMERGENCIA"})
    assert r.status_code in (401, 403)
    r = c.post("/acoes/emergencia", data={"confirmacao": "EMERGENCIA", "token": "errado"})
    assert r.status_code == 401
    assert not (tmp / "dados" / "EMERGENCIA").exists()


def test_acao_sem_token_configurado_negada(tmp_path):
    c = TestClient(criar_app(banco=None, dir_dados=tmp_path, token=""))
    assert c.post("/acoes/emergencia", data={"confirmacao": "EMERGENCIA", "token": ""}).status_code == 403


def test_kill_switch_exige_confirmacao(cli):
    c, tmp = cli
    r = c.post("/acoes/emergencia", data={"token": "segredo"})
    assert r.status_code == 400
    assert not (tmp / "dados" / "EMERGENCIA").exists()


@pytest.mark.parametrize("via", ["form", "header"])
def test_kill_switch_grava_flag_e_incidente(cli, via):
    c, tmp = cli
    if via == "form":
        r = c.post("/acoes/emergencia", data={"token": "segredo", "confirmacao": "EMERGENCIA"})
    else:
        r = c.post("/acoes/emergencia", data={"confirmacao": "EMERGENCIA"}, headers={"X-Robowin-Token": "segredo"})
    assert r.status_code == 200 and r.json()["ok"]
    assert (tmp / "dados" / "EMERGENCIA").exists()
    con = sqlite3.connect(tmp / "demo.sqlite")
    assert con.execute("SELECT COUNT(*) FROM incidentes WHERE origem='painel' AND severidade='critico'").fetchone()[0] == 1
    assert "EMERGENCIA" in c.get("/").text


def test_cookie_de_token(cli):
    c, tmp = cli
    assert c.post("/entrar", data={"token": "x"}, follow_redirects=False).status_code == 401
    assert c.post("/entrar", data={"token": "segredo"}, follow_redirects=False).status_code == 303
    assert c.post("/acoes/emergencia", data={"confirmacao": "EMERGENCIA"}).status_code == 200


def test_kill_switch_com_banco_ausente(tmp_path):
    c = TestClient(criar_app(banco=tmp_path / "novo.sqlite", dir_dados=tmp_path / "d", token="t"))
    assert c.post("/acoes/emergencia", data={"token": "t", "confirmacao": "EMERGENCIA"}).status_code == 200
    assert (tmp_path / "d" / "EMERGENCIA").exists()


def test_rotulo_candidato_nao_validado(cli):
    c, _ = cli
    t = c.get("/decisoes").text
    assert "candidato não validado" in t
    assert "texto sintético de demonstração" in t
    assert "candidato não validado" in c.get("/modelos").text


def test_desempenho_wilson_controle_e_amostra(cli):
    c, _ = cli
    t = c.get("/desempenho").text
    assert "controle_aleatorio" in t
    assert "amostra insuficiente" not in t  # demo tem n=40
    assert "IC de Wilson" in t and "p99" in t


def test_amostra_insuficiente():
    m = metricas_braco([10.0, -5.0, 7.0])
    assert m["amostra_insuficiente"]
    lo, hi = wilson(2, 3)
    assert 0 < lo < 2 / 3 < hi < 1
    assert wilson(0, 0) == (0.0, 1.0)


def test_metricas_provider_substituivel(tmp_path):
    banco = popular(tmp_path / "d.sqlite")
    prov = lambda b: {"x": metricas_braco([1.0] * 5)}  # noqa: E731
    c = TestClient(criar_app(banco=banco, dir_dados=tmp_path, token="t", metricas_provider=prov))
    t = c.get("/desempenho").text
    assert "amostra insuficiente" in t and "<td>x</td>" in t


def test_auditoria_verifica_cadeia(tmp_path):
    from robowin.auditoria.cadeia import verificar_diretorio  # noqa: F401
    (tmp_path / "dados" / "auditoria").mkdir(parents=True)
    c = TestClient(criar_app(banco=None, dir_dados=tmp_path / "dados", token="t"))
    assert c.get("/auditoria").status_code == 200


def test_ui_diz_flag_gravada_e_so_confirma_com_incidente_do_gateway(cli):
    import time
    c, tmp = cli
    r = c.post("/acoes/emergencia", data={"token": "segredo", "confirmacao": "EMERGENCIA"})
    assert r.json()["estado"] == "flag_gravada_aguardando_confirmacao_do_gateway"
    pagina = c.get("/?msg=emergencia").text
    assert "aguardando confirmação do gateway" in pagina
    assert "Kill switch acionado" not in pagina and "deve estar zerado" not in pagina
    assert "gateway/núcleo confirmou" not in pagina
    # o gateway/nucleo registra o incidente depois da flag: so entao a UI confirma
    con = sqlite3.connect(tmp / "demo.sqlite")
    con.execute("INSERT INTO incidentes(t_ns, origem, severidade, detalhe) VALUES (?,?,?,?)",
                (time.time_ns() + 10**9, "gateway", "alerta", "bloqueio_emergencia"))
    con.commit()
    con.close()
    assert "gateway/núcleo confirmou" in c.get("/").text


def test_entrar_tem_limite_de_tentativas(cli):
    c, _ = cli
    for _ in range(5):
        assert c.post("/entrar", data={"token": "x"}, follow_redirects=False).status_code == 401
    assert c.post("/entrar", data={"token": "x"}, follow_redirects=False).status_code == 429
    assert c.post("/entrar", data={"token": "segredo"}, follow_redirects=False).status_code == 429
    assert c.post("/acoes/emergencia", data={"token": "segredo", "confirmacao": "EMERGENCIA"}).status_code == 429


def test_flag_de_emergencia_vem_da_config_central_e_nao_do_cwd(tmp_path, monkeypatch):
    from robowin.core.config import caminho_flag_emergencia, dir_dados
    raiz = tmp_path / "proj"
    (raiz / "config").mkdir(parents=True)
    toml = raiz / "config" / "robowin.toml"
    toml.write_text('[geral]\ndados = "meus_dados"\n', encoding="utf-8")
    monkeypatch.delenv("ROBOWIN_DADOS", raising=False)
    outro = tmp_path / "outro_cwd"
    outro.mkdir()
    monkeypatch.chdir(outro)
    esperado = (raiz / "meus_dados" / "EMERGENCIA").resolve()
    assert caminho_flag_emergencia(toml) == esperado and dir_dados(toml) == esperado.parent
    monkeypatch.chdir(raiz)
    assert caminho_flag_emergencia(toml) == esperado  # mesmo caminho, qualquer CWD
    assert caminho_flag_emergencia(toml, arquivo=tmp_path / "x" / "F") == (tmp_path / "x" / "F").resolve()
    monkeypatch.setenv("ROBOWIN_DADOS", str(tmp_path / "env"))
    assert dir_dados(toml) == (tmp_path / "env").resolve()


def test_cli_gateway_painel_e_emergencia_usam_o_mesmo_caminho(tmp_path, monkeypatch):
    from robowin.cli import main
    raiz = tmp_path / "proj"
    (raiz / "config").mkdir(parents=True)
    toml = raiz / "config" / "robowin.toml"
    toml.write_text('[geral]\ndados = "dd"\nconta = "demo_mt5"\n', encoding="utf-8")
    monkeypatch.delenv("ROBOWIN_DADOS", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["emergencia", "--config", str(toml), "--motivo", "teste"]) == 0
    assert (raiz / "dd" / "EMERGENCIA").exists() and not (tmp_path / "dados_locais").exists()
