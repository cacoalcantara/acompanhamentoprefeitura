"""Limites de risco POR USUARIO e POR CONTA: isolamento, usuario novo bloqueado, alteracao invalida o hash, aprovacao
expressa, auditoria, conta REAL sempre bloqueada, IA sem acesso aos limites, conta divergente bloqueia e modo de
arquivo unico compativel. Sem rede e sem chave de API."""
from __future__ import annotations

import argparse
import ast
import asyncio
import datetime as dt
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from perfis_teste import WIN

from robowin.auditoria.cadeia import RegistradorAuditoria, verificar_diretorio
from robowin.cli import main
from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.config import ErroConfig, carregar_toml, sha256_arquivo
from robowin.core.ids import novo_id
from robowin.core.tipos import Acao, ConfigRisco, Intencao, Posicao
from robowin.risco.aprovar import AprovacaoRecusada
from robowin.risco.motor import MotorRiscoPadrao
from robowin.risco.politica import risco_do_ativo
from robowin.risco.usuarios import (
    APROVADO,
    INVALIDO,
    NAO_APROVADO,
    SEM_LIMITES,
    ArmazemUsuarios,
    AutorizacaoRealAusente,
    ChaveConta,
    ContaDivergente,
    ErroUsuario,
    config_de_formulario,
    modelo_inicial,
    risco_para,
    selecao_de,
    texto_risco,
    validar_ia_sem_limites,
)

RAIZ = Path(__file__).resolve().parents[2]
CONTA_A = ChaveConta.de_texto("mt5:CORRETORA-DEMO:111111")
CONTA_B = ChaveConta.de_texto("mt5:OUTRA-DEMO:222222")
APROVO = lambda _r: "APROVO"  # noqa: E731


def _limites(**kw) -> ConfigRisco:
    return modelo_inicial(WIN).model_copy(update=kw)


@pytest.fixture
def arm(tmp_path) -> ArmazemUsuarios:
    return ArmazemUsuarios(tmp_path / "usuarios", RegistradorAuditoria(tmp_path / "auditoria"))


def _usuario_com_conta(arm: ArmazemUsuarios, u: str, chave: ChaveConta, tipo: str = "demo") -> str:
    tok = arm.criar_usuario(u)
    arm.registrar_conta(u, chave, tipo)
    return tok


def _eventos(tmp_path) -> list[dict]:
    import json
    out = []
    for arq in sorted((tmp_path / "auditoria").glob("auditoria-*.jsonl")):
        out += [json.loads(ln) for ln in arq.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


# ======================================================================================= chave da conta ==
def test_chave_conta_parse_valida_e_confere():
    c = ChaveConta.de_texto(" MT5:Corretora-Demo:123 ")
    assert (c.plataforma, c.servidor, c.login, c.texto) == ("mt5", "Corretora-Demo", 123, "mt5:Corretora-Demo:123")
    assert c.confere("mt5", "Corretora-Demo", 123) == ""
    assert "login" in c.confere("mt5", "Corretora-Demo", 124) and "servidor" in c.confere("mt5", "X", 123)
    assert "plataforma" in c.confere("mt4", "Corretora-Demo", 123)
    for ruim in ("mt5:srv", "mt9:srv:1", "mt5:../x:1", "mt5:srv:-1", "mt5:srv:abc", "mt5:a b:1"):
        with pytest.raises(ErroUsuario):
            ChaveConta.de_texto(ruim)


def test_nome_de_usuario_invalido_e_recusado(arm):
    for ruim in ("", "A", "../x", "a/b", "Maiusculo", "x" * 40):
        with pytest.raises(ErroUsuario):
            arm.criar_usuario(ruim)


# ======================================================================================= usuario novo ==
def test_usuario_novo_comeca_sem_limites_e_bloqueado(arm, tmp_path):
    tok = _usuario_com_conta(arm, "ana", CONTA_A)
    assert len(tok) >= 24 and tok not in (arm.raiz / "ana" / "usuario.toml").read_text(encoding="utf-8")  # so o hash
    assert arm.autenticar("ana", tok) and not arm.autenticar("ana", tok + "x") and not arm.autenticar("bia", tok)
    assert arm.limites("ana", CONTA_A) is None  # nada imposto
    st = arm.estado("ana", CONTA_A)
    assert st.estado == SEM_LIMITES and not st.pode_operar_demo and not st.pode_operar_real
    with pytest.raises(ErroConfig, match="SEM_LIMITES"):
        arm.exigir_operacao("ana", CONTA_A, WIN)
    # simulacao roda, mas com o modelo conservador e o erro (=> rotulo risco_nao_aprovado), nunca com o risco-v2
    cfg, erro = arm.risco_simulacao("ana", CONTA_A, WIN)
    assert erro and cfg == modelo_inicial(WIN)
    legado = ConfigRisco(**carregar_toml(RAIZ / "config/risco.toml"))
    assert cfg.perda_max_dia_brl < legado.perda_max_dia_brl and cfg.perda_max_operacao_brl < legado.perda_max_operacao_brl
    with pytest.raises(AprovacaoRecusada, match="sem limites"):
        arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)


