"""Backtest: determinismo, replay por disponibilidade, zeragem, stop/gap, custos, banco, relatorio e CLI."""
from __future__ import annotations

import ast
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from robowin.avaliacao import relatorio
from robowin.avaliacao.cli_cmds import banco_sintetico, carregar_trades
from robowin.backtest import (
    ConfigBacktest,
    ControleAleatorio,
    DistribuicaoLatencia,
    FonteMemoria,
    MotorBacktest,
    NaoOperar,
    executar_cenarios,
    ordenar_por_disponibilidade,
    ordenar_por_evento,
)
from robowin.backtest.cli_cmds import CONTROLE
from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.tipos import Acao, Intencao, Tick
from robowin.dados.banco import BancoSQLite
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.ia.dossie import hash_dossie

MS, S = 1_000_000, 1_000_000_000
DIA = (2026, 3, 10)
RAIZ = Path(__file__).resolve().parents[2] / "src" / "robowin" / "backtest"


def ns(h: int, m: int = 0, s: int = 0) -> int:
    return dt_utc_ns(dt.datetime(*DIA, h, m, s))


def tk(t_ns: int, preco: float, recebido: int | None = None, spread: float = 5.0, vol: float = 1.0) -> Tick:
    r = t_ns + MS if recebido is None else recebido
    return Tick(t_servidor_ms=t_ns // MS, t_utc_ns=t_ns, t_recebido_ns=r, bid=preco, ask=preco + spread,
                last=preco, volume=vol, flags=32)


def serie(fn, de=(9, 0), ate=(18, 0), passo_s=1) -> list[Tick]:
    out, t = [], ns(*de)
    while t <= ns(*ate):
        out.append(tk(t, fn(t)))
        t += passo_s * S
    return out


class Roteiro:
    """Braco roteirizado (teste de infraestrutura): emite cada intencao no primeiro instante >= t_alvo."""

    def __init__(self, passos, id="roteiro"):
        self.id, self.passos, self._i, self.dossies = id, list(passos), 0, []

    def decidir(self, t_ns, dossie):
        self.dossies.append((t_ns, dossie))
        if self._i < len(self.passos) and t_ns >= self.passos[self._i][0]:
            _, acao, kw = self.passos[self._i]
            self._i += 1
            return Intencao(id=f"{self.id}-{self._i}", braco=self.id, acao=acao, validade_ns=t_ns + 60 * S, **kw)
        return None


def cfg(**kw) -> ConfigBacktest:
    base = dict(aquecimento_s=0.0, intervalo_decisao_s=1.0, latencia=DistribuicaoLatencia.fixa(100.0))
    base.update(kw)
    return ConfigBacktest(**base)


def rodar(ticks, passos, c=None, banco=None, livros=()):
    r = Roteiro(passos)
    res = MotorBacktest(FonteMemoria(ticks, livros), [r], c or cfg(), banco).executar()
    return res, r


COMPRA = dict(quantidade=1, stop_pontos=100.0)


# ============================================================================================== estaticos
def test_backtest_sem_metatrader5_order_send_nem_indicadores():
    rx = re.compile(r"(?<![a-z0-9])(rsi|macd|sma|ema|media_movel|bollinger|cruzamento)(?![a-z0-9])")
    for arq in RAIZ.glob("*.py"):
        txt = arq.read_text()
        for no in ast.walk(ast.parse(txt)):
            if isinstance(no, ast.Import):
                assert not any(a.name.split(".")[0] == "MetaTrader5" for a in no.names), arq
            if isinstance(no, ast.ImportFrom):
                assert (no.module or "").split(".")[0] != "MetaTrader5", arq
            if isinstance(no, ast.Call):
                f = no.func
                assert (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) != "order_send", arq
        for linha in txt.lower().splitlines():
            assert "proib" in linha or not rx.search(linha), (arq, linha)


# ============================================================================================== determinismo
def _sintetico(seed=5, n=3000):
    return gerar(ConfigSintetico(seed=seed, n_ticks=n))


def _rodar_sint(d, seed_braco=1, seed_motor=1, banco=None):
    bracos = [ControleAleatorio(seed_braco, p_entrada=0.2), NaoOperar()]
    return MotorBacktest(FonteMemoria(d.ticks, d.livros, sintetico=True), bracos,
                         ConfigBacktest(seed=seed_motor, intervalo_decisao_s=30.0), banco).executar()


