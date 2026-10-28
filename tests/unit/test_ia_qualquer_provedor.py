"""Qualquer IA, qualquer modelo, SEM chave e SEM rede.

* registro de provedores (config/provedores.toml): cada provedor cria o cliente certo com a base_url certa;
* bateria de contrato contra TODO provedor do registro, com transporte falso (respostas fabricadas no formato
  da API para os SDKs instalados; cliente falso no formato do SDK para os opcionais);
* modelo sem preco/corte so com declaracao explicita (teto conservador, rotulo, contaminacao);
* CLI ``ia listar | adicionar-modelo | testar``; simulacao ponta a ponta com braco de outro provedor.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from test_ia_provedores import ClienteAnthropic, ClienteGoogle, ClienteOllama, ErroHTTP, _cenario
from transporte_falso import TransporteFalso, sse_anthropic, sse_chat_completions, sse_responses

from robowin.cli import main
from robowin.core.tipos import Acao, PropostaIA, RevisaoIA
from robowin.ia.catalogo import CORTE_DESCONHECIDO, TETO_PRECO_DESCONHECIDO_USD, Catalogo, ErroCatalogo
from robowin.ia.orcamento import OrcamentoIA, ProvedorComOrcamento
from robowin.ia.provedores.anthropic import (
    ProvedorAnthropic,
    ProvedorAnthropicBedrock,
    ProvedorAnthropicVertex,
)
from robowin.ia.provedores.falso import ProvedorFalso
from robowin.ia.provedores.google import ProvedorGeminiVertex, ProvedorGoogle
from robowin.ia.provedores.ollama import ProvedorOllama
from robowin.ia.provedores.openai import ProvedorAzureOpenAI, ProvedorOpenAI
from robowin.ia.provedores.openai_compativel import ProvedorOpenAICompativel
from robowin.ia.registro_provedores import ErroRegistro, RegistroProvedores

RAIZ = Path(__file__).resolve().parents[2]
REG = RegistroProvedores.carregar(RAIZ / "config" / "provedores.toml")
OK = PropostaIA(acao=Acao.NAO_OPERAR, incerteza=0.7, justificativa_curta="sem sinal")
OK_JSON = OK.model_dump_json()
REV_JSON = RevisaoIA(veredito="REJEITAR", justificativa_curta="sem confirmacao").model_dump_json()
CHAVE_FALSA = "chave-falsa-de-teste-0123456789"

PEDIDOS = ("anthropic", "openai", "azure_openai", "google", "gemini_vertex", "aws_bedrock", "google_vertex", "mistral",
           "deepseek", "xai", "groq", "together", "fireworks", "openrouter", "perplexity", "cohere", "cerebras",
           "dashscope", "moonshot", "ollama", "lmstudio", "vllm", "llamacpp")

CLASSES = {"anthropic": ProvedorAnthropic, "anthropic_bedrock": ProvedorAnthropicBedrock,
           "anthropic_vertex": ProvedorAnthropicVertex, "openai": ProvedorOpenAI, "azure_openai": ProvedorAzureOpenAI,
           "google": ProvedorGoogle, "gemini_vertex": ProvedorGeminiVertex, "openai_compativel": ProvedorOpenAICompativel,
           "ollama": ProvedorOllama}

VALORES_FALSOS = {"AWS_REGION": "sa-east-1", "CLOUD_ML_REGION": "us-east5", "ANTHROPIC_VERTEX_PROJECT_ID": "projeto-falso",
                  "AZURE_OPENAI_ENDPOINT": "https://recurso-falso.openai.azure.com", "OPENAI_API_VERSION": "2099-01-01",
                  "GOOGLE_CLOUD_PROJECT": "projeto-falso", "GOOGLE_CLOUD_LOCATION": "us-central1",
                  "OPENAI_COMPATIVEL_BASE_URL": "http://localhost:9/v1"}

# catalogo com 'm' em TODO provedor do registro (precos para conferir o custo; locais com custo zero)
CATALOGO = Catalogo.de_dict({"modelo": [
    {"provedor": d.nome, "id": "m", "fixavel": True, "corte_conhecimento": "2026-01",
     **({"local": True} if d.local else {"preco_entrada_mtok": 1.0, "preco_saida_mtok": 4.0, "preco_cache_mtok": 0.1})}
    for d in REG]})


def env_falso(nome: str) -> dict[str, str]:
    """Ambiente FICTICIO (um dicionario, nunca os.environ) com chave e variaveis exigidas do provedor."""
    d = REG.obter(nome)
    env = {v: VALORES_FALSOS.get(v, "x") for v in d.exige_env}
    chaves = [v for v in d.chave_env if v != "GOOGLE_APPLICATION_CREDENTIALS"]
    if chaves:
        env[chaves[0]] = CHAVE_FALSA
    return env


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Qualquer conexao para fora da maquina derruba o teste (loopback continua livre: no Windows o asyncio cria
    o par de sockets interno conectando em 127.0.0.1)."""
    connect_original = socket.socket.connect

    def _loopback(endereco) -> bool:
        return isinstance(endereco, tuple) and str(endereco[0]) in ("127.0.0.1", "::1", "localhost")

    def connect(self, endereco):
        if not _loopback(endereco):
            raise AssertionError(f"tentativa de conexao de rede em teste: {endereco}")
        return connect_original(self, endereco)

    def create_connection(endereco, *a, **k):
        raise AssertionError(f"tentativa de conexao de rede em teste: {endereco}")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)


