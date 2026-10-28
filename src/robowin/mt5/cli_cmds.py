"""Comandos de CLI do modulo mt5: diagnostico, gateway, coletar, emergencia.

Todos funcionam contra o DUBLE (dados sinteticos) com `--duble`; sem ele exigem o terminal MT5 real (Windows).
Nenhum comando envia ordem: `diagnostico` usa somente `order_check`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any

from robowin.core.barramento import BarramentoLocal
from robowin.core.config import ErroConfig, caminho_flag_emergencia, carregar_toml
from robowin.core.instrumentos import (
    PerfilInstrumento,
    PerfilNaoConfirmado,
    ativos_disponiveis,
    perfil_da_config,
)
from robowin.core.relogio import Relogio, RelogioReal

from . import constantes as C
from .capacidades import PLATAFORMAS, capacidades_do_terminal, normalizar_plataforma
from .coleta import ColetorLivro, ColetorTicks, LogBruto
from .conta import ler_estado_conta
from .contrato import ContratoRecusado, resolver_contrato
from .execucao_demo import ConfigDemo, ExecucaoDemo, GuardaDemoViolada
from .specs import EspecEsperada, valor_ponto_brl
from .tempo_servidor import calibrar


# ----------------------------------------------------------------------------------------------
# diagnostico
# ----------------------------------------------------------------------------------------------
def executar_diagnostico(
    terminal: Any, perfil: PerfilInstrumento, *, login_esperado: int | None = None, servidor_esperado: str | None = None,
    hoje: dt.date | None = None, relogio: Relogio | None = None, modo_config: str = "demo_mt5",
    plataforma: str | None = None,
) -> dict[str, Any]:
    """Coleta o diagnostico do ambiente MT5. NUNCA envia ordens (somente order_check)."""
    relogio = relogio or RelogioReal()
    hoje = hoje or dt.datetime.fromtimestamp(relogio.agora_ns() / 1e9, dt.UTC).date()
    caps = capacidades_do_terminal(terminal, plataforma)
    rel: dict[str, Any] = {
        "gerado_em_utc": dt.datetime.fromtimestamp(relogio.agora_ns() / 1e9, dt.UTC).isoformat(),
        "origem": _origem(terminal, caps.rotulo), "plataforma": caps.plataforma, "capacidades": caps.como_dict(),
        "recusas": [], "avisos": [],
    }
    rec, avi = rel["recusas"], rel["avisos"]
    if not terminal.initialize():
        rec.append(f"initialize falhou: {terminal.last_error()}")
        rel["ok"] = False
        return rel
    rel["origem"] = _origem(terminal, caps.rotulo)  # o MT4 so sabe se e duble depois do handshake
    conta = ler_estado_conta(terminal)
    if conta is None:
        rec.append("account_info indisponivel")
        rel["ok"] = False
        return rel
    ti = terminal.terminal_info()
    rel["conta"] = conta.model_dump()
    rel["terminal"] = {"trade_expert": conta.trade_expert, "trade_allowed": conta.trade_allowed,
                       "conectado": bool(getattr(ti, "connected", False)), "caminho": str(getattr(ti, "path", ""))}
    if conta.trade_mode != "demo":
        rec.append(f"conta {conta.trade_mode.upper()}: o robo so opera em conta DEMO")
    if login_esperado is None or servidor_esperado is None:
        avi.append("login/servidor esperados nao informados: a guarda de envio exige ambos")
    if login_esperado is not None and conta.login != login_esperado:
        rec.append(f"login {conta.login} difere do esperado {login_esperado}")
    if servidor_esperado is not None and conta.servidor != servidor_esperado:
        rec.append(f"servidor '{conta.servidor}' difere do esperado '{servidor_esperado}'")
    if not conta.trade_expert:
        rec.append("trade_expert falso (algotrading desligado na conta)")
    if not conta.trade_allowed:
        rec.append("trade_allowed falso (negociacao desabilitada)")
    esperada = C.NOMES_MARGEM[C.MARGEM_HEDGING if caps.conta == "hedging" else C.MARGEM_EXCHANGE]
    if conta.margin_mode != esperada:
        avi.append(f"margin_mode {conta.margin_mode}: {caps.rotulo} espera {esperada}; confirme o comportamento")
    avi += [f"capacidade ausente: {m}" for m in caps.bloqueios(usa_livro=True, usa_mm=True, usa_backfill=True,
                                                               usa_fluxo_agressor=True)]

    # contrato e especificacoes
    res = None
    try:
        res = resolver_contrato(terminal, hoje, perfil)
        info = terminal.symbol_info(res.simbolo_corretora)
        esp = EspecEsperada.do_perfil(perfil)
        rel["contrato"] = {"codigo_calendario": res.codigo_calendario, "simbolo_corretora": res.simbolo_corretora,
                           "prefixo": res.prefixo, "sufixo": res.sufixo, "vencimento": res.vencimento.isoformat() if res.vencimento else None,
                           "outros_contratos": res.outros_contratos}
        rel["especificacoes"] = {
            "tick_pontos": res.instrumento.tick_pontos, "ponto_valor_brl": res.instrumento.ponto_valor_brl,
            "lote": res.instrumento.lote, "esperado": {"tick_pontos": esp.tick_pontos,
                                                       "ponto_valor_brl": esp.ponto_valor_brl, "lote": esp.lote},
            "filling_mode": res.instrumento.filling_mode, "trade_exemode": res.instrumento.trade_exemode,
            "ticks_bookdepth": res.instrumento.book_depth, "valor_ponto_calculado": valor_ponto_brl(info),
            "validas": True,
        }
    except ContratoRecusado as e:
        rec.extend(f"contrato/especificacao: {m}" for m in e.motivos)
        rel["contrato"] = {"codigo_calendario": None, "recusado": e.motivos}
    except PerfilNaoConfirmado as e:
        rec.append(f"perfil de instrumento: {e}")
        rel["contrato"] = {"codigo_calendario": None, "recusado": [str(e)]}
    rel["ativo"] = perfil.simbolo_base

    # ativos
    simbolos = terminal.symbols_get() or ()
    nomes = sorted(str(s.name) for s in simbolos)
    rel["ativos"] = [n for n in nomes if any(a in n for a in ativos_disponiveis())]
    rel["total_simbolos"] = len(nomes)

    if res is not None:
        simbolo = res.simbolo_corretora
        rs = calibrar(terminal, simbolo, relogio, amostras=3, intervalo_s=0.4)
        rel["offset_servidor"] = {"offset_s": rs.offset_s, "confiavel": rs.confiavel,
                                  "idade_implicita_s": round(rs.idade_implicita_s, 1), "amostras": rs.amostras}
        if not rs.confiavel:
            avi.append("offset do servidor nao calibrado com confianca (mercado fechado?): use offset_fixo_s")
        lv = ColetorLivro(terminal, simbolo, relogio, BarramentoLocal())
        assinou = lv.assinar() if caps.livro else False
        lv.sondar()
        rel["livro"] = {"assinado": assinou, "profundidade_declarada": lv.profundidade_declarada,
                        "profundidade_observada": lv.profundidade_observada}
        lv.liberar()
        if caps.livro and not assinou:
            avi.append("market_book_add falhou: sem livro L2")
        # order_check SEM envio
        if conta.trade_mode == "demo" and login_esperado is not None and servidor_esperado is not None:
            ex = ExecucaoDemo(terminal, ConfigDemo(login=login_esperado, servidor=servidor_esperado, simbolo=simbolo,
                                                   modo_config=modo_config, conta=caps.conta), relogio, relogio_srv=rs)
            try:
                rel["order_check"] = ex.checar_sem_enviar()
                if not rel["order_check"].get("ok"):
                    avi.append(f"order_check nao passou: {rel['order_check']}")
            except GuardaDemoViolada as e:
                rec.append(f"guarda demo: {e}")
                rel["order_check"] = {"ok": False, "motivo": f"guarda demo: {e}"}
        else:
            rel["order_check"] = {"ok": False, "motivo": "nao executado (conta nao demo ou login/servidor nao informados)"}
    rel["ok"] = not rec
    return rel


AVISO_EA_MT4 = "AVISO: EA mql4/RobowinBridge.mq4 NAO compilado/verificado em MT4 real"


def _origem(terminal: Any, rotulo: str) -> str:
    aviso = f" - {AVISO_EA_MT4}" if rotulo == "MT4" else ""
    if getattr(terminal, "eh_duble", False):
        return f"DUBLE {rotulo} (dados sinteticos; NAO e o terminal real){aviso}"
    return f"terminal {rotulo} real" + (" (ponte EA)" if rotulo == "MT4" else "") + aviso


def texto_relatorio(rel: dict[str, Any]) -> str:
    L = [f"=== DIAGNOSTICO {rel.get('plataforma', 'mt5').upper()} (robowin) ===", f"Origem: {rel['origem']}", f"Gerado em (UTC): {rel['gerado_em_utc']}"]
    c = rel.get("conta")
    if c:
        L += ["", "[Conta]", f"  login: {c['login']}   servidor: {c['servidor']}",
              f"  modo: {c['trade_mode']}   margem: {c['margin_mode']}",
              f"  trade_allowed: {c['trade_allowed']}   trade_expert: {c['trade_expert']}"]
    k = rel.get("contrato")
    if k:
        L += ["", "[Contrato vigente]"]
        if k.get("codigo_calendario"):
            L += [f"  calendario B3: {k['codigo_calendario']}   simbolo da corretora: {k['simbolo_corretora']}",
                  f"  vencimento: {k['vencimento']}   outros: {', '.join(k['outros_contratos']) or '-'}"]
        else:
            L += ["  RECUSADO: " + " | ".join(k.get("recusado", []))]
    e = rel.get("especificacoes")
    if e:
        L += ["", "[Especificacoes]", f"  tick: {e['tick_pontos']} pts (esperado {e['esperado']['tick_pontos']})",
              f"  valor do ponto: R$ {e['ponto_valor_brl']:.2f} (esperado R$ {e['esperado']['ponto_valor_brl']:.2f})",
              f"  lote: {e['lote']}   filling_mode: {e['filling_mode']}   trade_exemode: {e['trade_exemode']}"]
    if "livro" in rel:
        b = rel["livro"]
        L += ["", "[Livro L2]", f"  assinado: {b['assinado']}   profundidade declarada: {b['profundidade_declarada']}"
              f"   observada: {b['profundidade_observada']}"]
    if "offset_servidor" in rel:
        o = rel["offset_servidor"]
        L += ["", "[Relogio do servidor]", f"  offset vs UTC: {o['offset_s']:.0f} s   confiavel: {o['confiavel']}"
              f"   idade implicita do tick: {o['idade_implicita_s']} s"]
    L += ["", "[Ativos (symbols_get)]", "  " + (", ".join(rel.get("ativos", [])) or "-")]
    oc = rel.get("order_check")
    if oc:
        L += ["", "[order_check - sem envio]", f"  ok: {oc.get('ok')}   retcode: {oc.get('retcode')}   {oc.get('comentario', oc.get('motivo', ''))}"]
    if rel["avisos"]:
        L += ["", "AVISOS:"] + [f"  - {a}" for a in rel["avisos"]]
    if rel["recusas"]:
        L += ["", "RECUSAS (nao operar):"] + [f"  - {a}" for a in rel["recusas"]]
    L += ["", "RESULTADO: " + ("APTO para a simulacao em conta DEMO" if rel["ok"] else "NAO APTO")]
    return "\n".join(L)


def _gravar_relatorio(rel: dict[str, Any], destino: Path) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    carimbo = dt.datetime.fromisoformat(rel["gerado_em_utc"]).strftime("%Y%m%d-%H%M%S")
    j = destino / f"diagnostico-{carimbo}.json"
    j.write_text(json.dumps(rel, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (destino / f"diagnostico-{carimbo}.txt").write_text(texto_relatorio(rel), encoding="utf-8")
    return j


# ----------------------------------------------------------------------------------------------
# utilidades de CLI
# ----------------------------------------------------------------------------------------------
def _config_mt5(args: argparse.Namespace) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    caminho = Path(getattr(args, "config", "") or "")
    if caminho.is_file():
        toml = carregar_toml(caminho)
        cfg = {"modo_config": toml.get("geral", {}).get("conta", ""), **toml.get("mt5", {})}
    return cfg


def _plataforma(args: argparse.Namespace, cfg: dict[str, Any]) -> str:
    """`--plataforma` > `[mt5].plataforma` > 'mt5'. Valor invalido levanta ValueError."""
    return normalizar_plataforma(getattr(args, "plataforma", None) or cfg.get("plataforma"))


def _terminal(args: argparse.Namespace, cfg: dict[str, Any], relogio: Relogio) -> Any:
    if _plataforma(args, cfg) == "mt4":
        from .conector_mt4 import ConectorMT4
        if args.duble:  # servidor ZeroMQ falso (thread) falando o MESMO protocolo do EA
            from .duble_mt4 import ServidorDubleMT4, criar_duble_mt4_padrao
            srv = ServidorDubleMT4(criar_duble_mt4_padrao(relogio, n_ticks=int(getattr(args, "ticks_duble", 6000))))
            return ConectorMT4(srv.iniciar())
        return ConectorMT4(getattr(args, "mt4_endereco", None) or cfg.get("mt4_endereco") or "tcp://127.0.0.1:5560",
                           token=str(cfg.get("mt4_token") or "") or None)  # None => variavel ROBOWIN_MT4_TOKEN
    if args.duble:
        from .duble import criar_duble_padrao
        return criar_duble_padrao(relogio, n_ticks=int(getattr(args, "ticks_duble", 6000)))
    from .conector import ConectorMT5
    return ConectorMT5(caminho_terminal=args.terminal or cfg.get("caminho_terminal"),
                       login=args.login or cfg.get("login"), servidor=args.servidor or cfg.get("servidor"))


def _comuns(p: argparse.ArgumentParser) -> None:
    p.add_argument("--duble", action="store_true", help="usa o duble sintetico em vez do terminal MT5 real")
    p.add_argument("--config", default="config/robowin.toml", help="TOML com [geral].conta e opcional [mt5]")
    p.add_argument("--ativo", default=None, help="simbolo_base do perfil em config/instrumentos.toml (padrao: [geral].simbolo_base)")
    p.add_argument("--login", type=int, default=None)
    p.add_argument("--servidor", default=None)
    p.add_argument("--terminal", default=None, help="caminho do terminal portatil (terminal64.exe)")
    p.add_argument("--plataforma", choices=PLATAFORMAS, default=None,
                   help="mt5 (padrao) ou mt4 (ponte EA mql4/RobowinBridge.mq4); padrao: [mt5].plataforma do TOML")
    p.add_argument("--mt4-endereco", default=None,
                   help="endpoint ZeroMQ do EA (padrao [mt5].mt4_endereco ou tcp://127.0.0.1:5560)")


def _perfil(args: argparse.Namespace, *, estrito: bool) -> PerfilInstrumento:
    """Perfil do ativo (`--ativo` > [geral].simbolo_base). Estrito: perfil a_confirmar BLOQUEIA (PerfilNaoConfirmado)."""
    bruto = carregar_toml(args.config) if Path(args.config).is_file() else {}
    return perfil_da_config(bruto, args.ativo, permitir_a_confirmar=not estrito)


def _servidor_duble(plataforma: str) -> str:
    return "BROKERMT4-Demo" if plataforma == "mt4" else "XPMT5-DEMO"


def _cmd_diagnostico(args: argparse.Namespace) -> int:
    try:
        perfil = _perfil(args, estrito=False)  # o diagnostico REPORTA o bloqueio a_confirmar em vez de abortar
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    cfg = _config_mt5(args)
    try:
        plat = _plataforma(args, cfg)
    except ValueError as e:
        print(f"ERRO: {e}")
        return 2
    relogio = RelogioReal()
    try:
        term = _terminal(args, cfg, relogio)
    except Exception as e:  # noqa: BLE001 - MT5IndisponivelErro etc.
        print(f"Nao foi possivel abrir o terminal {plat.upper()}: {e}")
        return 3
    login = args.login or cfg.get("login") or (900001 if args.duble else None)
    servidor = args.servidor or cfg.get("servidor") or (_servidor_duble(plat) if args.duble else None)
    rel = executar_diagnostico(term, perfil, login_esperado=login, servidor_esperado=servidor, relogio=relogio,
                               modo_config=cfg.get("modo_config") or "demo_mt5", plataforma=plat)
    arq = _gravar_relatorio(rel, Path(args.saida))
    print(texto_relatorio(rel))
    print(f"\nRelatorio gravado em {arq} (+ .txt)")
    term.shutdown()
    return 0 if rel["ok"] else 1


def limites_do_gateway(args: argparse.Namespace, perfil: PerfilInstrumento, plataforma: str, login: int,
                       servidor: str) -> tuple[Any, str]:
    """Limites do GUARDIAO (contratos, janela, perda diaria, stop) = os do usuario+conta ativos ou, sem usuario, os do
    arquivo unico. Terminal real: exige aprovacao valida (ErroConfig se nao houver). Com usuario, a conta do gateway
    (plataforma, servidor, login) TEM de ser a conta aprovada. No DUBLE (sintetico) sem aprovacao, segue rotulado."""
    from robowin.risco.usuarios import ContaDivergente, risco_para, selecao_de
    sel = selecao_de(args.config, getattr(args, "usuario", None), getattr(args, "conta", None),
                     getattr(args, "usuarios", None))
    if sel.ativa:
        motivo = sel.chave.confere(plataforma, servidor, login)
        if motivo:
            raise ContaDivergente(f"a conta do gateway ({plataforma}:{servidor}:{login}) difere da conta aprovada "
                                  f"{sel.chave.texto} do usuario {sel.usuario}: {motivo}")
    try:
        cfg, _, arq = risco_para(sel, perfil, args.risco, args.aprovacao, ao_vivo=True)
        return cfg, f"{sel.descricao()} ({arq}, aprovado)"
    except ErroConfig as e:
        if not args.duble:
            raise
        cfg, erro, arq = risco_para(sel, perfil, args.risco, args.aprovacao, ao_vivo=False)
        return cfg, f"{sel.descricao()} ({arq}) risco_nao_aprovado - DUBLE SINTETICO: {erro or e}"


def _cmd_gateway(args: argparse.Namespace) -> int:
    from .gateway import ConfigGateway, rodar_gateway_zmq
    from .guardiao import ConfigGuardiao
    try:
        perfil = _perfil(args, estrito=True)
    except ErroConfig as e:
        print(f"ERRO (operacao bloqueada): {e}")
        return 2
    cfg = _config_mt5(args)
    try:
        plat = _plataforma(args, cfg)
    except ValueError as e:
        print(f"ERRO (operacao bloqueada): {e}")
        return 2
    relogio = RelogioReal()
    login = args.login or cfg.get("login") or (900001 if args.duble else None)
    servidor = args.servidor or cfg.get("servidor") or (_servidor_duble(plat) if args.duble else None)
    if login is None or servidor is None:
        print("Informe --login e --servidor (ou [mt5] no TOML): a guarda demo exige ambos exatos.")
        return 2
    if (cfg.get("modo_config") or "demo_mt5") != "demo_mt5":
        print(f"[geral].conta = '{cfg.get('modo_config')}': o gateway so inicia com 'demo_mt5'.")
        return 2
    try:
        risco, origem_risco = limites_do_gateway(args, perfil, plat, int(login), str(servidor))
    except ErroConfig as e:
        print(f"ERRO (operacao bloqueada): {e}")
        return 2
    print(f"Limites do guardiao: {origem_risco}")
    gcfg = ConfigGateway(
        login=int(login), servidor=str(servidor), modo_config="demo_mt5", dir_log=Path(args.dir_log), perfil=perfil,
        offset_fixo_s=args.offset_fixo_s, plataforma=plat,
        guardiao=ConfigGuardiao.de_risco(risco, arquivo_emergencia=caminho_flag_emergencia(args.config, args.arquivo_emergencia),
                                         exigir_dia_util=not args.duble))
    caminho_cfg = Path(getattr(args, "config", "") or "")
    gcfg.modo_mm = bool(args.modo_mm or (carregar_toml(caminho_cfg).get("mm", {}).get("demo_mm") if caminho_cfg.is_file() else False))
    if args.duble:
        # o duble sintetico nao tem horario de pregao: libera a janela so para exercitar a infraestrutura
        gcfg.guardiao.entradas_de, gcfg.guardiao.entradas_ate, gcfg.guardiao.zeragem = "00:00", "23:58", "23:59"

    def fabrica() -> Any:
        return _terminal(args, cfg, relogio)

    print(f"Gateway {plat.upper()} {'(DUBLE sintetico) ' if args.duble else ''}publicando em {args.pub}, comandos em {args.cmd}")
    rodar_gateway_zmq(fabrica, gcfg, args.pub, args.cmd, duracao_s=args.duracao_s, relogio=relogio)
    return 0


def _cmd_coletar(args: argparse.Namespace) -> int:
    try:
        perfil = _perfil(args, estrito=True)
    except ErroConfig as e:
        print(f"ERRO (operacao bloqueada): {e}")
        return 2
    cfg = _config_mt5(args)
    try:
        _plataforma(args, cfg)
    except ValueError as e:
        print(f"ERRO (operacao bloqueada): {e}")
        return 2
    relogio = RelogioReal()
    term = _terminal(args, cfg, relogio)
    if not term.initialize():
        print(f"initialize falhou: {term.last_error()}")
        return 3
    try:
        res = resolver_contrato(term, dt.datetime.fromtimestamp(relogio.agora_ns() / 1e9, dt.UTC).date(), perfil)
    except ContratoRecusado as e:
        print("Contrato recusado: " + "; ".join(e.motivos))
        return 1
    simbolo = res.simbolo_corretora
    rs = calibrar(term, simbolo, relogio, amostras=3, intervalo_s=0.3, offset_fixo_s=args.offset_fixo_s)
    bus = BarramentoLocal()
    contagem = {"tick": 0, "livro": 0}
    bus.assinar("tick", lambda m: contagem.__setitem__("tick", contagem["tick"] + 1))
    bus.assinar("livro", lambda m: contagem.__setitem__("livro", contagem["livro"] + 1))
    log = LogBruto(args.dir_log, relogio)
    tk = ColetorTicks(term, simbolo, relogio, rs, bus, log, inicio_ns=relogio.agora_ns() - int(args.recuo_s * 1e9))
    lv = ColetorLivro(term, simbolo, relogio, bus, log)
    if capacidades_do_terminal(term).livro:  # MT4: sem livro; so ticks
        lv.assinar()
    fim = time.monotonic() + args.duracao_s
    while time.monotonic() < fim:
        tk.sondar()
        lv.sondar()
        time.sleep(0.05)
    lv.liberar()
    log.fechar()
    print(f"{simbolo}: {contagem['tick']} ticks, {contagem['livro']} livros; log em {args.dir_log}; "
          f"backfills: {len(tk.backfills)}; lacunas sem backfill: {tk.lacunas_sem_backfill}; "
          f"duplicados ignorados: {tk.duplicados}")
    return 0


def _cmd_emergencia(args: argparse.Namespace) -> int:
    rc = 0
    try:
        plat = normalizar_plataforma(getattr(args, "plataforma", None) or _config_mt5(args).get("plataforma"))
    except ValueError as e:
        print(f"ERRO: {e}")
        return 2
    if plat == "mt4":
        print("MT4 (hedging): o gateway fecha CADA posicao pelo seu ticket e cancela as pendentes; nunca por ordem oposta.")
    if not args.sem_arquivo:
        arq = caminho_flag_emergencia(args.config, args.arquivo)
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(f"EMERGENCIA acionada em {dt.datetime.now(dt.UTC).isoformat()} motivo={args.motivo}\n",
                       encoding="utf-8")
        print(f"Arquivo {arq} criado: o guardiao do gateway zera e trava em ate ~1 s.")
    if args.comando:
        from .canais import ClienteComandosZMQ
        cli = ClienteComandosZMQ(args.comando)
        r = cli.enviar({"tipo": "emergencia", "origem": f"cli: {args.motivo} [{plat}]"}, timeout_s=args.timeout_s)
        cli.fechar()
        if r is None:
            print(f"SEM CONFIRMACAO do gateway no prazo. Use o 'fechar tudo' manual do {plat.upper()}.")
            return 1
        print(f"Confirmacao: zerado={r.get('zerado')} travado={r.get('travado')} posicoes={r.get('posicoes')} "
              f"ordens={r.get('ordens')}")
        rc = 0 if r.get("confirmado") else 1
    return rc


def registrar(sub: Any) -> None:
    p = sub.add_parser("diagnostico", help="diagnostico do MT5 (conta, contrato, specs, livro, offset, order_check)")
    _comuns(p)
    p.add_argument("--saida", default="dados_locais/diagnostico")
    p.set_defaults(func=_cmd_diagnostico)

    g = sub.add_parser("gateway", help="gateway MT5: coleta, guardiao e execucao demo via ZeroMQ")
    _comuns(g)
    g.add_argument("--pub", default="tcp://127.0.0.1:5555")
    g.add_argument("--cmd", default="tcp://127.0.0.1:5556")
    g.add_argument("--dir-log", default="dados_locais/mt5_bruto")
    g.add_argument("--arquivo-emergencia", default=None,
                   help="padrao: <[geral].dados do robowin.toml>/EMERGENCIA (mesmo caminho do painel e do nucleo)")
    g.add_argument("--offset-fixo-s", type=int, default=None)
    g.add_argument("--duracao-s", type=float, default=None)
    g.add_argument("--modo-mm", action="store_true",
                   help="aceita as cotacoes do MM na demo (uma limitada por lado); tambem ligado por [mm].demo_mm = true")
    g.add_argument("--risco", default="config/risco.toml", help="modo arquivo unico (sem --usuario/--conta)")
    g.add_argument("--aprovacao", default="config/aprovacoes/risco-v1.toml", help="modo arquivo unico")
    from robowin.risco.usuarios import adicionar_args
    adicionar_args(g)
    g.set_defaults(func=_cmd_gateway)

    c = sub.add_parser("coletar", help="coleta ticks e livro (sem ordens) gravando o log bruto")
    _comuns(c)
    c.add_argument("--dir-log", default="dados_locais/mt5_bruto")
    c.add_argument("--duracao-s", type=float, default=60.0)
    c.add_argument("--recuo-s", type=float, default=5.0, help="quanto passado buscar no inicio")
    c.add_argument("--offset-fixo-s", type=int, default=None)
    c.set_defaults(func=_cmd_coletar)

    e = sub.add_parser("emergencia", help="acao de emergencia: cria o arquivo EMERGENCIA e/ou envia o comando")
    e.add_argument("--arquivo", default=None,
                   help="padrao: <[geral].dados do robowin.toml>/EMERGENCIA (mesmo caminho do painel e do gateway)")
    e.add_argument("--config", default="config/robowin.toml")
    e.add_argument("--sem-arquivo", action="store_true")
    e.add_argument("--plataforma", choices=PLATAFORMAS, default=None, help="mt5 (padrao) ou mt4; padrao: [mt5].plataforma do TOML")
    e.add_argument("--comando", default=None, help="endereco ROUTER do gateway (ex.: tcp://127.0.0.1:5556)")
    e.add_argument("--motivo", default="acionada pelo operador")
    e.add_argument("--timeout-s", type=float, default=10.0)
    e.set_defaults(func=_cmd_emergencia)
