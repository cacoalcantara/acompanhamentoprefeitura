"""Aprovacao guiada da politica de risco (`robowin risco aprovar`) e confirmacao de perfis (`robowin perfil confirmar`).

Tudo em diretorios temporarios: config/ real NUNCA e alterado aqui."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import tomllib
from pathlib import Path

import pytest

from robowin.auditoria.cadeia import RegistradorAuditoria, verificar_diretorio
from robowin.cli import main
from robowin.core.config import (
    ErroConfig,
    aprovacao_pendente,
    carregar_risco,
    resolver_aprovacao,
    sha256_arquivo,
)
from robowin.core.instrumentos import carregar_perfil
from robowin.perfil.confirmacao import ConfirmacaoRecusada, confirmar_perfil, interpretar_alvo, listar_status
from robowin.perfil.edicao_toml import ErroEdicaoToml, atualizar_secao
from robowin.risco.aprovar import AprovacaoRecusada, aprovar_politica, estado_aprovacao, proximo_registro
from robowin.verificacao.checagens import Contexto, politica_risco

RAIZ = Path(__file__).resolve().parents[2]
HOJE = dt.date(2026, 10, 28)
# ativo de teste: o primeiro perfil a_confirmar do config real (o duble simula a corretora de todos os perfis)
ATIVO_A_CONFIRMAR = "IND"


def _aud(d: Path) -> list[dict]:
    linhas = []
    for arq in sorted(d.glob("auditoria-*.jsonl")):
        linhas += [json.loads(x) for x in arq.read_text(encoding="utf-8").splitlines() if x.strip()]
    return linhas


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Mini-repositorio temporario com copias de config/ (o real nunca e tocado)."""
    (tmp_path / "config" / "aprovacoes").mkdir(parents=True)
    for rel in ("config/risco.toml", "config/aprovacoes/risco-v1.toml", "config/instrumentos.toml", "config/bolsas.toml",
                "config/robowin.toml"):
        shutil.copy(RAIZ / rel, tmp_path / rel)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    return tmp_path


# =========================================================================================== risco aprovar ==
def test_aprovacao_valida_grava_v2_audita_e_verificar_passa_a_aprovado(repo):
    risco, ap = repo / "config/risco.toml", repo / "config/aprovacoes"
    v1_antes = (ap / "risco-v1.toml").read_bytes()
    assert politica_risco(Contexto(repo)).estado == "NAO VERIFICAVEL AQUI"  # v1 pendente
    vistos: list[list[str]] = []
    aud = RegistradorAuditoria(repo / "aud")
    res = aprovar_politica(risco, ap, "Fulana de Tal", "limites conferidos",
                           confirmar=lambda r: (vistos.append(r), "APROVO")[1], auditoria=aud, hoje=HOJE)
    assert res.arquivo == ap / "risco-v2.toml" and res.versao_anterior == ap / "risco-v1.toml"
    reg = tomllib.loads(res.arquivo.read_text(encoding="utf-8"))
    assert reg["sha256"] == sha256_arquivo(risco) and reg["responsavel"] == "Fulana de Tal"
    assert reg["data"] == "2026-10-28" and reg["motivo"] == "limites conferidos"
    assert (ap / "risco-v1.toml").read_bytes() == v1_antes  # nunca sobrescreve
    resumo = "\n".join(vistos[0])
    assert "R$ 60,00" in resumo and "R$ 1.500,00" in resumo and "09:15 ate 17:30" in resumo and reg["sha256"] in resumo
    # carregamento usa a versao mais recente valida, inclusive para quem aponta para risco-v1.toml
    assert resolver_aprovacao(ap / "risco-v1.toml", risco) == ap / "risco-v2.toml"
    assert not aprovacao_pendente(ap / "risco-v1.toml", risco)
    assert carregar_risco(risco, ap / "risco-v1.toml", ao_vivo=True).contratos_max == 1
    r = politica_risco(Contexto(repo))
    assert r.estado == "APROVADO" and "risco-v2.toml" in r.detalhe
    assert estado_aprovacao(risco, ap / "risco-v1.toml")[0] == "APROVADO"
    ev = _aud(repo / "aud")
    assert [e["tipo"] for e in ev] == ["risco_aprovado"] and ev[0]["dados"]["sha256"] == reg["sha256"]
    assert verificar_diretorio(repo / "aud")[0]