def test_modelo_inicial_e_muito_conservador_e_coerente():
    m = modelo_inicial(WIN)
    legado = ConfigRisco(**carregar_toml(RAIZ / "config/risco.toml"))
    assert m.contratos_max == 1 and m.operacoes_dia_max <= 3 and m.perdas_seguidas_max <= 2
    for campo in ("perda_max_operacao_brl", "perda_max_dia_brl", "perda_max_semana_brl", "dd_max_brl", "stop_max_pontos"):
        assert getattr(m, campo) < getattr(legado, campo), campo


def test_usuario_inexistente_ou_conta_nao_registrada(arm):
    with pytest.raises(ErroUsuario, match="nao existe"):
        arm.estado("ze", CONTA_A)
    arm.criar_usuario("ze")
    with pytest.raises(ErroUsuario, match="nao registrada"):
        arm.estado("ze", CONTA_A)
    with pytest.raises(ErroUsuario, match="ja existe"):
        arm.criar_usuario("ze")


# ======================================================================================= isolamento ==
def test_isolamento_entre_dois_usuarios_e_duas_contas(arm):
    _usuario_com_conta(arm, "ana", CONTA_A)
    arm.registrar_conta("ana", CONTA_B)
    _usuario_com_conta(arm, "bia", CONTA_A)  # MESMA conta (login+servidor+plataforma), outro usuario
    arm.gravar_limites("ana", CONTA_A, _limites(perda_max_dia_brl=50.0), autor="ana", origem="teste")
    arm.gravar_limites("ana", CONTA_B, _limites(perda_max_dia_brl=60.0), autor="ana", origem="teste")
    arm.gravar_limites("bia", CONTA_A, _limites(perda_max_dia_brl=70.0), autor="bia", origem="teste")
    arm.aprovar("ana", CONTA_A, "Ana", "teste", confirmar=APROVO)
    assert arm.estado("ana", CONTA_A).estado == APROVADO
    assert arm.estado("ana", CONTA_B).estado == NAO_APROVADO  # outra conta da mesma pessoa
    assert arm.estado("bia", CONTA_A).estado == NAO_APROVADO  # mesma conta, outro usuario
    assert arm.exigir_operacao("ana", CONTA_A, WIN).perda_max_dia_brl == 50.0
    with pytest.raises(ErroConfig):
        arm.exigir_operacao("bia", CONTA_A, WIN)
    arm.aprovar("bia", CONTA_A, "Bia", "teste", confirmar=APROVO)
    assert arm.exigir_operacao("bia", CONTA_A, WIN).perda_max_dia_brl == 70.0
    assert arm.limites("ana", CONTA_B).perda_max_dia_brl == 60.0
    assert [c.chave for c in arm.contas("ana")] == sorted([CONTA_A, CONTA_B], key=lambda c: c.pasta)
    assert [c.chave for c in arm.contas("bia")] == [CONTA_A]


def test_pasta_copiada_de_outro_usuario_e_recusada(arm):
    _usuario_com_conta(arm, "ana", CONTA_A)
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    arm.criar_usuario("bia")
    shutil.copytree(arm.raiz / "ana" / "contas", arm.raiz / "bia" / "contas")  # copia a aprovacao da ana
    with pytest.raises(ErroUsuario, match="nao corresponde"):
        arm.exigir_operacao("bia", CONTA_A, WIN)


