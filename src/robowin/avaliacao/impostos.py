"""Imposto de renda de day trade (estimativa; nao substitui parecer contabil).

Regras modeladas: aliquota de 20% sobre o ganho liquido mensal; IRRF de 1% sobre o ganho diario positivo,
creditado contra o imposto devido (saldo excedente transportado); prejuizo so compensa day trade (de
meses anteriores, sem prazo); DARF codigo 6015 com recolhimento minimo (valores menores acumulam).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from robowin.core.tipos import Trade

from .metricas import agrupar_por_dia


@dataclass(frozen=True)
class ConfigImpostos:
    aliquota_day_trade: float = 0.20
    aliquota_irrf: float = 0.01
    codigo_darf: str = "6015"
    darf_minimo_brl: float = 10.0


def carregar_config(dados: dict | None) -> ConfigImpostos:
    """Le a secao [impostos] de um dict de configuracao (TOML); ausente = padroes vigentes."""
    sec = (dados or {}).get("impostos", dados or {})
    campos = {k: v for k, v in sec.items() if k in ConfigImpostos.__dataclass_fields__}
    return ConfigImpostos(**campos)


@dataclass(frozen=True)
class ApuracaoMensal:
    mes: str  # AAAA-MM
    resultado_liquido_brl: float
    irrf_brl: float  # retido no mes (1% dos ganhos diarios positivos)
    prejuizo_compensado_brl: float
    prejuizo_a_compensar_brl: float  # saldo ao fim do mes
    base_calculo_brl: float
    imposto_bruto_brl: float
    irrf_utilizado_brl: float
    irrf_saldo_brl: float  # credito de IRRF transportado
    imposto_a_pagar_brl: float  # apos IRRF, antes do minimo
    darf_brl: float  # efetivamente recolhido no mes seguinte
    darf_acumulado_brl: float  # abaixo do minimo, transportado
    codigo_darf: str
    vencimento: str  # ultimo dia util do mes seguinte (aprox.: ultimo dia util seg-sex)

    def como_dict(self) -> dict:
        return asdict(self)


def _ultimo_dia_util_mes_seguinte(mes: str) -> str:
    a, m = int(mes[:4]), int(mes[5:7])
    m += 2  # primeiro dia do segundo mes seguinte, menos um dia = ultimo dia do mes seguinte
    if m > 12:
        a, m = a + 1, m - 12
    d = dt.date(a, m, 1) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def apurar(trades: Sequence[Trade], cfg: ConfigImpostos | None = None) -> list[ApuracaoMensal]:
    cfg = cfg or ConfigImpostos()
    por_mes: dict[str, list[float]] = {}
    for dia, ts in agrupar_por_dia(trades).items():
        por_mes.setdefault(dia[:7], []).append(sum(t.liquido_brl for t in ts))
    out: list[ApuracaoMensal] = []
    prejuizo, irrf_saldo, darf_pend = 0.0, 0.0, 0.0
    for mes in sorted(por_mes):
        dias = por_mes[mes]
        resultado = sum(dias)
        irrf = cfg.aliquota_irrf * sum(d for d in dias if d > 0)
        comp = 0.0
        if resultado < 0:
            prejuizo += -resultado
            base = 0.0
        else:
            comp = min(prejuizo, resultado)
            prejuizo -= comp
            base = resultado - comp
        bruto = cfg.aliquota_day_trade * base
        disponivel = irrf_saldo + irrf
        usado = min(disponivel, bruto)
        irrf_saldo = disponivel - usado
        a_pagar = bruto - usado
        total = a_pagar + darf_pend
        if total >= cfg.darf_minimo_brl:
            darf, darf_pend = total, 0.0
        else:
            darf, darf_pend = 0.0, total
        out.append(ApuracaoMensal(
            mes=mes, resultado_liquido_brl=resultado, irrf_brl=irrf, prejuizo_compensado_brl=comp,
            prejuizo_a_compensar_brl=prejuizo, base_calculo_brl=base, imposto_bruto_brl=bruto,
            irrf_utilizado_brl=usado, irrf_saldo_brl=irrf_saldo, imposto_a_pagar_brl=a_pagar, darf_brl=darf,
            darf_acumulado_brl=darf_pend, codigo_darf=cfg.codigo_darf, vencimento=_ultimo_dia_util_mes_seguinte(mes)))
    return out


def resultado_apos_ir(trades: Sequence[Trade], cfg: ConfigImpostos | None = None) -> dict[str, float]:
    """Lucro liquido antes e depois do IR estimado (IR efetivo = DARF + pendente; IRRF ja descontado nele)."""
    ap = apurar(trades, cfg)
    antes = sum(a.resultado_liquido_brl for a in ap)
    ir = sum(a.imposto_bruto_brl for a in ap)  # imposto total devido (IRRF + DARF)
    return {"antes_ir_brl": antes, "ir_estimado_brl": ir, "depois_ir_brl": antes - ir}
