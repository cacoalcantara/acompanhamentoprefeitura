from __future__ import annotations

import json

import pytest
from perfis_teste import WIN

from robowin.cli import main
from robowin.core.tipos import Acao, PropostaIA
from robowin.ia.prompts import carregar_prompt, hash_texto, hashes_prompts, mensagem_proponente
from robowin.ia.provedores.falso import ProvedorFalso
from robowin.ia.regressao import Faixas, bateria_padrao, rodar_regressao
from robowin.ia.validadores import validar_proposta


# ---------------- prompts ----------------
def test_prompts_existem_em_portugues_e_tem_hash():
    for papel in ("proponente", "revisor", "classificador_nlp"):
        p = carregar_prompt(papel)
        assert p.hash == hash_texto(p.texto) and len(p.hash) == 64 and p.arquivo.endswith(".md")
    assert set(hashes_prompts()) >= {"proponente_v1.md", "revisor_v1.md", "nlp_v1.md"}


def test_prompt_proponente_conteudo_exigido():
    t = carregar_prompt("proponente").texto
    for acao in ("COMPRAR", "VENDER", "MANTER", "ENCERRAR", "NAO_OPERAR"):
        assert acao in t
    for termo in ("preco_toca", "preco_rompe_acima", "janela_tempo", "spread_max_pontos", "evidencias_a_favor",
                  "evidencias_contra", "invalidacoes", "NUNCA reproduza", "Proibido"):
        assert termo in t
    assert "RSI" in t and "Proibido" in t  # os indicadores so aparecem na lista de PROIBICOES
    assert "SIMULACAO" in t


def test_prompt_revisor_conteudo_exigido():
    t = carregar_prompt("revisor").texto
    assert "concordancia entre modelos NAO prova lucro" in t
    assert "ENTRADAS" in t and "coerente" in t and "afirmacoes_sem_suporte" in t


def test_prompts_nao_pedem_raciocinio_literal():
    for papel in ("proponente", "revisor", "classificador_nlp"):
        t = carregar_prompt(papel).texto.lower()
        assert "passo a passo" not in t.replace("nunca reproduza seu raciocinio passo a passo", "")


def test_variante_por_provedor_tem_hash_proprio(tmp_path):
    (tmp_path / "proponente_v1.md").write_text("base", encoding="utf-8")
    (tmp_path / "proponente_v1.google.md").write_text("variante google", encoding="utf-8")
    base = carregar_prompt("proponente", diretorio=tmp_path)
    var = carregar_prompt("proponente", provedor="google", diretorio=tmp_path)
    outro = carregar_prompt("proponente", provedor="openai", diretorio=tmp_path)
    assert var.variante == "google" and var.hash != base.hash and outro.hash == base.hash


def test_mudar_o_prompt_muda_o_hash(tmp_path):
    (tmp_path / "nlp_v1.md").write_text("a", encoding="utf-8")
    h1 = carregar_prompt("nlp_v1", diretorio=tmp_path).hash
    (tmp_path / "nlp_v1.md").write_text("b", encoding="utf-8")
    assert carregar_prompt("nlp_v1", diretorio=tmp_path).hash != h1


# ---------------- regressao ----------------
class Esperto(ProvedorFalso):
    """Responde de forma sensata conforme o dossie; permite trocar o modelo servido."""
    servido = "gpt-x-v1"

    async def _executar(self, **kw):
        r = await super()._executar(**kw)
        com_pos = '"lado":"compra"' in kw["mensagem"]
        obj = PropostaIA(acao=Acao.ENCERRAR if com_pos else Acao.NAO_OPERAR, incerteza=0.6,
                         justificativa_curta="ok")
        r.objeto, r.texto, r.modelo_servido = obj, obj.model_dump_json(), self.servido
        return r


async def test_bateria_fixa_e_estavel():
    assert [c.id for c in bateria_padrao(WIN)] == [c.id for c in bateria_padrao(WIN)]
    assert len(bateria_padrao(WIN)) >= 4


async def test_regressao_ok_com_modelo_estavel():
    p = Esperto()
    p.nome = "openai"
    res = await rodar_regressao(p, "openai:gpt-x", perfil=WIN, k=3, fixavel=False)
    assert res.ok, res.violacoes
    assert res.modelos_servidos == ("gpt-x-v1",) and not res.mudou_modelo_servido
    assert res.alias_nao_fixavel  # alias sem snapshot: sinalizado mesmo quando estavel
    assert all(r.n == 3 and r.validas == 3 for r in res.por_caso.values())
    assert p.chamadas == 3 * len(bateria_padrao(WIN))


