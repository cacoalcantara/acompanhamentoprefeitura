"""Pagina "Risco" do painel: cada USUARIO define, altera e aprova os PROPRIOS limites, por CONTA de negociacao.

* Identificacao: usuario + token do usuario (`robowin usuario criar`; so o hash fica gravado). A sessao (cookie
  aleatorio, em memoria) so ve e edita as contas DAQUELE usuario: o usuario vem sempre da sessao, nunca do formulario.
* Gravar limites e registrar conta exigem tambem o token de ACOES do painel (ROBOWIN_TOKEN), como a emergencia.
* Aprovar exige digitar APROVO + o token de acoes no formulario (nao vale o cookie) + responsavel.
* Pre-visualizar so valida (validar_coerencia) e mostra os limites em pt-BR: nada e gravado.
* Toda gravacao (conta, limites, aprovacao) vai para a cadeia de auditoria (<dados>/auditoria).
"""
from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from robowin.core.config import ErroConfig
from robowin.core.instrumentos import PerfilInstrumento
from robowin.risco.aprovar import PALAVRA_APROVACAO, descrever_limites
from robowin.risco.usuarios import (
    CAMPOS_RISCO,
    ArmazemUsuarios,
    ChaveConta,
    config_de_formulario,
    modelo_inicial,
)

COOKIE_USUARIO = "robowin_sessao_usuario"

ROTULOS = {
    "contratos_max": "Contratos por operação (máximo; em forex/CFD: lotes mínimos)",
    "perda_max_operacao_brl": "Perda máxima por operação (moeda da conta)",
    "perda_max_dia_brl": "Perda máxima no dia (moeda da conta)",
    "perda_max_semana_brl": "Perda máxima na semana (moeda da conta)",
    "dd_alerta_brl": "Drawdown de alerta (moeda da conta)",
    "dd_max_brl": "Drawdown máximo (moeda da conta)",
    "operacoes_dia_max": "Operações por dia (máximo)",
    "perdas_seguidas_max": "Perdas seguidas antes de parar",
    "pausa_apos_perda_s": "Pausa após uma perda (s)",
    "stop_max_pontos": "Stop máximo (pontos)",
    "slippage_folga_pontos": "Folga de slippage (pontos)",
    "entradas_de": "Novas entradas a partir de (HH:MM)",
    "entradas_ate": "Novas entradas até (HH:MM)",
    "zeragem": "Zeragem obrigatória (HH:MM)",
    "bloqueio_evento_min": "Bloqueio em torno de evento de agenda (min)",
    "ordens_min_max": "Ordens por minuto (máximo)",
    "tempo_min_livro_s": "Tempo mínimo de livro antes de entrar (s)",
    "volume_max_lotes": "Volume máximo por posição em lotes (forex/CFD; 0 = usa contratos)",
    "nocional_max_conta": "Nocional máximo por posição (moeda da conta; 0 = desligado)",
    "tempo_max_posicao_s": "Tempo máximo de uma posição, desde a abertura (s)",
}


