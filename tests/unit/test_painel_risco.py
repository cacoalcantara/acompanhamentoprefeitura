"""Pagina "Risco" do painel: login por usuario+token, isolamento entre usuarios, formulario com pre-visualizacao,
gravacao com token de acoes, aprovacao com APROVO + token digitado, historico e auditoria. Sem rede."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from robowin.auditoria.cadeia import RegistradorAuditoria, verificar_diretorio
from robowin.core.config import carregar_toml
from robowin.painel.app import criar_app
from robowin.risco.usuarios import (
    APROVADO,
    NAO_APROVADO,
    SEM_LIMITES,
    ArmazemUsuarios,
    ChaveConta,
    modelo_inicial,
)

RAIZ = Path(__file__).resolve().parents[2]
CONTA = "mt5:CORRETORA-DEMO:111111"
TOKEN_ACOES = "segredo-acoes"


@pytest.fixture
def amb(tmp_path):
    dados = tmp_path / "dados"
    arm = ArmazemUsuarios(tmp_path / "usuarios", RegistradorAuditoria(dados / "auditoria"))
    toks = {u: arm.criar_usuario(u) for u in ("ana", "bia")}
    arm.registrar_conta("ana", ChaveConta.de_texto(CONTA))
    cfg = carregar_toml(RAIZ / "config/robowin.toml")
    app = criar_app(banco=tmp_path / "nao.sqlite", dir_dados=dados, token=TOKEN_ACOES, config=cfg,
                    dir_usuarios=tmp_path / "usuarios")
    return TestClient(app), arm, toks, dados


def _entrar(c, usuario, token):
    return c.post("/risco/entrar", data={"usuario": usuario, "token_usuario": token}, follow_redirects=False)


def _form(**kw):
    from perfis_teste import WIN
    vals = {k: str(v) for k, v in modelo_inicial(WIN).model_dump().items()}
    vals.update({k: str(v) for k, v in kw.items()})
    return {"conta": CONTA, **vals}


def _eventos(dados: Path) -> list[dict]:
    out = []
    for arq in sorted((dados / "auditoria").glob("*.jsonl")):
        out += [json.loads(ln) for ln in arq.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


def test_pagina_risco_sem_login_pede_usuario_e_token(amb):
    c, *_ = amb
    r = c.get("/risco")
    assert r.status_code == 200 and '<html lang="pt-BR">' in r.text
    assert 'action="/risco/entrar"' in r.text and "SIMULAÇÃO — sem ordens reais" in r.text
    assert 'href="/risco"' in c.get("/").text  # no menu


def test_login_com_token_errado_e_negado_e_limitado(amb):
    c, _, toks, _ = amb
    assert _entrar(c, "ana", "errado").status_code == 401
    assert _entrar(c, "ana", toks["bia"]).status_code == 401  # token de outro usuario
    assert _entrar(c, "ninguem", toks["ana"]).status_code == 401
    for _ in range(5):
        _entrar(c, "ana", "errado")
    assert _entrar(c, "ana", toks["ana"]).status_code == 429


def test_novo_usuario_ve_modelo_conservador_e_estado_sem_limites(amb):
    c, _, toks, _ = amb
    assert _entrar(c, "ana", toks["ana"]).status_code == 303
    r = c.get("/risco")
    assert SEM_LIMITES in r.text and "muito conservador" in r.text and CONTA in r.text
    assert "BLOQUEADA" in r.text and "NÃO (bloqueado)" in r.text


def test_formulario_tem_o_tempo_maximo_da_posicao_e_grava(amb):
    """Campo novo do ConfigRisco (docs/16): aparece no formulario com rotulo pt-BR e e gravado como os demais."""
    c, arm, toks, _ = amb
    _entrar(c, "ana", toks["ana"])
    r = c.get("/risco")
    assert 'name="tempo_max_posicao_s"' in r.text and "Tempo máximo de uma posição" in r.text
    assert 'value="300"' in r.text.split('name="tempo_max_posicao_s"')[1].split(">")[0]
    r = c.post("/risco/limites", data={**_form(tempo_max_posicao_s="240"), "token": TOKEN_ACOES},
               follow_redirects=False)
    assert r.status_code == 303 and arm.limites("ana", ChaveConta.de_texto(CONTA)).tempo_max_posicao_s == 240
    sem = {k: v for k, v in _form().items() if k != "tempo_max_posicao_s"}  # nada e imposto por padrao
    assert c.post("/risco/limites", data={**sem, "token": TOKEN_ACOES}).status_code == 400


def test_isolamento_cada_usuario_so_ve_as_proprias_contas(amb):
    c, arm, toks, _ = amb
    _entrar(c, "bia", toks["bia"])
    r = c.get("/risco?conta=" + quote(CONTA))
    assert CONTA not in r.text and "Nenhuma conta registrada" in r.text
    # bia tenta gravar na conta da ana: o usuario vem da sessao, entao vira a conta (inexistente) da bia
    r = c.post("/risco/limites", data={**_form(), "token": TOKEN_ACOES})
    assert r.status_code == 400 and arm.limites("ana", ChaveConta.de_texto(CONTA)) is None


def test_previa_valida_sem_gravar(amb):
    c, arm, toks, dados = amb
    _entrar(c, "ana", toks["ana"])
    n = len(_eventos(dados))
    r = c.post("/risco/previa", data=_form(perda_max_dia_brl="1"))
    assert r.status_code == 400 and "incoerentes" in r.text
    r = c.post("/risco/previa", data=_form())
    assert r.status_code == 200 and "Pré-visualização" in r.text and "Perda maxima no dia" in r.text
    assert arm.limites("ana", ChaveConta.de_texto(CONTA)) is None and len(_eventos(dados)) == n


def test_gravar_exige_sessao_e_token_de_acoes_e_audita(amb):
    c, arm, toks, dados = amb
    chave = ChaveConta.de_texto(CONTA)
    assert c.post("/risco/limites", data={**_form(), "token": TOKEN_ACOES}).status_code == 401  # sem sessao
    _entrar(c, "ana", toks["ana"])
    assert c.post("/risco/limites", data=_form()).status_code == 401  # sem token de acoes
    assert c.post("/risco/limites", data={**_form(), "token": "x"}).status_code == 401
    assert c.post("/risco/limites", data={**_form(contratos_max="3"), "token": TOKEN_ACOES}).status_code == 400
    assert arm.limites("ana", chave) is None
    r = c.post("/risco/limites", data={**_form(perda_max_dia_brl="70"), "token": TOKEN_ACOES}, follow_redirects=False)
    assert r.status_code == 303
    assert arm.limites("ana", chave).perda_max_dia_brl == 70.0
    assert arm.estado("ana", chave).estado == NAO_APROVADO
    ev = _eventos(dados)[-1]
    assert ev["tipo"] == "risco_limites_alterados" and ev["dados"]["usuario"] == "ana" and ev["dados"]["origem"] == "painel"


def test_aprovar_exige_aprovo_e_token_digitado_e_alteracao_invalida(amb):
    c, arm, toks, dados = amb
    chave = ChaveConta.de_texto(CONTA)
    _entrar(c, "ana", toks["ana"])
    c.post("/risco/limites", data={**_form(), "token": TOKEN_ACOES})
    base = {"conta": CONTA, "responsavel": "Ana Souza", "motivo": "teste demo"}
    assert c.post("/risco/aprovar", data={**base, "confirmacao": "APROVO"}).status_code == 401  # token nao digitado
    c.cookies.set("robowin_token", TOKEN_ACOES)  # cookie de acoes nao basta: precisa digitar
    assert c.post("/risco/aprovar", data={**base, "confirmacao": "APROVO"}).status_code == 401
    assert c.post("/risco/aprovar", data={**base, "confirmacao": "aprovo", "token": TOKEN_ACOES}).status_code == 400
    assert c.post("/risco/aprovar", data={**base, "responsavel": "pendente", "confirmacao": "APROVO",
                                          "token": TOKEN_ACOES}).status_code == 400
    assert arm.estado("ana", chave).estado == NAO_APROVADO
    r = c.post("/risco/aprovar", data={**base, "confirmacao": "APROVO", "token": TOKEN_ACOES}, follow_redirects=False)
    assert r.status_code == 303 and arm.estado("ana", chave).estado == APROVADO
    pg = c.get("/risco?conta=" + quote(CONTA)).text
    assert "VIGENTE" in pg and "risco-v1.toml" in pg and "risco_aprovado" in pg and "Ana Souza" in pg
    c.post("/risco/limites", data={**_form(operacoes_dia_max="2"), "token": TOKEN_ACOES})
    assert arm.estado("ana", chave).estado == NAO_APROVADO  # alterar invalida a aprovacao
    tipos = [e["tipo"] for e in _eventos(dados)]
    assert tipos.count("risco_aprovado") == 1 and tipos.count("risco_limites_alterados") == 2
    ok, msgs = verificar_diretorio(dados / "auditoria")
    assert ok, msgs


def test_registrar_conta_pelo_painel_e_conta_real_nao_aprova(amb):
    c, arm, toks, _ = amb
    _entrar(c, "bia", toks["bia"])
    dados_conta = {"plataforma": "mt5", "servidor": "CORRETORA-REAL", "login": "42", "tipo": "real"}
    assert c.post("/risco/contas", data=dados_conta).status_code == 401  # sem token de acoes
    assert c.post("/risco/contas", data={**dados_conta, "token": TOKEN_ACOES}, follow_redirects=False).status_code == 303
    real = "mt5:CORRETORA-REAL:42"
    c.post("/risco/limites", data={**_form(), "conta": real, "token": TOKEN_ACOES})
    r = c.post("/risco/aprovar", data={"conta": real, "responsavel": "Bia", "confirmacao": "APROVO", "token": TOKEN_ACOES})
    assert r.status_code == 400 and "REAL" in r.text
    assert not arm.estado("bia", ChaveConta.de_texto(real)).pode_operar_real
    assert c.post("/risco/contas", data={"plataforma": "mt9", "servidor": "x", "login": "1",
                                         "token": TOKEN_ACOES}).status_code == 400


def test_sem_token_de_acoes_configurado_nada_e_gravado(tmp_path):
    arm = ArmazemUsuarios(tmp_path / "u")
    tok = arm.criar_usuario("ana")
    arm.registrar_conta("ana", ChaveConta.de_texto(CONTA))
    c = TestClient(criar_app(banco=None, dir_dados=tmp_path / "d", token="", dir_usuarios=tmp_path / "u"))
    _entrar(c, "ana", tok)
    assert "não configurado" in c.get("/risco").text
    assert c.post("/risco/limites", data={**_form(), "token": ""}).status_code == 403
    assert arm.limites("ana", ChaveConta.de_texto(CONTA)) is None


def test_sair_encerra_a_sessao(amb):
    c, _, toks, _ = amb
    _entrar(c, "ana", toks["ana"])
    assert CONTA in c.get("/risco").text
    c.post("/risco/sair")
    assert CONTA not in c.get("/risco").text
