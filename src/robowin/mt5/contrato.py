"""Resolucao do contrato vigente do ATIVO configurado: calendario B3 x symbols_get da corretora x perfil."""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from robowin.core import calendario_b3
from robowin.core.instrumentos import LETRAS_MES, PerfilInstrumento
from robowin.core.tipos import Instrumento

from .specs import EspecEsperada, instrumento_de, validar_especificacoes


class ContratoRecusado(Exception):
    """Contrato nao resolvido ou com especificacoes divergentes: NAO operar."""

    def __init__(self, motivos: list[str]) -> None:
        super().__init__("; ".join(motivos))
        self.motivos = motivos


@dataclass(frozen=True)
class Candidato:
    nome: str
    prefixo: str
    sufixo: str
    codigo: str  # ex.: WINZ26 (sem prefixo/sufixo da corretora)


@dataclass
class ResolucaoContrato:
    instrumento: Instrumento
    codigo_calendario: str
    simbolo_corretora: str
    prefixo: str
    sufixo: str
    vencimento: dt.date | None
    outros_contratos: list[str] = field(default_factory=list)


def _padrao(perfil: PerfilInstrumento) -> re.Pattern[str]:
    base = re.escape(perfil.simbolo_base)
    cod = rf"{base}[{LETRAS_MES}]\d{{2}}" if perfil.tem_vencimento else base
    return re.compile(rf"^(?P<pre>.*?)(?P<cod>{cod})(?P<suf>.*)$")


def listar_contratos(nomes: list[str], perfil: PerfilInstrumento) -> list[Candidato]:
    pad = _padrao(perfil)
    out = []
    for n in nomes:
        m = pad.match(n)
        if m:
            out.append(Candidato(n, m["pre"], m["suf"], m["cod"]))
    return out


def escolher_simbolo(nomes: list[str], codigo: str, perfil: PerfilInstrumento) -> Candidato:
    """Acha o simbolo da corretora para `codigo`, respeitando o mesmo prefixo/sufixo dos demais contratos."""
    todos = listar_contratos(nomes, perfil)
    base = perfil.simbolo_base
    if not todos:
        raise ContratoRecusado([f"symbols_get nao traz nenhum simbolo de {base}"])
    alvo = [c for c in todos if c.codigo == codigo]
    if not alvo:
        achados = sorted({c.codigo for c in todos})
        raise ContratoRecusado([f"contrato vigente {codigo} nao oferecido pela corretora (ha: {achados})"])
    if len(alvo) == 1:
        return alvo[0]
    exato = [c for c in alvo if c.nome == codigo]  # sem vencimento (acao/ETF): o ticker puro vence o fracionario
    if not perfil.tem_vencimento and len(exato) == 1:
        return exato[0]
    # varios: prefere a decoracao (prefixo, sufixo) predominante entre todos os contratos da corretora
    freq = Counter((c.prefixo, c.sufixo) for c in todos)
    melhor = max(freq.values())
    topo = {k for k, v in freq.items() if v == melhor}
    filtrados = [c for c in alvo if (c.prefixo, c.sufixo) in topo]
    if len(filtrados) == 1:
        return filtrados[0]
    raise ContratoRecusado([f"contrato {codigo} ambiguo na corretora: {[c.nome for c in alvo]}"])


def resolver_contrato(
    terminal: Any,
    hoje: dt.date,
    perfil: PerfilInstrumento,
    selecionar: bool = True,
) -> ResolucaoContrato:
    """Resolve o contrato vigente do ATIVO do perfil em `hoje` e valida as especificacoes da corretora CONTRA o perfil.

    Levanta `PerfilNaoConfirmado` se o perfil tem valores a_confirmar e `ContratoRecusado` se a corretora divergir."""
    perfil.exigir_confirmado()
    codigo = calendario_b3.contrato_vigente(perfil, hoje)
    simbolos = terminal.symbols_get(f"*{perfil.simbolo_base}*")
    if not simbolos:
        simbolos = terminal.symbols_get()
    if not simbolos:
        raise ContratoRecusado(["symbols_get vazio ou indisponivel"])
    nomes = [str(s.name) for s in simbolos]
    cand = escolher_simbolo(nomes, codigo, perfil)
    if selecionar and not terminal.symbol_select(cand.nome, True):
        raise ContratoRecusado([f"symbol_select falhou para {cand.nome}"])
    info = terminal.symbol_info(cand.nome)
    venc = calendario_b3.vencimento_do_codigo(perfil, codigo)
    erros = validar_especificacoes(info, EspecEsperada.do_perfil(perfil), venc)
    if erros:
        raise ContratoRecusado([f"{cand.nome}: {e}" for e in erros])
    inst = instrumento_de(cand.nome, info, venc, ativo=perfil.simbolo_base)
    outros = sorted(c.nome for c in listar_contratos(nomes, perfil) if c.nome != cand.nome)
    return ResolucaoContrato(inst, codigo, cand.nome, cand.prefixo, cand.sufixo, venc, outros)