# ======================================================================================= alteracao ==
def test_alteracao_invalida_hash_e_exige_nova_aprovacao(arm, tmp_path):
    _usuario_com_conta(arm, "ana", CONTA_A)
    sha1 = arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    r1 = arm.aprovar("ana", CONTA_A, "Ana", "primeira", confirmar=APROVO)
    assert r1.arquivo.name == "risco-v1.toml" and arm.estado("ana", CONTA_A).estado == APROVADO
    assert arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t") == sha1  # sem mudanca: nada novo
    sha2 = arm.gravar_limites("ana", CONTA_A, _limites(operacoes_dia_max=2), autor="ana", origem="t")
    assert sha2 != sha1
    st = arm.estado("ana", CONTA_A)
    assert st.estado == NAO_APROVADO and "hash difere" in st.detalhe
    with pytest.raises(ErroConfig):
        arm.exigir_operacao("ana", CONTA_A, WIN)
    # edicao manual do arquivo (fora do painel/CLI) tambem invalida
    r2 = arm.aprovar("ana", CONTA_A, "Ana", "segunda", confirmar=APROVO)
    assert r2.arquivo.name == "risco-v2.toml" and r2.sha256 == sha2
    risco = arm.conta("ana", CONTA_A).risco
    risco.write_text(risco.read_text(encoding="utf-8").replace("operacoes_dia_max = 2", "operacoes_dia_max = 1"),
                     encoding="utf-8")
    assert arm.estado("ana", CONTA_A).estado == NAO_APROVADO
    aprs = arm.aprovacoes("ana", CONTA_A)
    assert [a["arquivo"] for a in aprs] == ["risco-v2.toml", "risco-v1.toml"] and not any(a["vigente"] for a in aprs)
    hist = arm.historico_limites("ana", CONTA_A)
    assert [h["seq"] for h in hist] == [1, 2] and list(hist[1]["alterados"]) == ["operacoes_dia_max"]
    assert sha256_arquivo(arm.conta("ana", CONTA_A).dir / "historico" / "risco-0002.toml") == sha2


def test_limites_incoerentes_ou_incompletos_nao_sao_gravados(arm):
    _usuario_com_conta(arm, "ana", CONTA_A)
    vals = {k: str(v) for k, v in _limites().model_dump().items()}
    for ruim in ({**vals, "perda_max_dia_brl": "10"},  # dia < operacao
                 {**vals, "contratos_max": "5"},  # acima do teto da v1
                 {k: v for k, v in vals.items() if k != "dd_max_brl"},  # campo faltando: nada e imposto por padrao
                 {**vals, "perda_max_semana_brl": "abc"}, {**vals, "campo_estranho": "1"}):
        with pytest.raises(ErroConfig):
            arm.gravar_limites("ana", CONTA_A, ruim, autor="ana", origem="t")
    assert arm.limites("ana", CONTA_A) is None
    assert config_de_formulario({**vals, "perda_max_operacao_brl": "30,5"}).perda_max_operacao_brl == 30.5


def test_texto_risco_e_deterministico_e_le_de_volta():
    cfg = _limites(perda_max_dia_brl=77.5)
    assert texto_risco(cfg) == texto_risco(ConfigRisco(**cfg.model_dump()))
    import tomllib
    assert ConfigRisco(**tomllib.loads(texto_risco(cfg))) == cfg


# ======================================================================================= auditoria ==
def test_todas_as_configuracoes_alteracoes_e_aprovacoes_vao_para_a_auditoria(arm, tmp_path):
    _usuario_com_conta(arm, "ana", CONTA_A)
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "m", confirmar=APROVO)
    arm.gravar_limites("ana", CONTA_A, _limites(perdas_seguidas_max=1), autor="ana", origem="t")
    ev = _eventos(tmp_path)
    assert [e["tipo"] for e in ev] == ["usuario_criado", "conta_registrada", "risco_limites_alterados",
                                       "risco_aprovado", "risco_limites_alterados"]
    assert all(e["dados"]["usuario"] == "ana" for e in ev)
    assert ev[3]["dados"]["conta"] == CONTA_A.texto and ev[3]["dados"]["escopo"] == "somente_demo"
    assert ev[4]["dados"]["alterados"] == {"perdas_seguidas_max": [2, 1]}
    ok, msgs = verificar_diretorio(tmp_path / "auditoria")
    assert ok, msgs
    reg = carregar_toml(arm.conta("ana", CONTA_A).dir_aprovacoes / "risco-v1.toml")
    assert reg["usuario"] == "ana" and reg["conta"] == CONTA_A.texto and reg["autorizacao_real"] == "bloqueada"


def test_recusa_de_aprovacao_nao_grava_nada(arm, tmp_path):
    _usuario_com_conta(arm, "ana", CONTA_A)
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    n = len(_eventos(tmp_path))
    for digitado, resp in (("aprovo", "Ana"), ("", "Ana"), ("APROVO", "pendente"), ("APROVO", "")):
        with pytest.raises(AprovacaoRecusada):
            arm.aprovar("ana", CONTA_A, resp, "", confirmar=lambda _r, d=digitado: d)
    assert not arm.aprovacoes("ana", CONTA_A) and len(_eventos(tmp_path)) == n


