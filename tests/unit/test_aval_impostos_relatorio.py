import datetime as dt
import json
import sqlite3

import pytest
from perfis_teste import WIN

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
    efetivo = relatorio.stop_efetivo_padrao_pontos(WIN)
    assert efetivo == 250.0  # (300+50)*0.2 = 70 > 60 => stop efetivo 250
    txt = relatorio.texto_aviso_acerto(WIN)
    assert "250 pontos" in txt and "300 pontos" not in txt and "76%" not in txt
    assert "NAO implica expectativa positiva" in relatorio.texto_aviso_acerto(WIN, stop_pontos=200, alvo_pontos=50)
    assert "200 pontos" in relatorio.texto_aviso_acerto(WIN, stop_pontos=200, alvo_pontos=50)
    # C1: sem geometria realizada o aviso diz que e o TETO, nao a geometria usada; com geometria, nao
    assert relatorio.ROTULO_TETO in txt
    assert relatorio.ROTULO_TETO not in relatorio.texto_aviso_acerto(WIN, stop_pontos=200, alvo_pontos=50)


def test_aviso_por_braco_usa_a_geometria_realizada_e_cai_no_teto_rotulado():
    geo = relatorio.geometria_mediana([(30, 30), (30, 30), (40, 20), (None, 30), (0, 10), (float("nan"), 5)])
    assert geo == (30.0, 30.0)  # medianas so dos pares validos
    assert relatorio.geometria_mediana([(None, None)]) is None
    av = relatorio.texto_aviso_acerto_por_braco(WIN, {"quant": (30.0, 30.0), "falso": (30.0, 40.0)},
                                                bracos=["quant", "falso", "nao_operar"])
    assert set(av) == {"quant", "falso", "nao_operar"}
    assert "stop 30 / alvo 30" in av["quant"] and "62%" in av["quant"] and "73%" in av["quant"]
    assert "stop 30 / alvo 40" in av["falso"] and "54%" in av["falso"]
    assert relatorio.ROTULO_TETO in av["nao_operar"] and relatorio.ROTULO_TETO not in av["quant"]
    assert "92%" not in av["quant"]  # a combinacao teto 250 + alvo 30 nao e atribuida a quem nao a usou
    ts = {"quant": _trades(60, braco="quant"), "nao_operar": []}
    rel = relatorio.gerar_relatorio(ts, tentativas=1, sintetico=False, contaminado=False, n_boot=50, perfil=WIN,
                                    geometrias={"quant": (30.0, 30.0)})
    md = relatorio.renderizar_markdown(rel)
    assert "Geometria realizada" in md and relatorio.ROTULO_TETO in rel["avisos_acerto_por_braco"]["nao_operar"]
    # sem geometrias o relatorio e o de antes (nenhuma secao nova)
    rel0 = relatorio.gerar_relatorio(ts, tentativas=1, sintetico=False, contaminado=False, n_boot=50, perfil=WIN)
    assert "avisos_acerto_por_braco" not in rel0 and "injetados" not in rel0 and "gestao" not in rel0


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


# ================================================================== docs/16 (WP8): selo com pre-registro e secoes
NS = 1_000_000_000


def _registro(tmp_path, dia_registro: str, variantes=None):
    from robowin.avaliacao import preregistro as prr
    from robowin.core.tipos import PreRegistro
    t = int(dt.datetime.fromisoformat(f"{dia_registro}T12:00:00").replace(tzinfo=SP).timestamp()) * NS
    pr = PreRegistro(id_experimento="exp-wp8", t_registro_ns=t, inicio_avaliacao=dia_registro,
                     variantes=variantes or {"B": "hB"}, comparacoes_primarias=("B-C",),
                     margens_nao_inferioridade={"pior_dia": 1.0}, n_pregoes_alvo=10)
    return prr.registrar(pr, tmp_path / "exp"), tmp_path / "exp"


def _lucrativo():
    return {"B": _trades(400, ganho=30, perda=-10)}  # 2026-11-03 .. 2026-11-22


