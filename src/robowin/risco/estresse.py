"""Cenarios de estresse: gap ate o limite de oscilacao (10%) e circuit breaker com posicao aberta.

O stop NAO protege de gap: ele executa no primeiro preco disponivel, que pode estar muito alem do stop.
Este modulo imprime a perda em R$ contra os limites da politica para o usuario tomar ciencia (obrigatoria
antes de qualquer etapa real).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.tipos import ConfigRisco, Instrumento, Lado

LIMITE_OSCILACAO_PCT = 10.0  # limite de oscilacao diaria considerado (ver especificacao do contrato)


@dataclass(frozen=True)
class CenarioEstresse:
    nome: str
    movimento_pct: float  # contra a posicao
    halt_min: int = 0  # circuit breaker: tempo sem negociacao (stop nao executa)
    extra_reabertura_pct: float = 0.0  # gap adicional na reabertura


CENARIOS_PADRAO = (
    CenarioEstresse("gap_2pct", 2.0),
    CenarioEstresse("gap_5pct", 5.0),
    CenarioEstresse("gap_ate_limite_10pct", LIMITE_OSCILACAO_PCT),
    CenarioEstresse("circuit_breaker_10pct_halt_30min_reabre_-2pct", 10.0, halt_min=30, extra_reabertura_pct=2.0),
    CenarioEstresse("circuit_breaker_15pct_halt_60min_reabre_-3pct", 15.0, halt_min=60, extra_reabertura_pct=3.0),
)


@dataclass(frozen=True)
class ResultadoEstresse:
    cenario: str
    movimento_total_pct: float
    pontos: float
    perda_brl: float
    perda_com_stop_brl: float
    x_perda_operacao: float
    excede: tuple[str, ...]


def perda_cenario(
    cenario: CenarioEstresse, preco: float, quantidade: int, instrumento: Instrumento, cfg: ConfigRisco,
    stop_pontos: float, lado: Lado = Lado.COMPRA,
) -> ResultadoEstresse:
    mov = cenario.movimento_pct + cenario.extra_reabertura_pct
    pontos_gap = preco * mov / 100.0
    pontos_perda = max(pontos_gap, 0.0)
    # com gap, o stop executa no pior preco: a perda e o maior entre o stop e o gap
    pontos_stop = stop_pontos + cfg.slippage_folga_pontos
    perda = pontos_perda * quantidade * instrumento.ponto_valor_brl
    perda_stop_ideal = pontos_stop * quantidade * instrumento.ponto_valor_brl  # sem gap (referencia)
    # gap menor que o stop: a perda ainda e o gap (posicao segue aberta); maior: stop executa no pior preco
    perda_real = perda
    excede = tuple(
        nome for nome, lim in (
            ("perda_max_operacao", cfg.perda_max_operacao_brl), ("perda_max_dia", cfg.perda_max_dia_brl),
            ("perda_max_semana", cfg.perda_max_semana_brl), ("dd_max", cfg.dd_max_brl))
        if perda_real > lim
    )
    return ResultadoEstresse(
        cenario=cenario.nome, movimento_total_pct=mov, pontos=pontos_perda, perda_brl=perda_real,
        perda_com_stop_brl=perda_stop_ideal, x_perda_operacao=perda_real / cfg.perda_max_operacao_brl,
        excede=excede)


def rodar_estresse(
    cfg: ConfigRisco, instrumento: Instrumento, preco: float = 130_000.0, quantidade: int = 1,
    stop_pontos: float | None = None, cenarios: tuple[CenarioEstresse, ...] = CENARIOS_PADRAO,
) -> list[ResultadoEstresse]:
    sp = cfg.stop_max_pontos if stop_pontos is None else stop_pontos
    return [perda_cenario(c, preco, quantidade, instrumento, cfg, sp) for c in cenarios]


def formatar_relatorio(res: list[ResultadoEstresse], cfg: ConfigRisco, preco: float, quantidade: int) -> str:
    linhas = [
        f"ESTRESSE DE GAP - {quantidade} contrato(s) a {preco:.0f} pts (SIMULACAO)",
        f"Limites: operacao R$ {cfg.perda_max_operacao_brl:.0f} | dia R$ {cfg.perda_max_dia_brl:.0f} | "
        f"semana R$ {cfg.perda_max_semana_brl:.0f} | drawdown R$ {cfg.dd_max_brl:.0f}",
        f"{'cenario':<50} {'mov%':>6} {'pontos':>9} {'perda R$':>10} {'x op.':>6}  excede",
    ]
    for r in res:
        linhas.append(f"{r.cenario:<50} {r.movimento_total_pct:>6.1f} {r.pontos:>9.0f} {r.perda_brl:>10.2f} "
                      f"{r.x_perda_operacao:>6.1f}  {', '.join(r.excede) or '-'}")
    linhas.append("O stop nao limita a perda em gap/circuit breaker: o usuario deve estar ciente antes de qualquer etapa real.")
    return "\n".join(linhas)


def imprimir_estresse(cfg: ConfigRisco, instrumento: Instrumento, preco: float = 130_000.0, quantidade: int = 1) -> str:
    res = rodar_estresse(cfg, instrumento, preco, quantidade)
    txt = formatar_relatorio(res, cfg, preco, quantidade)
    print(txt)
    return txt
