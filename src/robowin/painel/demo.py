"""Dados de DEMONSTRACAO sinteticos para popular o painel. Nunca representam mercado real.

O banco gerado e marcado com um incidente de origem `dados_sinteticos`, o que acende a faixa
"DADOS SINTÉTICOS" no painel.
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

from robowin.core.esquema_banco import DDL

from .dados import ORIGEM_SINTETICO

T0 = 1_760_000_000 * 10**9  # instante fixo e arbitrario (sintetico)
BRACOS = ("quant", "claude", "claude_gpt", "controle_aleatorio")


def popular(caminho: str | Path, semente: int = 42, n_trades: int = 40) -> Path:
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(semente)
    con = sqlite3.connect(caminho)
    for d in DDL:
        con.execute(d)
    con.execute("INSERT INTO incidentes(t_ns, origem, severidade, detalhe) VALUES (?,?,?,?)",
                (T0, ORIGEM_SINTETICO, "info", "Banco de demonstração com dados sintéticos; não é mercado real."))
    dia = "2099-01-02"
    for braco in BRACOS:
        eq = 100_000.0
        pico = eq
        for i in range(n_trades):
            ganho = rng.random() < 0.5
            liq = rng.uniform(10, 60) if ganho else -rng.uniform(10, 60)
            t = T0 + (i + 1) * 60 * 10**9
            con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"{braco}-{i}", braco, rng.choice(["COMPRA", "VENDA"]), 100000.0, 100000.0 + liq * 5,
                         t, t + 30 * 10**9, liq + 3, 1.0, 1.0, 1.0, liq, rng.choice(["alvo", "stop", "tempo"]), dia))
            eq += liq
            pico = max(pico, eq)
            con.execute("INSERT INTO equity VALUES (?,?,?,?)", (braco, t, eq, pico - eq))
            for etapa in ("dado_para_gatilho", "decisao", "risco", "ordem_para_fill"):
                con.execute("INSERT INTO latencias(t_ns,braco,etapa,dur_ms,injetado,modelo,effort) VALUES (?,?,?,?,?,?,?)",
                            (t, braco, etapa, rng.lognormvariate(3, 0.6), 0, "", ""))
    for braco in ("claude", "claude_gpt"):
        for i in range(3):
            h = f"dossie-{braco}-{i}".ljust(32, "0")
            t = T0 + i * 10**9
            proposta = {"acao": "COMPRAR", "justificativa_curta": "texto sintético de demonstração",
                        "incerteza": 0.6}
            con.execute("INSERT INTO decisoes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"dec-{braco}-{i}", t, braco, "imediato", "gatilho_sintetico", "COMPRAR", "{}",
                         json.dumps(proposta), json.dumps({"veredito": "APROVAR", "motivos": []}),
                         json.dumps({"aprovada": i != 2, "motivos": [] if i != 2 else ["limite diário"]}),
                         "executada" if i != 2 else "vetada", "", h))
            con.execute("INSERT INTO chamadas_llm(id,t_ns,braco,papel,modelo_pedido,modelo_servido,hash_dossie,"
                        "custo_usd,total_ms,valida) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (f"ch-{braco}-{i}", t, braco, "proponente", "modelo-sintetico", "modelo-sintetico", h,
                         0.05, 1500.0, 1))
        con.execute("INSERT INTO orcamento_ia VALUES (?,?,?,?)", (dia, braco, 0.45, 0.0))
    con.execute("INSERT INTO eventos_risco(t_ns,braco,tipo,detalhe,critico) VALUES (?,?,?,?,?)",
                (T0, "claude", "limite_diario", "evento sintético", 0))
    con.execute("INSERT INTO posicoes VALUES (?,?,?,?,?,?,?)", ("quant", T0, "COMPRA", 1, 100000.0, 99900.0, 100100.0))
    con.execute("INSERT INTO modelos VALUES (?,?,?,?,?,?,?,?)",
                ("microestrutura", "0.1", "nao_treinado", "0" * 64, "1" * 64, 0, "{}", 0))
    con.execute("INSERT INTO modelos VALUES (?,?,?,?,?,?,?,?)",
                ("statarb", "0.1", "em_validacao", "2" * 64, "3" * 64, T0, "{}", 0))
    man = {"id": "exp-demo", "hipoteses": ["H1 sintética"], "metrica_primaria": "expectativa_liquida", "mde_brl": 5.0,
           "alfa": 0.05, "n_alvo": 200, "regras_parada": ["n>=200"], "modelos": {"proponente": "sintetico"},
           "fixavel": {"proponente": False}, "hashes": {"config": "a" * 64}}
    con.execute("INSERT INTO experimentos VALUES (?,?,?,?)", ("exp-demo", T0, json.dumps(man), "em_andamento"))
    con.commit()
    con.close()
    return caminho
