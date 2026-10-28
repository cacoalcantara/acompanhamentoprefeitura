"""Treino walk-forward do modelo quant: o modelo do pregao D so ve pregoes ESTRITAMENTE anteriores a D.

Sem busca de parametros (os mesmos do ``robowin simular``). As amostras de cada pregao (features causais +
rotulos de tripla barreira do proprio pregao) sao calculadas uma vez e reaproveitadas; o rotulo de um pregao
anterior termina no proprio pregao, logo antes de D. ``treinar_para_dia`` CONFERE (e levanta) se algo de D ou
posterior entrar no treino.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import Tick
from robowin.dados.ticks import dia_ns
from robowin.modelos.microestrutura import Amostras, CriterioOOS, ModeloMicroestrutura
from robowin.modelos.rotulagem import ConfigBarreiras
from robowin.modelos.rotulagem import Custos as CustosRotulagem
from robowin.modelos.walkforward import VazamentoDetectado

PASSO_AMOSTRAS_S = 15  # mesmo passo do treino do `robowin simular`
MIN_AMOSTRAS = 50      # minimo do ModeloMicroestrutura
ORIGEM_MODELO = "historico_real"


def amostras_do_dia(ticks: list[Tick], perfil: PerfilInstrumento) -> Amostras | None:
    """Amostras de UM pregao (None se curto demais para ter instantes com horizonte completo)."""
    from robowin.modelos.demo_sintetico import montar_amostras
    if len(ticks) < 2:
        return None
    try:
        am = montar_amostras(ticks, [], ConfigBarreiras.do_perfil(perfil), CustosRotulagem.do_perfil(perfil),
                             passo_s=PASSO_AMOSTRAS_S)
    except ValueError:
        return None
    return am if len(am) else None


def juntar_amostras(lista: list[Amostras]) -> Amostras | None:
    lista = [a for a in lista if a is not None and len(a)]
    if not lista:
        return None
    colunas = sorted(set().union(*(a.X.columns for a in lista)))
    return Amostras(
        t_ns=np.concatenate([a.t_ns for a in lista]), t_fim_ns=np.concatenate([a.t_fim_ns for a in lista]),
        X=pd.concat([a.X.reindex(columns=colunas) for a in lista], ignore_index=True),
        y_compra=np.concatenate([a.y_compra for a in lista]), y_venda=np.concatenate([a.y_venda for a in lista]),
        pnl_compra=np.concatenate([a.pnl_compra for a in lista]), pnl_venda=np.concatenate([a.pnl_venda for a in lista]))


@dataclass
class InfoTreino:
    dia: str
    dias_treino: list[str] = field(default_factory=list)
    amostras: int = 0
    status: str = "nao_treinado"
    treinado_ate: str = ""
    hash_dados: str = ""
    motivo: str = ""

    def como_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def treinar_para_dia(d: dt.date, amostras_por_dia: Mapping[dt.date, Amostras | None], perfil: PerfilInstrumento, *,
                     dias_treino: int, tipo: str = "logistica", seed: int = 7) -> tuple[ModeloMicroestrutura, InfoTreino]:
    """Modelo para o pregao `d` com os ultimos `dias_treino` pregoes < `d` que tem amostras. Sem amostras
    suficientes: modelo ``nao_treinado`` (o braco quant NAO opera nesse dia)."""
    m = ModeloMicroestrutura(tipo=tipo, nome=f"microestrutura_{tipo}", seed=seed)
    m.ativo = perfil.simbolo_base
    info = InfoTreino(d.isoformat())
    anteriores = sorted(x for x, am in amostras_por_dia.items() if x < d and am is not None)[-max(dias_treino, 0):]
    if any(x >= d for x in anteriores):  # defensivo
        raise VazamentoDetectado(f"pregao >= {d} no treino do pregao {d}")
    info.dias_treino = [x.isoformat() for x in anteriores]
    am = juntar_amostras([amostras_por_dia[x] for x in anteriores])  # type: ignore[misc]
    if am is None or len(am) < MIN_AMOSTRAS:
        info.amostras = 0 if am is None else len(am)
        info.motivo = ("sem pregao anterior com amostras" if not anteriores
                       else f"amostras insuficientes ({info.amostras} < {MIN_AMOSTRAS})")
        return m, info
    inicio_d = dia_ns(d)
    if int(am.t_fim_ns.max()) >= inicio_d or int(am.t_ns.max()) >= inicio_d:
        raise VazamentoDetectado(f"amostra com rotulo/instante no pregao {d} ou depois")
    m.treinar(am, origem=ORIGEM_MODELO, criterio=CriterioOOS())
    m.ativo = perfil.simbolo_base
    if m.treinado_ate_ns >= inicio_d:
        raise VazamentoDetectado(f"modelo do pregao {d} treinado ate {m.treinado_ate_ns} (>= inicio do pregao)")
    info.amostras, info.status, info.hash_dados = len(am), m.status, m.hash_dados
    info.treinado_ate = dt.datetime.fromtimestamp(m.treinado_ate_ns / 1e9, dt.UTC).isoformat()
    return m, info