def test_selo_recusa_pregao_anterior_ou_igual_ao_registro(tmp_path):
    pr, pasta = _registro(tmp_path, "2026-11-10")
    rel = relatorio.gerar_relatorio(_lucrativo(), tentativas=1, sintetico=False, contaminado=False, n_alvo=300,
                                    n_boot=100, preregistro=pr, dir_preregistro=pasta, hashes_variantes={"B": "hB"})
    s = rel["bracos"]["B"]["selo"]
    assert s["selo"] == "recusado"
    assert any("pregao_nao_posterior_ao_registro" in m for m in s["motivos"])
    # registro ANTES de todos os pregoes, hashes iguais: o pre-registro nao recusa (o resto dos criterios decide)
    pr2, pasta2 = _registro(tmp_path / "b", "2026-10-29")
    ok = relatorio.gerar_relatorio(_lucrativo(), tentativas=1, sintetico=False, contaminado=False, n_alvo=300,
                                   n_boot=100, preregistro=pr2, dir_preregistro=pasta2, hashes_variantes={"B": "hB"})
    assert ok["bracos"]["B"]["selo"]["selo"] != "recusado"
    assert not any("pre-registro" in m for m in ok["bracos"]["B"]["selo"]["motivos"])
    # hash de variante divergente: recusado
    div = relatorio.gerar_relatorio(_lucrativo(), tentativas=1, sintetico=False, contaminado=False, n_alvo=300,
                                    n_boot=100, preregistro=pr2, dir_preregistro=pasta2, hashes_variantes={"B": "outro"})
    assert div["bracos"]["B"]["selo"]["selo"] == "recusado"
    assert any("hash_divergente:B" in m for m in div["bracos"]["B"]["selo"]["motivos"])


def test_selo_recusa_provedor_falso_e_cadeia_quebrada_e_conta_tentativas_da_cadeia(tmp_path):
    pr, pasta = _registro(tmp_path, "2026-10-29", variantes={"B": "hB", "C": "hC", "P": "hP"})
    rel = relatorio.gerar_relatorio(_lucrativo(), tentativas=1, sintetico=False, contaminado=False, n_alvo=300,
                                    n_boot=100, modelos_servidos={"B": ["modelo-x-FALSO"]})
    assert rel["bracos"]["B"]["selo"]["selo"] == "recusado"
    assert any("provedor FALSO" in m for m in rel["bracos"]["B"]["selo"]["motivos"])
    # tentativas do DSR = variantes registradas na cadeia (3), nunca menos que o informado
    rel = relatorio.gerar_relatorio(_lucrativo(), tentativas=1, sintetico=False, contaminado=False, n_alvo=300,
                                    n_boot=100, var_sr_tentativas=0.01, preregistro=pr, dir_preregistro=pasta,
                                    hashes_variantes=pr.variantes)
    assert rel["tentativas"] == 3 and rel["bracos"]["B"]["dsr"]["n_tentativas"] == 3
    linhas = (pasta / "registro.jsonl").read_text(encoding="utf-8").splitlines()
    (pasta / "registro.jsonl").write_text("\n".join([linhas[0].replace("hB", "hX")] + linhas[1:]) + "\n",
                                          encoding="utf-8")
    rel = relatorio.gerar_relatorio(_lucrativo(), tentativas=1, sintetico=False, contaminado=False, n_alvo=300,
                                    n_boot=100, preregistro=pr, dir_preregistro=pasta, hashes_variantes=pr.variantes)
    assert rel["bracos"]["B"]["selo"]["selo"] == "recusado"
    assert any("cadeia de pre-registro invalida" in m for m in rel["bracos"]["B"]["selo"]["motivos"])
    assert relatorio.decidir_selo(sintetico=False, contaminado=False, n=500, n_alvo=10, exp_ic_inf=1.0,
                                  fator_lucro=2.0, dsr=0.99, pregoes=30, provedor_falso=True)["selo"] == "recusado"


def test_secao_injetados_separa_falhas_do_falso_e_nunca_toca_bracos_sem_ia():
    motivos = {"claude_gpt": {"bloqueio:resposta_tardia": 3, "bloqueio:erro_http": 1,
                              "bloqueio:divergencia_revisor": 2, "risco:perdas_seguidas_maximo": 4},
               "quant": {"bloqueio:resposta_tardia": 1, "risco:limite_diario": 1}}
    s = relatorio.secao_injetados(motivos, {"claude_gpt": ["claude-x-FALSO", "gpt-y-FALSO"]})
    assert s["falsos"] == ["claude_gpt"] and s["total"] == 6
    assert s["bracos"] == {"claude_gpt": {"bloqueio:divergencia_revisor": 2, "bloqueio:erro_http": 1,
                                          "bloqueio:resposta_tardia": 3}}
    assert "quant" not in s["bracos"]  # braco sem IA nao e FALSO
    s = relatorio.secao_injetados(motivos, {"claude_gpt": ["claude-x"]})  # modelo real: nada injetado
    assert s["falsos"] == [] and s["bracos"] == {}
    rel = relatorio.gerar_relatorio({"claude_gpt": _trades(20, braco="claude_gpt"), "quant": _trades(20, braco="quant")},
                                    tentativas=1, n_boot=20, injetados=motivos,
                                    modelos_servidos={"claude_gpt": ["claude-x-FALSO"]})
    md = relatorio.renderizar_markdown(rel)
    assert "Injetado (FALSO)" in md and "fora de qualquer comparacao" in md
    assert any("FALSO" in m for m in rel["bracos"]["claude_gpt"]["selo"]["motivos"])
    assert not any("FALSO" in m for m in rel["bracos"]["quant"]["selo"]["motivos"])