def test_confirmacao_errada_nao_muda_nada(repo):
    ap = repo / "config/aprovacoes"
    antes = sorted(p.name for p in ap.iterdir())
    for digitado in ("aprovo", "", "SIM", "APROVO!"):
        with pytest.raises(AprovacaoRecusada, match="nada foi alterado"):
            aprovar_politica(repo / "config/risco.toml", ap, "Fulana", "", confirmar=lambda _r, d=digitado: d,
                             auditoria=RegistradorAuditoria(repo / "aud"))
    assert sorted(p.name for p in ap.iterdir()) == antes and not _aud(repo / "aud")


def test_cli_confirmacao_errada_sai_com_codigo_nao_zero(repo, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _p="": "nao")
    rc = main(["risco", "aprovar", "--responsavel", "Fulana", "--risco", str(repo / "config/risco.toml"),
               "--aprovacoes", str(repo / "config/aprovacoes"), "--auditoria", str(repo / "aud")])
    assert rc != 0 and "RECUSADO" in capsys.readouterr().out
    assert not (repo / "config/aprovacoes/risco-v2.toml").exists()
    rc = main(["risco", "aprovar", "--responsavel", "Fulana", "--sim-eu-aprovo", "--risco", str(repo / "config/risco.toml"),
               "--aprovacoes", str(repo / "config/aprovacoes"), "--auditoria", str(repo / "aud")])
    assert rc == 0 and (repo / "config/aprovacoes/risco-v2.toml").is_file()
    assert main(["risco", "status", "--risco", str(repo / "config/risco.toml"),
                 "--aprovacao", str(repo / "config/aprovacoes/risco-v1.toml")]) == 0


@pytest.mark.parametrize("resp", ["", "   ", "pendente", "Pendente: depois", "PENDENTE fulano"])
def test_responsavel_vazio_ou_pendente_recusa(repo, resp):
    chamado = []
    with pytest.raises(AprovacaoRecusada, match="responsavel"):
        aprovar_politica(repo / "config/risco.toml", repo / "config/aprovacoes", resp, "",
                         confirmar=lambda r: (chamado.append(r), "APROVO")[1])
    assert not chamado and not (repo / "config/aprovacoes/risco-v2.toml").exists()


def test_hash_nao_confere_apos_editar_risco(repo):
    risco, ap = repo / "config/risco.toml", repo / "config/aprovacoes"
    aprovar_politica(risco, ap, "Fulana", "", confirmar=lambda _r: "APROVO", hoje=HOJE)
    risco.write_text(risco.read_text(encoding="utf-8").replace("perda_max_dia_brl = 200.0", "perda_max_dia_brl = 250.0"),
                     encoding="utf-8")
    with pytest.raises(ErroConfig, match="hash"):
        carregar_risco(risco, ap / "risco-v1.toml")
    r = politica_risco(Contexto(repo))
    assert r.estado == "REPROVADO" and "robowin risco aprovar" in r.detalhe
    assert estado_aprovacao(risco, ap / "risco-v1.toml")[0] == "INVALIDO"
    # reaprovar gera v3 e volta a valer
    res = aprovar_politica(risco, ap, "Fulana", "novo limite diario", confirmar=lambda _r: "APROVO", hoje=HOJE)
    assert res.arquivo.name == "risco-v3.toml" and politica_risco(Contexto(repo)).estado == "APROVADO"


def test_versao_mais_recente_valida(repo):
    risco, ap = repo / "config/risco.toml", repo / "config/aprovacoes"
    sha = sha256_arquivo(risco)
    (ap / "risco-v2.toml").write_text(f'sha256 = "{sha}"\ndata = "2026-10-28"\nresponsavel = "Beltrano"\n')
    (ap / "risco-v3.toml").write_text("isto nao e toml = = \n")  # ilegivel: ignorado
    (ap / "risco-v10.toml").write_text(f'sha256 = "{"0" * 64}"\nresponsavel = "Outro"\n')  # outro conteudo de risco
    assert resolver_aprovacao(ap / "risco-v1.toml", risco) == ap / "risco-v2.toml"  # v10 nao confere; v3 ilegivel
    assert proximo_registro(ap).name == "risco-v11.toml"  # numerico (v10 > v3), nunca reaproveita numero
    # sem o arquivo de risco para comparar, vale a maior versao legivel
    assert resolver_aprovacao(ap / "risco-v1.toml") == ap / "risco-v10.toml"
    # fora do padrao -vN, o arquivo e usado como esta
    outro = repo / "ap.toml"
    outro.write_text(f'sha256 = "{sha}"\n')
    assert resolver_aprovacao(outro, risco) == outro