# ======================================================================================= conta REAL ==
def test_conta_real_sempre_bloqueada(arm):
    real = ChaveConta.de_texto("mt5:CORRETORA-REAL:333333")
    _usuario_com_conta(arm, "ana", real, "real")
    arm.gravar_limites("ana", real, _limites(), autor="ana", origem="t")
    with pytest.raises(AutorizacaoRealAusente):
        arm.aprovar("ana", real, "Ana", "", confirmar=APROVO)
    with pytest.raises(AutorizacaoRealAusente, match="autorizacao_real"):
        arm.exigir_operacao("ana", real, WIN)
    # mesmo adulterando conta.toml (autorizacao_real = "concedida"), o estado continua "bloqueada"
    toml = arm.conta("ana", real).dir / "conta.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('"bloqueada"', '"concedida"'), encoding="utf-8")
    c = arm.conta("ana", real)
    assert c.autorizacao_real_bruta == "concedida" and c.autorizacao_real == "bloqueada"
    st = arm.estado("ana", real)
    assert st.autorizacao_real == "bloqueada" and not st.pode_operar_real and not st.pode_operar_demo
    with pytest.raises(AutorizacaoRealAusente):
        arm.exigir_operacao("ana", real, WIN)
    # e uma conta demo aprovada tambem nunca pode operar em real
    _ = arm.registrar_conta("ana", CONTA_A)
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    assert arm.estado("ana", CONTA_A).pode_operar_demo and not arm.estado("ana", CONTA_A).pode_operar_real


def test_conta_com_tipo_desconhecido_e_tratada_como_real(arm):
    _usuario_com_conta(arm, "ana", CONTA_A)
    toml = arm.conta("ana", CONTA_A).dir / "conta.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('tipo = "demo"', 'tipo = "xyz"'), encoding="utf-8")
    assert arm.conta("ana", CONTA_A).tipo == "real"


# ======================================================================================= limite atingido ==
def _intencao(t, stop=20.0, qtd=1):
    return Intencao(id=novo_id("int"), braco="b1", acao=Acao.COMPRAR, quantidade=qtd, stop_pontos=stop,
                    validade_ns=t + int(60e9), preco_referencia=130000.0)


def test_limites_do_usuario_bloqueiam_ao_serem_atingidos(arm):
    _usuario_com_conta(arm, "ana", CONTA_A)
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    cfg = arm.exigir_operacao("ana", CONTA_A, WIN)
    m = MotorRiscoPadrao(cfg, WIN.instrumento("WINZ26"))
    t = dt_utc_ns(dt.datetime(2026, 11, 10, 13, 0))
    assert m.avaliar(_intencao(t), Posicao(simbolo="WINZ26"), t).aprovada
    # stop cujo pior caso passa da perda por operacao DO USUARIO (o do arquivo unico aceitaria)
    legado = ConfigRisco(**carregar_toml(RAIZ / "config/risco.toml"))
    stop_grande = cfg.stop_max_pontos + 10
    assert stop_grande < legado.stop_max_pontos
    assert not m.avaliar(_intencao(t, stop=stop_grande), Posicao(simbolo="WINZ26"), t).aprovada
    assert not m.avaliar(_intencao(t, qtd=2), Posicao(simbolo="WINZ26"), t).aprovada
    m.registrar_trade("b1", -cfg.perda_max_dia_brl, 1, t + 1)  # perda do dia atingida
    d = m.avaliar(_intencao(t + 2), Posicao(simbolo="WINZ26"), t + 2)
    assert not d.aprovada


# ======================================================================================= IA sem limites ==
def test_config_de_ia_nao_pode_definir_limites(tmp_path):
    validar_ia_sem_limites(carregar_toml(RAIZ / "config/robowin.toml"))  # config do repositorio: ok
    for ruim in ({"ia": {"perda_max_dia_brl": 1000}}, {"ia": {"papeis": {"x": {"modelo": "a:b", "contratos_max": 5}}}},
                 {"bracos": [{"id": "D", "risco": {"dd_max_brl": 1}}]}, {"reflexao": {"limites": {}}},
                 {"decisao": {"stop_max_pontos": 900}}):
        with pytest.raises(ErroConfig, match="IA nao pode"):
            validar_ia_sem_limites(ruim)
    from robowin.decisao.simulacao import ParamsSimulacao, carregar_ambiente
    cfg = tmp_path / "robowin.toml"
    cfg.write_text('[ia]\norcamento_mensal_usd = 1\nperda_max_operacao_brl = 9999.0\n', encoding="utf-8")
    with pytest.raises(ErroConfig, match="IA nao pode"):
        carregar_ambiente(ParamsSimulacao(config=cfg, catalogo=RAIZ / "config/modelos.toml"))