def test_nenhuma_variavel_de_chave_no_ambiente_dos_testes():
    """tests/conftest.py limpa *_API_KEY e as credenciais do registro antes de cada teste."""
    assert not [v for v in os.environ if v.endswith("_API_KEY")]
    for d in REG:
        assert not any(os.environ.get(v) for v in (*d.chave_env, *d.exige_env)), d.nome
        if d.chave_obrigatoria and d.chave_env:
            assert not d.chave_presente(), d.nome


# ================================================================================ registro ====
def test_registro_tem_todos_os_provedores_pedidos_e_adaptadores_validos():
    assert set(PEDIDOS) <= set(REG.nomes())
    for d in REG:
        assert d.adaptador in CLASSES, d.nome
        assert ":" not in d.nome
        if d.verificado:
            assert d.fonte_verificacao, f"{d.nome}: verificado=true exige fonte_verificacao"
        if d.base_url.startswith("http://"):
            assert d.local, f"{d.nome}: http sem TLS so local"
    assert {d.nome for d in REG if d.local} >= {"ollama", "lmstudio", "vllm", "llamacpp"}
    assert REG.obter("aws_bedrock").adaptador == "anthropic_bedrock"
    assert REG.obter("google_vertex").adaptador == "anthropic_vertex"  # Claude no Vertex


def test_registro_recusa_entradas_invalidas():
    casos = [({"x": {"adaptador": "inexistente"}}, "adaptador invalido"),
             ({"x": {"adaptador": "openai_compativel"}}, "exige base_url"),
             ({"x": {"adaptador": "openai_compativel", "base_url": "http://remoto.exemplo/v1"}}, "so para servidor local"),
             ({"x": {"adaptador": "openai_compativel", "base_url": "ftp://a"}}, "http"),
             ({"x": {"adaptador": "openai", "chave": "sk-valor"}}, "campos desconhecidos"),
             ({"a:b": {"adaptador": "openai"}}, "nome de provedor invalido"),
             ({"x": {"adaptador": "openai_compativel", "base_url": "https://a", "saida_estruturada": "xml"}}, "saida")]
    for provs, msg in casos:
        with pytest.raises(ErroRegistro, match=msg):
            RegistroProvedores.de_dict({"provedor": provs})
    with pytest.raises(ErroRegistro, match="nao registrado"):
        REG.obter("inexistente")


def test_registro_nunca_guarda_valor_de_chave():
    texto = (RAIZ / "config" / "provedores.toml").read_text(encoding="utf-8")
    assert not re.search(r"(sk-|AIza|xai-|gsk_)[A-Za-z0-9]{8,}", texto)
    for d in REG:
        assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", v) for v in d.chave_env), d.nome


