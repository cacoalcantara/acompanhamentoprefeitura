import datetime as dt
import json
import sqlite3

import pytest

from robowin.avaliacao import impostos, relatorio
from robowin.avaliacao.cli_cmds import carregar_trades
from robowin.cli import main
from robowin.core.esquema_banco import DDL
from robowin.core.tipos import Lado, Trade

SP = dt.timezone(dt.timedelta(hours=-3))


def mk(i, liquido, dia="2026-11-03", hora=10, braco="A", slip=0.5, tarifa=0.5, corr=0.0):
    t = dt.datetime.fromisoformat(f"{dia}T{hora:02d}:{i % 60:02d}:00").replace(tzinfo=SP)
    ns = int(t.timestamp() * 1e9)
    return Trade(id=f"{braco}-{dia}-{i}", braco=braco, lado=Lado.COMPRA, entrada=100000, saida=100010,
                 t_entrada_ns=ns - 1_000_000_000, t_saida_ns=ns, bruto_brl=liquido + tarifa + corr + slip,
                 tarifas_brl=tarifa, corretagem_brl=corr, slippage_brl=slip, motivo_saida="alvo")


def test_imposto_mes_simples_com_irrf():
    ts = [mk(1, 600, dia="2026-11-03"), mk(2, 400, dia="2026-11-04")]
    (m,) = impostos.apurar(ts)
    assert m.mes == "2026-11" and m.resultado_liquido_brl == pytest.approx(1000)
    assert m.irrf_brl == pytest.approx(10)  # 1% de 1000 (ganhos diarios positivos)
    assert m.imposto_bruto_brl == pytest.approx(200)
    assert m.darf_brl == pytest.approx(190) and m.codigo_darf == "6015"
    assert m.vencimento == "2026-12-31"  # quinta-feira


def test_irrf_so_sobre_dias_positivos_e_imposto_sobre_saldo_mensal():
    ts = [mk(1, 1000, dia="2026-11-03"), mk(2, -400, dia="2026-11-04")]
    (m,) = impostos.apurar(ts)
    assert m.irrf_brl == pytest.approx(10)  # so o dia positivo
    assert m.base_calculo_brl == pytest.approx(600) and m.imposto_bruto_brl == pytest.approx(120)
    assert m.darf_brl == pytest.approx(110)


def test_compensacao_de_prejuizo_entre_meses():
    ts = [mk(1, -500, dia="2026-10-05"), mk(2, 800, dia="2026-11-03")]
    out = impostos.apurar(ts)
    assert out[0].darf_brl == 0 and out[0].prejuizo_a_compensar_brl == pytest.approx(500)
    assert out[1].prejuizo_compensado_brl == pytest.approx(500)
    assert out[1].base_calculo_brl == pytest.approx(300) and out[1].imposto_bruto_brl == pytest.approx(60)
    assert out[1].irrf_brl == pytest.approx(8) and out[1].darf_brl == pytest.approx(52)
    assert out[1].prejuizo_a_compensar_brl == 0


def test_irrf_excedente_e_darf_minimo_acumulam():
    # mes 1: dias positivos mas saldo negativo -> IRRF vira credito
    m1 = [mk(1, 1000, dia="2026-10-05"), mk(2, -1500, dia="2026-10-06")]
    # mes 2: ganho pequeno -> imposto abaixo de R$ 10 acumula
    m2 = [mk(3, 400, dia="2026-11-03")]
    a = impostos.apurar(m1 + m2)
    assert a[0].irrf_saldo_brl == pytest.approx(10) and a[0].prejuizo_a_compensar_brl == pytest.approx(500)
    assert a[1].base_calculo_brl == 0 and a[1].irrf_saldo_brl == pytest.approx(14)
    peq = impostos.apurar([mk(1, 40, dia="2026-11-03")])
    assert peq[0].imposto_a_pagar_brl == pytest.approx(7.6)
    assert peq[0].darf_brl == 0 and peq[0].darf_acumulado_brl == pytest.approx(7.6)


