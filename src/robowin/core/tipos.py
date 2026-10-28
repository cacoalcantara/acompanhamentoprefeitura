"""Tipos de dominio congelados (M0). Dinheiro em reais; precos em pontos de cotacao do ativo (perfil)."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Lado(StrEnum):
    COMPRA = "compra"
    VENDA = "venda"


class Acao(StrEnum):
    COMPRAR = "COMPRAR"
    VENDER = "VENDER"
    MANTER = "MANTER"
    ENCERRAR = "ENCERRAR"
    NAO_OPERAR = "NAO_OPERAR"


class Envelope(Base):
    schema_v: int = 1
    seq: int
    t_evento_ns: int
    t_disponivel_ns: int
    correlacao_id: str = ""


class Heartbeat(Base):
    origem: str
    hb: int
    t_ns: int


class Instrumento(Base):
    simbolo: str  # simbolo da corretora (ex.: contrato com vencimento)
    ponto_valor_brl: float  # sempre do perfil (core.instrumentos), nunca fixo no codigo
    tick_pontos: float
    lote: int = 1
    ativo: str = ""  # simbolo_base do perfil (ex.: o ativo cujo perfil gerou este instrumento)
    moeda: str = "BRL"  # moeda do P&L, custos e risco deste instrumento
    vencimento: str | None = None  # AAAA-MM-DD
    filling_mode: int = 0
    trade_exemode: int = 0
    book_depth: int = 0
    # adicoes opcionais (qualquer mercado): o padrao preserva o contrato antigo
    volume_unidade: float = 0.0  # volume do terminal por unidade do robo (0 = `lote`); ex.: 0.01 em forex
    moeda_conta: str = ""  # moeda da conta (limites de risco); vazio/igual a `moeda` = sem conversao

    @property
    def tick_valor_brl(self) -> float:
        return self.tick_pontos * self.ponto_valor_brl

    @property
    def volume_por_unidade(self) -> float:
        """Volume enviado ao terminal por unidade de `quantidade` (lote fracionario em forex/CFD)."""
        return self.volume_unidade if self.volume_unidade > 0 else float(self.lote)

    @property
    def precisa_conversao(self) -> bool:
        """P&L do simbolo em moeda diferente da conta: exige cotacao de conversao no instante."""
        return bool(self.moeda_conta) and self.moeda_conta.upper() != self.moeda.upper()


class Tick(Base):
    t_servidor_ms: int
    t_utc_ns: int
    t_recebido_ns: int
    bid: float
    ask: float
    last: float
    volume: float = 0.0
    flags: int = 0

    @property
    def meio(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last


class NivelBook(Base):
    preco: float
    volume: float
    lado: Literal["compra", "venda"]


class LivroSnapshot(Base):
    t_utc_ns: int
    niveis: tuple[NivelBook, ...]


class Perna(Base):
    simbolo: str
    lado: Lado
    quantidade: int = 1


class Condicao(Base):
    """DSL permitida do plano condicional. Nao existe construcao de indicador."""
    tipo: Literal["preco_toca", "preco_rompe_acima", "preco_rompe_abaixo", "spread_max_pontos", "janela_tempo"]
    valor: float | None = None  # pontos relativos a referencia (ou segundos)
    ate_ns: int | None = None


class PlanoCondicional(Base):
    gatilhos: tuple[Condicao, ...]
    invalidacoes: tuple[Condicao, ...] = ()
    expira_ns: int


class Intencao(Base):
    id: str
    braco: str
    acao: Acao
    quantidade: int = 1
    stop_pontos: float | None = None
    alvo_pontos: float | None = None
    tempo_max_s: int | None = None
    validade_ns: int
    modo: Literal["imediato", "condicional", "quant"] = "imediato"
    plano: PlanoCondicional | None = None
    preco_referencia: float = 0.0
    proposta_id: str | None = None
    revisao_id: str | None = None
    t0_ns: int = 0


class DecisaoRisco(Base):
    intencao_id: str
    aprovada: bool
    motivos: tuple[str, ...] = ()


class ComandoOrdem(Base):
    intencao_id: str
    magic: int
    lado: Lado
    tipo: Literal["mercado", "limitada", "saida"]
    quantidade: int = 1
    preco: float | None = None
    sl: float | None = None
    tp: float | None = None
    position_ticket: int | None = None


class AckOrdem(Base):
    retcode: int | None
    order: int = 0
    deal: int = 0
    t_setup_msc: int = 0
    t_done_msc: int = 0
    preco: float = 0.0
    comentario: str = ""


class EstadoOrdem(StrEnum):
    NOVA = "NOVA"
    ENVIADA = "ENVIADA"
    INCERTA = "INCERTA"
    ACEITA = "ACEITA"
    PARCIAL = "PARCIAL"
    EXECUTADA = "EXECUTADA"
    CANCELADA = "CANCELADA"
    REJEITADA = "REJEITADA"
    EXPIRADA = "EXPIRADA"


class Execucao(Base):
    ordem_id: str
    preco: float
    quantidade: int
    t_ns: int
    taxas_brl: float = 0.0
    simulada: bool = True


class Posicao(Base):
    simbolo: str
    lado: Lado | None = None
    quantidade: int = 0
    preco_medio: float = 0.0
    ticket: int | None = None
    stop: float | None = None
    alvo: float | None = None
    aberta_ns: int = 0


class Trade(Base):
    id: str
    braco: str
    lado: Lado
    entrada: float
    saida: float
    t_entrada_ns: int
    t_saida_ns: int
    bruto_brl: float
    tarifas_brl: float
    corretagem_brl: float
    slippage_brl: float
    motivo_saida: str
    swap_brl: float = 0.0  # adicao opcional: swap PAGO (positivo) ou recebido (negativo) ao atravessar o rollover

    @property
    def liquido_brl(self) -> float:
        return self.bruto_brl - self.tarifas_brl - self.corretagem_brl - self.slippage_brl - self.swap_brl


class EstadoConta(Base):
    login: int
    servidor: str
    trade_mode: Literal["demo", "contest", "real"]
    margin_mode: str = ""
    trade_allowed: bool = False
    trade_expert: bool = False


class CarimboLatencia(Base):
    etapa: str
    braco: str = ""
    t_ini_ns: int
    t_fim_ns: int
    injetado: bool = False

    @property
    def dur_ms(self) -> float:
        return (self.t_fim_ns - self.t_ini_ns) / 1e6


class SessaoPregao(Base):
    vigente_desde: str
    abertura: str
    fechamento: str
    entradas_ate: str
    zeragem: str


class VetorFeatures(Base):
    t_ns: int
    valores: dict[str, float]


class ChamadaLLM(Base):
    papel: str
    modelo_pedido: str
    modelo_servido: str = ""
    request_id: str = ""
    versao_api: str = ""
    hash_prompt: str = ""
    hash_dossie: str = ""
    resposta_bruta: str = ""
    stop_reason: str = ""
    categoria_recusa: str | None = None
    tokens_entrada: int = 0
    tokens_cache: int = 0
    tokens_saida: int = 0
    custo_usd: float = 0.0
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    valida: bool = False
    erro: str | None = None


class PropostaIA(Base):
    """Neutra quanto ao fornecedor."""
    acao: Acao
    hipoteses: tuple[str, ...] = ()
    evidencias_a_favor: tuple[str, ...] = ()
    evidencias_contra: tuple[str, ...] = ()
    riscos: tuple[str, ...] = ()
    incerteza: float = Field(ge=0, le=1, default=0.5)
    stop_pontos: float | None = None
    alvo_pontos: float | None = None
    validade_s: int = 60
    invalidacoes: tuple[str, ...] = ()
    justificativa_curta: str = ""
    plano: PlanoCondicional | None = None


class RevisaoIA(Base):
    veredito: Literal["APROVAR", "REJEITAR"]
    coerente: bool = True
    dados_ok: bool = True
    regras_ok: bool = True
    afirmacoes_sem_suporte: tuple[str, ...] = ()
    justificativa_curta: str = ""


class ClassificacaoNoticia(Base):
    relevancia: float = Field(ge=0, le=1)
    direcao: Literal["alta", "baixa", "neutra"]
    intensidade: float = Field(ge=0, le=1)
    horizonte_min: int = 0
    tipo: str = ""
    confianca: float = Field(ge=0, le=1, default=0.5)


class EventoNoticia(Base):
    id: str
    fonte: str
    titulo: str
    t_publicado_ns: int
    t_disponivel_ns: int
    t_recebido_ns: int
    verificado: bool = False


class ManifestoExperimento(Base):
    id: str
    hipoteses: tuple[str, ...]
    metrica_primaria: str
    mde_brl: float
    alfa: float = 0.05
    n_alvo: int
    regras_parada: tuple[str, ...] = ()
    modelos: dict[str, str] = Field(default_factory=dict)
    fixavel: dict[str, bool] = Field(default_factory=dict)
    hashes: dict[str, str] = Field(default_factory=dict)
    origem_dados: str = "desconhecida"  # "sinteticos" | "historico_real" | "desconhecida"
    ativo: str = ""  # simbolo_base do perfil: trocar o ativo abre OUTRO experimento (outro hash)


class EntradaRegistroModelo(Base):
    nome: str
    versao: str
    ativo: str = ""  # simbolo_base do perfil em que foi treinado; nunca reutilizado em outro ativo
    hash_dados: str
    hash_codigo: str
    status: Literal["nao_treinado", "em_validacao", "validado_oos", "reprovado"] = "nao_treinado"
    treinado_ate_ns: int = 0
    metricas: dict[str, float] = Field(default_factory=dict)


class EntradaCatalogoModelo(Base):
    provedor: str
    id: str
    fixavel: bool
    versao: str = ""
    corte_conhecimento: str
    preco_entrada_mtok: float
    preco_saida_mtok: float
    preco_cache_mtok: float = 0.0
    saida_estruturada: Literal["nativa", "json_schema", "json"] = "nativa"
    streaming: bool = True
    notas: str = ""
    # declaracoes explicitas (robowin ia adicionar-modelo); adicoes opcionais, o padrao preserva o contrato antigo
    preco_desconhecido: bool = False  # preco nao informado: teto conservador por chamada e rotulo no manifesto
    teto_chamada_usd: float = 0.0     # teto por chamada usado quando preco_desconhecido (0 = padrao do catalogo)
    corte_desconhecido: bool = False  # sem data de corte: contaminado em QUALQUER backtest historico
    local: bool = False               # modelo local (custo de API zero)


class Papel(Base):
    nome: str
    modelo: str  # "provedor:id"
    effort: str | None = None
    parametros: dict[str, Any] = Field(default_factory=dict)


class ConfigRisco(Base):
    contratos_max: int = 1
    perda_max_operacao_brl: float = 60.0
    perda_max_dia_brl: float = 200.0
    perda_max_semana_brl: float = 500.0
    dd_alerta_brl: float = 1000.0
    dd_max_brl: float = 1500.0
    operacoes_dia_max: int = 10
    perdas_seguidas_max: int = 3
    pausa_apos_perda_s: int = 300
    stop_max_pontos: float = 300.0
    slippage_folga_pontos: float = 50.0
    entradas_de: str = "09:15"
    entradas_ate: str = "17:30"
    zeragem: str = "17:45"
    bloqueio_evento_min: int = 5
    ordens_min_max: int = 20
    tempo_min_livro_s: float = 2.0
    # adicoes opcionais (qualquer mercado). Valores em dinheiro estao NA MOEDA DA CONTA. 0 = desligado:
    # vale `contratos_max` em unidades do perfil (1 contrato no WIN; 1 lote minimo em forex/CFD).
    volume_max_lotes: float = 0.0  # volume maximo do terminal (lotes) por posicao/exposicao
    nocional_max_conta: float = 0.0  # nocional maximo (preco x valor do ponto x quantidade), moeda da conta


class RegistroAprovacao(Base):
    arquivo: str
    sha256: str
    data: str
    responsavel: str
    motivo: str
