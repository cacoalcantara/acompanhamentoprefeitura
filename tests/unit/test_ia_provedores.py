"""Contrato comum dos provedores, rodando contra cada adaptador com transporte (cliente) FALSO. Sem rede."""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from robowin.core.tipos import Acao, ClassificacaoNoticia, PropostaIA
from robowin.ia.catalogo import Catalogo
from robowin.ia.provedores import criar_provedor
from robowin.ia.provedores.anthropic import ProvedorAnthropic
from robowin.ia.provedores.base import ErroProvedor, PacoteAusente
from robowin.ia.provedores.falso import ProvedorFalso
from robowin.ia.provedores.google import ProvedorGoogle
from robowin.ia.provedores.ollama import ProvedorOllama
from robowin.ia.provedores.openai import ProvedorOpenAI
from robowin.ia.provedores.openai_compativel import ProvedorOpenAICompativel

CHAVE_SECRETA = "sk-SEGREDO-NAO-VAZAR-123456"
OK = PropostaIA(acao=Acao.NAO_OPERAR, incerteza=0.7, justificativa_curta="sem sinal")
OK_JSON = OK.model_dump_json()

CATALOGO = Catalogo.de_dict({"modelo": [
    {"provedor": "anthropic", "id": "m", "fixavel": True, "corte_conhecimento": "2026-06",
     "preco_entrada_mtok": 4.0, "preco_saida_mtok": 20.0, "preco_cache_mtok": 0.2},
    {"provedor": "openai", "id": "m", "fixavel": False, "corte_conhecimento": "2026-04",
     "preco_entrada_mtok": 2.0, "preco_saida_mtok": 10.0, "preco_cache_mtok": 0.1},
    {"provedor": "google", "id": "m", "fixavel": True, "corte_conhecimento": "2026-01",
     "preco_entrada_mtok": 1.0, "preco_saida_mtok": 5.0},
    {"provedor": "openai_compativel", "id": "m", "fixavel": True, "corte_conhecimento": "2025-12",
     "preco_entrada_mtok": 1.0, "preco_saida_mtok": 1.0, "saida_estruturada": "json_schema"},
    {"provedor": "ollama", "id": "m", "fixavel": True, "corte_conhecimento": "2025-10",
     "preco_entrada_mtok": 0.0, "preco_saida_mtok": 0.0},
]})


class ErroHTTP(Exception):
    def __init__(self, status: int, msg: str = "erro"):
        super().__init__(msg)
        self.status_code = status


def _cenario(**kw):
    base = {"texto": OK_JSON, "stop": "end_turn", "recusa": False, "servido": "m", "demora": 0.0,
            "erro": None, "parse_ok": True}
    return {**base, "stop_ok": True, **kw}


async def _pre(c):
    if c["demora"]:
        await asyncio.sleep(c["demora"])
    if c["erro"]:
        raise c["erro"]


# ---------- clientes falsos, um por SDK ----------
class _CtxAsync:
    def __init__(self, c, eventos, final):
        self.c, self.eventos, self._final = c, eventos, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def it():
            await _pre(self.c)
            for e in self.eventos:
                yield e
        return it()


class ClienteAnthropic:
    def __init__(self, c):
        self.c, self.kw = c, {}
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        self.kw = kw
        c = self.c
        ctx = _CtxAsync(c, [SimpleNamespace(type="message_start"), SimpleNamespace(type="text")], None)
        parsed = None  # o adaptador manda o schema em output_config.format e valida o texto na camada comum
        msg = SimpleNamespace(
            model=c["servido"], _request_id="req_123", stop_reason="refusal" if c["recusa"] else c["stop"],
            stop_details=SimpleNamespace(category="reasoning_extraction") if c["recusa"] else None,
            content=[SimpleNamespace(type="text", text=c["texto"])], parsed_output=parsed,
            usage=SimpleNamespace(input_tokens=1000, cache_read_input_tokens=5000, output_tokens=500))

        async def final():
            return msg
        ctx.get_final_message = final
        return ctx


class ClienteOpenAI:
    def __init__(self, c):
        self.c, self.kw = c, {}
        self.responses = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        self.kw = kw
        c = self.c
        ctx = _CtxAsync(c, [SimpleNamespace(type="response.created"), SimpleNamespace(type="response.output_text.delta")], None)
        item = SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="nao posso")] if c["recusa"]
                               else [SimpleNamespace(type="output_text", text=c["texto"])])
        inc = SimpleNamespace(reason="max_output_tokens") if c["stop"] == "max_tokens" else None
        parsed = None  # o adaptador manda o schema em text.format e valida o texto na camada comum
        resp = SimpleNamespace(
            model=c["servido"], id="resp_1", status="incomplete" if inc else "completed", incomplete_details=inc,
            output=[item], output_parsed=parsed,
            usage=SimpleNamespace(input_tokens=6000, input_tokens_details=SimpleNamespace(cached_tokens=5000),
                                  output_tokens=500))

        async def final():
            return resp
        ctx.get_final_response = final
        return ctx


