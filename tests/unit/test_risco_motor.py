import datetime as dt
from pathlib import Path

import pytest

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.config import ErroConfig, sha256_arquivo
from robowin.core.ids import novo_id
from robowin.core.tipos import Acao, ConfigRisco, Instrumento, Intencao, Lado, Posicao, SessaoPregao
from robowin.risco.kill_switch import KillSwitch
from robowin.risco.motor import EstadoMercado, EventoAgenda, MotorRiscoPadrao
from robowin.risco.politica import carregar_politica, stop_maximo_efetivo_pontos, validar_coerencia

RAIZ = Path(__file__).resolve().parents[2]


def T(h, m, s=0, dia=(2026, 11, 10)):
    return dt_utc_ns(dt.datetime(*dia, h, m, s))


def motor(cfg=None, **kw):
    ins = kw.pop("instrumento", Instrumento(simbolo="WINZ26"))
    return MotorRiscoPadrao(cfg or ConfigRisco(), ins, **kw)


def intencao(acao=Acao.COMPRAR, stop=100.0, qtd=1, t=None, braco="b1", **kw):
    t = t or T(10, 0)
    return Intencao(id=novo_id("int"), braco=braco, acao=acao, quantidade=qtd, stop_pontos=stop,
                    validade_ns=t + int(60e9), preco_referencia=130000.0, **kw)


PLANO = Posicao(simbolo="WINZ26")


def avaliar(m, it, pos=PLANO, t=None):
    return m.avaliar(it, pos, t or T(10, 0))


def test_aprova_entrada_valida():
    d = avaliar(motor(), intencao())
    assert d.aprovada and d.motivos == ()


def test_manter_e_nao_operar_aprovados():
    m = motor()
    assert avaliar(m, intencao(Acao.MANTER, stop=None)).aprovada
    assert avaliar(m, intencao(Acao.NAO_OPERAR, stop=None)).aprovada


@pytest.mark.parametrize("stop,motivo", [
    (None, "stop_obrigatorio"), (0.0, "stop_obrigatorio"), (-5.0, "stop_obrigatorio"),
    (float("nan"), "stop_obrigatorio"), (301.0, "stop_acima_do_maximo"),
    (260.0, "perda_por_operacao_acima_do_maximo"),
])
def test_veto_stop(stop, motivo):
    d = avaliar(motor(), intencao(stop=stop))
    assert not d.aprovada and motivo in d.motivos


def test_perda_por_operacao_inclui_folga_de_slippage():
    m = motor()
    assert avaliar(m, intencao(stop=250.0)).aprovada  # (250+50)*0.2 = 60 exatos
    assert not avaliar(m, intencao(stop=255.0)).aprovada  # (255+50)*0.2 = 61
    assert stop_maximo_efetivo_pontos(ConfigRisco(), 0.2) == 250.0


def test_contratos_acima_do_maximo():
    d = avaliar(motor(), intencao(qtd=2))
    assert "contratos_acima_do_maximo" in d.motivos and "exposicao_acima_do_maximo" in d.motivos


def test_quantidade_invalida():
    d = avaliar(motor(), intencao(qtd=0))
    assert "quantidade_invalida" in d.motivos


@pytest.mark.parametrize("h,m_,s", [(9, 14, 59), (17, 30, 0), (17, 50, 0), (8, 0, 0), (18, 10, 0)])
def test_fora_da_janela(h, m_, s):
    t = T(h, m_, s)
    d = avaliar(motor(), intencao(t=t), t=t)
    assert "fora_da_janela_de_entradas" in d.motivos


@pytest.mark.parametrize("h,m_,s", [(9, 15, 0), (12, 0, 0), (17, 29, 59)])
def test_dentro_da_janela(h, m_, s):
    t = T(h, m_, s)
    assert avaliar(motor(), intencao(t=t), t=t).aprovada


