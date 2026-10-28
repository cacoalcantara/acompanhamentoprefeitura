"""CLI de IA: ``robowin ia listar | adicionar-modelo | testar`` e ``robowin latencia --modelos a:b,c:d``.

Provedores vem do registro (config/provedores.toml); modelos, do catalogo (config/modelos.toml). Chamadas reais
SO com chave no ambiente: ``ia testar`` sem chave FALHA com mensagem clara (``--falso`` roda o provedor FALSO,
rotulado); ``latencia`` sem chave usa o FALSO e avisa. Nenhum valor de chave e impresso.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import numpy as np

from robowin.core.instrumentos import argumentos_ativo, perfil_do_cli
from robowin.core.tipos import ClassificacaoNoticia
from robowin.ia.catalogo import (
    Catalogo,
    ErroCatalogo,
    carregar_toml_texto_tolerante,
    dividir_modelo,
    validar_entrada,
)
from robowin.ia.provedores import criar_provedor, criar_provedor_registrado
from robowin.ia.provedores.base import ErroProvedor
from robowin.ia.registro_provedores import ErroRegistro, RegistroProvedores, registro_padrao


def tem_chave(provedor: str, registro: RegistroProvedores | None = None) -> bool:
    """O provedor do registro tem chave (e as variaveis exigidas) no ambiente? Desconhecido => False."""
    try:
        reg = registro if registro is not None else registro_padrao()
    except ErroRegistro:
        return False
    return reg.chave_presente(provedor)


def _caminho_catalogo(caminho: str | None) -> Path:
    for c in (caminho, "config/modelos.toml", Path(__file__).resolve().parents[3] / "config" / "modelos.toml"):
        if c and Path(c).exists():
            return Path(c)
    raise ErroCatalogo("config/modelos.toml nao encontrado")


def _catalogo(caminho: str | None) -> Catalogo:
    return Catalogo.carregar(_caminho_catalogo(caminho))


def _registro(caminho: str | None) -> RegistroProvedores:
    return RegistroProvedores.carregar(caminho) if caminho else registro_padrao()


def provedor_para_cli(provedor: str, catalogo: Catalogo) -> tuple[Any, bool]:
    """(provedor, real). Sem chave => FALSO, com aviso em stderr."""
    if tem_chave(provedor):
        try:
            return criar_provedor_registrado(provedor, catalogo=catalogo), True
        except (ErroProvedor, ErroRegistro) as e:
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


def _erro(msg: str) -> int:
    print(f"ERRO: {msg}", file=sys.stderr)
    return 2


def _cmd_testar(a: Any) -> int:
    from robowin.ia.regressao import Faixas, rodar_regressao

    cat = _catalogo(a.catalogo)
    prov_nome, _ = dividir_modelo(a.modelo)
    entrada = cat.obter(a.modelo)  # recusa modelo fora do catalogo / sem preco / sem corte
    reg = registro_padrao()
    if prov_nome not in reg:
        return _erro(f"provedor '{prov_nome}' nao registrado em config/provedores.toml")
    if a.falso:
        prov, real = criar_provedor("falso", catalogo=None), False
        print("AVISO: --falso: provedor FALSO (resultados NAO sao do modelo real)", file=sys.stderr)
    elif not reg.chave_presente(prov_nome):
        return _erro(f"{reg.mensagem_sem_chave(prov_nome)} Para so ensaiar a bateria sem chave, use --falso.")
    else:
        try:
            prov, real = reg.criar(prov_nome, catalogo=cat), True
        except ErroProvedor as e:
            return _erro(str(e))
    res = asyncio.run(rodar_regressao(prov, a.modelo, perfil=perfil_do_cli(a), k=a.k, fixavel=entrada.fixavel, effort=a.effort,
                                      prazo_s=a.prazo, faixas=Faixas()))
    print(json.dumps({
        "modelo": res.modelo, "provedor_real": real, "rotulo": "" if real else "FALSO", "k": res.k,
        "rotulos_catalogo": list(cat.rotulos(a.modelo)),
        "modelos_servidos": res.modelos_servidos, "mudou_modelo_servido": res.mudou_modelo_servido,
        "alias_nao_fixavel": res.alias_nao_fixavel, "custo_usd": round(res.custo_usd, 6),
        "casos": {c: {"n": r.n, "validas": r.validas, "regras_ok": r.regras_ok, "acoes": r.acoes}
                  for c, r in res.por_caso.items()},
        "violacoes": res.violacoes,
    }, ensure_ascii=False, indent=2))
    return 0 if (res.ok or not real) else 1


def _cmd_listar(a: Any) -> int:
    reg = _registro(a.registro)
    cat = _catalogo(a.catalogo)
    modelos: dict[str, list[dict[str, Any]]] = {}
    for e in cat:
        ref = f"{e.provedor}:{e.id}"
        preco = (f"desconhecido (teto US$ {cat.teto_chamada(ref):.2f}/chamada)" if e.preco_desconhecido
                 else f"US$ {e.preco_entrada_mtok:g}/{e.preco_saida_mtok:g} por Mtok")
        corte = "desconhecido (contaminado em backtest historico)" if e.corte_desconhecido else e.corte_conhecimento
        modelos.setdefault(e.provedor, []).append({"modelo": ref, "fixavel": e.fixavel, "local": e.local,
                                                   "preco": preco, "corte": corte, "rotulos": list(cat.rotulos(ref))})
    provs = [{**d.resumo(), "modelos": [m["modelo"] for m in modelos.get(d.nome, [])]} for d in reg]
    orfaos = sorted(p for p in modelos if p not in reg)
    if a.json:
        print(json.dumps({"registro": reg.origem, "provedores": provs, "modelos": modelos,
                          "provedores_nao_registrados": orfaos}, ensure_ascii=False, indent=2))
        return 0
    print(f"Provedores ({reg.origem}); chave = presente/ausente (o valor NUNCA e exibido):")
    for d in provs:
        falta = f"  falta: {', '.join(d['faltam'])}" if d["faltam"] else ""
        print(f"  {d['nome']:<18} {d['adaptador']:<18} chave={d['chave']:<16} {'local ' if d['local'] else ''}"
              f"{d['base_url'] or '(url do SDK/regiao)'}{falta}")
    print("\nModelos do catalogo:")
    for ms in modelos.values():
        for m in ms:
            rot = f"  [{', '.join(m['rotulos'])}]" if m["rotulos"] else ""
            print(f"  {m['modelo']:<40} preco={m['preco']}; corte={m['corte']}{rot}")
    if orfaos:
        print(f"\nATENCAO: provedores no catalogo sem entrada em config/provedores.toml: {', '.join(orfaos)}")
    return 0


_RE_DATA = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def _toml_valor(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v))
    return json.dumps(str(v), ensure_ascii=False)  # string basica do TOML (aspas e escapes compativeis)


def entrada_de_argumentos(a: Any, local_registro: bool) -> dict[str, Any]:
    """Monta a entrada do catalogo a partir da CLI. ValueError com a mensagem clara se faltar declaracao."""
    prov_nome, mid = dividir_modelo(a.modelo)
    if (a.preco_entrada is None) != (a.preco_saida is None):
        raise ValueError("informe --preco-entrada E --preco-saida (US$ por milhao de tokens), ou nenhum dos dois")
    tem_preco = a.preco_entrada is not None
    local = bool(a.local or local_registro)
    if tem_preco and a.preco_desconhecido:
        raise ValueError("--preco-desconhecido nao combina com --preco-entrada/--preco-saida")
    if not tem_preco and not local and not a.preco_desconhecido:
        raise ValueError("modelo sem preco e recusado (falha segura). Informe --preco-entrada X --preco-saida Y, ou "
                         "declare --preco-desconhecido (cada chamada reserva um teto conservador do orcamento e o "
                         "manifesto rotula o modelo)")
    if a.corte and not _RE_DATA.match(a.corte):
        raise ValueError("--corte deve ser AAAA-MM-DD")
    bruta: dict[str, Any] = {"provedor": prov_nome, "id": mid, "fixavel": bool(a.fixavel)}
    if a.corte:
        bruta["corte_conhecimento"] = a.corte
    else:
        bruta["corte_desconhecido"] = True
    if tem_preco:
        bruta.update(preco_entrada_mtok=a.preco_entrada, preco_saida_mtok=a.preco_saida)
        if a.preco_cache is not None:
            bruta["preco_cache_mtok"] = a.preco_cache
    elif a.preco_desconhecido:
        bruta["preco_desconhecido"] = True
        if a.teto_chamada is not None:
            bruta["teto_chamada_usd"] = a.teto_chamada
    if local:
        bruta["local"] = True
    if a.saida_estruturada:
        bruta["saida_estruturada"] = a.saida_estruturada
    if a.notas:
        bruta["notas"] = a.notas
    return bruta


def _cmd_adicionar_modelo(a: Any) -> int:
    try:
        prov_nome, _ = dividir_modelo(a.modelo)
        reg = _registro(a.registro)
    except (ErroCatalogo, ErroRegistro) as e:
        return _erro(str(e))
    if prov_nome not in reg:
        return _erro(f"provedor '{prov_nome}' nao registrado. Adicione [provedor.{prov_nome}] em config/provedores.toml "
                     "(adaptador = \"openai_compativel\" + base_url) e rode de novo.")
    try:
        bruta = entrada_de_argumentos(a, reg.obter(prov_nome).local)
        validar_entrada(bruta)
        caminho = _caminho_catalogo(a.catalogo)
        cat = Catalogo.carregar(caminho)
    except (ValueError, ErroCatalogo) as e:
        return _erro(str(e))
    if a.modelo in cat:
        return _erro(f"{a.modelo} ja esta no catalogo ({caminho}); edite a entrada existente.")
    texto = caminho.read_text(encoding="utf-8")
    bloco = "\n[[modelo]]\n" + "".join(f"{k} = {_toml_valor(v)}\n" for k, v in bruta.items())
    novo = texto + ("" if texto.endswith("\n") or not texto else "\n") + bloco
    try:
        cat_novo = Catalogo.de_dict(carregar_toml_texto_tolerante(novo))  # o arquivo inteiro continua valido
    except (tomllib.TOMLDecodeError, ErroCatalogo) as e:
        return _erro(f"o catalogo ficaria invalido ({e}); nada foi gravado.")
    with open(caminho, "w", encoding="utf-8", newline="\n") as f:
        f.write(novo)
    print(f"{a.modelo} adicionado a {caminho}.")
    if not a.corte:
        print("AVISO: sem data de corte: o braco sera tratado como CONTAMINADO em qualquer backtest historico "
              "(so avaliacao prospectiva).")
    if bruta.get("preco_desconhecido"):
        print(f"AVISO: preco desconhecido: teto conservador de US$ {cat_novo.teto_chamada(a.modelo):.2f} "
              "por chamada no orcamento e rotulo 'preco_desconhecido' no manifesto.")
    if not reg.chave_presente(prov_nome):
        print(f"Proximo passo: {reg.mensagem_sem_chave(prov_nome)}")
    print(f"Teste: robowin ia testar --modelo {a.modelo}")
    return 0


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
    p = sub.add_parser("ia", help="ferramentas de IA (provedores, modelos, testes)")
    sp = p.add_subparsers(dest="ia_cmd")
    t = sp.add_parser("testar", help="bateria de regressao contra um provedor:modelo (falha clara sem chave)")
    t.add_argument("--modelo", required=True, help="provedor:modelo (ex.: anthropic:claude-opus-5-5)")
    t.add_argument("--k", type=int, default=3)
    t.add_argument("--effort", default=None)
    t.add_argument("--prazo", type=float, default=40.0)
    t.add_argument("--catalogo", default=None)
    t.add_argument("--falso", action="store_true", help="roda com o provedor FALSO (rotulado), sem chave e sem rede")
    argumentos_ativo(t)
    t.set_defaults(func=_cmd_testar)

    li = sp.add_parser("listar", help="provedores, modelos e chave presente/ausente (nunca o valor)")
    li.add_argument("--catalogo", default=None)
    li.add_argument("--registro", default=None, help="registro de provedores (padrao: config/provedores.toml)")
    li.add_argument("--json", action="store_true")
    li.set_defaults(func=_cmd_listar)

    ad = sp.add_parser("adicionar-modelo", help="cadastra provedor:modelo no catalogo (qualquer modelo, com declaracao)")
    ad.add_argument("modelo", help="provedor:modelo (o provedor precisa estar em config/provedores.toml)")
    ad.add_argument("--preco-entrada", type=float, default=None, help="US$ por milhao de tokens de entrada")
    ad.add_argument("--preco-saida", type=float, default=None, help="US$ por milhao de tokens de saida")
    ad.add_argument("--preco-cache", type=float, default=None, help="US$ por milhao de tokens lidos do cache")
    ad.add_argument("--preco-desconhecido", action="store_true",
                    help="sem preco: teto conservador de orcamento por chamada e rotulo no manifesto")
    ad.add_argument("--teto-chamada", type=float, default=None,
                    help="teto por chamada em US$ (com --preco-desconhecido; padrao do catalogo se omitido)")
    ad.add_argument("--corte", default=None,
                    help="data de corte de conhecimento AAAA-MM-DD (sem ela: contaminado em qualquer backtest historico)")
    ad.add_argument("--local", action="store_true", help="modelo local (custo de API zero)")
    ad.add_argument("--fixavel", action="store_true", help="o id e um snapshot fixo (a versao servida nao muda)")
    ad.add_argument("--saida-estruturada", choices=["nativa", "json_schema", "json"], default=None)
    ad.add_argument("--notas", default="")
    ad.add_argument("--catalogo", default=None)
    ad.add_argument("--registro", default=None)
    ad.set_defaults(func=_cmd_adicionar_modelo)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])

    la = sub.add_parser("latencia", help="mede TTFT/total p50/p95/p99 dos modelos (reais so com chave)")
    la.add_argument("--modelos", required=True, help="lista separada por virgula de provedor:modelo")
    la.add_argument("--n", type=int, default=20)
    la.add_argument("--effort", default=None)
    la.add_argument("--prazo", type=float, default=60.0)
    la.add_argument("--catalogo", default=None)
    la.set_defaults(func=_cmd_latencia)
