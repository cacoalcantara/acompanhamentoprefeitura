"""Estatico: os prompts do braco planejador sao GENERICOS (qualquer mercado). Sem nome de ativo/contrato de
config/instrumentos.toml nem par de moedas (RE_ATIVO), sem "R$", "B3" ou "Ibovespa" e sem valor de tick fixo.
Os prompts v1 do proponente/revisor ficam congelados por pre-registro (pendencia anotada no doc 16)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_sem_constantes_de_ativo import RE_ATIVO

DIR = Path(__file__).resolve().parents[2] / "config" / "prompts"
PROMPTS_PLANEJADOR = ("planejador_v1.md", "revisor_plano_v1.md")
_RE_PROIBIDO = re.compile(r"R\$|\bB3\b|ibovespa|\b1 tick\s*=|\btick\s*=\s*\d", re.IGNORECASE)


def _variantes() -> list[Path]:
    """O prompt base e qualquer variante por provedor (``nome.provedor.md``)."""
    out: list[Path] = []
    for nome in PROMPTS_PLANEJADOR:
        base = nome.removesuffix(".md")
        out += sorted(DIR.glob(f"{base}.md")) + sorted(DIR.glob(f"{base}.*.md"))
    return out


def violacoes(texto: str) -> list[str]:
    v = [f"termo de mercado especifico: {m.group(0)!r}" for m in _RE_PROIBIDO.finditer(texto)]
    v += [f"nome de ativo: {tok!r}" for tok in re.findall(r"[A-Za-z0-9_.]+", texto) if RE_ATIVO.match(tok)]
    return v


def test_prompts_do_planejador_existem():
    nomes = {p.name for p in _variantes()}
    assert set(PROMPTS_PLANEJADOR) <= nomes


@pytest.mark.parametrize("arq", _variantes(), ids=lambda p: p.name)
def test_prompt_do_planejador_e_generico(arq):
    assert violacoes(arq.read_text(encoding="utf-8")) == []


def test_detector_pega_mercado_especifico():
    for ruim in ("minicontrato WIN da B3", "1 ponto = R$ 0,20", "indice Ibovespa", "o EURUSD subiu",
                 "WDOX26 abriu", "1 tick = 5 pontos"):
        assert violacoes(ruim), ruim
    assert violacoes("pontos relativos ao ultimo preco; use envelope.tick_pontos") == []