def test_janela_usa_a_grade_se_mais_restritiva():
    grade = SessaoPregao(vigente_desde="2026-01-01", abertura="10:00", fechamento="17:00", entradas_ate="16:00",
                         zeragem="16:30")
    m = motor(grade=lambda d: grade)
    assert "fora_da_janela_de_entradas" in avaliar(m, intencao(), t=T(9, 30)).motivos  # antes da abertura da grade
    assert "fora_da_janela_de_entradas" in avaliar(m, intencao(), t=T(16, 5)).motivos
    assert m.limites_janela_s(dt.date(2026, 11, 10)) == (10 * 3600 + 0, 16 * 3600, 16 * 3600 + 1800)


def test_feriado_e_fim_de_semana():
    for dia in ((2026, 11, 20), (2026, 11, 14), (2026, 11, 2)):
        t = T(10, 0, dia=dia)
        assert "fora_do_pregao" in avaliar(motor(), intencao(t=t), t=t).motivos


def test_intencao_expirada():
    it = intencao()
    assert "intencao_expirada" in motor().avaliar(it, PLANO, it.validade_ns + 1).motivos


def test_kill_switch_veta_entrada():
    ks = KillSwitch()
    ks.acionar("teste", "manual_cli", T(9, 0))
    assert "kill_switch_ativo" in avaliar(motor(kill_switch=ks), intencao()).motivos


def test_bloqueio_por_evento_5_min():
    ev = EventoAgenda(t_ns=T(10, 0), nome="payroll")
    m = motor(eventos=[ev])
    for t in (T(9, 55), T(10, 0), T(10, 5)):
        assert "evento_calendario:payroll" in avaliar(m, intencao(t=t), t=t).motivos
    for t in (T(9, 54, 59), T(10, 5, 1)):
        assert avaliar(m, intencao(t=t), t=t).aprovada


@pytest.mark.parametrize("flag,motivo", [
    ("vol_extrema", "regime_extremo_volatilidade"), ("leilao", "leilao"),
    ("limite_oscilacao", "limite_de_oscilacao"), ("rolagem", "rolagem_de_contrato"),
    ("dado_critico", "dado_critico"), ("feed_ok", "feed_parado"),
])
def test_regimes(flag, motivo):
    m = motor()
    m.atualizar_mercado(EstadoMercado(**{flag: flag != "feed_ok"}))
    assert motivo in avaliar(m, intencao()).motivos


def test_rolagem_no_dia_do_vencimento():
    m = motor(instrumento=Instrumento(simbolo="WINZ26", vencimento="2026-11-10"))
    assert "rolagem_de_contrato" in avaliar(m, intencao()).motivos
    m2 = motor(instrumento=Instrumento(simbolo="WINZ26", vencimento="2026-12-16"))
    assert avaliar(m2, intencao()).aprovada


def test_sem_aumento_preco_medio_ou_piramidacao_nem_inversao():
    m = motor()
    longa = Posicao(simbolo="WINZ26", lado=Lado.COMPRA, quantidade=1, preco_medio=130000, stop=129900)
    assert "sem_aumento_de_posicao" in avaliar(m, intencao(Acao.COMPRAR), longa).motivos
    assert "inversao_proibida_use_encerrar" in avaliar(m, intencao(Acao.VENDER), longa).motivos


def test_ordem_pendente_conta_na_exposicao():
    m = motor()
    it1 = intencao()
    assert avaliar(m, it1).aprovada
    m.reservar_pendente(it1, Lado.COMPRA, T(10, 0))
    d = avaliar(m, intencao(Acao.VENDER))
    assert "ordem_de_entrada_pendente" in d.motivos and "exposicao_acima_do_maximo" in d.motivos
    m.liberar_pendente(it1.id)
    assert avaliar(m, intencao()).aprovada


def test_encerrar_sempre_permitido():
    ks = KillSwitch()
    ks.acionar("x", "manual_cli", 1)
    m = motor(kill_switch=ks)
    longa = Posicao(simbolo="WINZ26", lado=Lado.COMPRA, quantidade=1, preco_medio=1, stop=1)
    t = T(18, 30)
    assert m.avaliar(intencao(Acao.ENCERRAR, stop=None, t=t), longa, t).aprovada


