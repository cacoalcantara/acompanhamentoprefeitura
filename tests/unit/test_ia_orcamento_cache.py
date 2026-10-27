from __future__ import annotations

import pytest

from robowin.core.tipos import Acao, PropostaIA
from robowin.ia.cache import CacheLLM, ProvedorComCache, hash_requisicao
from robowin.ia.catalogo import Catalogo
from robowin.ia.orcamento import OrcamentoEsgotado, OrcamentoIA, ProvedorComOrcamento
from robowin.ia.provedores.falso import ProvedorFalso

CAT = Catalogo.carregar("config/modelos.toml")
MODELO = "claude-opus-5-5"


def test_reservar_confirmar_liberar():
    o = OrcamentoIA(100.0, pregoes_no_mes=10)
    r = o.reservar(2.0)
    assert o.reservado == 2.0 and o.disponivel_mes == 98.0
    o.confirmar(r, 0.5)
    assert o.gasto_mes == 0.5 and o.reservado == 0.0
    r2 = o.reservar(3.0)
    o.liberar(r2)
    assert o.gasto_mes == 0.5 and o.reservado == 0.0
    with pytest.raises(KeyError):
        o.confirmar(r, 1.0)  # reserva ja resolvida


def test_teto_mensal_conta_reservas_em_aberto():
    o = OrcamentoIA(10.0, pregoes_no_mes=1)
    a = o.reservar(6.0)
    with pytest.raises(OrcamentoEsgotado) as e:
        o.reservar(5.0)  # 6 reservado + 5 > 10
    assert e.value.motivo == "teto_mensal"
    o.liberar(a)
    o.reservar(5.0)


def test_ritmo_por_pregao_distribui_a_verba():
    o = OrcamentoIA(210.0, pregoes_no_mes=21)  # cota do dia = 10
    o.iniciar_pregao("d1", 21)
    assert o.cota_dia == pytest.approx(10.0)
    r = o.reservar(9.0)
    o.confirmar(r, 9.0)
    with pytest.raises(OrcamentoEsgotado) as e:
        o.reservar(2.0)
    assert e.value.motivo == "ritmo_pregao"
    o.iniciar_pregao("d2", 20)  # novo pregao: cota = (210-9)/20
    assert o.cota_dia == pytest.approx(10.05)
    o.reservar(2.0)


async def test_orcamento_esgotado_no_meio_do_pregao_bloqueia_novas_chamadas():
    # cada chamada reserva o pior caso (~US$ 0,082 com max_tokens=4096); sobra verba para ~2
    o = OrcamentoIA(0.12, pregoes_no_mes=1)
    falso = ProvedorFalso(catalogo=CAT)
    # o falso nao e "anthropic": usa o nome do provedor real para precificar
    falso.nome = "anthropic"
    prov = ProvedorComOrcamento(falso, o, CAT)
    resultados = []
    for _ in range(8):
        obj, ch = await prov.gerar_estruturado(modelo=MODELO, sistema="s", mensagem="m", schema=PropostaIA,
                                               prazo_s=1.0, max_tokens=4096)
        resultados.append((obj, ch))
    validas = [c.valida for _, c in resultados]
    assert validas[0] and not validas[-1]
    bloq = [c for _, c in resultados if (c.erro or "").startswith("orcamento_esgotado")]
    assert bloq and prov.bloqueios == len(bloq)
    # apos esgotar, nenhuma chamada nova chegou ao provedor
    assert falso.chamadas == len(resultados) - len(bloq) == sum(validas)
    # esgotado e persistente: continua bloqueando
    _, ch = await prov.gerar_estruturado(modelo=MODELO, sistema="s", mensagem="m", schema=PropostaIA, prazo_s=1.0)
    assert ch.erro.startswith("orcamento_esgotado")
    assert o.gasto_mes <= 0.12 + 1e-9


