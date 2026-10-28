"""CLI: ``robowin ia testar --modelo provedor:modelo`` e ``robowin latencia --modelos a:b,c:d``.

Chamadas reais SO se houver chave no ambiente; sem chave usa o provedor FALSO (rotulado) e avisa.
Nenhuma chave e impressa.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

from robowin.core.instrumentos import argumentos_ativo, perfil_do_cli
from robowin.core.tipos import ClassificacaoNoticia
from robowin.ia.catalogo import Catalogo, ErroCatalogo, dividir_modelo
from robowin.ia.provedores import criar_provedor
from robowin.ia.provedores.base import ErroProvedor

VARIAVEIS_CHAVE = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "openai_compativel": ("OPENAI_COMPATIVEL_BASE_URL",),
    "ollama": ("OLLAMA_HOST",),
}


def tem_chave(provedor: str) -> bool:
    return any(os.environ.get(v) for v in VARIAVEIS_CHAVE.get(provedor, ()))


def _catalogo(caminho: str | None) -> Catalogo:
    for c in (caminho, "config/modelos.toml", Path(__file__).resolve().parents[3] / "config" / "modelos.toml"):
        if c and Path(c).exists():
            return Catalogo.carregar(c)
    raise ErroCatalogo("config/modelos.toml nao encontrado")


def provedor_para_cli(provedor: str, catalogo: Catalogo) -> tuple[Any, bool]:
    """(provedor, real). Sem chave => FALSO, com aviso em stderr."""
    if tem_chave(provedor):
        try:
            return criar_provedor(provedor, catalogo=catalogo), True
        except ErroProvedor as e:
            print(f"AVISO: {e}; usando provedor FALSO", file=sys.stderr)
    else:
        print(f"AVISO: sem chave para '{provedor}'; usando provedor FALSO (resultados NAO sao do modelo real)",
              file=sys.stderr)
    return criar_provedor("falso", catalogo=None), False


def _resumo(valores: list[float]) -> dict[str, float]:
    if not valores:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    p = np.percentile(valores, [50, 95, 99])
    return {"p50": round(float(p[0]), 1), "p95": round(float(p[1]), 1), "p99": round(float(p[2]), 1)}


async def medir_latencia(prov: Any, modelo: str, n: int, prazo_s: float, effort: str | None = None) -> dict[str, Any]:
    ttft: list[float] = []
    total: list[float] = []
    falhas = 0
    for _ in range(n):
        _, ch = await prov.gerar_estruturado(
            modelo=modelo, sistema="Classifique a noticia.", mensagem="TITULO: Banco central mantem a taxa de juros.",
            schema=ClassificacaoNoticia, prazo_s=prazo_s, effort=effort, max_tokens=300)
        if ch.valida:
            ttft.append(ch.ttft_ms)
            total.append(ch.total_ms)
        else:
            falhas += 1
    return {"n": n, "falhas": falhas, "ttft_ms": _resumo(ttft), "total_ms": _resumo(total)}


def _cmd_testar(a: Any) -> int:
    from robowin.ia.regressao import Faixas, rodar_regressao

    cat = _catalogo(a.catalogo)
    prov_nome, modelo = dividir_modelo(a.modelo)
    entrada = cat.obter(a.modelo)  # recusa modelo fora do catalogo / sem preco / sem corte
    prov, real = provedor_para_cli(prov_nome, cat)
    res = asyncio.run(rodar_regressao(prov, a.modelo, perfil=perfil_do_cli(a), k=a.k, fixavel=entrada.fixavel, effort=a.effort,
                                      prazo_s=a.prazo, faixas=Faixas()))
    print(json.dumps({
        "modelo": res.modelo, "provedor_real": real, "rotulo": "" if real else "FALSO", "k": res.k,
        "modelos_servidos": res.modelos_servidos, "mudou_modelo_servido": res.mudou_modelo_servido,
        "alias_nao_fixavel": res.alias_nao_fixavel, "custo_usd": round(res.custo_usd, 6),
        "casos": {c: {"n": r.n, "validas": r.validas, "regras_ok": r.regras_ok, "acoes": r.acoes}
                  for c, r in res.por_caso.items()},
        "violacoes": res.violacoes,
    }, ensure_ascii=False, indent=2))
    return 0 if (res.ok or not real) else 1


def _cmd_latencia(a: Any) -> int:
    cat = _catalogo(a.catalogo)
    saida: dict[str, Any] = {}
    for ref in [m.strip() for m in a.modelos.split(",") if m.strip()]:
        prov_nome, modelo = dividir_modelo(ref)
        cat.obter(ref)
        prov, real = provedor_para_cli(prov_nome, cat)
        r = asyncio.run(medir_latencia(prov, modelo, a.n, a.prazo, a.effort))
        r["provedor_real"] = real
        r["rotulo"] = "" if real else "FALSO (latencia nao representa o modelo)"
        saida[ref] = r
    print(json.dumps(saida, ensure_ascii=False, indent=2))
    return 0


def registrar(sub: Any) -> None:
    p = sub.add_parser("ia", help="ferramentas de IA")
    sp = p.add_subparsers(dest="ia_cmd")
    t = sp.add_parser("testar", help="bateria de regressao contra um provedor:modelo")
    t.add_argument("--modelo", required=True, help="provedor:modelo (ex.: anthropic:claude-opus-5-5)")
    t.add_argument("--k", type=int, default=3)
    t.add_argument("--effort", default=None)
    t.add_argument("--prazo", type=float, default=40.0)
    t.add_argument("--catalogo", default=None)
    argumentos_ativo(t)
    t.set_defaults(func=_cmd_testar)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])

    la = sub.add_parser("latencia", help="mede TTFT/total p50/p95/p99 dos modelos (reais so com chave)")
    la.add_argument("--modelos", required=True, help="lista separada por virgula de provedor:modelo")
    la.add_argument("--n", type=int, default=20)
    la.add_argument("--effort", default=None)
    la.add_argument("--prazo", type=float, default=60.0)
    la.add_argument("--catalogo", default=None)
    la.set_defaults(func=_cmd_latencia)
