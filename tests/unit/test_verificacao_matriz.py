"""`robowin verificar`: a matriz tem os 3 estados, REPROVADO muda o codigo de saida e nenhum segredo vaza."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from robowin.cli import main
from robowin.verificacao import checagens as K
from robowin.verificacao import matriz as M

RAIZ = Path(__file__).resolve().parents[2]
SEGREDO = "sk-ant-SEGREDO-NAO-PODE-VAZAR-123456"


@pytest.fixture(scope="module")
def completo():
    return K.executar_todas(RAIZ)


def test_matriz_real_tem_aprovado_e_nao_verificavel_e_nenhuma_reprovacao(completo):
    estados = {r.estado for r in completo}
    assert M.APROVADO in estados and M.NAO_VERIFICAVEL in estados
    assert M.REPROVADO not in estados, [r for r in completo if r.estado == M.REPROVADO]
    assert M.codigo_de_saida(completo) == 0


def test_cobre_todas_as_checagens_pedidas(completo):
    nomes = " | ".join(r.nome for r in completo)
    for trecho in ("Importabilidade", "Ruff", "MQL4", "auditoria", "Catalogo", "Perfis", "Politica de risco", "order_send",
                   "numero/nome de ativo", "duble MT5", "duble MT4", "Painel", "Gateway", "MetaTrader5", "ANTHROPIC_API_KEY",
                   "OPENAI_API_KEY", "Conectividade"):
        assert trecho in nomes, trecho


def test_risco_do_repositorio_aprovado_pelo_titular(completo):
    # config/aprovacoes/risco-v2.toml: aprovacao provisoria do titular, somente DEMO (o caso 'pendente' e testado
    # em test_ativo_configuravel e test_aprovacao_risco_perfil com copias isoladas)
    r = next(x for x in completo if x.nome.startswith("Politica de risco"))
    assert r.estado == M.APROVADO and "responsavel" in r.detalhe


def test_perfis_listam_bloqueados(completo):
    r = next(x for x in completo if x.nome.startswith("Perfis"))
    assert r.estado == M.APROVADO and "BLOQUEADOS" in r.detalhe and "CONFIRMADO" in r.detalhe


def test_formatar_e_json_com_os_tres_estados():
    res = [M.aprovado("a", "ok"), M.reprovado("b", "falhou"), M.nao_verificavel("c", "motivo X")]
    txt = M.formatar(res)
    assert "[APROVADO" in txt and "[REPROVADO" in txt and "[NAO VERIFICAVEL AQUI" in txt and "motivo X" in txt
    assert "RESULTADO: REPROVADO" in txt
    j = json.loads(M.para_json(res))
    assert j["contagem"] == {"APROVADO": 1, "REPROVADO": 1, "NAO VERIFICAVEL AQUI": 1} and j["resultado"] == "REPROVADO"


def test_estado_invalido_recusado():
    with pytest.raises(ValueError):
        M.Resultado("x", "TALVEZ")


def test_so_nao_verificavel_nao_reprova():
    assert M.codigo_de_saida([M.aprovado("a"), M.nao_verificavel("b", "m")]) == 0
    assert M.codigo_de_saida([M.aprovado("a"), M.reprovado("b", "x")]) != 0


def _repo_minimo(tmp_path: Path) -> Path:
    raiz = tmp_path / "repo"
    shutil.copytree(RAIZ / "config", raiz / "config")
    (raiz / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return raiz


def test_reprovado_real_muda_o_codigo_de_saida(tmp_path, capsys):
    raiz = _repo_minimo(tmp_path)
    assert main(["verificar", "--raiz", str(raiz), "--somente", "risco"]) == 0
    with open(raiz / "config" / "risco.toml", "a", encoding="utf-8") as f:
        f.write("\n# adulterado depois da aprovacao\n")
    capsys.readouterr()
    codigo = main(["verificar", "--raiz", str(raiz), "--somente", "risco"])
    saida = capsys.readouterr().out
    assert codigo != 0 and "[REPROVADO" in saida and "hash" in saida


def test_json_via_cli(tmp_path, capsys):
    raiz = _repo_minimo(tmp_path)
    assert main(["verificar", "--json", "--raiz", str(raiz), "--somente", "risco", "--somente", "catalogo"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert len(j["itens"]) == 2 and {"resultado", "contagem", "itens"} <= set(j)


def test_checagem_que_explode_vira_reprovado(monkeypatch):
    def quebra(ctx):
        raise RuntimeError("boom")
    monkeypatch.setattr(K, "checagens", lambda: [("x", quebra)])
    res = K.executar_todas(RAIZ)
    assert len(res) == 1 and res[0].estado == M.REPROVADO and "boom" in res[0].detalhe


def test_nunca_vaza_segredos(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SEGREDO)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-OPENAI-SEGREDO-987654321")
    monkeypatch.setattr(K, "checagens", lambda: [("chaves", K.chaves_api), ("x", lambda c: M.reprovado("y", f"vazou {SEGREDO}"))])
    for flags in ([], ["--json"]):
        main(["verificar", *flags])
        saida = capsys.readouterr().out
        assert "SEGREDO" not in saida
        assert "ANTHROPIC_API_KEY" in saida and "presente" in saida


def test_chave_ausente_e_nao_verificavel(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert all(r.estado == M.NAO_VERIFICAVEL for r in K.chaves_api(K.Contexto(RAIZ)))


def test_conectividade_so_lista_hosts():
    r = K.conectividade(K.Contexto(RAIZ))
    assert r.estado == M.NAO_VERIFICAVEL and "api.anthropic.com" in r.detalhe and "NAO verificada" in r.detalhe


def test_sem_raiz_vira_nao_verificavel():
    ctx = K.Contexto(None)
    for fn in (K.ruff, K.lint_mql4, K.catalogo_modelos, K.perfis_ativo_bolsa, K.politica_risco):
        assert fn(ctx).estado == M.NAO_VERIFICAVEL
