"""Negocios publicos da B3: parser (formato exato), cancelamentos, fuso, lado inferido, agregacao, ZIP, download
com novas tentativas (transporte injetado, sem rede) e recusa de mistura real/sintetico."""
from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

import numpy as np
import pytest
from b3_fixture import PASTA_FIXOS, escrever_zip, linhas_dia, zip_de_texto
from perfis_teste import WIN

from robowin.cli import main
from robowin.dados.b3_negocios import (
    FLAG_BUY,
    FLAG_SELL,
    ConfigB3,
    ErroFormatoB3,
    baixar_b3,
    baixar_pregao,
    config_b3,
    importar_arquivo,
    caminho_proveniencia,
    lado_regra_do_tick,
    ler_arquivo,
    origem_do_armazem,
    registrar_proveniencia,
    url_da_b3,
    verificar_proveniencia,
    validar_zip,
)
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.dados.ticks import ArmazenamentoTicks, MisturaDeDados

FIXO = PASTA_FIXOS / "23-10-2026_NEGOCIOSAVISTA_WINZ26.txt"
D = dt.date(2026, 10, 23)
CFG = ConfigB3(url_negocios="https://exemplo.invalido/{ticker}/{data}", atraso_entrega_ms=50.0, espera_inicial_s=0.0)


def _utc_ns(hh: int, mm: int, ss: int, ms: int) -> int:
    # HoraFechamento em Brasilia (UTC-3): 09:00 local = 12:00 UTC
    x = dt.datetime(2026, 10, 23, hh + 3, mm, ss, ms * 1000, tzinfo=dt.UTC) - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return x // dt.timedelta(microseconds=1) * 1000


def test_config_do_toml_do_projeto():
    from robowin.core.config import carregar_toml
    c = config_b3(carregar_toml(Path(__file__).resolve().parents[2] / "config" / "robowin.toml"))
    assert "{ticker}" in c.url_negocios and "{data}" in c.url_negocios and c.tentativas == 5
    assert c.fuso_utc_horas == -3 and c.agregacao == "ms_preco" and c.atraso_entrega_ms > 0
    with pytest.raises(ValueError):
        ConfigB3(agregacao="outra")


def test_regra_do_tick():
    s = lado_regra_do_tick(np.array([100, 100, 105, 105, 100, 100, 110, 110.0]))
    assert s.tolist() == [0, 0, 1, 1, -1, -1, 1, 1]
    assert lado_regra_do_tick(np.array([])).tolist() == []


def test_parser_formato_exato_cancelamento_fuso_lado_agregacao(tmp_path):
    arm = ArmazenamentoTicks(tmp_path / "arm")
    r = importar_arquivo(FIXO, arm, WIN, CFG)
    assert r.ticker == "WINZ26" and r.linhas == 11 and r.outros_instrumentos == 1 and r.invalidas == 0
    q = r.dias[0]
    assert q["data"] == "2026-10-23" and q["linhas_do_dia"] == 10
    assert q["linhas_atualizacao"] == 1 and q["negocios_cancelados"] == 1 and q["negocios_validos"] == 8
    assert q["ticks_gravados"] == 6 and q["lado_inferido"] is True and q["livro"] is False
    assert q["primeiro"] == "09:00:59.146" and q["ultimo"] == "17:59:59.999"
    assert q["lado"] == {"compra": 4, "venda": 2, "desconhecido": 2}
    assert any("INCOMPLETO" in a for a in q["achados"])
    # fixo sem registro de proveniencia (URL B3 + sha256): dado de mercado, mas origem DESCONHECIDA (C24)
    assert arm.eh_real and not arm.eh_sintetico and arm.origens_reais() == ["desconhecida"]
    assert r.origem == q["origem"] == "desconhecida" and not q["proveniencia"]["verificada"]
    assert origem_do_armazem(arm) == "desconhecida"
    tab = arm.ler_tabela("WINZ26", D)
    assert len(tab) == 6
    # 1o grupo: mesmo ms e preco agregados (250 + 5), lado desconhecido (sem variacao anterior), sem cotacao
    g0 = {c: tab[c].iloc[0].item() for c in tab.columns}  # por coluna: int64 nao vira float
    assert g0["last"] == 207190.0 and g0["volume"] == 255.0 and g0["bid"] == 0.0 and g0["ask"] == 0.0
    assert g0["t_utc_ns"] == _utc_ns(9, 0, 59, 146)
    assert g0["t_recebido_ns"] - g0["t_utc_ns"] == 50_000_000
    assert g0["t_servidor_ms"] * 1_000_000 == g0["t_utc_ns"]
    # subida = compra agressora: ask = preco, bid = preco - 1 tick do perfil
    g1 = {c: tab[c].iloc[1].item() for c in tab.columns}
    assert g1["last"] == 207195.0 and g1["flags"] & FLAG_BUY and g1["ask"] == 207195.0
    assert g1["bid"] == 207195.0 - WIN.tick_pontos
    # descida: venda, agregada no mesmo ms (3 + 4); o cancelado (207180, id 70) sumiu
    vendas = tab[(tab["flags"] & FLAG_SELL) != 0]
    assert vendas["volume"].tolist() == [7.0] and vendas["bid"].tolist() == [207185.0]
    assert 207180.0 not in tab["last"].tolist()
    # apos o cancelamento o anterior e 207185: 207190 sobe => compra
    assert tab["last"].iloc[4] == 207190.0 and tab["flags"].iloc[4] & FLAG_BUY
    assert arm.ler_meta("WINZ26", "b3_negocios_2026-10-23")["negocios_cancelados"] == 1