async def test_regressao_sinaliza_mudanca_do_modelo_servido():
    p = Esperto()
    p.nome = "openai"
    res = await rodar_regressao(p, "openai:gpt-x", perfil=WIN, k=2, fixavel=False, servido_anterior="gpt-x-v0")
    assert res.mudou_modelo_servido and not res.ok
    assert any("modelo servido mudou" in v for v in res.violacoes)


async def test_regressao_sinaliza_mudanca_no_meio_da_execucao():
    class Troca(Esperto):
        async def _executar(self, **kw):
            Esperto.servido = "gpt-x-v1" if self.chamadas < 5 else "gpt-x-v2"
            return await super()._executar(**kw)
    try:
        p = Troca()
        res = await rodar_regressao(p, "openai:gpt-x", perfil=WIN, k=3, fixavel=False)
        assert res.mudou_modelo_servido and set(res.modelos_servidos) == {"gpt-x-v1", "gpt-x-v2"}
    finally:
        Esperto.servido = "gpt-x-v1"


async def test_regressao_falha_com_respostas_invalidas_e_fora_de_faixa():
    ruim = ProvedorFalso(roteiro=["lixo"])
    res = await rodar_regressao(ruim, "falso:m", perfil=WIN, k=2)
    assert not res.ok and any("validas" in v for v in res.violacoes)
    # modelo que sempre compra: acao fora do aceito na bateria
    class Compra(Esperto):
        async def _executar(self, **kw):
            r = await ProvedorFalso._executar(self, **kw)
            obj = PropostaIA(acao=Acao.COMPRAR, stop_pontos=50, alvo_pontos=60)
            r.objeto, r.texto, r.modelo_servido = obj, obj.model_dump_json(), "m-FALSO"
            return r
    res2 = await rodar_regressao(Compra(), "falso:m", perfil=WIN, k=2, faixas=Faixas(taxa_acao_aceita_min=0.9))
    assert any("acoes aceitas" in v for v in res2.violacoes)


def test_proposta_da_bateria_validada_pelo_validador():
    caso = next(c for c in bateria_padrao(WIN) if c.id == "posicao_comprada_perdendo")
    assert not validar_proposta(PropostaIA(acao=Acao.COMPRAR, stop_pontos=10, alvo_pontos=10), caso.dossie, stop_max_pontos=WIN.stop_max_pontos).ok
    assert "proponente" not in mensagem_proponente(caso.dossie)  # a mensagem so leva o dossie


# ---------------- CLI ----------------
CHAVES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_COMPATIVEL_BASE_URL",
          "OLLAMA_HOST")


def test_cli_ia_testar_sem_chave_usa_falso_e_avisa(monkeypatch, capsys):
    for v in CHAVES:
        monkeypatch.delenv(v, raising=False)
    rc = main(["ia", "testar", "--modelo", "anthropic:claude-opus-5-5", "--k", "1"])
    cap = capsys.readouterr()
    assert rc == 0
    assert "FALSO" in cap.err and "sem chave" in cap.err
    saida = json.loads(cap.out)
    assert saida["provedor_real"] is False and saida["rotulo"] == "FALSO"


def test_cli_ia_testar_recusa_modelo_fora_do_catalogo(monkeypatch):
    from robowin.ia.catalogo import ErroCatalogo

    for v in CHAVES:
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(ErroCatalogo):
        main(["ia", "testar", "--modelo", "anthropic:inexistente", "--k", "1"])


def test_cli_latencia_sem_chave_usa_falso(monkeypatch, capsys):
    for v in CHAVES:
        monkeypatch.delenv(v, raising=False)
    rc = main(["latencia", "--modelos", "anthropic:claude-haiku-5-5,openai:gpt-6.1-sol", "--n", "3"])
    cap = capsys.readouterr()
    assert rc == 0 and cap.err.count("FALSO") >= 2
    saida = json.loads(cap.out)
    assert set(saida) == {"anthropic:claude-haiku-5-5", "openai:gpt-6.1-sol"}
    for r in saida.values():
        assert r["provedor_real"] is False and r["n"] == 3 and r["falhas"] == 0
        assert set(r["ttft_ms"]) == {"p50", "p95", "p99"} and set(r["total_ms"]) == {"p50", "p95", "p99"}
        assert "FALSO" in r["rotulo"]


def test_cli_com_chave_instancia_provedor_real_sem_chamar_rede(monkeypatch):
    from robowin.ia.catalogo import Catalogo
    from robowin.ia.cli_cmds import provedor_para_cli, tem_chave
    from robowin.ia.provedores.anthropic import ProvedorAnthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-teste-0000000000")
    assert tem_chave("anthropic") and not tem_chave("desconhecido")
    prov, real = provedor_para_cli("anthropic", Catalogo.carregar("config/modelos.toml"))
    assert real and isinstance(prov, ProvedorAnthropic)
