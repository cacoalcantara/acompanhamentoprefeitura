from __future__ import annotations

import json

import pytest
from perfis_teste import WIN

from robowin.cli import main
from robowin.core.tipos import Acao, AjustePosicao, PlanoTradeIA, PropostaIA
from robowin.ia.prompts import (
    NOMES,
    carregar_prompt,
    hash_texto,
    hashes_prompts,
    mensagem_planejador,
    mensagem_proponente,
    mensagem_revisor_plano,
)
from robowin.ia.provedores.falso import ProvedorFalso
from robowin.ia.regressao import Faixas, bateria_padrao, bateria_planejador, rodar_regressao
from robowin.ia.validadores import validar_proposta


# ---------------- prompts do planejador (docs/16 WP4) ----------------
def test_prompts_do_planejador_registrados_e_com_hash():
    assert NOMES["planejador"] == "planejador_v1" and NOMES["revisor_plano"] == "revisor_plano_v1"
    for papel in ("planejador", "revisor_plano"):
        p = carregar_prompt(papel)
        assert p.hash == hash_texto(p.texto) and p.arquivo.endswith(f"{NOMES[papel]}.md")
    assert {"planejador_v1.md", "revisor_plano_v1.md"} <= set(hashes_prompts())


def test_prompt_planejador_conteudo_exigido():
    t = carregar_prompt("planejador").texto
    for termo in ("-30", "+10", "`0`", "ancoragem em t0", "faixa_contra_pontos", "faixa_favor_pontos", "latencia_ia",
                  "p_equilibrio", "custo_ida_volta_pontos", "limites do usuario", "NAO pode alterar", "reprovado",
                  "so pode APERTAR", "NAO_OPERAR e uma resposta legitima", "PONTUADA", "Proibido", "RSI",
                  "preco_rompe_acima", "janela_tempo", "stop_resultado_pontos", "NUNCA reproduza", "SIMULACAO"):
        assert termo in t, termo
    assert "passo a passo" not in t.lower().replace("nunca reproduza seu raciocinio passo a passo", "")


def test_prompt_revisor_plano_conteudo_exigido():
    t = carregar_prompt("revisor_plano").texto
    for termo in ("PLANO COMPILADO", "coerente", "dados_ok", "regras_ok", "obrigatorios", "afirmacoes_sem_suporte",
                  "p_alvo_revisor", "equilibrio", "Concordancia entre modelos NAO prova lucro", "ENTRADA"):
        assert termo in t, termo


def test_mensagens_do_planejador_e_do_revisor_do_plano():
    caso = bateria_planejador(WIN)[1]
    m = mensagem_planejador(caso.dossie)
    assert "precos e distancias em pontos relativos" in m and "demais campos com a unidade no nome" in m
    assert "todos os numeros sao pontos relativos" not in m  # a frase do v1 era falsa para o dossie v2
    assert json.loads(m.split("\n", 1)[1]) == caso.dossie
    r = mensagem_revisor_plano(caso.dossie, {"lado": "compra", "stop_pontos": 100.0, "p_equilibrio": 0.45})
    assert "PLANO COMPILADO" in r and '"p_equilibrio":0.45' in r


# ---------------- regressao do planejador ----------------
class PlanejadorEsperto(ProvedorFalso):
    """Nao opera sem posicao; com posicao, MANTER apertando o stop para o empate se der (senao, so MANTER)."""

    async def _executar(self, **kw):
        r = await ProvedorFalso._executar(self, **kw)
        dossie = json.loads(kw["mensagem"].split("\n", 1)[1])
        pos = dossie["posicao"]
        if pos["quantidade"] and pos["resultado_pontos"] > WIN.tick_pontos:
            obj = PlanoTradeIA(acao=Acao.MANTER, ajuste=AjustePosicao(stop_resultado_pontos=0.0))
        elif pos["quantidade"]:
            obj = PlanoTradeIA(acao=Acao.ENCERRAR, justificativa_curta="tese desfeita")
        else:
            obj = PlanoTradeIA(acao=Acao.NAO_OPERAR, justificativa_curta="sem vantagem liquida")
        r.objeto, r.texto, r.modelo_servido = obj, obj.model_dump_json(), "plan-v1"
        return r


def test_bateria_do_planejador_fixa_e_v2():
    a, b = bateria_planejador(WIN), bateria_planejador(WIN)
    assert [c.id for c in a] == [c.id for c in b] and len(a) >= 5
    assert [c.dossie for c in a] == [c.dossie for c in b]
    assert all(c.dossie["meta"]["versao_dossie"] == 2 and c.envelope is not None for c in a)
    assert any(not c.envelope.entradas_possiveis for c in a)


async def test_regressao_do_planejador_ok_com_modelo_estavel():
    p = PlanejadorEsperto()
    res = await rodar_regressao(p, "falso:plan", perfil=WIN, k=2, papel="planejador")
    assert res.ok, res.violacoes
    assert all(r.validas == r.n == 2 and r.regras_ok == 2 for r in res.por_caso.values())


async def test_regressao_do_planejador_conta_regras_violadas():
    class Compra(PlanejadorEsperto):
        async def _executar(self, **kw):
            r = await ProvedorFalso._executar(self, **kw)
            obj = PlanoTradeIA(acao=Acao.COMPRAR, stop_pontos=10 * WIN.tick_pontos, alvo_pontos=WIN.tick_pontos)
            r.objeto, r.texto, r.modelo_servido = obj, obj.model_dump_json(), "plan-v1"
            return r
    res = await rodar_regressao(Compra(), "falso:plan", perfil=WIN, k=1, papel="planejador")
    assert all(r.regras_ok == 0 for r in res.por_caso.values())
    assert any("acoes aceitas" in v for v in res.violacoes)


async def test_regressao_recusa_papel_sem_bateria():
    with pytest.raises(ValueError):
        await rodar_regressao(ProvedorFalso(), "falso:m", perfil=WIN, k=1, papel="revisor")


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


def test_cli_ia_testar_sem_chave_falha_claro(monkeypatch, capsys):
    for v in CHAVES:
        monkeypatch.delenv(v, raising=False)
    rc = main(["ia", "testar", "--modelo", "anthropic:claude-opus-5-5", "--k", "1"])
    cap = capsys.readouterr()
    assert rc == 2 and cap.out == ""
    assert "sem chave" in cap.err and "ANTHROPIC_API_KEY" in cap.err and "setx" in cap.err and "--falso" in cap.err


def test_cli_ia_testar_com_falso_explicito_roda_rotulado(monkeypatch, capsys):
    for v in CHAVES:
        monkeypatch.delenv(v, raising=False)
    rc = main(["ia", "testar", "--modelo", "anthropic:claude-opus-5-5", "--k", "1", "--falso"])
    cap = capsys.readouterr()
    assert rc == 0
    assert "FALSO" in cap.err
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
