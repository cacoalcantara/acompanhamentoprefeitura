"""TravaEspelho: criacao atomica e trava vazia/ilegivel recente = OCUPADA (sem corrida)."""
from __future__ import annotations

import os
import time

import pytest

from robowin.execucao.ponte_gateway import EspelhamentoNaoExclusivo, TravaEspelho


def test_arquivo_vazio_recente_e_ocupado_nao_orfao(tmp_path):
    arq = tmp_path / "E.lock"
    arq.write_text("")  # outro processo acabou de criar (ainda nao escreveu)
    with pytest.raises(EspelhamentoNaoExclusivo):
        TravaEspelho(arq).adquirir("braco_a")
    assert arq.exists()


def test_arquivo_ilegivel_antigo_e_orfao_e_retomado(tmp_path):
    arq = tmp_path / "E.lock"
    arq.write_text("{lixo")
    velho = time.time() - 60
    os.utime(arq, (velho, velho))
    t = TravaEspelho(arq)
    t.adquirir("braco_a")
    assert t.atual()["dono"] == "braco_a"


def test_criacao_atomica_nunca_expoe_arquivo_vazio_e_nao_deixa_tmp(tmp_path):
    arq = tmp_path / "E.lock"
    t = TravaEspelho(arq)
    t.adquirir("x")
    assert t.atual()["pid"] == os.getpid() and list(tmp_path.glob("*.tmp")) == []
    t.liberar()
    assert not arq.exists()