async def test_timeout_e_cobrado_a_entrada_e_erro_http_libera():
    o = OrcamentoIA(50.0, pregoes_no_mes=1)
    falso = ProvedorFalso(atraso_s=5.0, catalogo=CAT)
    falso.nome = "anthropic"
    prov = ProvedorComOrcamento(falso, o, CAT)
    _, ch = await prov.gerar_estruturado(modelo=MODELO, sistema="s" * 1000, mensagem="m", schema=PropostaIA, prazo_s=0.05)
    assert ch.erro == "timeout" and o.gasto_mes > 0 and o.reservado == 0
    antes = o.gasto_mes
    falso2 = ProvedorFalso(roteiro=[RuntimeError("x")], catalogo=CAT)
    falso2.nome = "anthropic"
    prov2 = ProvedorComOrcamento(falso2, o, CAT)
    _, ch2 = await prov2.gerar_estruturado(modelo=MODELO, sistema="s", mensagem="m", schema=PropostaIA, prazo_s=1.0)
    assert not ch2.valida and o.gasto_mes == antes and o.reservado == 0


# ---------------- cache ----------------
def _prop(acao=Acao.COMPRAR):
    return PropostaIA(acao=acao, incerteza=0.4, stop_pontos=80, alvo_pontos=120, justificativa_curta="x")


KW = dict(modelo="m", sistema="sis", mensagem="msg", schema=PropostaIA, prazo_s=1.0, effort="low", max_tokens=1000)


def test_hash_requisicao_estavel_e_sensivel():
    base = dict(modelo="a:m", sistema="s", mensagem="x", schema=PropostaIA, effort="low", max_tokens=10)
    assert hash_requisicao(**base) == hash_requisicao(**base)
    for k, v in (("mensagem", "y"), ("sistema", "t"), ("effort", "high"), ("max_tokens", 11), ("modelo", "b:m")):
        assert hash_requisicao(**{**base, k: v}) != hash_requisicao(**base)


async def test_gravacao_e_repeticao_com_hash_identico(tmp_path):
    d1, d2 = tmp_path / "g1", tmp_path / "g2"
    for d in (d1, d2):  # duas execucoes independentes de gravacao
        prov = ProvedorComCache(ProvedorFalso(roteiro=[_prop()]), CacheLLM(d, "gravar"))
        await prov.gerar_estruturado(**KW)
        await prov.gerar_estruturado(**{**KW, "mensagem": "outra"})
    assert sorted(p.name for p in d1.iterdir()) == sorted(p.name for p in d2.iterdir())  # mesmos hashes
    assert len(list(d1.iterdir())) == 2

    interno = ProvedorFalso(roteiro=[_prop(Acao.VENDER)])  # se chamado, daria resposta diferente
    rep = ProvedorComCache(interno, CacheLLM(d1, "repetir"))
    o1, c1 = await rep.gerar_estruturado(**KW)
    o2, c2 = await rep.gerar_estruturado(**KW)
    assert o1 == o2 == _prop() and c1 == c2 and c1.valida
    assert interno.chamadas == 0  # repeticao nunca chama o provedor
    assert rep.cache.acertos == 2


async def test_repeticao_sem_gravacao_e_miss_deterministico(tmp_path):
    interno = ProvedorFalso()
    rep = ProvedorComCache(interno, CacheLLM(tmp_path, "repetir"))
    obj, ch = await rep.gerar_estruturado(**KW)
    assert obj is None and ch.erro == "cache_miss" and interno.chamadas == 0 and rep.cache.faltas == 1


async def test_gravar_reaproveita_entrada_existente_e_registra_invalidas(tmp_path):
    interno = ProvedorFalso(roteiro=["lixo"])
    prov = ProvedorComCache(interno, CacheLLM(tmp_path, "gravar"))
    o, c = await prov.gerar_estruturado(**KW)
    assert o is None and c.erro == "json_invalido"
    o2, c2 = await prov.gerar_estruturado(**KW)  # segunda vez vem do cache
    assert interno.chamadas == 1 and c2.erro == "json_invalido"
    assert ProvedorComCache(ProvedorFalso(), CacheLLM(tmp_path, "desligado")).cache.modo == "desligado"
