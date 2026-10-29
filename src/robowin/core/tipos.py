"""Tipos de dominio congelados (M0). Dinheiro em reais; precos em pontos de cotacao do ativo (perfil)."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer


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


# ---- gestao da posicao (braco planejador; docs/16) ------------------------------------------------------------
# Adicoes opcionais: nenhum tipo antigo muda de comportamento. Tudo em PONTOS, UNIDADES e SEGUNDOS (nunca dinheiro).
TipoRegraGestao = Literal["breakeven", "trailing", "parcial", "sem_progresso", "apertar_apos", "ao_vencer_tempo"]
GatilhoReavaliacao = Literal["perto_do_stop", "perto_do_alvo", "metade_do_tempo", "regra_disparou",
                             "lucro_acima_stop"]


class RegraGestao(Base):
    """Regra de gestao automatica declarada pela IA; executada a cada tick por codigo puro (decisao/gestao.py)."""
    tipo: TipoRegraGestao
    mfe_pontos: float | None = None  # excursao favoravel minima (pontos, desde a entrada) para ativar
    apos_s: int | None = None  # segundos desde a abertura para ativar
    distancia_pontos: float | None = None  # trailing/apertar_apos: distancia stop-preco (ou stop-extremo)
    folga_ticks: int | None = None  # breakeven: entrada + custo ida e volta + folga
    quantidade: int | None = None  # parcial: unidades a realizar (1..q-1)
    acao_vencimento: Literal["encerrar", "proteger_e_estender"] | None = None
    extensao_s: int | None = None


class AjustePosicao(Base):
    """So com acao=MANTER. Niveis em PONTOS DE RESULTADO desde o preco medio de entrada."""
    stop_resultado_pontos: float | None = None  # -30 perde 30; 0 empate bruto; +10 trava 10
    alvo_resultado_pontos: float | None = None
    tempo_max_total_s: int | None = None  # total desde a abertura
    reduzir_quantidade: int | None = None  # 1..q-1
    substituir_gestao: tuple[RegraGestao, ...] | None = None


class RegraCompilada(Base):
    """Mesmos campos de RegraGestao, ja em pontos na grade do tick (decisao/compilador)."""
    tipo: str
    mfe_pontos: float | None = None
    apos_s: int | None = None
    distancia_pontos: float | None = None
    folga_pontos: float | None = None
    quantidade: int | None = None
    acao_vencimento: str | None = None
    extensao_s: int | None = None


class PlanoGestaoCompilado(Base):
    regras: tuple[RegraCompilada, ...] = ()
    passo_min_pontos: float
    intervalo_ajuste_s: float
    reavaliar_em_s: int | None = None
    reavaliar_se: tuple[str, ...] = ()
    versao_dsl: int = 1


# campos opcionais acrescentados a Intencao (docs/16, WP0): ausentes (None) NAO entram na serializacao, para que a
# Intencao antiga serialize exatamente igual (intencao_json, auditoria e hashes de sessao nao mudam).
CAMPOS_OPCIONAIS_INTENCAO = ("posicao_ticket", "quantidade_t0", "gestao", "ajuste", "ancora_t0", "p_alvo")


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
    # adicoes opcionais (braco planejador): o padrao reproduz o comportamento atual
    posicao_ticket: int | None = None  # ticket da posicao vista em t0 (MANTER/ENCERRAR presos a ela)
    quantidade_t0: int | None = None  # quantidade da posicao vista em t0
    gestao: PlanoGestaoCompilado | None = None
    ajuste: AjustePosicao | None = None
    ancora_t0: float | None = None  # presente => niveis da entrada ancorados no preco de t0
    p_alvo: float | None = None  # so pontuada (Brier/calibracao); nunca libera nem bloqueia entrada

    @model_serializer(mode="wrap")
    def _serializar(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        d: dict[str, Any] = handler(self)
        for k in CAMPOS_OPCIONAIS_INTENCAO:
            if k in d and d[k] is None:
                del d[k]
        return d


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


class PlanoTradeIA(Base):
    """Esquema do papel ``planejador``: a decisao de trading inteira (docs/16 1.2). Nenhum campo chega a um limite
    de risco (extra=forbid): o veto e sempre do motor de risco. Distancias em pontos a partir da referencia de t0."""
    acao: Acao
    hipoteses: tuple[str, ...] = ()
    evidencias_a_favor: tuple[str, ...] = ()
    evidencias_contra: tuple[str, ...] = ()
    riscos: tuple[str, ...] = ()
    invalidacoes: tuple[str, ...] = ()
    justificativa_curta: str = ""
    incerteza: float = Field(ge=0, le=1, default=0.5)
    p_alvo: float | None = Field(default=None, ge=0, le=1)  # P(alvo antes do stop e do tempo); so pontuada
    quantidade: int = 1
    stop_pontos: float | None = None  # distancia a partir da referencia de t0 (nivel absoluto fixo)
    alvo_pontos: float | None = None
    tempo_max_s: int | None = None
    gatilhos: tuple[Condicao, ...] = ()  # DSL existente; vazio = "agora" (dentro da faixa)
    faixa_contra_pontos: float | None = None
    faixa_favor_pontos: float | None = None
    expira_s: int = 120
    gestao: tuple[RegraGestao, ...] = ()
    reavaliar_em_s: int | None = None
    reavaliar_se: tuple[GatilhoReavaliacao, ...] = ()
    manter_plano_vigente: bool = False
    ajuste: AjustePosicao | None = None


class RevisaoPlanoIA(Base):
    """Revisao do plano (papel ``revisor_plano``). Os booleanos NAO tem padrao: aprovar exige declarar os tres."""
    veredito: Literal["APROVAR", "REJEITAR"]
    coerente: bool
    dados_ok: bool
    regras_ok: bool
    afirmacoes_sem_suporte: tuple[str, ...] = ()
    p_alvo_revisor: float | None = Field(default=None, ge=0, le=1)
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
    # adicao opcional: teto do usuario para o tempo TOTAL de uma posicao (segundos desde a abertura). O padrao e o
    # maior tempo fixo de antes (LLM com 300 s): arquivo aprovado sem o campo continua valendo, com o mesmo hash.
    tempo_max_posicao_s: int = 300


class EnvelopeDecisao(Base):
    """Envelope DERIVADO dos limites, somente leitura, em pontos/unidades/segundos (nunca dinheiro). Calculado SO
    por ``MotorRiscoPadrao.envelope`` (mesma funcao que o motor usa no veto)."""
    unidades_max: int
    stop_max_pontos_por_q: tuple[float, ...]  # indice q-1; ja descontados perda op/dia/semana, folga e fator
    stop_min_pontos: float
    dist_min_ajuste_pontos: float
    tempo_max_s: int  # total desde a abertura (posicao atual ou nova)
    entradas_possiveis: bool
    motivos_sem_entrada: tuple[str, ...] = ()  # codigos, sem valores numericos


class ConfigPlanejador(Base):
    """``[planejador]``: parametros PRE-REGISTRADOS da variante. So restringem a IA; nunca sao limite de risco."""
    ativo: bool = False
    expor_envelope: Literal["envelope", "nenhum"] = "envelope"
    k_custo: float = 1.0  # alvo restante > k*custo_ida_volta_estresse + 1 tick
    razao_liquida_min: float = 0.0  # 0 = desligado (sem geometria imposta)
    max_regras: int = 6
    tolerancia_max_ticks: int = 8
    tolerancia_max_fracao_stop: float = 0.5
    tempo_min_s: int = 30
    margem_zeragem_s: int = 120
    expira_max_s: int = 600
    reavaliar_min_s: int = 10
    passo_min_ticks: int = 1
    passo_min_ticks_espelhado: int = 2
    intervalo_ajuste_s: float = 2.0
    reserva_gestao_frac: float = 0.3
    exigir_p_alvo: bool = True


class PreRegistro(Base):
    """Cadeia de hash do experimento (avaliacao/preregistro grava e verifica)."""
    id_experimento: str
    t_registro_ns: int
    inicio_avaliacao: str
    variantes: dict[str, str]
    comparacoes_primarias: tuple[str, ...]
    margens_nao_inferioridade: dict[str, float]
    metrica_unidade: Literal["pregao"] = "pregao"
    alfa: float = 0.05
    n_pregoes_alvo: int
    regras_parada: tuple[str, ...] = ()
    hash_anterior: str = ""
    hash_registro: str = ""


class RegistroAprovacao(Base):
    arquivo: str
    sha256: str
    data: str
    responsavel: str
    motivo: str