def test_aliquotas_em_configuracao_e_resultado_apos_ir():
    cfg = impostos.carregar_config({"impostos": {"aliquota_day_trade": 0.15}})
    assert cfg.aliquota_day_trade == 0.15 and cfg.aliquota_irrf == 0.01
    r = impostos.resultado_apos_ir([mk(1, 1000)], cfg)
    assert r["ir_estimado_brl"] == pytest.approx(150) and r["depois_ir_brl"] == pytest.approx(850)
    assert impostos.apurar([]) == []


# ----------------------------------------------------------- relatorio
def _trades(n, ganho=20, perda=-60, p=0.8, braco="B"):
    out = []
    for i in range(n):
        dia = f"2026-11-{3 + (i // 5) % 20:02d}"
        out.append(mk(i, ganho if (i % 10) < p * 10 else perda, dia=dia, hora=10 + (i % 5), braco=braco))
    return out


def test_relatorio_recusa_selo_em_dados_sinteticos():
    ts = {"B": _trades(400, ganho=30, perda=-10)}  # lucrativo de fato
    rel = relatorio.gerar_relatorio(ts, tentativas=1, sintetico=True, contaminado=False, n_alvo=300, n_boot=200)
    assert rel["bracos"]["B"]["selo"]["selo"] == "recusado"
    assert any("sinteticos" in m for m in rel["bracos"]["B"]["selo"]["motivos"])
    md = relatorio.renderizar_markdown(rel)
    assert "DADOS SINTETICOS" in md and "recusado" in md and "validado**" not in md.replace("nao_validado", "")
    rel2 = relatorio.gerar_relatorio(ts, tentativas=1, sintetico=False, contaminado=True, n_alvo=300, n_boot=200)
    assert rel2["bracos"]["B"]["selo"]["selo"] == "recusado"


def test_relatorio_concede_selo_so_com_todos_os_criterios():
    ts = {"B": _trades(400, ganho=30, perda=-10)}
    kw = {"sintetico": False, "contaminado": False, "tentativas": 1}
    rel = relatorio.gerar_relatorio(ts, n_alvo=300, n_boot=300, **kw)
    assert rel["bracos"]["B"]["selo"] == {"selo": "validado", "motivos": []}
    # amostra menor que o n pre-registrado
    rel = relatorio.gerar_relatorio(ts, n_alvo=1000, n_boot=100, **kw)
    b = rel["bracos"]["B"]
    assert b["selo"]["selo"] == "nao_validado" and b["amostra"]["suficiente"] is False
    assert "Amostra insuficiente" in relatorio.renderizar_markdown(rel)
    # sem n pre-registrado nunca valida
    assert relatorio.gerar_relatorio(ts, n_boot=100, **kw)["bracos"]["B"]["selo"]["selo"] == "nao_validado"


def test_selo_recusa_origem_desconhecida_poucos_pregoes_e_dsr_sem_variancia():
    ts = {"B": _trades(400, ganho=30, perda=-10)}
    # defaults seguros: origem/contaminacao nao afirmadas => recusado
    rel = relatorio.gerar_relatorio(ts, tentativas=1, n_alvo=300, n_boot=100)
    assert rel["bracos"]["B"]["selo"]["selo"] == "recusado"
    assert any("desconhecida" in m for m in rel["bracos"]["B"]["selo"]["motivos"])
    # tentativas > 1 sem variancia entre tentativas: sem selo
    rel = relatorio.gerar_relatorio(ts, tentativas=50, sintetico=False, contaminado=False, n_alvo=300, n_boot=100)
    assert rel["bracos"]["B"]["selo"]["selo"] == "nao_validado"
    assert any("variancia" in m for m in rel["bracos"]["B"]["selo"]["motivos"])
    # com a variancia o deflator existe (SR0 > 0)
    rel = relatorio.gerar_relatorio(ts, tentativas=50, var_sr_tentativas=0.01, sintetico=False, contaminado=False,
                                    n_alvo=300, n_boot=100)
    assert rel["bracos"]["B"]["dsr"]["sr0"] > 0
    # menos de 20 pregoes: sem selo
    poucos = {"B": [t for t in _trades(400, ganho=30, perda=-10) if t.id.split("-")[-2] <= "04"]}
    assert len({t.id.rsplit("-", 1)[0] for t in poucos["B"]}) < 20
    rel = relatorio.gerar_relatorio(poucos, tentativas=1, sintetico=False, contaminado=False, n_alvo=10, n_boot=100)
    assert rel["bracos"]["B"]["selo"]["selo"] == "nao_validado"
    assert any("pregoes" in m for m in rel["bracos"]["B"]["selo"]["motivos"])
    with pytest.raises(TypeError):
        relatorio.gerar_relatorio(ts)  # tentativas e obrigatorio