@pytest.mark.parametrize("nome", REG.nomes())
def test_provedor_do_registro_cria_o_cliente_certo_com_a_base_url_certa(nome):
    d = REG.obter(nome)
    env = env_falso(nome)
    assert d.chave_presente(env)
    if d.chave_obrigatoria and (d.chave_env or d.exige_env):
        assert not d.chave_presente({}) and d.variaveis_ausentes({})
    p = REG.criar(nome, catalogo=CATALOGO, env=env)
    assert type(p) is CLASSES[d.adaptador] and p.nome == nome
    params = p.parametros_cliente()
    url = d.url_efetiva(env)
    if d.adaptador in ("anthropic", "openai", "openai_compativel"):
        assert params["base_url"] == url and params["max_retries"] == 0
    elif d.adaptador == "ollama":
        assert params["host"] == url
    elif d.adaptador == "azure_openai":
        assert params["azure_endpoint"] == env["AZURE_OPENAI_ENDPOINT"] and params["api_version"] == "2099-01-01"
    elif d.adaptador == "anthropic_bedrock":
        assert params["aws_region"] == "sa-east-1" and params["api_key"] == CHAVE_FALSA
    elif d.adaptador == "anthropic_vertex":
        assert params["region"] == "us-east5" and params["project_id"] == "projeto-falso"
    elif d.adaptador == "gemini_vertex":
        assert params["vertexai"] is True and params.get("api_key") == CHAVE_FALSA
    elif d.adaptador == "google":
        assert params["api_key"] == CHAVE_FALSA
    sdk = {"anthropic": "anthropic", "anthropic_bedrock": "anthropic", "anthropic_vertex": "anthropic",
           "openai": "openai", "azure_openai": "openai", "openai_compativel": "openai"}.get(d.adaptador)
    if sdk is None or importlib.util.find_spec(sdk) is None:
        return  # SDK opcional ausente (google-genai, ollama): basta conferir os argumentos do construtor
    cli = p._get_cliente()  # instancia o SDK real: nenhuma conexao (o teste falha se houver)
    esperado = {"anthropic_bedrock": "https://bedrock-runtime.sa-east-1.amazonaws.com",
                "anthropic_vertex": "https://us-east5-aiplatform.googleapis.com/v1",
                "azure_openai": "https://recurso-falso.openai.azure.com/openai"}.get(d.adaptador, url)
    assert str(cli.base_url).rstrip("/") == esperado.rstrip("/")
    assert cli.max_retries == 0


def test_provedor_novo_e_so_uma_entrada_no_registro():
    reg = RegistroProvedores.de_dict({"provedor": {"meu_provedor": {
        "adaptador": "openai_compativel", "base_url": "https://api.meu-provedor.exemplo/v1",
        "chave_env": ["MEU_PROVEDOR_API_KEY"], "saida_estruturada": "json_schema"}}})
    cat = Catalogo.de_dict({"modelo": [{"provedor": "meu_provedor", "id": "modelo-x", "fixavel": False,
                                        "corte_conhecimento": "2026-01", "preco_entrada_mtok": 1.0,
                                        "preco_saida_mtok": 2.0}]})
    t = TransporteFalso(sse_chat_completions(OK_JSON, modelo="modelo-x"))
    p = reg.criar("meu_provedor", catalogo=cat, env={"MEU_PROVEDOR_API_KEY": CHAVE_FALSA}, http_client=t.cliente())
    obj, ch = asyncio.run(p.gerar_estruturado(modelo="modelo-x", sistema="s", mensagem="m", schema=PropostaIA, prazo_s=5))
    assert ch.valida and isinstance(obj, PropostaIA) and ch.modelo_pedido == "meu_provedor:modelo-x"
    assert str(t.ultima.url) == "https://api.meu-provedor.exemplo/v1/chat/completions"
    assert t.ultima.headers["authorization"] == f"Bearer {CHAVE_FALSA}"
    assert t.corpo_json()["response_format"]["type"] == "json_schema" and ch.custo_usd > 0


# ================================================================== contrato (transporte falso) ====
HTTP = {"anthropic", "anthropic_vertex", "openai", "azure_openai", "openai_compativel"}
SEM_RECUSA = {"ollama"}


def _corpo(adaptador: str, cen: dict) -> bytes:
    texto, servido = cen["texto"], cen["servido"]
    if adaptador in ("anthropic", "anthropic_vertex"):
        parada = "refusal" if cen["recusa"] else ("max_tokens" if cen["stop"] == "max_tokens" else "end_turn")
        return sse_anthropic(texto, modelo=servido, parada=parada)
    if adaptador in ("openai", "azure_openai"):
        return sse_responses(texto, modelo=servido, recusa=cen["recusa"],
                             incompleto="max_output_tokens" if cen["stop"] == "max_tokens" else None)
    return sse_chat_completions(texto, modelo=servido, recusa=cen["recusa"],
                                fim="length" if cen["stop"] == "max_tokens" else "stop")


def _provedor(nome: str, **kw):
    """Provedor do registro com transporte falso: HTTP fabricado (SDK real) ou cliente falso (SDK opcional)."""
    d = REG.obter(nome)
    cen = _cenario(**kw)
    if d.adaptador in HTTP:
        t = TransporteFalso(_corpo(d.adaptador, cen), status=cen["erro"].status_code if cen["erro"] else 200,
                            atraso_s=cen["demora"])
        return REG.criar(nome, catalogo=CATALOGO, env=env_falso(nome), http_client=t.cliente()), t
    cliente = {"anthropic_bedrock": ClienteAnthropic, "google": ClienteGoogle, "gemini_vertex": ClienteGoogle,
               "ollama": ClienteOllama}[d.adaptador](cen)
    return REG.criar(nome, catalogo=CATALOGO, env=env_falso(nome), cliente=cliente), cliente


