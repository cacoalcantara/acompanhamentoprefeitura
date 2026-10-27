"""Coleta de ticks/livro: incremental, dedup por time_msc, backfill, log bruto (dados sinteticos)."""
from __future__ import annotations

import json

from test_mt5_comum import OFFSET_S, SIMB, T0_MS, T0_NS, montar_duble, ticks_planos

from robowin.core.barramento import BarramentoLocal
from robowin.mt5 import constantes as C
from robowin.mt5.coleta import ColetorLivro, ColetorTicks, LogBruto
from robowin.mt5.duble import gerar_ticks_sinteticos
from robowin.mt5.tempo_servidor import calibrar


def montar(ticks, tmp_path=None, **kw):
    d, rel = montar_duble(ticks)
    rs = calibrar(d, SIMB, rel)
    bus = BarramentoLocal()
    ev: list[dict] = []
    bus.assinar("tick", ev.append)
    log = LogBruto(tmp_path, rel) if tmp_path else None
    col = ColetorTicks(d, SIMB, rel, rs, bus, log, inicio_ns=T0_NS - 1_000_000_000, **kw)
    return d, rel, rs, bus, ev, col, log


def chave(t):
    return (t[0], t[1], t[2], t[3], t[4], t[5])


def test_incremental_sem_duplicatas_nem_perdas_mesmo_com_ticks_no_mesmo_ms():
    ticks = gerar_ticks_sinteticos(T0_MS - 1000, 1500, 50, semente=11, p_mesmo_ms=0.4)
    d, rel, rs, bus, ev, col, _ = montar(ticks)
    for _ in range(30):
        col.sondar()
        rel.avancar(500_000_000)
    col.sondar()
    visiveis = [t for t in ticks if t[0] <= rel.agora_ns() // 1_000_000]
    esperado = {chave(t) for t in visiveis}
    recebidos = [(e["t_servidor_ms"] - OFFSET_S * 1000, e["bid"], e["ask"], e["last"], e["volume"], e["flags"]) for e in ev]
    assert len(recebidos) == len(set(recebidos)) == len(esperado)  # sem duplicata, nenhum perdido
    assert set(recebidos) == {(t[0], t[1], t[2], t[3], t[4], t[5]) for t in visiveis}
    ms = [e["t_servidor_ms"] for e in ev]
    assert ms == sorted(ms)
    assert col.duplicados > 0  # a releitura da janela de 1 s foi deduplicada


def test_evento_grava_ms_bruto_e_utc_normalizado():
    d, rel, rs, bus, ev, col, _ = montar(ticks_planos())
    col.sondar()
    e = ev[-1]
    assert e["t_servidor_ms"] == e["t_utc_ns"] // 1_000_000 + OFFSET_S * 1000
    assert e["simbolo"] == SIMB and e["backfill"] is False and e["t_disponivel_ns"] == e["t_recebido_ns"] == rel.agora_ns()


def test_flags_buy_sell_preservadas():
    ticks = gerar_ticks_sinteticos(T0_MS - 1000, 400, 10, semente=2, p_negocio=0.7)
    d, rel, rs, bus, ev, col, _ = montar(ticks)
    col.sondar()
    fl = [e["flags"] for e in ev]
    assert any(f & C.FLAG_BUY for f in fl) and any(f & C.FLAG_SELL for f in fl)


def test_backfill_na_reconexao_sem_duplicar_nem_perder():
    ticks = ticks_planos(600)
    d, rel, rs, bus, ev, col, _ = montar(ticks)
    col.sondar()
    n_antes = len(ev)
    assert n_antes > 0
    d.desconectar()
    rel.avancar(5_000_000_000)
    assert col.sondar() == 0 and col.lacuna_pendente and col.falhas_consecutivas >= 1 and len(ev) == n_antes
    rel.avancar(5_000_000_000)
    d.reconectar()
    n = col.sondar()
    assert n > 40
    nb = [e for e in ev if e["backfill"]]
    assert len(nb) > 40 and all(e["t_disponivel_ns"] == rel.agora_ns() for e in nb)
    assert all(e["t_utc_ns"] <= e["t_disponivel_ns"] for e in nb)
    assert sum(e["t_utc_ns"] < e["t_disponivel_ns"] for e in nb) > 40  # disponivel DEPOIS do evento
    assert col.backfills and col.backfills[0]["n"] == len(nb)
    visiveis = [t for t in ticks if t[0] <= rel.agora_ns() // 1_000_000]
    assert len(ev) == len({chave(t) for t in visiveis})
    ms = [e["t_servidor_ms"] for e in ev]
    assert ms == sorted(ms) and not col.lacuna_pendente


def test_backfill_tenta_de_novo_se_terminal_ainda_fora():
    d, rel, rs, bus, ev, col, _ = montar(ticks_planos(600))
    col.sondar()
    d.desconectar()
    col.sondar()
    rel.avancar(2_000_000_000)
    assert col.sondar() == 0 and col.lacuna_pendente
    d.reconectar()
    assert col.sondar() > 0 and not col.lacuna_pendente


def test_feed_parado():
    d, rel, rs, bus, ev, col, _ = montar(ticks_planos(100), limite_feed_parado_s=5.0)
    col.sondar()
    assert not col.feed_parado
    rel.avancar(20_000_000_000)  # a serie (10 s) acaba de ser consumida
    col.sondar()
    assert not col.feed_parado
    rel.avancar(30_000_000_000)
    col.sondar()
    assert col.feed_parado


def test_lote_pequeno_drena_tudo_em_uma_sondagem():
    ticks = gerar_ticks_sinteticos(T0_MS - 1000, 300, 10, semente=4, p_mesmo_ms=0.0)
    d, rel, rs, bus, ev, col, _ = montar(ticks, lote=40)
    rel.avancar(10_000_000_000)
    col.sondar()
    assert len(ev) == len({chave(t) for t in ticks})


def test_livro_registra_profundidade_e_nao_republica_igual(tmp_path):
    d, rel = montar_duble(ticks_planos())
    bus = BarramentoLocal()
    ev = []
    bus.assinar("livro", ev.append)
    log = LogBruto(tmp_path, rel)
    lv = ColetorLivro(d, SIMB, rel, bus, log)
    assert lv.sondar() is None  # nao assinado
    assert lv.assinar() and lv.profundidade_declarada == 10
    snap = lv.sondar()
    assert snap is not None and lv.profundidade_observada == 5
    assert {n.lado for n in snap.niveis} == {"compra", "venda"}
    precos = [(n.lado, n.preco) for n in snap.niveis]
    assert len(precos) == len(set(precos))  # ja agregado
    assert lv.sondar() is None  # igual: nao republica
    rel.avancar(2_000_000_000)
    lv.sondar()
    assert len(ev) >= 1 and ev[0]["simbolo"] == SIMB
    lv.liberar()
    log.fechar()
    arq = list(tmp_path.rglob(f"{SIMB}.livro.jsonl"))
    assert arq and len(arq[0].read_text().splitlines()) == len(ev)


def test_livro_sem_profundidade_nao_assina():
    d, rel = montar_duble(ticks_planos())
    d.especificacoes[SIMB].ticks_bookdepth = 0
    lv = ColetorLivro(d, SIMB, rel, BarramentoLocal())
    assert lv.assinar() is False and lv.profundidade_declarada == 0


def test_log_bruto_em_disco_tem_um_registro_por_tick(tmp_path):
    d, rel, rs, bus, ev, col, log = montar(ticks_planos(), tmp_path)
    rel.avancar(3_000_000_000)
    col.sondar()
    log.fechar()
    arq = list(tmp_path.rglob(f"{SIMB}.ticks.jsonl"))
    assert len(arq) == 1
    linhas = [json.loads(x) for x in arq[0].read_text().splitlines()]
    assert len(linhas) == len(ev) > 0
    assert linhas[0]["time_msc"] == ev[0]["t_servidor_ms"] and "t_rec_ns" in linhas[0] and "bid" in linhas[0]
