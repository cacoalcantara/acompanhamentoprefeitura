"""Capacidades do terminal, declaradas POR PLATAFORMA (MT5 ou MT4).

O nucleo, os bracos, o risco, o market making (MM) e as features de livro consultam estas capacidades em vez de
supor que o terminal e um MT5. Regra unica: capacidade exigida e ausente => BLOQUEIO com mensagem clara
(`CapacidadeAusente` ou lista de motivos); nunca degradacao silenciosa.

- MT5 (B3, conta netting/EXCHANGE): livro L2, historico de ticks (copy_ticks), tempo em ms, ticks de negocio.
- MT4 (ponte EA, mql4/RobowinBridge.mq4): conta HEDGING (ordem oposta abre OUTRA posicao), sem livro, sem
  historico de ticks (o EA transmite cada tick; lacuna so BLOQUEIA entradas), tempo em segundos (+GetTickCount).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

PLATAFORMAS = ("mt5", "mt4")


class CapacidadeAusente(RuntimeError):
    """Uma capacidade exigida nao existe na plataforma do terminal: a funcionalidade fica BLOQUEADA."""


@dataclass(frozen=True)
class CapacidadesTerminal:
    plataforma: Literal["mt5", "mt4"]
    livro: bool                 # livro de ofertas (market_book_*)
    historico_ticks: bool       # copy_ticks_from/range: permite recuperar lacunas (backfill)
    conta: Literal["netting", "hedging"]
    tempo_ms: int               # resolucao do carimbo de tempo do servidor (1 = ms; 1000 = segundos)
    ticks_de_negocio: bool      # last/volume/flags de agressor (BUY/SELL) nos ticks
    saida_por_ticket: bool = True
    sltp_na_ordem: bool = True

    @property
    def rotulo(self) -> str:
        return self.plataforma.upper()

    def como_dict(self) -> dict[str, Any]:
        return asdict(self)

    def exigir(self, nome: str, para: str) -> None:
        """Levanta `CapacidadeAusente` (mensagem clara) se a capacidade booleana `nome` nao existe."""
        if not bool(getattr(self, nome)):
            raise CapacidadeAusente(f"{para}: exige '{nome}', ausente em {self.rotulo} - {_POR_QUE[nome]}")

    def bloqueios(self, *, usa_livro: bool = False, usa_mm: bool = False, usa_backfill: bool = False,
                  usa_fluxo_agressor: bool = False, precisa_ms: int | None = None) -> list[str]:
        """Motivos de bloqueio para as exigencias informadas (lista vazia = nada a bloquear)."""
        m: list[str] = []
        if usa_livro and not self.livro:
            m.append(f"livro: {self.rotulo} nao tem livro de ofertas; features de livro desligadas")
        if usa_mm and not self.livro:
            m.append(f"market making: exige livro de ofertas, ausente em {self.rotulo}; MM desligado")
        if usa_backfill and not self.historico_ticks:
            m.append(f"backfill: {self.rotulo} nao tem historico de ticks; lacuna do feed so bloqueia entradas")
        if usa_fluxo_agressor and not self.ticks_de_negocio:
            m.append(f"fluxo agressor: {self.rotulo} nao entrega ticks de negocio (last/flags BUY/SELL)")
        if precisa_ms is not None and self.tempo_ms > precisa_ms:
            m.append(f"tempo: resolucao de {self.tempo_ms} ms em {self.rotulo}; a estrategia exige {precisa_ms} ms")
        return m


_POR_QUE = {
    "livro": "o MT4 nao expoe o livro de ofertas",
    "historico_ticks": "o MT4 nao tem copy_ticks (so os ticks que o EA viu ao vivo)",
    "ticks_de_negocio": "o MT4 so entrega bid/ask, sem last/volume/agressor",
    "saida_por_ticket": "a saida precisa ser por ticket",
    "sltp_na_ordem": "SL/TP precisam ir na ordem",
}

CAPACIDADES_MT5 = CapacidadesTerminal("mt5", livro=True, historico_ticks=True, conta="netting", tempo_ms=1,
                                      ticks_de_negocio=True)
CAPACIDADES_MT4 = CapacidadesTerminal("mt4", livro=False, historico_ticks=False, conta="hedging", tempo_ms=1000,
                                      ticks_de_negocio=False)
_POR_PLATAFORMA = {"mt5": CAPACIDADES_MT5, "mt4": CAPACIDADES_MT4}


def normalizar_plataforma(valor: str | None) -> str:
    """'mt5' | 'mt4' (padrao 'mt5'). Valor invalido levanta ValueError (nunca assume em silencio)."""
    p = (valor or "mt5").strip().lower()
    if p not in PLATAFORMAS:
        raise ValueError(f"plataforma '{valor}' invalida: use 'mt5' ou 'mt4'")
    return p


def capacidades_da_plataforma(plataforma: str | None) -> CapacidadesTerminal:
    return _POR_PLATAFORMA[normalizar_plataforma(plataforma)]


def capacidades_do_terminal(terminal: Any, plataforma: str | None = None) -> CapacidadesTerminal:
    """Capacidades do terminal: atributo `capacidades` do objeto, senao pela plataforma informada (padrao MT5)."""
    caps = getattr(terminal, "capacidades", None)
    if isinstance(caps, CapacidadesTerminal):
        return caps
    return capacidades_da_plataforma(plataforma)


def features_dependentes_de_livro() -> tuple[str, ...]:
    """Nomes das features do registro que consomem o livro L2 (causalidade 'snapshots do livro')."""
    from robowin.features.registro import (
        REGISTRO_PERMITIDO as REGISTRO,  # import tardio: evita ciclo features <-> mt5
    )
    return tuple(sorted(n for n, d in REGISTRO.items() if "livro" in d.causalidade))


def features_desligadas(caps: CapacidadesTerminal) -> tuple[str, ...]:
    """Features que a plataforma nao consegue alimentar (livro ausente => as de livro)."""
    return () if caps.livro else features_dependentes_de_livro()


def features_desligadas_por(caps: CapacidadesTerminal | None, perfil: Any) -> tuple[str, ...]:
    """Plataforma + INSTRUMENTO: forex/CFD/cripto na corretora nao tem negocios publicos (fluxo agressor e volume
    negociado desligados; atividade = `volume_tick_30s`); perfil sem livro desliga as de livro."""
    from robowin.features.registro import features_desligadas_instrumento
    fora = set(features_desligadas(caps)) if caps is not None else set()
    return tuple(sorted(fora | set(features_desligadas_instrumento(perfil))))
