import pytest

from robowin.risco.kill_switch import ErroRearme, ErroToken, KillSwitch, hash_token


class Aud:
    def __init__(self):
        self.l = []

    def registrar(self, tipo, dados):
        self.l.append((tipo, dados))
        return "h"


def test_manual_por_flag(tmp_path):
    flag = tmp_path / "EMERGENCIA"
    ks = KillSwitch(arquivo_flag=flag)
    assert not ks.verificar_flag(1)
    KillSwitch.criar_flag(flag, "teste")
    assert ks.verificar_flag(2) and ks.ultimo.origem == "manual_flag"


def test_manual_por_token_e_cli():
    ks = KillSwitch(token_hash=hash_token("segredo"))
    with pytest.raises(ErroToken):
        ks.acionar_com_token("errado", "x", 1)
    assert not ks.ativo
    assert ks.acionar_com_token("segredo", "painel", 2) and ks.ultimo.origem == "manual_token"
    ks2 = KillSwitch()
    assert ks2.acionar_manual("cli", 3) and ks2.ultimo.origem == "manual_cli"


def test_acionamento_e_idempotente_e_efeitos_executam():
    ks = KillSwitch()
    vistos = []
    ks.registrar_efeito(lambda e: vistos.append(e))
    ks.registrar_efeito(lambda e: 1 / 0)  # efeito defeituoso nao impede os demais
    ks.registrar_efeito(lambda e: vistos.append("depois"))
    assert ks.acionar("a", "manual_cli", 1) and not ks.acionar("b", "manual_cli", 2)
    assert len(vistos) == 2 and vistos[0].cancelar and vistos[0].zerar and vistos[0].bloquear


@pytest.mark.parametrize("acao,origem", [
    (lambda ks: ks.checar_perda_diaria(-200.0, 200.0, 1), "perda_diaria"),
    (lambda ks: ks.checar_drawdown(1500.0, 1500.0, 1), "drawdown"),
    (lambda ks: ks.dado_critico("feed corrompido", 1), "dado_critico"),
    (lambda ks: ks.divergencia_reconciliacao("posicao", 1), "divergencia_reconciliacao"),
])
def test_gatilhos_automaticos(acao, origem):
    ks = KillSwitch()
    assert acao(ks) and ks.ultimo.origem == origem


def test_gatilhos_nao_disparam_abaixo_do_limite():
    ks = KillSwitch()
    assert not ks.checar_perda_diaria(-199.0, 200.0, 1)
    assert not ks.checar_drawdown(1499.0, 1500.0, 1)
    assert not ks.ativo


def test_erros_repetidos_na_janela():
    ks = KillSwitch(erros_max=3, janela_erros_s=10)
    assert not ks.registrar_erro("x", int(1e9))
    assert not ks.registrar_erro("x", int(2e9))
    assert not ks.registrar_erro("x", int(30e9))  # os anteriores sairam da janela
    assert not ks.registrar_erro("x", int(31e9))
    assert ks.registrar_erro("x", int(32e9)) and ks.ultimo.origem == "erros_repetidos"


def test_heartbeat():
    ks = KillSwitch(heartbeat_timeout_s=5, origens_heartbeat=("gateway", "nucleo"))
    ks.registrar_heartbeat("gateway", int(10e9))
    ks.registrar_heartbeat("nucleo", int(10e9))
    assert not ks.checar_heartbeat(int(14e9))
    ks.registrar_heartbeat("gateway", int(14e9))
    assert ks.checar_heartbeat(int(16e9)) and ks.ultimo.origem == "heartbeat"  # nucleo calou


def test_rearme_so_manual_com_motivo_na_auditoria(tmp_path):
    aud = Aud()
    flag = tmp_path / "EMERGENCIA"
    ks = KillSwitch(arquivo_flag=flag, auditoria=aud, token_hash=hash_token("t"))
    with pytest.raises(ErroRearme):
        ks.rearmar("nada ativo", "ana", 1, token="t")
    ks.acionar("dd", "drawdown", 1)
    # gatilhos automaticos NUNCA rearmam: condicao normalizada nao basta
    ks.checar_perda_diaria(0.0, 200.0, 2)
    assert ks.ativo
    with pytest.raises(ErroRearme):
        ks.rearmar("ok", "ana", 3, token="t")  # motivo curto
    with pytest.raises(ErroRearme):
        ks.rearmar("motivo valido", "", 3, token="t")
    with pytest.raises(ErroToken):
        ks.rearmar("motivo valido", "ana", 3, token="errado")
    KillSwitch.criar_flag(flag)
    with pytest.raises(ErroRearme):
        ks.rearmar("motivo valido", "ana", 3, token="t")  # flag ainda presente
    flag.unlink()
    ks.rearmar("revisei o drawdown", "ana", 4, token="t")
    assert not ks.ativo
    tipo, dados = [x for x in aud.l if x[0] == "kill_switch_rearmado"][0]
    assert dados["motivo"] == "revisei o drawdown" and dados["responsavel"] == "ana" and dados["acionado_por"] == "drawdown"
    assert any(x[0] == "kill_switch_acionado" for x in aud.l)