def test_manter_so_aperta_stop():
    m = motor()
    longa = Posicao(simbolo="WINZ26", lado=Lado.COMPRA, quantidade=1, preco_medio=130000, stop=129900)
    # preco_referencia=130000: stop_pontos=50 -> novo stop 129950 (aperta); 150 -> 129850 (afrouxa)
    assert avaliar(m, intencao(Acao.MANTER, stop=50.0), longa).aprovada
    d = avaliar(m, intencao(Acao.MANTER, stop=150.0), longa)
    assert d.motivos == ("stop_so_pode_apertar",)
    assert m.validar_stop(longa, 129950) and not m.validar_stop(longa, 129850) and not m.validar_stop(longa, None)
    curta = Posicao(simbolo="WINZ26", lado=Lado.VENDA, quantidade=1, preco_medio=130000, stop=130100)
    assert m.validar_stop(curta, 130050) and not m.validar_stop(curta, 130150)


def test_perda_diaria_atingida_e_orcamento_restante():
    m = motor()
    t = T(10, 0)
    m.registrar_trade("b1", -100.0, 1, t)
    assert avaliar(m, intencao(stop=250.0, t=T(10, 10)), t=T(10, 10)).aprovada is True  # 100 + pior 60 = 160 < 200
    m.registrar_trade("b1", -50.0, 1, t)  # -150: pior caso (60) estouraria 200
    d = avaliar(m, intencao(stop=250.0, t=T(10, 20)), t=T(10, 20))
    assert "perda_diaria_excederia_limite" in d.motivos
    m.registrar_trade("b1", -60.0, 1, t)
    assert "perda_diaria_atingida" in avaliar(m, intencao(t=T(10, 30)), t=T(10, 30)).motivos
    assert m.kill.ativo and m.kill.ultimo.origem == "perda_diaria"


def test_perda_semanal():
    cfg = ConfigRisco(perda_max_dia_brl=100, perda_max_semana_brl=150)
    m = motor(cfg)
    m.registrar_trade("b1", -90.0, 1, T(10, 0, dia=(2026, 11, 9)))  # segunda
    m.registrar_trade("b1", -50.0, 1, T(10, 0, dia=(2026, 11, 10)))  # terca
    t = T(11, 0, dia=(2026, 11, 11))
    d = m.avaliar(intencao(t=t), PLANO, t)
    assert "perda_semanal_excederia_limite" in d.motivos
    assert "perda_diaria_atingida" not in d.motivos
    prox = T(10, 0, dia=(2026, 11, 17))  # semana seguinte zera (16/11 e segunda)
    assert m.avaliar(intencao(t=prox), PLANO, prox).aprovada


def test_drawdown_maximo_veta_e_aciona_kill():
    cfg = ConfigRisco(dd_alerta_brl=200, dd_max_brl=300, perda_max_semana_brl=5000)
    m = motor(cfg)
    m.registrar_trade("b1", -150.0, 1, T(10, 0, dia=(2026, 11, 9)))
    assert not m.kill.ativo
    m.registrar_trade("b1", -150.0, 1, T(10, 0, dia=(2026, 11, 10)))
    assert m.kill.ativo and m.kill.ultimo.origem == "drawdown"
    t = T(12, 0, dia=(2026, 11, 11))
    assert "drawdown_maximo" in m.avaliar(intencao(t=t), PLANO, t).motivos


def test_operacoes_por_dia():
    m = motor(ConfigRisco(operacoes_dia_max=2))
    for k in range(2):
        it = intencao(t=T(10, k))
        m.reservar_pendente(it, Lado.COMPRA, T(10, k))
        m.liberar_pendente(it.id)
    assert "operacoes_dia_maximo" in avaliar(m, intencao(t=T(11, 0)), t=T(11, 0)).motivos
    t2 = T(10, 0, dia=(2026, 11, 11))
    assert m.avaliar(intencao(t=t2), PLANO, t2).aprovada  # novo dia


