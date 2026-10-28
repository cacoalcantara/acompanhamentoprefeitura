"""Execucao da simulacao historica walk-forward causal (um orquestrador NOVO por pregao).

Garantias (testadas em tests/unit/test_historico.py):
* replay de D por instante de DISPONIBILIDADE (``t_recebido_ns = t_evento + atraso de entrega``): o orquestrador,
  as features, o simulador de execucao e os bracos so veem o que ja chegou; perturbar o futuro de D nao muda as
  decisoes anteriores ao instante perturbado;
* modelo quant de D treinado so com pregoes < D (``treino.treinar_para_dia`` confere); Maquina do Tempo so com
  sessoes < D; nenhum estado passa de um pregao para o outro alem disso (orquestrador, banco e contas novos);
* IA: provedor FALSO (rotulado) sem chave de API; real so com TODAS as chaves dos provedores dos papeis
  (``provedor = auto``). Pregao posterior ao corte de conhecimento dos LLMs = ``limpo``; anterior = ``contaminado``.
"""
from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import json
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robowin.avaliacao import maquina_do_tempo as mt
from robowin.core.calendario_b3 import contrato_vigente, vencimento_do_codigo
from robowin.core.config import ErroConfig
from robowin.core.instrumentos import PerfilInstrumento, perfil_da_config
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import Tick, Trade
from robowin.dados.b3_negocios import ORIGEM as ORIGEM_B3
from robowin.dados.b3_negocios import ROTULO_REAL_B3
from robowin.dados.ticks import ArmazenamentoTicks
from robowin.decisao.simulacao import (
    RAIZ_CONFIG,
    ParamsSimulacao,
    abrir_banco,
    carregar_ambiente,
    construir_orquestrador,
)
from robowin.ia.catalogo import Catalogo, dividir_modelo
from robowin.ia.papeis import carregar_papeis
from robowin.modelos.microestrutura import Amostras

from .compactacao import EstatisticaCompactacao, compactar_janelas, ticks_no_instante
from .treino import amostras_do_dia, treinar_para_dia

NS = 1_000_000_000
CONTROLE = "controle_aleatorio"
SELO = ("DADOS REAIS B3 (negocios publicos), lado agressor INFERIDO pela regra do tick, cotacao inferida (spread de "
        "1 tick), SEM livro de ofertas; replay estritamente causal por t_disponivel. Poucos pregoes = AMOSTRA "
        "INSUFICIENTE. NAO prova lucratividade.")


class ErroHistorico(ErroConfig):
    """Configuracao/dados invalidos para a simulacao historica."""


@dataclass
class ParamsHistorico:
    armazem: Path
    saida: Path
    ativo: str | None = None
    dias: int | None = None  # ultimos N pregoes do armazem simulados (None = todos)
    bracos: tuple[str, ...] | None = None
    provedor: str = "auto"  # auto | falso | real
    seed: int = 7
    compactar_ms: float = 1000.0
    dias_treino: int = 5
    tipo_modelo: str = "logistica"
    periodo_ia_s: float = 240.0
    idade_max_dado_s: float = 15.0
    com_condicional: bool = True
    p_timeout: float = 0.03
    p_http: float = 0.02
    orcamento_usd: float | None = None
    n_boot: int = 1000
    config: Path = field(default_factory=lambda: RAIZ_CONFIG / "robowin.toml")
    catalogo: Path = field(default_factory=lambda: RAIZ_CONFIG / "modelos.toml")
    risco: Path = field(default_factory=lambda: RAIZ_CONFIG / "risco.toml")
    aprovacao: Path = field(default_factory=lambda: RAIZ_CONFIG / "aprovacoes" / "risco-v1.toml")


def params_de_config(cfg: Mapping[str, Any] | None, **kw: Any) -> ParamsHistorico:
    """``[historico]`` de config/robowin.toml como padrao; argumentos explicitos (nao None) vencem."""
    sec = dict((cfg or {}).get("historico", {}) or {})
    base = {k: v for k, v in kw.items() if v is not None}
    for chave, tipo in (("compactar_ms", float), ("dias_treino", int), ("periodo_ia_s", float),
                        ("idade_max_dado_s", float)):
        if chave not in base and chave in sec:
            base[chave] = tipo(sec[chave])
    return ParamsHistorico(**base)


