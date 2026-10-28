"""Checagens rapidas de `robowin verificar`. Cada uma devolve `Resultado`; excecao inesperada vira REPROVADO.

Nada aqui usa rede, envia ordem ou le o valor de uma chave de API. Dados de demonstracao sao SINTETICOS.
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib
import importlib.util
import json
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import robowin

from .matriz import Resultado, aprovado, nao_verificavel, reprovado

RAIZ_PACOTE = Path(robowin.__file__).resolve().parent
HOSTS_CRITICOS = ("api.anthropic.com", "api.openai.com", "servidor da corretora (MT5/MT4)", "feeds de noticias/calendario")
CHAVES_API = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
_NOME_ENVIO = "order_" + "send"
PAGINAS_PAINEL = ("/", "/decisoes", "/operacoes", "/desempenho", "/modelos", "/auditoria")


def achar_raiz(preferida: str | Path | None = None) -> Path | None:
    """Raiz do repositorio (com pyproject.toml e config/): a pasta atual, a pedida ou a de origem do pacote."""
    candidatos = [Path(preferida)] if preferida else [Path.cwd(), RAIZ_PACOTE.parents[1]]
    for c in candidatos:
        if (c / "pyproject.toml").is_file() and (c / "config").is_dir():
            return c.resolve()
    return None


class Contexto:
    def __init__(self, raiz: Path | None) -> None:
        self.raiz = raiz

    def arq(self, rel: str) -> Path:
        assert self.raiz is not None
        return self.raiz / rel


# ----------------------------------------------------------------------------------------------
def importabilidade(ctx: Contexto) -> Resultado:
    nome = "Importabilidade de todos os pacotes de src/robowin"
    falhas, n = [], 0
    for m in pkgutil.walk_packages(robowin.__path__, "robowin."):
        n += 1
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001
            falhas.append(f"{m.name}: {type(e).__name__}: {e}")
    if falhas:
        return reprovado(nome, f"{len(falhas)} de {n} modulos nao importam: " + " | ".join(falhas[:5]))
    return aprovado(nome, f"{n} modulos importados")


def ruff(ctx: Contexto) -> Resultado:
    nome = "Ruff (estilo/erros estaticos)"
    if ctx.raiz is None:
        return nao_verificavel(nome, "raiz do repositorio nao encontrada (pacote instalado fora do repositorio)")
    cmd = None
    if importlib.util.find_spec("ruff") is not None:
        cmd = [sys.executable, "-m", "ruff"]
    elif shutil.which("ruff"):
        cmd = [str(shutil.which("ruff"))]
    if cmd is None:
        return nao_verificavel(nome, "ruff nao instalado (uv sync --extra dev)")
    p = subprocess.run([*cmd, "check", "--no-cache", "."], cwd=ctx.raiz, capture_output=True, text=True, timeout=120)
    if p.returncode == 0:
        return aprovado(nome, "sem achados")
    return reprovado(nome, (p.stdout + p.stderr).strip().splitlines()[-1][:200] if (p.stdout + p.stderr).strip() else "ruff falhou")


def lint_mql4(ctx: Contexto) -> Resultado:
    nome = "Linter estatico MQL4 (scripts/lint_mql4.py)"
    if ctx.raiz is None:
        return nao_verificavel(nome, "raiz do repositorio nao encontrada")
    script, ea = ctx.arq("scripts/lint_mql4.py"), ctx.arq("mql4/RobowinBridge.mq4")
    if not script.is_file() or not ea.is_file():
        return nao_verificavel(nome, "scripts/lint_mql4.py ou mql4/RobowinBridge.mq4 ausente")
    p = subprocess.run([sys.executable, str(script), str(ea)], cwd=ctx.raiz, capture_output=True, text=True, timeout=60)
    linhas = p.stdout.splitlines()
    erros = [ln for ln in linhas if ": ERRO [" in ln]
    if erros or p.returncode not in (0,):
        return reprovado(nome, f"{len(erros)} ERRO(s): " + (erros[0][:160] if erros else f"saida {p.returncode}"))
    revisar = sum(1 for ln in linhas if ": REVISAR [" in ln)
    return aprovado(nome, f"0 ERRO ({revisar} item(ns) REVISAR para humano). NAO e compilacao: compilar no MetaEditor depende do usuario")


def cadeia_auditoria(ctx: Contexto) -> Resultado:
    nome = "Cadeia de auditoria (hash) e deteccao de adulteracao"
    from robowin.auditoria.cadeia import RegistradorAuditoria, verificar_arquivo
    with tempfile.TemporaryDirectory() as d:
        reg = RegistradorAuditoria(d)
        for i in range(5):
            reg.registrar("verificacao", {"i": i, "rotulo": "sintetico"}, dia="2099-01-01")
        arq = Path(d) / "auditoria-2099-01-01.jsonl"
        ok, msg = verificar_arquivo(arq)
        if not ok:
            return reprovado(nome, f"cadeia integra foi rejeitada: {msg}")
        linhas = arq.read_text(encoding="utf-8").splitlines()
        reg_adulterado = json.loads(linhas[2])
        reg_adulterado["dados"]["i"] = 999
        linhas[2] = json.dumps(reg_adulterado, ensure_ascii=False)
        arq.write_text("\n".join(linhas) + "\n", encoding="utf-8")
        ok2, msg2 = verificar_arquivo(arq)
        if ok2:
            return reprovado(nome, "adulteracao de um registro NAO foi detectada")
        linhas2 = [ln for k, ln in enumerate(linhas) if k != 1]  # remocao de um registro
        arq.write_text("\n".join(linhas2) + "\n", encoding="utf-8")
        ok3, _ = verificar_arquivo(arq)
        if ok3:
            return reprovado(nome, "remocao de um registro NAO foi detectada")
    return aprovado(nome, f"5 registros integros; adulteracao detectada ({msg2}); remocao detectada")


def catalogo_modelos(ctx: Contexto) -> Resultado:
    nome = "Catalogo de modelos (config/modelos.toml: preco e corte)"
    if ctx.raiz is None:
        return nao_verificavel(nome, "raiz do repositorio nao encontrada")
    from robowin.ia.catalogo import Catalogo, ErroCatalogo
    try:
        cat = Catalogo.carregar(ctx.arq("config/modelos.toml"))
    except ErroCatalogo as e:
        return reprovado(nome, str(e))
    modelos = list(cat)
    sem = [f"{m.provedor}:{m.id}" for m in modelos if not m.corte_conhecimento or m.preco_entrada_mtok is None
           or m.preco_saida_mtok is None]
    if sem or not modelos:
        return reprovado(nome, f"modelos sem preco/corte: {sem}" if sem else "catalogo vazio")
    # falha segura: modelo sem preco tem de ser recusado
    try:
        Catalogo.de_dict({"modelo": [{"provedor": "x", "id": "y", "corte_conhecimento": "2026-01"}]})
        return reprovado(nome, "modelo sem preco foi aceito (falha segura quebrada)")
    except ErroCatalogo:
        pass
    return aprovado(nome, f"{len(modelos)} modelos, todos com preco e data de corte; modelo sem preco e recusado")


def perfis_ativo_bolsa(ctx: Contexto) -> Resultado:
    nome = "Perfis de ativo e de bolsa (instrumentos.toml, bolsas.toml)"
    if ctx.raiz is None:
        return nao_verificavel(nome, "raiz do repositorio nao encontrada")
    from robowin.core.bolsas import bolsas_disponiveis, carregar_bolsa
    from robowin.core.config import carregar_toml
    from robowin.core.instrumentos import (
        ErroConfig,
        PerfilNaoConfirmado,
        ativo_da_config,
        ativos_disponiveis,
        carregar_perfil,
    )
    try:
        instr, bols = ctx.arq("config/instrumentos.toml"), ctx.arq("config/bolsas.toml")
        ativo = ativo_da_config(carregar_toml(ctx.arq("config/robowin.toml")))
        confirmados, bloqueados = [], []
        for a in ativos_disponiveis(instr):
            p = carregar_perfil(a, instr, permitir_a_confirmar=True)
            if p.a_confirmar:
                try:
                    p.exigir_confirmado()
                    return reprovado(nome, f"perfil a_confirmar {a} NAO bloqueou a operacao")
                except PerfilNaoConfirmado:
                    bloqueados.append(a)
            else:
                confirmados.append(a)
        bolsas_ok, bolsas_bloq = [], []
        for b in bolsas_disponiveis(bols):
            pb = carregar_bolsa(b, bols)
            (bolsas_bloq if pb.a_confirmar else bolsas_ok).append(b)
        if ativo not in confirmados:
            return reprovado(nome, f"ativo configurado ({ativo}) nao esta confirmado; confirmados: {confirmados}")
        carregar_perfil(ativo, instr)  # estrito: nao pode levantar
    except ErroConfig as e:
        return reprovado(nome, str(e))
    return aprovado(nome, f"ativo configurado {ativo} CONFIRMADO; BLOQUEADOS (a_confirmar): {', '.join(bloqueados) or '-'}; "
                    f"bolsas confirmadas: {', '.join(bolsas_ok)}; bolsas BLOQUEADAS: {', '.join(bolsas_bloq) or '-'}")


def politica_risco(ctx: Contexto) -> Resultado:
    nome = "Politica de risco (hash x config/aprovacoes)"
    if ctx.raiz is None:
        return nao_verificavel(nome, "raiz do repositorio nao encontrada")
    from robowin.core.config import ErroConfig, carregar_toml, responsavel_pendente
    from robowin.risco.politica import carregar_politica
    risco, apr = ctx.arq("config/risco.toml"), ctx.arq("config/aprovacoes/risco-v1.toml")
    try:
        carregar_politica(risco, apr)  # hash + coerencia
    except (ErroConfig, OSError, ValueError) as e:
        return reprovado(nome, str(e))
    if responsavel_pendente(carregar_toml(apr)):
        return nao_verificavel(nome, "hash confere, mas o responsavel da aprovacao esta 'pendente': risco nao aprovado para o vivo "
                               "(o modo vivo recusa; a simulacao roda rotulada risco_nao_aprovado)")
    return aprovado(nome, "hash confere e ha responsavel pela aprovacao")


def _chamadas_order_send(arvore: ast.AST) -> list[int]:
    out = []
    for n in ast.walk(arvore):
        if isinstance(n, ast.Call):
            f = n.func
            nome = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if nome is not None and nome.startswith(_NOME_ENVIO):
                out.append(n.lineno)
    return out


def order_send_unico(ctx: Contexto) -> Resultado:
    nome = "Estatico: order_send so em mt5/execucao_demo.py (uma chamada)"
    unico = RAIZ_PACOTE / "mt5" / "execucao_demo.py"
    fora, no_unico = [], 0
    for p in sorted(RAIZ_PACOTE.rglob("*.py")):
        linhas = _chamadas_order_send(ast.parse(p.read_text(encoding="utf-8")))
        if p == unico:
            no_unico = len(linhas)
        elif linhas:
            fora.append(f"{p.relative_to(RAIZ_PACOTE)}:{linhas}")
    if fora:
        return reprovado(nome, "order_send chamado fora do modulo unico: " + "; ".join(fora))
    if no_unico != 1:
        return reprovado(nome, f"execucao_demo.py deve ter exatamente 1 chamada a order_send (tem {no_unico})")
    return aprovado(nome, "exatamente 1 chamada, em mt5/execucao_demo.py (guarda demo)")


def constantes_de_ativo(ctx: Contexto) -> Resultado:
    nome = "Estatico: nenhum numero/nome de ativo no codigo (so em config)"
    if ctx.raiz is None or not ctx.arq("tests/estaticos/test_sem_constantes_de_ativo.py").is_file():
        return nao_verificavel(nome, "tests/estaticos/ ausente (reutiliza o teste estatico do repositorio)")
    arq = ctx.arq("tests/estaticos/test_sem_constantes_de_ativo.py")
    spec = importlib.util.spec_from_file_location("_estatico_ativos", arq)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for fn in ("test_nenhuma_constante_de_ativo_fora_do_perfil", "test_allowlist_curta_e_justificada"):
        try:
            getattr(mod, fn)()
        except AssertionError as e:
            return reprovado(nome, f"{fn}: {str(e)[:200]}")
    return aprovado(nome, "teste estatico (AST) sem violacoes")


def _perfil_ativo(ctx: Contexto):  # type: ignore[no-untyped-def]
    from robowin.core.config import carregar_toml
    from robowin.core.instrumentos import ativo_da_config, carregar_perfil
    assert ctx.raiz is not None
    ativo = ativo_da_config(carregar_toml(ctx.arq("config/robowin.toml")))
    return carregar_perfil(ativo, ctx.arq("config/instrumentos.toml"))


def diagnostico_duble(ctx: Contexto) -> list[Resultado]:
    nome5, nome4 = "Diagnostico contra o duble MT5 (sem terminal real)", "Diagnostico contra o duble MT4 (--duble)"
    if ctx.raiz is None:
        return [nao_verificavel(nome5, "raiz do repositorio nao encontrada"), nao_verificavel(nome4, "idem")]
    from robowin.core.relogio import RelogioReal
    from robowin.mt5.cli_cmds import executar_diagnostico
    perfil = _perfil_ativo(ctx)
    out = []
    rel = RelogioReal()
    from robowin.mt5.duble import criar_duble_padrao
    t = criar_duble_padrao(rel, n_ticks=2000, login=900001, servidor="XPMT5-DEMO")
    try:
        r = executar_diagnostico(t, perfil, login_esperado=900001, servidor_esperado="XPMT5-DEMO", relogio=rel, plataforma="mt5")
        out.append(aprovado(nome5, f"origem: {r['origem']}; APTO") if r["ok"] else reprovado(nome5, "; ".join(r["recusas"])[:200]))
    finally:
        t.shutdown()
    from robowin.mt5.conector_mt4 import ConectorMT4
    from robowin.mt5.duble_mt4 import ServidorDubleMT4, criar_duble_mt4_padrao
    srv = ServidorDubleMT4(criar_duble_mt4_padrao(rel, n_ticks=2000))
    term = None
    try:
        term = ConectorMT4(srv.iniciar())
        r = executar_diagnostico(term, perfil, login_esperado=900001, servidor_esperado="BROKERMT4-Demo", relogio=rel,
                                 plataforma="mt4")
        out.append(aprovado(nome4, f"origem: {r['origem']}; APTO") if r["ok"] else reprovado(nome4, "; ".join(r["recusas"])[:200]))
    finally:
        if term is not None:
            term.shutdown()
        srv.parar()
    return out


def painel(ctx: Contexto) -> Resultado:
    nome = "Painel: criar_app + 6 rotas sobre banco SINTETICO de demonstracao"
    try:
        from fastapi.testclient import TestClient
    except Exception as e:  # noqa: BLE001
        return nao_verificavel(nome, f"fastapi/httpx indisponivel: {type(e).__name__}")
    from robowin.painel.app import criar_app
    from robowin.painel.demo import popular
    with tempfile.TemporaryDirectory() as d:
        banco = popular(Path(d) / "demo-sintetico.sqlite")
        c = TestClient(criar_app(banco=banco, dir_dados=Path(d) / "dados", token="token-de-verificacao"))
        falhas = []
        for rota in PAGINAS_PAINEL:
            r = c.get(rota)
            if r.status_code != 200:
                falhas.append(f"{rota}={r.status_code}")
            elif "SIMULAÇÃO — sem ordens reais" not in r.text or "DADOS SINTÉTICOS" not in r.text:
                falhas.append(f"{rota}: faixa de simulacao/dados sinteticos ausente")
    if falhas:
        return reprovado(nome, "; ".join(falhas))
    return aprovado(nome, "6 rotas 200 com faixas 'SIMULAÇÃO' e 'DADOS SINTÉTICOS' (banco sintetico rotulado). Renderizacao em navegador: ver docs/11")


def gateway_cliente_local(ctx: Contexto) -> Resultado:
    nome = "Gateway + cliente local com duble: ordem demo aceita, conta real recusada"
    if ctx.raiz is None:
        return nao_verificavel(nome, "raiz do repositorio nao encontrada")
    from robowin.core.barramento import BarramentoLocal
    from robowin.core.relogio import RelogioSimulado
    from robowin.core.tipos import ComandoOrdem, Lado
    from robowin.mt5 import constantes as C
    from robowin.mt5.canais import CanalComandosLocal
    from robowin.mt5.duble import criar_duble_padrao
    from robowin.mt5.execucao_demo import ExecucaoRealNaoAutorizada
    from robowin.mt5.gateway import ConfigGateway, Gateway
    perfil = _perfil_ativo(ctx)
    sp = dt.timezone(dt.timedelta(hours=-3))
    ini = dt.datetime(2026, 10, 27, 12, 0, tzinfo=sp)  # terca, dentro da janela de entradas
    t0 = int(ini.timestamp() * 1e9)

    # montagem explicita (o diretorio de log precisa viver durante o teste)
    with tempfile.TemporaryDirectory() as dl:
        rel = RelogioSimulado(t0)
        d = criar_duble_padrao(rel, inicio_utc_ms=t0 // 1_000_000 - 1000, n_ticks=3000, login=900001,
                               servidor="XPMT5-DEMO", offset_servidor_s=10800)
        canal = CanalComandosLocal()
        cfg = ConfigGateway(perfil=perfil, login=900001, servidor="XPMT5-DEMO", hoje=ini.date(), dir_log=Path(dl),
                            inicio_coleta_ns=t0 - 1_000_000_000, espera_emergencia_s=0)
        gw = Gateway(d, cfg, rel, BarramentoLocal(), canal, dormir=lambda s: rel.avancar(int(s * 1e9)))
        gw.iniciar()
        cli = canal.cliente("nucleo")
        cli.enviar_sem_resposta({"tipo": "heartbeat", "origem": "nucleo"})
        for _ in range(5):
            rel.avancar(200_000_000)
            gw.passo()
        simb = gw.resolucao.simbolo_corretora if gw.resolucao else ""
        bid = d.symbol_info_tick(simb).bid
        stop = bid - perfil.stop_padrao_pontos
        cmd = ComandoOrdem(intencao_id="verificar-0001", magic=7, lado=Lado.COMPRA, tipo="mercado", quantidade=1, sl=stop)
        r = cli.enviar({"tipo": "ordem", "cmd": cmd.model_dump(mode="json")}, bombear=gw.passo)
        if not r or r.get("estado") != "EXECUTADA" or d.posicao_liquida(simb) != 1.0:
            return reprovado(nome, f"ordem demo nao foi aceita: {r}")
        n_envios = len(d.envios)
        d.trocar_conta(4242, "XPMT5-DEMO", "real")
        cmd2 = ComandoOrdem(intencao_id="verificar-0002", magic=7, lado=Lado.VENDA, tipo="mercado", quantidade=1,
                            sl=d.symbol_info_tick(simb).ask + perfil.stop_padrao_pontos)
        r2 = cli.enviar({"tipo": "ordem", "cmd": cmd2.model_dump(mode="json")}, bombear=gw.passo)
        if len(d.envios) != n_envios:
            return reprovado(nome, "ordem foi ENVIADA em conta real (guarda demo quebrada)")
        if not r2 or r2["ack"]["retcode"] != C.RET_LOCAL_RECUSADO or not gw.parado:
            return reprovado(nome, f"conta real nao foi recusada/parada: {r2}")
    # inicio com conta real tambem recusa
    rel2 = RelogioSimulado(t0)
    d2 = criar_duble_padrao(rel2, inicio_utc_ms=t0 // 1_000_000 - 1000, n_ticks=300, login=900001, servidor="XPMT5-DEMO",
                            trade_mode="real", offset_servidor_s=10800)
    gw2 = Gateway(d2, ConfigGateway(perfil=perfil, login=900001, servidor="XPMT5-DEMO", hoje=ini.date()), rel2,
                  BarramentoLocal(), CanalComandosLocal())
    try:
        gw2.iniciar()
        return reprovado(nome, "gateway iniciou em conta real")
    except ExecucaoRealNaoAutorizada:
        pass
    return aprovado(nome, "ordem demo EXECUTADA via cliente local; troca para conta real => recusa local + gateway parado, "
                    "0 envios; inicio em conta real => ExecucaoRealNaoAutorizada (tudo contra o DUBLE)")


def pacote_metatrader5(ctx: Contexto) -> Resultado:
    nome = "Plataforma: pacote MetaTrader5 e terminal MT5/MT4 reais"
    if not sys.platform.startswith("win"):
        return nao_verificavel(nome, f"o pacote MetaTrader5 so existe no Windows (aqui: {sys.platform}); terminal MT5/MT4 real e conta demo "
                               "dependem do usuario")
    if importlib.util.find_spec("MetaTrader5") is None:
        return nao_verificavel(nome, "pacote MetaTrader5 nao instalado (pip install MetaTrader5); terminal real depende do usuario")
    return aprovado(nome, "pacote MetaTrader5 presente (o terminal real e a conta demo ainda dependem do usuario: robowin diagnostico)")


def chaves_api(ctx: Contexto) -> list[Resultado]:
    out = []
    for v in CHAVES_API:
        nome = f"Chave de API {v}"
        if os.environ.get(v):
            out.append(aprovado(nome, "presente no ambiente (valor nunca exibido; validade nao testada, sem rede)"))
        else:
            out.append(nao_verificavel(nome, "ausente: bracos Claude/GPT reais nao rodam; so o provedor FALSO (depende do usuario)"))
    return out


def conectividade(ctx: Contexto) -> Resultado:
    return nao_verificavel("Conectividade com hosts criticos",
                           "NAO verificada por desenho (sem rede em `verificar`). Hosts: " + "; ".join(HOSTS_CRITICOS))


def checagens() -> list[tuple[str, Callable[[Contexto], Resultado | list[Resultado]]]]:
    return [
        ("importabilidade", importabilidade), ("ruff", ruff), ("lint_mql4", lint_mql4),
        ("auditoria", cadeia_auditoria), ("catalogo", catalogo_modelos), ("perfis", perfis_ativo_bolsa),
        ("risco", politica_risco), ("envio_unico", order_send_unico), ("constantes", constantes_de_ativo),
        ("diagnostico", diagnostico_duble), ("painel", painel), ("gateway", gateway_cliente_local),
        ("metatrader5", pacote_metatrader5), ("chaves", chaves_api), ("conectividade", conectividade),
    ]


def executar_todas(raiz: str | Path | None = None, *, pular: set[str] | None = None,
                   somente: set[str] | None = None) -> list[Resultado]:
    ctx = Contexto(achar_raiz(raiz))
    res: list[Resultado] = []
    for chave, fn in checagens():
        if (pular and chave in pular) or (somente and chave not in somente):
            continue
        t0 = time.monotonic()
        try:
            r = fn(ctx)
            lote = r if isinstance(r, list) else [r]
        except Exception as e:  # noqa: BLE001 - uma checagem quebrada e REPROVADO, nunca silencio
            lote = [reprovado(f"Checagem '{chave}'", f"excecao {type(e).__name__}: {e}")]
        dur = time.monotonic() - t0
        for x in lote:
            x.duracao_s = round(dur, 2)
        res.extend(lote)
    return res