def _episodios():
    return [
        {"id": "ep1", "braco": "P", "intencao_id": "it1", "t_aberta_ns": 1, "t_fechada_ns": 2, "liquido_total": 10.0,
         "n_parciais": 1, "n_ajustes_ia": 1, "n_ajustes_auto": 2, "mfe_pts": 40.0, "mae_pts": 10.0,
         "motivo_final": "alvo", "sombra_de": None},
        {"id": "ep2", "braco": "P", "intencao_id": "it2", "t_aberta_ns": 3, "t_fechada_ns": 4, "liquido_total": -5.0,
         "n_parciais": 0, "n_ajustes_ia": 0, "n_ajustes_auto": 1, "mfe_pts": 5.0, "mae_pts": 30.0,
         "motivo_final": "stop", "sombra_de": None},
        {"id": "ep3", "braco": "P", "intencao_id": "it3", "t_aberta_ns": 5, "t_fechada_ns": 6, "liquido_total": 2.0,
         "n_parciais": 0, "n_ajustes_ia": 0, "n_ajustes_auto": 0, "mfe_pts": 8.0, "mae_pts": 4.0,
         "motivo_final": "tempo", "sombra_de": None},
        {"id": "es1", "braco": "sombra_sem_gestao", "intencao_id": "it1:sombra_sem_gestao", "t_aberta_ns": 1,
         "t_fechada_ns": 2, "liquido_total": 4.0, "sombra_de": "P", "motivo_final": "alvo"},
        {"id": "es2", "braco": "sombra_sem_gestao", "intencao_id": "it2:sombra_sem_gestao", "t_aberta_ns": 3,
         "t_fechada_ns": 4, "liquido_total": -9.0, "sombra_de": "P", "motivo_final": "stop"},
    ]


def test_secoes_de_tentativas_episodios_gestao_sombras_e_perdas_seguidas():
    eps = _episodios()
    evs = [{"braco": "P", "origem": "auto", "aplicado": 1}, {"braco": "P", "origem": "ia", "aplicado": 0,
                                                              "motivo": "stop_afrouxa"}]
    rel = relatorio.gerar_relatorio({"P": [], "sombra_sem_gestao": [], "C": _trades(10, braco="C")}, tentativas=1,
                                    n_boot=50, tentativas_entrada={"P": 12, "C": 30}, episodios=eps,
                                    eventos_gestao=evs, dias_perdas_seguidas={"C": ["2026-11-04", "2026-11-03"]})
    te = rel["tentativas_episodios"]
    assert te["P"] == {"tentativas": 12, "episodios": 3, "fonte": "posicoes_episodio",
                       "tentativas_por_episodio": 4.0}
    assert te["C"]["fonte"] == "trades" and te["C"]["episodios"] == 10 and te["C"]["tentativas_por_episodio"] == 3.0
    g = rel["gestao"]["P"]
    assert g["episodios"] == 3 and g["liquido_total_brl"] == pytest.approx(7.0)
    assert g["acerto_episodio"] == pytest.approx(2 / 3) and g["ajustes_auto_por_posicao"] == pytest.approx(1.0)
    assert g["mfe_mediano_pts"] == 8.0 and g["motivos_final"] == {"alvo": 1, "stop": 1, "tempo": 1}
    assert g["eventos_aplicados"] == {"auto": 1} and g["motivos_nao_aplicados"] == {"stop_afrouxa": 1}
    s = rel["sombras"]["sombra_sem_gestao"]
    assert s["referencia"] == "P" and s["n_pares"] == 2 and s["media_dif"] == pytest.approx(5.0)  # (6 + 4) / 2
    assert s["incompletos_ref"] == 1 and s["taxa_incompletos"] == pytest.approx(1 / 3)
    assert rel["dias_perdas_seguidas"] == {"C": ["2026-11-03", "2026-11-04"]}
    md = relatorio.renderizar_markdown(rel)
    for titulo in ("Tentativas de entrada contra episodios", "Gestao por posicao", "Diferencas pareadas com as sombras",
                   "Dias encerrados por perdas seguidas"):
        assert titulo in md
    json.dumps(relatorio.para_json_seguro(rel))


def test_segredos_do_mt5_e_do_painel_sao_redigidos(monkeypatch):
    from robowin.ia.segredos import redigir
    monkeypatch.setenv("ROBOWIN_MT5_SENHA", "senha-mt5-123")
    monkeypatch.setenv("ROBOWIN_TOKEN", "token-painel-456")
    out = redigir("erro: senha-mt5-123 e token-painel-456")
    assert "senha-mt5-123" not in out and "token-painel-456" not in out