# ================================================================================================ provedor / corte
def refs_ia(cfg: Mapping[str, Any]) -> list[str]:
    """Modelos (provedor:id) que os papeis e os bracos extras podem chamar."""
    from robowin.decisao.bracos.fabrica import extras_de_config
    papeis = carregar_papeis(dict(cfg)) if (cfg.get("ia") or {}).get("papeis") else {}
    refs = [p.modelo for p in papeis.values()]
    refs += [r for e in extras_de_config(dict(cfg))
             for r in (e.proponente, e.revisor, e.pesquisador, e.debatedor_risco) if r and ":" in r]
    return sorted(set(refs))


def decidir_provedor(pedido: str, refs: Collection[str]) -> tuple[str, str]:
    """('falso'|'real', motivo). ``auto``: real SO se houver chave para TODOS os provedores dos papeis."""
    from robowin.ia.cli_cmds import tem_chave
    nomes = sorted({dividir_modelo(r)[0] for r in refs})
    sem = [n for n in nomes if not tem_chave(n)]
    if pedido == "falso":
        return "falso", "pedido --provedor falso"
    if pedido == "real":
        if sem:
            raise ErroHistorico(f"--provedor real sem chave de API para: {', '.join(sem)}")
        return "real", f"chaves presentes para {', '.join(nomes)}"
    if pedido != "auto":
        raise ErroHistorico(f"--provedor invalido: {pedido!r} (auto|falso|real)")
    if not nomes:
        return "falso", "nenhum papel de IA configurado"
    if sem:
        return "falso", f"sem chave de API para {', '.join(sem)}: provedor FALSO (rotulado)"
    return "real", f"chaves presentes para {', '.join(nomes)}"


def fim_do_corte(corte: str) -> dt.date:
    """'AAAA-MM' -> ultimo dia do mes; 'AAAA-MM-DD' -> o proprio dia."""
    p = [int(x) for x in corte.split("-")]
    if len(p) == 3:
        return dt.date(p[0], p[1], p[2])
    return dt.date(p[0], p[1], calendar.monthrange(p[0], p[1])[1])


def fronteira(catalogo: Catalogo, refs: Collection[str]) -> str:
    """Corte de conhecimento mais recente entre os LLMs usados (sem refs: todo o catalogo)."""
    usados = [r for r in refs if r in catalogo] or [f"{e.provedor}:{e.id}" for e in catalogo]
    return catalogo.fronteira_contaminacao(usados)


def contaminacao(d: dt.date, corte: str) -> str:
    """'limpo' se o pregao e POSTERIOR ao corte (o LLM nao pode conhecer o dia); senao 'contaminado'."""
    return "limpo" if d > fim_do_corte(corte) else "contaminado"


# ================================================================================================ dados
def mapa_de_datas(arm: ArmazenamentoTicks, perfil: PerfilInstrumento) -> dict[dt.date, str]:
    """data -> simbolo (contrato) a usar. Mais de um contrato no mesmo dia: o vigente pelo calendario do perfil."""
    por_data: dict[dt.date, list[str]] = {}
    for s in arm.simbolos():
        if s.startswith("SINT_") or not perfil.codigo_pertence(s):
            continue
        for d in arm.datas(s):
            por_data.setdefault(d, []).append(s)
    out: dict[dt.date, str] = {}
    for d, ss in sorted(por_data.items()):
        vig = contrato_vigente(perfil, d)
        out[d] = vig if vig in ss else sorted(ss)[0]
    return out


@dataclass
class DadosDia:
    data: dt.date
    simbolo: str
    ticks: list[Tick]
    n_armazem: int
    compactacao: EstatisticaCompactacao
    qualidade: dict[str, Any] | None
    segundos_carga: float


def carregar_dia(arm: ArmazenamentoTicks, simbolo: str, d: dt.date, compactar_ms: float) -> DadosDia:
    t0 = time.perf_counter()
    tab = arm.ler_tabela(simbolo, d)
    comp_tab, est = compactar_janelas(tab, compactar_ms)
    ticks = ticks_no_instante(comp_tab)
    return DadosDia(d, simbolo, ticks, len(tab), est, arm.ler_meta(simbolo, f"b3_negocios_{d.isoformat()}"),
                    time.perf_counter() - t0)


