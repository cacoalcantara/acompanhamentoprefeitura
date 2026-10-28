"""Negocios intradiarios PUBLICOS da B3 (``[dados_b3].url_negocios``): download e importacao para o armazem.

*** DADOS REAIS DE MERCADO, mas INCOMPLETOS: so negocios (preco, quantidade, horario). Sem livro de ofertas e sem
lado agressor. Nada aqui envia ordem; serve so para o replay historico causal (``robowin historico``). ***

Formato (um ZIP por contrato e pregao, com um ``DD-MM-AAAA_NEGOCIOSAVISTA_<TICKER>.txt``; ``;`` e decimal ``,``)::

    DataReferencia;CodigoInstrumento;AcaoAtualizacao;PrecoNegocio;QuantidadeNegociada;HoraFechamento;
    CodigoIdentificadorNegocio;TipoSessaoPregao;DataNegocio;CodigoParticipanteComprador;CodigoParticipanteVendedor

* ``HoraFechamento`` = HHMMSSmmm no horario de Brasilia (``[dados_b3].fuso_utc_horas``, UTC-3 sem horario de verao).
* ``AcaoAtualizacao`` 0 = negocio novo; qualquer outro valor (ex. 2) = cancelamento/correcao: o negocio citado
  (mesmo ``CodigoIdentificadorNegocio`` no mesmo dia) e DESCARTADO junto com a linha de atualizacao; ambos contados.
* Lado INFERIDO pela regra do tick (classificacao de microestrutura de Lee-Ready, NAO e indicador tecnico): preco
  maior que o do negocio anterior = compra agressora, menor = venda, igual = repete o lado anterior; o primeiro
  negocio do dia (ou ate a primeira variacao) fica com lado DESCONHECIDO. Rotulado ``lado_inferido`` nos metadados.
* Cotacao INFERIDA (nao ha livro): compra agressora -> ask = preco, bid = preco - 1 tick; venda -> bid = preco,
  ask = preco + 1 tick (spread minimo); lado desconhecido -> bid = ask = 0 (o consumidor usa o ultimo negocio).
  Rotulado ``cotacao_inferida``. Features de livro/microprice/OFI e o market making ficam DESLIGADOS (sem livro).
* Tempo: ``t_utc_ns`` = instante do negocio (UTC); ``t_recebido_ns`` = ``t_utc_ns + atraso_entrega_ms`` (quando o
  dado estaria disponivel ao robo). O replay usa SEMPRE o instante de disponibilidade.
* Agregacao (``[dados_b3].agregacao``): ``ms_preco`` (padrao) soma o volume dos negocios do mesmo milissegundo e
  preco (um agressor costuma bater em varias ordens de uma vez); a contagem original fica nos metadados.
* Leitura em blocos (``pandas.read_csv(chunksize=...)``) direto do ZIP: os ~470 MB de texto nunca ficam inteiros
  na memoria; so os vetores numericos compactos (dezenas de bytes por negocio).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import io
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

import numpy as np
import pandas as pd

from robowin.core.calendario_b3 import SP, contrato_vigente, eh_pregao
from robowin.core.instrumentos import PerfilInstrumento

from .ticks import COLS_TICK, ArmazenamentoTicks

ORIGEM = "real_b3_negocios"
ROTULO_REAL_B3 = ("DADOS REAIS B3 (negocios publicos) - lado agressor INFERIDO pela regra do tick, cotacao "
                  "inferida (spread de 1 tick), SEM livro de ofertas")
FLAG_LAST, FLAG_VOLUME, FLAG_BUY, FLAG_SELL = 8, 16, 32, 64  # mesmas constantes do MT5
COLUNAS_OBRIGATORIAS = ("CodigoInstrumento", "AcaoAtualizacao", "PrecoNegocio", "QuantidadeNegociada",
                        "HoraFechamento", "CodigoIdentificadorNegocio", "DataNegocio")
COLUNA_SESSAO = "TipoSessaoPregao"
AGREGACOES = ("ms_preco", "nenhuma")
RE_NOME_ARQUIVO = re.compile(r"NEGOCIOSAVISTA_([A-Z0-9]+)\.txt$", re.IGNORECASE)
SESSAO_INI_S, SESSAO_FIM_S = 9 * 3600, 18 * 3600 + 25 * 60  # lacunas so contam dentro desta janela (Brasilia)
LACUNA_AVISO_S = 60.0
FIM_ESPERADO_S = 18 * 3600  # ultimo negocio antes disto: pregao possivelmente incompleto (baixado durante o dia?)
ZIP_PEQUENO_BYTES = 64 * 1024  # resposta "valida" mas sem ZIP e pequena: a B3 nao tem o dia (nao insiste)


class ErroFormatoB3(ValueError):
    """Arquivo de negocios fora do formato esperado."""


# ================================================================================================ config
@dataclass(frozen=True)
class ConfigB3:
    url_negocios: str = ""
    tentativas: int = 5
    espera_inicial_s: float = 5  # segundos (int: nao confundir com preco no teste estatico)
    timeout_s: float = 300.0
    cache: str = ""
    fuso_utc_horas: int = -3
    atraso_entrega_ms: float = 50.0
    agregacao: str = "ms_preco"
    tamanho_bloco: int = 500_000

    def __post_init__(self) -> None:
        if self.agregacao not in AGREGACOES:
            raise ValueError(f"[dados_b3].agregacao invalida: {self.agregacao!r} (use {', '.join(AGREGACOES)})")
        if self.atraso_entrega_ms < 0:
            raise ValueError("[dados_b3].atraso_entrega_ms nao pode ser negativo")
        if self.tentativas < 1:
            raise ValueError("[dados_b3].tentativas deve ser >= 1")


def config_b3(cfg: Mapping[str, Any] | None, **sobrepor: Any) -> ConfigB3:
    """Le ``[dados_b3]`` de config/robowin.toml; ``sobrepor`` (valores nao None) vence o arquivo."""
    sec = dict((cfg or {}).get("dados_b3", {}) or {})
    base = ConfigB3()
    kw: dict[str, Any] = {}
    for campo, tipo in (("url_negocios", str), ("tentativas", int), ("espera_inicial_s", float),
                        ("timeout_s", float), ("cache", str), ("fuso_utc_horas", int),
                        ("atraso_entrega_ms", float), ("agregacao", str), ("tamanho_bloco", int)):
        v = sobrepor.get(campo)
        if v is None:
            v = sec.get(campo, getattr(base, campo))
        kw[campo] = tipo(v)
    return ConfigB3(**kw)


# ============================================================================================= download
Transporte = Callable[[str, Path, float], int]
"""Baixa ``url`` para ``destino`` com timeout (s) e devolve o status HTTP. Falha de rede: levanta OSError ou
http.client.HTTPException. Injetavel para testar sem rede."""


def transporte_urllib(url: str, destino: Path, timeout_s: float) -> int:
    """HTTP/1.1 (urllib/http.client), respeitando HTTPS_PROXY. Grava em streaming (blocos de 1 MiB)."""
    req = urllib.request.Request(url, headers={"User-Agent": "robowin/0.1 (simulacao; dados publicos da B3)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r, open(destino, "wb") as f:
            while True:
                bloco = r.read(1 << 20)
                if not bloco:
                    break
                f.write(bloco)
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def validar_zip(caminho: Path) -> str | None:
    """None se o ZIP e integro (CRC de todos os membros) e tem um .txt; senao o motivo."""
    if not caminho.exists():
        return "arquivo inexistente"
    if not zipfile.is_zipfile(caminho):
        return "nao e um ZIP"
    try:
        with zipfile.ZipFile(caminho) as z:
            ruim = z.testzip()
            if ruim is not None:
                return f"ZIP corrompido (CRC) em {ruim}"
            if not any(n.lower().endswith(".txt") for n in z.namelist()):
                return "ZIP sem arquivo .txt"
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        return f"ZIP ilegivel: {type(e).__name__}: {e}"
    return None


@dataclass(frozen=True)
class ResultadoDownload:
    data: dt.date
    ticker: str
    status: str  # ok | cache | sem_dados | falha
    caminho: str = ""
    tentativas: int = 0
    bytes: int = 0
    motivo: str = ""

    @property
    def disponivel(self) -> bool:
        return self.status in ("ok", "cache")

    def linha(self) -> str:
        extra = f" ({self.motivo})" if self.motivo else ""
        tam = f" {self.bytes / 1e6:.1f} MB" if self.bytes else ""
        return f"{self.data} {self.ticker}: {self.status}{tam} tentativas={self.tentativas}{extra}"


def caminho_cache(cache: Path, ticker: str, d: dt.date) -> Path:
    return Path(cache) / ticker / f"{d.isoformat()}.zip"


def baixar_pregao(perfil: PerfilInstrumento, d: dt.date, cfg: ConfigB3, cache: Path, *,
                  transporte: Transporte = transporte_urllib, dormir: Callable[[float], None] = time.sleep
                  ) -> ResultadoDownload:
    """Baixa o ZIP do contrato VIGENTE em `d` (calendario do perfil), com novas tentativas e espera crescente.

    404/4xx ou resposta pequena que nao e ZIP => ``sem_dados`` (sem insistir); queda de conexao, 5xx/429 ou ZIP
    truncado => nova tentativa. So o ZIP integro (``testzip``) entra no cache (troca atomica)."""
    if not cfg.url_negocios:
        raise ValueError("[dados_b3].url_negocios vazio em config/robowin.toml")
    ticker = contrato_vigente(perfil, d)
    destino = caminho_cache(cache, ticker, d)
    if destino.exists() and validar_zip(destino) is None:
        return ResultadoDownload(d, ticker, "cache", str(destino), 0, destino.stat().st_size)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".parcial")
    url = cfg.url_negocios.format(ticker=ticker, data=d.isoformat())
    motivo = ""
    for k in range(1, cfg.tentativas + 1):
        if k > 1:
            dormir(cfg.espera_inicial_s * 2 ** (k - 2))
        try:
            status = transporte(url, tmp, cfg.timeout_s)
        except (OSError, http.client.HTTPException) as e:
            motivo = f"rede: {type(e).__name__}: {e}"[:200]
            continue
        tam = tmp.stat().st_size if tmp.exists() else 0
        if status == 200:
            erro = validar_zip(tmp)
            if erro is None:
                tmp.replace(destino)
                return ResultadoDownload(d, ticker, "ok", str(destino), k, tam)
            if tam < ZIP_PEQUENO_BYTES:
                _apagar(tmp)
                return ResultadoDownload(d, ticker, "sem_dados", "", k, tam, f"resposta sem ZIP ({tam} bytes)")
            motivo = f"{erro} ({tam} bytes)"
            continue
        if status == 429 or status >= 500:
            motivo = f"HTTP {status}"
            continue
        _apagar(tmp)
        return ResultadoDownload(d, ticker, "sem_dados", "", k, 0, f"HTTP {status}")
    _apagar(tmp)
    return ResultadoDownload(d, ticker, "falha", "", cfg.tentativas, 0, motivo)


def _apagar(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def pregao_anterior(d: dt.date, calendario: str) -> dt.date:
    d -= dt.timedelta(days=1)
    while not eh_pregao(d, calendario):
        d -= dt.timedelta(days=1)
    return d


def hoje_sp() -> dt.date:
    return dt.datetime.now(SP).date()


def baixar_b3(perfil: PerfilInstrumento, cfg: ConfigB3, cache: Path, *, de: dt.date | None = None,
              ate: dt.date | None = None, ultimos: int | None = None, transporte: Transporte = transporte_urllib,
              dormir: Callable[[float], None] = time.sleep, ao_progresso: Callable[[ResultadoDownload], None] | None = None,
              hoje: dt.date | None = None) -> list[ResultadoDownload]:
    """Baixa os pregoes de [de, ate] (calendario da bolsa do perfil) ou os `ultimos` N DISPONIVEIS ate `ate`.

    Padrao de `ate`: o pregao anterior a hoje (o de hoje esta em andamento e viria incompleto). Com `ultimos`, anda
    para tras pulando dias sem dados, ate juntar N ou examinar 2N+10 pregoes."""
    cal = perfil.bolsa_perfil().calendario
    ate = ate or pregao_anterior(hoje or hoje_sp(), cal)
    out: list[ResultadoDownload] = []

    def um(d: dt.date) -> ResultadoDownload:
        r = baixar_pregao(perfil, d, cfg, cache, transporte=transporte, dormir=dormir)
        out.append(r)
        if ao_progresso is not None:
            ao_progresso(r)
        return r

    if ultimos is not None:
        if ultimos < 1:
            raise ValueError("--ultimos deve ser >= 1")
        d, ok, vistos = ate, 0, 0
        if not eh_pregao(d, cal):
            d = pregao_anterior(d, cal)
        while ok < ultimos and vistos < 2 * ultimos + 10:
            if de is not None and d < de:
                break
            ok += um(d).disponivel
            vistos += 1
            d = pregao_anterior(d, cal)
        out.sort(key=lambda r: r.data)
        return out
    if de is None:
        raise ValueError("informe --de (e --ate) ou --ultimos")
    if de > ate:
        raise ValueError(f"--de {de} posterior a --ate {ate}")
    d = de
    while d <= ate:
        if eh_pregao(d, cal):
            um(d)
        d += dt.timedelta(days=1)
    return out


# ============================================================================================== leitura
@contextmanager
def abrir_texto(caminho: Path) -> Iterator[tuple[IO[str], str]]:
    """(fluxo de texto, nome do .txt). ZIP: le o membro .txt em streaming, sem extrair para o disco."""
    caminho = Path(caminho)
    if zipfile.is_zipfile(caminho):
        with zipfile.ZipFile(caminho) as z:
            nomes = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if len(nomes) != 1:
                raise ErroFormatoB3(f"{caminho.name}: esperava 1 arquivo .txt no ZIP, achei {len(nomes)}")
            with z.open(nomes[0]) as bruto:
                yield io.TextIOWrapper(bruto, encoding="utf-8", errors="replace", newline=""), Path(nomes[0]).name
    else:
        with open(caminho, encoding="utf-8", errors="replace", newline="") as f:
            yield f, caminho.name


def _cabecalho(linha: str) -> list[str]:
    return [c.strip().lstrip("﻿") for c in linha.strip().split(";")]


@dataclass
class _Bruto:
    """Vetores compactos de um arquivo inteiro (antes de cancelamentos, ordenacao e agregacao)."""
    data: list[np.ndarray] = field(default_factory=list)      # dias desde 1970 (int32)
    t_ms: list[np.ndarray] = field(default_factory=list)      # ms desde a meia-noite LOCAL (int64)
    preco: list[np.ndarray] = field(default_factory=list)
    qtd: list[np.ndarray] = field(default_factory=list)
    ident: list[np.ndarray] = field(default_factory=list)
    acao: list[np.ndarray] = field(default_factory=list)
    sessao: list[np.ndarray] = field(default_factory=list)


@dataclass
class LeituraB3:
    """Resultado da leitura de um arquivo: vetores por negocio + contagens de qualidade."""
    arquivo: str
    nome_txt: str
    ticker: str
    linhas: int
    invalidas: int
    outros_instrumentos: int
    data: np.ndarray
    t_ms: np.ndarray
    preco: np.ndarray
    qtd: np.ndarray
    ident: np.ndarray
    acao: np.ndarray
    sessao: np.ndarray
    instrumentos: dict[str, int]


def ler_arquivo(caminho: Path, *, ticker: str | None = None, tamanho_bloco: int = 500_000) -> LeituraB3:
    """Le o .txt (ou o ZIP) em BLOCOS. `ticker`: so linhas desse CodigoInstrumento (as outras sao contadas).

    Sem `ticker`, usa o do nome do arquivo (``..._NEGOCIOSAVISTA_<TICKER>.txt``) ou o instrumento dominante."""
    caminho = Path(caminho)
    b = _Bruto()
    linhas = invalidas = 0
    instrumentos: dict[str, int] = {}
    with abrir_texto(caminho) as (fluxo, nome_txt):
        primeira = fluxo.readline()
        if not primeira.strip():
            raise ErroFormatoB3(f"{caminho.name}: arquivo vazio")
        nomes = _cabecalho(primeira)
        falta = [c for c in COLUNAS_OBRIGATORIAS if c not in nomes]
        if falta:
            raise ErroFormatoB3(f"{caminho.name}: colunas ausentes {falta} (cabecalho: {nomes})")
        if ticker is None:
            m = RE_NOME_ARQUIVO.search(nome_txt)
            ticker = m.group(1).upper() if m else None
        usar = [c for c in (*COLUNAS_OBRIGATORIAS, COLUNA_SESSAO) if c in nomes]
        leitor = pd.read_csv(fluxo, sep=";", decimal=",", header=None, names=nomes, usecols=usar,
                             dtype={"CodigoInstrumento": str, "DataNegocio": str},
                             chunksize=max(1000, tamanho_bloco), engine="c", on_bad_lines="skip")
        for bloco in leitor:
            linhas += len(bloco)
            inst = bloco["CodigoInstrumento"].fillna("").str.strip()
            for k, v in inst.value_counts().items():
                instrumentos[str(k)] = instrumentos.get(str(k), 0) + int(v)
            if ticker is not None:
                bloco = bloco[inst == ticker]
            invalidas += _acumular(bloco, b)
    if ticker is None:
        if not instrumentos:
            raise ErroFormatoB3(f"{caminho.name}: sem negocios")
        ticker = max(instrumentos, key=lambda k: instrumentos[k])
        raise ErroFormatoB3(f"{caminho.name}: ticker nao identificado pelo nome; informe --ticker "
                            f"(instrumento dominante: {ticker})")
    return LeituraB3(
        arquivo=str(caminho), nome_txt=nome_txt, ticker=ticker, linhas=linhas, invalidas=invalidas,
        outros_instrumentos=sum(v for k, v in instrumentos.items() if k != ticker),
        data=_juntar(b.data, np.int32), t_ms=_juntar(b.t_ms, np.int64), preco=_juntar(b.preco, np.float64),
        qtd=_juntar(b.qtd, np.int64), ident=_juntar(b.ident, np.int64), acao=_juntar(b.acao, np.int16),
        sessao=_juntar(b.sessao, np.int16), instrumentos=instrumentos)


def _juntar(xs: list[np.ndarray], tipo: Any) -> np.ndarray:
    return np.concatenate(xs).astype(tipo, copy=False) if xs else np.zeros(0, tipo)


def _num(s: pd.Series, decimal_virgula: bool = False) -> np.ndarray:
    """Coluna numerica -> float (NaN se invalido). O caminho rapido e o parse do proprio read_csv (decimal ',');
    coluna com lixo vira texto e cai no caminho lento, linha a linha."""
    if pd.api.types.is_numeric_dtype(s.dtype):
        return s.to_numpy(dtype=float, na_value=np.nan)
    x = s.astype(str).str.strip()
    if decimal_virgula:
        x = x.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    return pd.to_numeric(x, errors="coerce").to_numpy(dtype=float)


def _dias_desde_1970(s: pd.Series) -> np.ndarray:
    """'AAAA-MM-DD' -> dias desde 1970 (NaN se invalido); converte so os valores distintos (poucos por arquivo)."""
    cat = s.fillna("").astype(str).str.strip().astype("category")
    conv = pd.to_datetime(pd.Series(cat.cat.categories), format="%Y-%m-%d", errors="coerce")
    dias = ((conv - pd.Timestamp("1970-01-01")) // pd.Timedelta(days=1)).to_numpy(dtype=float)
    codigos = cat.cat.codes.to_numpy()
    return np.where(codigos >= 0, dias[np.maximum(codigos, 0)] if dias.size else np.nan, np.nan)


def _acumular(bloco: pd.DataFrame, b: _Bruto) -> int:
    """Converte um bloco de texto em vetores numericos; devolve quantas linhas eram invalidas."""
    if bloco.empty:
        return 0
    preco = _num(bloco["PrecoNegocio"], True)
    qtd = _num(bloco["QuantidadeNegociada"])
    hora = _num(bloco["HoraFechamento"])
    ident = _num(bloco["CodigoIdentificadorNegocio"])
    acao = _num(bloco["AcaoAtualizacao"])
    sessao = _num(bloco[COLUNA_SESSAO]) if COLUNA_SESSAO in bloco else np.zeros(len(bloco))
    dias = _dias_desde_1970(bloco["DataNegocio"])
    ok = np.isfinite(preco) & np.isfinite(qtd) & np.isfinite(hora) & np.isfinite(ident) & np.isfinite(acao)
    ok &= np.isfinite(dias) & (preco > 0) & (qtd > 0) & (hora >= 0)
    h = np.where(ok, hora, 0).astype(np.int64)
    hh, mm, ss, ms = h // 10_000_000, (h // 100_000) % 100, (h // 1000) % 100, h % 1000
    ok &= (hh < 24) & (mm < 60) & (ss < 60)
    b.data.append(dias[ok].astype(np.int32))
    b.t_ms.append((((hh * 60 + mm) * 60 + ss) * 1000 + ms)[ok])
    b.preco.append(preco[ok])
    b.qtd.append(qtd[ok].astype(np.int64))
    b.ident.append(ident[ok].astype(np.int64))
    b.acao.append(acao[ok].astype(np.int16))
    b.sessao.append(np.nan_to_num(sessao[ok]).astype(np.int16))
    return int((~ok).sum())


# ===================================================================================== conversao por dia
def lado_regra_do_tick(preco: np.ndarray) -> np.ndarray:
    """+1 compra, -1 venda, 0 desconhecido. Sobe = compra; desce = venda; igual = repete o anterior (o primeiro
    negocio e os iguais a ele, antes da primeira variacao, ficam desconhecidos)."""
    n = preco.size
    if n == 0:
        return np.zeros(0, np.int8)
    d = np.sign(np.diff(preco))
    s = np.zeros(n, np.int8)
    s[1:] = d.astype(np.int8)
    idx = np.where(s != 0, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    return s[idx]


@dataclass
class DiaB3:
    data: dt.date
    ticker: str
    tabela: pd.DataFrame  # colunas de COLS_TICK, ordenadas
    qualidade: dict[str, Any]


def converter_dia(leit: LeituraB3, data_dias: int, perfil: PerfilInstrumento, cfg: ConfigB3) -> DiaB3:
    """Negocios de UM dia -> ticks do armazem (cancelamentos fora, regra do tick, cotacao inferida, agregacao)."""
    m = leit.data == data_dias
    t_ms, preco, qtd = leit.t_ms[m], leit.preco[m], leit.qtd[m]
    ident, acao, sessao = leit.ident[m], leit.acao[m], leit.sessao[m]
    d = (dt.date(1970, 1, 1) + dt.timedelta(days=int(data_dias)))
    n_linhas = int(m.sum())
    # cancelamentos/correcoes: descarta a atualizacao E o negocio citado (mesmo id no mesmo dia)
    atualiz = acao != 0
    ids_cancel = np.unique(ident[atualiz])
    citado = (~atualiz) & np.isin(ident, ids_cancel)
    manter = (~atualiz) & (~citado)
    t_ms, preco, qtd, ident, sessao = t_ms[manter], preco[manter], qtd[manter], ident[manter], sessao[manter]
    # duplicatas exatas do mesmo negocio (mesmo id): fica a primeira
    _, primeiro = np.unique(ident, return_index=True)
    dup = ident.size - primeiro.size
    if dup:
        k = np.sort(primeiro)
        t_ms, preco, qtd, ident, sessao = t_ms[k], preco[k], qtd[k], ident[k], sessao[k]
    ordem = np.lexsort((ident, t_ms))
    t_ms, preco, qtd, sessao = t_ms[ordem], preco[ordem], qtd[ordem], sessao[ordem]
    lado = lado_regra_do_tick(preco)
    n_neg = int(t_ms.size)
    if cfg.agregacao == "ms_preco" and n_neg:
        df = pd.DataFrame({"t": t_ms, "p": preco, "q": qtd, "s": lado, "i": np.arange(n_neg)})
        agg = (df.groupby(["t", "p"], sort=False).agg(q=("q", "sum"), s=("s", "first"), i=("i", "first"))
               .reset_index().sort_values(["t", "i"], kind="mergesort"))  # ordem: instante e 1a ocorrencia
        t_ms_g, preco_g = agg["t"].to_numpy(np.int64), agg["p"].to_numpy(float)
        qtd_g, lado_g = agg["q"].to_numpy(np.int64), agg["s"].to_numpy(np.int8)
    else:
        t_ms_g, preco_g, qtd_g, lado_g = t_ms, preco, qtd, lado
    tick = perfil.tick_pontos
    base_ms = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp() * 1000) - cfg.fuso_utc_horas * 3_600_000
    t_utc_ms = base_ms + t_ms_g
    t_utc_ns = t_utc_ms * 1_000_000
    compra, venda = lado_g > 0, lado_g < 0
    bid = np.where(compra, preco_g - tick, np.where(venda, preco_g, 0.0))
    ask = np.where(compra, preco_g, np.where(venda, preco_g + tick, 0.0))
    flags = FLAG_LAST | FLAG_VOLUME | np.where(compra, FLAG_BUY, np.where(venda, FLAG_SELL, 0))
    tabela = pd.DataFrame({
        "t_servidor_ms": t_utc_ms.astype(np.int64), "t_utc_ns": t_utc_ns.astype(np.int64),
        "t_recebido_ns": (t_utc_ns + int(round(cfg.atraso_entrega_ms * 1_000_000))).astype(np.int64),
        "bid": bid.astype(float), "ask": ask.astype(float), "last": preco_g.astype(float),
        "volume": qtd_g.astype(float), "flags": flags.astype(np.int64)})[COLS_TICK]
    q = _qualidade(d, leit, t_ms, preco, qtd, lado, sessao, n_linhas=n_linhas,
                   atualizacoes=int(atualiz.sum()), cancelados=int(citado.sum()), duplicados=int(dup),
                   ticks=len(tabela), cfg=cfg)
    return DiaB3(d, leit.ticker, tabela, q)


def _hhmmss(ms: int) -> str:
    s, mmm = divmod(int(ms), 1000)
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}.{mmm:03d}"


def _qualidade(d: dt.date, leit: LeituraB3, t_ms: np.ndarray, preco: np.ndarray, qtd: np.ndarray, lado: np.ndarray,
               sessao: np.ndarray, *, n_linhas: int, atualizacoes: int, cancelados: int, duplicados: int, ticks: int,
               cfg: ConfigB3) -> dict[str, Any]:
    q: dict[str, Any] = {
        "data": d.isoformat(), "ticker": leit.ticker, "arquivo": Path(leit.arquivo).name, "origem": ORIGEM,
        "rotulo": ROTULO_REAL_B3, "lado_inferido": True, "regra_lado": "regra_do_tick", "cotacao_inferida": True,
        "livro": False, "agregacao": cfg.agregacao, "atraso_entrega_ms": cfg.atraso_entrega_ms,
        "fuso_utc_horas": cfg.fuso_utc_horas, "linhas_do_dia": n_linhas, "linhas_atualizacao": atualizacoes,
        "negocios_cancelados": cancelados, "duplicados": duplicados, "negocios_validos": int(t_ms.size),
        "ticks_gravados": ticks, "fator_agregacao": round(t_ms.size / ticks, 3) if ticks else None,
        "achados": []}
    ach: list[str] = q["achados"]
    if t_ms.size == 0:
        ach.append("CRITICO: nenhum negocio valido no dia")
        return q
    q.update(primeiro=_hhmmss(int(t_ms[0])), ultimo=_hhmmss(int(t_ms[-1])), preco_min=float(preco.min()),
             preco_max=float(preco.max()), preco_abertura=float(preco[0]), preco_fechamento=float(preco[-1]),
             volume_total=int(qtd.sum()),
             lado={"compra": int((lado > 0).sum()), "venda": int((lado < 0).sum()),
                   "desconhecido": int((lado == 0).sum())},
             tipos_sessao={str(int(k)): int(v) for k, v in zip(*np.unique(sessao, return_counts=True), strict=True)})
    s = t_ms / 1000.0
    dentro = (s[:-1] >= SESSAO_INI_S) & (s[1:] <= SESSAO_FIM_S)
    gap = np.diff(s)
    lac = dentro & (gap > LACUNA_AVISO_S)
    q["lacunas_maior_60s"] = int(lac.sum())
    q["maior_lacuna_s"] = round(float(gap[dentro].max()), 3) if dentro.any() else None
    if lac.any():
        ach.append(f"AVISO: {int(lac.sum())} lacuna(s) > {LACUNA_AVISO_S:g}s dentro do pregao (maior "
                   f"{float(gap[lac].max()):.0f}s, a partir de {_hhmmss(int(t_ms[:-1][lac][0]))})")
    if t_ms[-1] < FIM_ESPERADO_S * 1000:
        ach.append(f"AVISO: ultimo negocio as {_hhmmss(int(t_ms[-1]))}: pregao possivelmente INCOMPLETO")
    if cancelados or atualizacoes:
        ach.append(f"INFO: {atualizacoes} linha(s) de atualizacao; {cancelados} negocio(s) cancelado(s) descartado(s)")
    tick = 1.0
    passos = np.abs(np.diff(preco))
    if passos.size and np.any(passos > 0):
        tick = float(np.min(passos[passos > 0]))
    q["menor_variacao_preco"] = tick
    return q


# ============================================================================================ importacao
@dataclass
class ResultadoImportacao:
    arquivo: str
    ticker: str
    dias: list[dict[str, Any]]
    linhas: int
    invalidas: int
    outros_instrumentos: int
    sha256: str

    def como_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def importar_arquivo(caminho: Path, armazem: ArmazenamentoTicks, perfil: PerfilInstrumento, cfg: ConfigB3, *,
                     ticker: str | None = None) -> ResultadoImportacao:
    """Le, converte e grava (substituindo os dias presentes) um arquivo de negocios no armazem REAL."""
    caminho = Path(caminho)
    armazem.marcar_real(ORIGEM)  # recusa armazem sintetico (MisturaDeDados)
    leit = ler_arquivo(caminho, ticker=ticker, tamanho_bloco=cfg.tamanho_bloco)
    if not perfil.codigo_pertence(leit.ticker):
        raise ErroFormatoB3(f"{caminho.name}: contrato {leit.ticker} nao pertence ao ativo {perfil.simbolo_base} "
                            "(use --ativo do perfil certo)")
    sha = _sha256(caminho)
    dias: list[dict[str, Any]] = []
    for dd in sorted(np.unique(leit.data).tolist()):
        dia = converter_dia(leit, int(dd), perfil, cfg)
        dia.qualidade.update(sha256_arquivo=sha, linhas_invalidas_arquivo=leit.invalidas,
                             outros_instrumentos_arquivo=leit.outros_instrumentos)
        armazem.gravar_tabela(leit.ticker, dia.tabela)
        armazem.gravar_meta(leit.ticker, f"b3_negocios_{dia.data.isoformat()}", dia.qualidade)
        dias.append(dia.qualidade)
    return ResultadoImportacao(str(caminho), leit.ticker, dias, leit.linhas, leit.invalidas,
                               leit.outros_instrumentos, sha)


def arquivos_de(entradas: list[str]) -> list[Path]:
    """Expande diretorios (``**/*.zip`` e ``**/*NEGOCIOSAVISTA*.txt``), em ordem de nome."""
    out: list[Path] = []
    for e in entradas:
        p = Path(e)
        if p.is_dir():
            out += sorted(x for x in p.rglob("*") if x.is_file() and (
                x.suffix.lower() == ".zip" or RE_NOME_ARQUIVO.search(x.name)))
        else:
            out.append(p)
    return out


def formatar_qualidade(q: Mapping[str, Any]) -> str:
    """Uma linha + achados, em portugues, do relatorio de qualidade de um dia."""
    if not q.get("negocios_validos"):
        return f"{q['data']} {q['ticker']}: SEM NEGOCIOS VALIDOS " + "; ".join(q.get("achados", []))
    lado = q.get("lado", {})
    lin = [f"{q['data']} {q['ticker']}: linhas={q['linhas_do_dia']} validos={q['negocios_validos']} "
           f"cancelados={q['negocios_cancelados']} (atualizacoes={q['linhas_atualizacao']}) "
           f"duplicados={q['duplicados']} ticks={q['ticks_gravados']} (agregacao {q['agregacao']}, "
           f"fator {q['fator_agregacao']}) horario {q['primeiro']} -> {q['ultimo']} (Brasilia) "
           f"preco {q['preco_min']:g}..{q['preco_max']:g} volume={q['volume_total']} "
           f"lado inferido c/v/? = {lado.get('compra')}/{lado.get('venda')}/{lado.get('desconhecido')} "
           f"lacunas>60s={q.get('lacunas_maior_60s')}"]
    lin += [f"    {a}" for a in q.get("achados", [])]
    return "\n".join(lin)