def test_aviso_de_acerto_vem_dos_parametros_efetivos():
    efetivo = relatorio.stop_efetivo_padrao_pontos()
    assert efetivo == 250.0  # (300+50)*0.2 = 70 > 60 => stop efetivo 250
    txt = relatorio.texto_aviso_acerto()
    assert "250 pontos" in txt and "300 pontos" not in txt and "76%" not in txt
    assert "NAO implica expectativa positiva" in relatorio.texto_aviso_acerto(stop_pontos=200, alvo_pontos=50)
    assert "200 pontos" in relatorio.texto_aviso_acerto(stop_pontos=200, alvo_pontos=50)


def test_acerto_alto_com_expectativa_negativa_e_aviso():
    ts = {"B": _trades(400, ganho=20, perda=-100, p=0.8)}  # 80% de acerto, expectativa negativa
    rel = relatorio.gerar_relatorio(ts, tentativas=1, sintetico=False, contaminado=False, n_alvo=300, n_boot=200)
    b = rel["bracos"]["B"]
    assert b["acerto"]["taxa"] == pytest.approx(0.8) and b["metricas"]["expectativa_brl"] < 0
    assert b["selo"]["selo"] == "nao_validado"
    assert "NAO implica expectativa positiva" in rel["aviso_acerto"]
    md = relatorio.renderizar_markdown(rel)
    assert "NAO implica expectativa positiva" in md and "IC de Wilson" in md and "400 operacoes" in md


def test_relatorio_com_controle_holm_e_json():
    ts = {"B": _trades(300, p=0.9), "CTRL": _trades(300, p=0.7, braco="CTRL")}
    rel = relatorio.gerar_relatorio(ts, tentativas=1, controle="CTRL", n_alvo=300, n_boot=100)
    c = rel["comparacoes_com_controle"]["B"]
    assert c["p_valor_braco_maior"] < 0.001 and "holm" in c
    md = relatorio.renderizar_markdown(rel)
    assert "Controle aleatorio" in md and "(controle aleatorio)" in md
    json.dumps(relatorio.para_json_seguro(rel))  # nao levanta mesmo com NaN/inf/tuplas


def test_relatorio_vazio():
    rel = relatorio.gerar_relatorio({"B": []}, tentativas=1, sintetico=False, contaminado=False, n_boot=10)
    assert rel["bracos"]["B"]["metricas"]["n"] == 0 and rel["bracos"]["B"]["selo"]["selo"] == "nao_validado"
    relatorio.renderizar_markdown(rel)


def _criar_banco(caminho, trades, manifesto=None):
    con = sqlite3.connect(caminho)
    for ddl in DDL:
        con.execute(ddl)
    for t in trades:
        con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.id, t.braco, t.lado.value, t.entrada, t.saida, t.t_entrada_ns, t.t_saida_ns, t.bruto_brl,
                     t.tarifas_brl, t.corretagem_brl, t.slippage_brl, t.liquido_brl, t.motivo_saida, "x"))
    if manifesto:
        con.execute("INSERT INTO experimentos VALUES ('e1', 0, ?, 'ativo')", (manifesto,))
    con.commit()
    con.close()