async def _chamar(p, prazo=5.0):
    return await p.gerar_estruturado(modelo="m", sistema="sis", mensagem="msg", schema=PropostaIA, prazo_s=prazo,
                                     effort="low", max_tokens=1000)


NOMES = REG.nomes()


@pytest.mark.parametrize("nome", NOMES)
async def test_contrato_ok_custo_e_nome_do_registro(nome):
    p, _ = _provedor(nome)
    obj, ch = await _chamar(p)
    assert ch.valida and ch.erro is None, ch.erro
    assert isinstance(obj, PropostaIA) and obj.acao == Acao.NAO_OPERAR
    assert ch.modelo_pedido == f"{nome}:m" and ch.modelo_servido == "m"
    assert ch.tokens_saida > 0 and ch.total_ms >= ch.ttft_ms >= 0
    assert (ch.custo_usd == 0.0) if REG.obter(nome).local else (ch.custo_usd > 0)


@pytest.mark.parametrize("nome", NOMES)
async def test_contrato_timeout_imposto(nome):
    p, _ = _provedor(nome, demora=30.0)
    t0 = time.monotonic()
    obj, ch = await _chamar(p, prazo=0.05)
    assert time.monotonic() - t0 < 2.0 and obj is None and ch.erro == "timeout"


@pytest.mark.parametrize("nome", NOMES)
async def test_contrato_http_429_vira_resposta_invalida(nome):
    p, _ = _provedor(nome, erro=ErroHTTP(429))
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "http_429"


@pytest.mark.parametrize("nome", [n for n in NOMES if REG.obter(n).adaptador not in SEM_RECUSA])
async def test_contrato_recusa(nome):
    p, _ = _provedor(nome, recusa=True, texto="")
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "recusa" and ch.categoria_recusa


@pytest.mark.parametrize("nome", NOMES)
async def test_contrato_max_tokens(nome):
    p, _ = _provedor(nome, stop="max_tokens", texto='{"acao": "COMPR')
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "max_tokens"


@pytest.mark.parametrize("nome", NOMES)
async def test_contrato_json_invalido(nome):
    p, _ = _provedor(nome, texto="isto nao e json", parse_ok=False)
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "json_invalido"


@pytest.mark.parametrize("nome", NOMES)
async def test_contrato_modelo_servido_trocado(nome):
    p, _ = _provedor(nome, servido="outro-modelo-trocado")
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro.startswith("modelo_servido_fora_da_lista")


@pytest.mark.parametrize("nome", [n for n in NOMES if REG.obter(n).adaptador == "openai_compativel"])
async def test_compativel_requisicao_vai_ao_endpoint_do_registro(nome):
    d = REG.obter(nome)
    p, t = _provedor(nome)
    await _chamar(p)
    assert str(t.ultima.url) == d.url_efetiva(env_falso(nome)).rstrip("/") + "/chat/completions"
    chave = env_falso(nome).get(d.chave_env[0]) if d.chave_env else None
    assert t.ultima.headers["authorization"] == f"Bearer {chave or 'sem-chave'}"
    corpo = t.corpo_json()
    assert corpo["model"] == "m" and corpo["stream"] is True
    assert ("reasoning_effort" in corpo) == d.envia_effort  # parametro extra so quando o registro permite
    modo = d.saida_estruturada or "json"
    rf = corpo.get("response_format")
    if modo == "json_schema":
        assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] == PropostaIA.model_json_schema()
    elif modo == "json":
        assert rf == {"type": "json_object"} and "schema" in corpo["messages"][0]["content"]
    else:
        assert rf is None and "schema" in corpo["messages"][0]["content"]


async def test_anthropic_e_vertex_por_http_vao_ao_endpoint_do_sdk():
    p, t = _provedor("anthropic")
    await _chamar(p)
    assert str(t.ultima.url) == "https://api.anthropic.com/v1/messages"
    h = t.ultima.headers
    assert CHAVE_FALSA in (h.get("x-api-key", ""), h.get("authorization", "").removeprefix("Bearer "))
    assert "temperature" not in t.corpo_json() and t.corpo_json()["output_config"]["format"]["type"] == "json_schema"
    p, t = _provedor("google_vertex")
    await _chamar(p)
    assert str(t.ultima.url).startswith(
        "https://us-east5-aiplatform.googleapis.com/v1/projects/projeto-falso/locations/us-east5/publishers/anthropic/models/m:")
    assert t.ultima.headers["authorization"] == f"Bearer {CHAVE_FALSA}"


