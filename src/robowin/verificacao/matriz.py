"""Modelo e formatacao da matriz de verificacao. Nunca imprime valores de segredos."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from robowin.ia.segredos import redigir

APROVADO = "APROVADO"
REPROVADO = "REPROVADO"
NAO_VERIFICAVEL = "NAO VERIFICAVEL AQUI"
ESTADOS = (APROVADO, REPROVADO, NAO_VERIFICAVEL)


@dataclass
class Resultado:
    """Uma linha da matriz. `detalhe` explica o resultado; em NAO VERIFICAVEL AQUI e o MOTIVO."""
    nome: str
    estado: str
    detalhe: str = ""
    duracao_s: float = 0.0

    def __post_init__(self) -> None:
        if self.estado not in ESTADOS:
            raise ValueError(f"estado invalido: {self.estado!r}")
        self.detalhe = redigir(self.detalhe)


def aprovado(nome: str, detalhe: str = "") -> Resultado:
    return Resultado(nome, APROVADO, detalhe)


def reprovado(nome: str, detalhe: str) -> Resultado:
    return Resultado(nome, REPROVADO, detalhe)


def nao_verificavel(nome: str, motivo: str) -> Resultado:
    return Resultado(nome, NAO_VERIFICAVEL, motivo)


def contagem(res: list[Resultado]) -> dict[str, int]:
    return {e: sum(1 for r in res if r.estado == e) for e in ESTADOS}


def codigo_de_saida(res: list[Resultado]) -> int:
    """!= 0 se houver algum REPROVADO. NAO VERIFICAVEL AQUI nao reprova (e declarado, nao escondido)."""
    return 1 if any(r.estado == REPROVADO for r in res) else 0


def formatar(res: list[Resultado]) -> str:
    larg = max([len(r.nome) for r in res] + [10])
    linhas = ["=" * 100, "  robowin verificar - matriz de autoverificacao (SIMULACAO; nenhuma ordem real, nenhuma rede)",
              "  APROVADO = verificado aqui | REPROVADO = falhou | NAO VERIFICAVEL AQUI = depende de algo ausente neste ambiente",
              "=" * 100]
    for r in res:
        linhas.append(f"[{r.estado:<20}] {r.nome:<{larg}}  {r.detalhe}".rstrip())
    c = contagem(res)
    linhas += ["-" * 100, f"Resumo: {c[APROVADO]} aprovado(s), {c[REPROVADO]} reprovado(s), "
               f"{c[NAO_VERIFICAVEL]} nao verificavel(is) aqui.",
               "Aprovado aqui NAO prova lucratividade nem funcionamento no terminal real (ver docs/ENTREGA.md).",
               "RESULTADO: " + ("REPROVADO" if codigo_de_saida(res) else "SEM REPROVACOES")]
    return redigir("\n".join(linhas))


def para_json(res: list[Resultado]) -> str:
    corpo = {"resultado": "REPROVADO" if codigo_de_saida(res) else "SEM_REPROVACOES", "contagem": contagem(res),
             "itens": [asdict(r) for r in res]}
    return redigir(json.dumps(corpo, ensure_ascii=False, indent=2))
