"""Pre-registro: hash estavel, cadeia quebrada detectada, elegibilidade do pregao, n_tentativas e CLI."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from robowin.avaliacao import preregistro as prr
from robowin.cli import main
from robowin.core.tipos import PreRegistro

NS = 1_000_000_000
T_REG = int(dt.datetime(2026, 10, 29, 15, tzinfo=dt.UTC).timestamp()) * NS  # 12h em Sao Paulo


def _pr(id_="exp-a", t=T_REG, variantes=None, **kw) -> PreRegistro:
    return PreRegistro(id_experimento=id_, t_registro_ns=t, inicio_avaliacao=kw.pop("inicio", "2026-10-30"),
                       variantes=variantes or {"planejador": "h1", "planejador_revisor": "h2"},
                       comparacoes_primarias=("planejador-claude_gpt_cond",),
                       margens_nao_inferioridade={"pior_dia": 1.5, "drawdown": 2}, n_pregoes_alvo=40, **kw)


def test_hash_estavel_e_sensivel():
    a = prr.calcular_hash(_pr())
    assert a == prr.calcular_hash(_pr(variantes={"planejador_revisor": "h2", "planejador": "h1"}))
    assert a != prr.calcular_hash(_pr(alfa=0.1))
    assert a != prr.calcular_hash(_pr(hash_anterior="x"))
    # hash_registro nao entra no proprio hash
    assert a == prr.calcular_hash(_pr(hash_registro="qualquer"))


def test_registrar_encadeia_e_verifica(tmp_path):
    r1 = prr.registrar(_pr("exp-a"), tmp_path)
    r2 = prr.registrar(_pr("exp-b", T_REG + NS, variantes={"planejador_v2": "h3"}), tmp_path)
    assert r1.hash_anterior == prr.GENESIS and r2.hash_anterior == r1.hash_registro
    assert r1.hash_registro == prr.calcular_hash(r1)
    assert [x.id_experimento for x in prr.verificar(tmp_path)] == ["exp-a", "exp-b"]
    assert prr.ler_toml(tmp_path / "exp-a.toml") == r1
    assert prr.n_tentativas(tmp_path) == 3
    assert prr.verificar(tmp_path / "vazio") == [] and prr.n_tentativas(tmp_path / "vazio") == 0


def test_registro_duplicado_ou_invalido_recusado(tmp_path):
    prr.registrar(_pr("exp-a"), tmp_path)
    with pytest.raises(prr.ErroPreRegistro, match="ja registrado"):
        prr.registrar(_pr("exp-a", T_REG + NS), tmp_path)
    with pytest.raises(prr.ErroPreRegistro, match="anterior"):
        prr.registrar(_pr("exp-b", T_REG - NS), tmp_path)
    with pytest.raises(prr.ErroPreRegistro, match="invalido"):
        prr.registrar(_pr("../fora"), tmp_path)
    with pytest.raises(prr.ErroPreRegistro, match="sem variantes"):
        prr.registrar(_pr("exp-c", variantes={}).model_copy(update={"variantes": {}}), tmp_path)


@pytest.mark.parametrize("adulterar", ["campo", "remover", "toml", "reordenar", "envelope_id", "envelope_t",
                                       "envelope_hash_anterior", "chave_extra"])
def test_cadeia_quebrada_detectada(tmp_path, adulterar):
    prr.registrar(_pr("exp-a"), tmp_path)
    prr.registrar(_pr("exp-b", T_REG + NS), tmp_path)
    cadeia = tmp_path / prr.ARQUIVO_CADEIA
    linhas = cadeia.read_text(encoding="utf-8").splitlines()
    if adulterar == "campo":
        reg = json.loads(linhas[0])
        reg["registro"]["alfa"] = 0.2
        linhas[0] = json.dumps(reg)
    elif adulterar.startswith("envelope_") or adulterar == "chave_extra":
        reg = json.loads(linhas[0])  # o hash cobre so ``registro``: o envelope precisa ser conferido a parte
        if adulterar == "envelope_id":
            reg["id_experimento"] = "exp-x"
        elif adulterar == "envelope_t":
            reg["t_registro_ns"] += 1
        elif adulterar == "envelope_hash_anterior":
            reg["hash_anterior"] = "f" * 64
        else:
            reg["nota"] = "acrescentada a mao"
        linhas[0] = json.dumps(reg)
    elif adulterar == "remover":
        linhas = linhas[1:]
    elif adulterar == "reordenar":
        linhas = linhas[::-1]
    if adulterar == "toml":
        arq = tmp_path / "exp-a.toml"
        arq.write_text(arq.read_text(encoding="utf-8").replace("n_pregoes_alvo = 40", "n_pregoes_alvo = 41"),
                       encoding="utf-8")
    else:
        cadeia.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    with pytest.raises(prr.ErroPreRegistro):
        prr.verificar(tmp_path)
    with pytest.raises(prr.ErroPreRegistro):  # nao acrescenta sobre cadeia quebrada
        prr.registrar(_pr("exp-c", T_REG + 2 * NS), tmp_path)


def test_elegibilidade(tmp_path):
    pr = prr.registrar(_pr("exp-a"), tmp_path)
    hs = {"planejador": "h1", "planejador_revisor": "h2"}
    assert prr.elegivel(pr, data_pregao="2026-10-30", hashes_variantes=hs, provedor_falso=False) == (True, ())
    ok, m = prr.elegivel(pr, data_pregao="2026-10-29", hashes_variantes=hs, provedor_falso=False)
    assert not ok and "pregao_nao_posterior_ao_registro" in m
    ok, m = prr.elegivel(pr, data_pregao="2026-10-30", hashes_variantes={**hs, "planejador": "x"},
                         provedor_falso=True)
    assert not ok and "hash_divergente:planejador" in m and "provedor_falso" in m
    ok, m = prr.elegivel(pr, data_pregao="2026-10-30", hashes_variantes={"planejador": "h1", "outra": "z"},
                         provedor_falso=False)
    assert "variante_sem_hash:planejador_revisor" in m and "variante_nao_registrada:outra" in m
    tardio = prr.registrar(_pr("exp-b", T_REG + NS, inicio="2026-11-05"), tmp_path)
    ok, m = prr.elegivel(tardio, data_pregao="2026-11-03", hashes_variantes=hs, provedor_falso=False)
    assert m == ("pregao_antes_do_inicio_da_avaliacao",)
    sem_hash = _pr("exp-x")
    assert "registro_sem_hash_valido" in prr.elegivel(sem_hash, data_pregao="2026-11-03", hashes_variantes=hs,
                                                      provedor_falso=False)[1]


def test_componentes_e_variantes_padrao(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "robowin.toml").write_text(
        "[planejador]\nativo = true\n[planejador.gatilhos]\nperiodo_s = 240\n[prazos]\n"
        "planejador = { proponente_s = 60 }\n", encoding="utf-8")
    c = prr.hashes_componentes(tmp_path)
    assert c["prompt:planejador"] == "ausente" and c["config:planejador"] != "ausente"
    v = prr.variantes_padrao(c)
    (tmp_path / "prompts" / "revisor_plano_v1.md").write_text("revisor", encoding="utf-8")
    v2 = prr.variantes_padrao(prr.hashes_componentes(tmp_path))
    assert v2["planejador"] == v["planejador"] and v2["planejador_revisor"] != v["planejador_revisor"]


def test_cli_registrar_verificar_e_adulteracao(tmp_path, capsys):
    d = str(tmp_path / "exp")
    assert main(["experimento", "registrar", "--id", "exp-teste", "--dir", d]) == 0
    assert main(["experimento", "verificar", "--dir", d]) == 0
    assert main(["experimento", "registrar", "--id", "exp-teste", "--dir", d]) == 1
    cadeia = tmp_path / "exp" / prr.ARQUIVO_CADEIA
    cadeia.write_text(cadeia.read_text(encoding="utf-8").replace('"alfa": 0.05', '"alfa": 0.5'), encoding="utf-8")
    assert main(["experimento", "verificar", "--dir", d]) == 1
    assert "QUEBRADA" in capsys.readouterr().out


def test_hash_do_codigo_e_da_fonte_e_cobre_sombra_seeds_e_linha_de_base(tmp_path, monkeypatch):
    """dsl_gestao/compilador/correcoes sao o hash da FONTE (antes: sha do texto do rotulo, constante): editar o
    codigo depois do registro diverge. Entram tambem a regra da sombra trailing, as seeds e os prompts de B/C."""
    c = prr.hashes_componentes(tmp_path)
    for k in prr.VERSOES_FIXAS:
        assert c[k] != prr._sha(k)  # nunca o sha do rotulo
    assert {"regra:sombra_trailing", "seeds", "prompt:proponente", "prompt:revisor", "esquema:PropostaIA",
            "esquema:RevisaoIA", "codigo:linha_de_base"} <= set(c)
    v = prr.variantes_padrao(c)
    assert set(v) == {"planejador", "planejador_revisor", "claude_gpt_cond"}
    assert prr.variantes_padrao(prr.hashes_componentes(tmp_path, seed=8))["planejador"] != v["planejador"]
    # editar a fonte da DSL de gestao muda o hash da variante
    real = prr._sha_fonte
    monkeypatch.setattr(prr, "_sha_fonte", lambda m: "editado" if m == "robowin.decisao.gestao" else real(m))
    c2 = prr.hashes_componentes(tmp_path)
    assert c2["dsl_gestao:v1"] != c["dsl_gestao:v1"] and prr.variantes_padrao(c2)["planejador"] != v["planejador"]
    # o prompt de C (linha de base da H1) tambem
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "revisor_v1.md").write_text("outro revisor", encoding="utf-8")
    monkeypatch.setattr(prr, "_sha_fonte", real)
    v3 = prr.variantes_padrao(prr.hashes_componentes(tmp_path))
    assert v3["claude_gpt_cond"] != v["claude_gpt_cond"] and v3["planejador"] == v["planejador"]


def test_manifesto_usa_os_mesmos_hashes_de_codigo_do_preregistro():
    from robowin.decisao.correcoes import CORRECOES_V11
    cod = prr.hashes_codigo()
    assert len(set(cod.values())) == len(cod)
    assert all(x in CORRECOES_V11 for x in ("C7", "C8", "C9", "C19", "veto_teto_tempo_posicao"))