class _Iter:
    def __init__(self, c, chunks):
        self.c, self.chunks = c, chunks

    def __aiter__(self):
        async def it():
            await _pre(self.c)
            for ch in self.chunks:
                yield ch
        return it()


class ClienteCompativel:
    def __init__(self, c):
        self.c, self.kw = c, {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.kw = kw
        c = self.c
        ch1 = SimpleNamespace(model=c["servido"], id="cmpl_1", usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None if c["recusa"] else c["texto"], refusal="nao" if c["recusa"] else None),
            finish_reason=None)])
        fim = {"max_tokens": "length"}.get(c["stop"], "stop")
        ch2 = SimpleNamespace(model=c["servido"], id="cmpl_1", choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, refusal=None), finish_reason=fim)],
            usage=SimpleNamespace(prompt_tokens=6000, completion_tokens=500,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=5000)))
        return _Iter(c, [ch1, ch2])


class ClienteGoogle:
    def __init__(self, c):
        self.c, self.kw = c, {}
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._gen))

    async def _gen(self, **kw):
        self.kw = kw
        c = self.c
        fim = SimpleNamespace(name="MAX_TOKENS" if c["stop"] == "max_tokens" else ("SAFETY" if c["recusa"] else "STOP"))
        ch = SimpleNamespace(
            text=None if c["recusa"] else c["texto"], model_version=c["servido"], response_id="g1",
            usage_metadata=SimpleNamespace(prompt_token_count=6000, cached_content_token_count=5000,
                                           candidates_token_count=400, thoughts_token_count=100),
            prompt_feedback=None, candidates=[SimpleNamespace(finish_reason=fim)])
        return _Iter(c, [ch])


class ClienteOllama:
    def __init__(self, c):
        self.c, self.kw = c, {}

    async def chat(self, **kw):
        self.kw = kw
        c = self.c
        ch1 = SimpleNamespace(message=SimpleNamespace(content=c["texto"]), model=c["servido"], done=False)
        ch2 = SimpleNamespace(message=SimpleNamespace(content=""), model=c["servido"], done=True,
                              done_reason="length" if c["stop"] == "max_tokens" else "stop",
                              prompt_eval_count=6000, eval_count=500)
        return _Iter(c, [ch1, ch2])


ADAPTADORES = {
    "anthropic": (ProvedorAnthropic, ClienteAnthropic, True),
    "openai": (ProvedorOpenAI, ClienteOpenAI, True),
    "google": (ProvedorGoogle, ClienteGoogle, True),
    "openai_compativel": (ProvedorOpenAICompativel, ClienteCompativel, True),
    "ollama": (ProvedorOllama, ClienteOllama, False),  # Ollama nao tem conceito de recusa
}


def _prov(nome, **cen):
    classe, cliente_cls, _ = ADAPTADORES[nome]
    cliente = cliente_cls(_cenario(**cen))
    return classe(cliente=cliente, catalogo=CATALOGO), cliente


async def _chamar(p, prazo=2.0, schema=PropostaIA):
    return await p.gerar_estruturado(modelo="m", sistema="sis", mensagem="msg", schema=schema, prazo_s=prazo,
                                     effort="low", max_tokens=1000)


NOMES = list(ADAPTADORES)


@pytest.mark.parametrize("nome", NOMES)
async def test_contrato_ok(nome):
    p, _ = _prov(nome)
    obj, ch = await _chamar(p)
    assert ch.valida and ch.erro is None
    assert isinstance(obj, PropostaIA) and obj.acao == Acao.NAO_OPERAR
    assert ch.modelo_pedido == f"{nome}:m" and ch.modelo_servido == "m"
    assert ch.tokens_saida > 0 and ch.tokens_cache == 5000 and ch.tokens_entrada in (1000, 6000 - 5000) or nome in ("google", "ollama")
    assert ch.total_ms >= ch.ttft_ms >= 0
    assert ch.hash_prompt and ch.hash_dossie
    if nome != "ollama":
        assert ch.custo_usd > 0
    else:
        assert ch.custo_usd == 0.0