def test_sem_agregacao_mantem_cada_negocio(tmp_path):
    arm = ArmazenamentoTicks(tmp_path / "arm")
    r = importar_arquivo(FIXO, arm, WIN, ConfigB3(atraso_entrega_ms=0.0, agregacao="nenhuma"))
    tab = arm.ler_tabela("WINZ26", D)
    assert len(tab) == 8 == r.dias[0]["ticks_gravados"]
    assert (tab["t_recebido_ns"] == tab["t_utc_ns"]).all()


def test_zip_e_reimportacao_idempotente(tmp_path):
    z = zip_de_texto(tmp_path / "x.zip", FIXO.name, FIXO.read_text(encoding="utf-8"))
    assert validar_zip(z) is None
    arm = ArmazenamentoTicks(tmp_path / "arm")
    importar_arquivo(z, arm, WIN, CFG)
    importar_arquivo(z, arm, WIN, CFG)  # substitui o dia: nao duplica
    assert len(arm.ler_tabela("WINZ26", D)) == 6
    lido = ler_arquivo(z, tamanho_bloco=1000)
    assert lido.nome_txt == FIXO.name and lido.ticker == "WINZ26"


def test_leitura_em_blocos_igual_a_leitura_inteira(tmp_path):
    z = escrever_zip(tmp_path / "z", "WINZ26", D, n=5000, seed=3, cancelar=7)
    a = ler_arquivo(z, tamanho_bloco=1000)
    b = ler_arquivo(z, tamanho_bloco=10_000_000)
    assert a.linhas == b.linhas == 5007
    for campo in ("t_ms", "preco", "qtd", "ident", "acao"):
        assert np.array_equal(getattr(a, campo), getattr(b, campo))
    arm = ArmazenamentoTicks(tmp_path / "arm")
    q = importar_arquivo(z, arm, WIN, CFG).dias[0]
    assert q["negocios_cancelados"] == 7 and q["negocios_validos"] == 5000 - 7
    tab = arm.ler_tabela("WINZ26", D)
    assert tab["volume"].sum() == q["volume_total"]
    assert (np.diff(tab["t_utc_ns"].to_numpy()) >= 0).all()


def test_linhas_invalidas_contadas_e_cabecalho_errado(tmp_path):
    txt = FIXO.read_text(encoding="utf-8").splitlines()
    ruim = txt[:2] + ["2026-10-23;WINZ26;0;xyz;1;090101000;999;1;2026-10-23;3;88",
                      "2026-10-23;WINZ26;0;207190,000;1;250000000;998;1;2026-10-23;3;88"] + txt[2:]
    p = tmp_path / "23-10-2026_NEGOCIOSAVISTA_WINZ26.txt"
    p.write_text("\n".join(ruim) + "\n", encoding="utf-8")
    assert ler_arquivo(p).invalidas == 2
    (tmp_path / "c.txt").write_text("a;b\n1;2\n", encoding="utf-8")
    with pytest.raises(ErroFormatoB3, match="colunas ausentes"):
        ler_arquivo(tmp_path / "c.txt")


def test_recusa_contrato_de_outro_ativo(tmp_path):
    p = tmp_path / "23-10-2026_NEGOCIOSAVISTA_WDOX26.txt"
    p.write_text(FIXO.read_text(encoding="utf-8").replace("WINZ26", "WDOX26"), encoding="utf-8")
    with pytest.raises(ErroFormatoB3, match="nao pertence"):
        importar_arquivo(p, ArmazenamentoTicks(tmp_path / "arm"), WIN, CFG)


def test_recusa_misturar_real_e_sintetico(tmp_path):
    sint = ArmazenamentoTicks(tmp_path / "sint")
    gerar(ConfigSintetico.do_perfil(WIN, n_ticks=200)).gravar(sint)
    with pytest.raises(MisturaDeDados):
        importar_arquivo(FIXO, sint, WIN, CFG)
    real = ArmazenamentoTicks(tmp_path / "real")
    importar_arquivo(FIXO, real, WIN, CFG)
    with pytest.raises(MisturaDeDados):
        gerar(ConfigSintetico.do_perfil(WIN, n_ticks=200)).gravar(real)
    assert not real.eh_sintetico


