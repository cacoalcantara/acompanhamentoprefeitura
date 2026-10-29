"""Contratos compartilhados do braco planejador (docs/16, WP0): esquemas novos com extra=forbid e sem campo de
limite, esquemas legados congelados (hash do JSON Schema), Intencao antiga serializa igual, ConfigRisco com o teto de
tempo da posicao e politica aprovada intacta. Sem rede e sem chave de API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from robowin.core.tipos import (
    Acao,
    AjustePosicao,
    Base,
    Condicao,
    ConfigPlanejador,
    ConfigRisco,
    EnvelopeDecisao,
    Intencao,
    PlanoCondicional,
    PlanoGestaoCompilado,
    PlanoTradeIA,
    PreRegistro,
    PropostaIA,
    RegraCompilada,
    RegraGestao,
    RevisaoIA,
    RevisaoPlanoIA,
)
from robowin.decisao.bracos.base import BracoBase, ContextoDecisao, Geometria, ResultadoDecisao
from robowin.ia.esquemas import ESQUEMAS_POR_PAPEL
from robowin.ia.provedores.base import verificar_schema
from robowin.risco.politica import carregar_politica

RAIZ = Path(__file__).resolve().parents[2]

# sha256 do JSON Schema (chaves ordenadas, sem espacos) capturado ANTES das mudancas do WP0: os esquemas legados dos
# bracos B/C estao congelados por pre-registro (mudar = outra variante).
SHA_PROPOSTA_IA = "b3386f3d1126a5e4339e6bfe942fbf545e61d6aa31d8e3897f9a871af3cade15"
SHA_REVISAO_IA = "565daca4cc8d28d69c328ffe54ba89e89ffc61e0236849d81f3176a7d92842db"


def _sha_schema(m: type[Base]) -> str:
    return hashlib.sha256(json.dumps(m.model_json_schema(), sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


class IntencaoAntiga(Base):
    """Copia fiel da Intencao antes do WP0 (referencia de serializacao)."""
    id: str
    braco: str
    acao: Acao
    quantidade: int = 1
    stop_pontos: float | None = None
    alvo_pontos: float | None = None
    tempo_max_s: int | None = None
    validade_ns: int
    modo: Literal["imediato", "condicional", "quant"] = "imediato"
    plano: PlanoCondicional | None = None
    preco_referencia: float = 0.0
    proposta_id: str | None = None
    revisao_id: str | None = None
    t0_ns: int = 0


# ================================================================================== esquemas legados ==
def test_esquemas_legados_congelados():
    assert _sha_schema(PropostaIA) == SHA_PROPOSTA_IA
    assert _sha_schema(RevisaoIA) == SHA_REVISAO_IA
    assert ESQUEMAS_POR_PAPEL["proponente"] is PropostaIA and ESQUEMAS_POR_PAPEL["revisor"] is RevisaoIA


def test_revisao_legada_continua_aprovando_so_com_veredito():
    """C15: o legado (braco C) fica intacto de proposito; o planejador usa RevisaoPlanoIA."""
    assert RevisaoIA(veredito="APROVAR").coerente is True


# ===================================================================================== esquemas novos ==
def test_papeis_novos_registrados():
    assert ESQUEMAS_POR_PAPEL["planejador"] is PlanoTradeIA
    assert ESQUEMAS_POR_PAPEL["revisor_plano"] is RevisaoPlanoIA


@pytest.mark.parametrize("esquema", [PlanoTradeIA, RevisaoPlanoIA])
def test_esquemas_novos_sem_campo_de_raciocinio(esquema):
    verificar_schema(esquema)


@pytest.mark.parametrize("modelo", [RegraGestao, AjustePosicao, PlanoTradeIA, RevisaoPlanoIA, RegraCompilada,
                                    PlanoGestaoCompilado, EnvelopeDecisao, ConfigPlanejador, PreRegistro])
def test_extra_forbid_e_congelado(modelo):
    assert modelo.model_config.get("extra") == "forbid" and modelo.model_config.get("frozen") is True


@pytest.mark.parametrize("campo", ["perda_max_operacao_brl", "tempo_max_posicao_s", "contratos_max",
                                   "stop_max_pontos", "perda_max_dia_brl", "risco", "limites"])
def test_plano_recusa_campos_de_limite(campo):
    with pytest.raises(ValidationError):
        PlanoTradeIA.model_validate({"acao": "COMPRAR", campo: 1})
    with pytest.raises(ValidationError):
        AjustePosicao.model_validate({campo: 1})


def test_nenhum_esquema_da_ia_tem_campo_de_configrisco():
    proibidos = set(ConfigRisco.model_fields)

    def nomes(m: type[Base]) -> set[str]:
        out: set[str] = set()

        def varrer(d: object) -> None:
            if isinstance(d, dict):
                if isinstance(d.get("properties"), dict):
                    out.update(d["properties"])
                for v in d.values():
                    varrer(v)
            elif isinstance(d, list):
                for v in d:
                    varrer(v)
        varrer(m.model_json_schema())
        return out
    for m in (PlanoTradeIA, RevisaoPlanoIA, EnvelopeDecisao):
        assert not nomes(m) & proibidos, m.__name__


def test_plano_completo_valida_e_le_de_volta():
    p = PlanoTradeIA(
        acao=Acao.COMPRAR, p_alvo=0.55, quantidade=1, stop_pontos=60, alvo_pontos=90, tempo_max_s=240,
        gatilhos=(Condicao(tipo="preco_rompe_acima", valor=10),), faixa_contra_pontos=10, faixa_favor_pontos=5,
        expira_s=180, gestao=(RegraGestao(tipo="breakeven", mfe_pontos=40, folga_ticks=1),
                              RegraGestao(tipo="ao_vencer_tempo", acao_vencimento="proteger_e_estender",
                                          extensao_s=60)),
        reavaliar_em_s=60, reavaliar_se=("perto_do_stop", "regra_disparou"))
    assert PlanoTradeIA.model_validate_json(p.model_dump_json()) == p
    with pytest.raises(ValidationError):
        PlanoTradeIA(acao=Acao.COMPRAR, p_alvo=1.5)
    with pytest.raises(ValidationError):
        PlanoTradeIA.model_validate({"acao": "COMPRAR", "reavaliar_se": ["quando_quiser"]})
    with pytest.raises(ValidationError):
        RegraGestao.model_validate({"tipo": "media_movel"})
    with pytest.raises(ValidationError):  # nao existe Acao nova
        PlanoTradeIA.model_validate({"acao": "AJUSTAR"})


def test_ajuste_com_manter():
    p = PlanoTradeIA(acao=Acao.MANTER, ajuste=AjustePosicao(
        stop_resultado_pontos=0, alvo_resultado_pontos=120, tempo_max_total_s=280, reduzir_quantidade=1,
        substituir_gestao=(RegraGestao(tipo="trailing", distancia_pontos=50),)))
    assert p.ajuste is not None and p.ajuste.stop_resultado_pontos == 0


def test_revisao_do_plano_exige_os_tres_booleanos():
    """C15: so {"veredito": "APROVAR"} NAO aprova o plano."""
    with pytest.raises(ValidationError):
        RevisaoPlanoIA.model_validate({"veredito": "APROVAR"})
    with pytest.raises(ValidationError):
        RevisaoPlanoIA.model_validate({"veredito": "APROVAR", "coerente": True, "dados_ok": True})
    r = RevisaoPlanoIA(veredito="APROVAR", coerente=True, dados_ok=True, regras_ok=True, p_alvo_revisor=0.4)
    assert r.regras_ok and r.p_alvo_revisor == 0.4


def test_config_planejador_padrao_e_toml_do_repositorio():
    from robowin.core.config import carregar_toml
    c = ConfigPlanejador()
    assert not c.ativo and c.expor_envelope == "envelope" and c.exigir_p_alvo
    sec = dict(carregar_toml(RAIZ / "config/robowin.toml")["planejador"])
    gat = sec.pop("gatilhos")
    assert gat == {"periodo_s": 240, "eventos_posicao_permitidos": ["fechou"], "perto_frac": 0.3}
    cfg = ConfigPlanejador(**sec)
    assert cfg.ativo and cfg.model_dump(exclude={"ativo"}) == c.model_dump(exclude={"ativo"})
    with pytest.raises(ValidationError):
        ConfigPlanejador(expor_envelope="tudo")


def test_envelope_sem_dinheiro():
    e = EnvelopeDecisao(unidades_max=1, stop_max_pontos_por_q=(250.0,), stop_min_pontos=10.0,
                        dist_min_ajuste_pontos=5.0, tempo_max_s=300, entradas_possiveis=True)
    assert e.stop_max_pontos_por_q[0] == 250.0 and e.motivos_sem_entrada == ()
    assert not any(k.endswith(("_brl", "_conta", "_usd")) for k in EnvelopeDecisao.model_fields)


# ========================================================================================== Intencao ==
def _casos_intencao() -> list[dict]:
    plano = PlanoCondicional(gatilhos=(Condicao(tipo="preco_toca", valor=-10),), expira_ns=9)
    return [
        {"id": "a", "braco": "B", "acao": Acao.COMPRAR, "validade_ns": 1},
        {"id": "b", "braco": "C", "acao": Acao.VENDER, "quantidade": 1, "stop_pontos": 50.0, "alvo_pontos": 75.0,
         "tempo_max_s": 300, "validade_ns": 2, "modo": "condicional", "plano": plano, "preco_referencia": 123.5,
         "proposta_id": "p", "revisao_id": "r", "t0_ns": 7},
        {"id": "c", "braco": "Q", "acao": Acao.ENCERRAR, "validade_ns": 3, "modo": "quant"},
    ]


@pytest.mark.parametrize("campos", _casos_intencao())
def test_intencao_sem_campos_novos_serializa_igual_a_antiga(campos):
    nova, antiga = Intencao(**campos), IntencaoAntiga(**campos)
    assert nova.model_dump() == antiga.model_dump()
    assert nova.model_dump(mode="json") == antiga.model_dump(mode="json")
    assert nova.model_dump_json() == antiga.model_dump_json()
    assert Intencao.model_validate_json(antiga.model_dump_json()) == nova


def test_intencao_com_campos_novos_serializa_e_le_de_volta():
    g = PlanoGestaoCompilado(regras=(RegraCompilada(tipo="breakeven", mfe_pontos=40, folga_pontos=10),),
                             passo_min_pontos=5, intervalo_ajuste_s=2.0, reavaliar_se=("perto_do_alvo",))
    it = Intencao(id="x", braco="planejador", acao=Acao.MANTER, validade_ns=1, posicao_ticket=42, quantidade_t0=1,
                  gestao=g, ajuste=AjustePosicao(stop_resultado_pontos=-20), ancora_t0=100.0, p_alvo=0.5)
    d = it.model_dump(mode="json")
    assert d["posicao_ticket"] == 42 and d["gestao"]["regras"][0]["tipo"] == "breakeven" and d["p_alvo"] == 0.5
    assert Intencao.model_validate_json(it.model_dump_json()) == it
    so_um = Intencao(id="y", braco="planejador", acao=Acao.ENCERRAR, validade_ns=1, posicao_ticket=7)
    assert "posicao_ticket" in so_um.model_dump() and "p_alvo" not in so_um.model_dump()


# ========================================================================================= risco ==
def test_config_risco_tem_teto_de_tempo_da_posicao():
    assert ConfigRisco().tempo_max_posicao_s == 300


def test_politica_aprovada_continua_carregando():
    cfg = carregar_politica(RAIZ / "config/risco.toml", RAIZ / "config/aprovacoes/risco-v1.toml")
    assert cfg.tempo_max_posicao_s == 300
    assert "tempo_max_posicao_s" not in (RAIZ / "config/risco.toml").read_text(encoding="utf-8")


# =================================================================================== bracos/base ==
class _Braco(BracoBase):
    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:  # pragma: no cover - nao usado
        raise NotImplementedError


def _ctx(**kw) -> ContextoDecisao:
    from robowin.core.tipos import Lado, Posicao
    pos = kw.pop("posicao", Posicao(simbolo="X", lado=Lado.COMPRA, quantidade=2, preco_medio=10.0, ticket=99))
    return ContextoDecisao(t0_ns=5, dossie={}, hash_dossie="h", preco_ref=10.0, bid=9.0, ask=11.0, posicao=pos,
                           ciclo_id="cic", tick_pontos=1.0, **kw)


def test_contexto_e_resultado_novos_campos_com_padrao():
    ctx = _ctx()
    assert ctx.envelope is None and ctx.custo_ida_volta_pontos == 0.0 and not ctx.plano_vigente
    assert ctx.latencia_ia_s is None
    b = _Braco("b", geometria=Geometria(stop_pontos=10, alvo_pontos=10))
    r = b._resultado(ctx, Acao.MANTER, "manter_sem_ajuste")
    assert r.posicao_ticket_t0 == 99 and r.quantidade_t0 == 2
    assert r.reavaliar_em_s is None and r.reavaliar_se == () and r.p_alvo is None
    from robowin.core.tipos import Posicao
    r0 = b._resultado(_ctx(posicao=Posicao(simbolo="X")), Acao.NAO_OPERAR, "nao_operar")
    assert r0.posicao_ticket_t0 is None and r0.quantidade_t0 == 0


def test_intencao_do_braco_padrao_igual_e_com_quantidade_e_extras():
    b = _Braco("b", geometria=Geometria(stop_pontos=10, alvo_pontos=10))
    ctx = _ctx()
    it = b._intencao(ctx, Acao.COMPRAR, stop=10, alvo=20, tempo_max_s=60, validade_ns=9)
    assert it.quantidade == 1 and it.model_dump() == IntencaoAntiga(**it.model_dump()).model_dump()
    it2 = b._intencao(ctx, Acao.MANTER, stop=None, alvo=None, tempo_max_s=None, validade_ns=9, quantidade=2,
                      posicao_ticket=99, quantidade_t0=2, p_alvo=0.3)
    assert it2.quantidade == 2 and it2.posicao_ticket == 99 and it2.quantidade_t0 == 2 and it2.p_alvo == 0.3