# ================================================================================================ um pregao
@dataclass
class ResultadoDia:
    data: dt.date
    simbolo: str
    contaminacao: str
    provedor: str
    resumo: dict[str, dict[str, Any]]
    trades: dict[str, list[Trade]]
    decisoes: list[dict[str, Any]]
    bloqueios: dict[str, dict[str, int]]
    eventos_risco: dict[str, int]
    latencias: dict[str, dict[str, float]]
    treino: dict[str, Any]
    maquina_do_tempo: dict[str, Any]
    dados: dict[str, Any]
    tempos_s: dict[str, float]
    banco: str
    latencias_brutas: dict[str, list[float]] = field(default_factory=dict, repr=False)  # para o consolidado

    def como_dict(self) -> dict[str, Any]:
        return {"data": self.data.isoformat(), "simbolo": self.simbolo, "contaminacao": self.contaminacao,
                "provedor": self.provedor, "resumo": self.resumo,
                "trades": {b: [_trade_dict(t) for t in ts] for b, ts in self.trades.items()},
                "bloqueios": self.bloqueios, "eventos_risco": self.eventos_risco, "latencias": self.latencias,
                "treino": self.treino, "maquina_do_tempo": self.maquina_do_tempo, "dados": self.dados,
                "tempos_s": self.tempos_s, "banco": self.banco}


def _trade_dict(t: Trade) -> dict[str, Any]:
    return {"lado": t.lado.value, "entrada": t.entrada, "saida": t.saida, "t_entrada_ns": t.t_entrada_ns,
            "t_saida_ns": t.t_saida_ns, "liquido_brl": t.liquido_brl, "motivo_saida": t.motivo_saida}


