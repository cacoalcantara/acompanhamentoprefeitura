"""Perfis de instrumento para os testes (a fonte dos numeros e config/instrumentos.toml, nunca o codigo)."""
from __future__ import annotations

from robowin.core.instrumentos import carregar_perfil

WIN = carregar_perfil("WIN")
WDO = carregar_perfil("WDO", permitir_a_confirmar=True)
ACAO = carregar_perfil("PETR4", permitir_a_confirmar=True)
PERFIS = {"WIN": WIN, "WDO": WDO, "PETR4": ACAO}
INS_WIN = WIN.instrumento("WINZ26")


def amostras(perfil, ticks, livros, **kw):
    """`montar_amostras` com barreiras e custos do perfil (30 pontos / R$ 0,50 no WIN)."""
    from robowin.modelos.demo_sintetico import montar_amostras
    from robowin.modelos.rotulagem import ConfigBarreiras, Custos
    return montar_amostras(ticks, livros, ConfigBarreiras.do_perfil(perfil), Custos.do_perfil(perfil), **kw)
