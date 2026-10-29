"""Pre-registro do experimento (docs/16 secao 4.3): cadeia de hash so de acrescimo em ``config/experimentos``.

Cada registro grava ``<id>.toml`` (o ``PreRegistro`` legivel) e acrescenta uma linha em ``registro.jsonl`` com
``hash_registro = sha256(hash_anterior + json canonico do registro)``, no estilo de ``auditoria/cadeia.py``. A
primeira linha encadeia em ``GENESIS``. ``verificar`` recalcula a cadeia inteira e confere cada ``<id>.toml``
contra a linha do JSONL: qualquer adulteracao (linha editada, inclusive o envelope, removida, reordenada ou TOML
divergente) levanta
``ErroPreRegistro``.

``elegivel`` diz se um pregao pode entrar na avaliacao confirmatoria do experimento (o selo do relatorio usa isto):
so pregoes POSTERIORES ao registro (e a partir de ``inicio_avaliacao``), com os hashes de todas as variantes iguais
aos registrados e sem o provedor FALSO. Nada aqui le ou altera limites de risco.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robowin.core.tipos import PreRegistro
from robowin.risco.tempo import dia_sp

GENESIS = "0" * 64
ARQUIVO_CADEIA = "registro.jsonl"
DIR_PADRAO = Path("config/experimentos")
RE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
# chaves de cada linha do JSONL: o envelope repete campos do registro para leitura; o hash so cobre ``registro``,
# entao ``verificar`` exige que o envelope tenha exatamente estas chaves e espelhe o registro encadeado
CHAVES_LINHA = frozenset({"n", "id_experimento", "t_registro_ns", "registro", "hash_anterior", "hash_registro"})


class ErroPreRegistro(ValueError):
    """Cadeia de pre-registro quebrada, registro duplicado ou invalido."""


# ============================================================================================ hash
def _corpo(pr: PreRegistro) -> dict[str, Any]:
    """Campos cobertos pelo hash: tudo menos o proprio ``hash_registro`` (``hash_anterior`` incluido)."""
    return pr.model_dump(mode="json", exclude={"hash_registro"})


def calcular_hash(pr: PreRegistro) -> str:
    """sha256(hash_anterior + json canonico). Estavel: nao depende da ordem de insercao dos dicionarios."""
    canon = json.dumps(_corpo(pr), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((pr.hash_anterior + canon).encode("utf-8")).hexdigest()


# ============================================================================================ TOML
def _toml_valor(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_valor(x) for x in v) + "]"
    raise TypeError(f"valor sem representacao TOML: {type(v).__name__}")


def para_toml(pr: PreRegistro) -> str:
    d = pr.model_dump(mode="json")
    tabelas = {k: v for k, v in d.items() if isinstance(v, dict)}
    linhas = ["# Pre-registro do experimento (gerado por `robowin experimento registrar`; NAO editar a mao:",
              "# a cadeia em registro.jsonl detecta qualquer alteracao).", ""]
    linhas += [f"{k} = {_toml_valor(v)}" for k, v in d.items() if k not in tabelas]
    for nome, tab in tabelas.items():
        linhas += ["", f"[{nome}]"]
        linhas += [f"{json.dumps(str(k), ensure_ascii=False)} = {_toml_valor(v)}" for k, v in sorted(tab.items())]
    return "\n".join(linhas) + "\n"


def ler_toml(arq: Path) -> PreRegistro:
    with open(arq, "rb") as f:
        return PreRegistro.model_validate(tomllib.load(f))


# ============================================================================================ cadeia
def _linhas(dir: Path) -> list[dict[str, Any]]:
    arq = Path(dir) / ARQUIVO_CADEIA
    if not arq.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(arq, encoding="utf-8") as f:
        for i, ln in enumerate(f):
            if not ln.strip():
                continue
            try:
                reg = json.loads(ln)
            except ValueError as e:
                raise ErroPreRegistro(f"{arq.name} linha {i}: JSON invalido ({e})") from e
            if not isinstance(reg, dict):
                raise ErroPreRegistro(f"{arq.name} linha {i}: registro nao e um objeto")
            out.append(reg)
    return out


def verificar(dir: Path = DIR_PADRAO) -> list[PreRegistro]:
    """Recalcula a cadeia; devolve os registros em ordem. Diretorio sem ``registro.jsonl`` => lista vazia."""
    dir = Path(dir)
    prev, out, ids = GENESIS, [], set()
    t_ant = -1
    for i, reg in enumerate(_linhas(dir)):
        if reg.get("n") != i:
            raise ErroPreRegistro(f"linha {i}: sequencia {reg.get('n')} != {i}")
        try:
            pr = PreRegistro.model_validate(reg.get("registro"))
        except ValueError as e:
            raise ErroPreRegistro(f"linha {i}: registro invalido ({e})") from e
        if pr.hash_anterior != prev:
            raise ErroPreRegistro(f"linha {i} ({pr.id_experimento}): hash_anterior nao encadeia")
        h = calcular_hash(pr)
        if h != pr.hash_registro or reg.get("hash_registro") != h:
            raise ErroPreRegistro(f"linha {i} ({pr.id_experimento}): hash invalido (registro adulterado)")
        if set(reg) != CHAVES_LINHA:
            raise ErroPreRegistro(f"linha {i} ({pr.id_experimento}): chaves da linha adulteradas "
                                  f"({sorted(set(reg) ^ CHAVES_LINHA)})")
        envelope = {"id_experimento": pr.id_experimento, "t_registro_ns": pr.t_registro_ns,
                    "hash_anterior": pr.hash_anterior}
        divergentes = sorted(k for k, v in envelope.items() if reg.get(k) != v)
        if divergentes:
            raise ErroPreRegistro(f"linha {i} ({pr.id_experimento}): envelope diverge do registro encadeado "
                                  f"({', '.join(divergentes)})")
        if pr.id_experimento in ids:
            raise ErroPreRegistro(f"linha {i}: id repetido {pr.id_experimento}")
        if pr.t_registro_ns < t_ant:
            raise ErroPreRegistro(f"linha {i} ({pr.id_experimento}): t_registro_ns anterior ao registro anterior")
        toml = dir / f"{pr.id_experimento}.toml"
        if not toml.exists():
            raise ErroPreRegistro(f"{toml.name} ausente (registro {i})")
        try:
            no_toml = ler_toml(toml)
        except (ValueError, tomllib.TOMLDecodeError) as e:
            raise ErroPreRegistro(f"{toml.name} ilegivel: {e}") from e
        if no_toml != pr:
            raise ErroPreRegistro(f"{toml.name} diverge do registro encadeado")
        ids.add(pr.id_experimento)
        t_ant = pr.t_registro_ns
        prev = h
        out.append(pr)
    return out


def registrar(pr: PreRegistro, dir: Path = DIR_PADRAO) -> PreRegistro:
    """Encadeia ``pr`` no fim da cadeia (verificada antes) e grava ``<id>.toml``. Id repetido e erro: qualquer
    mudanca de prompt ou parametro e uma variante nova, com registro novo."""
    dir = Path(dir)
    if not RE_ID.match(pr.id_experimento):
        raise ErroPreRegistro(f"id de experimento invalido: {pr.id_experimento!r} (letras, digitos, . _ -)")
    if not pr.variantes:
        raise ErroPreRegistro("pre-registro sem variantes")
    existentes = verificar(dir)
    if any(e.id_experimento == pr.id_experimento for e in existentes):
        raise ErroPreRegistro(f"experimento {pr.id_experimento} ja registrado")
    toml = dir / f"{pr.id_experimento}.toml"
    if toml.exists():
        raise ErroPreRegistro(f"{toml} ja existe fora da cadeia: nao sobrescrevo")
    if existentes and pr.t_registro_ns < existentes[-1].t_registro_ns:
        raise ErroPreRegistro("t_registro_ns anterior ao ultimo registro da cadeia")
    anterior = existentes[-1].hash_registro if existentes else GENESIS
    base = pr.model_copy(update={"hash_anterior": anterior, "hash_registro": ""})
    final = base.model_copy(update={"hash_registro": calcular_hash(base)})
    final = PreRegistro.model_validate(final.model_dump())  # revalida (model_copy nao valida)
    dir.mkdir(parents=True, exist_ok=True)
    tmp = toml.with_suffix(".parcial")
    tmp.write_text(para_toml(final), encoding="utf-8")
    if ler_toml(tmp) != final:  # o TOML precisa reproduzir exatamente o registro
        tmp.unlink()
        raise ErroPreRegistro("TOML gerado nao reproduz o registro (valor nao representavel)")
    tmp.replace(toml)
    linha = {"n": len(existentes), "id_experimento": final.id_experimento, "t_registro_ns": final.t_registro_ns,
             "registro": final.model_dump(mode="json"), "hash_anterior": anterior,
             "hash_registro": final.hash_registro}
    with open(dir / ARQUIVO_CADEIA, "a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False, sort_keys=True) + "\n")
    return final


def n_tentativas(dir: Path = DIR_PADRAO) -> int:
    """Total de variantes registradas na cadeia (pares nome+hash distintos): o numero de tentativas do DSR."""
    return len({(nome, h) for pr in verificar(dir) for nome, h in pr.variantes.items()})


# ======================================================================================= elegibilidade
def data_registro(pr: PreRegistro) -> dt.date:
    """Data (America/Sao_Paulo) do instante de registro."""
    return dia_sp(pr.t_registro_ns)


def elegivel(pr: PreRegistro, *, data_pregao: str, hashes_variantes: Mapping[str, str],
             provedor_falso: bool) -> tuple[bool, tuple[str, ...]]:
    """(elegivel, motivos). Recusa: registro com hash invalido, pregao com data <= registro ou antes de
    ``inicio_avaliacao``, variante registrada sem hash ou com hash divergente, variante nao registrada, FALSO."""
    motivos: list[str] = []
    if not pr.hash_registro or calcular_hash(pr) != pr.hash_registro:
        motivos.append("registro_sem_hash_valido")
    try:
        d = dt.date.fromisoformat(str(data_pregao)[:10])
    except ValueError:
        motivos.append("data_pregao_invalida")
        d = None
    if d is not None:
        if d <= data_registro(pr):
            motivos.append("pregao_nao_posterior_ao_registro")
        try:
            inicio = dt.date.fromisoformat(pr.inicio_avaliacao[:10])
        except ValueError:
            motivos.append("inicio_avaliacao_invalido")
        else:
            if d < inicio:
                motivos.append("pregao_antes_do_inicio_da_avaliacao")
    for nome, h in sorted(pr.variantes.items()):
        if nome not in hashes_variantes:
            motivos.append(f"variante_sem_hash:{nome}")
        elif str(hashes_variantes[nome]) != h:
            motivos.append(f"hash_divergente:{nome}")
    motivos += [f"variante_nao_registrada:{n}" for n in sorted(set(hashes_variantes) - set(pr.variantes))]
    if provedor_falso:
        motivos.append("provedor_falso")
    return (not motivos, tuple(motivos))


# ================================================================================ hashes das variantes
# rotulos de versao do CODIGO (docs/16): o hash e o da FONTE (e dos esquemas), nunca o do texto do rotulo
VERSOES_FIXAS = ("dsl_gestao:v1", "compilador:v1", "correcoes:v1.1")
PROMPTS = {"planejador": "planejador_v1.md", "revisor_plano": "revisor_plano_v1.md"}
# linha de base da H1 (C condicional; B usa o mesmo proponente): prompts e esquemas do proponente e do revisor
PROMPTS_LINHA_BASE = {"proponente": "proponente_v1.md", "revisor": "revisor_v1.md"}
COMPONENTES_SO_REVISOR = ("prompt:revisor_plano", "esquema:RevisaoPlanoIA")
COMPONENTES_LINHA_BASE = ("prompt:proponente", "prompt:revisor", "esquema:PropostaIA", "esquema:RevisaoIA",
                          "codigo:linha_de_base")
SEED_PADRAO = 7  # mesma semente padrao do historico (``ParamsHistorico.seed``)


def _sha(texto: str | bytes) -> str:
    return hashlib.sha256(texto.encode("utf-8") if isinstance(texto, str) else texto).hexdigest()


def _sha_json(v: Any) -> str:
    return _sha(json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str))


def _sha_fonte(modulo: str) -> str:
    """sha256 da fonte de ``modulo`` (quebras de linha normalizadas: o mesmo codigo da o mesmo hash no Windows)."""
    import importlib
    arq = getattr(importlib.import_module(modulo), "__file__", None)
    if not arq or not Path(arq).is_file():
        return "ausente"
    return _sha(Path(arq).read_bytes().replace(b"\r\n", b"\n"))


def hashes_codigo() -> dict[str, str]:
    """Hashes do CODIGO pre-registrado (os mesmos do manifesto da simulacao): DSL de gestao (esquemas + fonte de
    ``decisao/gestao.py``), compilador (versao + fonte), ``correcoes:v1.1`` (lista + fonte dos modulos que as
    implementam), regra da sombra trailing (regras + fonte de ``bracos/sombra.py``) e o codigo da linha de base."""
    from robowin.avaliacao.contrafactual import TRAILING_FIXO
    from robowin.core.tipos import PlanoGestaoCompilado, RegraCompilada, RegraGestao
    from robowin.decisao import compilador_plano
    from robowin.decisao.bracos.sombra import REGRA_TRAILING_FIXO
    from robowin.decisao.correcoes import CORRECOES_V11, MODULOS_CORRECOES_V11

    esquemas = {n: m.model_json_schema() for n, m in (("RegraGestao", RegraGestao), ("RegraCompilada", RegraCompilada),
                                                      ("PlanoGestaoCompilado", PlanoGestaoCompilado))}
    return {
        "dsl_gestao:v1": _sha_json({"esquemas": esquemas, "fonte": _sha_fonte("robowin.decisao.gestao")}),
        "compilador:v1": _sha_json({"versao": compilador_plano.VERSAO_COMPILADOR,
                                    "fonte": _sha_fonte("robowin.decisao.compilador_plano")}),
        "correcoes:v1.1": _sha_json({"correcoes": list(CORRECOES_V11),
                                     "fontes": {m: _sha_fonte(m) for m in MODULOS_CORRECOES_V11}}),
        "regra:sombra_trailing": _sha_json({"sombra": REGRA_TRAILING_FIXO, "contrafactual": TRAILING_FIXO,
                                            "fonte": _sha_fonte("robowin.decisao.bracos.sombra")}),
        "codigo:linha_de_base": _sha_json({m: _sha_fonte(m) for m in (
            "robowin.decisao.bracos.llm", "robowin.decisao.bracos.claude", "robowin.decisao.bracos.claude_gpt")}),
    }


def hashes_componentes(raiz_config: Path = Path("config"), *, seed: int = SEED_PADRAO) -> dict[str, str]:
    """sha256 de cada componente pre-registrado (docs/16 4.3): prompts e esquemas do planejador e do revisor do
    plano, secoes ``[planejador]``, ``[planejador.gatilhos]`` e ``[prazos].planejador`` de ``robowin.toml``, o
    CODIGO (``hashes_codigo``: DSL, compilador, correcoes v1.1, regra da sombra trailing), as seeds e a linha de
    base da H1 (prompts, esquemas e codigo de B/C). Arquivo ou secao ausente entra como ``ausente``."""
    from robowin.ia.esquemas import ESQUEMAS_POR_PAPEL

    raiz = Path(raiz_config)
    out: dict[str, str] = {}
    for papel, arq in {**PROMPTS, **PROMPTS_LINHA_BASE}.items():
        p = raiz / "prompts" / arq
        out[f"prompt:{papel}"] = _sha(p.read_bytes()) if p.is_file() else "ausente"
    for papel in ("planejador", "revisor_plano", "proponente", "revisor"):
        esq = ESQUEMAS_POR_PAPEL.get(papel)
        out[f"esquema:{esq.__name__}" if esq else f"esquema:{papel}"] = (
            _sha_json(esq.model_json_schema()) if esq else "ausente")
    cfg: dict[str, Any] = {}
    toml = raiz / "robowin.toml"
    if toml.is_file():
        with open(toml, "rb") as f:
            cfg = tomllib.load(f)
    plan = dict(cfg.get("planejador") or {})
    gat = plan.pop("gatilhos", None)
    prazos = (cfg.get("prazos") or {}).get("planejador")
    out["config:planejador"] = _sha_json(plan) if plan else "ausente"
    out["config:planejador.gatilhos"] = _sha_json(gat) if gat is not None else "ausente"
    out["config:prazos.planejador"] = _sha_json(prazos) if prazos is not None else "ausente"
    out.update(hashes_codigo())
    out["seeds"] = _sha_json({"seed": int(seed)})
    return out


def variantes_padrao(componentes: Mapping[str, str]) -> dict[str, str]:
    """Hash composto de cada variante: P (sem o revisor do plano nem a linha de base), PC (P + revisor do plano) e
    a linha de base da H1, ``claude_gpt_cond`` (prompts, esquemas e codigo de B/C, correcoes v1.1 e seeds): mudar
    o prompt de C depois do registro tambem diverge."""
    base = {k: v for k, v in componentes.items() if k not in COMPONENTES_LINHA_BASE}
    p = {k: v for k, v in base.items() if k not in COMPONENTES_SO_REVISOR}
    c = {k: componentes[k] for k in (*COMPONENTES_LINHA_BASE, "correcoes:v1.1", "seeds") if k in componentes}
    return {"planejador": _sha_json(p), "planejador_revisor": _sha_json(base), "claude_gpt_cond": _sha_json(c)}
