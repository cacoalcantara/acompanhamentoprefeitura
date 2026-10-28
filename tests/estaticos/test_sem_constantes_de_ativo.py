"""Teste ESTATICO: nenhum numero/nome de ativo no codigo de src/ fora de config e do modulo de perfis.

Falha se aparecerem, como CODIGO (comentarios e docstrings sao ignorados), (a) o nome de um contrato/ativo de
config/instrumentos.toml como string ("WIN", "WDOX26", "SINT_WIN"...) ou (b) os literais float 0.20 e 5.0,
salvo a allowlist abaixo (cada entrada com a justificativa)."""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2] / "src" / "robowin"
MODULOS_DE_PERFIL = {"core/instrumentos.py", "core/bolsas.py"}
# O duble e uma CORRETORA SINTETICA: lista simbolos de varios ativos como a corretora real o faria (os numeros
# vem dos perfis; os nomes dos contratos sao o "cardapio" da corretora simulada e o default da grade sintetica).
EXCECOES_DE_ARQUIVO = {"mt5/duble.py"}
# Todos os ativos de config/instrumentos.toml (B3, forex, metais, CFD, cripto) e QUALQUER par de moedas/metal/cripto
# no formato da corretora (EURUSD, USDJPY, XAUUSD, BTCUSD, com sufixo .m/pro...), mesmo sem perfil.
_PERFIS = sorted(k for k, v in tomllib.loads((RAIZ.parents[1] / "config" / "instrumentos.toml").read_text(
    encoding="utf-8")).items() if isinstance(v, dict) and k != "sessao_padrao")
_MOEDAS = "USD|EUR|GBP|JPY|CHF|AUD|NZD|CAD|BRL|MXN|ZAR|TRY|SEK|NOK|HKD|SGD|CNH|XAU|XAG|XPT|XPD|BTC|ETH|LTC|XRP"
RE_ATIVO = re.compile(rf"^(?:SINT_)?(?:(?:{'|'.join(map(re.escape, _PERFIS))})(?:[FGHJKMNQUVXZ]\d{{2}})?"
                      rf"|(?:{_MOEDAS})(?:{_MOEDAS})(?:[._]?[a-z0-9]{{0,4}})?)$")

# (arquivo, valor, funcao/escopo) -> justificativa. Valores que NAO sao do ativo.
ALLOWLIST: dict[tuple[str, float, str], str] = {
    ("avaliacao/impostos.py", 0.2, "ConfigImpostos"): "aliquota fiscal do day trade (20%), nao e do ativo",
    ("avaliacao/estatistica.py", 0.2, "sprt_binomial"): "beta (poder) estatistico do SPRT",
    ("avaliacao/estatistica.py", 0.2, "sprt_media"): "beta (poder) estatistico do SPRT",
    ("avaliacao/estatistica.py", 5.0, "bootstrap_estacionario"): "tamanho medio de bloco do bootstrap",
    ("modelos/microestrutura.py", 5.0, "<module>"): "reg_lambda do LightGBM",
    ("modelos/demo_sintetico.py", 5.0, "_sessao"): "opcoes de volume do tick sintetico (contratos), nao e preco",
    ("risco/estresse.py", 5.0, "<module>"): "cenario de gap de 5% (percentual)",
    ("risco/kill_switch.py", 5.0, "ConfigKill"): "timeout de heartbeat em segundos",
    ("mm/markouts.py", 5.0, "markouts"): "horizonte de markout em segundos",
    ("decisao/simulacao.py", 5.0, "ParamsSimulacao"): "menor efeito de interesse (mde) em moeda do instrumento: parametro de amostragem",
    ("decisao/simulacao.py", 5.0, "montar_manifesto"): "idem (mde padrao)",
    ("decisao/orquestrador.py", 5.0, "aguardar_tarefas"): "timeout em segundos",
    ("painel/demo.py", 5.0, "<module>"): "manifesto de demonstracao do painel (mde)",
    ("rl/ambiente.py", 5.0, "ConfigAmbiente"): "passo/apregoacao em segundos",
    ("mt5/coleta.py", 5.0, "ColetorTicks.__init__"): "limite de feed parado em segundos",
    ("mt5/gateway.py", 5.0, "ConfigGateway"): "espera de ordem incerta em segundos",
    ("mt5/execucao_demo.py", 5.0, "ConfigDemo"): "espera de ordem incerta em segundos",
    ("mt5/canais.py", 5.0, "CanalComandosZMQ.enviar"): "timeout em segundos",
    ("mt5/canais.py", 5.0, "ClienteComandosZMQ.enviar"): "timeout em segundos",
    ("mt5/cli_cmds.py", 5.0, "registrar"): "recuo de coleta em segundos",
    ("ia/provedores/base.py", 5.0, "?"): "timeout/backoff em segundos",
    ("avaliacao/estatistica.py", 0.2, "sprt_bernoulli"): "beta (poder) estatistico do SPRT",
    ("backtest/vazamento.py", 0.2, "perturbar_futuro"): "multiplicador aleatorio do VOLUME (contratos), nao e preco",
    ("backtest/vazamento.py", 5.0, "perturbar_futuro"): "idem (limite superior do multiplicador de volume)",
    ("decisao/gatilhos.py", 0.2, "ConfigGatilhos"): "peso da media movel do custo de IA",
    ("ia/regressao.py", 0.2, "bateria_padrao"): "ruido/deriva da bateria fixa, EM TICKS do ativo (x tick do perfil)",
    ("decisao/orquestrador.py", 5.0, "Orquestrador.aguardar_tarefas"): "timeout em segundos",
    ("mt5/canais.py", 5.0, "ClienteLocal.enviar"): "timeout em segundos",
    ("painel/demo.py", 5.0, "popular"): "mde do manifesto de demonstracao do painel",
    ("risco/kill_switch.py", 5.0, "KillSwitch"): "timeout de heartbeat em segundos",
}