def test_politica_incoerente_nao_e_aprovada(repo):
    risco = repo / "config/risco.toml"
    risco.write_text(risco.read_text(encoding="utf-8").replace("contratos_max = 1", "contratos_max = 5"), encoding="utf-8")
    with pytest.raises(AprovacaoRecusada, match="contratos_max"):
        aprovar_politica(risco, repo / "config/aprovacoes", "Fulana", "", confirmar=lambda _r: "APROVO")


def test_config_real_aprovacao_do_titular_e_provisoria_e_demo():
    """v1 segue 'pendente' (historico); a v2 foi aprovada pelo titular (pedido explicito em 28/10/2026), so DEMO."""
    reg1 = tomllib.loads((RAIZ / "config/aprovacoes/risco-v1.toml").read_text(encoding="utf-8"))
    assert reg1["responsavel"].startswith("pendente")
    reg2 = tomllib.loads((RAIZ / "config/aprovacoes/risco-v2.toml").read_text(encoding="utf-8"))
    assert "somente DEMO" in reg2["responsavel"] and "provisoria" in reg2["motivo"].lower()


# =========================================================================================== perfil confirmar ==
def _confirmar(repo, alvo="IND", digitado=None, **kw):
    a = interpretar_alvo(alvo)
    kw.setdefault("fonte", "especificacao B3 + corretora (teste)")
    return confirmar_perfil(a, instrumentos=repo / "config/instrumentos.toml", bolsas=repo / "config/bolsas.toml",
                            responsavel=kw.pop("responsavel", "Fulana"), hoje=HOJE,
                            confirmar=lambda _r: a.codigo if digitado is None else digitado, **kw)


def test_perfil_confirmado_preserva_resto_do_toml_e_audita(repo):
    arq = repo / "config/instrumentos.toml"
    antes_txt = arq.read_text(encoding="utf-8")
    antes = tomllib.loads(antes_txt)
    res = _confirmar(repo, ATIVO_A_CONFIRMAR, fonte_data="2026-10-01", auditoria=RegistradorAuditoria(repo / "aud"))
    assert res.alterado
    depois_txt = arq.read_text(encoding="utf-8")
    depois = tomllib.loads(depois_txt)
    sec = depois[ATIVO_A_CONFIRMAR]
    assert sec["a_confirmar"] is False and sec["a_confirmar_itens"] == []
    assert sec["fonte_data"] == "2026-10-01" and sec["confirmado_por"] == "Fulana" and sec["confirmado_em"] == "2026-10-28"
    # tudo o mais identico: outras secoes, tarifas do proprio ativo, e os comentarios do arquivo
    for k in antes:
        if k != ATIVO_A_CONFIRMAR:
            assert depois[k] == antes[k]
    resto = {k: v for k, v in sec.items() if k not in {"a_confirmar", "a_confirmar_itens", "fonte", "fonte_data",
                                                        "confirmado_por", "confirmado_em"}}
    assert resto == {k: v for k, v in antes[ATIVO_A_CONFIRMAR].items() if k not in {"a_confirmar", "a_confirmar_itens"}}
    comentarios = [ln for ln in antes_txt.splitlines() if ln.lstrip().startswith("#")]
    assert comentarios == [ln for ln in depois_txt.splitlines() if ln.lstrip().startswith("#")]
    assert len(depois_txt.splitlines()) - len(antes_txt.splitlines()) <= 4  # so as 4 chaves novas (itens multilinha encolhe)
    p = carregar_perfil(ATIVO_A_CONFIRMAR, arq)  # estrito: agora carrega
    assert not p.a_confirmar and p.confirmado_por == "Fulana"
    ev = _aud(repo / "aud")
    assert [e["tipo"] for e in ev] == ["perfil_confirmado"]
    assert ev[0]["dados"]["codigo"] == ATIVO_A_CONFIRMAR and ev[0]["dados"]["responsavel"] == "Fulana"
    assert ev[0]["dados"]["sha256_depois"] == sha256_arquivo(arq) and verificar_diretorio(repo / "aud")[0]
    # segunda vez: ja confirmado, nada muda
    assert not _confirmar(repo, ATIVO_A_CONFIRMAR).alterado and arq.read_text(encoding="utf-8") == depois_txt
    st = {s.codigo: s for s in listar_status(arq, repo / "config/bolsas.toml")}
    assert st[ATIVO_A_CONFIRMAR].confirmado and st[ATIVO_A_CONFIRMAR].fonte.startswith("especificacao")