def test_perdas_seguidas_e_pausa():
    m = motor(ConfigRisco(perdas_seguidas_max=2, perda_max_dia_brl=500, perda_max_semana_brl=900))
    m.registrar_trade("b1", -10.0, 1, T(10, 0))
    d = avaliar(m, intencao(), t=T(10, 2))
    assert "pausa_apos_perda" in d.motivos
    assert avaliar(m, intencao(t=T(10, 5, 1)), t=T(10, 5, 1)).aprovada
    m.registrar_trade("b1", -10.0, 1, T(10, 6))
    d = avaliar(m, intencao(t=T(11, 0)), t=T(11, 0))
    assert "perdas_seguidas_maximo" in d.motivos and "pausa_apos_perda" not in d.motivos
    m.registrar_trade("b2", 5.0, 1, T(10, 6))  # outro braco independente
    assert avaliar(m, intencao(braco="b2", t=T(11, 0)), t=T(11, 0)).aprovada


def test_sem_martingale_apos_perda():
    m = motor()
    m.registrar_trade("b1", -5.0, 1, T(10, 0))
    d = avaliar(m, intencao(qtd=2, t=T(11, 0)), t=T(11, 0))
    assert "martingale_proibido" in d.motivos


def test_erro_interno_vira_veto():
    def quebrado(_):
        raise RuntimeError("falha")
    m = motor(grade=quebrado)
    d = avaliar(m, intencao())
    assert not d.aprovada and d.motivos[0].startswith("erro_interno")


def test_precisa_zerar():
    m = motor()
    longa = Posicao(simbolo="WINZ26", lado=Lado.COMPRA, quantidade=1, preco_medio=1, stop=1)
    assert m.precisa_zerar(longa, T(12, 0)) == (False, "")
    assert m.precisa_zerar(longa, T(17, 45)) == (True, "zeragem")
    assert m.precisa_zerar(PLANO, T(17, 50)) == (False, "")
    m.kill.acionar("x", "manual_cli", 1)
    assert m.precisa_zerar(longa, T(12, 0)) == (True, "kill_switch")


def test_auditoria_registra_vetos():
    class Aud:
        def __init__(self):
            self.l = []

        def registrar(self, tipo, dados):
            self.l.append((tipo, dados))
            return "h"
    a = Aud()
    m = motor(auditoria=a)
    avaliar(m, intencao(stop=None))
    assert a.l and a.l[0][0] == "risco_decisao" and not a.l[0][1]["aprovada"]


# ---- politica ----------------------------------------------------------------------------------
def test_politica_carrega_com_hash_aprovado():
    cfg = carregar_politica(RAIZ / "config/risco.toml", RAIZ / "config/aprovacoes/risco-v1.toml")
    assert cfg.contratos_max == 1


def test_politica_recusa_hash_divergente(tmp_path):
    arq = tmp_path / "risco.toml"
    arq.write_text((RAIZ / "config/risco.toml").read_text().replace("200.0", "250.0"))
    ap = tmp_path / "ap.toml"
    ap.write_text(f'sha256 = "{sha256_arquivo(RAIZ / "config/risco.toml")}"\n')
    with pytest.raises(ErroConfig):
        carregar_politica(arq, ap)
    with pytest.raises(ErroConfig):
        carregar_politica(arq, None)


def test_politica_incoerente_ou_acima_do_teto():
    with pytest.raises(ErroConfig):
        validar_coerencia(ConfigRisco(contratos_max=2))
    with pytest.raises(ErroConfig):
        validar_coerencia(ConfigRisco(perda_max_operacao_brl=300))
    with pytest.raises(ErroConfig):
        validar_coerencia(ConfigRisco(entradas_ate="18:00", zeragem="17:45"))