def registrar_rotas(app: FastAPI, *, tpl: Any, ctx: Callable[..., dict[str, Any]], tok: str, dir_usuarios: Path,
                    dir_auditoria: Path, perfil: PerfilInstrumento | None,
                    limitado: Callable[[Request], bool], falhou: Callable[[Request], None],
                    ler_form: Callable[[Request], Any]) -> None:
    sessoes: dict[str, str] = {}  # id aleatorio do cookie -> usuario (em memoria: reiniciar o painel desloga)
    auditoria: list[Any] = []

    def armazem() -> ArmazemUsuarios:
        if not auditoria:
            from robowin.auditoria.cadeia import RegistradorAuditoria
            auditoria.append(RegistradorAuditoria(dir_auditoria))
        return ArmazemUsuarios(dir_usuarios, auditoria[0])

    def usuario_da_sessao(request: Request) -> str | None:
        sid = request.cookies.get(COOKIE_USUARIO)
        return sessoes.get(sid) if sid else None

    def token_acoes_ok(cand: str | None) -> bool:
        return bool(tok) and bool(cand) and hmac.compare_digest(str(cand).encode(), tok.encode())

    def token_acoes(request: Request, form: dict[str, str], *, so_formulario: bool = False) -> bool:
        if so_formulario:
            return token_acoes_ok(form.get("token"))
        from .app import COOKIE
        return any(token_acoes_ok(c) for c in (request.headers.get("x-robowin-token"), request.cookies.get(COOKIE),
                                                 form.get("token")))

    def chave_de(texto: str | None) -> ChaveConta | None:
        if not texto:
            return None
        try:
            return ChaveConta.de_texto(texto)
        except ErroConfig:
            return None

    def pagina(request: Request, usuario: str | None, conta_txt: str | None = None, *, status: int = 200,
               msg: str | None = None, erro: str | None = None, valores: dict[str, str] | None = None,
               previa: list[str] | None = None) -> Response:
        dados: dict[str, Any] = {"usuario": usuario, "msg": msg, "erro": erro, "previa": previa, "contas": [],
                                 "sel": None, "estado": None, "valores": None, "historico": [], "aprovacoes": [],
                                 "eventos": [], "campos": [(c, ROTULOS.get(c, c)) for c in CAMPOS_RISCO],
                                 "PALAVRA": PALAVRA_APROVACAO, "tem_modelo": False}
        if usuario is not None:
            a = armazem()
            try:
                contas = a.contas(usuario)
            except ErroConfig:
                contas = []
            dados["contas"] = [(c.chave.texto, c.tipo, a.estado(usuario, c.chave)) for c in contas]
            chave = chave_de(conta_txt) or (contas[0].chave if contas else None)
            if chave is not None and any(c.chave == chave for c in contas):
                st = a.estado(usuario, chave)
                atual = a.limites(usuario, chave)
                if valores is None:
                    if atual is not None:
                        valores = {k: str(v) for k, v in atual.model_dump().items()}
                    elif perfil is not None:  # modelo conservador so PRE-PREENCHE: nada e gravado nem aprovado
                        valores = {k: str(v) for k, v in modelo_inicial(perfil).model_dump().items()}
                        dados["tem_modelo"] = True
                    else:
                        valores = {}
                dados.update(sel=chave.texto, estado=st, valores=valores,
                             historico=list(reversed(a.historico_limites(usuario, chave))),
                             aprovacoes=a.aprovacoes(usuario, chave),
                             eventos=eventos_da_conta(dir_auditoria, usuario, chave.texto))
        return tpl.TemplateResponse(request, "risco.html", ctx(request, "risco", **dados), status_code=status)

    def voltar(conta: str | None, msg: str) -> RedirectResponse:
        q = f"?msg={quote(msg)}" + (f"&conta={quote(conta)}" if conta else "")
        return RedirectResponse("/risco" + q, status_code=303)

    @app.get("/risco", response_class=HTMLResponse)
    def pg_risco(request: Request, conta: str | None = None, msg: str | None = None):
        return pagina(request, usuario_da_sessao(request), conta, msg=msg)

    @app.post("/risco/entrar")
    async def risco_entrar(request: Request):
        form = await ler_form(request)
        if limitado(request):
            return pagina(request, None, status=429, erro="Muitas tentativas inválidas. Aguarde alguns minutos.")
        u = (form.get("usuario") or "").strip()
        if not armazem().autenticar(u, form.get("token_usuario") or ""):
            falhou(request)
            return pagina(request, None, status=401, erro="Usuário ou token do usuário inválido.")
        sid = secrets.token_urlsafe(24)
        sessoes[sid] = u
        r = RedirectResponse("/risco", status_code=303)
        r.set_cookie(COOKIE_USUARIO, sid, httponly=True, samesite="strict")
        return r

    @app.post("/risco/sair")
    async def risco_sair(request: Request):
        sid = request.cookies.get(COOKIE_USUARIO)
        if sid:
            sessoes.pop(sid, None)
        r = RedirectResponse("/risco", status_code=303)
        r.delete_cookie(COOKIE_USUARIO)
        return r

    def exigir(request: Request, form: dict[str, str], *, so_formulario: bool = False) -> tuple[str | None, Response | None]:
        u = usuario_da_sessao(request)
        if u is None:
            return None, pagina(request, None, status=401, erro="Entre com seu usuário e token para ver e editar seus limites.")
        if not tok:
            return u, pagina(request, u, form.get("conta"), status=403,
                             erro="Token de ações não configurado (defina ROBOWIN_TOKEN). Gravação desativada.")
        if limitado(request):
            return u, pagina(request, u, form.get("conta"), status=429, erro="Muitas tentativas inválidas. Aguarde.")
        if not token_acoes(request, form, so_formulario=so_formulario):
            falhou(request)
            return u, pagina(request, u, form.get("conta"), status=401, erro="Token de ações ausente ou inválido.")
        return u, None

    @app.post("/risco/contas")
    async def risco_conta(request: Request):
        form = await ler_form(request)
        u, neg = exigir(request, form)
        if neg is not None:
            return neg
        assert u is not None
        try:
            chave = ChaveConta.criar(form.get("plataforma", ""), form.get("servidor", ""), form.get("login", ""))
            armazem().registrar_conta(u, chave, form.get("tipo", "demo"))
        except ErroConfig as e:
            return pagina(request, u, status=400, erro=str(e))
        return voltar(chave.texto, "Conta registrada SEM limites: defina e aprove.")

    def _valores(form: dict[str, str]) -> dict[str, str]:
        return {c: form.get(c, "") for c in CAMPOS_RISCO}

    @app.post("/risco/previa")
    async def risco_previa(request: Request):
        form = await ler_form(request)
        u = usuario_da_sessao(request)
        if u is None:
            return pagina(request, None, status=401, erro="Entre com seu usuário e token.")
        vals = _valores(form)
        try:
            cfg = config_de_formulario(vals)
        except ErroConfig as e:
            return pagina(request, u, form.get("conta"), status=400, erro=f"Limites incoerentes: {e}", valores=vals)
        return pagina(request, u, form.get("conta"), valores=vals, previa=descrever_limites(cfg),
                      msg="Pré-visualização: nada foi gravado.")

    @app.post("/risco/limites")
    async def risco_limites(request: Request):
        form = await ler_form(request)
        u, neg = exigir(request, form)
        if neg is not None:
            return neg
        assert u is not None
        chave = chave_de(form.get("conta"))
        vals = _valores(form)
        if chave is None:
            return pagina(request, u, status=400, erro="Conta inválida.", valores=vals)
        try:
            armazem().gravar_limites(u, chave, vals, autor=u, origem="painel")
        except ErroConfig as e:
            return pagina(request, u, chave.texto, status=400, erro=f"Nada foi gravado: {e}", valores=vals)
        return voltar(chave.texto, "Limites gravados (auditoria). Ainda NÃO aprovados: operar segue bloqueado até aprovar.")

    @app.post("/risco/aprovar")
    async def risco_aprovar(request: Request):
        form = await ler_form(request)
        u, neg = exigir(request, form, so_formulario=True)  # o token de acoes tem de ser DIGITADO
        if neg is not None:
            return neg
        assert u is not None
        chave = chave_de(form.get("conta"))
        if chave is None:
            return pagina(request, u, status=400, erro="Conta inválida.")
        if (form.get("confirmacao") or "").strip() != PALAVRA_APROVACAO:
            return pagina(request, u, chave.texto, status=400,
                          erro=f"Confirmação ausente: digite {PALAVRA_APROVACAO} para aprovar. Nada foi gravado.")
        digitado = (form.get("confirmacao") or "").strip()
        try:
            res = armazem().aprovar(u, chave, form.get("responsavel", ""), form.get("motivo", "") or
                                    "aprovacao pelo usuario no painel", confirmar=lambda _r: digitado)
        except ErroConfig as e:
            return pagina(request, u, chave.texto, status=400, erro=f"Aprovação recusada: {e}")
        return voltar(chave.texto, f"APROVADO: {res.arquivo.name} (auditoria). Vale só para esta conta DEMO.")



def eventos_da_conta(dir_auditoria: Path, usuario: str, conta: str, limite: int = 50) -> list[dict[str, Any]]:
    """Eventos da auditoria (cadeia de hash) deste usuario+conta, do mais novo ao mais antigo."""
    import json
    out: list[dict[str, Any]] = []
    if not dir_auditoria.is_dir():
        return out
    for arq in sorted(dir_auditoria.glob("auditoria-*.jsonl")):
        with open(arq, encoding="utf-8") as f:
            for ln in f:
                try:
                    reg = json.loads(ln)
                except ValueError:
                    continue
                d = reg.get("dados") or {}
                if d.get("usuario") == usuario and d.get("conta") == conta:
                    out.append({"tipo": reg.get("tipo"), "t_ns": reg.get("t_ns"), "hash": str(reg.get("hash", ""))[:16],
                                "sha256": str(d.get("sha256", ""))[:16]})
    return list(reversed(out))[:limite]