MODULOS_SEM_RISCO = ("ia", "decisao/bracos", "noticias")
PROIBIDOS = ("robowin.risco.usuarios", "robowin.risco.aprovar", "robowin.risco.politica", "robowin.core.config")


def test_modulos_de_ia_nao_leem_nem_alteram_limites():
    """Estatico: o codigo da IA (provedores, papeis, prompts, bracos, noticias) nao importa o armazem de limites, a
    aprovacao nem a politica, e nao cita os arquivos de risco. `core.config` so e aceito para `carregar_toml`/`papeis`."""
    src = RAIZ / "src" / "robowin"
    for mod in MODULOS_SEM_RISCO:
        for arq in (src / mod).rglob("*.py"):
            texto = arq.read_text(encoding="utf-8")
            assert "risco.toml" not in texto and "aprovacoes/" not in texto and "usuarios/" not in texto, arq
            for no in ast.walk(ast.parse(texto)):
                if isinstance(no, ast.ImportFrom):
                    base = ("robowin." if no.level else "") + (no.module or "")
                    nomes = {a.name for a in no.names}
                    if base.endswith("core.config"):
                        assert nomes <= {"carregar_toml", "papeis", "ErroConfig"}, (arq, nomes)
                        continue
                    assert not any(base.endswith(p.removeprefix("robowin.")) for p in PROIBIDOS[:3]), (arq, base)
                    assert not (base.endswith("risco") and nomes & {"usuarios", "aprovar", "politica"}), (arq, nomes)
                elif isinstance(no, ast.Import):
                    assert not any(a.name in PROIBIDOS[:3] for a in no.names), arq
                elif isinstance(no, ast.Name):
                    assert no.id not in ("ArmazemUsuarios", "gravar_limites", "aprovar_politica"), arq


# ======================================================================================= selecao e compat ==
def test_modo_arquivo_unico_continua_igual(tmp_path):
    sel = selecao_de(RAIZ / "config/robowin.toml", usuarios=tmp_path / "u")
    assert not sel.ativa and "arquivo unico" in sel.descricao()
    a = risco_para(sel, WIN, RAIZ / "config/risco.toml", RAIZ / "config/aprovacoes/risco-v1.toml", ao_vivo=False)
    b = risco_do_ativo(WIN, RAIZ / "config/risco.toml", RAIZ / "config/aprovacoes/risco-v1.toml")
    assert a[:2] == b and a[2] == RAIZ / "config/risco.toml"
    with pytest.raises(ErroUsuario, match="juntos"):
        selecao_de(None, usuario="ana")
    with pytest.raises(ErroUsuario, match="juntos"):
        selecao_de(None, conta=CONTA_A.texto)


def test_selecao_pelo_toml_e_risco_para(arm, tmp_path):
    _usuario_com_conta(arm, "ana", CONTA_A)
    cfg = tmp_path / "config" / "robowin.toml"
    cfg.parent.mkdir()
    cfg.write_text(f'[geral]\nusuario = "ana"\nconta_negociacao = "{CONTA_A.texto}"\nusuarios = "{arm.raiz.as_posix()}"\n',
                   encoding="utf-8")
    sel = selecao_de(cfg)
    assert sel.ativa and sel.usuario == "ana" and sel.chave == CONTA_A and sel.raiz.resolve() == arm.raiz.resolve()
    with pytest.raises(ErroConfig):
        risco_para(sel, WIN, "x", None, ao_vivo=True)  # sem limites: ao vivo recusa
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    r, erro, arq = risco_para(sel, WIN, "x", None, ao_vivo=True)
    assert erro is None and r == _limites() and arq == arm.conta("ana", CONTA_A).risco


def test_simulacao_e_nucleo_carregam_os_limites_do_usuario(arm, tmp_path):
    from robowin.decisao.simulacao import ParamsSimulacao, carregar_risco_simulacao
    _usuario_com_conta(arm, "ana", CONTA_A)
    p = ParamsSimulacao(config=tmp_path / "nao.toml", usuario="ana", conta_negociacao=CONTA_A.texto,
                        dir_usuarios=arm.raiz)
    cfg, erro = carregar_risco_simulacao(p, WIN)
    assert erro and cfg == modelo_inicial(WIN)  # simulacao: roda rotulada
    with pytest.raises(ErroConfig):
        carregar_risco_simulacao(p, WIN, ao_vivo=True)  # nucleo ao vivo: recusa
    arm.gravar_limites("ana", CONTA_A, _limites(perda_max_dia_brl=55.0), autor="ana", origem="t")
    cfg, erro = carregar_risco_simulacao(p, WIN)
    assert erro and cfg.perda_max_dia_brl == 55.0  # nao aprovado: os do proprio usuario, rotulado
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    cfg, erro = carregar_risco_simulacao(p, WIN, ao_vivo=True)
    assert erro is None and cfg.perda_max_dia_brl == 55.0