async def test_openai_e_azure_por_http():
    p, t = _provedor("openai")
    await _chamar(p)
    assert str(t.ultima.url) == "https://api.openai.com/v1/responses" and t.corpo_json()["store"] is False
    p, t = _provedor("azure_openai")
    await _chamar(p)
    assert str(t.ultima.url) == "https://recurso-falso.openai.azure.com/openai/responses?api-version=2099-01-01"
    assert t.ultima.headers["api-key"] == CHAVE_FALSA


async def test_chave_nunca_aparece_na_chamada_registrada():
    for nome in ("deepseek", "anthropic", "openai"):
        p, _ = _provedor(nome, erro=ErroHTTP(401))
        _, ch = await _chamar(p)
        assert CHAVE_FALSA not in ch.model_dump_json()


# ============================================================== qualquer modelo (declaracoes) ====
BASE = {"provedor": "deepseek", "id": "novo", "fixavel": False}


def test_modelo_sem_preco_ou_corte_sem_declaracao_continua_recusado():
    with pytest.raises(ErroCatalogo, match="--preco-desconhecido"):
        Catalogo.de_dict({"modelo": [{**BASE, "corte_conhecimento": "2026-01"}]})
    with pytest.raises(ErroCatalogo, match="sem data de corte"):
        Catalogo.de_dict({"modelo": [{**BASE, "preco_entrada_mtok": 1.0, "preco_saida_mtok": 1.0}]})
    with pytest.raises(ErroCatalogo, match="ambiguo"):
        Catalogo.de_dict({"modelo": [{**BASE, "preco_desconhecido": True, "preco_entrada_mtok": 1.0,
                                      "corte_conhecimento": "2026-01"}]})
    with pytest.raises(ErroCatalogo, match="ambiguo"):
        Catalogo.de_dict({"modelo": [{**BASE, "preco_entrada_mtok": 1.0, "preco_saida_mtok": 1.0,
                                      "corte_desconhecido": True, "corte_conhecimento": "2026-01"}]})


def test_preco_desconhecido_cobra_teto_conservador_e_rotula():
    cat = Catalogo.de_dict({"modelo": [{**BASE, "preco_desconhecido": True, "corte_conhecimento": "2026-01"},
                                       {**BASE, "id": "teto", "preco_desconhecido": True, "teto_chamada_usd": 0.3,
                                        "corte_conhecimento": "2026-01"}]})
    assert cat.pior_caso("deepseek:novo", 10, 10) == TETO_PRECO_DESCONHECIDO_USD
    assert cat.custo_tokens("deepseek:novo", 1, 0, 1) == TETO_PRECO_DESCONHECIDO_USD
    assert cat.custo_tokens("deepseek:novo", 0, 0, 0) == 0.0
    assert cat.teto_chamada("deepseek:teto") == 0.3
    assert cat.rotulos("deepseek:novo") == ("preco_desconhecido",)
    # o orcamento reserva e confirma o teto: 3 chamadas de US$ 1 esgotam um teto diario de US$ 2,5
    orc = OrcamentoIA(2.5 * 21, pregoes_no_mes=21)
    prov = ProvedorComOrcamento(ProvedorFalso(nome="deepseek", catalogo=cat), orc, cat)
    r = [asyncio.run(prov.gerar_estruturado(modelo="novo", sistema="s", mensagem="m", schema=PropostaIA, prazo_s=5))
         for _ in range(3)]
    assert [ch.valida for _, ch in r] == [True, True, False] and r[2][1].erro == "orcamento_esgotado:ritmo_pregao"
    assert orc.gasto_hoje == pytest.approx(2 * TETO_PRECO_DESCONHECIDO_USD)


def test_corte_desconhecido_contamina_qualquer_backtest_historico():
    cat = Catalogo.de_dict({"modelo": [{**BASE, "preco_entrada_mtok": 1.0, "preco_saida_mtok": 1.0,
                                        "corte_desconhecido": True},
                                       {**BASE, "id": "datado", "preco_entrada_mtok": 1.0, "preco_saida_mtok": 1.0,
                                        "corte_conhecimento": "2026-03"}]})
    assert cat.obter("deepseek:novo").corte_conhecimento == CORTE_DESCONHECIDO
    assert cat.rotulos("deepseek:novo") == ("corte_desconhecido:so_avaliacao_prospectiva",)
    assert cat.contaminado_em_historico(["deepseek:novo"])
    assert cat.contaminado_em_historico(["deepseek:novo"], inicio_dados="2099-01-01")  # qualquer data historica
    assert cat.fronteira_contaminacao(["deepseek:novo", "deepseek:datado"]) == CORTE_DESCONHECIDO
    assert cat.contaminado_em_historico(["deepseek:datado"], inicio_dados="2026-02-01")
    assert not cat.contaminado_em_historico(["deepseek:datado"], inicio_dados="2026-04-01")


