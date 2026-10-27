"""Painel web (FastAPI + Jinja2 + HTMX). Somente simulacao. Escuta 127.0.0.1 por padrao."""
from __future__ import annotations

import hmac
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from robowin.core.tipos import Instrumento

from . import dados as D
from .metricas import CONTROLE, N_MIN_AMOSTRA

AQUI = Path(__file__).parent
NOME_FLAG = "EMERGENCIA"
FRASE_CONFIRMACAO = "EMERGENCIA"
COOKIE = "robowin_token"
MAX_FALHAS = 5  # tentativas de token invalido por cliente na janela
JANELA_FALHAS_S = 300.0
ESTADO_FLAG = "flag_gravada_aguardando_confirmacao_do_gateway"
ROTULO_CANDIDATO = "candidato não validado"


def _fmt_brl(v: Any) -> str:
    if v is None:
        return "—"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_num(v: Any, casas: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_pct(v: Any) -> str:
    return "—" if v is None else _fmt_num(v * 100, 1) + "%"


def criar_app(
    banco: str | Path | None = None,
    dir_dados: str | Path | None = None,
    token: str | None = None,
    config: dict[str, Any] | None = None,
    metricas_provider=None,
) -> FastAPI:
    """`metricas_provider(banco) -> {braco: metricas}` e o ponto de extensao para robowin.avaliacao."""
    config = config or {}
    geral = config.get("geral", {})
    dir_d = Path(dir_dados or os.environ.get("ROBOWIN_DADOS") or geral.get("dados", "dados_locais")).resolve()
    tok = token if token is not None else os.environ.get("ROBOWIN_TOKEN", "")
    b = D.Banco(banco)
    instr = Instrumento(simbolo=geral.get("simbolo_base", "WIN"), **config.get("instrumento", {}))

    app = FastAPI(title="robowin — painel", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
    tpl = Jinja2Templates(directory=str(AQUI / "templates"))
    tpl.env.filters.update(brl=_fmt_brl, num=_fmt_num, pct=_fmt_pct)
    tpl.env.globals.update(ROTULO_CANDIDATO=ROTULO_CANDIDATO, N_MIN=N_MIN_AMOSTRA, CONTROLE=CONTROLE)
    app.state.banco = b
    app.state.dir_dados = dir_d

    falhas: dict[str, list[float]] = {}  # tentativas de token invalido por cliente (limite simples, em memoria)

    def cliente_id(request: Request) -> str:
        return request.client.host if request.client else "?"

    def limitado(request: Request) -> bool:
        agora = time.monotonic()
        recentes = [t for t in falhas.get(cliente_id(request), []) if agora - t < JANELA_FALHAS_S]
        falhas[cliente_id(request)] = recentes
        return len(recentes) >= MAX_FALHAS

    def falhou(request: Request) -> None:
        falhas.setdefault(cliente_id(request), []).append(time.monotonic())

    def ctx(request: Request, pagina: str, **extra: Any) -> dict[str, Any]:
        return {"pagina": pagina, "sintetico": b.sintetico() or os.environ.get("ROBOWIN_DADOS_SINTETICOS") == "1",
                "tem_token": bool(tok), "banco_existe": b.existe(), **extra}

    def token_ok(request: Request, form: dict[str, str]) -> bool:
        if not tok:
            return False
        for cand in (request.headers.get("x-robowin-token"), request.cookies.get(COOKIE), form.get("token")):
            if cand and hmac.compare_digest(cand.encode(), tok.encode()):
                return True
        return False

    async def ler_form(request: Request) -> dict[str, str]:
        corpo = (await request.body()).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(corpo).items()}

    def negar(request: Request, form: dict[str, str]) -> JSONResponse | None:
        if not tok:
            return JSONResponse({"erro": "Token não configurado (defina ROBOWIN_TOKEN). Ações estão desativadas."},
                                status_code=403)
        if limitado(request):
            return JSONResponse({"erro": "Muitas tentativas inválidas. Aguarde alguns minutos."}, status_code=429)
        if not token_ok(request, form):
            falhou(request)
            return JSONResponse({"erro": "Token ausente ou inválido."}, status_code=401)
        return None

    # ---------------- paginas ----------------
    @app.get("/", response_class=HTMLResponse)
    def visao_geral(request: Request, msg: str | None = None):
        v = D.visao_geral(b)
        arq = dir_d / NOME_FLAG
        flag = arq.exists()
        confirmacao = D.confirmacao_emergencia(b, arq.stat().st_mtime_ns) if flag else None
        return tpl.TemplateResponse(request, "visao_geral.html", ctx(
            request, "visao", v=v, instr=instr, flag=flag, confirmacao=confirmacao, msg=msg, conta=geral.get("conta", "não informada"),
            modo=geral.get("modo", "simulacao"), orcamento_mensal=config.get("ia", {}).get("orcamento_mensal_usd")))

    @app.get("/fragmentos/resumo", response_class=HTMLResponse)
    def frag_resumo(request: Request):
        return tpl.TemplateResponse(request, "_resumo.html", ctx(request, "visao", v=D.visao_geral(b)))

    @app.get("/decisoes", response_class=HTMLResponse)
    def pg_decisoes(request: Request):
        return tpl.TemplateResponse(request, "decisoes.html", ctx(request, "decisoes", itens=D.decisoes(b)))

    @app.get("/operacoes", response_class=HTMLResponse)
    def pg_operacoes(request: Request):
        return tpl.TemplateResponse(request, "operacoes.html", ctx(request, "operacoes", o=D.operacoes(b)))

    @app.get("/desempenho", response_class=HTMLResponse)
    def pg_desempenho(request: Request):
        return tpl.TemplateResponse(request, "desempenho.html", ctx(
            request, "desempenho", d=D.desempenho(b, metricas_provider)))

    @app.get("/modelos", response_class=HTMLResponse)
    def pg_modelos(request: Request):
        return tpl.TemplateResponse(request, "modelos.html", ctx(request, "modelos", m=D.modelos(b)))

    @app.get("/auditoria", response_class=HTMLResponse)
    def pg_auditoria(request: Request):
        from robowin.auditoria.cadeia import verificar_diretorio
        pasta = dir_d / "auditoria"
        if pasta.exists():
            ok, msgs = verificar_diretorio(pasta)
        else:
            ok, msgs = None, []
        return tpl.TemplateResponse(request, "auditoria.html", ctx(
            request, "auditoria", ok=ok, msgs=msgs, pasta=str(pasta)))

    # ---------------- API JSON ----------------
    @app.get("/api/resumo")
    def api_resumo():
        return D.visao_geral(b)

    @app.get("/api/capital")
    def api_capital():
        return D.curvas_capital(b)

    @app.get("/api/desempenho")
    def api_desempenho():
        return D.desempenho(b, metricas_provider)

    @app.get("/api/latencia")
    def api_latencia():
        return D.latencia_por_etapa(b)

    @app.get("/api/decisoes")
    def api_decisoes(limite: int = 100):
        return D.decisoes(b, max(1, min(limite, 500)))

    # ---------------- acoes (token obrigatorio) ----------------
    @app.post("/entrar")
    async def entrar(request: Request):
        form = await ler_form(request)
        if limitado(request):
            return JSONResponse({"erro": "Muitas tentativas inválidas. Aguarde alguns minutos."}, status_code=429)
        if not tok or not hmac.compare_digest(form.get("token", "").encode(), tok.encode()):
            falhou(request)
            return JSONResponse({"erro": "Token inválido."}, status_code=401)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, tok, httponly=True, samesite="strict")
        return r

    @app.post("/acoes/emergencia")
    async def emergencia(request: Request):
        form = await ler_form(request)
        neg = negar(request, form)
        if neg:
            return neg
        if form.get("confirmacao", "").strip() != FRASE_CONFIRMACAO:
            return JSONResponse({"erro": f"Confirmação ausente: digite {FRASE_CONFIRMACAO}."}, status_code=400)
        dir_d.mkdir(parents=True, exist_ok=True)
        agora = time.time_ns()
        (dir_d / NOME_FLAG).write_text(f"acionado pelo painel em ns={agora}\n", encoding="utf-8")
        try:
            b.escrever("INSERT INTO incidentes(t_ns, origem, severidade, detalhe) VALUES (?,?,?,?)",
                       (agora, "painel", "critico", "Flag EMERGENCIA gravada pelo painel; aguardando confirmacao do gateway/nucleo."))
        except Exception as e:  # a flag ja foi gravada; falha de registro nao pode desfazer a emergencia
            return JSONResponse({"ok": True, "flag": str(dir_d / NOME_FLAG), "estado": ESTADO_FLAG,
                                 "aviso": f"incidente nao registrado: {e}"})
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/?msg=emergencia", status_code=303)
        return {"ok": True, "flag": str(dir_d / NOME_FLAG), "estado": ESTADO_FLAG}

    return app
