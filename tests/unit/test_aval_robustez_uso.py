"""PBO (CSCV), MinTRL, contabilidade de uso de IA e a integracao com `robowin avaliar`."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from robowin.avaliacao import uso_ia
from robowin.avaliacao.robustez import min_track_record_length, pbo_cscv, pbo_entre_bracos
from robowin.cli import main
from robowin.core.tipos import Lado, Trade

NS = 1_000_000_000


# ================================================================================ PBO ==
def test_pbo_baixo_quando_um_braco_e_realmente_melhor_em_todos_os_periodos():
    rng = np.random.default_rng(0)
    m = rng.normal(0, 1, (200, 6))
    m[:, 2] += 0.8  # braco 2 tem edge verdadeiro e estavel
    r = pbo_cscv(m, 8)
    assert r["pbo"] is not None and r["pbo"] < 0.15 and r["n_combinacoes"] == 70


def test_pbo_alto_quando_o_vencedor_da_amostra_e_so_ruido():
    rng = np.random.default_rng(1)
    m = rng.normal(0, 1, (160, 30))  # 30 bracos sem edge nenhum: o melhor na amostra e sorte
    r = pbo_cscv(m, 8)
    assert 0.35 <= r["pbo"] <= 0.75 and "ruidosa" in r["aviso"]


def test_pbo_recusa_amostra_inadequada_em_vez_de_inventar_numero():
    assert pbo_cscv(np.zeros((100, 1)), 8)["pbo"] is None
    assert pbo_cscv(np.zeros((10, 3)), 8)["pbo"] is None
    with pytest.raises(ValueError):
        pbo_cscv(np.zeros((100, 3)), 7)
    with pytest.raises(ValueError):
        pbo_cscv(np.full((100, 3), np.nan), 8)
    with pytest.raises(ValueError):
        pbo_cscv(np.zeros(5), 8)


def _trade(braco: str, dia: int, liquido: float) -> Trade:
    t = (1_773_000_000 + dia * 86_400) * NS
    return Trade(id=f"{braco}-{dia}", braco=braco, lado=Lado.COMPRA, entrada=100.0, saida=100.0 + liquido,
                 t_entrada_ns=t, t_saida_ns=t + 60 * NS, bruto_brl=liquido, tarifas_brl=0.0, corretagem_brl=0.0,
                 slippage_brl=0.0, motivo_saida="alvo")


def test_pbo_entre_bracos_usa_pnl_por_pregao_e_ignora_braco_sem_operacoes():
    rng = np.random.default_rng(2)
    tr = {b: [_trade(b, d, float(rng.normal(0, 10))) for d in range(40)] for b in ("a", "b", "c")}
    tr["vazio"] = []
    r = pbo_entre_bracos(tr, 8)
    assert r["bracos"] == ["a", "b", "c"] and r["pbo"] is not None and r["n_periodos"] == 40
    assert pbo_entre_bracos({"a": tr["a"]})["pbo"] is None


# ================================================================================ MinTRL ==
def test_min_track_record_length_cresce_quando_o_sharpe_cai_e_infinito_sem_vantagem():
    a = min_track_record_length(0.2, 0.0)
    b = min_track_record_length(0.1, 0.0)
    assert 1 < a < b and b == pytest.approx(4 * a, rel=0.1)
    assert math.isinf(min_track_record_length(0.0)) and math.isinf(min_track_record_length(0.1, 0.2))
    assert min_track_record_length(0.2, 0.0, assimetria=-1.0, curtose=6.0) > a  # cauda gorda exige mais dados
    with pytest.raises(ValueError):
        min_track_record_length(0.2, confianca=0.4)


# ================================================================================ uso de IA ==
LINHAS = [
    {"braco": "b", "papel": "proponente", "total_ms": 1000.0, "custo_usd": 0.10, "valida": 1,
     "tokens_entrada": 100, "tokens_saida": 50, "tokens_cache": 10},
    {"braco": "b", "papel": "proponente", "total_ms": 3000.0, "custo_usd": 0.30, "valida": 0,
     "tokens_entrada": 100, "tokens_saida": 0, "tokens_cache": 0},
    {"braco": "debate", "papel": "debate_altista", "total_ms": 500.0, "custo_usd": 0.01, "valida": 1,
     "tokens_entrada": 10, "tokens_saida": 5, "tokens_cache": 0},
    {"braco": "debate", "papel": "proponente", "total_ms": 900.0, "custo_usd": 0.20, "valida": 1,
     "tokens_entrada": 10, "tokens_saida": 5, "tokens_cache": 0},
]


def test_resumir_uso_agrega_por_braco_e_papel_com_taxa_de_invalidas_e_latencia():
    u = uso_ia.resumir_uso(LINHAS)
    assert u["total"]["chamadas"] == 4 and u["total"]["invalidas"] == 1
    assert u["total"]["custo_usd"] == pytest.approx(0.61)
    b = u["por_braco"]["b"]
    assert b["chamadas"] == 2 and b["taxa_invalidas"] == 0.5 and b["latencia_ms"]["p50"] == 2000.0
    assert set(u["por_braco"]["debate"]["papeis"]) == {"debate_altista", "proponente"}
    assert u["por_braco"]["debate"]["custo_usd"] == pytest.approx(0.21)
    md = uso_ia.renderizar_markdown(u)
    assert "Uso de IA por braco e papel" in md and "debate_altista" in md and "0.6100" in md
    assert uso_ia.renderizar_markdown(uso_ia.resumir_uso([])) == ""


def test_carregar_chamadas_le_o_banco_e_tolera_banco_sem_a_tabela(tmp_path):
    p = tmp_path / "x.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE chamadas_llm (braco TEXT, papel TEXT, total_ms REAL, custo_usd REAL, valida INTEGER)")
    con.execute("INSERT INTO chamadas_llm VALUES ('b','proponente',10,0.5,1)")
    con.commit()
    con.close()
    (x,) = uso_ia.carregar_chamadas(p)
    assert x["braco"] == "b" and x["custo_usd"] == 0.5 and x["tokens_entrada"] == 0  # coluna ausente => 0
    q = tmp_path / "y.db"
    sqlite3.connect(q).close()
    assert uso_ia.carregar_chamadas(q) == []


def test_avaliar_anexa_uso_de_ia_cauda_e_pbo_ao_relatorio(tmp_path):
    saida = tmp_path / "sim"
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["simular", "--n-ticks", "3000", "--saida", str(saida), "--seed", "11"]) == 0
        rc = main(["avaliar", "--banco", str(saida / "robowin.db"), "--saida", str(tmp_path / "rel"),
                   "--tentativas", "1", "--n-boot", "200"])
    assert rc == 0
    md = (tmp_path / "rel" / "relatorio.md").read_text(encoding="utf-8")
    js = json.loads((tmp_path / "rel" / "relatorio.json").read_text(encoding="utf-8"))
    assert "Uso de IA por braco e papel" in md and "Monitoramento (informativo; nao altera o veto de risco)" in md
    assert "VaR/ES diario historico" in md and "PBO" in md
    assert js["uso_ia"]["total"]["chamadas"] > 0 and "claude" in js["uso_ia"]["por_braco"]
    assert "pbo_entre_bracos" in js and js["cauda_perdas"]
    assert js["monitoramento"]["cauda"] == js["cauda_perdas"]
    assert "nao sao veto" in md
    assert Path(saida / "robowin.db").exists()
