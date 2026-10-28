"""`robowin perfil importar-do-terminal <SIMBOLO>`: gera o perfil de QUALQUER mercado a partir do terminal.

Le `symbol_info` (MT5) ou a especificacao `MarketInfo` do EA (MT4, via `ConectorMT4`, ja traduzida para os nomes do
MT5) e grava em config/instrumentos.toml um perfil SEMPRE `a_confirmar = true`, com `fonte` = o terminal (corretora,
servidor, plataforma) e a data. Nada e inventado:

- valores do contrato (tamanho, lote min/passo/max, digitos, ponto, tick, moedas, modo de calculo, swap) vem do
  terminal; o que o terminal nao informa fica de fora e vira item `a_confirmar` (ex.: comissao por lote);
- o valor do tick NA MOEDA DE LUCRO vem de `trade_tick_value` quando a conta esta na moeda de lucro; com a conta em
  outra moeda o terminal informa o valor JA convertido, entao o importador usa tick_size x contract_size (modos
  forex/CFD) e registra isso; em outros modos sem a moeda da conta igual, recusa (nao ha como saber);
- os limites em TICKS (stop maximo, folga de slippage, stop/alvo padrao) sao escolhas de risco do usuario, nao dados
  do terminal: saem dos argumentos ou de um PLACEHOLDER explicito (multiplos do spread atual), rotulado a confirmar.

Sufixo da corretora (EURUSD.m, EURUSDpro): o nome exato vence; senao `[mt5].sufixo_simbolo` (ou `--sufixo`); senao a
unica decoracao aceitavel (`core.moedas.decoracao_ok`). Ambiguo => recusa, listando os candidatos.
"""
from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.instrumentos import CLASSES_CORRETORA, PerfilInstrumento
from ..core.moedas import decoracao_ok
from .edicao_toml import atualizar_secao, gravar_atomico, valor_toml

# SYMBOL_CALC_MODE do MT5 -> modo_calculo do perfil
CALC_MODE: dict[int, str] = {0: "forex", 5: "forex", 1: "futuros", 2: "cfd", 3: "cfd", 4: "cfd", 32: "bolsa",
                             33: "futuros", 34: "futuros", 35: "bolsa", 36: "bolsa", 37: "bolsa", 38: "bolsa"}
# ENUM_DAY_OF_WEEK (0 = domingo)
DIA_MT = ("domingo", "segunda", "terca", "quarta", "quinta", "sexta", "sabado")
METAIS = frozenset({"XAU", "XAG", "XPT", "XPD"})
CRIPTO = frozenset({"BTC", "ETH", "LTC", "XRP", "BCH", "ADA", "DOT", "SOL", "DOGE", "BNB"})
BOLSA_DA_CLASSE = {"forex": "FOREX", "cripto": "CRIPTO", "metal": "CFD", "cfd_indice": "CFD", "cfd_acao": "CFD",
                   "commodity": "CFD"}
# PLACEHOLDER dos limites em ticks quando o usuario nao informa: multiplos do spread atual (piso em ticks)
MULT_STOP_MAX, MULT_FOLGA, MULT_STOP_PADRAO = 30, 2, 10
PISO_STOP_MAX, PISO_FOLGA, PISO_STOP_PADRAO = 30, 2, 10


class ImportacaoRecusada(Exception):
    pass


@dataclass
class PerfilImportado:
    chave: str
    simbolo_terminal: str
    dados: dict[str, Any]
    avisos: list[str] = field(default_factory=list)

    def texto_toml(self) -> str:
        """Secao TOML do perfil (chaves na ordem do modelo; sessoes/tarifas nao sao geradas)."""
        linhas = [f"[{self.chave}]"]
        for k, v in self.dados.items():
            linhas.append(f"{k} = {valor_toml(v)}")
        return "\n".join(linhas) + "\n"


def resolver_nome(nomes: list[str], simbolo: str, sufixo: str = "") -> str:
    """Nome do simbolo no terminal (ver docstring do modulo)."""
    if simbolo in nomes:
        return simbolo
    alvo = simbolo.upper()
    if sufixo and f"{alvo}{sufixo}" in nomes:
        return f"{alvo}{sufixo}"
    cands = []
    for n in nomes:
        i = n.find(alvo)
        if i < 0:
            continue
        pre, suf = n[:i], n[i + len(alvo):]
        if decoracao_ok(pre, prefixo=True) and decoracao_ok(suf, prefixo=False):
            cands.append(n)
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise ImportacaoRecusada(f"simbolo {simbolo} nao oferecido pelo terminal (symbols_get); confira o nome ou o "
                                 "sufixo da corretora ([mt5].sufixo_simbolo / --sufixo)")
    raise ImportacaoRecusada(f"simbolo {simbolo} ambiguo no terminal: {sorted(cands)}; informe o nome exato ou "
                             "--sufixo")