@pytest.mark.parametrize("nome", NOMES)
async def test_timeout_real_por_wait_for(nome):
    p, _ = _prov(nome, demora=30.0)
    t0 = time.monotonic()
    obj, ch = await _chamar(p, prazo=0.05)
    assert time.monotonic() - t0 < 2.0  # nao esperou os 30 s
    assert obj is None and not ch.valida and ch.erro == "timeout"


@pytest.mark.parametrize("nome", NOMES)
@pytest.mark.parametrize("status", [429, 529])
async def test_erros_http(nome, status):
    p, _ = _prov(nome, erro=ErroHTTP(status))
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == f"http_{status}"


@pytest.mark.parametrize("nome", [n for n in NOMES if ADAPTADORES[n][2]])
async def test_recusa_e_resposta_invalida(nome):
    p, _ = _prov(nome, recusa=True, texto="")
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "recusa"
    assert ch.categoria_recusa


async def test_recusa_anthropic_registra_categoria():
    p, _ = _prov("anthropic", recusa=True, texto="")
    _, ch = await _chamar(p)
    assert ch.stop_reason == "refusal" and ch.categoria_recusa == "reasoning_extraction"


@pytest.mark.parametrize("nome", NOMES)
async def test_max_tokens(nome):
    p, _ = _prov(nome, stop="max_tokens", texto='{"acao": "COMPR')
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "max_tokens"


@pytest.mark.parametrize("nome", NOMES)
async def test_json_invalido(nome):
    p, _ = _prov(nome, texto="isto nao e json", parse_ok=False)
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "json_invalido"


@pytest.mark.parametrize("nome", NOMES)
async def test_json_fora_do_schema(nome):
    p, _ = _prov(nome, texto='{"acao": "COMPRAR_TUDO"}', parse_ok=False)
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro == "json_invalido"


@pytest.mark.parametrize("nome", NOMES)
async def test_modelo_servido_fora_da_lista(nome):
    p, _ = _prov(nome, servido="outro-modelo-trocado")
    obj, ch = await _chamar(p)
    assert obj is None and not ch.valida and ch.erro.startswith("modelo_servido_fora_da_lista")
    assert ch.modelo_servido == "outro-modelo-trocado"  # registrado mesmo assim


async def test_modelo_servido_alias_com_sufixo_de_data_e_aceito():
    p, _ = _prov("openai", servido="m-2026-10-20")
    _, ch = await _chamar(p)
    assert ch.valida and ch.modelo_servido == "m-2026-10-20"


async def test_modelo_servido_lista_explicita():
    classe, cli, _ = ADAPTADORES["openai"]
    p = classe(cliente=cli(_cenario(servido="m-v2")), catalogo=CATALOGO, modelos_servidos_aceitos=["m-v1"])
    _, ch = await _chamar(p)
    assert not ch.valida and "fora_da_lista" in ch.erro


@pytest.mark.parametrize("nome", NOMES)
async def test_schema_com_campo_de_raciocinio_e_recusado(nome):
    class Ruim(BaseModel):
        acao: str
        reasoning: str

    p, cli = _prov(nome)
    obj, ch = await _chamar(p, schema=Ruim)
    assert obj is None and not ch.valida and "raciocinio" in ch.erro
    assert cli.kw == {}  # nem chegou a chamar o provedor


@pytest.mark.parametrize("nome", NOMES)
async def test_segredo_nao_vai_para_erro_nem_logs(nome, caplog):
    caplog.set_level(logging.DEBUG)
    p, _ = _prov(nome, erro=RuntimeError(f"falha de conexao api_key={CHAVE_SECRETA} Bearer {CHAVE_SECRETA}"))
    obj, ch = await _chamar(p)
    assert not ch.valida
    assert CHAVE_SECRETA not in (ch.erro or "")
    assert CHAVE_SECRETA not in ch.model_dump_json()
    assert CHAVE_SECRETA not in caplog.text


async def test_segredo_registrado_e_redigido(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "chave-estranha-sem-prefixo-999")
    p = ProvedorAnthropic(cliente=ClienteAnthropic(_cenario(erro=RuntimeError("401 chave-estranha-sem-prefixo-999 invalida"))))
    _, ch = await _chamar(p)
    assert "chave-estranha-sem-prefixo-999" not in (ch.erro or "")
    assert "chave-estranha-sem-prefixo-999" not in caplog.text


# ---------- parametros enviados ao SDK ----------
async def test_anthropic_kwargs():
    p, cli = _prov("anthropic")
    await _chamar(p)
    kw = cli.kw
    assert kw["model"] == "m" and "output_format" not in kw and kw["output_config"]["effort"] == "low"
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema" and set(fmt["schema"]["properties"]) == set(PropostaIA.model_json_schema()["properties"])
    assert kw["max_tokens"] == 1000
    for proibido in ("temperature", "top_p", "top_k", "thinking"):
        assert proibido not in kw