@pytest.mark.parametrize("digitado", ["", "nao", "WINZ26", "sim"])
def test_perfil_confirmacao_errada_recusa_sem_alterar(repo, digitado):
    arq = repo / "config/instrumentos.toml"
    antes = arq.read_bytes()
    aud = RegistradorAuditoria(repo / "aud")
    with pytest.raises(ConfirmacaoRecusada, match="nada foi alterado"):
        _confirmar(repo, ATIVO_A_CONFIRMAR, digitado=digitado, auditoria=aud)
    assert arq.read_bytes() == antes and not _aud(repo / "aud")


def test_perfil_recusa_responsavel_pendente_e_fonte_ausente(repo):
    arq = repo / "config/instrumentos.toml"
    antes = arq.read_bytes()
    with pytest.raises(ConfirmacaoRecusada, match="responsavel"):
        _confirmar(repo, ATIVO_A_CONFIRMAR, responsavel="pendente")
    with pytest.raises(ConfirmacaoRecusada, match="fonte"):
        _confirmar(repo, ATIVO_A_CONFIRMAR, fonte="")
    with pytest.raises(ConfirmacaoRecusada, match="AAAA-MM-DD"):
        _confirmar(repo, ATIVO_A_CONFIRMAR, fonte_data="01/10/2026")
    with pytest.raises(ConfirmacaoRecusada, match="nao tem secao"):
        _confirmar(repo, "XYZ9")
    assert arq.read_bytes() == antes