# ======================================================================================= migracao ==
def test_migracao_da_aprovacao_legada_e_provisoria_somente_demo(arm, tmp_path):
    _usuario_com_conta(arm, "titular", CONTA_A)
    risco, apr = RAIZ / "config/risco.toml", RAIZ / "config/aprovacoes/risco-v1.toml"
    with pytest.raises(AprovacaoRecusada):
        arm.migrar_legado("titular", CONTA_A, risco, apr, confirmar=lambda _r: "nao")
    assert arm.limites("titular", CONTA_A) is None
    destino = arm.migrar_legado("titular", CONTA_A, risco, apr, confirmar=APROVO)
    reg = carregar_toml(destino)
    assert reg["provisoria"] is True and reg["escopo"] == "somente_demo" and reg["origem"] == "risco-v2.toml"
    st = arm.estado("titular", CONTA_A)
    assert st.estado == APROVADO and st.provisoria
    assert arm.exigir_operacao("titular", CONTA_A, WIN) == ConfigRisco(**carregar_toml(risco))
    assert "risco_migrado_legado" in [e["tipo"] for e in _eventos(tmp_path)]
    with pytest.raises(AprovacaoRecusada, match="ja tem"):
        arm.migrar_legado("titular", CONTA_A, risco, apr, confirmar=APROVO)
    # outro usuario NAO herda nada do titular
    _usuario_com_conta(arm, "outro", CONTA_A)
    assert arm.estado("outro", CONTA_A).estado == SEM_LIMITES
    real = ChaveConta.de_texto("mt5:X-REAL:9")
    arm.registrar_conta("titular", real, "real")
    with pytest.raises(AutorizacaoRealAusente):
        arm.migrar_legado("titular", real, risco, apr, confirmar=APROVO)


def test_estado_invalido_quando_aprovacao_ilegivel(arm):
    _usuario_com_conta(arm, "ana", CONTA_A)
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    risco = arm.conta("ana", CONTA_A).risco
    sha = sha256_arquivo(risco)
    risco.write_text(risco.read_text(encoding="utf-8").replace("contratos_max = 1", "contratos_max = 9"),
                     encoding="utf-8")
    ap = arm.conta("ana", CONTA_A).dir_aprovacoes / "risco-v2.toml"
    ap.write_text(f'sha256 = "{sha256_arquivo(risco)}"\nresponsavel = "Ana"\n', encoding="utf-8")  # forja o hash
    assert sha != sha256_arquivo(risco)
    assert arm.estado("ana", CONTA_A).estado == INVALIDO  # contratos_max acima do teto: incoerente


# ======================================================================================= gateway / nucleo ==
def _args_gateway(arm, *, usuario="ana", conta=CONTA_A.texto, duble=False):
    return argparse.Namespace(config="nao_existe.toml", usuario=usuario, conta=conta, usuarios=str(arm.raiz),
                              risco=str(RAIZ / "config/risco.toml"), aprovacao=str(RAIZ / "config/aprovacoes/risco-v1.toml"),
                              duble=duble)