def test_mesma_seed_gera_hash_identico_e_seed_diferente_gera_outro():
    d = _sintetico()
    a, b = _rodar_sint(d), _rodar_sint(d)
    assert a.hash_resultado() == b.hash_resultado()
    assert a.decisoes and sum(len(v) for v in a.trades.values()) > 0, "o cenario precisa operar para valer"
    assert _rodar_sint(d, seed_braco=2).hash_resultado() != a.hash_resultado()
    assert _rodar_sint(d, seed_motor=2).hash_resultado() != a.hash_resultado()


def test_hash_nao_depende_do_banco_nem_de_ids_globais():
    d = _sintetico()
    com_banco = _rodar_sint(d, banco=BancoSQLite(":memory:"))
    assert com_banco.hash_resultado() == _rodar_sint(d).hash_resultado()


def test_motor_e_de_uso_unico():
    m = MotorBacktest(FonteMemoria(serie(lambda t: 130000.0, ate=(9, 10))), [NaoOperar()], cfg())
    m.executar()
    with pytest.raises(RuntimeError):
        m.executar()


def test_nao_operar_registra_decisoes_e_nao_gera_ordens():
    res = MotorBacktest(FonteMemoria(serie(lambda t: 130000.0, ate=(10, 0))), [NaoOperar()],
                        cfg(intervalo_decisao_s=10.0)).executar()
    assert res.decisoes and {d["acao"] for d in res.decisoes} == {"NAO_OPERAR"}
    assert not res.ordens and not res.execucoes and not res.trades["nao_operar"]
    assert {e["equity_brl"] for e in res.equity} == {0.0}


# ============================================================================================== replay
def test_ordem_por_disponibilidade_difere_da_ordem_por_evento():
    base = [tk(ns(10, 0, i), 130000.0 + 5 * i) for i in range(0, 10)]
    atrasado = tk(ns(10, 0, 5) + 500 * MS, 131000.0, recebido=ns(10, 5))  # backfill: chegou 5 min depois
    ticks = sorted(base + [atrasado], key=lambda t: t.t_utc_ns)
    por_evento = [e.tick for e in ordenar_por_evento(ticks)]
    por_disp = [e.tick for e in ordenar_por_disponibilidade(ticks)]
    assert por_evento.index(atrasado) == 6 and por_disp.index(atrasado) == len(ticks) - 1
    assert [e.t_disp_ns for e in ordenar_por_disponibilidade(ticks)] == sorted(e.t_disp_ns for e in ordenar_por_disponibilidade(ticks))


class Gravador:
    id = "gravador"

    def __init__(self):
        self.h = {}

    def decidir(self, t_ns, dossie):
        self.h[t_ns] = hash_dossie(dossie)
        return None


