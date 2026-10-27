"""Esquemas de saida dos papeis (neutros quanto ao fornecedor), reexportados de core.tipos.

Nenhum deles tem campo de raciocinio/thinking: o LLM so devolve justificativa curta.
"""
from robowin.core.tipos import ClassificacaoNoticia, PropostaIA, RevisaoIA

ESQUEMAS_POR_PAPEL = {
    "proponente": PropostaIA,
    "revisor": RevisaoIA,
    "classificador_nlp": ClassificacaoNoticia,
}

__all__ = ["ESQUEMAS_POR_PAPEL", "ClassificacaoNoticia", "PropostaIA", "RevisaoIA"]
