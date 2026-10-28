"""Coleta de ticks e livro (L2) do MT5: incremental, deduplicada, com backfill na reconexao.

- Ticks: `copy_ticks_from` incremental a partir do ultimo `time_msc` visto; deduplicacao por `time_msc`
  (mais a chave completa do tick, pois varios ticks podem cair no mesmo milissegundo).
- Livro: `market_book_add/get`; registra a profundidade declarada e a observada.
- Backfill: apos falha de leitura, `copy_ticks_range` cobre a lacuna; os ticks de backfill saem com
  `backfill=True` e `t_disponivel_ns` = instante em que ficaram disponiveis (replay por disponibilidade).
- Topicos no barramento: 'tick' e 'livro' (dicts JSON-serializaveis).
- `LogBruto` guarda tudo em disco (JSONL por dia/simbolo), sobrevivendo a quedas do nucleo.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from robowin.core.relogio import Relogio
from robowin.core.tipos import LivroSnapshot, NivelBook

from . import constantes as C
from .tempo_servidor import RelogioServidor


def linhas_de_ticks(arr: Any) -> list[dict[str, Any]]:
    """Converte o retorno de copy_ticks_* (ndarray estruturado) em lista de dicts."""
    if arr is None:
        return []
    nomes = arr.dtype.names
    if nomes is None:
        return []
    return [{n: (v.item() if hasattr(v, "item") else v) for n, v in zip(nomes, row, strict=True)} for row in arr]


class LogBruto:
    """Log bruto em disco (JSONL) da coleta: `dir/AAAA-MM-DD/<simbolo>.<tipo>.jsonl`."""

    def __init__(self, diretorio: str | Path, relogio: Relogio, fsync: bool = False) -> None:
        self.dir = Path(diretorio)
        self.relogio = relogio
        self.fsync = fsync
        self._arqs: dict[Path, Any] = {}

    def gravar(self, tipo: str, simbolo: str, dados: dict[str, Any]) -> None:
        dia = dt.datetime.fromtimestamp(self.relogio.agora_ns() / 1e9, dt.UTC).strftime("%Y-%m-%d")
        p = self.dir / dia / f"{simbolo}.{tipo}.jsonl"
        f = self._arqs.get(p)
        if f is None:
            p.parent.mkdir(parents=True, exist_ok=True)
            f = self._arqs[p] = open(p, "a", encoding="utf-8")  # noqa: SIM115
        f.write(json.dumps(dados, default=str, separators=(",", ":")) + "\n")

    def descarregar(self) -> None:
        import os
        for f in self._arqs.values():
            f.flush()
            if self.fsync:
                os.fsync(f.fileno())

    def fechar(self) -> None:
        self.descarregar()
        for f in self._arqs.values():
            f.close()
        self._arqs.clear()


class ColetorTicks:
    def __init__(
        self, terminal: Any, simbolo: str, relogio: Relogio, relogio_srv: RelogioServidor, barramento: Any,
        log: LogBruto | None = None, lote: int = 5000, inicio_ns: int | None = None,
        limite_feed_parado_s: float = 5.0,
    ) -> None:
        self.terminal, self.simbolo, self.relogio, self.rs = terminal, simbolo, relogio, relogio_srv
        self.barramento, self.log, self.lote = barramento, log, lote
        self.ultimo_msc: int | None = None
        self._chaves: set[tuple[Any, ...]] = set()
        self._inicio_ns = inicio_ns if inicio_ns is not None else relogio.agora_ns()
        self._precisa_backfill = False
        self._ultima_recepcao_ns = relogio.agora_ns()
        self.limite_feed_parado_s = limite_feed_parado_s
        self.total = 0
        self.duplicados = 0
        self.falhas_consecutivas = 0
        self.backfills: list[dict[str, int]] = []
        self.lacunas_sem_backfill = 0  # terminal sem historico de ticks (MT4): a lacuna nao se recupera

    # ------------------------------------------------------------------------------------------
    @property
    def feed_parado(self) -> bool:
        return (self.relogio.agora_ns() - self._ultima_recepcao_ns) / 1e9 > self.limite_feed_parado_s

    @property
    def lacuna_pendente(self) -> bool:
        return self._precisa_backfill

    def _aceitar(self, linha: dict[str, Any]) -> bool:
        msc = int(linha["time_msc"])
        chave = (msc, linha["bid"], linha["ask"], linha["last"], linha.get("volume_real", linha.get("volume")),
                 linha.get("flags"), linha.get("seq"))  # `seq` (MT4): ticks do mesmo segundo nao sao duplicatas
        if self.ultimo_msc is not None:
            if msc < self.ultimo_msc:
                self.duplicados += 1
                return False
            if msc == self.ultimo_msc:
                if chave in self._chaves:
                    self.duplicados += 1
                    return False
                self._chaves.add(chave)
                return True
        self.ultimo_msc = msc
        self._chaves = {chave}
        return True

    def _ingerir(self, arr: Any, backfill: bool) -> int:
        n = 0
        recebido = self.relogio.agora_ns()
        for linha in linhas_de_ticks(arr):
            if not self._aceitar(linha):
                continue
            tk = self.rs.tick_de_linha(linha, recebido)
            dados = {**tk.model_dump(mode="json"), "simbolo": self.simbolo, "t_disponivel_ns": recebido,
                     "backfill": backfill}
            if self.log is not None:
                self.log.gravar("ticks", self.simbolo, {"t_rec_ns": recebido, "backfill": backfill, **linha})
            self.barramento.publicar("tick", dados)
            n += 1
        if n:
            self._ultima_recepcao_ns = recebido
            self.total += n
        if self.log is not None:
            self.log.descarregar()
        return n

    def _dt_srv(self, msc: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(msc // 1000, dt.UTC)

    # ------------------------------------------------------------------------------------------
    def backfill(self) -> int | None:
        """Cobre a lacuna com copy_ticks_range. None se o terminal ainda nao responde."""
        if self.ultimo_msc is None:
            self._precisa_backfill = False
            return 0
        if not getattr(self.terminal, "suporta_backfill", True):
            self._precisa_backfill = False
            self.lacunas_sem_backfill += 1  # o gateway BLOQUEIA entradas; nao ha como recuperar os ticks
            return 0
        agora_srv = self.rs.utc_ns_para_servidor_ms(self.relogio.agora_ns())
        arr = self.terminal.copy_ticks_range(
            self.simbolo, self._dt_srv(self.ultimo_msc), self._dt_srv(agora_srv + 1000), C.COPY_TICKS_ALL)
        if arr is None:
            return None
        de = self.ultimo_msc
        n = self._ingerir(arr, backfill=True)
        self.backfills.append({"de_msc": de, "ate_msc": self.ultimo_msc or de, "n": n})
        self._precisa_backfill = False
        return n

    def sondar(self) -> int:
        """Uma passada incremental. Retorna o numero de ticks novos publicados."""
        total = 0
        if self._precisa_backfill:
            n = self.backfill()
            if n is None:
                self.falhas_consecutivas += 1
                return 0
            total += n
        for _ in range(100):  # protecao contra laco infinito
            if self.ultimo_msc is None:
                dt_ini = self.rs.utc_ns_para_dt_servidor(self._inicio_ns)
            else:
                dt_ini = self._dt_srv(self.ultimo_msc)
            arr = self.terminal.copy_ticks_from(self.simbolo, dt_ini, self.lote, C.COPY_TICKS_ALL)
            if arr is None:
                self.falhas_consecutivas += 1
                if self.ultimo_msc is not None:
                    self._precisa_backfill = True
                return total
            self.falhas_consecutivas = 0
            n = self._ingerir(arr, backfill=False)
            total += n
            if len(arr) >= self.lote and n == 0 and self.ultimo_msc is not None:
                # lote cheio so de ticks ja vistos (> lote ticks no mesmo segundo): copy_ticks_range nao tem limite
                agora_srv = self.rs.utc_ns_para_servidor_ms(self.relogio.agora_ns())
                grande = self.terminal.copy_ticks_range(self.simbolo, dt_ini, self._dt_srv(agora_srv + 1000),
                                                        C.COPY_TICKS_ALL)
                total += self._ingerir(grande, backfill=False) if grande is not None else 0
                break
            if len(arr) < self.lote or n == 0:
                break
        return total


class ColetorLivro:
    def __init__(self, terminal: Any, simbolo: str, relogio: Relogio, barramento: Any,
                 log: LogBruto | None = None) -> None:
        self.terminal, self.simbolo, self.relogio, self.barramento, self.log = terminal, simbolo, relogio, barramento, log
        self.assinado = False
        self.profundidade_declarada = 0
        self.profundidade_observada = 0
        self._ultimo: tuple[Any, ...] | None = None
        self.total = 0

    def assinar(self) -> bool:
        info = self.terminal.symbol_info(self.simbolo)
        self.profundidade_declarada = int(getattr(info, "ticks_bookdepth", 0) or 0) if info is not None else 0
        self.assinado = bool(self.terminal.market_book_add(self.simbolo))
        return self.assinado

    def liberar(self) -> None:
        if self.assinado:
            self.terminal.market_book_release(self.simbolo)
            self.assinado = False

    def sondar(self) -> LivroSnapshot | None:
        if not self.assinado:
            return None
        bruto = self.terminal.market_book_get(self.simbolo)
        if not bruto:
            return None
        niveis = []
        for b in bruto:
            lado = "venda" if int(b.type) in (C.BOOK_VENDA, C.BOOK_VENDA_MERCADO) else "compra"
            niveis.append(NivelBook(preco=float(b.price), volume=float(getattr(b, "volume_dbl", b.volume)), lado=lado))
        chave = tuple((n.lado, n.preco, n.volume) for n in niveis)
        nv = sum(1 for n in niveis if n.lado == "venda")
        self.profundidade_observada = max(self.profundidade_observada, nv, len(niveis) - nv)
        if chave == self._ultimo:
            return None
        self._ultimo = chave
        agora = self.relogio.agora_ns()
        snap = LivroSnapshot(t_utc_ns=agora, niveis=tuple(niveis))
        if self.log is not None:
            self.log.gravar("livro", self.simbolo, {"t_rec_ns": agora, "niveis": [[n.lado, n.preco, n.volume] for n in niveis]})
        self.barramento.publicar("livro", {**snap.model_dump(mode="json"), "simbolo": self.simbolo, "t_disponivel_ns": agora})
        self.total += 1
        return snap
