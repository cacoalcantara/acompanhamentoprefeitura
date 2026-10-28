"""Confirmacao GUIADA de perfis de ativo/bolsa (`robowin perfil confirmar`): decisao que SO o usuario pode tomar.

Mostra os valores do perfil e a fonte registrada, opcionalmente compara com as especificacoes reportadas pelo
terminal (qualquer divergencia => recusa), exige que o usuario DIGITE o simbolo do ativo (ou o codigo da bolsa) e so
entao troca `a_confirmar = false` (esvaziando `a_confirmar_itens`) e grava `fonte`, `fonte_data`, `confirmado_por` e
`confirmado_em`, preservando o resto do TOML (`edicao_toml`). O evento vai para a cadeia de auditoria.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.bolsas import PerfilBolsa, bolsas_disponiveis, carregar_bolsa
from ..core.config import ErroConfig, carregar_toml, responsavel_pendente, sha256_arquivo
from ..core.instrumentos import PerfilInstrumento, ativos_disponiveis, carregar_perfil
from .edicao_toml import ErroEdicaoToml, atualizar_secao, gravar_atomico

PREFIXO_BOLSA = "bolsa:"
RE_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConfirmacaoRecusada(ErroConfig):
    """O perfil NAO foi confirmado (nada foi alterado)."""


@dataclass(frozen=True)
class Alvo:
    tipo: str  # "ativo" | "bolsa"
    codigo: str

    @property
    def rotulo(self) -> str:
        return f"bolsa {self.codigo}" if self.tipo == "bolsa" else f"ativo {self.codigo}"


def interpretar_alvo(texto: str) -> Alvo:
    t = (texto or "").strip()
    if t.lower().startswith(PREFIXO_BOLSA):
        cod = t[len(PREFIXO_BOLSA):].strip().upper()
        if not cod:
            raise ConfirmacaoRecusada("informe a bolsa como bolsa:<CODIGO>")
        return Alvo("bolsa", cod)
    if not t:
        raise ConfirmacaoRecusada("informe o ativo (simbolo_base) ou bolsa:<CODIGO>")
    return Alvo("ativo", t.upper())


def _fmt(v: float) -> str:
    return f"{v:,.6g}".replace(",", "_").replace(".", ",").replace("_", ".")


def descrever_ativo(p: PerfilInstrumento, hoje: dt.date) -> list[str]:
    from ..core import calendario_b3
    L = [f"Ativo {p.simbolo_base} - {p.descricao} (classe {p.classe}, bolsa {p.bolsa}, moeda {p.moeda})",
         f"  tick (variacao minima) ........ {_fmt(p.tick_pontos)} pontos",
         f"  valor do ponto ................ {p.moeda} {_fmt(p.ponto_valor_brl)} por ponto por lote "
         f"(tick vale {p.moeda} {_fmt(p.tick_valor_brl)})",
         f"  lote .......................... {p.lote}",
         "  margem de day trade ........... "
         + (f"{p.moeda} {_fmt(p.margem_day_trade_brl)}" if p.margem_day_trade_brl is not None else "nao informada"),
         f"  limites em ticks .............. stop max {p.stop_max_ticks}, folga {p.slippage_folga_ticks}, "
         f"stop padrao {p.stop_padrao_ticks}, alvo padrao {p.alvo_padrao_ticks}"]
    L.append("  tarifas:")
    for t in p.tarifas:
        L.append(f"    desde {t.vigente_desde}: {p.moeda} {_fmt(t.tarifa_lado_brl)} por lado por lote; zeragem "
                 f"{p.moeda} {_fmt(t.taxa_zeragem_brl)} (fonte: {t.fonte or 'nao informada'})")
    try:
        s = p.sessao_em(hoje)
        L.append(f"  sessao vigente ({s.vigente_desde}) .. abertura {s.abertura}, fechamento {s.fechamento}, "
                 f"entradas ate {s.entradas_ate}, zeragem {s.zeragem}")
    except ValueError as e:
        L.append(f"  sessao ........................ {e}")
    if p.tem_vencimento:
        try:
            cod = calendario_b3.contrato_vigente(p, hoje)
            venc = calendario_b3.vencimento_do_codigo(p, cod)
            L.append(f"  vencimento .................... regra {p.vencimento}; contrato vigente {cod} vence {venc}")
        except Exception as e:  # noqa: BLE001
            L.append(f"  vencimento .................... regra {p.vencimento} (calculo falhou: {e})")
    else:
        L.append("  vencimento .................... nenhum (mercado a vista)")
    return L


def descrever_bolsa(b: PerfilBolsa) -> list[str]:
    L = [f"Bolsa {b.codigo} - {b.nome}",
         f"  fuso / calendario ............. {b.fuso} / {b.calendario}",
         f"  moeda ......................... {b.moeda}",
         f"  modelo de tarifas ............. {b.modelo_tarifas}",
         f"  regime tributario ............. {b.regime_tributario}",
         "  limite de oscilacao ........... "
         + (f"{_fmt(b.limite_oscilacao.percentual * 100)}%" if b.limite_oscilacao else "nao informado")]
    for s in b.sessoes:
        L.append(f"  sessao desde {s.vigente_desde} .. abertura {s.abertura}, fechamento {s.fechamento}, "
                 f"entradas ate {s.entradas_ate}, zeragem {s.zeragem}")
    return L


def _bruto_secao(arquivo: Path, codigo: str) -> dict[str, Any]:
    sec = carregar_toml(arquivo).get(codigo)
    if not isinstance(sec, dict):
        raise ConfirmacaoRecusada(f"'{codigo}' nao tem secao em {arquivo}")
    return sec


def comparar_com_terminal(terminal: Any, perfil: PerfilInstrumento, hoje: dt.date) -> tuple[str, list[str], dict[str, Any]]:
    """(origem, divergencias, observado) entre o perfil e o simbolo vigente reportado pelo terminal (somente leitura:
    initialize/symbols_get/symbol_info; nenhuma ordem). Divergencia ou terminal indisponivel => lista nao vazia."""
    from ..mt5.contrato import ContratoRecusado, resolver_contrato
    from ..mt5.specs import valor_ponto_brl
    origem = "DUBLE (dados sinteticos; NAO e o terminal real)" if getattr(terminal, "eh_duble", False) else "terminal real"
    if not terminal.initialize():
        return origem, [f"initialize falhou: {terminal.last_error()}"], {}
    # a comparacao e justamente o que falta para confirmar: o bloqueio a_confirmar e suspenso SO em memoria
    p = perfil.model_copy(update={"a_confirmar": False, "a_confirmar_itens": ()})
    try:
        res = resolver_contrato(terminal, hoje, p, selecionar=True)
    except ContratoRecusado as e:
        return origem, list(e.motivos), {}
    info = terminal.symbol_info(res.simbolo_corretora)
    vp = valor_ponto_brl(info)
    observado = {"simbolo": res.simbolo_corretora, "tick_pontos": float(getattr(info, "trade_tick_size", 0.0) or 0.0),
                 "ponto_valor_por_lote": None if vp is None else vp * p.lote,
                 "volume_min": float(getattr(info, "volume_min", 0.0) or 0.0),
                 "moeda": str(getattr(info, "currency_profit", "") or ""),
                 "vencimento": res.vencimento.isoformat() if res.vencimento else None}
    return origem, [], observado


@dataclass
class ResultadoConfirmacao:
    alvo: Alvo
    arquivo: Path
    alterado: bool
    mensagens: list[str] = field(default_factory=list)


def confirmar_perfil(alvo: Alvo, *, instrumentos: str | Path, bolsas: str | Path, responsavel: str,
                     confirmar: Callable[[list[str]], str], fonte: str | None = None, fonte_data: str | None = None,
                     terminal: Any | None = None, auditoria: Any | None = None,
                     hoje: dt.date | None = None) -> ResultadoConfirmacao:
    """Confirma o perfil `alvo`. `confirmar(resumo)` devolve o texto digitado pelo usuario (tem de ser o simbolo).
    `terminal` (opcional) liga a comparacao com as especificacoes do terminal. Recusa => `ConfirmacaoRecusada`,
    sem alterar nada."""
    hoje = hoje or dt.date.today()
    resp = (responsavel or "").strip()
    if responsavel_pendente({"responsavel": resp}) or "\n" in resp or len(resp) > 200:
        raise ConfirmacaoRecusada("responsavel vazio, 'pendente...' ou invalido: informe quem confirma os valores")
    arquivo = Path(instrumentos if alvo.tipo == "ativo" else bolsas)
    if not arquivo.is_file():
        raise ConfirmacaoRecusada(f"arquivo nao encontrado: {arquivo}")
    if terminal is not None and alvo.tipo == "bolsa":
        raise ConfirmacaoRecusada("--comparar-terminal so se aplica a perfil de ativo (o terminal nao informa a bolsa)")
    sec = _bruto_secao(arquivo, alvo.codigo)
    if alvo.tipo == "ativo" and alvo.codigo not in ativos_disponiveis(arquivo):
        raise ConfirmacaoRecusada(f"'{alvo.codigo}' nao e um perfil de ativo em {arquivo}")
    if sec.get("a_confirmar") is False:
        return ResultadoConfirmacao(alvo, arquivo, False, [
            f"{alvo.rotulo} ja esta confirmado (por {sec.get('confirmado_por') or 'n/d'} em "
            f"{sec.get('confirmado_em') or 'n/d'}): nada a fazer."])
    nova_fonte = (fonte or "").strip() or str(sec.get("fonte") or "").strip()
    if not nova_fonte:
        raise ConfirmacaoRecusada("sem fonte registrada: informe --fonte (ex.: 'especificacao B3 + corretora X, pagina Y')")
    nova_data = (fonte_data or "").strip() or (str(sec.get("fonte_data") or "") if not fonte else "") or hoje.isoformat()
    if not RE_DATA.match(nova_data):
        raise ConfirmacaoRecusada(f"fonte_data '{nova_data}' fora do formato AAAA-MM-DD")
    try:
        dt.date.fromisoformat(nova_data)
    except ValueError as e:
        raise ConfirmacaoRecusada(f"fonte_data invalida: {e}") from e

    msgs: list[str] = []
    resumo: list[str]
    valores: dict[str, Any]
    perfil: PerfilInstrumento | None = None
    if alvo.tipo == "ativo":
        try:
            perfil = carregar_perfil(alvo.codigo, arquivo, permitir_a_confirmar=True)
        except ErroConfig as e:
            raise ConfirmacaoRecusada(f"perfil invalido: {e}") from e
        resumo = descrever_ativo(perfil, hoje)
        valores = perfil.model_dump(mode="json", exclude={"sessoes", "a_confirmar_itens"})
        for t in perfil.tarifas:
            if any(x in t.fonte.lower() for x in ("placeholder", "a confirmar", "suposicao")):
                msgs.append(f"AVISO: a tarifa vigente desde {t.vigente_desde} tem fonte '{t.fonte}'. Se o valor nao foi "
                            "conferido, NAO confirme: corrija a tarifa e a fonte no TOML antes.")
        try:
            b = carregar_bolsa(perfil.bolsa, bolsas if Path(bolsas).is_file() else None)
            if b.a_confirmar:
                msgs.append(f"AVISO: a bolsa {b.codigo} continua a_confirmar e mantem o ativo BLOQUEADO; confirme-a "
                            f"com: robowin perfil confirmar {PREFIXO_BOLSA}{b.codigo}")
        except ErroConfig as e:
            msgs.append(f"AVISO: bolsa do perfil nao carregou: {e}")
    else:
        try:
            b = carregar_bolsa(alvo.codigo, arquivo)
        except ErroConfig as e:
            raise ConfirmacaoRecusada(f"perfil de bolsa invalido: {e}") from e
        resumo = descrever_bolsa(b)
        valores = b.model_dump(mode="json")
    itens = list(sec.get("a_confirmar_itens", []))
    resumo += ["", "Itens que voce declara ter conferido:"] + [f"  - {i}" for i in itens or ["(lista vazia)"]]
    resumo += ["", f"Fonte: {nova_fonte} (data {nova_data})"
               + ("" if (fonte or "").strip() or not sec.get("fonte") else " [ja registrada no TOML]"),
               f"Responsavel: {resp}"]

    terminal_info: dict[str, Any] | None = None
    if terminal is not None and perfil is not None:
        origem, divergencias, observado = comparar_com_terminal(terminal, perfil, hoje)
        if divergencias:
            raise ConfirmacaoRecusada(f"especificacoes do {origem} DIVERGEM do perfil: " + "; ".join(divergencias)
                                      + ". Corrija o perfil (ou confira o simbolo) antes de confirmar.")
        terminal_info = {"origem": origem, **observado}
        resumo += [f"Terminal ({origem}): {observado.get('simbolo')} confere com o perfil "
                   f"(tick {observado.get('tick_pontos')}, valor do ponto por lote {observado.get('ponto_valor_por_lote')}, "
                   f"lote {observado.get('volume_min')}, moeda {observado.get('moeda')})"]
    resumo += msgs

    digitado = confirmar(resumo)
    if (digitado or "").strip().upper() != alvo.codigo:
        raise ConfirmacaoRecusada(f"confirmacao nao recebida (era preciso digitar {alvo.codigo}): nada foi alterado")

    sha_antes = sha256_arquivo(arquivo)
    texto = arquivo.read_bytes().decode("utf-8")
    novos = {"a_confirmar": False, "a_confirmar_itens": [], "fonte": nova_fonte, "fonte_data": nova_data,
             "confirmado_por": resp, "confirmado_em": hoje.isoformat()}
    try:
        novo_texto = atualizar_secao(texto, alvo.codigo, novos)
    except ErroEdicaoToml as e:
        raise ConfirmacaoRecusada(f"edicao de {arquivo.name} recusada: {e}") from e
    if sha256_arquivo(arquivo) != sha_antes:
        raise ConfirmacaoRecusada(f"{arquivo.name} mudou durante a confirmacao: nada foi gravado; rode de novo")
    gravar_atomico(arquivo, novo_texto)
    # releitura: o perfil continua valido e esta confirmado
    try:
        if alvo.tipo == "ativo":
            carregar_perfil(alvo.codigo, arquivo, permitir_a_confirmar=True)  # continua valido
            ok = carregar_toml(arquivo)[alvo.codigo].get("a_confirmar") is False  # o PROPRIO perfil (a bolsa e a parte)
        else:
            ok = not carregar_bolsa(alvo.codigo, arquivo).a_confirmar
    except ErroConfig as e:
        gravar_atomico(arquivo, texto)  # desfaz
        raise ConfirmacaoRecusada(f"perfil ficou invalido apos a edicao (desfeita): {e}") from e
    if not ok:
        gravar_atomico(arquivo, texto)
        raise ConfirmacaoRecusada("releitura nao mostrou o perfil confirmado (edicao desfeita)")
    if auditoria is not None:
        auditoria.registrar("perfil_confirmado", {
            "tipo": alvo.tipo, "codigo": alvo.codigo, "arquivo": str(arquivo), "responsavel": resp,
            "data": hoje.isoformat(), "fonte": nova_fonte, "fonte_data": nova_data, "itens_conferidos": itens,
            "valores": valores, "terminal": terminal_info, "sha256_antes": sha_antes,
            "sha256_depois": sha256_arquivo(arquivo)})
    return ResultadoConfirmacao(alvo, arquivo, True, msgs)


@dataclass(frozen=True)
class StatusPerfil:
    tipo: str
    codigo: str
    confirmado: bool  # o PROPRIO perfil (a_confirmar = false)
    bloqueado_por_bolsa: str | None
    fonte: str
    fonte_data: str
    confirmado_por: str
    confirmado_em: str
    itens_pendentes: int


def listar_status(instrumentos: str | Path, bolsas: str | Path) -> list[StatusPerfil]:
    out: list[StatusPerfil] = []
    bruto_b = carregar_toml(bolsas) if Path(bolsas).is_file() else {}
    b_pend = {k for k, v in bruto_b.items() if isinstance(v, dict) and v.get("a_confirmar", True)}
    bruto_i = carregar_toml(instrumentos)
    for a in ativos_disponiveis(instrumentos):
        s = bruto_i[a]
        bolsa = str(s.get("bolsa", "B3"))
        out.append(StatusPerfil("ativo", a, s.get("a_confirmar", True) is False,
                                bolsa if bolsa in b_pend or bolsa not in bruto_b else None,
                                str(s.get("fonte", "")), str(s.get("fonte_data", "")), str(s.get("confirmado_por", "")),
                                str(s.get("confirmado_em", "")), len(s.get("a_confirmar_itens", []))))
    for b in (bolsas_disponiveis(bolsas) if bruto_b else []):
        s = bruto_b[b]
        out.append(StatusPerfil("bolsa", b, s.get("a_confirmar", True) is False, None, str(s.get("fonte", "")),
                                str(s.get("fonte_data", "")), str(s.get("confirmado_por", "")),
                                str(s.get("confirmado_em", "")), len(s.get("a_confirmar_itens", []))))
    return out
