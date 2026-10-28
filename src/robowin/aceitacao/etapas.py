"""Etapas A1..A11 da aceitacao no terminal real (SOMENTE conta DEMO).

SEGURANCA (inegociavel, por construcao):
- Este modulo NAO contem nenhum envio de ordem. TODA ordem passa por `mt5.execucao_demo.ExecucaoDemo` (direto, nas etapas A5/A6, ou
  pelo `Gateway`, que o hospeda, nas etapas A8/A9). O teste estatico de envio unico continua valendo.
- Sem `ctx.confirmo` (flag --confirmo-conta-demo) o executor e o gateway NUNCA sao criados: so ha etapas de leitura.
- Conta real/contest, servidor sem 'DEMO' ou login diferente do esperado => a execucao ABORTA (nenhuma etapa seguinte roda).
- Perfil `a_confirmar` => as etapas de ordem nao rodam (NAO VERIFICAVEL com o motivo).
- Ordens de teste: 1 contrato (lote do perfil); a limitada fica LONGE do mercado e e cancelada; a de mercado leva SL e e
  fechada por TICKET. Ao final da etapa: posicao zero e nenhuma ordem pendente (com limpeza em `finally`).
- Etapa sem pre-requisito (pregao fechado, sem chave de API, plataforma errada...) e NAO VERIFICAVEL, nunca aprovada.
"""
from __future__ import annotations

import datetime as dt
import importlib.metadata
import math
import platform
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from robowin.core import calendario_b3
from robowin.core.barramento import BarramentoLocal
from robowin.core.config import papeis
from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.relogio import Relogio
from robowin.core.tipos import ComandoOrdem, ConfigRisco, Lado, LivroSnapshot, Tick
from robowin.dados import qualidade
from robowin.ia.catalogo import ErroCatalogo, dividir_modelo
from robowin.ia.cli_cmds import _catalogo, medir_latencia, provedor_para_cli, tem_chave
from robowin.mt5 import constantes as C
from robowin.mt5.canais import CanalComandosLocal
from robowin.mt5.capacidades import CapacidadesTerminal
from robowin.mt5.coleta import ColetorLivro, ColetorTicks
from robowin.mt5.conta import ler_estado_conta
from robowin.mt5.contrato import ContratoRecusado, escolher_simbolo, resolver_contrato
from robowin.mt5.execucao_demo import ConfigDemo, ExecucaoDemo
from robowin.mt5.gateway import ConfigGateway, Gateway
from robowin.mt5.guardiao import ConfigGuardiao
from robowin.mt5.specs import EspecEsperada, valor_ponto_brl
from robowin.mt5.tempo_servidor import RelogioServidor, calibrar

from .matriz import (
    REPROVADO,
    ResultadoEtapa,
    aprovada,
    nao_verificavel,
    reprovada,
)

MAGIC_ACEITACAO = 990777
DONO = "aceitacao"
FRESCOR_MAX_S = 30  # cotacao mais velha que isto = mercado parado: ordens nao rodam
HB_BLOQUEIO_S = 2   # heartbeat curto de TESTE do guardiao (producao: 15 s)
HB_ZERA_S = 5       # idem (producao: 60 s)
FOLGA_ZERA_S = 8    # zerar por queda do nucleo em ate HB_ZERA_S + esta folga (ciclo do guardiao = 1 s)


@dataclass
class Contexto:
    terminal: Any
    plataforma: str
    caps: CapacidadesTerminal
    perfil: PerfilInstrumento
    risco: ConfigRisco
    cfg: dict[str, Any]
    relogio: Relogio
    dormir: Callable[[float], None]
    duble: bool
    login: int | None
    servidor: str | None
    confirmo: bool
    saida: Path
    minutos_coleta: float = 5
    repeticoes: int = 10
    pausa_s: float = 3.0
    limite_latencia_ms: float = 1500.0
    msgs_por_min: int = 20
    intervalo_poll_s: float = 0.25
    pular_ia: bool = False
    # estado compartilhado entre etapas
    t_inicio_ns: int = 0
    abortar: str | None = None
    conta_ok: bool = False
    resolucao: Any = None
    rs: RelogioServidor | None = None
    ex: ExecucaoDemo | None = None
    envios_gateway: int = 0
    latencias: dict[str, dict[str, Any]] = field(default_factory=dict)
    versoes: dict[str, str] = field(default_factory=dict)
    conta: dict[str, Any] = field(default_factory=dict)
    resultados: dict[str, ResultadoEtapa] = field(default_factory=dict)
    deals_teste: int = 0
    _n: int = 0

    @property
    def simbolo(self) -> str:
        return str(self.resolucao.simbolo_corretora)

    def novo_id(self, prefixo: str) -> str:
        self._n += 1
        return f"acc-{prefixo}-{self.relogio.agora_ns() // 1_000_000}-{self._n}"

    @property
    def envios(self) -> int:
        """Total de order_send efetivamente chamados (executor direto + gateway). 0 sem a flag, por construcao."""
        return (self.ex.envios if self.ex is not None else 0) + self.envios_gateway


# ----------------------------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------------------------
def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _agora_sp(ctx: Contexto) -> dt.datetime:
    return dt.datetime.fromtimestamp(ctx.relogio.agora_ns() / 1e9, calendario_b3.SP)


def _pct(valores: list[float]) -> dict[str, float]:
    if not valores:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    p = np.percentile(valores, [50, 95, 99])
    return {"p50": round(float(p[0]), 1), "p95": round(float(p[1]), 1), "p99": round(float(p[2]), 1)}


def _esperar(ctx: Contexto, condicao: Callable[[], bool], timeout_s: float = 6, passo_s: float = 0.25) -> bool:
    t = 0.0
    while True:
        if condicao():
            return True
        if t >= timeout_s:
            return False
        ctx.dormir(passo_s)
        t += passo_s


def _pregao(ctx: Contexto, margem_min: int = 0) -> tuple[bool, str]:
    """Pregao aberto = dia de pregao + dentro da janela de entradas do risco (com margem) + feed vivo (offset calibrado)."""
    a = _agora_sp(ctx)
    if not calendario_b3.eh_pregao(a.date()):
        return False, f"{a.date()} nao e dia de pregao da B3"
    m = a.hour * 60 + a.minute
    de, ate = _hhmm(ctx.risco.entradas_de), _hhmm(ctx.risco.entradas_ate)
    if not de <= m < ate - margem_min:
        return False, (f"{a:%H:%M} (Sao Paulo) fora da janela de entradas {ctx.risco.entradas_de}-{ctx.risco.entradas_ate}"
                       f" (+{margem_min} min de folga para a etapa)")
    if ctx.rs is None or not ctx.rs.confiavel:
        return False, "sem ticks novos para calibrar o relogio do servidor (mercado parado ou fora do pregao)"
    return True, ""


def _cotacao(ctx: Contexto) -> tuple[Any, float]:
    """(tick, idade_s). Levanta RuntimeError se nao ha cotacao valida."""
    tk = ctx.terminal.symbol_info_tick(ctx.simbolo)
    if tk is None or not float(tk.bid) > 0 or not float(tk.ask) > 0:
        raise RuntimeError("sem cotacao valida (bid/ask)")
    assert ctx.rs is not None
    idade = (ctx.relogio.agora_ns() - ctx.rs.servidor_ms_para_utc_ns(int(tk.time_msc))) / 1e9
    return tk, idade


def _distancia_longe(ctx: Contexto, preco: float) -> float:
    """Distancia da limitada de teste ao mercado: >= 100 ticks e >= 0,5% do preco (nunca executa)."""
    return ctx.perfil.arredondar(max(ctx.perfil.pontos(100), preco * 0.005))


def _estado_simbolo(ctx: Contexto) -> tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]:
    ps = ctx.terminal.positions_get(symbol=ctx.simbolo)
    os_ = ctx.terminal.orders_get(symbol=ctx.simbolo)
    return (None if ps is None else tuple(ps)), (None if os_ is None else tuple(os_))


def _executor(ctx: Contexto) -> ExecucaoDemo:
    """UNICO lugar que cria o executor, e so com a flag. Sem ela, levanta (defesa em profundidade)."""
    if not ctx.confirmo:
        raise RuntimeError("executor pedido sem --confirmo-conta-demo: nenhuma ordem pode ser enviada")
    if ctx.ex is None:
        assert ctx.login is not None and ctx.servidor is not None and ctx.rs is not None
        ctx.ex = ExecucaoDemo(
            ctx.terminal,
            ConfigDemo(login=ctx.login, servidor=ctx.servidor, simbolo=ctx.simbolo, modo_config="demo_mt5",
                       lote=ctx.perfil.volume_unidade, conta=ctx.caps.conta, msgs_por_min=ctx.msgs_por_min),
            ctx.relogio, relogio_srv=ctx.rs)
    return ctx.ex