class _Transporte:
    """Transporte FALSO: roteiro de respostas por chamada ('cai', 'truncado', 'ok', 404, 503...)."""

    def __init__(self, roteiro, conteudo: bytes):
        self.roteiro, self.conteudo, self.urls = list(roteiro), conteudo, []

    def __call__(self, url, destino, timeout):
        self.urls.append(url)
        acao = self.roteiro.pop(0) if self.roteiro else "ok"
        if acao == "cai":
            raise ConnectionResetError("conexao caiu no meio do ZIP")
        if acao == "truncado":
            Path(destino).write_bytes(self.conteudo[: len(self.conteudo) // 2] + b"\0" * 70_000)
            return 200
        if isinstance(acao, int):
            Path(destino).write_bytes(b"<html>nao ha</html>")
            return acao
        Path(destino).write_bytes(self.conteudo)
        return 200


def _zip_bytes(tmp_path: Path) -> bytes:
    z = escrever_zip(tmp_path / "orig", "WINZ26", D, n=20_000, seed=1)
    return z.read_bytes()


def test_download_com_novas_tentativas_e_cache(tmp_path):
    conteudo = _zip_bytes(tmp_path)
    tr = _Transporte(["cai", "truncado", 503, "ok"], conteudo)
    esperas: list[float] = []
    cfg = ConfigB3(url_negocios="https://b3.invalido/rapinegocios/tickercsv/{ticker}/{data}", espera_inicial_s=2.0)
    r = baixar_pregao(WIN, D, cfg, tmp_path / "cache", transporte=tr, dormir=esperas.append)
    assert r.status == "ok" and r.tentativas == 4 and r.ticker == "WINZ26"
    assert esperas == [2.0, 4.0, 8.0]  # espera crescente
    assert tr.urls[0] == "https://b3.invalido/rapinegocios/tickercsv/WINZ26/2026-10-23"
    assert validar_zip(Path(r.caminho)) is None
    r2 = baixar_pregao(WIN, D, cfg, tmp_path / "cache", transporte=tr, dormir=esperas.append)
    assert r2.status == "cache" and len(tr.urls) == 4


def test_download_sem_dados_e_falha(tmp_path):
    cfg = ConfigB3(url_negocios="https://b3.invalido/{ticker}/{data}", espera_inicial_s=0.0, tentativas=3)
    r = baixar_pregao(WIN, D, cfg, tmp_path / "c", transporte=_Transporte([404], b""), dormir=lambda s: None)
    assert r.status == "sem_dados" and r.tentativas == 1
    r = baixar_pregao(WIN, D, cfg, tmp_path / "c", transporte=_Transporte(["cai"] * 5, b""), dormir=lambda s: None)
    assert r.status == "falha" and r.tentativas == 3 and "rede" in r.motivo
    assert not list((tmp_path / "c").rglob("*.zip"))  # nada invalido no cache


def test_contrato_vigente_de_cada_pregao_e_ultimos_disponiveis(tmp_path):
    conteudo = _zip_bytes(tmp_path)
    vistos: list[str] = []

    def tr(url, destino, timeout):
        vistos.append(url)
        if url.endswith("2026-10-20"):  # um dia sem dados no meio: pulado
            Path(destino).write_bytes(b"{}")
            return 404
        Path(destino).write_bytes(conteudo)
        return 200

    cfg = ConfigB3(url_negocios="u/{ticker}/{data}", espera_inicial_s=0.0)
    res = baixar_b3(WIN, cfg, tmp_path / "c", ultimos=3, ate=dt.date(2026, 10, 21), transporte=tr, dormir=lambda s: None)
    ok = [r.data.isoformat() for r in res if r.disponivel]
    assert ok == ["2026-10-16", "2026-10-19", "2026-10-21"]
    assert "u/WINZ26/2026-10-20" in vistos
    # antes do vencimento de outubro (quarta mais proxima do dia 15), o vigente era o V26
    res = baixar_b3(WIN, cfg, tmp_path / "c2", de=dt.date(2026, 10, 9), ate=dt.date(2026, 10, 13), transporte=tr,
                    dormir=lambda s: None)
    assert {r.ticker for r in res} == {"WINV26"} and len(res) == 2  # 12/10 e feriado (pregao: 09 e 13)


def test_cli_importar_e_relatorio(tmp_path, capsys):
    z = escrever_zip(tmp_path / "zips", "WINZ26", D, n=500, seed=2, cancelar=2)
    rel = tmp_path / "q.md"
    cod = main(["dados", "importar-b3", "--ativo", "WIN", "--arquivo", str(z.parent), "--armazem",
                str(tmp_path / "arm"), "--relatorio", str(rel)])
    out = capsys.readouterr().out
    assert cod == 0 and "cancelados=2" in out and "lado INFERIDO" in out
    assert "lado agressor INFERIDO" in rel.read_text(encoding="utf-8")
    assert ArmazenamentoTicks(tmp_path / "arm").eh_real
    sint = tmp_path / "sint"
    gerar(ConfigSintetico.do_perfil(WIN, n_ticks=100)).gravar(ArmazenamentoTicks(sint))
    assert main(["dados", "importar-b3", "--ativo", "WIN", "--arquivo", str(z), "--armazem", str(sint)]) == 2


def test_linhas_dia_formato():
    lin = linhas_dia("WINZ26", D, n=3, seed=0)
    campos = lin[0].split(";")
    assert len(campos) == 11 and campos[3].endswith(",000") and len(campos[5]) == 9


def test_zip_com_dois_txt_recusado(tmp_path):
    z = tmp_path / "dois.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("a.txt", "x")
        f.writestr("b.txt", "y")
    with pytest.raises(ErroFormatoB3, match="1 arquivo"):
        ler_arquivo(z)


def test_quebra_de_linha_crlf_e_bom(tmp_path):
    texto = "﻿" + FIXO.read_text(encoding="utf-8").replace("\n", "\r\n")
    p = tmp_path / FIXO.name
    p.write_bytes(texto.encode("utf-8"))
    a = importar_arquivo(p, ArmazenamentoTicks(tmp_path / "a"), WIN, CFG).dias[0]
    b = importar_arquivo(FIXO, ArmazenamentoTicks(tmp_path / "b"), WIN, CFG).dias[0]
    for k in ("negocios_validos", "negocios_cancelados", "ticks_gravados", "volume_total", "lado"):
        assert a[k] == b[k]


def test_proveniencia_real_so_com_url_b3_e_sha256(tmp_path):
    z = escrever_zip(tmp_path / "z", "WINZ26", D, n=500, seed=3)
    # sem registro: desconhecida
    assert importar_arquivo(z, ArmazenamentoTicks(tmp_path / "a0"), WIN, CFG).origem == "desconhecida"
    # URL fora da B3: desconhecida
    registrar_proveniencia(z, "https://exemplo.invalido/WINZ26/2026-10-23")
    assert verificar_proveniencia(z)["origem"] == "desconhecida"
    # URL da B3 + sha256 conferido: real
    registrar_proveniencia(z, "https://arquivos.b3.com.br/rapinegocios/tickercsv/WINZ26/2026-10-23")
    arm = ArmazenamentoTicks(tmp_path / "a1")
    r = importar_arquivo(z, arm, WIN, CFG)
    assert r.origem == "real_b3_negocios" and r.dias[0]["proveniencia"]["verificada"]
    assert arm.origens_reais() == ["real_b3_negocios"] and origem_do_armazem(arm) == "real_b3_negocios"
    # arquivo trocado depois do registro: sha256 nao confere => desconhecida (e o armazem deixa de ser so B3)
    outro = escrever_zip(tmp_path / "z2", "WINZ26", D, n=600, seed=4)
    caminho_proveniencia(outro).write_bytes(caminho_proveniencia(z).read_bytes())
    p = verificar_proveniencia(outro)
    assert p["origem"] == "desconhecida" and "sha256" in p["motivo"]
    importar_arquivo(outro, arm, WIN, CFG)
    assert origem_do_armazem(arm) == "desconhecida"


def test_url_da_b3():
    assert url_da_b3("https://arquivos.b3.com.br/x") and url_da_b3("https://b3.com.br/")
    assert not url_da_b3("http://arquivos.b3.com.br/x")  # sem TLS
    assert not url_da_b3("https://b3.com.br.invalido/x") and not url_da_b3("https://fakeb3.com.br/x")
    assert not url_da_b3("")


def test_download_registra_proveniencia(tmp_path):
    conteudo = _zip_bytes(tmp_path)
    cfg = ConfigB3(url_negocios="https://arquivos.b3.com.br/rapinegocios/tickercsv/{ticker}/{data}",
                   espera_inicial_s=0.0)
    r = baixar_pregao(WIN, D, cfg, tmp_path / "cache", transporte=_Transporte(["ok"], conteudo),
                      dormir=lambda s: None)
    p = verificar_proveniencia(Path(r.caminho))
    assert p["verificada"] and p["url"].endswith("/WINZ26/2026-10-23")
    assert importar_arquivo(Path(r.caminho), ArmazenamentoTicks(tmp_path / "a"), WIN, CFG).origem == "real_b3_negocios"
