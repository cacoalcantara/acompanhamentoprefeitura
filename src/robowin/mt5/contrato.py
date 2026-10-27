"""Resolucao do contrato WIN vigente: calendario B3 x symbols_get da corretora x especificacoes."""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from robowin.core import calendario_b3
from robowin.core.tipos import Instrumento

from .specs import ESPEC_WIN, EspecEsperada, instrumento_de, validar_especificacoes

LETRAS_MES = "FGHJKMNQUVXZ"


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
    vencimento: dt.date
    outros_contratos: list[str] = field(default_factory=list)


def _padrao(base: str) -> re.Pattern[str]:
    return re.compile(rf"^(?P<pre>.*?)(?P<cod>{re.escape(base)}[{LETRAS_MES}]\d{{2}})(?P<suf>.*)$")


def listar_contratos(nomes: list[str], base: str = "WIN") -> list[Candidato]:
    pad = _padrao(base)
    out = []
    for n in nomes:
        m = pad.match(n)
        if m:
            out.append(Candidato(n, m["pre"], m["suf"], m["cod"]))
    return out


def escolher_simbolo(nomes: list[str], codigo: str, base: str = "WIN") -> Candidato:
    """Acha o simbolo da corretora para `codigo`, respeitando o mesmo prefixo/sufixo dos demais contratos."""
    todos = listar_contratos(nomes, base)
    if not todos:
        raise ContratoRecusado([f"symbols_get nao traz nenhum contrato {base}"])
    alvo = [c for c in todos if c.codigo == codigo]
    if not alvo:
        achados = sorted({c.codigo for c in todos})
        raise ContratoRecusado([f"contrato vigente {codigo} nao oferecido pela corretora (ha: {achados})"])
    if len(alvo) == 1:
        return alvo[0]
    # varios: prefere a decoracao (prefixo, sufixo) predominante entre todos os contratos da corretora
    freq = Counter((c.prefixo, c.sufixo) for c in todos)
    melhor = max(freq.values())
    topo = {k for k, v in freq.items() if v == melhor}
    filtrados = [c for c in alvo if (c.prefixo, c.sufixo) in topo]
    if len(filtrados) == 1:
        return filtrados[0]
    raise ContratoRecusado([f"contrato {codigo} ambiguo na corretora: {[c.nome for c in alvo]}"])


def _vencimento_do_codigo(codigo: str, base: str = "WIN") -> dt.date:
    letra, aa = codigo[len(base)], int(codigo[len(base) + 1:])
    mes = LETRAS_MES.index(letra) + 1
    return calendario_b3.vencimento_win(2000 + aa, mes)


def resolver_contrato_win(
    terminal: Any,
    hoje: dt.date,
    esperado: EspecEsperada = ESPEC_WIN,
    base: str = "WIN",
    selecionar: bool = True,
) -> ResolucaoContrato:
    """Resolve o WIN vigente em `hoje` e valida as especificacoes. Levanta `ContratoRecusado` se divergir."""
    codigo = calendario_b3.contrato_vigente(hoje)
    simbolos = terminal.symbols_get(f"*{base}*")
    if not simbolos:
        simbolos = terminal.symbols_get()
    if not simbolos:
        raise ContratoRecusado(["symbols_get vazio ou indisponivel"])
    nomes = [str(s.name) for s in simbolos]
    cand = escolher_simbolo(nomes, codigo, base)
    if selecionar and not terminal.symbol_select(cand.nome, True):
        raise ContratoRecusado([f"symbol_select falhou para {cand.nome}"])
    info = terminal.symbol_info(cand.nome)
    venc = _vencimento_do_codigo(codigo, base)
    erros = validar_especificacoes(info, esperado, venc)
    if erros:
        raise ContratoRecusado([f"{cand.nome}: {e}" for e in erros])
    inst = instrumento_de(cand.nome, info, venc)
    outros = sorted(c.nome for c in listar_contratos(nomes, base) if c.nome != cand.nome)
    return ResolucaoContrato(inst, codigo, cand.nome, cand.prefixo, cand.sufixo, venc, outros)