def test_cli_avaliar_banco_do_simular_nao_recebe_selo(tmp_path):
    """Banco gerado por `simular` (incidente dados_sinteticos, manifesto sem a palavra 'sinteti')."""
    from robowin.avaliacao.cli_cmds import banco_sintetico
    banco = tmp_path / "sim.db"
    _criar_banco(banco, _trades(400, ganho=30, perda=-10), manifesto='{"id": "sim-7", "n_alvo": 200}')
    con = sqlite3.connect(banco)
    con.execute("INSERT INTO incidentes(t_ns, origem, severidade, detalhe) VALUES (0, 'dados_sinteticos', 'info', 'x')")
    con.commit()
    con.close()
    assert banco_sintetico(banco)
    arq = tmp_path / "r.md"
    assert main(["avaliar", "--banco", str(banco), "--saida", str(arq), "--n-alvo", "10", "--tentativas", "1",
                 "--dados-reais", "--nao-contaminado", "--n-boot", "100"]) == 0
    js = json.loads(arq.with_suffix(".json").read_text(encoding="utf-8"))
    assert js["dados"]["sintetico"] is True and js["bracos"]["B"]["selo"]["selo"] == "recusado"


def test_banco_sintetico_por_origem_dados_no_manifesto_do_simular(tmp_path):
    from robowin.avaliacao.cli_cmds import banco_sintetico
    banco = tmp_path / "m.db"
    _criar_banco(banco, _trades(5), manifesto='{"origem_dados": "sinteticos"}')
    assert banco_sintetico(banco)


def test_cli_avaliar_sem_afirmar_origem_recusa_selo(tmp_path):
    banco = tmp_path / "r.db"
    _criar_banco(banco, _trades(400, ganho=30, perda=-10))
    arq = tmp_path / "r.md"
    assert main(["avaliar", "--banco", str(banco), "--saida", str(arq), "--n-alvo", "300", "--tentativas", "1",
                 "--n-boot", "100"]) == 0
    js = json.loads(arq.with_suffix(".json").read_text(encoding="utf-8"))
    assert js["dados"]["sintetico"] is None and js["bracos"]["B"]["selo"]["selo"] == "recusado"
    arq2 = tmp_path / "r2.md"
    assert main(["avaliar", "--banco", str(banco), "--saida", str(arq2), "--n-alvo", "300", "--tentativas", "1",
                 "--dados-reais", "--nao-contaminado", "--n-boot", "100"]) == 0
    js = json.loads(arq2.with_suffix(".json").read_text(encoding="utf-8"))
    assert js["bracos"]["B"]["selo"]["selo"] == "validado"


def test_cli_avaliar(tmp_path):
    ts = _trades(60)
    banco = tmp_path / "r.db"
    _criar_banco(banco, ts)
    assert len(carregar_trades(banco)["B"]) == 60
    saida = tmp_path / "out"
    assert main(["avaliar", "--banco", str(banco), "--saida", str(saida), "--n-alvo", "50", "--tentativas", "1", "--n-boot", "100"]) == 0
    assert "Braco B" in (saida / "relatorio.md").read_text(encoding="utf-8")
    assert json.loads((saida / "relatorio.json").read_text(encoding="utf-8"))["bracos"]["B"]["metricas"]["n"] == 60


def test_cli_detecta_banco_sintetico(tmp_path):
    banco = tmp_path / "s.db"
    _criar_banco(banco, _trades(40, ganho=30, perda=-10), manifesto='{"dados": "sinteticos"}')
    arq = tmp_path / "rel.md"
    assert main(["avaliar", "--banco", str(banco), "--saida", str(arq), "--n-alvo", "10", "--tentativas", "1", "--n-boot", "100"]) == 0
    js = json.loads(arq.with_suffix(".json").read_text(encoding="utf-8"))
    assert js["dados"]["sintetico"] and js["bracos"]["B"]["selo"]["selo"] == "recusado"


def test_segredos_do_mt5_e_do_painel_sao_redigidos(monkeypatch):
    from robowin.ia.segredos import redigir
    monkeypatch.setenv("ROBOWIN_MT5_SENHA", "senha-mt5-123")
    monkeypatch.setenv("ROBOWIN_TOKEN", "token-painel-456")
    out = redigir("erro: senha-mt5-123 e token-painel-456")
    assert "senha-mt5-123" not in out and "token-painel-456" not in out
