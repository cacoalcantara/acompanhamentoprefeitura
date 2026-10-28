"""Probabilidades do plano proposto como EVIDENCIA para o revisor (Maquina do Tempo) e veto opcional por odds.

Atribuicao: ideia e metodo do Time Machine inspirados em day-trader-skill (Jake Schincariol, MIT); ver
``avaliacao/maquina_do_tempo.py`` e THIRD_PARTY_NOTICES.md.

* Depois que o proponente propoe stop/alvo, ``AvaliadorOdds.avaliar`` roda a simulacao de bootstrap das sessoes
  ANTERIORES ao dia de ``t0`` (sem vazamento, tambem em backtest) e devolve o campo ANONIMIZADO
  ``probabilidades_do_plano`` (probabilidades, R medio, veredito, n de sessoes: sem datas e sem niveis absolutos).
* O revisor recebe o campo no dossie da revisao. E SIMULACAO, NAO e previsao; concordancia nao prova lucro.
* ``[decisao].veto_por_odds`` (DESLIGADO por padrao) bloqueia a entrada se a fracao liquida de alvos entre os
  desfechos resolvidos ficar abaixo do equilibrio liquido de custos + ``margem_veto_odds``.
* Falha ou dados insuficientes => ``indisponivel``: NUNCA bloqueia e NUNCA aprova por omissao.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from robowin.avaliacao import maquina_do_tempo as mt
from robowin.core.calendario_b3 import SP
from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import Lado
from robowin.execucao.custos import Custos
from robowin.ia.dossie import verificar_anonimizacao

CAMPO = "probabilidades_do_plano"
MOTIVO_VETO = "veto_por_odds"
STATUS_INDISPONIVEL = "indisponivel"
STATUS_OK = "ok"
NATUREZA = "simulacao_de_bootstrap_nao_e_previsao"
MINIMO_RESOLVIDOS = 200  # replays que resolveram (alvo ou stop) para o veto ter base estatistica


@dataclass(frozen=True)
class ConfigOdds:
    veto_por_odds: bool = False  # DESLIGADO por padrao
    margem_veto_odds: float = 0.05  # fracao somada ao equilibrio liquido
    replays: int = mt.REPLAYS_PADRAO
    seed: int = mt.SEED_PADRAO
    janela: int = mt.JANELA_PADRAO
    slippage_stop_ticks: float = 0.0
    intervalo_s: int = mt.INTERVALO_PADRAO_S


def config_odds(cfg: Mapping[str, Any] | None) -> ConfigOdds:
    """Le ``[decisao]`` de config/robowin.toml (chaves ausentes => padroes; veto_por_odds = false)."""
    sec = dict((cfg or {}).get("decisao", {}) or {})
    base = ConfigOdds()
    return ConfigOdds(
        veto_por_odds=bool(sec.get("veto_por_odds", base.veto_por_odds)),
        margem_veto_odds=float(sec.get("margem_veto_odds", base.margem_veto_odds)),
        replays=int(sec.get("odds_replays", base.replays)), seed=int(sec.get("odds_seed", base.seed)),
        janela=int(sec.get("odds_janela", base.janela)),
        slippage_stop_ticks=float(sec.get("odds_slippage_stop_ticks", base.slippage_stop_ticks)),
        intervalo_s=int(sec.get("odds_intervalo_s", base.intervalo_s)))


def campo_indisponivel(motivo: str) -> dict[str, Any]:
    """Campo do dossie quando nao ha base: o revisor NAO deve inferir nada dele."""
    return {"status": STATUS_INDISPONIVEL, "motivo": motivo, "natureza": NATUREZA}


@dataclass(frozen=True)
class AvaliacaoOdds:
    campo: dict[str, Any]  # o que vai ao revisor (anonimizado)
    disponivel: bool
    veto: bool = False
    motivo_veto: str = ""
    resultado: mt.ResultadoSimulacao | None = None

    @property
    def motivo_bloqueio(self) -> str:
        return f"{MOTIVO_VETO}:{self.motivo_veto}"


def _arred(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def campo_do_resultado(r: mt.ResultadoSimulacao) -> dict[str, Any]:
    return {
        "status": STATUS_OK, "natureza": NATUREZA,
        "p_alvo_primeiro": _arred(r.p_alvo), "p_stop_primeiro": _arred(r.p_stop), "p_nenhum": _arred(r.p_nenhum),
        "p_verde": _arred(r.p_verde), "p_alvo_entre_resolvidos": _arred(r.p_alvo_entre_resolvidos),
        "equilibrio_liquido": _arred(r.equilibrio_liquido), "r_alvo": _arred(r.r_alvo, 3),
        "r_medio_bruto": _arred(r.r_bruto_medio), "r_medio_liquido": _arred(r.r_liquido_medio),
        "veredito": r.veredito.codigo, "n_sessoes": int(r.n_sessoes), "n_replays": int(r.replays),
        "sintetico": bool(r.sintetico),
    }


class AvaliadorOdds:
    """Calcula o campo ``probabilidades_do_plano``. Nunca levanta excecao: falha => ``indisponivel``."""

    def __init__(self, perfil: PerfilInstrumento, piscina: mt.PiscinaSessoes | None, cfg: ConfigOdds | None = None,
                 custos: Custos | None = None) -> None:
        self.perfil, self.piscina, self.cfg = perfil, piscina, cfg or ConfigOdds()
        self.custos = custos
        self._cache: dict[tuple[Any, ...], AvaliacaoOdds] = {}

    def avaliar(self, *, t0_ns: int, lado: Lado, stop_pontos: float, alvo_pontos: float,
                preco_ref: float | None = None) -> AvaliacaoOdds:
        try:
            return self._avaliar(t0_ns, lado, stop_pontos, alvo_pontos, preco_ref)
        except Exception as e:  # qualquer falha: indisponivel (nunca bloqueia nem aprova por omissao)
            return AvaliacaoOdds(campo_indisponivel(f"falha:{type(e).__name__}"), False)

    def _avaliar(self, t0_ns: int, lado: Lado, stop_pontos: float, alvo_pontos: float,
                 preco_ref: float | None) -> AvaliacaoOdds:
        if self.piscina is None:
            return AvaliacaoOdds(campo_indisponivel("sem_piscina_de_sessoes"), False)
        agora = dt.datetime.fromtimestamp(t0_ns / 1e9, SP)
        grade = mt.grade_da_sessao(self.perfil, agora.date(), self.cfg.intervalo_s)
        seg = agora.hour * 3600 + agora.minute * 60 + agora.second
        if not grade.abertura_s <= seg < grade.zeragem_s:
            return AvaliacaoOdds(campo_indisponivel("fora_da_janela_da_sessao"), False)
        j = (seg - grade.abertura_s) // grade.intervalo_s + 1  # a entrada vale na PROXIMA fronteira de barra
        plano = mt.PlanoMT(lado, stop_pontos, alvo_pontos, agora.date(), grade.hora_da_barra(j),
                           mt.s_para_hhmm(grade.zeragem_s), preco_ref)
        rp = mt.resolver_plano(plano, self.perfil, self.cfg.intervalo_s)
        chave = (agora.date(), rp.j, lado, rp.stop_ticks, rp.alvo_ticks)
        if chave in self._cache:
            return self._cache[chave]
        r = mt.simular(self.piscina, self.perfil, plano, seed=self.cfg.seed, replays=self.cfg.replays,
                       janela=self.cfg.janela, slippage_stop_ticks=self.cfg.slippage_stop_ticks, custos=self.custos)
        campo = campo_do_resultado(r)
        niveis = [x for x in (preco_ref, r.preco_entrada) if x]
        if verificar_anonimizacao({CAMPO: campo}, niveis):  # nunca vaza data/nivel absoluto
            return AvaliacaoOdds(campo_indisponivel("anonimizacao_falhou"), False)
        veto, motivo = self._veto(r)
        av = AvaliacaoOdds(campo, True, veto, motivo, r)
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[chave] = av
        return av

    def _veto(self, r: mt.ResultadoSimulacao) -> tuple[bool, str]:
        """Veto (so se ligado): fracao de alvos entre resolvidos < equilibrio liquido + margem."""
        if not self.cfg.veto_por_odds or r.p_alvo_entre_resolvidos is None:
            return False, ""
        if (r.p_alvo + r.p_stop) * r.replays < MINIMO_RESOLVIDOS:
            return False, ""  # base estatistica insuficiente: indisponivel nunca bloqueia
        limiar = r.equilibrio_liquido + self.cfg.margem_veto_odds
        if r.p_alvo_entre_resolvidos < limiar:
            return True, (f"p_alvo_resolvidos={r.p_alvo_entre_resolvidos:.3f}<equilibrio_liquido+margem={limiar:.3f}")
        return False, ""
