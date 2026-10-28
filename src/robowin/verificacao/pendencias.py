"""O que ainda DEPENDE DO USUARIO (`robowin pendencias`): cada item com o comando exato que o resolve.

So leitura: nao altera arquivo, nao usa rede e nunca le o VALOR de uma chave de API (so se a variavel existe).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .checagens import CHAVES_API

CMD_RISCO = 'uv run robowin risco aprovar --responsavel "<seu nome>" --motivo "<por que estes limites>"'


@dataclass(frozen=True)
class Pendencia:
    prioridade: str  # "OBRIGATORIA" (bloqueia o uso pretendido) | "OPCIONAL"
    descricao: str
    comando: str


def listar_pendencias(raiz: str | Path) -> list[Pendencia]:
    from robowin.core.config import carregar_toml
    from robowin.perfil.confirmacao import PREFIXO_BOLSA, listar_status
    from robowin.risco.aprovar import estado_aprovacao
    r = Path(raiz)
    out: list[Pendencia] = []
    estado, detalhe = estado_aprovacao(r / "config/risco.toml", r / "config/aprovacoes/risco-v1.toml")
    if estado != "APROVADO":
        out.append(Pendencia("OBRIGATORIA", f"Aprovacao da politica de risco {estado}: {detalhe}", CMD_RISCO))
    out += _pendencias_usuarios(r)
    try:
        ativo = str(carregar_toml(r / "config/robowin.toml").get("geral", {}).get("simbolo_base") or "").upper()
    except (OSError, ValueError):
        ativo = ""
    st = listar_status(r / "config/instrumentos.toml", r / "config/bolsas.toml")
    bolsa_do_ativo = {s.codigo: s.bloqueado_por_bolsa for s in st if s.tipo == "ativo"}
    for s in st:
        alvo = f"{PREFIXO_BOLSA}{s.codigo}" if s.tipo == "bolsa" else s.codigo
        if s.confirmado:
            continue
        principal = (s.tipo == "ativo" and s.codigo == ativo) or (s.tipo == "bolsa" and bolsa_do_ativo.get(ativo) == s.codigo)
        extra = " --comparar-terminal" if s.tipo == "ativo" else ""
        out.append(Pendencia(
            "OBRIGATORIA" if principal else "OPCIONAL",
            f"Perfil de {s.tipo} {s.codigo} a confirmar ({s.itens_pendentes} itens)"
            + ("" if principal else " - so se for usar"),
            f'uv run robowin perfil confirmar {alvo} --responsavel "<seu nome>" --fonte "<fonte consultada>"{extra}'))
    for v in CHAVES_API:
        if not os.environ.get(v):
            out.append(Pendencia("OPCIONAL", f"Chave de API {v} ausente (bracos de IA reais e etapa A10 nao rodam)",
                                 f'setx {v} "<sua chave>"   (depois abra um novo terminal; nunca grave a chave no repositorio)'))
    return out


def _pendencias_usuarios(r: Path) -> list[Pendencia]:
    """Limites por usuario+conta: sem usuarios, lembra que os do arquivo unico sao provisorios do titular (DEMO);
    com usuarios, cada conta DEMO sem aprovacao valida e uma pendencia OBRIGATORIA (ela nao opera)."""
    from robowin.risco.usuarios import APROVADO, ArmazemUsuarios, dir_usuarios
    cfg = r / "config/robowin.toml"
    try:
        arm = ArmazemUsuarios(dir_usuarios(cfg) if cfg.is_file() else r / "config/usuarios")
        nomes = arm.usuarios()
    except (OSError, ValueError):
        return []
    if not nomes:
        return [Pendencia("OPCIONAL", "Limites de risco proprios por usuario e conta (os de config/risco.toml sao a "
                          "aprovacao PROVISORIA do titular, somente DEMO, e nao valem como padrao para outros usuarios)",
                          'uv run robowin usuario criar --usuario <nome>   (depois: robowin painel > Risco)')]
    out = []
    for u in nomes:
        for c in arm.contas(u):
            st = arm.estado(u, c.chave)
            if c.tipo == "demo" and st.estado != APROVADO:
                out.append(Pendencia("OBRIGATORIA", f"Risco de {u} na conta {c.chave.texto}: {st.estado} ({st.detalhe})",
                                     f'uv run robowin risco aprovar --usuario {u} --conta {c.chave.texto} '
                                     '--responsavel "<seu nome>"   (ou no painel, pagina Risco)'))
    return out


def formatar_pendencias(pend: list[Pendencia]) -> str:
    if not pend:
        return "Nada pendente do seu lado: risco aprovado, perfis em uso confirmados e chaves de API presentes."
    linhas = ["O QUE AINDA DEPENDE DE VOCE (decisoes que so o usuario pode tomar):"]
    for p in sorted(pend, key=lambda x: x.prioridade != "OBRIGATORIA"):
        linhas += [f"  [{p.prioridade}] {p.descricao}", f"      -> {p.comando}"]
    return "\n".join(linhas)
