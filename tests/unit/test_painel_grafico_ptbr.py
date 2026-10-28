"""Regressao: o grafico uPlot nao pode cair no padrao en-US (am/pm) nem no fuso do navegador."""
from pathlib import Path

import robowin.painel as painel

JS = (Path(painel.__file__).parent / "static" / "painel.js").read_text(encoding="utf-8")


def test_grafico_usa_ptbr_e_utc():
    assert 'Intl.DateTimeFormat("pt-BR"' in JS
    assert 'timeZone: "UTC"' in JS
    assert "hour12: false" in JS
    assert "values: function (u, ticks)" in JS  # eixo X com formatador proprio
    assert "Hora (UTC)" in JS
