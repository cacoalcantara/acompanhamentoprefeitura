"""Esquemas de saida dos papeis (neutros quanto ao fornecedor).

Os tres primeiros sao reexportados de core.tipos. Os demais (debate, parecer de risco, reflexao) sao do braco
experimental ``debate`` e da memoria reflexiva: nenhum tem campo de raciocinio/thinking, so texto curto que
a validacao deterministica limita e confere contra o dossie.
"""
from typing import Literal

from pydantic import Field

from robowin.core.tipos import Base, ClassificacaoNoticia, PropostaIA, RevisaoIA


class ArgumentoDebate(Base):
    """Argumento de um pesquisador (altista ou baixista). Cada evidencia DEVE citar um campo do dossie."""
    lado: Literal["altista", "baixista"]
    tese: str
    evidencias: tuple[str, ...] = ()
    pontos_fracos_do_oponente: tuple[str, ...] = ()
    forca: float = Field(ge=0, le=1, default=0.5)


class ParecerRisco(Base):
    """Parecer de um debatedor de risco (agressivo, neutro ou conservador) sobre a tese em debate."""
    perfil: Literal["agressivo", "neutro", "conservador"]
    recomendacao: Literal["operar", "reduzir", "nao_operar"]
    riscos: tuple[str, ...] = ()
    evidencias: tuple[str, ...] = ()
    justificativa_curta: str = ""


class ReflexaoIA(Base):
    """Licao curta escrita DEPOIS do resultado liquidado de uma operacao (memoria reflexiva)."""
    licao: str
    aspecto: Literal["tese", "risco", "geometria", "tempo", "dados"] = "tese"
    vies: Literal["manter", "mais_cautela", "menos_cautela"] = "manter"


ESQUEMAS_POR_PAPEL = {
    "proponente": PropostaIA,
    "revisor": RevisaoIA,
    "classificador_nlp": ClassificacaoNoticia,
    "debate_altista": ArgumentoDebate,
    "debate_baixista": ArgumentoDebate,
    "risco_agressivo": ParecerRisco,
    "risco_neutro": ParecerRisco,
    "risco_conservador": ParecerRisco,
    "reflexao": ReflexaoIA,
}

__all__ = ["ESQUEMAS_POR_PAPEL", "ArgumentoDebate", "ClassificacaoNoticia", "ParecerRisco", "PropostaIA",
           "ReflexaoIA", "RevisaoIA"]
