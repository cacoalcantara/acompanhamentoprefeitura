"""Limites de risco POR USUARIO e POR CONTA de negociacao (login + servidor + plataforma), com aprovacao expressa.

Os limites aprovados pelo titular em `config/aprovacoes/risco-v2.toml` sao PROVISORIOS (somente DEMO) e NAO sao
padrao de ninguem: um usuario novo comeca SEM limites (bloqueado para operar ao vivo/demo) ate definir e aprovar os
proprios, pelo painel (pagina "Risco") ou pela CLI (`robowin risco definir/aprovar --usuario U --conta C`).

Armazem em arquivos (simples e auditavel; padrao `config/usuarios`, fora do git):

    <raiz>/<usuario>/usuario.toml                       nome e HASH (sal + sha256) do token do usuario; nunca o token
    <raiz>/<usuario>/contas/<plat>-<servidor>-<login>/
        conta.toml                                      plataforma, servidor, login, tipo (demo|real), autorizacao_real
        risco.toml                                      limites do usuario PARA ESTA CONTA (travados por hash)
        aprovacoes/risco-vN.toml                        aprovacoes versionadas (nunca sobrescritas)
        historico/risco-NNNN.toml + historico/indice.jsonl   cada versao salva dos limites

Toda criacao, alteracao, aprovacao e migracao vai para a cadeia de auditoria (`auditoria.cadeia`). Alterar
`risco.toml` muda o hash e invalida a aprovacao (e preciso aprovar de novo). Conta REAL exige `autorizacao_real`
especifica, que NAO existe nesta versao: o estado e sempre "bloqueada" e qualquer operacao em conta real e recusada.

Sem usuario configurado (nem `--usuario` nem `[geral].usuario`), vale o modo de ARQUIVO UNICO de sempre
(`config/risco.toml` + `config/aprovacoes/`), por compatibilidade.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import ErroConfig, carregar_toml, sha256_arquivo, versoes_aprovacao
from ..core.instrumentos import PerfilInstrumento
from ..core.tipos import ConfigRisco
from .aprovar import (
    PALAVRA_APROVACAO,
    AprovacaoRecusada,
    ResultadoAprovacao,
    aprovar_politica,
    estado_aprovacao,
)
from .politica import ERRO_APROVACAO_PENDENTE, risco_do_ativo, validar_coerencia

PLATAFORMAS_CONTA = ("mt5", "mt4")
TIPOS_CONTA = ("demo", "real")
AUTORIZACAO_REAL_BLOQUEADA = "bloqueada"  # unico estado possivel nesta versao (o envio real nao existe)
RE_USUARIO = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
RE_SERVIDOR = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
ARQ_USUARIO, ARQ_CONTA, ARQ_RISCO = "usuario.toml", "conta.toml", "risco.toml"
CAMPOS_RISCO = tuple(ConfigRisco.model_fields)

# estados da politica de uma conta
SEM_LIMITES, NAO_APROVADO, INVALIDO, APROVADO = "SEM_LIMITES", "NAO_APROVADO", "INVALIDO", "APROVADO"


class ErroUsuario(ErroConfig):
    """Usuario/conta inexistente, invalido ou nao autorizado."""


class AutorizacaoRealAusente(ErroConfig):
    """Conta REAL: exige autorizacao especifica que nao existe nesta versao (sempre bloqueia)."""


class ContaDivergente(ErroConfig):
    """A conta conectada/configurada nao e a conta cujos limites foram aprovados."""


def _agora_utc() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def _gravar_novo(destino: Path, texto: str) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "x", encoding="utf-8", newline="\n") as f:  # "x": nunca sobrescreve
        f.write(texto)


def _gravar(destino: Path, texto: str) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(texto)
    os.replace(tmp, destino)


def validar_nome_usuario(usuario: str) -> str:
    u = (usuario or "").strip()
    if not RE_USUARIO.match(u):
        raise ErroUsuario(f"usuario invalido '{usuario}': use 2 a 32 caracteres [a-z0-9_-], comecando por letra")
    return u


@dataclass(frozen=True)
class ChaveConta:
    """Conta de negociacao: plataforma + servidor + login (os tres identificam a conta; nenhum sozinho basta)."""
    plataforma: str
    servidor: str
    login: int

    @classmethod
    def criar(cls, plataforma: str, servidor: str, login: int | str) -> ChaveConta:
        plat = str(plataforma or "").strip().lower()
        if plat not in PLATAFORMAS_CONTA:
            raise ErroUsuario(f"plataforma invalida '{plataforma}': use {' ou '.join(PLATAFORMAS_CONTA)}")
        srv = str(servidor or "").strip()
        if not RE_SERVIDOR.match(srv):
            raise ErroUsuario(f"servidor invalido '{servidor}': 1 a 100 caracteres [A-Za-z0-9._-]")
        try:
            lg = int(str(login).strip())
        except ValueError as e:
            raise ErroUsuario(f"login invalido '{login}': numero inteiro positivo") from e
        if lg <= 0:
            raise ErroUsuario(f"login invalido '{login}': numero inteiro positivo")
        return cls(plat, srv, lg)

    @classmethod
    def de_texto(cls, texto: str) -> ChaveConta:
        """'PLATAFORMA:SERVIDOR:LOGIN' (ex.: mt5:CORRETORA-DEMO:123456)."""
        partes = str(texto or "").strip().split(":")
        if len(partes) != 3:
            raise ErroUsuario(f"conta invalida '{texto}': use PLATAFORMA:SERVIDOR:LOGIN (ex.: mt5:CORRETORA-DEMO:123456)")
        return cls.criar(*partes)

    @property
    def texto(self) -> str:
        return f"{self.plataforma}:{self.servidor}:{self.login}"

    @property
    def pasta(self) -> str:
        return f"{self.plataforma}-{self.servidor}-{self.login}"

    def confere(self, plataforma: str | None, servidor: str | None, login: int | str | None) -> str:
        """'' se (plataforma, servidor, login) e esta conta; senao o motivo da divergencia."""
        dif = []
        if plataforma is not None and str(plataforma).strip().lower() != self.plataforma:
            dif.append(f"plataforma '{plataforma}' != '{self.plataforma}'")
        if servidor is not None and str(servidor).strip() != self.servidor:
            dif.append(f"servidor '{servidor}' != '{self.servidor}'")
        if login is not None and str(login).strip() != str(self.login):
            dif.append(f"login {login} != {self.login}")
        return "; ".join(dif)


def _hash_token(sal: str, token: str) -> str:
    return hashlib.sha256((sal + ":" + token).encode("utf-8")).hexdigest()


def texto_risco(cfg: ConfigRisco) -> str:
    """TOML deterministico dos limites (mesma ordem dos campos de ConfigRisco): o hash so muda se um valor mudar."""
    linhas = []
    for nome in CAMPOS_RISCO:
        v = getattr(cfg, nome)
        if isinstance(v, bool):
            linhas.append(f"{nome} = {'true' if v else 'false'}")
        elif isinstance(v, int):
            linhas.append(f"{nome} = {v}")
        elif isinstance(v, float):
            linhas.append(f"{nome} = {v!r}")
        else:
            linhas.append(f"{nome} = {_toml_str(str(v))}")
    return "\n".join(linhas) + "\n"


def config_de_formulario(valores: Mapping[str, Any]) -> ConfigRisco:
    """Limites a partir de texto (painel/CLI): TODOS os campos sao obrigatorios (nenhum valor e imposto por padrao);
    aceita virgula decimal. Campo desconhecido e recusado. Levanta ErroConfig com todos os problemas."""
    erros: list[str] = []
    desconhecidos = sorted(set(valores) - set(CAMPOS_RISCO))
    if desconhecidos:
        erros.append(f"campos desconhecidos: {', '.join(desconhecidos)}")
    out: dict[str, Any] = {}
    for nome, campo in ConfigRisco.model_fields.items():
        bruto = valores.get(nome)
        if bruto is None or str(bruto).strip() == "":
            erros.append(f"{nome} e obrigatorio")
            continue
        s = str(bruto).strip()
        try:
            if campo.annotation is int:
                f = float(s.replace(",", "."))
                if not f.is_integer():
                    raise ValueError
                out[nome] = int(f)
            elif campo.annotation is float:
                f = float(s.replace(",", "."))
                if not math.isfinite(f):
                    raise ValueError
                out[nome] = f
            else:
                out[nome] = s
        except ValueError:
            erros.append(f"{nome}: valor invalido '{s}'")
    if erros:
        raise ErroConfig("; ".join(erros))
    try:
        cfg = ConfigRisco(**out)
    except Exception as e:  # noqa: BLE001 - validacao pydantic
        raise ErroConfig(f"limites invalidos: {e}") from e
    return validar_coerencia(cfg)


def modelo_inicial(perfil: PerfilInstrumento) -> ConfigRisco:
    """Modelo MUITO conservador para o usuario EDITAR e aprovar (nunca vale sem aprovacao): 1 contrato, stop curto
    derivado do perfil do ativo, perda por operacao = pior caso desse stop + folga, dia = 2 operacoes, semana = 5,
    3 operacoes/dia, para apos 2 perdas seguidas e pausa de 15 min. Horarios: os da sessao padrao do ConfigRisco."""
    base = ConfigRisco()
    stop = min(perfil.stop_max_pontos, 4 * perfil.stop_padrao_pontos)
    folga = perfil.slippage_folga_pontos
    op = float(math.ceil((stop + folga) * perfil.ponto_valor_brl))
    cfg = base.model_copy(update={
        "contratos_max": 1, "perda_max_operacao_brl": op, "perda_max_dia_brl": 2 * op, "perda_max_semana_brl": 5 * op,
        "dd_alerta_brl": 6 * op, "dd_max_brl": 8 * op, "operacoes_dia_max": 3, "perdas_seguidas_max": 2,
        "pausa_apos_perda_s": 900, "stop_max_pontos": float(stop), "slippage_folga_pontos": float(folga),
        "bloqueio_evento_min": 15, "ordens_min_max": 6})
    return validar_coerencia(cfg)


@dataclass(frozen=True)
class Conta:
    usuario: str
    chave: ChaveConta
    tipo: str
    dir: Path
    autorizacao_real_bruta: str = AUTORIZACAO_REAL_BLOQUEADA

    @property
    def risco(self) -> Path:
        return self.dir / ARQ_RISCO

    @property
    def dir_aprovacoes(self) -> Path:
        return self.dir / "aprovacoes"

    @property
    def aprovacao(self) -> Path:
        """Ponteiro `risco-v1.toml` (resolvido para a versao mais recente valida, como no modo de arquivo unico)."""
        return self.dir_aprovacoes / "risco-v1.toml"

    @property
    def autorizacao_real(self) -> str:
        """Sempre 'bloqueada' nesta versao, qualquer que seja o texto gravado (o envio real nao existe)."""
        return AUTORIZACAO_REAL_BLOQUEADA


@dataclass(frozen=True)
class EstadoRiscoConta:
    usuario: str
    conta: str
    tipo: str
    estado: str  # SEM_LIMITES | NAO_APROVADO | INVALIDO | APROVADO
    detalhe: str
    provisoria: bool
    autorizacao_real: str
    sha256_atual: str | None

    @property
    def pode_operar_demo(self) -> bool:
        return self.estado == APROVADO and self.tipo == "demo"

    @property
    def pode_operar_real(self) -> bool:
        return False  # nesta versao, nunca (autorizacao_real sempre bloqueada; o envio real nao existe)


class ArmazemUsuarios:
    """Usuarios, contas e limites em `raiz`. `auditoria` (RegistradorAuditoria) recebe todos os eventos."""

    def __init__(self, raiz: str | Path, auditoria: Any | None = None) -> None:
        self.raiz = Path(raiz)
        self.auditoria = auditoria

    def _auditar(self, tipo: str, dados: dict[str, Any]) -> None:
        if self.auditoria is not None:
            self.auditoria.registrar(tipo, dados)

    # ------------------------------------------------------------------ usuarios -----------------
    def dir_usuario(self, usuario: str) -> Path:
        return self.raiz / validar_nome_usuario(usuario)

    def existe(self, usuario: str) -> bool:
        try:
            return (self.dir_usuario(usuario) / ARQ_USUARIO).is_file()
        except ErroUsuario:
            return False

    def usuarios(self) -> list[str]:
        if not self.raiz.is_dir():
            return []
        return sorted(p.name for p in self.raiz.iterdir() if RE_USUARIO.match(p.name) and (p / ARQ_USUARIO).is_file())

    def criar_usuario(self, usuario: str, nome: str = "", *, token: str | None = None) -> str:
        """Cria o usuario SEM limites (bloqueado ate aprovar) e devolve o token (mostrado UMA vez; so o hash fica)."""
        u = validar_nome_usuario(usuario)
        nome = (nome or u).strip().replace("\n", " ")[:120]
        token = token or secrets.token_urlsafe(24)
        if len(token) < 12:
            raise ErroUsuario("token muito curto (minimo 12 caracteres)")
        sal = secrets.token_hex(16)
        texto = (
            "# Usuario do robowin (gerado por `robowin usuario criar`). Guarda so o HASH do token (nunca o token).\n"
            f"usuario = {_toml_str(u)}\nnome = {_toml_str(nome)}\ntoken_sal = {_toml_str(sal)}\n"
            f"token_sha256 = {_toml_str(_hash_token(sal, token))}\ncriado_em_utc = {_toml_str(_agora_utc())}\n")
        try:
            _gravar_novo(self.raiz / u / ARQ_USUARIO, texto)
        except FileExistsError as e:
            raise ErroUsuario(f"usuario '{u}' ja existe") from e
        self._auditar("usuario_criado", {"usuario": u, "nome": nome, "limites": "nenhum (bloqueado ate aprovar)"})
        return token

    def autenticar(self, usuario: str, token: str) -> bool:
        try:
            reg = carregar_toml(self.dir_usuario(usuario) / ARQ_USUARIO)
        except (OSError, ValueError, ErroUsuario):
            return False
        sal, esperado = str(reg.get("token_sal", "")), str(reg.get("token_sha256", ""))
        if not sal or not esperado or not token:
            return False
        return hmac.compare_digest(_hash_token(sal, token), esperado)

    def _exigir_usuario(self, usuario: str) -> str:
        u = validar_nome_usuario(usuario)
        if not self.existe(u):
            raise ErroUsuario(f"usuario '{u}' nao existe: crie com `robowin usuario criar --usuario {u}`")
        return u

    # ------------------------------------------------------------------ contas -------------------
    def registrar_conta(self, usuario: str, chave: ChaveConta, tipo: str = "demo") -> Conta:
        u = self._exigir_usuario(usuario)
        tipo = (tipo or "").strip().lower()
        if tipo not in TIPOS_CONTA:
            raise ErroUsuario(f"tipo de conta invalido '{tipo}': use demo ou real")
        d = self.raiz / u / "contas" / chave.pasta
        texto = (
            "# Conta de negociacao do usuario (gerado pelo robowin). autorizacao_real: nesta versao SEMPRE 'bloqueada'\n"
            "# (o envio real nao existe; conta real exige nova autorizacao especifica, ver docs/07-risco-e-operacao.md).\n"
            f"usuario = {_toml_str(u)}\nplataforma = {_toml_str(chave.plataforma)}\n"
            f"servidor = {_toml_str(chave.servidor)}\nlogin = {chave.login}\ntipo = {_toml_str(tipo)}\n"
            f"autorizacao_real = {_toml_str(AUTORIZACAO_REAL_BLOQUEADA)}\ncriada_em_utc = {_toml_str(_agora_utc())}\n")
        try:
            _gravar_novo(d / ARQ_CONTA, texto)
        except FileExistsError as e:
            raise ErroUsuario(f"conta {chave.texto} ja registrada para '{u}'") from e
        self._auditar("conta_registrada", {"usuario": u, "conta": chave.texto, "tipo": tipo,
                                           "autorizacao_real": AUTORIZACAO_REAL_BLOQUEADA})
        return self.conta(u, chave)

    def conta(self, usuario: str, chave: ChaveConta) -> Conta:
        u = self._exigir_usuario(usuario)
        d = self.raiz / u / "contas" / chave.pasta
        try:
            reg = carregar_toml(d / ARQ_CONTA)
        except OSError as e:
            raise ErroUsuario(f"conta {chave.texto} nao registrada para o usuario '{u}': registre no painel (Risco) "
                              f"ou com `robowin usuario conta --usuario {u} --conta {chave.texto}`") from e
        gravada = ChaveConta.criar(reg.get("plataforma", ""), reg.get("servidor", ""), reg.get("login", 0))
        if gravada != chave or reg.get("usuario") != u:  # pasta renomeada/copiada de outra conta ou de outro usuario
            raise ErroUsuario(f"conta.toml em {d} nao corresponde a {u}/{chave.texto}: recusado")
        tipo = str(reg.get("tipo", "real")).lower()
        return Conta(u, chave, tipo if tipo in TIPOS_CONTA else "real", d, str(reg.get("autorizacao_real", "")))

    def contas(self, usuario: str) -> list[Conta]:
        u = self._exigir_usuario(usuario)
        base = self.raiz / u / "contas"
        out: list[Conta] = []
        if base.is_dir():
            for p in sorted(base.iterdir()):
                try:
                    reg = carregar_toml(p / ARQ_CONTA)
                    out.append(self.conta(u, ChaveConta.criar(reg["plataforma"], reg["servidor"], reg["login"])))
                except (OSError, ValueError, KeyError, ErroConfig):
                    continue
        return out

    # ------------------------------------------------------------------ limites ------------------
    def limites(self, usuario: str, chave: ChaveConta) -> ConfigRisco | None:
        """Limites gravados da conta (None = o usuario ainda nao definiu: nada e imposto)."""
        c = self.conta(usuario, chave)
        if not c.risco.is_file():
            return None
        return ConfigRisco(**carregar_toml(c.risco))

    def gravar_limites(self, usuario: str, chave: ChaveConta, cfg: ConfigRisco | Mapping[str, Any], *,
                       autor: str, origem: str) -> str:
        """Valida (coerencia) e grava os limites da conta; registra versao no historico e evento na auditoria.
        A aprovacao anterior deixa de valer (hash diferente). Devolve o sha256 novo."""
        c = self.conta(usuario, chave)
        novo = validar_coerencia(cfg if isinstance(cfg, ConfigRisco) else config_de_formulario(cfg))
        anterior: ConfigRisco | None = None
        sha_ant: str | None = None
        if c.risco.is_file():
            sha_ant = sha256_arquivo(c.risco)
            try:
                anterior = ConfigRisco(**carregar_toml(c.risco))
            except Exception:  # noqa: BLE001 - arquivo antigo invalido: tudo conta como alterado
                anterior = None
        alterados = {n: [None if anterior is None else getattr(anterior, n), getattr(novo, n)]
                     for n in CAMPOS_RISCO if anterior is None or getattr(anterior, n) != getattr(novo, n)}
        if anterior is not None and not alterados:
            return sha_ant or sha256_arquivo(c.risco)
        texto = texto_risco(novo)
        _gravar(c.risco, texto)
        sha = sha256_arquivo(c.risco)
        hist = c.dir / "historico"
        seq = len(self.historico_limites(usuario, chave)) + 1
        _gravar_novo(hist / f"risco-{seq:04d}.toml", texto)
        with open(hist / "indice.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"seq": seq, "sha256": sha, "sha256_anterior": sha_ant, "gravado_em_utc": _agora_utc(),
                                "autor": autor, "origem": origem, "alterados": alterados}, ensure_ascii=False) + "\n")
        self._auditar("risco_limites_alterados", {
            "usuario": c.usuario, "conta": chave.texto, "sha256": sha, "sha256_anterior": sha_ant, "versao": seq,
            "autor": autor, "origem": origem, "alterados": alterados,
            "efeito": "aprovacao anterior invalidada (hash mudou): nova aprovacao exigida"})
        return sha

    def historico_limites(self, usuario: str, chave: ChaveConta) -> list[dict[str, Any]]:
        arq = self.conta(usuario, chave).dir / "historico" / "indice.jsonl"
        if not arq.is_file():
            return []
        out = []
        with open(arq, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
        return out

    def aprovacoes(self, usuario: str, chave: ChaveConta) -> list[dict[str, Any]]:
        """Registros de aprovacao da conta, do mais novo ao mais antigo, com `vigente` (hash confere com o atual)."""
        c = self.conta(usuario, chave)
        atual = sha256_arquivo(c.risco) if c.risco.is_file() else None
        out = []
        for n, arq in versoes_aprovacao(c.aprovacao):
            try:
                reg = carregar_toml(arq)
            except (OSError, ValueError):
                continue
            out.append({"versao": n, "arquivo": arq.name, **{k: reg.get(k) for k in (
                "sha256", "data", "responsavel", "motivo", "aprovado_em_utc", "provisoria", "escopo", "origem")},
                "vigente": atual is not None and reg.get("sha256") == atual})
        return out

    # ------------------------------------------------------------------ aprovacao ----------------
    def aprovar(self, usuario: str, chave: ChaveConta, responsavel: str, motivo: str, *,
                confirmar: Callable[[list[str]], str], hoje: dt.date | None = None,
                agora_utc: dt.datetime | None = None) -> ResultadoAprovacao:
        """Aprovacao EXPRESSA dos limites desta conta (exige digitar APROVO). Conta real: recusa sempre."""
        c = self.conta(usuario, chave)
        if c.tipo != "demo":
            raise AutorizacaoRealAusente(f"conta {chave.texto} e REAL: exige autorizacao_real especifica, que nao existe "
                                         "nesta versao (sempre bloqueada); nada foi aprovado")
        if not c.risco.is_file():
            raise AprovacaoRecusada("conta sem limites definidos: defina os limites antes de aprovar")
        extra = {"usuario": c.usuario, "conta": chave.texto, "tipo_conta": c.tipo, "escopo": "somente_demo",
                 "autorizacao_real": AUTORIZACAO_REAL_BLOQUEADA}
        return aprovar_politica(c.risco, c.dir_aprovacoes, responsavel, motivo, confirmar=confirmar,
                                auditoria=self.auditoria, hoje=hoje, agora_utc=agora_utc, extra_registro=extra)

    def migrar_legado(self, usuario: str, chave: ChaveConta, risco_legado: str | Path, aprovacao_legado: str | Path,
                      *, confirmar: Callable[[list[str]], str]) -> Path:
        """Copia a politica do modo de arquivo unico (`config/risco.toml` + aprovacao efetiva, ex. risco-v2) para ESTA
        conta DEMO do usuario, como aprovacao PROVISORIA somente DEMO (origem registrada). Exige APROVO."""
        from ..core.config import resolver_aprovacao, responsavel_pendente
        c = self.conta(usuario, chave)
        if c.tipo != "demo":
            raise AutorizacaoRealAusente("a migracao provisoria so vale para conta DEMO")
        if c.risco.is_file() or versoes_aprovacao(c.aprovacao):
            raise AprovacaoRecusada(f"a conta {chave.texto} ja tem limites/aprovacoes: a migracao so vale para conta nova")
        estado, detalhe = estado_aprovacao(risco_legado, aprovacao_legado)
        if estado != "APROVADO":
            raise AprovacaoRecusada(f"politica do arquivo unico nao esta aprovada ({estado}: {detalhe})")
        efetiva = resolver_aprovacao(aprovacao_legado, risco_legado)
        assert efetiva is not None
        reg = carregar_toml(efetiva)
        if responsavel_pendente(reg):  # defensivo (estado_aprovacao ja recusa)
            raise AprovacaoRecusada("aprovacao do arquivo unico com responsavel pendente")
        cfg = validar_coerencia(ConfigRisco(**carregar_toml(risco_legado)))
        from .aprovar import descrever_limites
        resumo = [f"Migrar {Path(risco_legado)} (aprovado em {efetiva.name}: {reg.get('responsavel')}) para",
                  f"usuario {c.usuario}, conta {chave.texto} (DEMO), como aprovacao PROVISORIA somente DEMO.", "",
                  *descrever_limites(cfg)]
        if (confirmar(resumo) or "").strip() != PALAVRA_APROVACAO:
            raise AprovacaoRecusada(f"confirmacao nao recebida (era preciso digitar {PALAVRA_APROVACAO}): nada foi alterado")
        sha = self.gravar_limites(c.usuario, chave, cfg, autor=str(reg.get("responsavel")), origem=f"migracao:{efetiva.name}")
        destino = c.dir_aprovacoes / "risco-v1.toml"
        texto = (
            "# Aprovacao MIGRADA do modo de arquivo unico (`robowin usuario migrar-legado`): PROVISORIA, somente DEMO.\n"
            "# NAO edite. Para mudar, altere os limites (painel/CLI) e aprove de novo (gera a proxima versao).\n"
            f"sha256 = {_toml_str(sha)}\ndata = {_toml_str(str(reg.get('data', '')))}\n"
            f"responsavel = {_toml_str(str(reg.get('responsavel', '')))}\nmotivo = {_toml_str(str(reg.get('motivo', '')))}\n"
            f"arquivo = {_toml_str(ARQ_RISCO)}\naprovado_em_utc = {_toml_str(str(reg.get('aprovado_em_utc', '')))}\n"
            f"usuario = {_toml_str(c.usuario)}\nconta = {_toml_str(chave.texto)}\ntipo_conta = \"demo\"\n"
            "escopo = \"somente_demo\"\nprovisoria = true\n"
            f"autorizacao_real = {_toml_str(AUTORIZACAO_REAL_BLOQUEADA)}\n"
            f"origem = {_toml_str(efetiva.name)}\norigem_sha256_registro = {_toml_str(sha256_arquivo(efetiva))}\n"
            f"migrado_em_utc = {_toml_str(_agora_utc())}\n")
        _gravar_novo(destino, texto)
        self._auditar("risco_migrado_legado", {"usuario": c.usuario, "conta": chave.texto, "sha256": sha,
                                               "origem": efetiva.name, "responsavel": reg.get("responsavel"),
                                               "provisoria": True, "escopo": "somente_demo"})
        return destino

    # ------------------------------------------------------------------ estado e uso -------------
    def estado(self, usuario: str, chave: ChaveConta) -> EstadoRiscoConta:
        c = self.conta(usuario, chave)
        sha = sha256_arquivo(c.risco) if c.risco.is_file() else None
        aprs = self.aprovacoes(usuario, chave)
        vigente = next((a for a in aprs if a["vigente"]), None)
        provisoria = bool(vigente and vigente.get("provisoria"))
        if sha is None:
            est, det = SEM_LIMITES, "nenhum limite definido: defina e aprove (painel > Risco); operar esta BLOQUEADO"
        elif not aprs:
            est, det = NAO_APROVADO, "limites definidos mas nunca aprovados: aprovacao expressa exigida (APROVO)"
        elif vigente is None:
            est, det = NAO_APROVADO, ("limites alterados depois da ultima aprovacao (hash difere): aprove de novo")
        else:
            e, d = estado_aprovacao(c.risco, c.aprovacao)
            est, det = (APROVADO, d) if e == "APROVADO" else (INVALIDO, d)
        if c.tipo != "demo" and est == APROVADO:
            det += "; conta REAL: autorizacao_real bloqueada (nao opera)"
        return EstadoRiscoConta(c.usuario, chave.texto, c.tipo, est, det, provisoria, c.autorizacao_real, sha)

    def exigir_operacao(self, usuario: str, chave: ChaveConta, perfil: PerfilInstrumento) -> ConfigRisco:
        """Limites para OPERAR (nucleo ao vivo, espelho/MM na demo, gateway, aceitacao com ordens). Recusa
        (ErroConfig) conta real, conta sem limites, sem aprovacao ou com hash divergente."""
        c = self.conta(usuario, chave)
        if c.tipo != "demo":
            raise AutorizacaoRealAusente(
                f"conta {chave.texto} e REAL: operacao recusada. Conta real exige autorizacao_real especifica, que nao "
                "existe nesta versao (estado sempre 'bloqueada'); o envio real nao existe")
        st = self.estado(usuario, chave)
        if st.estado != APROVADO:
            raise ErroUsuario(f"usuario {c.usuario}, conta {chave.texto}: risco {st.estado} ({st.detalhe}). Operacao "
                              "automatizada BLOQUEADA ate aprovacao expressa (painel > Risco ou `robowin risco aprovar "
                              f"--usuario {c.usuario} --conta {chave.texto}`)")
        cfg, _ = risco_do_ativo(perfil, c.risco, c.aprovacao, ao_vivo=True)
        return cfg

    def risco_simulacao(self, usuario: str, chave: ChaveConta, perfil: PerfilInstrumento
                        ) -> tuple[ConfigRisco, str | None]:
        """Limites para SIMULACAO (pode rodar sem aprovacao, rotulada): os aprovados; senao os do proprio usuario (se
        coerentes) ou o modelo conservador, com erro (=> rotulo `risco_nao_aprovado`). Nunca os de outro usuario."""
        c = self.conta(usuario, chave)
        if not c.risco.is_file():
            return modelo_inicial(perfil), (f"usuario {c.usuario}, conta {chave.texto} sem limites definidos: simulacao "
                                            "com o modelo conservador NAO aprovado")
        cfg, erro = risco_do_ativo(perfil, c.risco, c.aprovacao)
        if erro is not None and erro != ERRO_APROVACAO_PENDENTE and perfil.risco_aprovado_padrao:
            try:
                cfg = validar_coerencia(ConfigRisco(**carregar_toml(c.risco)))
            except Exception:  # noqa: BLE001
                cfg = modelo_inicial(perfil)
        return cfg, erro


# ====================================================================== selecao (CLI / config) ==========
def dir_usuarios(config_path: str | Path | None = None, override: str | Path | None = None) -> Path:
    """Pasta do armazem: `override` > ROBOWIN_USUARIOS > [geral].usuarios (relativo a raiz do projeto) >
    <raiz>/config/usuarios (raiz = pasta pai da pasta do TOML)."""
    if override:
        return Path(override).resolve()
    if os.environ.get("ROBOWIN_USUARIOS"):
        return Path(os.environ["ROBOWIN_USUARIOS"]).resolve()
    base = Path.cwd()
    bruto: str = "config/usuarios"
    if config_path and Path(config_path).is_file():
        base = Path(config_path).resolve().parent.parent
        bruto = str(carregar_toml(config_path).get("geral", {}).get("usuarios", "") or bruto)
    p = Path(bruto)
    return p if p.is_absolute() else (base / p).resolve()


@dataclass(frozen=True)
class SelecaoConta:
    """Usuario + conta ativos. `ativa` False => modo de arquivo unico (compatibilidade)."""
    usuario: str | None
    chave: ChaveConta | None
    raiz: Path

    @property
    def ativa(self) -> bool:
        return self.usuario is not None and self.chave is not None

    def armazem(self, auditoria: Any | None = None) -> ArmazemUsuarios:
        return ArmazemUsuarios(self.raiz, auditoria)

    def descricao(self) -> str:
        return f"usuario {self.usuario}, conta {self.chave.texto}" if self.chave else "modo arquivo unico (config/risco.toml)"


def selecao_de(config_path: str | Path | None, usuario: str | None = None, conta: str | None = None,
               usuarios: str | Path | None = None) -> SelecaoConta:
    """`--usuario/--conta` > [geral].usuario / [geral].conta_negociacao. Os dois juntos ou nenhum."""
    g: dict[str, Any] = {}
    if config_path and Path(config_path).is_file():
        g = carregar_toml(config_path).get("geral", {}) or {}
    u = (usuario or str(g.get("usuario", "") or "")).strip() or None
    c = (conta or str(g.get("conta_negociacao", "") or "")).strip() or None
    if bool(u) != bool(c):
        raise ErroUsuario("informe usuario E conta juntos (--usuario U --conta PLATAFORMA:SERVIDOR:LOGIN, ou "
                          "[geral].usuario e [geral].conta_negociacao); sem nenhum dos dois vale o arquivo unico")
    raiz = dir_usuarios(config_path, usuarios)
    if u is None or c is None:
        return SelecaoConta(None, None, raiz)
    return SelecaoConta(validar_nome_usuario(u), ChaveConta.de_texto(c), raiz)


def risco_para(sel: SelecaoConta, perfil: PerfilInstrumento, risco_legado: str | Path,
               aprovacao_legado: str | Path | None, *, ao_vivo: bool) -> tuple[ConfigRisco, str | None, Path]:
    """(limites, erro, arquivo aplicado) do usuario+conta ativos ou, sem selecao, do arquivo unico.
    Ao vivo, erro vira excecao (ErroConfig); na simulacao o erro rotula `risco_nao_aprovado`."""
    if not sel.ativa:
        cfg, erro = risco_do_ativo(perfil, risco_legado, aprovacao_legado, ao_vivo=ao_vivo)
        return cfg, erro, Path(risco_legado)
    assert sel.usuario is not None and sel.chave is not None
    a = sel.armazem()
    c = a.conta(sel.usuario, sel.chave)
    if ao_vivo:
        return a.exigir_operacao(sel.usuario, sel.chave, perfil), None, c.risco
    cfg, erro = a.risco_simulacao(sel.usuario, sel.chave, perfil)
    return cfg, erro, c.risco


def adicionar_args(p: Any) -> None:
    """`--usuario`, `--conta` e `--usuarios` (mesmo significado em todos os comandos que operam)."""
    p.add_argument("--usuario", default=None,
                   help="usuario dono dos limites (padrao: [geral].usuario; vazio = modo arquivo unico config/risco.toml)")
    p.add_argument("--conta", default=None, metavar="PLATAFORMA:SERVIDOR:LOGIN",
                   help="conta de negociacao cujos limites valem (padrao: [geral].conta_negociacao)")
    p.add_argument("--usuarios", default=None,
                   help="pasta do armazem de usuarios (padrao: ROBOWIN_USUARIOS, [geral].usuarios ou config/usuarios)")


# ====================================================================== isolamento da IA ==============
SECOES_IA = ("ia", "bracos", "reflexao", "prazos", "decisao")


def validar_ia_sem_limites(cfg: Mapping[str, Any]) -> None:
    """A configuracao de IA/LLM NAO pode carregar limites de risco: qualquer campo de ConfigRisco (ou `risco`,
    `limites`, `usuario`, `conta_negociacao`) dentro de [ia], [[bracos]], [reflexao], [prazos] ou [decisao] e recusado.
    Os limites vivem SO no armazem do usuario/conta (ou no arquivo unico) e sao aplicados pelo veto deterministico."""
    proibidos = set(CAMPOS_RISCO) | {"risco", "limites", "limites_risco", "usuario", "conta_negociacao", "aprovacao"}
    achados: list[str] = []

    def varrer(no: Any, caminho: str) -> None:
        if isinstance(no, Mapping):
            for k, v in no.items():
                if str(k) in proibidos:
                    achados.append(f"{caminho}.{k}")
                varrer(v, f"{caminho}.{k}")
        elif isinstance(no, list):
            for i, v in enumerate(no):
                varrer(v, f"{caminho}[{i}]")

    for sec in SECOES_IA:
        if sec in cfg:
            varrer(cfg[sec], sec)
    if achados:
        raise ErroConfig("configuracao de IA nao pode definir/alterar limites de risco (so o usuario, no painel/CLI, "
                         f"com aprovacao): {', '.join(achados)}")