def chave_do_perfil(simbolo: str, nome_terminal: str, sufixo: str = "") -> str:
    base = simbolo.strip()
    if sufixo and base.endswith(sufixo):
        base = base[: -len(sufixo)]
    chave = re.sub(r"[^A-Z0-9_]", "", base.upper())
    if not chave:
        chave = re.sub(r"[^A-Z0-9_]", "", nome_terminal.upper())
    if not chave:
        raise ImportacaoRecusada(f"nao foi possivel derivar a chave do perfil de '{simbolo}'")
    return chave


def inferir_classe(info: Any, modo: str) -> str | None:
    """Classe pelo modo de calculo, moedas e pasta do simbolo (`path`). None = indeciso (use --classe)."""
    base = str(getattr(info, "currency_base", "") or "").upper()
    caminho = str(getattr(info, "path", "") or "").lower()
    if base in METAIS or "metal" in caminho:
        return "metal"
    if base in CRIPTO or "crypto" in caminho or "cripto" in caminho:
        return "cripto"
    if modo == "forex":
        return "forex"
    if any(k in caminho for k in ("indice", "indices", "index", "cfd_indice")):
        return "cfd_indice"
    if any(k in caminho for k in ("stock", "share", "acoes", "cfd_acao")):
        return "cfd_acao" if modo == "cfd" else "acao"
    if any(k in caminho for k in ("commod", "energ", "agri", "oil", "commodity")):
        return "commodity"
    if modo == "futuros":
        return "futuro"
    return None


