"""Comandos `robowin risco ...` (aprovacao da politica de risco pelo USUARIO) e `robowin usuario ...`.

Sem `--usuario/--conta`: modo de ARQUIVO UNICO (config/risco.toml + config/aprovacoes/), como sempre.
Com `--usuario U --conta PLATAFORMA:SERVIDOR:LOGIN`: limites isolados daquele usuario e daquela conta
(`risco.usuarios`); um usuario novo comeca SEM limites e fica bloqueado para operar ate definir e aprovar.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..auditoria.cadeia import RegistradorAuditoria
from ..core.config import ErroConfig, carregar_toml, dir_dados
from .aprovar import (
    PALAVRA_APROVACAO,
    aprovar_politica,
    descrever_limites,
    estado_aprovacao,
)


def _ler(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # stdin fechado (uso nao interativo sem --sim-eu-aprovo): conta como recusa
        return ""


def _dir_auditoria(a: argparse.Namespace) -> Path:
    return Path(a.auditoria) if getattr(a, "auditoria", None) else dir_dados(a.config) / "auditoria"


def _selecao(a: argparse.Namespace) -> Any:
    from .usuarios import selecao_de
    return selecao_de(a.config, a.usuario, a.conta, a.usuarios)


def _armazem(a: argparse.Namespace) -> Any:
    from .usuarios import ArmazemUsuarios, dir_usuarios
    return ArmazemUsuarios(dir_usuarios(a.config, a.usuarios), RegistradorAuditoria(_dir_auditoria(a)))


def _confirmador(a: argparse.Namespace, titulo: str, entrada: Callable[[str], str]) -> Callable[[list[str]], str]:
    def confirmar(resumo: list[str]) -> str:
        print(f"=== {titulo} ===")
        print("\n".join(resumo))
        print()
        if a.sim_eu_aprovo:
            print(f"--sim-eu-aprovo informado: equivale a digitar {PALAVRA_APROVACAO}.")
            return PALAVRA_APROVACAO
        return entrada(f"Digite {PALAVRA_APROVACAO} para aprovar (qualquer outra coisa cancela): ")
    return confirmar


def _cmd_aprovar(a: argparse.Namespace, entrada: Callable[[str], str] = _ler) -> int:
    try:
        sel = _selecao(a)
        if sel.ativa:
            arm = _armazem(a)
            res = arm.aprovar(sel.usuario, sel.chave, a.responsavel or "", a.motivo or "", confirmar=_confirmador(
                a, f"Aprovacao dos limites de risco de {sel.descricao()} (SOMENTE DEMO; voce assume estes limites)",
                entrada))
        else:
            res = aprovar_politica(a.risco, a.aprovacoes, a.responsavel or "", a.motivo or "", confirmar=_confirmador(
                a, "Aprovacao da politica de risco (SIMULACAO/DEMO; voce assume estes limites)", entrada),
                auditoria=RegistradorAuditoria(_dir_auditoria(a)))
    except ErroConfig as e:  # AprovacaoRecusada, usuario/conta inexistente, conta real...
        print(f"RECUSADO: {e}")
        return 1
    print(f"APROVADO: registro gravado em {res.arquivo} (sha256 {res.sha256[:16]}...).")
    if res.versao_anterior is not None:
        print(f"O registro anterior ({res.versao_anterior.name}) fica como historico; vale a versao mais recente valida.")
    print("Evento 'risco_aprovado' gravado na cadeia de auditoria. Confira com: uv run robowin verificar --somente risco")
    print("Se alterar os limites, o hash deixa de conferir e e preciso aprovar de novo.")
    return 0


def _cmd_status(a: argparse.Namespace) -> int:
    try:
        sel = _selecao(a)
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    if sel.ativa:
        try:
            st = sel.armazem().estado(sel.usuario, sel.chave)
        except ErroConfig as e:
            print(f"ERRO: {e}")
            return 2
        prov = " (PROVISORIA, somente DEMO)" if st.provisoria else ""
        print(f"Risco de {sel.descricao()} [conta {st.tipo.upper()}]: {st.estado}{prov} - {st.detalhe}")
        print(f"Autorizacao para conta REAL: {st.autorizacao_real.upper()} (nesta versao sempre bloqueada; o envio "
              "real nao existe)")
        if st.estado != "APROVADO":
            print(f"Para aprovar: uv run robowin risco aprovar --usuario {st.usuario} --conta {st.conta} "
                  '--responsavel "<seu nome>" --motivo "<motivo>"  (ou no painel, pagina Risco)')
        return 0 if st.pode_operar_demo else 1
    estado, detalhe = estado_aprovacao(a.risco, a.aprovacao)
    print(f"Politica de risco (modo arquivo unico): {estado} - {detalhe}")
    if estado != "APROVADO":
        print('Para aprovar: uv run robowin risco aprovar --responsavel "<seu nome>" --motivo "<motivo>"')
    return 0 if estado == "APROVADO" else 1


def _cmd_definir(a: argparse.Namespace) -> int:
    """Define/altera os limites do usuario+conta (invalida a aprovacao anterior; exige aprovar de novo)."""
    from robowin.core.instrumentos import perfil_da_config

    from .usuarios import CAMPOS_RISCO, modelo_inicial, texto_risco
    try:
        sel = _selecao(a)
        if not sel.ativa:
            print("ERRO: `risco definir` exige --usuario e --conta (no modo arquivo unico, edite config/risco.toml).")
            return 2
        arm = _armazem(a)
        atual = arm.limites(sel.usuario, sel.chave)
        valores: dict[str, Any] = {}
        if a.modelo_inicial:
            perfil = perfil_da_config(carregar_toml(a.config) if Path(a.config).is_file() else {}, a.ativo,
                                      permitir_a_confirmar=True)
            valores = modelo_inicial(perfil).model_dump()
        elif a.arquivo:
            valores = dict(carregar_toml(a.arquivo))
        elif atual is not None:
            valores = atual.model_dump()
        for item in a.campo or []:
            nome, sep, valor = item.partition("=")
            if not sep or nome.strip() not in CAMPOS_RISCO:
                print(f"ERRO: --campo '{item}' invalido (use NOME=VALOR; nomes: {', '.join(CAMPOS_RISCO)})")
                return 2
            valores[nome.strip()] = valor.strip()
        if not valores:
            print("ERRO: informe --modelo-inicial, --arquivo ou --campo NOME=VALOR (nenhum limite e imposto por padrao).")
            return 2
        sha = arm.gravar_limites(sel.usuario, sel.chave, {k: str(v) for k, v in valores.items()},
                                 autor=a.autor or sel.usuario, origem="cli")
    except ErroConfig as e:
        print(f"RECUSADO: {e}")
        return 1
    novos = arm.limites(sel.usuario, sel.chave)
    print(f"Limites de {sel.descricao()} gravados (sha256 {sha[:16]}...); evento na auditoria.")
    print("\n".join(descrever_limites(novos)))
    print("\nAINDA NAO APROVADOS: operar continua BLOQUEADO ate `robowin risco aprovar --usuario "
          f"{sel.usuario} --conta {sel.chave.texto} --responsavel \"<seu nome>\"`.")
    if a.mostrar_toml:
        print(texto_risco(novos))
    return 0


def _cmd_historico(a: argparse.Namespace) -> int:
    try:
        sel = _selecao(a)
        if not sel.ativa:
            print("ERRO: `risco historico` exige --usuario e --conta.")
            return 2
        arm = sel.armazem()
        print(f"Historico de {sel.descricao()}")
        print("Versoes dos limites:")
        for h in arm.historico_limites(sel.usuario, sel.chave):
            print(f"  #{h['seq']} {h['gravado_em_utc']} sha256 {h['sha256'][:16]}... por {h['autor']} ({h['origem']}); "
                  f"alterados: {', '.join(h.get('alterados', {}))}")
        print("Aprovacoes:")
        for ap in arm.aprovacoes(sel.usuario, sel.chave):
            print(f"  {ap['arquivo']} {ap.get('data')} {ap.get('responsavel')} sha256 {str(ap.get('sha256'))[:16]}..."
                  f"{' [VIGENTE]' if ap['vigente'] else ''}{' [PROVISORIA DEMO]' if ap.get('provisoria') else ''}")
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    return 0


# ============================================================================ robowin usuario ==========
def _cmd_usuario_criar(a: argparse.Namespace) -> int:
    try:
        token = _armazem(a).criar_usuario(a.usuario, a.nome or "")
    except ErroConfig as e:
        print(f"RECUSADO: {e}")
        return 1
    print(f"Usuario '{a.usuario}' criado SEM limites de risco (operar fica BLOQUEADO ate definir e aprovar).")
    print(f"Token do usuario (guarde agora; so o hash foi gravado e ele NAO sera mostrado de novo):\n  {token}")
    print(f"Proximo passo: uv run robowin usuario conta --usuario {a.usuario} --conta mt5:<SERVIDOR>:<LOGIN>")
    return 0


def _cmd_usuario_conta(a: argparse.Namespace) -> int:
    from .usuarios import ChaveConta
    try:
        c = _armazem(a).registrar_conta(a.usuario, ChaveConta.de_texto(a.conta), a.tipo)
    except ErroConfig as e:
        print(f"RECUSADO: {e}")
        return 1
    print(f"Conta {c.chave.texto} ({c.tipo.upper()}) registrada para '{c.usuario}', SEM limites. Defina e aprove no "
          "painel (Risco) ou com `robowin risco definir` + `robowin risco aprovar`.")
    if c.tipo == "real":
        print("Conta REAL: autorizacao_real = bloqueada (nesta versao nenhuma operacao em conta real e possivel).")
    return 0


def _cmd_usuario_listar(a: argparse.Namespace) -> int:
    from .usuarios import ArmazemUsuarios, dir_usuarios
    arm = ArmazemUsuarios(dir_usuarios(a.config, a.usuarios))
    nomes = arm.usuarios()
    if not nomes:
        print(f"Nenhum usuario em {arm.raiz}: vale o modo arquivo unico (config/risco.toml).")
        return 0
    for u in nomes:
        print(u)
        for c in arm.contas(u):
            st = arm.estado(u, c.chave)
            print(f"  {c.chave.texto} [{c.tipo}] risco {st.estado}{' (provisoria DEMO)' if st.provisoria else ''}; "
                  f"autorizacao_real {st.autorizacao_real}")
    return 0


def _cmd_usuario_migrar(a: argparse.Namespace, entrada: Callable[[str], str] = _ler) -> int:
    from .usuarios import ChaveConta
    try:
        destino = _armazem(a).migrar_legado(a.usuario, ChaveConta.de_texto(a.conta), a.risco, a.aprovacao,
                                            confirmar=_confirmador(a, "Migracao da aprovacao do arquivo unico", entrada))
    except ErroConfig as e:
        print(f"RECUSADO: {e}")
        return 1
    print(f"MIGRADO: {destino} (aprovacao PROVISORIA, somente DEMO; evento 'risco_migrado_legado' na auditoria).")
    return 0


def _comuns(p: argparse.ArgumentParser, *, conta_obrigatoria: bool = False) -> None:
    from .usuarios import adicionar_args
    adicionar_args(p)
    p.add_argument("--config", default="config/robowin.toml", help="para achar [geral].dados (auditoria) e [geral].usuarios")
    if conta_obrigatoria:
        for ac in p._actions:
            if ac.dest in ("usuario", "conta"):
                ac.required = True


def registrar(sub: Any) -> None:
    p = sub.add_parser("risco", help="limites de risco: definir, aprovar (APROVO), status e historico (por usuario/conta)")
    s = p.add_subparsers(dest="acao_risco")

    ap = s.add_parser("aprovar", help="mostra os limites e grava novo registro de aprovacao (exige digitar APROVO)")
    ap.add_argument("--responsavel", required=True, help="nome de quem assume o risco (nao pode ser vazio nem 'pendente')")
    ap.add_argument("--motivo", default="", help="por que estes limites estao sendo aprovados")
    ap.add_argument("--sim-eu-aprovo", action="store_true",
                    help="uso NAO interativo (scripts): equivale a digitar APROVO; o resumo e impresso do mesmo jeito")
    ap.add_argument("--risco", default="config/risco.toml", help="modo arquivo unico")
    ap.add_argument("--aprovacoes", default="config/aprovacoes", help="modo arquivo unico: pasta dos risco-vN.toml")
    ap.add_argument("--auditoria", default=None, help="pasta da cadeia de auditoria (padrao: <dados>/auditoria)")
    _comuns(ap)
    ap.set_defaults(func=_cmd_aprovar)

    st = s.add_parser("status", help="estado da aprovacao efetiva (usuario+conta ou arquivo unico)")
    st.add_argument("--risco", default="config/risco.toml")
    st.add_argument("--aprovacao", default="config/aprovacoes/risco-v1.toml")
    _comuns(st)
    st.set_defaults(func=_cmd_status)

    de = s.add_parser("definir", help="define/altera os limites do usuario+conta (invalida a aprovacao anterior)")
    de.add_argument("--modelo-inicial", action="store_true",
                    help="parte do modelo MUITO conservador derivado do perfil do ativo (para voce editar e aprovar)")
    de.add_argument("--arquivo", default=None, help="TOML com TODOS os campos de limite")
    de.add_argument("--campo", action="append", metavar="NOME=VALOR", help="altera um campo (repetivel)")
    de.add_argument("--ativo", default=None, help="perfil do --modelo-inicial (padrao: [geral].simbolo_base)")
    de.add_argument("--autor", default=None)
    de.add_argument("--mostrar-toml", action="store_true")
    de.add_argument("--auditoria", default=None)
    _comuns(de, conta_obrigatoria=True)
    de.set_defaults(func=_cmd_definir)

    hi = s.add_parser("historico", help="versoes dos limites e aprovacoes do usuario+conta")
    _comuns(hi, conta_obrigatoria=True)
    hi.set_defaults(func=_cmd_historico)
    p.set_defaults(func=lambda _a: (p.print_help(), 2)[1])

    u = sub.add_parser("usuario", help="usuarios e contas de negociacao (limites de risco isolados por usuario+conta)")
    us = u.add_subparsers(dest="acao_usuario")
    cr = us.add_parser("criar", help="cria usuario SEM limites (bloqueado ate aprovar); mostra o token uma unica vez")
    cr.add_argument("--nome", default="")
    cr.add_argument("--auditoria", default=None)
    _comuns(cr)
    cr.set_defaults(func=_cmd_usuario_criar)
    for ac in cr._actions:
        if ac.dest == "usuario":
            ac.required = True
    co = us.add_parser("conta", help="registra conta de negociacao (PLATAFORMA:SERVIDOR:LOGIN) do usuario")
    co.add_argument("--tipo", choices=["demo", "real"], default="demo",
                    help="real: registrada mas SEMPRE bloqueada (autorizacao_real inexistente nesta versao)")
    co.add_argument("--auditoria", default=None)
    _comuns(co, conta_obrigatoria=True)
    co.set_defaults(func=_cmd_usuario_conta)
    li = us.add_parser("listar", help="usuarios, contas e estado do risco")
    _comuns(li)
    li.set_defaults(func=_cmd_usuario_listar)
    mi = us.add_parser("migrar-legado",
                       help="copia a aprovacao do arquivo unico (ex.: risco-v2 do titular) para UMA conta DEMO, como "
                            "PROVISORIA somente DEMO (exige APROVO)")
    mi.add_argument("--risco", default="config/risco.toml")
    mi.add_argument("--aprovacao", default="config/aprovacoes/risco-v1.toml")
    mi.add_argument("--sim-eu-aprovo", action="store_true")
    mi.add_argument("--auditoria", default=None)
    _comuns(mi, conta_obrigatoria=True)
    mi.set_defaults(func=_cmd_usuario_migrar)
    u.set_defaults(func=lambda _a: (u.print_help(), 2)[1])