def test_gateway_usa_limites_do_usuario_e_recusa_conta_divergente(arm):
    from robowin.mt5.cli_cmds import limites_do_gateway
    from robowin.mt5.guardiao import ConfigGuardiao
    _usuario_com_conta(arm, "ana", CONTA_A)
    a = _args_gateway(arm)
    with pytest.raises(ErroConfig):  # terminal real sem aprovacao: recusa
        limites_do_gateway(a, WIN, "mt5", CONTA_A.login, CONTA_A.servidor)
    a.duble = True  # duble sintetico: segue rotulado
    _, origem = limites_do_gateway(a, WIN, "mt5", CONTA_A.login, CONTA_A.servidor)
    assert "risco_nao_aprovado" in origem
    a.duble = False
    arm.gravar_limites("ana", CONTA_A, _limites(perda_max_dia_brl=44.0), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    cfg, origem = limites_do_gateway(a, WIN, "mt5", CONTA_A.login, CONTA_A.servidor)
    g = ConfigGuardiao.de_risco(cfg)
    assert g.perda_max_dia_brl == 44.0 and g.stop_max_pontos == cfg.stop_max_pontos and "aprovado" in origem
    for plat, srv, lg in (("mt5", CONTA_A.servidor, 999), ("mt5", "OUTRO", CONTA_A.login), ("mt4", CONTA_A.servidor,
                                                                                            CONTA_A.login)):
        with pytest.raises(ContaDivergente, match="difere"):
            limites_do_gateway(a, WIN, plat, lg, srv)
    # modo arquivo unico: limites do config/risco.toml (aprovado provisoriamente)
    legado = _args_gateway(arm, usuario=None, conta=None)
    cfg, _ = limites_do_gateway(legado, WIN, "mt5", 1, "QUALQUER")
    assert cfg == ConfigRisco(**carregar_toml(RAIZ / "config/risco.toml"))


def _orq_falso():
    from robowin.decisao.validade import BloqueioDados
    incidentes = []
    reg = SimpleNamespace(incidente=lambda **kw: incidentes.append(kw))
    return SimpleNamespace(registro=reg, avaliador_odds=None, _agora=lambda: 10**18,
                           bloqueio=BloqueioDados()), incidentes


@pytest.mark.parametrize("conta,trade_mode,plat,bloqueia", [
    ({"login": 111111, "servidor": "CORRETORA-DEMO"}, "demo", "mt5", False),
    ({"login": 999, "servidor": "CORRETORA-DEMO"}, "demo", "mt5", True),
    ({"login": 111111, "servidor": "OUTRA"}, "demo", "mt5", True),
    ({"login": 111111, "servidor": "CORRETORA-DEMO"}, "demo", "mt4", True),
    ({"login": 111111, "servidor": "CORRETORA-DEMO"}, "real", "mt5", True),
    (None, "demo", "mt5", True),
])
def test_nucleo_bloqueia_entradas_com_conta_conectada_diferente_da_aprovada(conta, trade_mode, plat, bloqueia):
    from robowin.decisao.nucleo import FonteLista, Nucleo
    orq, incidentes = _orq_falso()
    n = Nucleo(orq, FonteLista([]), conta_esperada=CONTA_A)  # type: ignore[arg-type]
    n._verificar_integridade()
    assert orq.bloqueio.motivo(orq._agora()) == "conta_divergente"  # antes de o gateway confirmar: bloqueado
    orq.bloqueio = type(orq.bloqueio)()
    estado = {"plataforma": plat, "conta": None if conta is None else {**conta, "trade_mode": trade_mode}}
    asyncio.run(n.tratar("estado", estado))
    n._prox_marca_conta = 0
    n._verificar_integridade()
    assert (orq.bloqueio.motivo(orq._agora()) == "conta_divergente") is bloqueia
    assert bool(incidentes) is bloqueia
    if bloqueia:
        assert "conta aprovada" in incidentes[0]["detalhe"] and incidentes[0]["severidade"] == "critico"


def test_nucleo_sem_usuario_nao_confere_conta():
    from robowin.decisao.nucleo import FonteLista, Nucleo
    orq, incidentes = _orq_falso()
    n = Nucleo(orq, FonteLista([]))  # type: ignore[arg-type]
    asyncio.run(n.tratar("estado", {"plataforma": "mt5", "conta": None}))
    n._verificar_integridade()
    assert orq.bloqueio.motivo(orq._agora()) == "" and not incidentes


# ======================================================================================= CLI ==
def test_cli_usuario_conta_definir_aprovar_status(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p="": "nao")
    u = str(tmp_path / "usuarios")
    aud = str(tmp_path / "aud")
    base = ["--usuarios", u, "--config", str(tmp_path / "nao.toml")]
    assert main(["usuario", "criar", "--usuario", "ana", "--auditoria", aud, *base]) == 0
    assert "Token do usuario" in capsys.readouterr().out
    conta = ["--usuario", "ana", "--conta", CONTA_A.texto]
    assert main(["usuario", "conta", *conta, "--auditoria", aud, *base]) == 0
    assert main(["risco", "status", *conta, *base]) == 1
    assert "SEM_LIMITES" in capsys.readouterr().out
    assert main(["risco", "definir", *conta, "--modelo-inicial", "--config", str(RAIZ / "config/robowin.toml"),
                 "--usuarios", u, "--auditoria", aud]) == 0
    assert main(["risco", "definir", *conta, "--campo", "operacoes_dia_max=2", "--auditoria", aud, *base]) == 0
    assert main(["risco", "definir", *conta, "--campo", "perda_max_dia_brl=1", "--auditoria", aud, *base]) == 1
    assert main(["risco", "aprovar", *conta, "--responsavel", "Ana", "--auditoria", aud, *base]) == 1  # sem APROVO
    capsys.readouterr()
    assert main(["risco", "aprovar", *conta, "--responsavel", "Ana", "--sim-eu-aprovo", "--auditoria", aud, *base]) == 0
    assert main(["risco", "status", *conta, *base]) == 0
    out = capsys.readouterr().out
    assert "APROVADO" in out and "BLOQUEADA" in out
    assert main(["risco", "historico", *conta, *base]) == 0
    assert "[VIGENTE]" in capsys.readouterr().out
    assert main(["usuario", "listar", *base]) == 0
    assert "risco APROVADO" in capsys.readouterr().out
    real = ["--usuario", "ana", "--conta", "mt5:CORRETORA-REAL:5"]
    assert main(["usuario", "conta", *real, "--tipo", "real", "--auditoria", aud, *base]) == 0
    assert main(["risco", "definir", *real, "--modelo-inicial", "--config", str(RAIZ / "config/robowin.toml"),
                 "--usuarios", u, "--auditoria", aud]) == 0
    assert main(["risco", "aprovar", *real, "--responsavel", "Ana", "--sim-eu-aprovo", "--auditoria", aud, *base]) == 1
    assert "REAL" in capsys.readouterr().out
    ok, msgs = verificar_diretorio(aud)
    assert ok, msgs


def test_cli_nucleo_de_usuario_sem_aprovacao_recusa(tmp_path, capsys):
    arm = ArmazemUsuarios(tmp_path / "u")
    _usuario_com_conta(arm, "ana", CONTA_A)
    rc = main(["nucleo", "--usuario", "ana", "--conta", CONTA_A.texto, "--usuarios", str(arm.raiz),
               "--saida", str(tmp_path / "n"), "--duracao-s", "0", "--cmd", "", "--registro-modelos",
               str(tmp_path / "nada.json")])
    assert rc == 2 and "BLOQUEADA" in capsys.readouterr().out


def test_cli_simular_de_usuario_sem_limites_roda_rotulado(tmp_path, capsys):
    arm = ArmazemUsuarios(tmp_path / "u")
    _usuario_com_conta(arm, "ana", CONTA_A)
    saida = tmp_path / "s"
    rc = main(["simular", "--n-ticks", "1500", "--saida", str(saida), "--usuario", "ana", "--conta", CONTA_A.texto,
               "--usuarios", str(arm.raiz), "--sem-condicional"])
    assert rc == 0
    import sqlite3
    with sqlite3.connect(saida / "robowin.db") as con:
        inc = con.execute("SELECT detalhe FROM incidentes WHERE detalhe LIKE 'risco_nao_aprovado%'").fetchall()
    assert inc and "sem limites definidos" in inc[0][0]


# ======================================================================================= verificar / pendencias ==
def test_verificar_e_pendencias_mostram_contas_por_usuario(tmp_path, monkeypatch):
    from robowin.verificacao.checagens import Contexto, risco_por_usuario
    from robowin.verificacao.pendencias import listar_pendencias
    monkeypatch.delenv("ROBOWIN_USUARIOS", raising=False)
    (tmp_path / "config").mkdir()
    for nome in ("robowin.toml", "instrumentos.toml", "bolsas.toml", "risco.toml"):
        shutil.copy(RAIZ / "config" / nome, tmp_path / "config" / nome)
    r = risco_por_usuario(Contexto(tmp_path))
    assert r.estado == "APROVADO" and "arquivo unico" in r.detalhe
    pend = listar_pendencias(tmp_path)
    assert any("PROVISORIA do titular" in p.descricao and p.prioridade == "OPCIONAL" for p in pend)
    arm = ArmazemUsuarios(tmp_path / "config/usuarios")
    _usuario_com_conta(arm, "ana", CONTA_A)
    r = risco_por_usuario(Contexto(tmp_path))
    assert r.estado == "NAO VERIFICAVEL AQUI" and "ana/" in r.detalhe
    pend = listar_pendencias(tmp_path)
    assert any(p.prioridade == "OBRIGATORIA" and f"--usuario ana --conta {CONTA_A.texto}" in p.comando for p in pend)
    arm.gravar_limites("ana", CONTA_A, _limites(), autor="ana", origem="t")
    arm.aprovar("ana", CONTA_A, "Ana", "", confirmar=APROVO)
    assert risco_por_usuario(Contexto(tmp_path)).estado == "APROVADO"
    assert not any("--usuario ana" in p.comando for p in listar_pendencias(tmp_path))
    (tmp_path / "config/robowin.toml").write_text('[ia]\ncontratos_max = 3\n', encoding="utf-8")
    assert risco_por_usuario(Contexto(tmp_path)).estado == "REPROVADO"
