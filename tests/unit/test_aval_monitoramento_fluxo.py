"""Monitoramento ligado ao fluxo: PSI de features lido do banco (com incidente de aviso), custo de execucao real x
modelo, MinTRL por braco, VaR/ES e a secao "Monitoramento" do `robowin avaliar`. Tudo informativo, nunca veto."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import math
import sqlite3
from pathlib import Path

import numpy as np

from robowin.avaliacao import monitoramento_banco as mb
from robowin.cli import main
from robowin.core.esquema_banco import DDL
from robowin.dados.ticks import dia_ns

NS = 1_000_000_000
TICK = 1.0  # tick arbitrario do banco de teste (o modulo trabalha em ticks; nada de ativo aqui)
DIAS = [dt.date(2030, 1, 7), dt.date(2030, 1, 8), dt.date(2030, 1, 9), dt.date(2030, 1, 10)]


def _banco(caminho: Path, *, deslocar: float = 0.0, dias=DIAS, n_por_dia: int = 120, treinado_ate_ns: int = 0,
           slippage_ticks: float = 0.0, n_entradas: int = 0, seed: int = 0) -> Path:
    """Banco com decisoes de um braco 'quant' (saida E[R$] ~ N(0,1); nas sessoes da 2a metade, N(deslocar,1))."""
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(caminho)
    for d in DDL:
        con.execute(d)
    k = 0
    preco = 1000.0
    for i, dia in enumerate(dias):
        t0 = dia_ns(dia) + 10 * 3600 * NS
        mu = deslocar if i >= len(dias) // 2 else 0.0
        for j in range(n_por_dia):
            t = t0 + j * 15 * NS
            preco += float(rng.integers(-2, 3)) * TICK
            it = {"id": f"q-{k}", "braco": "quant", "acao": "NAO_OPERAR", "modo": "quant", "preco_referencia": preco}
            pr = {"origem": "quant", "theta_brl": 1.0, "e_brl_compra": float(rng.normal(mu, 1.0))}
            con.execute("INSERT INTO decisoes(id, t_ns, braco, modo, resultado, intencao_json, proposta_json) "
                        "VALUES (?,?,?,?,?,?,?)", (f"d-{k}", t, "quant", "quant", "sem_acao", json.dumps(it),
                                                   json.dumps(pr)))
            k += 1
    for e in range(n_entradas):
        t = dia_ns(dias[0]) + 11 * 3600 * NS + e * 60 * NS
        lado = "compra" if e % 2 == 0 else "venda"
        ref = 2000.0 + e
        it = {"id": f"e-{e}", "braco": "quant", "acao": "COMPRAR", "modo": "quant", "preco_referencia": ref}
        con.execute("INSERT INTO decisoes(id, t_ns, braco, modo, resultado, intencao_json) VALUES (?,?,?,?,?,?)",
                    (f"de-{e}", t, "quant", "quant", "executada", json.dumps(it)))
        con.execute("INSERT INTO ordens(id, t_ns, braco, intencao_id, lado, tipo, quantidade, simulada) "
                    "VALUES (?,?,?,?,?,?,?,?)", (f"o-{e}", t, "quant", f"e-{e}", lado, "mercado", 1, 1))
        sinal = 1 if lado == "compra" else -1
        con.execute("INSERT INTO execucoes(id, ordem_id, t_ns, preco, quantidade, simulada) VALUES (?,?,?,?,?,?)",
                    (f"x-{e}", f"o-{e}", t + NS, ref + sinal * slippage_ticks * TICK, 1, 1))
    if treinado_ate_ns:
        con.execute("INSERT INTO modelos(nome, versao, status, treinado_ate_ns, ativo) VALUES (?,?,?,?,?)",
                    ("microestrutura", "1", "validado_oos", treinado_ate_ns, 1))
    con.commit()
    con.close()
    return caminho


# ================================================================================ PSI ==
def test_psi_do_banco_compara_primeiras_e_ultimas_sessoes_e_omite_constantes(tmp_path):
    b = _banco(tmp_path / "a.db", deslocar=2.0)
    psi = mb.deriva_features(mb.series_de_decisoes(mb.ler_decisoes(b)), mb.corte_de_treino_ns(b))
    assert psi["status"] == "ok" and "primeiras 2 sessoes x ultimas 2" in psi["criterio"]
    assert psi["niveis"]["quant.e_brl_compra"] == "alerta" and psi["pior"] == "alerta"
    assert psi["features_em_alerta"] == ["quant.e_brl_compra"]
    assert "quant.theta_brl" in psi["omitidas"]  # parametro constante: sem distribuicao, sem PSI
    assert psi["niveis"]["variacao_preco_referencia_pts"] == "estavel"  # mesmo processo de preco nas 4 sessoes
    assert psi["limiares"] == {"aviso": 0.10, "alerta": 0.25} and "nao altera o veto" in psi["rotulo"]


def test_psi_estavel_sem_deslocamento_e_amostra_insuficiente_com_uma_sessao(tmp_path):
    b = _banco(tmp_path / "a.db")
    psi = mb.deriva_features(mb.series_de_decisoes(mb.ler_decisoes(b)))
    assert psi["niveis"]["quant.e_brl_compra"] in ("estavel", "aviso") and not psi["features_em_alerta"]
    um = _banco(tmp_path / "b.db", dias=DIAS[:1])
    psi1 = mb.deriva_features(mb.series_de_decisoes(mb.ler_decisoes(um)))
    assert psi1["status"] == "amostra insuficiente" and not psi1["psi"]
    assert not mb.registrar_incidente_deriva(um, psi1)


def test_psi_usa_a_janela_de_treino_quando_registrada(tmp_path):
    corte = dia_ns(DIAS[2]) + 9 * 3600 * NS  # treinado ate antes da 3a sessao
    b = _banco(tmp_path / "a.db", deslocar=2.0, dias=DIAS + [dt.date(2030, 1, 11)], treinado_ate_ns=corte)
    psi = mb.deriva_features(mb.series_de_decisoes(mb.ler_decisoes(b)), mb.corte_de_treino_ns(b))
    assert psi["criterio"].startswith("janela de treino")
    assert psi["n"]["quant.e_brl_compra"] == {"referencia": 240, "atual": 360}


def test_incidente_de_deriva_e_aviso_idempotente_e_nao_bloqueia(tmp_path):
    b = _banco(tmp_path / "a.db", deslocar=2.0)
    psi = mb.deriva_features(mb.series_de_decisoes(mb.ler_decisoes(b)))
    assert mb.registrar_incidente_deriva(b, psi, t_ns=1)
    assert not mb.registrar_incidente_deriva(b, psi, t_ns=2)  # mesmo detalhe: nao duplica
    linhas = sqlite3.connect(b).execute("SELECT origem, severidade, detalhe FROM incidentes").fetchall()
    assert len(linhas) == 1 and linhas[0][:2] == ("deriva_features", "aviso")
    assert "quant.e_brl_compra" in linhas[0][2] and "nao altera o veto de risco" in linhas[0][2]
    assert "emergencia" not in linhas[0][2].lower()  # nunca confundido com confirmacao de kill switch no painel


# ===================================================================== custo de execucao ==
def test_custo_real_x_modelo_em_ticks_com_alerta_quando_muito_acima(tmp_path):
    b = _banco(tmp_path / "a.db", dias=DIAS[:1], n_por_dia=5, slippage_ticks=3.0, n_entradas=12)
    c = mb.custo_execucao(b, mb.ler_decisoes(b), TICK)
    assert c["status"] == "ok" and c["n"] == 12 and c["fonte"] == "simulador"
    assert math.isclose(c["real_ticks"], 3.0) and math.isclose(c["esperado_ticks"], 0.5)
    assert c["alerta"] and "otimista" in c["alerta"]
    assert c["por_braco"]["quant"]["n"] == 12


def test_custo_com_poucas_execucoes_e_amostra_insuficiente_e_sem_tick_e_indisponivel(tmp_path):
    b = _banco(tmp_path / "a.db", dias=DIAS[:1], n_por_dia=5, slippage_ticks=0.0, n_entradas=4)
    c = mb.custo_execucao(b, mb.ler_decisoes(b), TICK)
    assert c["status"] == "amostra insuficiente" and c["n"] == 4 and "alerta" not in c
    assert mb.custo_execucao(b, mb.ler_decisoes(b), None)["status"] == "indisponivel"


def test_custo_ignora_entrada_de_plano_condicional(tmp_path):
    b = _banco(tmp_path / "a.db", dias=DIAS[:1], n_por_dia=5, slippage_ticks=1.0, n_entradas=12)
    con = sqlite3.connect(b)
    for (did, it) in con.execute("SELECT id, intencao_json FROM decisoes WHERE resultado='executada'").fetchall():
        d = json.loads(it)
        d["modo"] = "condicional"
        con.execute("UPDATE decisoes SET intencao_json=? WHERE id=?", (json.dumps(d), did))
    con.commit()
    con.close()
    assert mb.custo_execucao(b, mb.ler_decisoes(b), TICK)["n"] == 0


# ============================================================================ MinTRL / VaR ==
def test_mintrl_por_braco_faltam_operacoes_e_amostra_insuficiente():
    rng = np.random.default_rng(3)
    poucos = mb.mintrl_braco([1.0, -1.0, 2.0])
    assert poucos["status"] == "amostra insuficiente"
    fraco = mb.mintrl_braco(list(rng.normal(0.1, 1.0, 60)))
    assert fraco["status"] in ("ok", "sem vantagem")
    if fraco["status"] == "ok":
        assert fraco["faltam"] == max(0, math.ceil(fraco["mintrl"]) - 60)
    forte = mb.mintrl_braco(list(rng.normal(1.0, 1.0, 200)))
    assert forte["status"] == "ok" and forte["significativo"] and forte["faltam"] == 0
    negativo = mb.mintrl_braco(list(rng.normal(-1.0, 1.0, 60)))
    assert negativo["status"] == "sem vantagem" and negativo["faltam"] is None


def test_cauda_marca_amostra_insuficiente_abaixo_do_minimo_de_dias():
    c = mb.cauda_braco([-10.0, 5.0, 3.0])
    assert c["amostra_insuficiente"] and c["dias"] == 3 and c["niveis"]
    assert not mb.cauda_braco(list(np.linspace(-50, 50, 80)))["amostra_insuficiente"]


# =============================================================================== avaliar ==
def _trades(caminho: Path, n: int = 40) -> None:
    con = sqlite3.connect(caminho)
    for i in range(n):
        t = dia_ns(DIAS[i % len(DIAS)]) + 12 * 3600 * NS + i * NS
        liq = 10.0 if i % 3 else -8.0
        con.execute("INSERT INTO trades(id, braco, lado, entrada, saida, t_entrada_ns, t_saida_ns, bruto_brl, "
                    "tarifas_brl, corretagem_brl, slippage_brl, liquido_brl, motivo_saida, dia) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"t{i}", "quant", "compra", 100.0, 100.0, t - NS, t, liq, 0.0, 0.0, 0.0, liq, "alvo",
                     DIAS[i % len(DIAS)].isoformat()))
    con.commit()
    con.close()


def _avaliar(b: Path, saida: Path, *extra: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return main(["avaliar", "--banco", str(b), "--saida", str(saida), "--tentativas", "1", "--n-boot", "100",
                     *extra])


def test_avaliar_tem_secao_monitoramento_e_grava_incidente_de_deriva(tmp_path):
    b = _banco(tmp_path / "a.db", deslocar=2.0, n_entradas=3)
    _trades(b)
    assert _avaliar(b, tmp_path / "rel") == 0
    md = (tmp_path / "rel" / "relatorio.md").read_text(encoding="utf-8")
    js = json.loads((tmp_path / "rel" / "relatorio.json").read_text(encoding="utf-8"))
    secao = md.split("## Monitoramento (informativo; nao altera o veto de risco)")[1]
    for titulo in ("Deriva de features (PSI)", "Custo de execucao real x modelo", "MinTRL por braco",
                   "VaR/ES diario historico", "PBO"):
        assert titulo in secao
    assert "0,10" in secao and "0,25" in secao and "quant.e_brl_compra" in secao
    assert "amostra insuficiente" in secao.lower()  # custo (3 entradas) e VaR (4 dias)
    assert "faltam" in secao or "ja significativo" in secao
    assert js["monitoramento"]["psi"]["pior"] == "alerta"
    assert js["monitoramento"]["mintrl"]["quant"]["n"] == 40
    inc = sqlite3.connect(b).execute("SELECT severidade FROM incidentes WHERE origem='deriva_features'").fetchall()
    assert inc == [("aviso",)]
    assert _avaliar(b, tmp_path / "rel2") == 0  # rodar de novo nao duplica o incidente
    assert sqlite3.connect(b).execute(
        "SELECT COUNT(*) FROM incidentes WHERE origem='deriva_features'").fetchone()[0] == 1


def test_avaliar_sem_incidente_nao_escreve_no_banco(tmp_path):
    b = _banco(tmp_path / "a.db", deslocar=2.0)
    _trades(b)
    assert _avaliar(b, tmp_path / "rel", "--sem-incidente") == 0
    assert not sqlite3.connect(b).execute("SELECT 1 FROM incidentes").fetchone()
    assert _avaliar(b, tmp_path / "rel", "--ticks", str(tmp_path)) == 2  # --ticks exige --simbolo