async def test_openai_kwargs():
    p, cli = _prov("openai")
    await _chamar(p)
    kw = cli.kw
    assert "text_format" not in kw and kw["store"] is False and kw["reasoning"] == {"effort": "low"}
    fmt = kw["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["name"] == "PropostaIA"
    assert "temperature" not in kw


async def test_google_kwargs():
    p, cli = _prov("google")
    await _chamar(p)
    cfg = cli.kw["config"]
    assert cfg["response_mime_type"] == "application/json"
    assert cfg["response_json_schema"] == PropostaIA.model_json_schema()


async def test_ollama_kwargs():
    p, cli = _prov("ollama")
    await _chamar(p)
    assert cli.kw["format"] == PropostaIA.model_json_schema() and cli.kw["stream"] is True


async def test_compativel_usa_json_schema_quando_catalogo_declara():
    p, cli = _prov("openai_compativel")
    await _chamar(p)
    assert cli.kw["response_format"]["type"] == "json_schema"


async def test_compativel_cai_para_json_object_sem_declaracao():
    cat = Catalogo.de_dict({"modelo": [{"provedor": "openai_compativel", "id": "m", "fixavel": True,
                                        "corte_conhecimento": "2025-12", "preco_entrada_mtok": 0.0,
                                        "preco_saida_mtok": 0.0, "saida_estruturada": "json"}]})
    cli = ClienteCompativel(_cenario())
    p = ProvedorOpenAICompativel(cliente=cli, catalogo=cat)
    _, ch = await _chamar(p)
    assert cli.kw["response_format"] == {"type": "json_object"} and ch.valida


# ---------- clientes reais do SDK: max_retries=0 e pacotes opcionais ----------
def test_sdks_instanciados_com_max_retries_zero():
    assert ProvedorAnthropic(api_key="x")._get_cliente().max_retries == 0
    assert ProvedorOpenAI(api_key="x")._get_cliente().max_retries == 0
    assert ProvedorOpenAICompativel(base_url="http://localhost:1/v1", api_key="x")._get_cliente().max_retries == 0


def test_pacote_opcional_ausente_da_erro_claro(monkeypatch):
    import importlib

    real = importlib.import_module

    def falso(nome, *a, **k):
        if nome.startswith(("google", "ollama")):
            raise ImportError(nome)
        return real(nome, *a, **k)

    monkeypatch.setattr("robowin.ia.provedores.base.importlib.import_module", falso)
    with pytest.raises(PacoteAusente, match="uv sync --extra google"):
        ProvedorGoogle(api_key="x")._get_cliente()
    with pytest.raises(PacoteAusente, match="uv sync --extra ollama"):
        ProvedorOllama()._get_cliente()


async def test_pacote_ausente_propaga_como_erro_de_configuracao(monkeypatch):
    import importlib

    real = importlib.import_module
    monkeypatch.setattr("robowin.ia.provedores.base.importlib.import_module",
                        lambda n, *a, **k: (_ for _ in ()).throw(ImportError(n)) if n.startswith("google") else real(n, *a, **k))
    with pytest.raises(ErroProvedor):
        await _chamar(ProvedorGoogle(api_key="x"))


def test_compativel_exige_base_url(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPATIVEL_BASE_URL", raising=False)
    with pytest.raises(ErroProvedor):
        ProvedorOpenAICompativel()._get_cliente()


def test_fabrica_de_provedores():
    assert isinstance(criar_provedor("falso"), ProvedorFalso)
    with pytest.raises(ErroProvedor):
        criar_provedor("inexistente")


# ---------- provedor falso ----------
async def test_falso_rotulado_e_roteiro():
    p = ProvedorFalso(roteiro=["lixo", RuntimeError("boom"), OK])
    _, c1 = await _chamar(p)
    assert c1.erro == "json_invalido" and c1.modelo_servido == "m-FALSO"
    _, c2 = await _chamar(p)
    assert not c2.valida and "RuntimeError" in c2.erro
    o3, c3 = await _chamar(p)
    assert c3.valida and o3 == OK and p.nome == "falso" and p.rotulo == "FALSO"


async def test_falso_respostas_padrao_por_schema():
    p = ProvedorFalso()
    obj, ch = await p.gerar_estruturado(modelo="m", sistema="s", mensagem="x", schema=ClassificacaoNoticia, prazo_s=1)
    assert ch.valida and obj.direcao == "neutra"
