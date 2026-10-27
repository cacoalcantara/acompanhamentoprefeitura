import datetime as dt
import sqlite3
import threading

import pytest

from robowin.core.esquema_banco import TABELAS
from robowin.core.tipos import LivroSnapshot, NivelBook, Tick
from robowin.dados import ArmazenamentoTicks, BancoSQLite
from robowin.dados.ticks import dia_ns

D = dt.date(2026, 3, 10)
T0 = dia_ns(D) + 10 * 3600 * 10**9


def tk(i, bid=130000.0, vol=1.0, dia_off=0):
    t = T0 + i * 10**9 + dia_off * 86400 * 10**9
    return Tick(t_servidor_ms=t // 10**6, t_utc_ns=t, t_recebido_ns=t + 5_000_000, bid=bid, ask=bid + 5,
                last=bid + 5, volume=vol, flags=40)


def test_protocolo_e_roundtrip(tmp_path):
    arm = ArmazenamentoTicks(tmp_path)
    assert all(callable(getattr(arm, m)) for m in ("gravar", "ler"))
    ticks = [tk(i, 130000 + 5 * i, vol=i + 1) for i in range(50)]
    assert arm.gravar("WINJ26", ticks) == 50
    lidos = arm.ler("WINJ26", T0, T0 + 100 * 10**9)
    assert lidos == ticks
    assert list(tmp_path.glob("ticks/WINJ26/data=2026-03-10/part.parquet"))


def test_idempotencia_e_dedup(tmp_path):
    arm = ArmazenamentoTicks(tmp_path)
    ticks = [tk(i) for i in range(10)]
    assert arm.gravar("S", ticks) == 10
    assert arm.gravar("S", ticks) == 0
    # mesma chave com outro t_recebido: duplicata
    dup = ticks[3].model_copy(update={"t_recebido_ns": ticks[3].t_recebido_ns + 99, "t_utc_ns": ticks[3].t_utc_ns})
    assert arm.gravar("S", [dup, tk(11)]) == 1
    assert len(arm.ler("S", 0, 2**62)) == 11
    # dentro do mesmo lote
    assert arm.gravar("S", [tk(20), tk(20)]) == 1


def test_ordenacao_e_intervalos(tmp_path):
    arm = ArmazenamentoTicks(tmp_path)
    arm.gravar("S", [tk(5), tk(1), tk(3)])
    arm.gravar("S", [tk(2), tk(4, dia_off=1), tk(0, dia_off=-1)])
    todos = arm.ler("S", 0, 2**62)
    assert [t.t_utc_ns for t in todos] == sorted(t.t_utc_ns for t in todos) and len(todos) == 6
    # [de, ate): ate exclusivo
    r = arm.ler("S", T0 + 1 * 10**9, T0 + 3 * 10**9)
    assert [t.t_utc_ns for t in r] == [T0 + 10**9, T0 + 2 * 10**9]
    assert arm.ler("S", T0 + 10, T0 + 10) == []
    assert arm.ler("OUTRO", 0, 2**62) == []
    assert arm.datas("S") == [D - dt.timedelta(days=1), D, D + dt.timedelta(days=1)]
    assert arm.simbolos() == ["S"]
    assert len(arm.ler_dia("S", D)) == 4


def test_livro_e_offset(tmp_path):
    arm = ArmazenamentoTicks(tmp_path)
    lv = LivroSnapshot(t_utc_ns=T0, niveis=(NivelBook(preco=130000.0, volume=10, lado="compra"),
                                            NivelBook(preco=130005.0, volume=7, lado="venda")))
    assert arm.gravar_livros("S", [lv]) == 1
    assert arm.gravar_livros("S", [lv]) == 0
    assert arm.ler_livros("S", T0, T0 + 1) == [lv]
    assert arm.ler_livros("S", T0 + 1, T0 + 2) == []
    assert arm.ler_offset_servidor("S") is None
    arm.gravar_offset_servidor("S", -10_800_000)
    assert ArmazenamentoTicks(tmp_path).ler_offset_servidor("S") == -10_800_000


def test_simbolo_invalido(tmp_path):
    with pytest.raises(ValueError):
        ArmazenamentoTicks(tmp_path).gravar("../x", [tk(1)])