def _docstrings(arvore: ast.AST) -> set[int]:
    ids: set[int] = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and no.body:
            p = no.body[0]
            if isinstance(p, ast.Expr) and isinstance(p.value, ast.Constant) and isinstance(p.value.value, str):
                ids.add(id(p.value))
    return ids


def _escopo(arvore: ast.AST, alvo: ast.AST) -> str:
    achado = "<module>"

    def visita(no: ast.AST, pilha: list[str]) -> bool:
        nonlocal achado
        if no is alvo:
            achado = ".".join(pilha) or "<module>"
            return True
        nome = [no.name] if isinstance(no, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else []
        return any(visita(c, pilha + nome) for c in ast.iter_child_nodes(no))

    visita(arvore, [])
    return achado


def _varrer() -> list[str]:
    erros: list[str] = []
    for f in sorted(RAIZ.rglob("*.py")):
        rel = f.relative_to(RAIZ).as_posix()
        if rel in MODULOS_DE_PERFIL or rel in EXCECOES_DE_ARQUIVO:
            continue
        arv = ast.parse(f.read_text(encoding="utf-8"))
        docs = _docstrings(arv)
        for no in ast.walk(arv):
            if not isinstance(no, ast.Constant) or id(no) in docs:
                continue
            if isinstance(no.value, str) and RE_ATIVO.match(no.value):
                erros.append(f"{rel}:{no.lineno}: nome de ativo como constante de dominio: {no.value!r}")
            elif type(no.value) is float and no.value in (0.2, 5.0):
                escopo = _escopo(arv, no)
                chave = next((k for k in ALLOWLIST if k[0] == rel and k[1] == no.value
                              and (escopo == k[2] or escopo.startswith(k[2]) or k[2] == "?")), None)
                if chave is None:
                    erros.append(f"{rel}:{no.lineno}: literal {no.value} em '{escopo}' (use o perfil do ativo)")
    return erros


def test_nenhuma_constante_de_ativo_fora_do_perfil():
    assert _varrer() == []


def test_regex_cobre_todos_os_mercados():
    for nome in ("WIN", "WDOX26", "SINT_WIN", "PETR4", "EURUSD", "USDJPY", "XAUUSD", "BTCUSD", "US500", "EURUSD.m",
                 "GBPUSDpro"):
        assert RE_ATIVO.match(nome), nome
    for nome in ("USD", "BRL", "FOREX", "forex", "cfd_indice"):
        assert not RE_ATIVO.match(nome), nome


def test_allowlist_curta_e_justificada():
    assert len(ALLOWLIST) <= 35 and all(len(j) > 10 for j in ALLOWLIST.values())