def test_perfil_compara_com_terminal_duble_confere_e_diverge(repo):
    from robowin.core.relogio import RelogioSimulado
    from robowin.mt5.duble import criar_duble_padrao
    t0 = int(dt.datetime(2026, 10, 28, 15, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    arq = repo / "config/instrumentos.toml"
    # divergencia: o perfil diz um valor do ponto diferente do que a corretora (duble) reporta
    original = tomllib.loads(arq.read_text(encoding="utf-8"))[ATIVO_A_CONFIRMAR]["ponto_valor_brl"]
    txt = arq.read_text(encoding="utf-8")
    alterado = atualizar_secao(txt, ATIVO_A_CONFIRMAR, {"ponto_valor_brl": original * 2})
    arq.write_text(alterado, encoding="utf-8")
    d = criar_duble_padrao(RelogioSimulado(t0), n_ticks=50)
    with pytest.raises(ConfirmacaoRecusada, match="DIVERGEM"):
        _confirmar(repo, ATIVO_A_CONFIRMAR, terminal=d)
    assert arq.read_text(encoding="utf-8") == alterado
    # perfil igual ao reportado pelo terminal: confirma, registrando a origem (DUBLE) na auditoria
    arq.write_text(txt, encoding="utf-8")
    d2 = criar_duble_padrao(RelogioSimulado(t0), n_ticks=50)
    res = _confirmar(repo, ATIVO_A_CONFIRMAR, terminal=d2, auditoria=RegistradorAuditoria(repo / "aud"))
    assert res.alterado
    term = _aud(repo / "aud")[0]["dados"]["terminal"]
    assert term["origem"].startswith("DUBLE") and term["simbolo"].startswith(ATIVO_A_CONFIRMAR)


def test_confirma_bolsa_e_avisa_bolsa_pendente(repo):
    bol = repo / "config/bolsas.toml"
    bolsas = tomllib.loads(bol.read_text(encoding="utf-8"))
    pendente = next(k for k, v in bolsas.items() if v.get("a_confirmar"))
    res = _confirmar(repo, f"bolsa:{pendente.lower()}")
    assert res.alterado
    novo = tomllib.loads(bol.read_text(encoding="utf-8"))
    assert novo[pendente]["a_confirmar"] is False and novo[pendente]["sessoes"] == bolsas[pendente]["sessoes"]
    assert {k: v for k, v in novo.items() if k != pendente} == {k: v for k, v in bolsas.items() if k != pendente}
    with pytest.raises(ConfirmacaoRecusada, match="so se aplica"):
        _confirmar(repo, "bolsa:B3", terminal=object())
    # ativo apontando para bolsa ainda a_confirmar: confirma o ativo, mas avisa que segue bloqueado pela bolsa
    outra = next(k for k, v in novo.items() if v.get("a_confirmar"))
    instr = repo / "config/instrumentos.toml"
    instr.write_text(atualizar_secao(instr.read_text(encoding="utf-8"), ATIVO_A_CONFIRMAR, {"bolsa": outra}),
                     encoding="utf-8")
    res = _confirmar(repo, ATIVO_A_CONFIRMAR)
    assert res.alterado and any(f"bolsa:{outra}" in m for m in res.mensagens)
    st = {s.codigo: s for s in listar_status(instr, bol)}
    assert st[ATIVO_A_CONFIRMAR].confirmado and st[ATIVO_A_CONFIRMAR].bloqueado_por_bolsa == outra


def test_edicao_toml_recusa_o_que_nao_sabe_editar():
    with pytest.raises(ErroEdicaoToml, match="nao encontrada"):
        atualizar_secao('[A]\nx = 1\n', "B", {"x": 2})
    with pytest.raises(ErroEdicaoToml, match="multilinha"):
        atualizar_secao('[A]\nx = """a\nb"""\n', "A", {"y": 2})
    # chave existente com valor multilinha e substituida por inteiro; subtabela seguinte preservada
    t = '[A]\n# comentario\nx = ["a",\n     "b]"]  # fim\ny = 1\n[[A.t]]\nz = 2\n[B]\nx = 9\n'
    out = atualizar_secao(t, "A", {"x": [], "novo": "v"})
    assert tomllib.loads(out) == {"A": {"x": [], "y": 1, "novo": "v", "t": [{"z": 2}]}, "B": {"x": 9}}
    assert "# comentario" in out


def test_cli_perfil_listar_e_confirmar(repo, monkeypatch, capsys):
    base = ["--instrumentos", str(repo / "config/instrumentos.toml"), "--bolsas", str(repo / "config/bolsas.toml"),
            "--config", str(repo / "config/robowin.toml")]
    assert main(["perfil", "listar", *base]) == 0
    out = capsys.readouterr().out
    assert "A CONFIRMAR" in out and f"robowin perfil confirmar {ATIVO_A_CONFIRMAR}" in out
    monkeypatch.setattr("builtins.input", lambda _p="": "errado")
    rc = main(["perfil", "confirmar", ATIVO_A_CONFIRMAR, "--responsavel", "Fulana", "--fonte", "B3",
               "--auditoria", str(repo / "aud"), *base])
    assert rc != 0 and "RECUSADO" in capsys.readouterr().out
    rc = main(["perfil", "confirmar", ATIVO_A_CONFIRMAR, "--responsavel", "Fulana", "--fonte", "B3",
               "--confirmo", ATIVO_A_CONFIRMAR, "--auditoria", str(repo / "aud"), *base])
    assert rc == 0 and "CONFIRMADO" in capsys.readouterr().out


def test_perfis_reais_nao_foram_confirmados_por_agente():
    """Os perfis a_confirmar do repositorio seguem a_confirmar (so o usuario confirma)."""
    instr = tomllib.loads((RAIZ / "config/instrumentos.toml").read_text(encoding="utf-8"))
    assert instr[ATIVO_A_CONFIRMAR]["a_confirmar"] is True
    assert not any("confirmado_por" in v for v in instr.values() if isinstance(v, dict))


# =========================================================================================== pendencias ==
def test_pendencias_listam_o_que_falta_com_comando_e_somem_apos_resolver(repo, monkeypatch):
    from robowin.verificacao.pendencias import formatar_pendencias, listar_pendencias
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "valor-que-nunca-aparece")
    texto = formatar_pendencias(listar_pendencias(repo))
    assert "robowin risco aprovar --responsavel" in texto and "[OBRIGATORIA] Aprovacao da politica de risco" in texto
    assert f"robowin perfil confirmar {ATIVO_A_CONFIRMAR} " in texto and "setx ANTHROPIC_API_KEY" in texto
    assert "OPENAI_API_KEY" not in texto and "valor-que-nunca-aparece" not in texto
    aprovar_politica(repo / "config/risco.toml", repo / "config/aprovacoes", "Fulana", "", confirmar=lambda _r: "APROVO")
    _confirmar(repo, ATIVO_A_CONFIRMAR)
    texto = formatar_pendencias(listar_pendencias(repo))
    assert "risco aprovar" not in texto and f"confirmar {ATIVO_A_CONFIRMAR} " not in texto
