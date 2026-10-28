"""Gerador de arquivos de negocios no formato EXATO da B3 (rapinegocios/tickercsv) para os testes, sem rede.

Os precos sao um passeio aleatorio SINTETICO na grade de tick do perfil (nao e dado de mercado); o que importa
aqui e o FORMATO (';', decimal ',', HoraFechamento HHMMSSmmm em Brasilia, AcaoAtualizacao, ZIP com 1 .txt)."""
from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

import numpy as np

CABECALHO = ("DataReferencia;CodigoInstrumento;AcaoAtualizacao;PrecoNegocio;QuantidadeNegociada;HoraFechamento;"
             "CodigoIdentificadorNegocio;TipoSessaoPregao;DataNegocio;CodigoParticipanteComprador;"
             "CodigoParticipanteVendedor")
PASTA_FIXOS = Path(__file__).resolve().parent / "dados_b3"


def hora_b3(ms_do_dia: int) -> str:
    s, mmm = divmod(int(ms_do_dia), 1000)
    return f"{s // 3600:02d}{(s // 60) % 60:02d}{s % 60:02d}{mmm:03d}"


def preco_b3(p: float) -> str:
    return f"{p:.3f}".replace(".", ",")


def linhas_dia(ticker: str, d: dt.date, *, n: int = 3000, seed: int = 0, inicio_s: int = 9 * 3600,
               fim_s: int = 11 * 3600, preco0: float = 130000.0, tick: float = 5.0, cancelar: int = 0,
               perturbar_apos_s: int | None = None) -> list[str]:
    """Linhas (sem cabecalho) de um pregao: rajadas no mesmo ms, passeio na grade e `cancelar` cancelamentos.

    `perturbar_apos_s`: a partir deste horario (s do dia, Brasilia) os precos sobem 100 ticks e as quantidades
    mudam (o PASSADO fica identico byte a byte) - usado no teste de causalidade."""
    rng = np.random.default_rng(seed)
    ms = np.sort(rng.integers(inicio_s * 1000, fim_s * 1000, n))
    rajada = rng.random(n) < 0.3
    for i in range(1, n):
        if rajada[i]:
            ms[i] = ms[i - 1]
    passos = rng.choice([-1, 0, 0, 0, 1], size=n)
    preco = preco0 + np.cumsum(passos) * tick
    qtd = rng.choice([1, 1, 1, 2, 3, 5, 10], size=n)
    if perturbar_apos_s is not None:
        fut = ms >= perturbar_apos_s * 1000
        preco = np.where(fut, preco + 100 * tick, preco)
        qtd = np.where(fut, qtd * 3 + 1, qtd)
    ds = d.isoformat()
    out = [f"{ds};{ticker};0;{preco_b3(preco[i])};{int(qtd[i])};{hora_b3(int(ms[i]))};{10 * (i + 1)};1;{ds};3;88"
           for i in range(n)]
    for k in rng.choice(n, size=min(cancelar, n), replace=False):
        out.append(f"{ds};{ticker};2;{preco_b3(preco[k])};{int(qtd[k])};{hora_b3(int(ms[k]))};{10 * (k + 1)};1;{ds};3;88")
    return out


def nome_txt(ticker: str, d: dt.date) -> str:
    return f"{d:%d-%m-%Y}_NEGOCIOSAVISTA_{ticker}.txt"


def escrever_txt(pasta: Path, ticker: str, d: dt.date, **kw) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    p = pasta / nome_txt(ticker, d)
    p.write_text(CABECALHO + "\n" + "\n".join(linhas_dia(ticker, d, **kw)) + "\n", encoding="utf-8")
    return p


def escrever_zip(pasta: Path, ticker: str, d: dt.date, **kw) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    z = pasta / f"{d.isoformat()}.zip"
    texto = CABECALHO + "\n" + "\n".join(linhas_dia(ticker, d, **kw)) + "\n"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as f:
        f.writestr(nome_txt(ticker, d), texto)
    return z


def zip_de_texto(destino: Path, nome: str, texto: str) -> Path:
    with zipfile.ZipFile(destino, "w", compression=zipfile.ZIP_DEFLATED) as f:
        f.writestr(nome, texto)
    return destino