def test_modelo_local_sem_preco_tem_custo_zero():
    cat = Catalogo.de_dict({"modelo": [{"provedor": "ollama", "id": "llama:8b", "fixavel": True, "local": True,
                                        "corte_conhecimento": "2025-06"}]})
    assert cat.pior_caso("ollama:llama:8b", 1000, 1000) == 0.0 and cat.rotulos("ollama:llama:8b") == ()


# ======================================================================================== CLI ====
@pytest.fixture
def catalogo_tmp(tmp_path):
    c = tmp_path / "modelos.toml"
    c.write_text((RAIZ / "config" / "modelos.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return c


def test_cli_adicionar_modelo_com_preco_e_corte(catalogo_tmp, capsys):
    rc = main(["ia", "adicionar-modelo", "deepseek:deepseek-chat", "--preco-entrada", "0.3", "--preco-saida", "1.2",
               "--corte", "2025-07-01", "--catalogo", str(catalogo_tmp)])
    out = capsys.readouterr().out
    assert rc == 0 and "adicionado" in out and "DEEPSEEK_API_KEY" in out and "setx" in out
    e = Catalogo.carregar(catalogo_tmp).obter("deepseek:deepseek-chat")
    assert e.preco_saida_mtok == 1.2 and e.corte_conhecimento == "2025-07-01" and not e.corte_desconhecido
    assert b"\r\n" not in catalogo_tmp.read_bytes()
    assert Catalogo.carregar(catalogo_tmp).obter("anthropic:claude-opus-5-5")  # o resto do catalogo intacto
    assert main(["ia", "adicionar-modelo", "deepseek:deepseek-chat", "--preco-entrada", "1", "--preco-saida", "1",
                 "--corte", "2025-07-01", "--catalogo", str(catalogo_tmp)]) == 2  # duplicado


def test_cli_adicionar_modelo_sem_preco_exige_declaracao(catalogo_tmp, capsys):
    antes = catalogo_tmp.read_text(encoding="utf-8")
    assert main(["ia", "adicionar-modelo", "xai:grok-x", "--corte", "2025-01-01", "--catalogo", str(catalogo_tmp)]) == 2
    assert "--preco-desconhecido" in capsys.readouterr().err
    assert main(["ia", "adicionar-modelo", "xai:grok-x", "--preco-entrada", "1", "--catalogo", str(catalogo_tmp)]) == 2
    assert main(["ia", "adicionar-modelo", "naoexiste:m", "--preco-desconhecido", "--catalogo", str(catalogo_tmp)]) == 2
    assert "provedores.toml" in capsys.readouterr().err
    assert catalogo_tmp.read_text(encoding="utf-8") == antes  # nada gravado
    rc = main(["ia", "adicionar-modelo", "xai:grok-x", "--preco-desconhecido", "--teto-chamada", "0.4",
               "--catalogo", str(catalogo_tmp)])
    out = capsys.readouterr().out
    assert rc == 0 and "CONTAMINADO" in out and "teto conservador" in out and "0.40" in out
    cat = Catalogo.carregar(catalogo_tmp)
    assert cat.rotulos("xai:grok-x") == ("preco_desconhecido", "corte_desconhecido:so_avaliacao_prospectiva")
    assert cat.pior_caso("xai:grok-x", 99999, 99999) == 0.4


def test_cli_adicionar_modelo_local_nao_precisa_de_preco(catalogo_tmp):
    assert main(["ia", "adicionar-modelo", "ollama:llama3.1:8b", "--corte", "2023-12-01",
                 "--catalogo", str(catalogo_tmp)]) == 0
    e = Catalogo.carregar(catalogo_tmp).obter("ollama:llama3.1:8b")
    assert e.local and e.preco_entrada_mtok == 0.0


def test_cli_listar_mostra_presente_ausente_sem_valor(catalogo_tmp, capsys, monkeypatch):
    segredo = "sk-deepseek-SEGREDO-que-nunca-aparece-42"
    monkeypatch.setenv("DEEPSEEK_API_KEY", segredo)
    assert main(["ia", "listar", "--catalogo", str(catalogo_tmp)]) == 0
    out = capsys.readouterr().out
    assert segredo not in out and "deepseek" in out and "anthropic:claude-opus-5-5" in out
    linha = next(ln for ln in out.splitlines() if ln.strip().startswith("deepseek "))
    assert "chave=presente" in linha
    assert "chave=ausente" in next(ln for ln in out.splitlines() if ln.strip().startswith("anthropic "))
    assert main(["ia", "listar", "--catalogo", str(catalogo_tmp), "--json"]) == 0
    dados = json.loads(capsys.readouterr().out)
    por_nome = {p["nome"]: p for p in dados["provedores"]}
    assert set(PEDIDOS) <= set(por_nome) and por_nome["deepseek"]["chave"] == "presente"
    assert por_nome["openai"]["chave"] == "ausente" and por_nome["ollama"]["chave"] == "nao_exigida"
    assert segredo not in json.dumps(dados)


def test_cli_testar_outro_provedor_sem_chave_falha_claro(catalogo_tmp, capsys):
    main(["ia", "adicionar-modelo", "mistral:mistral-x", "--preco-entrada", "1", "--preco-saida", "3",
          "--corte", "2025-01-01", "--catalogo", str(catalogo_tmp)])
    capsys.readouterr()
    rc = main(["ia", "testar", "--modelo", "mistral:mistral-x", "--k", "1", "--catalogo", str(catalogo_tmp)])
    err = capsys.readouterr().err
    assert rc == 2 and "MISTRAL_API_KEY" in err and "setx" in err


# ===================================================== simulacao ponta a ponta (outro provedor) ====
def _cfg_com_braco_deepseek(tmp_path: Path) -> tuple[Path, Path]:
    cfg = tmp_path / "robowin.toml"
    cfg.write_text((RAIZ / "config" / "robowin.toml").read_text(encoding="utf-8") + (
        '\n[[bracos]]\nid = "D_deepseek"\nproponente = "deepseek:deepseek-novo"\nmodo = "imediato"\n'
        '\n[[bracos]]\nid = "D_deepseek_rev"\nproponente = "deepseek:deepseek-novo"\nrevisor = "revisor"\n'
        'modo = "imediato"\n'), encoding="utf-8")
    cat = tmp_path / "modelos.toml"
    cat.write_text((RAIZ / "config" / "modelos.toml").read_text(encoding="utf-8") + (
        '\n[[modelo]]\nprovedor = "deepseek"\nid = "deepseek-novo"\nfixavel = false\npreco_desconhecido = true\n'
        'teto_chamada_usd = 0.01\ncorte_desconhecido = true\n'), encoding="utf-8")
    return cfg, cat


def _resposta_por_schema(adaptador: str):
    def fabricar(req: httpx2.Request) -> bytes:
        texto = REV_JSON if "veredito" in req.content.decode("utf-8") else OK_JSON
        modelo = json.loads(req.content.decode("utf-8"))["model"]
        if adaptador == "anthropic":
            return sse_anthropic(texto, modelo=modelo)
        if adaptador == "openai":
            return sse_responses(texto, modelo=modelo)
        return sse_chat_completions(texto, modelo=modelo)
    return fabricar


def test_simulacao_real_ponta_a_ponta_com_braco_de_outro_provedor_por_transporte_falso(tmp_path, monkeypatch):
    from robowin.decisao.simulacao import ParamsSimulacao, executar_sessao, montar_sessao
    cfg, cat = _cfg_com_braco_deepseek(tmp_path)
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(v, CHAVE_FALSA)  # chaves FICTICIAS: o transporte e falso, nada sai da maquina
    transportes: dict[str, TransporteFalso] = {}
    criar_original = RegistroProvedores.criar

    def criar_com_transporte(self, nome, **kw):
        t = transportes.setdefault(nome, TransporteFalso(_resposta_por_schema(self.obter(nome).adaptador)))
        return criar_original(self, nome, http_client=t.cliente(), **kw)
    monkeypatch.setattr(RegistroProvedores, "criar", criar_com_transporte)
    p = ParamsSimulacao(seed=3, n_ticks=2200, saida=tmp_path / "s", com_condicional=False, provedor="real",
                        config=cfg, catalogo=cat)
    s = montar_sessao(p)
    try:
        asyncio.run(executar_sessao(s))
    finally:
        s.banco.fechar()
    assert set(transportes) == {"anthropic", "openai", "deepseek"}
    assert all(str(r.url) == "https://api.deepseek.com/chat/completions" for r in transportes["deepseek"].requisicoes)
    assert transportes["deepseek"].requisicoes
    m = s.manifesto
    assert m.modelos["D_deepseek.proponente"] == "deepseek:deepseek-novo"
    assert m.modelos["D_deepseek_rev.revisor"] == "openai:gpt-6.1-sol"
    assert m.hashes["rotulo:deepseek:deepseek-novo"] == "preco_desconhecido,corte_desconhecido:so_avaliacao_prospectiva"
    assert m.hashes["contaminacao:D_deepseek"] == "corte_desconhecido:so_avaliacao_prospectiva"
    c = sqlite3.connect(tmp_path / "s" / "robowin.db")
    linhas = c.execute("SELECT braco, modelo_pedido, valida, custo_usd FROM chamadas_llm "
                       "WHERE modelo_pedido LIKE 'deepseek:%'").fetchall()
    assert linhas and {b for b, *_ in linhas} >= {"D_deepseek"}
    assert all(v for _, _, v, _ in linhas) and all(custo == pytest.approx(0.01) for *_, custo in linhas)
    assert {r[0] for r in c.execute("SELECT DISTINCT braco FROM decisoes")} >= {"D_deepseek", "D_deepseek_rev"}


def test_simulacao_real_sem_chave_recusa_claro_nomeando_a_variavel(tmp_path, capsys):
    cfg, cat = _cfg_com_braco_deepseek(tmp_path)
    rc = main(["simular", "--n-ticks", "1500", "--saida", str(tmp_path / "x"), "--provedor", "real",
               "--config", str(cfg), "--catalogo", str(cat)])
    out = capsys.readouterr().out
    assert rc == 2 and "sem chave" in out and "setx" in out and "--provedor falso" in out
    assert re.search(r"ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|OPENAI_API_KEY", out)


def test_simulacao_falsa_com_braco_de_outro_provedor_sem_chave(tmp_path):
    from robowin.decisao.simulacao import ParamsSimulacao, executar_sessao, montar_sessao
    cfg, cat = _cfg_com_braco_deepseek(tmp_path)
    s = montar_sessao(ParamsSimulacao(seed=4, n_ticks=2200, saida=tmp_path / "f", com_condicional=False,
                                      config=cfg, catalogo=cat))
    try:
        asyncio.run(executar_sessao(s))
    finally:
        s.banco.fechar()
    c = sqlite3.connect(tmp_path / "f" / "robowin.db")
    servidos = {r[0] for r in c.execute("SELECT modelo_servido FROM chamadas_llm WHERE modelo_pedido LIKE 'deepseek:%'")}
    assert servidos and all(x == "" or x.endswith("-FALSO") for x in servidos)


def test_papeis_aceitam_qualquer_provedor_do_catalogo():
    from robowin.decisao.bracos.base import Geometria
    from robowin.decisao.bracos.fabrica import FabricaBracos
    from robowin.ia.papeis import carregar_papeis
    cfg = {"ia": {"papeis": {"proponente": {"modelo": "deepseek:m"}, "revisor": {"modelo": "mistral:m"},
                             "classificador_nlp": {"modelo": "ollama:m"}, "meu_papel_novo": {"modelo": "groq:m"}}},
           "bracos": [{"id": "X_novo", "proponente": "meu_papel_novo", "revisor": "openrouter:m"}]}
    papeis = carregar_papeis(cfg)
    provs = {n: SimpleNamespace(nome=n) for n in ("deepseek", "mistral", "ollama", "groq", "openrouter")}
    fab = FabricaBracos(provs, catalogo=CATALOGO, papeis=papeis, prazos=None, cfg={}, stop_max_pontos=100.0,
                        geometria=Geometria(50.0, 100.0, 300))
    b = fab.padrao(("imediato",))
    assert [x.manifesto()["proponente"]["modelo"] for x in b] == ["deepseek:m", "deepseek:m"]
    assert b[1].manifesto()["revisor"]["modelo"] == "mistral:m"
    extra = fab.extras(cfg)[0]
    assert extra.manifesto()["proponente"]["modelo"] == "groq:m"
    assert extra.manifesto()["revisor"]["modelo"] == "openrouter:m"


def test_exemplos_comentados_de_bracos_em_robowin_toml():
    texto = (RAIZ / "config" / "robowin.toml").read_text(encoding="utf-8")
    import tomllib
    assert "bracos" not in tomllib.loads(texto)  # desligados por padrao
    for prov in ("google:", "deepseek:", "xai:", "mistral:", "ollama:", "openrouter:"):
        assert re.search(rf"^# *proponente = \"{prov}", texto, re.M), prov
    descomentado = "\n".join(ln[2:] if ln.startswith("# ") and re.match(r"# *(\[\[bracos\]\]|id =|proponente =|revisor =|modo =)", ln)
                             else ln for ln in texto.splitlines())
    assert len(tomllib.loads(descomentado).get("bracos", [])) >= 6