def test_gravacao_concorrente(tmp_path):
    arm = ArmazenamentoTicks(tmp_path)
    ths = [threading.Thread(target=arm.gravar, args=("S", [tk(i) for i in range(k, k + 20)])) for k in range(0, 100, 10)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert len(arm.ler("S", 0, 2**62)) == 110


# ---------------- banco ----------------
def test_ddl_completo_wal(tmp_path):
    b = BancoSQLite(tmp_path / "x.sqlite")
    assert all(callable(getattr(b, m)) for m in ("inserir", "consultar"))
    assert set(TABELAS) <= set(b.tabelas())
    assert b.modo_journal == "wal"


def test_inserir_consultar_replace(tmp_path):
    b = BancoSQLite(tmp_path / "x.sqlite")
    b.inserir("incidentes", {"t_ns": 1, "origem": "o'; DROP TABLE incidentes;--", "severidade": "aviso", "detalhe": "d"})
    b.inserir("orcamento_ia", {"dia": "2026-03-10", "braco": "a", "gasto_usd": 1.0, "reservado_usd": 0.0})
    b.inserir("orcamento_ia", {"dia": "2026-03-10", "braco": "a", "gasto_usd": 2.5, "reservado_usd": 0.0})
    assert b.consultar("SELECT gasto_usd FROM orcamento_ia WHERE braco=?", ("a",)) == [{"gasto_usd": 2.5}]
    assert b.consultar("SELECT origem FROM incidentes")[0]["origem"].startswith("o'; DROP")
    b.inserir("experimentos", {"id": "e", "criado_ns": 1, "manifesto_json": {"a": [1]}, "status": "ok"})
    assert b.consultar("SELECT manifesto_json FROM experimentos")[0]["manifesto_json"] == '{"a": [1]}'
    with pytest.raises(ValueError):
        b.inserir("incidentes; DROP", {"t_ns": 1})
    with pytest.raises(ValueError):
        b.inserir("incidentes", {"coluna_x": 1})
    with pytest.raises(ValueError):
        b.inserir("nao_existe", {"a": 1})


def test_transacao_rollback(tmp_path):
    b = BancoSQLite(tmp_path / "x.sqlite")
    with pytest.raises(RuntimeError), b.transacao():
        b.inserir("incidentes", {"t_ns": 1, "origem": "a", "severidade": "x", "detalhe": ""})
        raise RuntimeError
    assert b.consultar("SELECT COUNT(*) n FROM incidentes")[0]["n"] == 0
    with b.transacao(), b.transacao():
        b.inserir("incidentes", {"t_ns": 1, "origem": "a", "severidade": "x", "detalhe": ""})
    assert b.consultar("SELECT COUNT(*) n FROM incidentes")[0]["n"] == 1


def test_migracao(tmp_path):
    b = BancoSQLite(tmp_path / "x.sqlite")
    ddl = ["CREATE TABLE IF NOT EXISTS mod_x(id INTEGER PRIMARY KEY, v TEXT)", "ALTER TABLE mod_x ADD COLUMN w REAL"]
    assert b.registrar_migracao("modx", ddl) == 2
    assert b.registrar_migracao("modx", ddl) == 0
    b.inserir("mod_x", {"id": 1, "v": "a", "w": 2.0})
    assert b.consultar("SELECT * FROM mod_x") == [{"id": 1, "v": "a", "w": 2.0}]
    with pytest.raises(ValueError):
        b.registrar_migracao("modx", ["CREATE TABLE outra(a)"])
    b.fechar()
    b2 = BancoSQLite(tmp_path / "x.sqlite")  # reabrir: persistente e idempotente
    assert b2.registrar_migracao("modx", ddl) == 0


def test_concorrencia_threads_e_conexoes(tmp_path):
    caminho = tmp_path / "c.sqlite"
    b = BancoSQLite(caminho)
    erros = []

    def escreve(k):
        try:
            for i in range(50):
                b.inserir("latencias", {"t_ns": k * 1000 + i, "braco": "a", "etapa": "e", "dur_ms": 1.0, "injetado": 0})
        except Exception as e:  # pragma: no cover
            erros.append(e)

    def le():
        try:
            c = sqlite3.connect(caminho)  # leitor externo concorrente (WAL)
            for _ in range(20):
                c.execute("SELECT COUNT(*) FROM latencias").fetchone()
            c.close()
        except Exception as e:  # pragma: no cover
            erros.append(e)

    ths = [threading.Thread(target=escreve, args=(k,)) for k in range(8)] + [threading.Thread(target=le)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert not erros
    assert b.consultar("SELECT COUNT(*) n FROM latencias")[0]["n"] == 400
