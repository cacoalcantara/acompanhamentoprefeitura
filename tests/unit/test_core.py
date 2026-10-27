import datetime as dt
import json

import pytest

from robowin.auditoria.cadeia import RegistradorAuditoria, verificar_arquivo
from robowin.core import calendario_b3 as cal
from robowin.core.barramento import BarramentoLocal
from robowin.core.config import ErroConfig, carregar_risco, sha256_arquivo
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import Instrumento


def test_contrato_vigente_e_vencimento():
    assert cal.vencimento_win(2026, 12) == dt.date(2026, 12, 16)
    assert cal.contrato_vigente(dt.date(2026, 10, 27)) == "WINZ26"
    assert cal.contrato_vigente(dt.date(2026, 12, 17)) == "WING27"
    assert cal.vencimento_win(2027, 2) == dt.date(2027, 2, 17)


def test_feriados_e_grade():
    assert not cal.eh_pregao(dt.date(2026, 11, 2))
    assert cal.eh_pregao(dt.date(2026, 11, 3))
    assert not cal.eh_pregao(dt.date(2026, 11, 20))
    assert cal.sessao_em(dt.date(2026, 11, 3)).fechamento == "18:25"


def test_instrumento_tick():
    assert Instrumento(simbolo="WINZ26").tick_valor_brl == pytest.approx(1.0)


def test_relogio_nao_volta():
    r = RelogioSimulado(10)
    r.avancar(5)
    with pytest.raises(ValueError):
        r.avancar_para(1)


def test_barramento():
    b, rec = BarramentoLocal(), []
    b.assinar("x", rec.append)
    b.publicar("x", {"a": 1})
    assert rec[0]["a"] == 1 and rec[0]["seq"] == 1


def test_cadeia_hash_detecta_adulteracao(tmp_path):
    r = RegistradorAuditoria(tmp_path)
    for i in range(5):
        r.registrar("evento", {"i": i}, dia="2026-10-27")
    arq = tmp_path / "auditoria-2026-10-27.jsonl"
    assert verificar_arquivo(arq)[0]
    linhas = arq.read_text().splitlines()
    # adulteracao
    alt = json.loads(linhas[2]); alt["dados"]["i"] = 99
    arq.write_text("\n".join(linhas[:2] + [json.dumps(alt)] + linhas[3:]) + "\n")
    assert not verificar_arquivo(arq)[0]
    # remocao
    arq.write_text("\n".join(linhas[:2] + linhas[3:]) + "\n")
    assert not verificar_arquivo(arq)[0]
    # reordenacao
    arq.write_text("\n".join([linhas[1], linhas[0]] + linhas[2:]) + "\n")
    assert not verificar_arquivo(arq)[0]


def test_risco_exige_hash(tmp_path):
    rk = tmp_path / "risco.toml"; rk.write_text("contratos_max = 1\n")
    ap = tmp_path / "ap.toml"; ap.write_text(f'sha256 = "{sha256_arquivo(rk)}"\n')
    assert carregar_risco(rk, ap).contratos_max == 1
    rk.write_text("contratos_max = 5\n")
    with pytest.raises(ErroConfig):
        carregar_risco(rk, ap)