def _pre_ordens(ctx: Contexto, etapa: str, nome: str, criterio: str, margem_min: int = 2,
                global_limpo: bool = False) -> ResultadoEtapa | None:
    """None se as etapas de ordem podem rodar; senao o resultado NAO VERIFICAVEL com o motivo."""
    def nv(m: str) -> ResultadoEtapa:
        return nao_verificavel(etapa, nome, criterio, m)
    if not ctx.confirmo:
        return nv("sem --confirmo-conta-demo: nenhuma ordem e enviada (somente etapas de leitura)")
    if ctx.perfil.a_confirmar:
        return nv(f"perfil {ctx.perfil.simbolo_base} a_confirmar ({'; '.join(ctx.perfil.a_confirmar_itens) or 'sem fonte'}): "
                  "etapas de ordem NAO rodam ate confirmar o perfil em config/instrumentos.toml")
    if not ctx.conta_ok:
        return nv("A2 (conta) nao aprovada: nenhuma ordem e enviada")
    if ctx.resolucao is None:
        return nv("A3 (contrato vigente e especificacoes) nao aprovada: sem simbolo validado para operar")
    ok, motivo = _pregao(ctx, margem_min)
    if not ok:
        return nv(f"pregao nao esta aberto: {motivo}")
    ps, os_ = _estado_simbolo(ctx)
    if ps is None or os_ is None:
        return nv("positions_get/orders_get sem resposta do terminal")
    if ps or os_:
        return nv(f"a conta demo ja tem {len(ps)} posicao(oes) e {len(os_)} ordem(ns) em {ctx.simbolo}: zere manualmente antes")
    if global_limpo:
        gp, go = ctx.terminal.positions_get(), ctx.terminal.orders_get()
        if gp is None or go is None or gp or go:
            return nv("o kill switch zera TODA a conta demo e ela tem posicoes/ordens abertas (de outro uso): "
                      "feche-as manualmente antes desta etapa")
    return None


def _limpar(ctx: Contexto) -> list[str]:
    """Cancela e zera SO o que tem o magic da aceitacao (nunca mexe em ordens/posicoes do usuario). Devolve o que limpou."""
    feito: list[str] = []
    if ctx.ex is None or ctx.resolucao is None:
        return feito
    ex = ctx.ex
    try:
        for o in ctx.terminal.orders_get(symbol=ctx.simbolo) or ():
            if int(o.magic) == MAGIC_ACEITACAO:
                ex.cancelar_ordem(int(o.ticket), dono=DONO, prioritaria=True)
                feito.append(f"ordem {int(o.ticket)} cancelada")
        for p in ctx.terminal.positions_get(symbol=ctx.simbolo) or ():
            if int(p.magic) == MAGIC_ACEITACAO:
                ex.zerar_posicao(int(p.ticket), dono=DONO, motivo="limpeza da aceitacao", forcar_dono=True)
                feito.append(f"posicao {int(p.ticket)} zerada")
    except Exception as e:  # noqa: BLE001 - a limpeza e melhor esforco; o resultado final confere o estado
        feito.append(f"limpeza interrompida: {type(e).__name__}: {e}")
    return feito


def _lat(ack: Any) -> float:
    return float(max(0, int(ack.t_done_msc) - int(ack.t_setup_msc)))


# ----------------------------------------------------------------------------------------------
# A1 ambiente
# ----------------------------------------------------------------------------------------------
def a1_ambiente(ctx: Contexto) -> list[ResultadoEtapa]:
    out: list[ResultadoEtapa] = []
    nome = "Ambiente"
    py = sys.version_info
    so = platform.platform()
    ctx.versoes.update({"so": so, "python": platform.python_version(), "plataforma_terminal": ctx.plataforma.upper()})
    crit1 = "Python >= 3.12; e, para MT5 real, sistema Windows (o pacote MetaTrader5 so existe nele)"
    erros = []
    if py < (3, 12):
        erros.append(f"Python {platform.python_version()} < 3.12")
    if ctx.plataforma == "mt5" and not ctx.duble and platform.system() != "Windows":
        erros.append(f"sistema {platform.system()}: o MT5 real exige Windows")
    out.append(reprovada("A1.1", f"{nome}: SO e Python", crit1, "; ".join(erros), so=so) if erros else
               aprovada("A1.1", f"{nome}: SO e Python", crit1, f"{so}; Python {platform.python_version()}", so=so))

    crit2 = ("MT5: pacote MetaTrader5 instalado (versao registrada); MT4: handshake da ponte responde com o protocolo esperado "
             "(EA, versao, build)")
    if ctx.plataforma == "mt5":
        if ctx.duble:
            ctx.versoes["MetaTrader5"] = "(duble)"
            out.append(aprovada("A1.2", f"{nome}: pacote MetaTrader5", crit2, "duble sintetico: pacote nao usado"))
        else:
            try:
                ctx.versoes["MetaTrader5"] = importlib.metadata.version("MetaTrader5")
                out.append(aprovada("A1.2", f"{nome}: pacote MetaTrader5", crit2, f"MetaTrader5 {ctx.versoes['MetaTrader5']}"))
            except importlib.metadata.PackageNotFoundError:
                ctx.versoes["MetaTrader5"] = "ausente"
                out.append(reprovada("A1.2", f"{nome}: pacote MetaTrader5", crit2,
                                     "pacote MetaTrader5 nao instalado (uv sync --extra mt5)"))
    else:
        ctx.versoes["MetaTrader5"] = "(nao usado: plataforma MT4)"
    ok_init = False
    try:
        ok_init = bool(ctx.terminal.initialize())
    except Exception as e:  # noqa: BLE001
        out.append(reprovada("A1.3", f"{nome}: terminal {ctx.plataforma.upper()}", "initialize() devolve True e o terminal esta conectado",
                             f"initialize levantou {type(e).__name__}: {e}"))
        ctx.abortar = "terminal nao inicializou"
        return out
    crit3 = "initialize() devolve True, terminal conectado e (MT4) handshake do EA com versao do protocolo compativel"
    if not ok_init:
        out.append(reprovada("A1.3", f"{nome}: terminal {ctx.plataforma.upper()}", crit3,
                             f"initialize falhou: {ctx.terminal.last_error()}"))
        ctx.abortar = "terminal nao inicializou"
        return out
    ti = ctx.terminal.terminal_info()
    conectado = bool(getattr(ti, "connected", False)) if ti is not None else False
    build = int(getattr(ti, "build", 0) or 0) if ti is not None else 0
    caminho = str(getattr(ti, "path", "")) if ti is not None else ""
    ola = dict(getattr(ctx.terminal, "ola", {}) or {})
    ctx.versoes["terminal_build"] = str(build)
    if ctx.plataforma == "mt4":
        ctx.versoes["ea"] = f"{ola.get('ea', '?')} {ola.get('ea_versao', '?')}"
    det = f"conectado={conectado}; build={build}; caminho={caminho or 'n/d'}" + (f"; EA={ctx.versoes['ea']}" if ola else "")
    out.append(aprovada("A1.3", f"{nome}: terminal {ctx.plataforma.upper()}", crit3, det, build=build) if conectado else
               reprovada("A1.3", f"{nome}: terminal {ctx.plataforma.upper()}", crit3,
                         "terminal sem conexao com o servidor da corretora (connected=False)"))
    return out


