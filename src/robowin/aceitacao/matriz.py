"""Matriz da aceitacao: reaproveita os 3 estados de `verificacao.matriz` e acrescenta etapa, criterio e dados medidos.

Regra central: etapa sem pre-requisito e NAO VERIFICAVEL (com o motivo), NUNCA aprovada por omissao.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from robowin.ia.segredos import redigir
from robowin.verificacao.matriz import (
    APROVADO,
    ESTADOS,
    NAO_VERIFICAVEL,
    REPROVADO,
    Resultado,
    codigo_de_saida,
    contagem,
)

__all__ = ["APROVADO", "ESTADOS", "NAO_VERIFICAVEL", "REPROVADO", "ResultadoEtapa", "aprovada", "reprovada",
           "nao_verificavel", "contagem", "codigo_de_saida", "veredito", "mascarar_login", "higienizar", "para_dict"]


@dataclass
class ResultadoEtapa(Resultado):
    """Uma linha da matriz de aceitacao. `aplicavel=False` so para o que nao se aplica de fato (ex.: EA em MT5)."""
    etapa: str = ""
    criterio: str = ""
    dados: dict[str, Any] = field(default_factory=dict)
    aplicavel: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        self.criterio = redigir(self.criterio)


def aprovada(etapa: str, nome: str, criterio: str, detalhe: str = "", **dados: Any) -> ResultadoEtapa:
    return ResultadoEtapa(nome, APROVADO, detalhe, etapa=etapa, criterio=criterio, dados=dados)


def reprovada(etapa: str, nome: str, criterio: str, detalhe: str, **dados: Any) -> ResultadoEtapa:
    return ResultadoEtapa(nome, REPROVADO, detalhe, etapa=etapa, criterio=criterio, dados=dados)


def nao_verificavel(etapa: str, nome: str, criterio: str, motivo: str, *, aplicavel: bool = True,
                    **dados: Any) -> ResultadoEtapa:
    return ResultadoEtapa(nome, NAO_VERIFICAVEL, motivo, etapa=etapa, criterio=criterio, dados=dados, aplicavel=aplicavel)


def veredito(res: list[ResultadoEtapa], *, duble: bool = False, abortada: bool = False) -> str:
    """REPROVADO | INCOMPLETO | ACEITO. Contra o duble NUNCA e 'ACEITO': so a aceitacao no terminal real vale."""
    if any(r.estado == REPROVADO for r in res):
        return "REPROVADO"
    if duble:
        return "SEM REPROVACOES NO DUBLE (simulacao: NAO vale como aceitacao no terminal real)"
    if abortada:
        return "INCOMPLETO (execucao abortada)"
    pend = [r.etapa for r in res if r.estado == NAO_VERIFICAVEL and r.aplicavel]
    if pend:
        return "INCOMPLETO (nao verificadas: " + ", ".join(pend) + ")"
    return "ACEITO (todas as etapas aplicaveis aprovadas e nenhuma reprovada)"


def mascarar_login(login: int | str | None) -> str:
    """Mascara o login da conta: so os 2 ultimos digitos."""
    s = "" if login is None else str(login).strip()
    if not s:
        return "(nao informado)"
    return "****" + s[-2:] if len(s) > 2 else "****"


_RE_USUARIO_WIN = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+")
_RE_USUARIO_UNIX = re.compile(r"(/(?:home|Users)/)[^/\s\"']+")


def higienizar(texto: str, logins: tuple[int | str | None, ...] = ()) -> str:
    """Ultima passada antes de gravar/imprimir: login completo -> mascara, nome de usuario dos caminhos -> ***,
    e `redigir` (chaves/senhas). Aplicada ao relatorio inteiro."""
    for lg in logins:
        if lg is None:
            continue
        s = str(lg).strip()
        if len(s) >= 4:
            texto = texto.replace(s, mascarar_login(s))
    texto = _RE_USUARIO_WIN.sub(r"\1***", texto)
    texto = _RE_USUARIO_UNIX.sub(r"\1***", texto)
    return redigir(texto)


def para_dict(r: ResultadoEtapa) -> dict[str, Any]:
    d = asdict(r)
    d["duracao_s"] = round(float(d["duracao_s"]), 3)
    return d