def test_backfill_so_afeta_o_dossie_a_partir_do_instante_em_que_chegou():
    limpo = serie(lambda t: 130000.0 + 5 * ((t // S) % 7), ate=(10, 20))
    backfill = tk(ns(10, 0, 5) + 500 * MS, 132000.0, recebido=ns(10, 5))
    sujo = sorted(limpo + [backfill], key=lambda t: t.t_utc_ns)

    def hashes(ticks):
        g = Gravador()
        MotorBacktest(FonteMemoria(ticks), [g], cfg(intervalo_decisao_s=5.0)).executar()
        return g.h

    a, b = hashes(limpo), hashes(sujo)
    antes = [t for t in a if ns(9, 30) <= t < ns(10, 5)]
    depois = [t for t in a if ns(10, 5) <= t < ns(10, 8)]
    assert antes and depois
    assert all(a[t] == b[t] for t in antes), "o backfill apareceu ANTES de ter chegado (vazamento por t_evento)"
    assert any(a[t] != b[t] for t in depois), "o backfill nunca entrou apos chegar"


# ============================================================================================== zeragem
def test_zeragem_no_horario_e_sem_novas_entradas():
    ticks = serie(lambda t: 130000.0, ate=(18, 10))
    passos = [(ns(17, 29), Acao.COMPRAR, COMPRA), (ns(17, 50), Acao.COMPRAR, COMPRA)]
    res, _ = rodar(ticks, passos)
    trs = res.trades["roteiro"]
    assert len(trs) == 1 and trs[0].motivo_saida == "zeragem"
    assert ns(17, 45) <= trs[0].t_saida_ns <= ns(17, 45, 5)
    assert not res.posicoes_abertas_fim
    tardia = [d for d in res.decisoes if d["t_ns"] >= ns(17, 50)]
    assert tardia and "fora_da_janela_de_entradas" in tardia[0]["motivos"] and tardia[0]["resultado"] == "vetada"
    assert any(e["tipo"] == "zeragem_forcada" for e in res.eventos_risco)


def test_tempo_limite_encerra_a_posicao():
    ticks = serie(lambda t: 130000.0, ate=(10, 30))
    res, _ = rodar(ticks, [(ns(10, 0), Acao.COMPRAR, {**COMPRA, "tempo_max_s": 30})])
    (tr,) = res.trades["roteiro"]
    assert tr.motivo_saida == "tempo_limite" and 30 * S <= tr.t_saida_ns - tr.t_entrada_ns <= 33 * S


def test_posicao_aberta_no_fim_dos_dados_e_reportada_sem_liquidar():
    res, _ = rodar(serie(lambda t: 130000.0, ate=(10, 1)), [(ns(10, 0), Acao.COMPRAR, COMPRA)])
    assert res.posicoes_abertas_fim == {"roteiro": 1} and not res.trades["roteiro"]
    assert any(e["tipo"] == "posicao_aberta_fim_dos_dados" and e["critico"] == 1 for e in res.eventos_risco)


# ============================================================================================== stop e gap
def _preco_gap(t):
    return 130000.0 if t < ns(10, 0, 30) else 129500.0


def test_stop_atravessado_por_gap_executa_pior_que_o_stop_e_estresse_nao_melhora():
    ticks = serie(_preco_gap, ate=(10, 10))
    res, _ = rodar(ticks, [(ns(10, 0), Acao.COMPRAR, COMPRA)])
    (tr,) = res.trades["roteiro"]
    sl = 130005.0 - 100.0
    assert tr.motivo_saida == "stop" and tr.entrada == 130005.0
    assert tr.saida < sl - 300, "o gap deve ser executado ao preco do gap, nao ao nivel do stop"
    assert tr.liquido_brl < -(100 * 0.2) - 5, "a perda real excede a perda nominal do stop"
    est = executar_cenarios(FonteMemoria(ticks), lambda: [Roteiro([(ns(10, 0), Acao.COMPRAR, COMPRA)])], cfg(),
                            cenarios=("base", "estresse"))
    assert est["estresse"].trades["roteiro"][0].saida <= tr.saida
    assert est["estresse"].trades["roteiro"][0].liquido_brl < tr.liquido_brl


# ============================================================================================== custos
def _preco_alvo(t):
    return 130000.0 if t < ns(10, 0, 20) else 130100.0


def test_custos_base_e_estresse_no_alvo():
    ticks = serie(_preco_alvo, ate=(10, 5))
    passos = [(ns(10, 0), Acao.COMPRAR, {**COMPRA, "alvo_pontos": 50.0})]
    out = executar_cenarios(FonteMemoria(ticks), lambda: [Roteiro(passos)], cfg())
    base, est = out["base"].trades["roteiro"][0], out["estresse"].trades["roteiro"][0]
    assert base.motivo_saida == "alvo" and base.saida == 130055.0
    assert base.bruto_brl == pytest.approx(10.0)
    assert base.tarifas_brl == pytest.approx(0.5) and base.corretagem_brl == 0.0
    assert base.liquido_brl == pytest.approx(9.5)
    assert est.corretagem_brl == pytest.approx(0.22) and est.liquido_brl == pytest.approx(9.28)
    assert out["base"].cenario == "base" and out["estresse"].cenario == "estresse"


# ============================================================================================== feed
def test_lacuna_de_feed_e_registrada_e_veta_entrada_na_recuperacao():
    ticks = [t for t in serie(lambda t: 130000.0, ate=(10, 30)) if not ns(10, 0) <= t.t_utc_ns < ns(10, 10)]
    res, _ = rodar(ticks, [(ns(10, 9), Acao.COMPRAR, COMPRA)])
    gaps = [e for e in res.eventos_risco if e["tipo"] == "feed_gap"]
    assert len(gaps) == 1 and json.loads(gaps[0]["detalhe"])["silencio_s"] >= 600
    d = next(x for x in res.decisoes if x["acao"] == "COMPRAR")
    assert d["resultado"] == "vetada" and "feed_parado" in d["motivos"]


# ============================================================================================== latencia
def test_latencia_injetada_marcada_e_amostrada_da_distribuicao():
    amostras = [50.0, 80.0, 120.0, 400.0]
    c = cfg(latencia=DistribuicaoLatencia.de_amostras(amostras, origem="teste"))
    res, _ = rodar(serie(_preco_alvo, ate=(10, 5)), [(ns(10, 0), Acao.COMPRAR, {**COMPRA, "alvo_pontos": 50.0})], c)
    assert res.latencias and all(x["injetado"] == 1 and x["dur_ms"] in amostras for x in res.latencias)
    assert any(x["etapa"] == "execucao_ordem" for x in res.latencias)


# ============================================================================================== banco
def test_integracao_com_bancosqlite_conforme_esquema(tmp_path):
    caminho = tmp_path / "bt.db"
    d = _sintetico()
    with BancoSQLite(caminho) as banco:
        res = _rodar_sint(d, banco=banco)
        cont = {t: banco.consultar(f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in
                ("decisoes", "ordens", "execucoes", "trades", "equity", "eventos_risco", "latencias", "posicoes")}
        assert cont["decisoes"] == len(res.decisoes) and cont["execucoes"] == len(res.execucoes)
        assert cont["trades"] == sum(len(v) for v in res.trades.values()) > 0
        assert cont["equity"] == len(res.equity) and cont["latencias"] == len(res.latencias) > 0
        assert cont["eventos_risco"] == len(res.eventos_risco)
        assert banco.consultar("SELECT COUNT(*) c FROM latencias WHERE injetado=0")[0]["c"] == 0
        estados = {r["estado"] for r in banco.consultar("SELECT estado FROM ordens")}
        assert estados and not estados & {"NOVA", "ENVIADA"}
        assert {r["simulada"] for r in banco.consultar("SELECT simulada FROM ordens")} == {1}
        soma = banco.consultar("SELECT SUM(liquido_brl) s FROM trades")[0]["s"]
        assert soma == pytest.approx(sum(res.liquido_brl(b) for b in res.bracos))
        assert banco.consultar("SELECT status FROM experimentos")[0]["status"] == "backtest_base"
    lidos = carregar_trades(caminho)
    assert sum(len(v) for v in lidos.values()) == sum(len(v) for v in res.trades.values())
    assert {t.id for ts in lidos.values() for t in ts} == {t.id for ts in res.trades.values() for t in ts}
    assert banco_sintetico(caminho) is True


def test_banco_de_dados_nao_sinteticos_nao_e_marcado_sintetico(tmp_path):
    caminho = tmp_path / "real.db"
    with BancoSQLite(caminho) as banco:
        MotorBacktest(FonteMemoria(serie(lambda t: 130000.0, ate=(9, 30)), sintetico=False), [NaoOperar()],
                      cfg(), banco).executar()
    assert banco_sintetico(caminho) is False


# ============================================================================================== relatorio e CLI
def test_relatorio_recusa_selo_validado_em_dados_sinteticos():
    res = _rodar_sint(_sintetico())
    assert res.sintetico
    rel = relatorio.gerar_relatorio(res.trades_por_braco(), controle="controle_aleatorio",
                                    sintetico=res.sintetico, contaminado=False, tentativas=1, n_alvo=1, n_boot=200, seed=1)
    assert {b["selo"]["selo"] for b in rel["bracos"].values()} == {"recusado"}
    assert "SINTETICOS" in relatorio.renderizar_markdown(rel)


def test_cli_backtest_sinteticos_escreve_relatorios_e_recusa_selo(tmp_path, capsys):
    from robowin.cli import main
    saida = tmp_path / "saida"
    rc = main(["backtest", "--dados", "sinteticos", "--saida", str(saida), "--n-ticks", "3000",
               "--intervalo-s", "30", "--p-entrada", "0.2", "--estresse", "--n-boot", "100"])
    assert rc == 0
    for nome in ("relatorio.md", "relatorio.json", "resultado.json", "backtest.db", "relatorio_estresse.md",
                 "backtest_estresse.db"):
        assert (saida / nome).exists(), nome
    rel = json.loads((saida / "relatorio.json").read_text())
    assert rel["dados"]["sintetico"] is True and rel["controle"] == CONTROLE
    assert {b["selo"]["selo"] for b in rel["bracos"].values()} == {"recusado"}
    md = (saida / "relatorio.md").read_text()
    assert "SINTETICO" in md and "validado**" not in md.replace("nao_validado", "")
    out = capsys.readouterr().out
    assert "SINTETICO" in out and "hash=" in out
    h1 = json.loads((saida / "resultado.json").read_text())["hash_resultado"]
    main(["backtest", "--dados", "sinteticos", "--saida", str(tmp_path / "s2"), "--n-ticks", "3000",
          "--intervalo-s", "30", "--p-entrada", "0.2", "--n-boot", "100"])
    assert json.loads((tmp_path / "s2" / "resultado.json").read_text())["hash_resultado"] == h1