# ----------------------------------------------------------------------------------------------
# A2 conta (+ guarda de identidade que ABORTA)
# ----------------------------------------------------------------------------------------------
def a2_conta(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Conta"
    c_id = "conta DEMO (nunca real/contest), servidor com 'DEMO' e login/servidor iguais aos esperados"
    est = ler_estado_conta(ctx.terminal)
    if est is None:
        ctx.abortar = "account_info indisponivel"
        return [reprovada("A2.1", f"{nome}: identidade", c_id, "account_info indisponivel: terminal sem resposta")]
    ai = ctx.terminal.account_info()
    moeda = str(getattr(ai, "currency", "") or "").strip().upper()
    ctx.conta = {"login": est.login, "servidor": est.servidor, "modo": est.trade_mode, "margem": est.margin_mode,
                 "moeda": moeda, "trade_allowed": est.trade_allowed, "trade_expert": est.trade_expert}
    problemas: list[str] = []
    if est.trade_mode != "demo":
        problemas.append(f"conta {est.trade_mode.upper()} (so DEMO e aceita)")
    if "DEMO" not in est.servidor.upper():
        problemas.append(f"servidor '{est.servidor}' sem 'DEMO' no nome")
    if ctx.login is None or ctx.servidor is None:
        problemas.append("login/servidor esperados nao informados (--login/--servidor ou [mt5] no TOML)")
    else:
        if est.login != ctx.login:
            problemas.append(f"login {est.login} difere do esperado {ctx.login}")
        if est.servidor != ctx.servidor:
            problemas.append(f"servidor '{est.servidor}' difere do esperado '{ctx.servidor}'")
    out: list[ResultadoEtapa] = []
    if problemas:
        ctx.abortar = "conta nao e a DEMO esperada: " + "; ".join(problemas)
        out.append(reprovada("A2.1", f"{nome}: identidade", c_id, "RECUSADA, execucao abortada: " + "; ".join(problemas)))
        return out
    out.append(aprovada("A2.1", f"{nome}: identidade", c_id, f"DEMO em '{est.servidor}'"))
    c_perm = "trade_allowed e trade_expert (Algo Trading) ligados na conta e no terminal"
    falt = [n for n, v in (("trade_allowed (negociacao)", est.trade_allowed), ("trade_expert (Algo Trading)", est.trade_expert)) if not v]
    out.append(reprovada("A2.2", f"{nome}: permissoes de negociacao", c_perm, "desligado: " + ", ".join(falt) +
                         " (ligue o botao Algo Trading e a opcao de negociacao automatizada)") if falt else
               aprovada("A2.2", f"{nome}: permissoes de negociacao", c_perm, "trade_allowed e trade_expert ligados"))
    esperada = C.NOMES_MARGEM[C.MARGEM_HEDGING] if ctx.caps.conta == "hedging" else None
    c_marg = f"modo de margem coerente com a plataforma ({ctx.caps.conta})"
    if ctx.caps.conta == "hedging":
        bom = est.margin_mode == esperada
    else:
        bom = est.margin_mode in (C.NOMES_MARGEM[C.MARGEM_EXCHANGE], C.NOMES_MARGEM[C.MARGEM_NETTING])
    out.append(aprovada("A2.3", f"{nome}: netting/hedging", c_marg, f"margin_mode={est.margin_mode}") if bom else
               reprovada("A2.3", f"{nome}: netting/hedging", c_marg,
                         f"margin_mode={est.margin_mode} incompativel com {ctx.plataforma.upper()} ({ctx.caps.conta}); "
                         "o projeto exige netting no MT5 e hedging no MT4"))
    c_moeda = (f"moeda da conta == moeda do perfil ({ctx.perfil.moeda}) ou, em qualquer mercado (moeda_lucro do perfil), "
               "par de conversao cotado na corretora (P&L e risco convertidos pela cotacao do instante)")
    if moeda == ctx.perfil.moeda.upper():
        out.append(aprovada("A2.4", f"{nome}: moeda", c_moeda, f"{moeda} == {ctx.perfil.moeda}"))
    elif moeda and ctx.perfil.moeda_lucro:
        from robowin.core.moedas import conversor_do_terminal
        conv = conversor_do_terminal(ctx.terminal, ctx.perfil.moeda, moeda)
        fator = conv.fator() if conv is not None else None
        if conv is not None and fator is not None:
            out.append(aprovada("A2.4", f"{nome}: moeda", c_moeda,
                                f"conta {moeda}, lucro em {ctx.perfil.moeda}: conversao {conv.rota.descrever()} "
                                f"(fator {fator:.6g} no instante)"))
        else:
            out.append(reprovada("A2.4", f"{nome}: moeda", c_moeda,
                                 f"conta {moeda}, lucro em {ctx.perfil.moeda}: sem par de conversao cotado na corretora "
                                 f"({'sem rota' if conv is None else conv.ultimo_motivo}): entradas seriam bloqueadas"))
    else:
        out.append(reprovada("A2.4", f"{nome}: moeda", c_moeda,
                             f"moeda da conta '{moeda or 'n/d'}' diverge da do perfil {ctx.perfil.moeda}: P&L e limites de risco ficariam em outra moeda"))
    ctx.conta_ok = all(r.estado != REPROVADO for r in out)
    return out


# ----------------------------------------------------------------------------------------------
# A3 contrato vigente e especificacoes
# ----------------------------------------------------------------------------------------------
_CHAVES_SPEC = ("tick_size", "valor do ponto", "volume_", "currency_profit", "moeda", "vencimento", "trade_tick", "symbol_info")


def _specs_observadas(info: Any) -> dict[str, Any]:
    if info is None:
        return {}
    chaves = ("trade_tick_size", "trade_tick_value", "volume_min", "volume_step", "currency_profit", "filling_mode",
              "trade_exemode", "ticks_bookdepth", "expiration_time")
    return {k: getattr(info, k) for k in chaves if hasattr(info, k)}


def a3_contrato(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Contrato e especificacoes"
    c1 = "contrato vigente do calendario B3 existe na corretora (symbols_get) e e selecionavel"
    c2 = "tick, valor do ponto, lote (volume_min/step) e moeda do simbolo == perfil confirmado"
    hoje = _agora_sp(ctx).date()
    perfil = ctx.perfil
    try:
        codigo = calendario_b3.contrato_vigente(perfil, hoje)
    except Exception as e:  # noqa: BLE001
        return [reprovada("A3.1", f"{nome}: contrato vigente", c1, f"calendario nao resolveu o contrato: {e}")]
    simbolos = ctx.terminal.symbols_get(f"*{perfil.simbolo_base}*") or ctx.terminal.symbols_get() or ()
    nomes = [str(s.name) for s in simbolos]
    try:
        cand = escolher_simbolo(nomes, codigo, perfil)
    except ContratoRecusado as e:
        return [reprovada("A3.1", f"{nome}: contrato vigente", c1, "; ".join(e.motivos), vigente=codigo)]
    info = ctx.terminal.symbol_info(cand.nome)
    obs = _specs_observadas(info)
    obs["valor_ponto_por_unidade"] = valor_ponto_brl(info) if info is not None else None
    if info is not None and getattr(info, "trade_contract_size", None):  # qualquer mercado: dados do contrato
        obs.update({k: getattr(info, k) for k in ("trade_contract_size", "digits", "point", "volume_max",
                                                  "currency_base", "currency_margin", "trade_calc_mode", "swap_long",
                                                  "swap_short", "swap_mode", "swap_rollover3days")
                    if getattr(info, k, None) is not None})
    if perfil.a_confirmar:
        itens = "; ".join(perfil.a_confirmar_itens) or "valores sem fonte confirmada"
        return [
            aprovada("A3.1", f"{nome}: contrato vigente", c1, f"vigente {codigo}; simbolo na corretora: {cand.nome}", vigente=codigo, simbolo=cand.nome),
            nao_verificavel("A3.2", f"{nome}: especificacoes x perfil", c2,
                            f"perfil {perfil.simbolo_base} a_confirmar ({itens}): nao ha valor confirmado para comparar. "
                            f"Especificacoes OBSERVADAS na corretora (use-as para confirmar o perfil): {obs}", observadas=obs)]
    try:
        res = resolver_contrato(ctx.terminal, hoje, perfil)
    except ContratoRecusado as e:
        spec = [m for m in e.motivos if any(k in m for k in _CHAVES_SPEC)]
        outros = [m for m in e.motivos if m not in spec]
        rows: list[ResultadoEtapa] = []
        rows.append(reprovada("A3.1", f"{nome}: contrato vigente", c1, "; ".join(outros), vigente=codigo) if outros else
                    aprovada("A3.1", f"{nome}: contrato vigente", c1, f"vigente {codigo}; simbolo {cand.nome}", vigente=codigo))
        rows.append(reprovada("A3.2", f"{nome}: especificacoes x perfil", c2, "; ".join(spec) or "recusado", observadas=obs))
        return rows
    ctx.resolucao = res
    esp = EspecEsperada.do_perfil(perfil)
    ok_rs = _calibrar(ctx)
    assert ctx.rs is not None
    venc = res.vencimento.isoformat() if res.vencimento else "sem vencimento"
    return [
        aprovada("A3.1", f"{nome}: contrato vigente", c1,
                 f"{codigo} -> {res.simbolo_corretora} ({venc}); outros contratos: {len(res.outros_contratos)}",
                 vigente=codigo, simbolo=res.simbolo_corretora, vencimento=venc),
        aprovada("A3.2", f"{nome}: especificacoes x perfil", c2,
                 f"tick {esp.tick_pontos} pts, R$ {esp.ponto_valor_brl:.2f}/ponto por lote, lote {esp.lote}, moeda {esp.moeda} "
                 f"(iguais ao perfil). Relogio do servidor: offset {ctx.rs.offset_s:.0f} s, confiavel={ok_rs}",
                 observadas=obs)]


def _calibrar(ctx: Contexto) -> bool:
    rs = calibrar(ctx.terminal, ctx.simbolo, ctx.relogio, amostras=3, intervalo_s=0.4, dormir=ctx.dormir)
    ctx.rs = rs
    return rs.confiavel


# ----------------------------------------------------------------------------------------------
# A4 coleta de ticks/livro por N minutos
# ----------------------------------------------------------------------------------------------
def a4_coleta(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Coleta de ticks e livro"
    crit = (f"{ctx.minutos_coleta:g} min de coleta: ticks com flags BUY/SELL, offset do servidor calibrado e estavel, "
            "livro com profundidade, sem lacuna critica (dados.qualidade)")
    if ctx.resolucao is None:
        return [nao_verificavel("A4", nome, crit, "A3 (contrato vigente e especificacoes) nao aprovada: sem simbolo para coletar")]
    ok, motivo = _pregao(ctx)
    if not ok:
        return [nao_verificavel("A4", nome, crit, f"pregao nao esta aberto: {motivo}")]
    assert ctx.rs is not None
    simbolo = ctx.simbolo
    bus = BarramentoLocal()
    msgs_t: list[dict[str, Any]] = []
    msgs_l: list[dict[str, Any]] = []
    bus.assinar("tick", msgs_t.append)
    bus.assinar("livro", msgs_l.append)
    tk = ColetorTicks(ctx.terminal, simbolo, ctx.relogio, ctx.rs, bus, None, inicio_ns=ctx.relogio.agora_ns() - 1_000_000_000)
    lv = ColetorLivro(ctx.terminal, simbolo, ctx.relogio, bus, None)
    assinou = lv.assinar() if ctx.caps.livro else False
    fim = ctx.relogio.monotonico_ns() + int(ctx.minutos_coleta * 60 * 1e9)
    try:
        while ctx.relogio.monotonico_ns() < fim:
            tk.sondar()
            lv.sondar()
            ctx.dormir(ctx.intervalo_poll_s)
    finally:
        lv.liberar()
    n = len(msgs_t)
    if n == 0:
        return [reprovada("A4", nome, crit, f"janela de pregao aberta, feed calibrado e ZERO ticks em {ctx.minutos_coleta:g} min: "
                                            "confira a permissao de dados de mercado do ativo no terminal")]
    campos = list(Tick.model_fields)
    ticks = [Tick(**{k: m[k] for k in campos}) for m in msgs_t]
    livros = [LivroSnapshot.model_validate({"t_utc_ns": m["t_utc_ns"], "niveis": m["niveis"]}) for m in msgs_l]
    comp = sum(1 for t in ticks if t.flags & C.FLAG_BUY)
    vend = sum(1 for t in ticks if t.flags & C.FLAG_SELL)
    negocios = sum(1 for t in ticks if t.flags & C.FLAG_LAST and t.last > 0)
    out: list[ResultadoEtapa] = []

    # A4.1 ticks e flags
    c41 = "ticks recebidos; (MT5) ticks de negocio com flags BUY/SELL e last > 0"
    if ctx.caps.ticks_de_negocio and not ctx.perfil.tem_negocios:
        # forex/CFD/cripto na corretora: sem negocios publicos; o "volume" e de tick (contagem de cotacoes)
        ok = n > 0
        out.append((aprovada if ok else reprovada)(
            "A4.1", f"{nome}: ticks e flags", c41 + f" ({ctx.perfil.classe}: so cotacoes bid/ask por natureza do mercado)",
            f"{n} ticks de bid/ask (volume de tick; fluxo do agressor desligado por capacidade do instrumento)",
            n_ticks=n))
    elif ctx.caps.ticks_de_negocio:
        if comp + vend == 0 or negocios == 0:
            out.append(reprovada("A4.1", f"{nome}: ticks e flags", c41,
                                 f"{n} ticks mas nenhum de negocio com flag BUY/SELL (compra={comp}, venda={vend}, negocios={negocios}): "
                                 "o fluxo do agressor nao esta disponivel", n_ticks=n, compra=comp, venda=vend))
        else:
            out.append(aprovada("A4.1", f"{nome}: ticks e flags", c41,
                                f"{n} ticks; negocios={negocios} (BUY={comp}, SELL={vend})", n_ticks=n, compra=comp, venda=vend,
                                negocios=negocios))
    else:
        out.append(aprovada("A4.1", f"{nome}: ticks e flags", c41 + " (MT4: sem ticks de negocio por capacidade declarada)",
                            f"{n} ticks de bid/ask; lacunas perdidas pelo EA: {int(getattr(ctx.terminal, 'lacunas_ticks', 0))}",
                            n_ticks=n))

    # A4.2 offset do servidor
    c42 = "offset do servidor calibrado com feed vivo, multiplo de 15 min, e estavel no fim da coleta"
    rs2 = calibrar(ctx.terminal, simbolo, ctx.relogio, amostras=3, intervalo_s=0.4, dormir=ctx.dormir)
    estavel = rs2.confiavel and rs2.offset_ms == ctx.rs.offset_ms
    grade = int(ctx.rs.offset_s) % 900 == 0
    if ctx.rs.confiavel and estavel and grade:
        out.append(aprovada("A4.2", f"{nome}: offset do servidor", c42,
                            f"offset {ctx.rs.offset_s:.0f} s ({ctx.rs.offset_s / 3600:+.2f} h), idade implicita {ctx.rs.idade_implicita_s:.1f} s",
                            offset_s=ctx.rs.offset_s))
    else:
        out.append(reprovada("A4.2", f"{nome}: offset do servidor", c42,
                             f"offset inicial {ctx.rs.offset_s:.0f} s (confiavel={ctx.rs.confiavel}); no fim {rs2.offset_s:.0f} s "
                             f"(confiavel={rs2.confiavel}); multiplo de 15 min={grade}", offset_s=ctx.rs.offset_s))

    # A4.3 livro
    c43 = "(MT5) market_book_add ok, snapshots recebidos e profundidade observada >= 1"
    if ctx.caps.livro and not ctx.perfil.aceita_book:
        out.append(nao_verificavel("A4.3", f"{nome}: livro de ofertas", c43,
                                   f"perfil {ctx.perfil.simbolo_base} sem livro (aceita_book = false; {ctx.perfil.classe}): "
                                   "features de livro desligadas", aplicavel=False))
    elif not ctx.caps.livro:
        out.append(nao_verificavel("A4.3", f"{nome}: livro de ofertas", c43,
                                   f"{ctx.plataforma.upper()} nao tem livro de ofertas (capacidade declarada): features de livro desligadas",
                                   aplicavel=False))
    elif not assinou or lv.total == 0 or lv.profundidade_observada < 1:
        out.append(reprovada("A4.3", f"{nome}: livro de ofertas", c43,
                             f"assinou={assinou}; snapshots={lv.total}; profundidade declarada={lv.profundidade_declarada}, "
                             f"observada={lv.profundidade_observada} (confira a permissao de livro/DOM do ativo)",
                             declarada=lv.profundidade_declarada, observada=lv.profundidade_observada))
    else:
        aviso = ("; observada menor que a declarada (normal com pouca liquidez)"
                 if lv.profundidade_observada < lv.profundidade_declarada else "")
        out.append(aprovada("A4.3", f"{nome}: livro de ofertas", c43,
                            f"{lv.total} snapshots; profundidade declarada {lv.profundidade_declarada}, observada "
                            f"{lv.profundidade_observada}{aviso}", declarada=lv.profundidade_declarada,
                            observada=lv.profundidade_observada, snapshots=lv.total))

    # A4.4 lacunas e qualidade
    c44 = "dados.qualidade sem achado CRITICO (lacuna, feed parado, tunel) e sem ticks fora de ordem"
    rq = qualidade.verificar(ticks, livros, agora_ns=ctx.relogio.agora_ns(), sintetico=ctx.duble)
    resumo = f"ticks={rq.n_ticks} livros={rq.n_livros} criticos={len(rq.criticos)} avisos={len(rq.avisos)}"
    if rq.bloqueia_entradas:
        out.append(reprovada("A4.4", f"{nome}: lacunas e qualidade", c44,
                             resumo + "; " + "; ".join(f"{a.tipo}: {a.detalhe}" for a in rq.criticos),
                             criticos=len(rq.criticos), avisos=len(rq.avisos)))
    else:
        extra = ("; avisos: " + "; ".join(f"{a.tipo} (x{a.contagem})" for a in rq.avisos)) if rq.avisos else ""
        out.append(aprovada("A4.4", f"{nome}: lacunas e qualidade", c44,
                            resumo + f"; backfills={len(tk.backfills)}; lacunas sem backfill={tk.lacunas_sem_backfill}" + extra,
                            criticos=0, avisos=len(rq.avisos)))
    return out


# ----------------------------------------------------------------------------------------------
# A5 ordens na DEMO
# ----------------------------------------------------------------------------------------------
def _enviar_limitada_longe(ctx: Contexto, ex: ExecucaoDemo) -> tuple[Any, float]:
    tk, _ = _cotacao(ctx)
    preco = ctx.perfil.arredondar(float(tk.bid) - _distancia_longe(ctx, float(tk.bid)))
    cmd = ComandoOrdem(intencao_id=ctx.novo_id("lim"), magic=MAGIC_ACEITACAO, lado=Lado.COMPRA, tipo="limitada",
                       quantidade=1, preco=preco)
    return ex.enviar(cmd, dono=DONO), preco


def _ticket_da_ordem(ctx: Contexto, ack: Any, preco: float) -> int | None:
    if int(ack.order) > 0:
        return int(ack.order)
    for o in ctx.terminal.orders_get(symbol=ctx.simbolo) or ():
        if int(o.magic) == MAGIC_ACEITACAO and abs(float(o.price_open) - preco) < ctx.perfil.tick_pontos:
            return int(o.ticket)
    return None


def a5_ordens(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Ordens na DEMO"
    c1 = "limitada de 1 contrato LONGE do mercado e aceita (10008), visivel em orders_get, e cancelada (ordem some)"
    c2 = "mercado de 1 contrato com SL e executado, posicao (1 contrato, SL) visivel e fechada por TICKET (posicao some)"
    c3 = "saida com TICKET VELHO e REJEITADA e nao abre nem inverte posicao"
    c4 = "ao final: posicao zero e nenhuma ordem pendente (nada com o magic da aceitacao); sem necessidade de limpeza"
    bloqueio = _pre_ordens(ctx, "A5", nome, c1 + " | " + c2)
    if bloqueio is not None:
        return [bloqueio]
    ex = _executor(ctx)
    out: list[ResultadoEtapa] = []
    lote = ctx.perfil.volume_unidade
    tick = ctx.perfil.tick_pontos
    ticket_velho: int | None = None
    try:
        # --- A5.1 limitada longe + cancelamento
        tk, idade = _cotacao(ctx)
        if idade > FRESCOR_MAX_S:
            return [nao_verificavel("A5", nome, c1, f"cotacao com {idade:.0f} s de idade (> {FRESCOR_MAX_S} s): mercado parado")]
        ack, preco = _enviar_limitada_longe(ctx, ex)
        r1: ResultadoEtapa
        if ack.retcode != C.RET_COLOCADA:
            r1 = reprovada("A5.1", f"{nome}: limitada longe + cancelamento", c1,
                           f"limitada nao foi aceita: retcode {ack.retcode} - {ack.comentario}")
        else:
            ctx.latencias.setdefault("colocar_limitada", {"amostras": []})["amostras"].append(_lat(ack))
            ticket = _ticket_da_ordem(ctx, ack, preco)
            visivel = ticket is not None and any(int(o.ticket) == ticket for o in ctx.terminal.orders_get(symbol=ctx.simbolo) or ())
            o_ok = False
            if visivel:
                o = next(x for x in ctx.terminal.orders_get(symbol=ctx.simbolo) if int(x.ticket) == ticket)
                o_ok = (int(o.type) == C.ORDEM_BUY_LIMIT and abs(float(o.price_open) - preco) < tick / 2
                        and abs(float(o.volume_current) - lote) < 1e-9)
            if ticket is None or not visivel or not o_ok:
                r1 = reprovada("A5.1", f"{nome}: limitada longe + cancelamento", c1,
                               f"ordem aceita (retcode 10008) mas orders_get nao a mostra como enviada "
                               f"(ticket={ticket}, visivel={visivel}, conferida={o_ok})")
            else:
                ack_c = ex.cancelar_ordem(ticket, dono=DONO)
                sumiu = _esperar(ctx, lambda: not any(int(x.ticket) == ticket for x in ctx.terminal.orders_get(symbol=ctx.simbolo) or ()))
                ctx.latencias.setdefault("cancelar", {"amostras": []})["amostras"].append(_lat(ack_c))
                if ack_c.retcode == C.RET_EXECUTADA and sumiu:
                    r1 = aprovada("A5.1", f"{nome}: limitada longe + cancelamento", c1,
                                  f"compra limitada de {lote} a {preco} (mercado {float(tk.bid)}) aceita em {_lat(ack):.0f} ms e "
                                  f"cancelada em {_lat(ack_c):.0f} ms", preco=preco)
                else:
                    r1 = reprovada("A5.1", f"{nome}: limitada longe + cancelamento", c1,
                                   f"cancelamento: retcode {ack_c.retcode} ({ack_c.comentario}); ordem ainda pendente={not sumiu}")
        out.append(r1)
        ctx.dormir(ctx.pausa_s)

        # --- A5.2 mercado com SL + saida por ticket
        tk, idade = _cotacao(ctx)
        sl = ctx.perfil.arredondar(float(tk.ask) - _stop_largo_pontos(ctx))
        entrada = ComandoOrdem(intencao_id=ctx.novo_id("ent"), magic=MAGIC_ACEITACAO, lado=Lado.COMPRA, tipo="mercado",
                               quantidade=1, sl=sl)
        ack_e = ex.enviar(entrada, dono=DONO)
        r2: ResultadoEtapa
        r3: ResultadoEtapa
        if ack_e.retcode != C.RET_EXECUTADA:
            out.append(reprovada("A5.2", f"{nome}: mercado com SL + saida por ticket", c2,
                                 f"entrada a mercado nao executou: retcode {ack_e.retcode} - {ack_e.comentario}"))
            out.append(nao_verificavel("A5.3", f"{nome}: saida com ticket velho", c3, "depende do A5.2 (sem posicao para encerrar)"))
        else:
            ctx.latencias.setdefault("mercado_entrada", {"amostras": []})["amostras"].append(_lat(ack_e))
            ctx.deals_teste += 1
            pos_ok = _esperar(ctx, lambda: len(ctx.terminal.positions_get(symbol=ctx.simbolo) or ()) >= 1)
            ps = tuple(ctx.terminal.positions_get(symbol=ctx.simbolo) or ())
            detalhe_pos = ""
            conforme = False
            if pos_ok and len(ps) == 1:
                p = ps[0]
                ticket_velho = int(p.ticket)
                lado_ok = int(p.type) == C.POS_COMPRA
                vol_ok = abs(float(p.volume) - lote) < 1e-9
                sl_ok = abs(float(p.sl) - sl) < tick / 2
                magic_ok = int(p.magic) == MAGIC_ACEITACAO
                conforme = lado_ok and vol_ok and sl_ok and magic_ok
                detalhe_pos = (f"posicao {ticket_velho}: compra={lado_ok}, volume={float(p.volume)} (esperado {lote})={vol_ok}, "
                               f"SL={float(p.sl)} (esperado {sl})={sl_ok}, magic={magic_ok}")
            elif pos_ok:
                ticket_velho = int(ps[0].ticket)
                detalhe_pos = f"{len(ps)} posicoes abertas (esperada 1)"
            else:
                detalhe_pos = "posicao nao apareceu em positions_get"
            if not conforme:
                out.append(reprovada("A5.2", f"{nome}: mercado com SL + saida por ticket", c2, "entrada executou mas " + detalhe_pos))
                out.append(nao_verificavel("A5.3", f"{nome}: saida com ticket velho", c3,
                                           "A5.2 reprovada: a posicao sera encerrada na limpeza, sem testar o ticket velho"))
            else:
                assert ticket_velho is not None
                saida = ComandoOrdem(intencao_id=ctx.novo_id("sai"), magic=MAGIC_ACEITACAO, lado=Lado.VENDA, tipo="saida",
                                     quantidade=1, position_ticket=ticket_velho)
                ack_s = ex.enviar(saida, dono=DONO)
                zerou = _esperar(ctx, lambda: not (ctx.terminal.positions_get(symbol=ctx.simbolo) or ()))
                if ack_s.retcode == C.RET_EXECUTADA and zerou:
                    ctx.latencias.setdefault("mercado_saida", {"amostras": []})["amostras"].append(_lat(ack_s))
                    ctx.deals_teste += 1
                    r2 = aprovada("A5.2", f"{nome}: mercado com SL + saida por ticket", c2,
                                  f"{detalhe_pos}; entrada em {_lat(ack_e):.0f} ms; saida por ticket {ticket_velho} em "
                                  f"{_lat(ack_s):.0f} ms, posicao zerada ({ctx.caps.conta})", ticket=ticket_velho)
                else:
                    r2 = reprovada("A5.2", f"{nome}: mercado com SL + saida por ticket", c2,
                                   f"saida por ticket: retcode {ack_s.retcode} ({ack_s.comentario}); posicao zerada={zerou}")
                out.append(r2)
                ctx.dormir(ctx.pausa_s)

                # --- A5.3 ticket velho: rejeitado, sem inverter
                if r2.estado == REPROVADO:
                    out.append(nao_verificavel("A5.3", f"{nome}: saida com ticket velho", c3, "A5.2 reprovada"))
                else:
                    velha = ComandoOrdem(intencao_id=ctx.novo_id("velho"), magic=MAGIC_ACEITACAO, lado=Lado.VENDA, tipo="saida",
                                         quantidade=1, position_ticket=ticket_velho)
                    envios_antes = ex.envios
                    ack_v = ex.enviar(velha, dono=DONO)
                    ctx.dormir(1)
                    ps2, os2 = _estado_simbolo(ctx)
                    rejeitada = ack_v.retcode not in (C.RET_EXECUTADA, C.RET_PARCIAL, C.RET_COLOCADA)
                    sem_posicao = ps2 is not None and len(ps2) == 0 and os2 is not None and len(os2) == 0
                    local = ex.envios == envios_antes
                    if rejeitada and sem_posicao:
                        r3 = aprovada("A5.3", f"{nome}: saida com ticket velho", c3,
                                      f"ticket {ticket_velho} (ja encerrado) rejeitado: retcode {ack_v.retcode} - {ack_v.comentario}; "
                                      f"{'recusa LOCAL (nenhum order_send)' if local else 'recusa do terminal'}; posicao segue zero",
                                      retcode=ack_v.retcode)
                    else:
                        r3 = reprovada("A5.3", f"{nome}: saida com ticket velho", c3,
                                       f"rejeitada={rejeitada} (retcode {ack_v.retcode}); posicao/ordens zeradas={sem_posicao} "
                                       f"(posicoes={None if ps2 is None else len(ps2)}, ordens={None if os2 is None else len(os2)})")
                    out.append(r3)
    except Exception as e:  # noqa: BLE001 - qualquer falha vira REPROVADO e a limpeza abaixo zera o que restar
        out.append(reprovada("A5.x", f"{nome}: execucao do roteiro", "roteiro termina sem excecao",
                             f"excecao: {type(e).__name__}: {e}"))
    finally:
        limpou = _limpar(ctx)
    # --- A5.4 estado final
    ps_fim, os_ = _estado_simbolo(ctx)
    nossos_p = [p for p in (ps_fim or ()) if int(p.magic) == MAGIC_ACEITACAO]
    nossas_o = [o for o in (os_ or ()) if int(o.magic) == MAGIC_ACEITACAO]
    if ps_fim is None or os_ is None:
        out.append(reprovada("A5.4", f"{nome}: estado final", c4, "terminal sem resposta ao conferir o estado final"))
    elif limpou or nossos_p or nossas_o or ps_fim or os_:
        out.append(reprovada("A5.4", f"{nome}: estado final", c4,
                             f"residuos: limpeza fez {limpou or 'nada'}; restam posicoes={len(ps_fim)}, ordens={len(os_)}"))
    else:
        out.append(aprovada("A5.4", f"{nome}: estado final", c4, "posicao zero e nenhuma ordem pendente", ordens_enviadas=ex.envios))
    return out


# ----------------------------------------------------------------------------------------------
# A6 latencia real order_send -> confirmacao
# ----------------------------------------------------------------------------------------------
def a6_latencia(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Latencia de ordens"
    n = max(1, int(ctx.repeticoes))
    crit = (f"{n} repeticoes (limitada longe + cancelamento): todas confirmadas, p95 <= {ctx.limite_latencia_ms:g} ms, "
            "orcamento de mensagens por minuto nunca excedido e nenhuma recusa por orcamento")
    margem = math.ceil(n * (ctx.pausa_s + 4) / 60) + 2
    bloqueio = _pre_ordens(ctx, "A6", nome, crit, margem_min=margem)
    if bloqueio is not None:
        return [bloqueio]
    ex = _executor(ctx)
    colocar: list[float] = []
    cancelar: list[float] = []
    falhas: list[str] = []
    max_msgs = 0
    try:
        for i in range(n):
            # respeita o orcamento: cada repeticao usa 2 mensagens
            if not _esperar(ctx, lambda: ex.orcamento.usadas() <= ex.orcamento.teto - 3, timeout_s=75, passo_s=1):
                falhas.append(f"rep {i + 1}: orcamento de mensagens nao liberou em 75 s")
                break
            ack, preco = _enviar_limitada_longe(ctx, ex)
            max_msgs = max(max_msgs, ex.orcamento.usadas())
            if ack.retcode != C.RET_COLOCADA:
                falhas.append(f"rep {i + 1}: retcode {ack.retcode} ({ack.comentario})")
            else:
                colocar.append(_lat(ack))
                tk = _ticket_da_ordem(ctx, ack, preco)
                if tk is None:
                    falhas.append(f"rep {i + 1}: ticket da ordem nao encontrado")
                else:
                    ack_c = ex.cancelar_ordem(tk, dono=DONO)
                    max_msgs = max(max_msgs, ex.orcamento.usadas())
                    if ack_c.retcode == C.RET_EXECUTADA:
                        cancelar.append(_lat(ack_c))
                    else:
                        falhas.append(f"rep {i + 1}: cancelamento retcode {ack_c.retcode} ({ack_c.comentario})")
            ctx.dormir(ctx.pausa_s)
    except Exception as e:  # noqa: BLE001
        falhas.append(f"excecao: {type(e).__name__}: {e}")
    finally:
        limpou = _limpar(ctx)
    ctx.latencias["colocar_limitada"] = {"amostras": colocar, **_pct(colocar), "n": len(colocar)}
    ctx.latencias["cancelar"] = {"amostras": cancelar, **_pct(cancelar), "n": len(cancelar)}
    ctx.latencias["envio_ate_confirmacao"] = {**_pct(colocar + cancelar), "n": len(colocar) + len(cancelar)}
    for k in ("mercado_entrada", "mercado_saida"):  # amostras unicas da A5 (sem percentil: n = 1)
        if k in ctx.latencias and "p50" not in ctx.latencias[k]:
            a = ctx.latencias[k]["amostras"]
            ctx.latencias[k] = {"amostras": a, "n": len(a), "unico_ms": a[0] if a else None}
    p = ctx.latencias["envio_ate_confirmacao"]
    det = (f"colocar: p50={ctx.latencias['colocar_limitada']['p50']} p95={ctx.latencias['colocar_limitada']['p95']} "
           f"p99={ctx.latencias['colocar_limitada']['p99']} ms (n={len(colocar)}); cancelar: p50={ctx.latencias['cancelar']['p50']} "
           f"p95={ctx.latencias['cancelar']['p95']} p99={ctx.latencias['cancelar']['p99']} ms (n={len(cancelar)}); "
           f"maximo de mensagens no minuto: {max_msgs}/{ex.orcamento.teto}")
    if n < 20:
        det += f"; AVISO: com n={n} o p99 e so indicativo (use --repeticoes 20 ou mais)"
    if ctx.duble:
        det += "; DUBLE: o relogio nao avanca durante o envio, latencia nao representa o terminal"
    if falhas or limpou:
        return [reprovada("A6", nome, crit, "; ".join(falhas + limpou) + " | " + det)]
    if p["p95"] > ctx.limite_latencia_ms:
        return [reprovada("A6", nome, crit, f"p95 {p['p95']} ms acima do limite {ctx.limite_latencia_ms:g} ms | " + det)]
    if max_msgs > ex.orcamento.teto:
        return [reprovada("A6", nome, crit, "orcamento de mensagens excedido | " + det)]
    return [aprovada("A6", nome, crit, det, **{"p50": p["p50"], "p95": p["p95"], "p99": p["p99"], "n": p["n"]})]


# ----------------------------------------------------------------------------------------------
# A7 reconciliacao
# ----------------------------------------------------------------------------------------------
def a7_reconciliacao(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Reconciliacao"
    crit = ("deals do teste classificados por reason (EXPERT/SL/TP/SO, nenhum manual/desconhecido), volume liquido dos deals == "
            "posicao do terminal (zero) e nenhuma posicao/ordem interna sem correspondente no terminal")
    if ctx.ex is None or ctx.deals_teste == 0:
        return [nao_verificavel("A7", nome, crit, "A5 nao gerou deals de teste (sem --confirmo-conta-demo, pregao fechado ou A5 nao concluida): "
                                                  "nao ha o que reconciliar")]
    assert ctx.rs is not None
    desde_ns = ctx.t_inicio_ns - 60 * 1_000_000_000
    deals = ctx.terminal.history_deals_get(ctx.rs.utc_ns_para_dt_servidor(desde_ns),
                                           ctx.rs.utc_ns_para_dt_servidor(ctx.relogio.agora_ns() + 86_400_000_000_000))
    if deals is None:
        return [reprovada("A7", nome, crit, "history_deals_get sem resposta do terminal")]
    ini_srv_ms = ctx.rs.utc_ns_para_servidor_ms(ctx.t_inicio_ns)
    nossos = [d for d in deals if int(d.magic) == MAGIC_ACEITACAO and str(d.symbol) == ctx.simbolo
              and int(d.type) in (C.DEAL_COMPRA, C.DEAL_VENDA) and int(d.time_msc) >= ini_srv_ms - 1000]
    por_reason: dict[str, int] = {}
    for d in nossos:
        r = C.NOMES_RAZAO.get(int(d.reason), str(int(d.reason)))
        por_reason[r] = por_reason.get(r, 0) + 1
    ruins = [r for r in por_reason if r not in ("EXPERT", "SL", "TP", "SO")]
    liquido = sum((float(d.volume) if int(d.type) == C.DEAL_COMPRA else -float(d.volume)) for d in nossos)
    ps, os_ = _estado_simbolo(ctx)
    interna_vazia = ps is not None and os_ is not None and not ps and not os_
    det = (f"{len(nossos)} deals do teste (esperados >= {ctx.deals_teste}); por reason: {por_reason}; volume liquido {liquido:g}; "
           f"terminal: posicoes={None if ps is None else len(ps)}, ordens={None if os_ is None else len(os_)}")
    if len(nossos) < ctx.deals_teste:
        return [reprovada("A7", nome, crit, "faltam deals no historico | " + det, por_reason=por_reason)]
    if ruins:
        return [reprovada("A7", nome, crit, f"reason inesperado {ruins} | " + det, por_reason=por_reason)]
    if abs(liquido) > 1e-9 or not interna_vazia:
        return [reprovada("A7", nome, crit, "volume dos deals diverge da posicao do terminal | " + det, por_reason=por_reason)]
    return [aprovada("A7", nome, crit, det, por_reason=por_reason, deals=len(nossos))]


# ----------------------------------------------------------------------------------------------
# A8 / A9 via Gateway real (em processo)
# ----------------------------------------------------------------------------------------------
@dataclass
class _Gw:
    gw: Gateway
    cli: Any
    tmp: Path

    def passo(self) -> None:
        self.gw.passo()


def _montar_gateway(ctx: Contexto, *, hb_bloqueio: float, hb_zera: float) -> _Gw:
    assert ctx.login is not None and ctx.servidor is not None and ctx.rs is not None
    tmp = Path(tempfile.mkdtemp(prefix="robowin-aceitacao-"))
    g = ConfigGuardiao.de_risco(ctx.risco, heartbeat_bloqueio_s=hb_bloqueio, heartbeat_zera_s=hb_zera,
                                arquivo_emergencia=tmp / "EMERGENCIA", exigir_dia_util=True)
    cfg = ConfigGateway(login=ctx.login, servidor=ctx.servidor, modo_config="demo_mt5", perfil=ctx.perfil,
                        hoje=_agora_sp(ctx).date(), dir_log=tmp / "log", offset_fixo_s=int(ctx.rs.offset_s),
                        msgs_por_min=ctx.msgs_por_min, plataforma=ctx.plataforma, guardiao=g,
                        espera_emergencia_s=0.0 if ctx.duble else 0.25)
    canal = CanalComandosLocal()
    gw = Gateway(ctx.terminal, cfg, ctx.relogio, BarramentoLocal(), canal, dormir=ctx.dormir)
    gw.iniciar()
    return _Gw(gw, canal.cliente(DONO), tmp)


def _fechar_gateway(ctx: Contexto, g: _Gw) -> None:
    try:
        if not g.gw.guardiao.esta_zerado():
            g.gw.guardiao.zerar_tudo("fim da aceitacao")
    except Exception:  # noqa: BLE001 - melhor esforco; o estado final e conferido por quem chamou
        pass
    ctx.envios_gateway += int(g.gw.executor.envios)
    try:
        g.gw.encerrar()
    finally:
        shutil.rmtree(g.tmp, ignore_errors=True)


def _stop_largo_pontos(ctx: Contexto) -> float:
    """Maior stop que o guardiao aceita: afasta o SL do mercado para o roteiro nao ser interrompido por ele."""
    return min(ctx.perfil.stop_max_pontos, ctx.risco.stop_max_pontos)


def _entrada_via_gateway(ctx: Contexto, g: _Gw, *, heartbeat: bool = True) -> dict[str, Any]:
    tk, _ = _cotacao(ctx)
    sl = ctx.perfil.arredondar(float(tk.ask) - _stop_largo_pontos(ctx))
    cmd = ComandoOrdem(intencao_id=ctx.novo_id("gw"), magic=MAGIC_ACEITACAO, lado=Lado.COMPRA, tipo="mercado", quantidade=1, sl=sl)
    if heartbeat:
        g.cli.enviar_sem_resposta({"tipo": "heartbeat", "origem": DONO})
    r = g.cli.enviar({"tipo": "ordem", "cmd": cmd.model_dump(mode="json"), "dono": DONO}, bombear=g.passo)
    return r or {"estado": "SEM_RESPOSTA", "ack": {"retcode": None, "comentario": "gateway nao respondeu"}}


def _tem_posicao(ctx: Contexto) -> bool:
    return bool(ctx.terminal.positions_get() or ctx.terminal.orders_get())


def _fechada_por_stop(ctx: Contexto, desde_ns: int) -> bool:
    """True se a posicao de teste foi fechada pelo SL/TP/stop-out (mercado andou) e nao pelo mecanismo sob teste."""
    assert ctx.rs is not None
    deals = ctx.terminal.history_deals_get(ctx.rs.utc_ns_para_dt_servidor(desde_ns - 60_000_000_000),
                                           ctx.rs.utc_ns_para_dt_servidor(ctx.relogio.agora_ns() + 86_400_000_000_000))
    ini = ctx.rs.utc_ns_para_servidor_ms(desde_ns) - 1000
    return any(int(d.magic) == MAGIC_ACEITACAO and int(d.time_msc) >= ini and int(d.reason) in C.RAZOES_ESPERADAS
               for d in deals or ())


def _com_tentativas(fn: Callable[[], ResultadoEtapa | None], etapa: str, nome: str, criterio: str, tentativas: int = 3) -> ResultadoEtapa:
    """Repete o cenario se o mercado fechou a posicao por SL/TP antes do evento testado (resultado inconclusivo)."""
    for _ in range(tentativas):
        r = fn()
        if r is not None:
            return r
    return nao_verificavel(etapa, nome, criterio, f"inconclusivo: o SL/TP fechou a posicao de teste antes do evento em {tentativas} "
                                                   "tentativas (mercado muito volatil); repita a aceitacao")


def a8_kill_switch(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Kill switch e queda do nucleo"
    c1 = "comando de emergencia: ordem executada, depois zerado + travado confirmado ponta a ponta e nova entrada recusada"
    c2 = f"arquivo EMERGENCIA: o guardiao zera e trava em ate {FOLGA_ZERA_S} s"
    c3 = (f"queda do nucleo (sem heartbeat): entradas vetadas apos {HB_BLOQUEIO_S} s e posicao zerada em ate "
          f"{HB_ZERA_S + FOLGA_ZERA_S} s (heartbeat curto de TESTE; producao 15/60 s)")
    bloqueio = _pre_ordens(ctx, "A8", nome, c1, margem_min=3, global_limpo=True)
    if bloqueio is not None:
        return [bloqueio]
    g = _montar_gateway(ctx, hb_bloqueio=HB_BLOQUEIO_S, hb_zera=HB_ZERA_S)
    n1, n2, n3 = f"{nome}: comando de emergencia", f"{nome}: arquivo EMERGENCIA", f"{nome}: queda do nucleo"

    def rearmar(motivo: str) -> None:
        g.cli.enviar({"tipo": "rearmar", "motivo": motivo}, bombear=g.passo)

    def k1() -> ResultadoEtapa | None:
        t0 = ctx.relogio.agora_ns()
        r = _entrada_via_gateway(ctx, g)
        if r.get("estado") != "EXECUTADA":
            return reprovada("A8.1", n1, c1, f"a entrada de teste nao executou: {r.get('estado')} - {r.get('ack', {}).get('comentario')}")
        em = g.cli.enviar({"tipo": "emergencia", "origem": "aceitacao"}, bombear=g.passo)
        zero = not _tem_posicao(ctx)
        if em and em.get("confirmado") and zero and _fechada_por_stop(ctx, t0):
            return None
        r2 = _entrada_via_gateway(ctx, g)
        vetada = r2.get("ack", {}).get("retcode") == C.RET_LOCAL_RECUSADO
        if em and em.get("confirmado") and zero and vetada:
            return aprovada("A8.1", n1, c1, f"confirmado (zerado={em.get('zerado')}, travado={em.get('travado')}); nova entrada "
                                            f"recusada: {r2.get('ack', {}).get('comentario', '')[:90]}")
        return reprovada("A8.1", n1, c1, f"resposta={em}; terminal zerado={zero}; nova entrada recusada={vetada}")

    def k2() -> ResultadoEtapa | None:
        t0 = ctx.relogio.agora_ns()
        r = _entrada_via_gateway(ctx, g)
        if r.get("estado") != "EXECUTADA":
            return reprovada("A8.2", n2, c2, f"a entrada de teste nao executou: {r.get('estado')}")
        arq = g.tmp / "EMERGENCIA"
        arq.write_text("EMERGENCIA acionada pela aceitacao\n", encoding="utf-8")
        t = 0.0
        while t <= FOLGA_ZERA_S and _tem_posicao(ctx):
            ctx.dormir(0.5)
            t += 0.5
            g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
            g.passo()
        zerou, travado = not _tem_posicao(ctx), g.gw.guardiao.travado_emergencia
        arq.unlink(missing_ok=True)
        if zerou and _fechada_por_stop(ctx, t0):
            return None
        if zerou and travado:
            return aprovada("A8.2", n2, c2, f"zerado e travado em ~{t:.1f} s", segundos=t)
        return reprovada("A8.2", n2, c2, f"zerado={zerou}, travado={travado} apos {t:.1f} s")

    def k3() -> ResultadoEtapa | None:
        t0 = ctx.relogio.agora_ns()
        r = _entrada_via_gateway(ctx, g)  # manda o ultimo heartbeat e abre 1 contrato
        if r.get("estado") != "EXECUTADA":
            return reprovada("A8.3", n3, c3, f"a entrada de teste nao executou: {r.get('estado')}")
        t = 0.0
        veto: bool | None = None
        while t <= HB_ZERA_S + FOLGA_ZERA_S and _tem_posicao(ctx):
            ctx.dormir(0.5)
            t += 0.5
            g.passo()
            if veto is None and t >= HB_BLOQUEIO_S + 1:
                rv = _entrada_via_gateway(ctx, g, heartbeat=False)  # vetada pelo guardiao: nenhum order_send
                veto = rv.get("ack", {}).get("retcode") == C.RET_LOCAL_RECUSADO and "heartbeat" in str(rv.get("ack", {}).get("comentario"))
        zerou = not _tem_posicao(ctx)
        if zerou and _fechada_por_stop(ctx, t0):
            return None
        if zerou and veto:
            return aprovada("A8.3", n3, c3, f"entrada vetada por heartbeat apos {HB_BLOQUEIO_S} s; posicao zerada pelo guardiao "
                                            f"em ~{t:.1f} s sem o nucleo", segundos_para_zerar=t)
        return reprovada("A8.3", n3, c3, f"entrada vetada por heartbeat={veto}; zerado={zerou} apos {t:.1f} s "
                                         f"(limite {HB_ZERA_S + FOLGA_ZERA_S} s)")

    out: list[ResultadoEtapa] = []
    try:
        r1 = _com_tentativas(k1, "A8.1", n1, c1)
        out.append(r1)
        rearmar("aceitacao: fim do teste do comando")
        r2 = _com_tentativas(k2, "A8.2", n2, c2)
        out.append(r2)
        rearmar("aceitacao: fim do teste do arquivo")
        out.append(_com_tentativas(k3, "A8.3", n3, c3))
    finally:
        _fechar_gateway(ctx, g)
    if _tem_posicao(ctx):
        out.append(reprovada("A8.4", f"{nome}: estado final", "conta demo sem posicao e sem ordem ao final",
                             "ainda ha posicao/ordem aberta na conta"))
    return out


def a9_zeragem(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "Zeragem por horario"
    crit = ("com a zeragem configurada para o proximo minuto: posicao aberta ate o horario e zerada em ate "
            f"{FOLGA_ZERA_S} s depois dele (relogio do guardiao = relogio da aceitacao)")
    bloqueio = _pre_ordens(ctx, "A9", nome, crit, margem_min=4, global_limpo=True)
    if bloqueio is not None:
        return [bloqueio]
    g = _montar_gateway(ctx, hb_bloqueio=15, hb_zera=60)

    def tentativa() -> ResultadoEtapa | None:
        a = _agora_sp(ctx)
        if a.second >= 54:  # a entrada precisa caber antes do horario de teste
            ctx.dormir(61 - a.second)
            a = _agora_sp(ctx)
        ok, motivo = _pregao(ctx, margem_min=3)
        if not ok:
            return nao_verificavel("A9", nome, crit, f"pregao nao esta aberto: {motivo}")
        alvo = a.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        hhmm = alvo.strftime("%H:%M")
        g.gw.guardiao.cfg.zeragem = hhmm
        g.gw.guardiao.cfg.entradas_ate = hhmm
        t0 = ctx.relogio.agora_ns()
        r = _entrada_via_gateway(ctx, g)
        if r.get("estado") != "EXECUTADA":
            return reprovada("A9", nome, crit, f"a entrada de teste nao executou: {r.get('estado')} - {r.get('ack', {}).get('comentario')}")
        limite = (alvo - _agora_sp(ctx)).total_seconds() + FOLGA_ZERA_S + 2
        t = 0.0
        while t <= limite:
            ctx.dormir(0.5)
            t += 0.5
            g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
            g.passo()
            if not _tem_posicao(ctx):
                agora = _agora_sp(ctx)
                if _fechada_por_stop(ctx, t0):
                    return None
                if agora < alvo:
                    return reprovada("A9", nome, crit, f"a posicao foi zerada ANTES do horario {hhmm} (nao foi por SL/TP): zeragem fora de hora")
                atraso = (agora - alvo).total_seconds()
                if atraso <= FOLGA_ZERA_S:
                    return aprovada("A9", nome, crit, f"zeragem de teste {hhmm}: posicao zerada {atraso:.1f} s apos o horario", atraso_s=atraso)
                return reprovada("A9", nome, crit, f"posicao zerada {atraso:.1f} s apos {hhmm} (limite {FOLGA_ZERA_S} s)")
        return reprovada("A9", nome, crit, f"a posicao NAO foi zerada ate {FOLGA_ZERA_S} s apos o horario {hhmm}")

    try:
        return [_com_tentativas(tentativa, "A9", nome, crit)]
    finally:
        _fechar_gateway(ctx, g)


# ----------------------------------------------------------------------------------------------
# A10 IA ao vivo
# ----------------------------------------------------------------------------------------------
def a10_ia(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "IA ao vivo"
    crit = "TTFT/total p50/p95/p99 reais; chamadas validas (<= 20% de falhas) e p95 total dentro do prazo de config/robowin.toml [prazos]"
    if ctx.pular_ia:
        return [nao_verificavel("A10", nome, crit, "pulada por --pular-ia (escolha do usuario: IA ao vivo fora do escopo)", aplicavel=False)]
    modelos: list[tuple[str, str]] = []
    for p in papeis(ctx.cfg).values():
        prov, _ = dividir_modelo(p.modelo)
        if tem_chave(prov) and p.modelo not in [m for m, _ in modelos]:
            modelos.append((p.modelo, prov))
    if not modelos:
        return [nao_verificavel("A10", nome, crit, "nenhuma chave de API no ambiente (ANTHROPIC_API_KEY / OPENAI_API_KEY) para os modelos "
                                                    "dos papeis de config/robowin.toml: latencia real de IA nao medida")]
    try:
        cat = _catalogo(None)
    except ErroCatalogo as e:
        return [reprovada("A10", nome, crit, f"catalogo de modelos indisponivel: {e}")]
    prazos = ctx.cfg.get("prazos", {}).get("imediato", {})
    import asyncio
    out: list[ResultadoEtapa] = []
    for ref, prov_nome in modelos:
        prazo_s = float(prazos.get("claude_s" if prov_nome == "anthropic" else "gpt_s", 60)) if prov_nome in ("anthropic", "openai") else 60.0
        n = max(1, int(ctx.repeticoes))
        try:
            cat.obter(ref)
            prov, real = provedor_para_cli(prov_nome, cat)
            _, modelo = dividir_modelo(ref)
            r = asyncio.run(medir_latencia(prov, modelo, n, max(prazo_s, 10.0)))
        except Exception as e:  # noqa: BLE001
            out.append(reprovada(f"A10.{ref}", f"{nome}: {ref}", crit, f"falha ao medir: {type(e).__name__}: {e}"))
            continue
        ctx.latencias[f"ia:{ref}"] = {"n": r["n"], "falhas": r["falhas"], "ttft_ms": r["ttft_ms"], "total_ms": r["total_ms"],
                                       "provedor_real": real}
        det = (f"n={r['n']} falhas={r['falhas']}; TTFT p50/p95/p99={r['ttft_ms']['p50']}/{r['ttft_ms']['p95']}/{r['ttft_ms']['p99']} ms; "
               f"total p50/p95/p99={r['total_ms']['p50']}/{r['total_ms']['p95']}/{r['total_ms']['p99']} ms; prazo {prazo_s:g} s")
        if not real:
            out.append(nao_verificavel(f"A10.{ref}", f"{nome}: {ref}", crit, "provedor FALSO (chave rejeitada): latencia nao representa o modelo | " + det))
        elif r["falhas"] * 5 > r["n"] or r["total_ms"]["p95"] > prazo_s * 1000:
            out.append(reprovada(f"A10.{ref}", f"{nome}: {ref}", crit, det))
        else:
            out.append(aprovada(f"A10.{ref}", f"{nome}: {ref}", crit, det))
    return out


# ----------------------------------------------------------------------------------------------
# A11 MT4
# ----------------------------------------------------------------------------------------------
def a11_mt4(ctx: Contexto) -> list[ResultadoEtapa]:
    nome = "MT4: ponte e hedging"
    c1 = "handshake do EA: protocolo compativel, EA identificado e (fora do duble) nao e o duble"
    c2 = "hedging por TICKET: entrada com SL e saida por ticket fecham exatamente aquela posicao; ticket velho nao abre posicao oposta"
    if ctx.plataforma != "mt4":
        return [nao_verificavel("A11", nome, c1 + " | " + c2, "so se aplica com --plataforma mt4", aplicavel=False)]
    ola = dict(getattr(ctx.terminal, "ola", {}) or {})
    out: list[ResultadoEtapa] = []
    if not ola:
        out.append(reprovada("A11.1", f"{nome}: handshake do EA", c1, "sem handshake (initialize nao concluiu)"))
    elif not ctx.duble and bool(ola.get("duble", False)):
        out.append(reprovada("A11.1", f"{nome}: handshake do EA", c1, "o servidor da ponte diz ser DUBLE: nao e o EA real"))
    else:
        out.append(aprovada("A11.1", f"{nome}: handshake do EA", c1,
                            f"protocolo v{ola.get('versao_protocolo')}; EA {ola.get('ea', '?')} {ola.get('ea_versao', '?')}; "
                            f"build {ola.get('build', '?')}; AVISO: a compilacao do .mq4 e conferida por scripts/windows/compilar_ea.ps1",
                            ea=str(ola.get("ea", "")), ea_versao=str(ola.get("ea_versao", ""))))
    a52, a53 = ctx.resultados.get("A5.2"), ctx.resultados.get("A5.3")
    if a52 is None or a53 is None:
        out.append(nao_verificavel("A11.2", f"{nome}: hedging por ticket", c2,
                                   "depende de A5.2/A5.3 (ordens na demo), que nao rodaram: " +
                                   (ctx.resultados["A5"].detalhe if "A5" in ctx.resultados else "A5 ausente")))
    elif a52.estado == "APROVADO" and a53.estado == "APROVADO":
        out.append(aprovada("A11.2", f"{nome}: hedging por ticket", c2,
                            "confirmado por A5.2 (saida por ticket em conta hedging) e A5.3 (ticket velho rejeitado, posicao segue zero)"))
    else:
        out.append(reprovada("A11.2", f"{nome}: hedging por ticket", c2, f"A5.2={a52.estado}; A5.3={a53.estado}"))
    return out


ETAPAS: list[tuple[str, str, Callable[[Contexto], list[ResultadoEtapa]]]] = [
    ("A1", "Ambiente", a1_ambiente),
    ("A2", "Conta", a2_conta),
    ("A3", "Contrato e especificacoes", a3_contrato),
    ("A4", "Coleta de ticks e livro", a4_coleta),
    ("A5", "Ordens na DEMO", a5_ordens),
    ("A6", "Latencia de ordens", a6_latencia),
    ("A7", "Reconciliacao", a7_reconciliacao),
    ("A8", "Kill switch e queda do nucleo", a8_kill_switch),
    ("A9", "Zeragem por horario", a9_zeragem),
    ("A10", "IA ao vivo", a10_ia),
    ("A11", "MT4: ponte e hedging", a11_mt4),
]
ETAPAS_DE_ORDEM = {"A5", "A6", "A8", "A9"}


def executar(ctx: Contexto) -> list[ResultadoEtapa]:
    """Roda A1..A11 na ordem. Excecao inesperada => REPROVADO da etapa (e limpeza). Aborta apos A1/A2 se a conta nao for a DEMO esperada."""
    res: list[ResultadoEtapa] = []
    ctx.t_inicio_ns = ctx.relogio.agora_ns()
    for codigo, nome, fn in ETAPAS:
        if ctx.abortar:
            res.append(nao_verificavel(codigo, nome, "(nao executada)", f"execucao abortada: {ctx.abortar}"))
            continue
        t0 = time.monotonic()
        try:
            linhas = fn(ctx)
        except Exception as e:  # noqa: BLE001
            linhas = [reprovada(codigo, nome, "etapa conclui sem excecao", f"excecao inesperada: {type(e).__name__}: {e}")]
            if codigo in ETAPAS_DE_ORDEM and ctx.confirmo:
                _limpar(ctx)
        dur = (time.monotonic() - t0) / max(len(linhas), 1)
        for r in linhas:
            r.duracao_s = dur
            ctx.resultados[r.etapa or codigo] = r
        ctx.resultados[codigo] = linhas[0] if len(linhas) == 1 else ctx.resultados.get(codigo, linhas[0])
        res.extend(linhas)
    return res


__all__ = ["Contexto", "executar", "ETAPAS", "MAGIC_ACEITACAO"]
