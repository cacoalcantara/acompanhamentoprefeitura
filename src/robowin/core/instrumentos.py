"""Perfil de instrumento: UNICA fonte dos numeros do ativo (tick, valor do ponto, lote, tarifas, vencimento, limites).

Trocar o ativo e editar ``[geral].simbolo_base`` (ou usar ``--ativo``): o codigo le o perfil em
``config/instrumentos.toml`` e nao tem nenhum numero de ativo embutido. Valor sem fonte confirmada fica
``a_confirmar = true`` e BLOQUEIA a operacao (falha segura): so a simulacao sobre dados sinteticos roda, rotulada.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .config import ErroConfig, carregar_toml
from .tipos import Base, Instrumento, SessaoPregao

CAMINHO_PADRAO = Path(__file__).resolve().parents[3] / "config" / "instrumentos.toml"
Classe = Literal["futuro_indice", "futuro_cambio", "acao", "etf"]
RegraVencimento = Literal["quarta_dia15_meses_pares", "primeiro_dia_util_mes", "nenhum"]
RESERVADAS = frozenset({"sessao_padrao"})
LETRAS_MES = "FGHJKMNQUVXZ"
ROTULO_A_CONFIRMAR = "ativo_a_confirmar"
ROTULO_BOLSA_A_CONFIRMAR = "bolsa_a_confirmar"


class PerfilNaoConfirmado(ErroConfig):
    """O perfil tem valores `a_confirmar`: operar (corretora/ao vivo) esta BLOQUEADO ate o usuario confirma-los."""


class TarifaB3(Base):
    vigente_desde: str  # AAAA-MM-DD
    tarifa_lado_brl: float  # emolumentos + liquidacao por lote e por lado
    taxa_zeragem_brl: float = 0.0  # cobrada pela corretora na zeragem forcada (por lote)
    fonte: str = ""


class PerfilInstrumento(Base):
    simbolo_base: str
    classe: Classe
    descricao: str = ""
    vencimento: RegraVencimento
    ponto_valor_brl: float = Field(gt=0)
    tick_pontos: float = Field(gt=0)
    lote: int = Field(ge=1)
    tarifas: tuple[TarifaB3, ...] = Field(min_length=1)
    margem_day_trade_brl: float | None = None
    stop_max_ticks: int = Field(ge=1)
    slippage_folga_ticks: int = Field(ge=0)
    stop_padrao_ticks: int = Field(ge=1)
    alvo_padrao_ticks: int = Field(ge=1)
    sessoes: tuple[SessaoPregao, ...] = ()
    bolsa: str = "B3"
    moeda: str = "BRL"  # moeda do instrumento: P&L, custos e limites de risco (campos *_brl) estao NELA
    aceita_book: bool = True
    rlp: bool = False
    risco_aprovado_padrao: bool = False
    preco_sintetico_ref: float = Field(gt=0)
    a_confirmar: bool = True
    a_confirmar_itens: tuple[str, ...] = ()

    # --- conversoes ticks <-> pontos (o unico lugar onde isso acontece)
    def pontos(self, ticks: float) -> float:
        return ticks * self.tick_pontos

    def ticks(self, pontos: float) -> float:
        return pontos / self.tick_pontos

    def arredondar(self, preco: float) -> float:
        return round(preco / self.tick_pontos) * self.tick_pontos

    @property
    def tick_valor_brl(self) -> float:
        return self.tick_pontos * self.ponto_valor_brl

    @property
    def stop_max_pontos(self) -> float:
        return self.pontos(self.stop_max_ticks)

    @property
    def slippage_folga_pontos(self) -> float:
        return self.pontos(self.slippage_folga_ticks)

    @property
    def stop_padrao_pontos(self) -> float:
        return self.pontos(self.stop_padrao_ticks)

    @property
    def alvo_padrao_pontos(self) -> float:
        return self.pontos(self.alvo_padrao_ticks)

    @property
    def tem_vencimento(self) -> bool:
        return self.vencimento != "nenhum"

    # --- estado de confirmacao
    def exigir_confirmado(self) -> PerfilInstrumento:
        """Falha segura: recusa operar com o perfil enquanto houver valor a confirmar."""
        if self.a_confirmar:
            itens = "; ".join(self.a_confirmar_itens) or "valores sem fonte confirmada"
            raise PerfilNaoConfirmado(
                f"perfil {self.simbolo_base} esta a_confirmar (operacao bloqueada): {itens}. Confira na B3/corretora e "
                "ponha a_confirmar = false em config/instrumentos.toml; a simulacao sintetica segue permitida.")
        return self

    def rotulos(self) -> frozenset[str]:
        return frozenset({ROTULO_A_CONFIRMAR}) if self.a_confirmar else frozenset()

    def bolsa_perfil(self) -> Any:
        from .bolsas import carregar_bolsa
        return carregar_bolsa(self.bolsa)

    # --- derivados
    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    def sessao_em(self, d: dt.date) -> SessaoPregao:
        grade = self.sessoes or sessoes_padrao()  # `carregar_perfil` preenche com a grade da bolsa
        vig = [s for s in grade if s.vigente_desde <= d.isoformat()]
        if not vig:
            raise ValueError(f"sem grade de sessao para {d}")
        return max(vig, key=lambda s: s.vigente_desde)

    def tarifa_em(self, d: dt.date) -> TarifaB3:
        vig = [t for t in self.tarifas if t.vigente_desde <= d.isoformat()]
        if not vig:
            raise ValueError(f"sem tarifa B3 vigente em {d} para {self.simbolo_base}")
        return max(vig, key=lambda t: t.vigente_desde)

    def instrumento(self, simbolo: str | None = None, vencimento: dt.date | str | None = None, **extra: Any) -> Instrumento:
        """`Instrumento` operacional deste perfil (``simbolo`` da corretora; por padrao o proprio simbolo_base)."""
        return Instrumento(
            simbolo=simbolo or self.simbolo_base, ativo=self.simbolo_base, moeda=self.moeda, ponto_valor_brl=self.ponto_valor_brl,
            tick_pontos=self.tick_pontos, lote=self.lote,
            vencimento=vencimento.isoformat() if isinstance(vencimento, dt.date) else vencimento, **extra)

    def codigo_pertence(self, simbolo: str) -> bool:
        """O `simbolo` (codigo da B3, sem decoracao da corretora) e deste ativo?"""
        if not self.tem_vencimento:
            return simbolo == self.simbolo_base
        return re.fullmatch(rf"{re.escape(self.simbolo_base)}[{LETRAS_MES}]\d{{2}}", simbolo) is not None


def _bruto(caminho: str | Path | None = None) -> dict[str, Any]:
    c = Path(caminho) if caminho else CAMINHO_PADRAO
    if not c.is_file():
        raise ErroConfig(f"arquivo de perfis de instrumento nao encontrado: {c}")
    return carregar_toml(c)


def sessoes_padrao(caminho: str | Path | None = None) -> tuple[SessaoPregao, ...]:
    return tuple(SessaoPregao(**s) for s in _bruto(caminho).get("sessao_padrao", []))


def ativos_disponiveis(caminho: str | Path | None = None) -> list[str]:
    return sorted(k for k, v in _bruto(caminho).items() if k not in RESERVADAS and isinstance(v, dict))


def carregar_perfil(simbolo_base: str, caminho: str | Path | None = None, *,
                    permitir_a_confirmar: bool = False) -> PerfilInstrumento:
    """Le o perfil de `simbolo_base`. Perfil `a_confirmar` so e devolvido com ``permitir_a_confirmar=True`` (simulacao)."""
    bruto = _bruto(caminho)
    chave = simbolo_base.strip().upper()
    sec = bruto.get(chave)
    if chave in RESERVADAS or not isinstance(sec, dict):
        raise ErroConfig(f"ativo '{simbolo_base}' sem perfil em instrumentos.toml (ha: {', '.join(ativos_disponiveis(caminho))}). "
                         "Copie o perfil mais parecido e confirme os valores.")
    dados = {**sec, "simbolo_base": chave}
    try:
        perfil = PerfilInstrumento(**{k: v for k, v in dados.items() if k != "sessoes"},
                                   sessoes=tuple(SessaoPregao(**s) for s in sec.get("sessoes", [])))
    except Exception as e:
        raise ErroConfig(f"perfil {chave} invalido: {e}") from e
    perfil = _aplicar_bolsa(perfil)
    return perfil if permitir_a_confirmar else perfil.exigir_confirmado()


def _aplicar_bolsa(perfil: PerfilInstrumento) -> PerfilInstrumento:
    """Herda moeda e sessoes da bolsa do perfil; bolsa a_confirmar torna o perfil a_confirmar (bloqueia operar)."""
    from .bolsas import carregar_bolsa
    b = carregar_bolsa(perfil.bolsa)
    itens = tuple(perfil.a_confirmar_itens) + tuple(f"bolsa {b.codigo}: {i}" for i in b.a_confirmar_itens)
    return perfil.model_copy(update={
        "moeda": b.moeda, "sessoes": perfil.sessoes or b.sessoes,
        "a_confirmar": perfil.a_confirmar or b.a_confirmar, "a_confirmar_itens": itens})


def ativo_da_config(cfg: dict[str, Any], override: str | None = None) -> str:
    """Ativo escolhido: `--ativo` (override) vence `[geral].simbolo_base`."""
    ativo = override or cfg.get("geral", {}).get("simbolo_base")
    if not ativo:
        raise ErroConfig("defina [geral].simbolo_base em config/robowin.toml ou use --ativo")
    return str(ativo).upper()


def perfil_da_config(cfg: dict[str, Any], override: str | None = None, *, permitir_a_confirmar: bool = False,
                     caminho: str | Path | None = None) -> PerfilInstrumento:
    return carregar_perfil(ativo_da_config(cfg, override), caminho, permitir_a_confirmar=permitir_a_confirmar)


def ativo_de(ins: Instrumento) -> str:
    """Ativo (simbolo_base) de um `Instrumento`; sem o campo, deduz do simbolo da corretora (WINZ26 -> WIN)."""
    if ins.ativo:
        return ins.ativo
    m = re.match(rf"^(?:SINT_)?([A-Z]{{3,6}}\d{{0,2}}?)(?:[{LETRAS_MES}]\d{{2}})?$", ins.simbolo)
    return m.group(1) if m else ins.simbolo


class AtivoDivergente(ErroConfig):
    """Artefato (modelo, orcamento, limites) pertence a OUTRO ativo: nunca e reutilizado."""


def exigir_mesmo_ativo(esperado: str, encontrado: str, o_que: str) -> None:
    if (encontrado or "").upper() != (esperado or "").upper():
        raise AtivoDivergente(f"{o_que} e do ativo '{encontrado or '?'}' e a sessao e de '{esperado}': nao reutilizado")


def perfil_de_instrumento(ins: Instrumento, caminho: str | Path | None = None) -> PerfilInstrumento:
    """Perfil do ativo de um `Instrumento` (sem bloquear `a_confirmar`: quem opera usa `exigir_confirmado`)."""
    return carregar_perfil(ativo_de(ins), caminho, permitir_a_confirmar=True)


def argumentos_ativo(parser: Any) -> None:
    """Adiciona `--ativo` e `--config` a um subcomando (a troca de ativo e so configuracao)."""
    parser.add_argument("--ativo", default=None,
                        help="simbolo_base do perfil em config/instrumentos.toml (padrao: [geral].simbolo_base)")
    if not any(a.dest == "config" for a in parser._actions):
        parser.add_argument("--config", default="config/robowin.toml")


def perfil_do_cli(args: Any, *, permitir_a_confirmar: bool = True) -> PerfilInstrumento:
    """Perfil escolhido por `--ativo` > [geral].simbolo_base. Em demos sinteticas `a_confirmar` roda, com aviso."""
    cfg = carregar_toml(args.config) if Path(args.config).is_file() else {}
    perfil = perfil_da_config(cfg, args.ativo, permitir_a_confirmar=permitir_a_confirmar)
    if perfil.a_confirmar:
        print(f"AVISO: perfil {perfil.simbolo_base} a_confirmar ({'; '.join(perfil.a_confirmar_itens)}): so simulacao "
              "sintetica rotulada; operar esta BLOQUEADO ate confirmar em config/instrumentos.toml.")
    return perfil