def _swap(info: Any, moeda_lucro: str, moeda_conta: str, avisos: list[str], itens: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    modo = getattr(info, "swap_mode", None)
    sl, ss = getattr(info, "swap_long", None), getattr(info, "swap_short", None)
    if modo is None or sl is None or ss is None:
        itens.append("swap_long/swap_short/modo (o terminal nao informou)")
        return out
    modo = int(modo)
    base = str(getattr(info, "currency_base", "") or "").upper()
    margem = str(getattr(info, "currency_margin", "") or "").upper()
    if modo == 0:  # SYMBOL_SWAP_MODE_DISABLED
        return {"swap_long": 0.0, "swap_short": 0.0, "swap_modo": "pontos"}
    swap_modo = {1: "pontos", 4: "moeda_conta"}.get(modo, "")
    if modo == 2 and base == moeda_lucro:
        swap_modo = "moeda_lucro"
    if modo == 3 and margem == moeda_lucro:
        swap_modo = "moeda_lucro"
    out = {"swap_long": float(sl), "swap_short": float(ss)}
    if swap_modo:
        out["swap_modo"] = swap_modo
        if swap_modo == "moeda_conta" and moeda_conta != moeda_lucro:
            itens.append(f"swap na moeda da conta ({moeda_conta}): nao convertido pelo simulador")
    else:
        itens.append(f"swap_modo {modo} do terminal (juros/reabertura) nao calculado pelo robo")
    tri = getattr(info, "swap_rollover3days", None)
    if tri is not None and 0 <= int(tri) <= 6:
        out["swap_dia_triplo"] = DIA_MT[int(tri)]
    return out


def gerar_perfil(
    terminal: Any, simbolo: str, *, sufixo: str = "", classe: str | None = None, bolsa: str | None = None,
    plataforma: str = "mt5", hoje: dt.date | None = None, stop_max_ticks: int | None = None,
    slippage_folga_ticks: int | None = None, stop_padrao_ticks: int | None = None,
    alvo_padrao_ticks: int | None = None,
) -> PerfilImportado:
    """Le o terminal e monta o perfil (sem gravar). `ImportacaoRecusada` se faltar dado essencial."""
    hoje = hoje or dt.date.today()
    if not terminal.initialize():
        raise ImportacaoRecusada(f"initialize falhou: {terminal.last_error()}")
    simbolos = terminal.symbols_get() or ()
    nomes = [str(s.name) for s in simbolos]
    if not nomes:
        raise ImportacaoRecusada("symbols_get vazio ou indisponivel")
    nome = resolver_nome(nomes, simbolo, sufixo)
    terminal.symbol_select(nome, True)
    info = terminal.symbol_info(nome)
    if info is None:
        raise ImportacaoRecusada(f"symbol_info({nome}) indisponivel")
    ai = terminal.account_info()
    moeda_conta = str(getattr(ai, "currency", "") or "").upper() if ai is not None else ""
    avisos: list[str] = []
    itens: list[str] = []

    tick = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    vmin = float(getattr(info, "volume_min", 0.0) or 0.0)
    vstep = float(getattr(info, "volume_step", 0.0) or 0.0)
    vmax = float(getattr(info, "volume_max", 0.0) or 0.0)
    cs = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
    moeda_lucro = str(getattr(info, "currency_profit", "") or "").upper()
    if tick <= 0 or vmin <= 0 or not moeda_lucro:
        raise ImportacaoRecusada(f"{nome}: trade_tick_size/volume_min/currency_profit ausentes no terminal")
    calc = getattr(info, "trade_calc_mode", None)
    modo = CALC_MODE.get(int(calc), "") if calc is not None else ""
    if not modo:
        itens.append(f"modo de calculo (trade_calc_mode={calc}) nao reconhecido")
    classe = classe or inferir_classe(info, modo)
    if classe is None:
        raise ImportacaoRecusada(f"{nome}: classe nao deduzivel (modo {modo or '?'}, pasta "
                                 f"'{getattr(info, 'path', '')}'); informe --classe")
    if classe not in CLASSES_CORRETORA and not bolsa:
        raise ImportacaoRecusada(f"{nome}: classe {classe} e de bolsa; informe --bolsa (codigo de config/bolsas.toml)"
                                 " e as tarifas da bolsa no perfil depois")
    bolsa = bolsa or BOLSA_DA_CLASSE[classe]

    # valor do tick por 1,0 lote NA MOEDA DE LUCRO
    tv_terminal = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    if moeda_conta and moeda_conta == moeda_lucro and tv_terminal > 0:
        tick_valor = tv_terminal
    elif modo in ("forex", "cfd") and cs > 0:
        tick_valor = tick * cs
        avisos.append(f"conta em {moeda_conta or '?'} e lucro em {moeda_lucro}: valor do tick = tick_size x "
                      f"contract_size = {tick_valor:.10g} {moeda_lucro} (o trade_tick_value do terminal ja vem convertido)")
    else:
        raise ImportacaoRecusada(f"{nome}: conta em {moeda_conta or '?'} e lucro em {moeda_lucro} no modo "
                                 f"{modo or '?'}: o valor do tick na moeda de lucro nao e deduzivel do terminal")
    tick_valor = float(f"{tick_valor:.12g}")

    tk = terminal.symbol_info_tick(nome)
    bid, ask = (float(tk.bid), float(tk.ask)) if tk is not None else (0.0, 0.0)
    if not (bid > 0 and ask >= bid):
        raise ImportacaoRecusada(f"{nome}: sem cotacao valida agora (bid/ask) para o nivel de preco de referencia")
    spread_ticks = max(1, round((ask - bid) / tick))
    ponto = float(getattr(info, "point", 0.0) or 0.0) or tick
    digitos = int(getattr(info, "digits", 0) or 0)

    limites_informados = all(x is not None for x in (stop_max_ticks, slippage_folga_ticks, stop_padrao_ticks))
    smax = stop_max_ticks or max(PISO_STOP_MAX, MULT_STOP_MAX * spread_ticks)
    folga = slippage_folga_ticks if slippage_folga_ticks is not None else max(PISO_FOLGA, MULT_FOLGA * spread_ticks)
    spad = min(stop_padrao_ticks or max(PISO_STOP_PADRAO, MULT_STOP_PADRAO * spread_ticks), smax)
    apad = alvo_padrao_ticks or spad
    if not limites_informados:
        itens.append(f"limites em ticks (stop_max/slippage/stop e alvo padrao): PLACEHOLDER = multiplos do spread de "
                     f"{spread_ticks} ticks lido na importacao; defina os SEUS")

    corretora = str(getattr(ai, "company", "") or "") if ai is not None else ""
    servidor = str(getattr(ai, "server", "") or "") if ai is not None else ""
    origem = "DUBLE SINTETICO" if getattr(terminal, "eh_duble", False) else f"terminal {plataforma.upper()}"
    fonte = (f"{origem}: symbol_info({nome}) em {corretora or '?'} / {servidor or '?'} "
             f"(conta em {moeda_conta or '?'}), importado por robowin perfil importar-do-terminal")
    chave = chave_do_perfil(simbolo, nome, sufixo)

    d: dict[str, Any] = {
        "classe": classe, "bolsa": bolsa,
        "descricao": f"importado do terminal: {str(getattr(info, 'description', '') or nome)}",
        "modo_calculo": modo, "tamanho_contrato": cs if cs > 0 else None,
        "volume_min": vmin, "volume_step": vstep or vmin, "volume_max": vmax or None,
        "digitos": digitos, "ponto": ponto, "tick_pontos": tick, "tick_valor_lucro": tick_valor,
        "moeda_base": str(getattr(info, "currency_base", "") or "").upper(),
        "moeda_lucro": moeda_lucro,
        "moeda_margem": str(getattr(info, "currency_margin", "") or "").upper(),
    }
    if classe == "forex":  # no forex a moeda de cotacao e a de lucro
        d["moeda_cotacao"] = moeda_lucro
        if digitos in (3, 5):
            d["pip"] = float(f"{ponto * 10:.10g}")
    d.update(_swap(info, moeda_lucro, moeda_conta, avisos, itens))
    d["spread_tipico_pontos"] = float(f"{(ask - bid):.10g}")
    itens.append("spread_tipico_pontos = spread no INSTANTE da importacao (nao e o tipico)")
    itens.append("comissao por lote (o terminal nao informa: comissao_lote_lado a confirmar com a corretora)")
    if nome != chave:
        d["simbolo_corretora"] = nome
    if int(getattr(info, "expiration_time", 0) or 0) > 0:
        itens.append("simbolo com vencimento: regra de vencimento/rolagem nao importada")
    d.update({
        "stop_max_ticks": int(smax), "slippage_folga_ticks": int(folga), "stop_padrao_ticks": int(spad),
        "alvo_padrao_ticks": int(apad), "aceita_book": int(getattr(info, "ticks_bookdepth", 0) or 0) > 0,
        "risco_aprovado_padrao": False, "preco_sintetico_ref": float(f"{(bid + ask) / 2:.10g}"),
        "a_confirmar": True, "a_confirmar_itens": ["valores importados do terminal: confira cada um", *itens],
        "fonte": fonte, "fonte_data": hoje.isoformat(),
    })
    d = {k: v for k, v in d.items() if v is not None and v != ""}
    # validacao: o perfil tem de carregar com o modelo
    try:
        PerfilInstrumento.model_validate({"simbolo_base": chave, **d})
    except Exception as e:  # noqa: BLE001
        raise ImportacaoRecusada(f"{nome}: perfil gerado invalido: {e}") from e
    return PerfilImportado(chave, nome, d, avisos)


def gravar_perfil(imp: PerfilImportado, arquivo: str | Path, *, substituir: bool = False) -> str:
    """Grava o perfil em `arquivo` (nova secao no fim, ou substituindo as chaves de uma secao existente com
    `substituir=True`). Devolve 'criado' | 'substituido'. Recusa sobrescrever sem `substituir`."""
    p = Path(arquivo)
    texto = p.read_text(encoding="utf-8") if p.is_file() else ""
    atual = tomllib.loads(texto) if texto else {}
    if imp.chave in atual:
        if not substituir:
            raise ImportacaoRecusada(f"perfil {imp.chave} ja existe em {p}: use --substituir (os valores do terminal "
                                     "trocam os do perfil e ele volta a a_confirmar)")
        novo = atualizar_secao(texto, imp.chave, imp.dados)
        gravar_atomico(p, novo)
        return "substituido"
    sep = "" if not texto or texto.endswith("\n\n") else ("\n" if texto.endswith("\n") else "\n\n")
    novo = texto + sep + f"# importado do terminal em {imp.dados.get('fonte_data', '')} (a_confirmar)\n" + imp.texto_toml()
    tomllib.loads(novo)  # conferencia
    gravar_atomico(p, novo)
    return "criado"
