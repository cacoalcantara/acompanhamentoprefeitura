"""Hash de configuracao independente de quebra de linha (git autocrlf no Windows converte LF -> CRLF)."""
from robowin.core.config import carregar_risco, sha256_arquivo


def test_crlf_e_lf_tem_o_mesmo_hash(tmp_path):
    lf = tmp_path / "lf.toml"
    crlf = tmp_path / "crlf.toml"
    lf.write_bytes(b"[a]\nx = 1\n")
    crlf.write_bytes(b"[a]\r\nx = 1\r\n")
    assert sha256_arquivo(lf) == sha256_arquivo(crlf)


def test_conteudo_diferente_muda_o_hash(tmp_path):
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_bytes(b"[a]\nx = 1\n")
    b.write_bytes(b"[a]\nx = 2\n")
    assert sha256_arquivo(a) != sha256_arquivo(b)


def test_risco_do_repositorio_em_crlf_continua_conferindo(tmp_path):
    risco = tmp_path / "risco.toml"
    risco.write_bytes(open("config/risco.toml", "rb").read().replace(b"\n", b"\r\n"))
    apr = tmp_path / "risco-v1.toml"
    apr.write_bytes(open("config/aprovacoes/risco-v1.toml", "rb").read().replace(b"\n", b"\r\n"))
    carregar_risco(risco, apr)  # nao levanta "hash difere" (aprovacao pendente so bloqueia o vivo)
