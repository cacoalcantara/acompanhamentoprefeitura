"""`robowin avaliar --banco ... --saida ...`"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from robowin.core.config import ErroConfig, carregar_toml
from robowin.core.instrumentos import perfil_da_config
from robowin.core.tipos import Lado, Trade

from . import metricas, relatorio, robustez, uso_ia
from . import monitoramento_banco as monit

ORIGEM_SINTETICO = "dados_sinteticos"  # mesma marca de decisao.registro e do painel

_SQL = ("SELECT id, braco, lado, entrada, saida, t_entrada_ns, t_saida_ns, bruto_brl, tarifas_brl, corretagem_brl, "
        "slippage_brl, motivo_saida FROM trades ORDER BY t_saida_ns, id")


def carregar_trades(banco: str | Path) -> dict[str, list[Trade]]:
    con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
    try:
        out: dict[str, list[Trade]] = {}
        for r in con.execute(_SQL):
            t = Trade(id=r[0], braco=r[1], lado=Lado(r[2]), entrada=r[3], saida=r[4], t_entrada_ns=r[5],
                      t_saida_ns=r[6], bruto_brl=r[7], tarifas_brl=r[8] or 0.0, corretagem_brl=r[9] or 0.0,
                      slippage_brl=r[10] or 0.0, motivo_saida=r[11] or "")
            out.setdefault(t.braco, []).append(t)
        return out
    finally:
        con.close()


def _origem_manifestos(banco: str | Path) -> set[str]:
    """Valores de `origem_dados` (e mencoes a 'sinteti') nos manifestos de experimento do banco."""
    con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
    out: set[str] = set()
    try:
        for (m,) in con.execute("SELECT manifesto_json FROM experimentos"):
            if not m:
                continue
            if "sinteti" in m.lower():
                out.add("sinteticos")
            try:
                v = json.loads(m)
            except ValueError:
                continue
            if isinstance(v, dict) and v.get("origem_dados"):
                out.add(str(v["origem_dados"]))
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def ativo_do_banco(banco: str | Path) -> str | None:
    """`ativo` gravado nos manifestos de experimento (None se ausente ou ambiguo)."""
    con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
    ativos: set[str] = set()
    try:
        for (m,) in con.execute("SELECT manifesto_json FROM experimentos"):
            try:
                v = json.loads(m or "")
            except ValueError:
                continue
            if isinstance(v, dict) and v.get("ativo"):
                ativos.add(str(v["ativo"]).upper())
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return next(iter(ativos)) if len(ativos) == 1 else None


def banco_sintetico(banco: str | Path) -> bool:
    """Sintetico se o manifesto declarar origem sintetica/mencionar 'sinteti' OU houver incidente
    de origem `dados_sinteticos` (a marca que o orquestrador/painel gravam)."""
    if any("sinteti" in o.lower() for o in _origem_manifestos(banco)):
        return True
    con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
    try:
        return con.execute("SELECT 1 FROM incidentes WHERE origem=? LIMIT 1", (ORIGEM_SINTETICO,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        con.close()


def origem_declarada_real(banco: str | Path) -> bool:
    """True so se algum manifesto declarar explicitamente `origem_dados = historico_real`."""
    return "historico_real" in _origem_manifestos(banco)


def _avaliar(a) -> int:
    if a.ticks and not a.simbolo:
        print("ERRO: --ticks exige --simbolo")
        return 2
    trades = carregar_trades(a.banco)
    # desconhecido (None) recusa o selo: so e "nao sintetico"/"nao contaminado" se AFIRMADO
    if a.sintetico or banco_sintetico(a.banco):
        sint: bool | None = True
    elif a.dados_reais or origem_declarada_real(a.banco):
        sint = False
    else:
        sint = None
    cont: bool | None = True if a.contaminado else (False if a.nao_contaminado else None)
    cfg_geral = carregar_toml(a.config) if Path(a.config).is_file() else {}
    ativo = a.ativo or ativo_do_banco(a.banco)  # --ativo > ativo gravado no manifesto do experimento > config
    try:
        perfil = perfil_da_config(cfg_geral, ativo, permitir_a_confirmar=True)
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    rel = relatorio.gerar_relatorio(trades, perfil=perfil, controle=a.controle, sintetico=sint, contaminado=cont,
                                    n_alvo=a.n_alvo, tentativas=a.tentativas, var_sr_tentativas=a.var_sr_tentativas,
                                    n_boot=a.n_boot, seed=a.seed)
    saida = Path(a.saida)
    if saida.suffix:
        md, js = saida, saida.with_suffix(".json")
    else:
        saida.mkdir(parents=True, exist_ok=True)
        md, js = saida / "relatorio.md", saida / "relatorio.json"
    md.parent.mkdir(parents=True, exist_ok=True)
    uso = uso_ia.resumir_uso(uso_ia.carregar_chamadas(a.banco))  # custo/latencia por braco e papel (debate: varias etapas)
    rel["uso_ia"] = uso
    # monitoramento (informativo; NUNCA veto): PSI de features, custo de execucao real x modelo, MinTRL, VaR/ES e
    # probabilidade de overfitting da escolha do melhor braco. PSI alto so grava incidente de aviso.
    extra = monit.series_de_ticks(a.ticks, a.simbolo) if a.ticks and a.simbolo else None
    mon = monit.montar_monitoramento(
        a.banco, tick_pontos=perfil.tick_pontos,
        liquidos_por_braco={b: [t.liquido_brl for t in metricas.ordenar(ts)] for b, ts in trades.items()},
        pnl_diario_por_braco={b: list(metricas.pnl_por_dia(ts).values()) for b, ts in trades.items()},
        series_extra=extra)
    rel["monitoramento"] = mon
    rel["cauda_perdas"] = mon["cauda"]
    rel["pbo_entre_bracos"] = robustez.pbo_entre_bracos(trades)
    if not a.sem_incidente and monit.registrar_incidente_deriva(a.banco, mon["psi"]):
        print(f"AVISO (informativo): PSI >= {monit.mon.PSI_ALERTA:.2f}; incidente "
              f"'{monit.ORIGEM_INCIDENTE_DERIVA}' gravado no banco (nada foi bloqueado)")
    md.write_text(relatorio.renderizar_markdown(rel) + uso_ia.renderizar_markdown(uso)
                  + monit.renderizar_markdown(mon, rel["pbo_entre_bracos"], moeda=perfil.moeda), encoding="utf-8")
    js.write_text(json.dumps(relatorio.para_json_seguro(rel), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"relatorio escrito em {md} e {js}")
    return 0


def registrar(sub):
    p = sub.add_parser("avaliar", help="gera o relatorio de avaliacao (Markdown e JSON) a partir do banco")
    p.add_argument("--banco", required=True)
    p.add_argument("--saida", required=True, help="arquivo .md ou diretorio")
    p.add_argument("--controle", default=None, help="nome do braco de controle aleatorio")
    p.add_argument("--n-alvo", type=int, default=None, help="n pre-registrado")
    p.add_argument("--tentativas", type=int, required=True,
                   help="numero TOTAL de tentativas/configuracoes testadas (deflator do DSR; 1 so se for mesmo uma)")
    p.add_argument("--var-sr-tentativas", type=float, default=None,
                   help="variancia dos SR entre as tentativas (obrigatoria para o selo se tentativas > 1)")
    p.add_argument("--sintetico", action="store_true")
    p.add_argument("--dados-reais", action="store_true",
                   help="afirma que os dados NAO sao sinteticos (sem isso a origem e desconhecida e o selo e recusado)")
    p.add_argument("--contaminado", action="store_true")
    p.add_argument("--nao-contaminado", action="store_true",
                   help="afirma que o periodo e posterior ao corte de conhecimento dos LLMs")
    p.add_argument("--n-boot", type=int, default=2000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--ativo", default=None, help="simbolo_base do perfil (padrao: o ativo do manifesto do banco)")
    p.add_argument("--config", default="config/robowin.toml")
    p.add_argument("--ticks", default=None,
                   help="armazem de ticks: acrescenta ao PSI o vetor de features recalculado (exige --simbolo)")
    p.add_argument("--simbolo", default=None, help="simbolo do armazem de ticks (com --ticks)")
    p.add_argument("--sem-incidente", action="store_true",
                   help="nao grava o incidente 'deriva_features' no banco (o relatorio continua mostrando o PSI)")
    p.set_defaults(func=_avaliar)
    from .cli_maquina import registrar_comando
    registrar_comando(sub)