def quantis(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {}
    s = sorted(xs)

    def q(f: float) -> float:
        return round(float(s[min(len(s) - 1, int(f * (len(s) - 1) + 0.5))]), 1)
    return {"n": float(len(s)), "p50_ms": q(0.5), "p95_ms": q(0.95), "max_ms": round(float(s[-1]), 1)}


def _chave_motivo(m: str) -> str:
    m = m.strip()
    return m.split(":", 1)[1] if m.startswith("ia:") else m.split(":", 1)[0]


def simular_dia(p: ParamsHistorico, dados: DadosDia, *, perfil: PerfilInstrumento, cfg: dict[str, Any],
                catalogo: Catalogo, modelo: Any, piscina: mt.PiscinaSessoes | None, provedor: str, corte: str,
                info_treino: dict[str, Any] | None = None, saida_dia: Path | None = None) -> ResultadoDia:
    """Replay causal de UM pregao num orquestrador novo; devolve trades, decisoes, bloqueios e latencias."""
    if not dados.ticks:
        raise ErroHistorico(f"pregao {dados.data} sem ticks")
    t_ini = time.perf_counter()
    d = dados.data
    saida_dia = saida_dia or (Path(p.saida) / "dias" / d.isoformat())
    pp = ParamsSimulacao(
        seed=p.seed, saida=saida_dia, provedor=provedor, config=p.config, catalogo=p.catalogo, risco=p.risco,
        aprovacao=p.aprovacao, com_condicional=p.com_condicional, p_timeout=p.p_timeout, p_http=p.p_http,
        periodo_ia_s=p.periodo_ia_s, idade_max_dado_s=p.idade_max_dado_s, ativo=perfil.simbolo_base,
        tipo_modelo=p.tipo_modelo)
    rel = RelogioSimulado(dados.ticks[0].t_utc_ns)
    sessao_id = f"historico-{d.isoformat()}-{p.seed}"
    banco, registro, caminho, _ = abrir_banco(saida_dia, rel, sessao_id)
    try:
        venc = vencimento_do_codigo(perfil, dados.simbolo)
        m = construir_orquestrador(
            pp, rel=rel, registro=registro, ins=perfil.instrumento(dados.simbolo, vencimento=venc), modelo=modelo,
            statarb=None, precos_aux=None, sintetico=False, modo_tempo="simulado", sessao_id=sessao_id,
            t_ref_ns=rel.agora_ns(), cfg=cfg, catalogo=catalogo, piscina_odds=piscina, ids_bracos=p.bracos)
        manifesto = m.manifesto.model_copy(update={
            "origem_dados": "historico_real",
            "hashes": {**m.manifesto.hashes, "dados": f"{ORIGEM_B3}:{d.isoformat()}:{dados.simbolo}",
                       "contaminacao": f"{contaminacao(d, corte)}:corte={corte}"}})
        orq = m.orq
        registro.incidente(t_ns=rel.agora_ns(), origem="historico", severidade="info",
                           detalhe=f"{ROTULO_REAL_B3}. Pregao {d} ({contaminacao(d, corte)}; corte dos LLMs {corte}). "
                                   f"IA {provedor.upper()}.")

        async def rodar() -> None:
            orq.iniciar(manifesto, t_ns=dados.ticks[0].t_utc_ns)
            for tk in dados.ticks:
                await orq.on_tick(tk)
            await orq.encerrar(manifesto)

        asyncio.run(rodar())
        resumo = orq.resumo()
        trades = {b: list(orq.sim.carteira(b).trades) for b in orq.contas}
        decisoes = banco.consultar("SELECT t_ns, braco, acao, resultado, motivos, hash_dossie FROM decisoes "
                                   "ORDER BY t_ns, id")
        bloqueios: dict[str, Counter[str]] = {}
        for r in decisoes:
            if r["resultado"] in ("vetada", "bloqueada"):
                # "vetada" = veto do motor de risco; "bloqueada" = falha segura (IA, dado velho, frescor, kill...)
                pref = "risco" if r["resultado"] == "vetada" else "bloqueio"
                c = bloqueios.setdefault(r["braco"], Counter())
                motivos = [m for m in str(r["motivos"] or "").split(";")
                           if m.strip() and not m.startswith("rotulo:")] or ["sem_motivo"]  # rotulo nao e motivo
                for mot in motivos:
                    c[f"{pref}:{_chave_motivo(mot)}"] += 1
        riscos = Counter(r["tipo"] for r in banco.consultar("SELECT tipo FROM eventos_risco"))
        lats: dict[str, list[float]] = {}
        for r in banco.consultar("SELECT etapa, dur_ms FROM latencias WHERE dur_ms IS NOT NULL"):
            lats.setdefault(str(r["etapa"]), []).append(float(r["dur_ms"]))
    finally:
        banco.fechar()
    n_sess = len(piscina.anteriores_a(d)) if piscina is not None else 0
    return ResultadoDia(
        data=d, simbolo=dados.simbolo, contaminacao=contaminacao(d, corte), provedor=provedor, resumo=resumo,
        trades=trades, decisoes=decisoes, bloqueios={b: dict(c) for b, c in bloqueios.items()},
        eventos_risco=dict(riscos), latencias={e: quantis(v) for e, v in sorted(lats.items())},
        treino=info_treino or {},
        maquina_do_tempo={"sessoes_anteriores": n_sess, "minimo": mt.MIN_SESSOES,
                          "disponivel": n_sess >= mt.MIN_SESSOES},
        dados={"ticks_armazem": dados.n_armazem, "eventos_replay": len(dados.ticks),
               "compactar_ms": dados.compactacao.janela_ms, "fator_compactacao": round(dados.compactacao.fator, 3),
               "qualidade": dados.qualidade},
        tempos_s={"carga": round(dados.segundos_carga, 2), "replay": round(time.perf_counter() - t_ini, 2)},
        banco=str(caminho), latencias_brutas=lats)


# ================================================================================================ todos os pregoes
@dataclass
class ResultadoHistorico:
    params: ParamsHistorico
    ativo: str
    provedor: str
    motivo_provedor: str
    corte: str
    dias: list[ResultadoDia]
    pulados: dict[str, str]
    tempos_s: dict[str, float]


def executar_historico(p: ParamsHistorico, *, ao_progresso: Callable[[str], None] | None = None) -> ResultadoHistorico:
    """Walk-forward causal em todos os pregoes pedidos. Nao sobrescreve resultados (saida/dias/<D> novo)."""
    log = ao_progresso or (lambda _s: None)
    t_total = time.perf_counter()
    cfg, catalogo = carregar_ambiente(ParamsSimulacao(config=p.config, catalogo=p.catalogo))
    cfg = dict(cfg)
    if p.orcamento_usd is not None:
        cfg["ia"] = {**(cfg.get("ia") or {}), "orcamento_mensal_usd": float(p.orcamento_usd)}
    perfil = perfil_da_config(cfg, p.ativo, permitir_a_confirmar=True)
    raiz = Path(p.armazem)
    if not raiz.is_dir():
        raise ErroHistorico(f"armazem nao encontrado: {raiz}")
    arm = ArmazenamentoTicks(raiz)
    if arm.eh_sintetico:
        raise ErroHistorico(f"{raiz} e um armazem SINTETICO: `robowin historico` so roda sobre dados REAIS "
                            "(use `robowin simular`/`robowin backtest` para sinteticos)")
    if not arm.eh_real:
        raise ErroHistorico(f"{raiz} sem marcador de dados reais (REAL.txt): importe com `robowin dados importar-b3`")
    datas = mapa_de_datas(arm, perfil)
    if not datas:
        raise ErroHistorico(f"nenhum pregao de {perfil.simbolo_base} em {raiz}")
    todas = sorted(datas)
    alvo = todas[-p.dias:] if p.dias else todas
    refs = refs_ia(cfg)
    provedor, motivo = decidir_provedor(p.provedor, refs)
    corte = fronteira(catalogo, refs)
    log(f"historico {perfil.simbolo_base}: {len(alvo)} pregao(oes) simulado(s) de {len(todas)} no armazem; "
        f"IA {provedor.upper()} ({motivo}); corte dos LLMs {corte}")
    saida = Path(p.saida)
    saida.mkdir(parents=True, exist_ok=True)
    amostras: dict[dt.date, Amostras | None] = {}
    sessoes: list[mt.SessaoBarras] = []
    descartadas: dict[dt.date, str] = {}
    intervalo = mt.INTERVALO_PADRAO_S
    dias: list[ResultadoDia] = []
    pulados: dict[str, str] = {}
    # pregoes so de treino (anteriores ao 1o simulado): dias_treino para o modelo e ate 60 para a Maquina do Tempo
    janela_previa = max(p.dias_treino, 60)
    previos = [x for x in todas if x < alvo[0]][-janela_previa:]
    for d in previos + alvo:
        dados = carregar_dia(arm, datas[d], d, p.compactar_ms)
        if d in alvo:
            if not dados.ticks:
                pulados[d.isoformat()] = "sem ticks"
            else:
                t0 = time.perf_counter()
                modelo, info = treinar_para_dia(d, amostras, perfil, dias_treino=p.dias_treino, tipo=p.tipo_modelo,
                                                seed=p.seed)
                t_treino = time.perf_counter() - t0
                anteriores = tuple(s for s in sessoes if s.data < d)
                piscina = (mt.PiscinaSessoes(anteriores, {k: v for k, v in descartadas.items() if k < d}, intervalo,
                                             sintetico=False, origem=f"{ORIGEM_B3}:<{d.isoformat()}")
                           if anteriores else None)
                r = simular_dia(p, dados, perfil=perfil, cfg=cfg, catalogo=catalogo, modelo=modelo, piscina=piscina,
                                provedor=provedor, corte=corte, info_treino=info.como_dict())
                r.tempos_s["treino"] = round(t_treino, 2)
                dias.append(r)
                tot = {b: x["liquido_brl"] for b, x in r.resumo.items()}
                log(f"  {d} {dados.simbolo} [{r.contaminacao}] ticks {dados.n_armazem} -> eventos {len(dados.ticks)} "
                    f"| modelo {info.status} ({info.amostras} amostras de {len(info.dias_treino)} pregao(oes)) "
                    f"| tempos carga {r.tempos_s['carga']}s treino {r.tempos_s['treino']}s replay {r.tempos_s['replay']}s "
                    f"| liquido {tot}")
        # DEPOIS de simular D: o pregao D passa a poder ser usado pelos pregoes seguintes
        t0 = time.perf_counter()
        amostras[d] = amostras_do_dia(dados.ticks, perfil)
        pisc = mt.barras_de_ticks(dados.ticks, perfil, intervalo, sintetico=False, origem=ORIGEM_B3)
        sessoes.extend(pisc.sessoes)
        descartadas.update(pisc.descartadas)
        if dias and dias[-1].data == d:
            dias[-1].tempos_s["amostras"] = round(time.perf_counter() - t0, 2)
    tempos = {"total": round(time.perf_counter() - t_total, 2)}
    pico = memoria_pico_mb()
    if pico is not None:
        tempos["memoria_pico_mb"] = pico
    return ResultadoHistorico(p, perfil.simbolo_base, provedor, motivo, corte, dias, pulados, tempos)


def memoria_pico_mb() -> float | None:
    """Pico de memoria residente do processo (Linux/macOS); None onde nao ha `resource` (Windows)."""
    try:
        import resource
        import sys
    except ImportError:
        return None
    kb = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return round(kb / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def salvar_json(caminho: Path, obj: Any) -> None:
    from robowin.avaliacao.relatorio import para_json_seguro
    caminho.write_text(json.dumps(para_json_seguro(obj), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


__all__ = ["CONTROLE", "SELO", "ErroHistorico", "ParamsHistorico", "ResultadoDia", "ResultadoHistorico",
           "contaminacao", "decidir_provedor", "executar_historico", "params_de_config", "simular_dia"]
