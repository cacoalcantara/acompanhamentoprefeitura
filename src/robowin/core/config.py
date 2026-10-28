"""Configuracao TOML com hash; politica de risco travada por aprovacao."""
from __future__ import annotations

import hashlib
import os
import tomllib
from pathlib import Path
from typing import Any

from .tipos import ConfigRisco, Papel


class ErroConfig(Exception):
    pass


def carregar_toml(caminho: str | Path) -> dict[str, Any]:
    with open(caminho, "rb") as f:
        return tomllib.load(f)


def sha256_arquivo(caminho: str | Path) -> str:
    return hashlib.sha256(Path(caminho).read_bytes()).hexdigest()


def responsavel_pendente(reg: dict[str, Any]) -> bool:
    """O registro de aprovacao ainda nao foi assumido por uma pessoa (responsavel vazio ou 'pendente...')."""
    resp = str(reg.get("responsavel", "")).strip().lower()
    return not resp or resp.startswith("pendente")


def aprovacao_pendente(aprovacao: str | Path | None) -> bool:
    """True se o arquivo de aprovacao existe mas o responsavel e 'pendente'/vazio (ilegivel conta como pendente)."""
    if aprovacao is None:
        return False
    try:
        return responsavel_pendente(carregar_toml(aprovacao))
    except (OSError, ValueError):
        return True


def carregar_risco(caminho: str | Path, aprovacao: str | Path | None, *, ao_vivo: bool = False) -> ConfigRisco:
    """Recusa iniciar se o hash do arquivo de risco diferir do ultimo registro de aprovacao.

    Ao vivo (`ao_vivo=True`) recusa tambem aprovacao cujo `responsavel` esteja 'pendente' (ninguem assumiu o risco);
    na simulacao isso e permitido, mas o chamador deve rotular `risco_nao_aprovado` (ver `risco.politica`)."""
    cfg = ConfigRisco(**carregar_toml(caminho))
    if aprovacao is None:
        raise ErroConfig("sem registro de aprovacao da politica de risco")
    reg = carregar_toml(aprovacao)
    if reg.get("sha256") != sha256_arquivo(caminho):
        raise ErroConfig("hash da politica de risco difere do registro de aprovacao")
    if ao_vivo and responsavel_pendente(reg):
        raise ErroConfig(f"aprovacao da politica de risco com responsavel pendente ('{reg.get('responsavel', '')}'): "
                         "preencha o responsavel em config/aprovacoes/ e regenere o hash (docs/07-risco-e-operacao.md)")
    return cfg


def papeis(cfg: dict[str, Any]) -> dict[str, Papel]:
    out: dict[str, Papel] = {}
    for nome, p in cfg.get("ia", {}).get("papeis", {}).items():
        out[nome] = Papel(nome=nome, modelo=p["modelo"], effort=p.get("effort"), parametros=p.get("parametros", {}))
    return out


NOME_FLAG_EMERGENCIA = "EMERGENCIA"


def dir_dados(config_path: str | Path | None = None, override: str | Path | None = None) -> Path:
    """Diretorio de dados UNICO (robowin.toml [geral].dados), absoluto: painel, gateway, nucleo e CLI de emergencia
    concordam no mesmo arquivo-flag independentemente do CWD.

    Precedencia: `override` explicito (relativo ao CWD de quem digitou) > ROBOWIN_DADOS > [geral].dados do TOML
    (relativo a RAIZ do projeto = pasta pai da pasta do TOML) > dados_locais."""
    if override:
        return Path(override).resolve()
    if os.environ.get("ROBOWIN_DADOS"):
        return Path(os.environ["ROBOWIN_DADOS"]).resolve()
    bruto: str | Path = "dados_locais"
    base = Path.cwd()
    if config_path and Path(config_path).is_file():
        bruto = carregar_toml(config_path).get("geral", {}).get("dados", "dados_locais")
        base = Path(config_path).resolve().parent.parent
    p = Path(bruto)
    return p if p.is_absolute() else (base / p).resolve()


def caminho_flag_emergencia(config_path: str | Path | None = None, arquivo: str | Path | None = None,
                            dados: str | Path | None = None) -> Path:
    """Caminho absoluto do arquivo EMERGENCIA. `arquivo` explicito vence; senao <dir_dados>/EMERGENCIA."""
    if arquivo:
        return Path(arquivo).resolve()
    return dir_dados(config_path, dados) / NOME_FLAG_EMERGENCIA
