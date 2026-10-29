"""Motor de risco: veto final, deterministico e independente de IA.

`avaliar` nunca levanta excecao: qualquer erro interno vira veto (falha segura). Saidas (ENCERRAR) e
apertos de stop nunca sao bloqueados; entradas passam por todas as regras e todos os motivos de veto
sao coletados (auditoria completa), nao so o primeiro. O despacho e *fail-closed*: so COMPRAR/VENDER vao para
as regras de entrada; acao nao tratada explicitamente recebe `acao_desconhecida`.

O motor NUNCA le dossie nem proposta de IA: decide so com a `Intencao` (pontos/unidades/segundos), a posicao, a
contabilidade e a politica aprovada. `envelope` publica, so para leitura, o que cabe nos limites (mesmas contas
do veto), sem nenhum valor em dinheiro.
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..core import calendario_b3
from ..core.calendario_b3 import SP
from ..core.moedas import motivo_sem_cotacao
from ..core.protocolos import RegistroAuditoria
from ..core.sessoes import SessaoInstrumento, sessao_especifica
from ..core.tipos import (
    Acao,
    Base,
    ConfigRisco,
    DecisaoRisco,
    EnvelopeDecisao,
    Instrumento,
    Intencao,
    Lado,
    Posicao,
    RegraCompilada,
    RegraGestao,
    SessaoPregao,
)
from .invariantes import inv_distancia_minima, stop_so_aperta
from .kill_switch import KillSwitch
from .politica import (
    DIST_MIN_STOP_TICKS,
    STOP_MIN_TICKS,
    perda_pior_caso_brl,
    stop_max_por_quantidade,
    unidades_maximas,
    validar_coerencia,
)
from .sessao import ContabilidadeSessao, EstadoBraco
from .tempo import dia_sp, hhmm_para_s, segundos_do_dia

NS = 1_000_000_000
_EPS = 1e-9
# conjunto FECHADO de regras de gestao que o motor aceita (rechecagem independente do validador da IA)
TIPOS_GESTAO = frozenset({"breakeven", "trailing", "parcial", "sem_progresso", "apertar_apos", "ao_vencer_tempo"})
_CAMPOS_NUMERICOS_GESTAO = ("mfe_pontos", "apos_s", "distancia_pontos", "folga_pontos", "folga_ticks",
                            "extensao_s")


class EstadoMercado(Base):
    """Flags de regime alimentadas por dados/features (nao pela IA)."""
    feed_ok: bool = True
    dado_critico: bool = False
    vol_extrema: bool = False
    leilao: bool = False
    limite_oscilacao: bool = False
    rolagem: bool = False
    spread_pts: float = 0.0  # spread corrente (pontos): entra na distancia minima de stop/alvo ajustado


def _na_grade(x: float, tick: float, modo: str) -> float:
    """Leva `x` para a grade do tick: "cima", "baixo" ou "perto" (sem tick valido, devolve x)."""
    if not (tick > 0 and math.isfinite(tick)):
        return x
    k = x / tick
    n = math.ceil(k - _EPS) if modo == "cima" else math.floor(k + _EPS) if modo == "baixo" else round(k)
    return round(n * tick, 10)


class EventoAgenda(Base):
    t_ns: int
    nome: str = "evento"


def _sessao_auto(ins: Instrumento) -> SessaoInstrumento | None:
    """Sessao do perfil do instrumento quando difere do legado B3 (forex 24x5, cripto 24x7, outro fuso)."""
    try:
        from ..core.instrumentos import perfil_de_instrumento
        return sessao_especifica(perfil_de_instrumento(ins))
    except Exception:  # noqa: BLE001 - instrumento sem perfil (testes) => relogio legado
        return None


@dataclass
class _Pendente:
    braco: str
    lado: Lado
    quantidade: int


class MotorRiscoPadrao:
    """Implementa core.protocolos.MotorRisco."""

    def __init__(
        self,
        cfg: ConfigRisco,
        instrumento: Instrumento,
        *,
        kill_switch: KillSwitch | None = None,
        contabilidade: ContabilidadeSessao | None = None,
        auditoria: RegistroAuditoria | None = None,
        eventos: Iterable[EventoAgenda] = (),
        grade: Callable[[dt.date], SessaoPregao] = calendario_b3.sessao_em,
        eh_pregao: Callable[[dt.date], bool] = calendario_b3.eh_pregao,
        sessao: SessaoInstrumento | None | str = "auto",
        conversor: Callable[[int], float | None] | None = None,
    ) -> None:
        """`sessao` = relogio de sessao do instrumento ("auto": o do perfil quando difere do legado B3, ex.: forex
        24x5, cripto 24x7, bolsa em outro fuso; None = legado `grade`/`eh_pregao` em Sao Paulo). `conversor(t_ns)` =
        fator moeda de lucro -> moeda da conta NO INSTANTE (None/ausente com moedas diferentes => entradas vetadas)."""
        self.cfg = validar_coerencia(cfg)
        self.instrumento = instrumento
        self.kill = kill_switch or KillSwitch()
        self.sessao: SessaoInstrumento | None = _sessao_auto(instrumento) if sessao == "auto" else (
            sessao if isinstance(sessao, SessaoInstrumento) else None)
        self.contab = contabilidade or ContabilidadeSessao(
            dia_de=self.sessao.dia_negociacao if self.sessao is not None else dia_sp)
        self.auditoria = auditoria
        self.eventos: list[EventoAgenda] = list(eventos)
        self.mercado = EstadoMercado()
        self._grade = grade
        self._eh_pregao = eh_pregao
        self._pendentes: dict[str, _Pendente] = {}
        self.conversor = conversor
        self.unidades_max = unidades_maximas(self.cfg, instrumento.volume_por_unidade)

    def definir_conversor(self, conversor: Callable[[int], float | None] | None) -> None:
        self.conversor = conversor

    def fator_conversao(self, t_ns: int) -> float | None:
        """Unidades da moeda da conta por 1 da moeda de lucro do simbolo, no instante (1.0 se mesma moeda)."""
        if not self.instrumento.precisa_conversao:
            return 1.0
        if self.conversor is None:
            return None
        try:
            f = self.conversor(t_ns)
        except Exception:  # noqa: BLE001 - falha segura: sem cotacao
            return None
        return f if f is not None and math.isfinite(f) and f > 0 else None

    def _dia(self, t_ns: int) -> dt.date:
        return self.sessao.dia_negociacao(t_ns) if self.sessao is not None else dia_sp(t_ns)

    def _aberto(self, t_ns: int) -> bool:
        if self.sessao is not None:
            return self.sessao.aberto(t_ns)[0]
        return self._eh_pregao(dia_sp(t_ns))

    # ------------------------------------------------------------------ estado externo -------------
    def definir_eventos(self, eventos: Iterable[EventoAgenda]) -> None:
        self.eventos = list(eventos)

    def atualizar_mercado(self, estado: EstadoMercado) -> None:
        self.mercado = estado

    def reservar_pendente(self, intencao: Intencao, lado: Lado, t_ns: int) -> None:
        """Registra uma entrada enviada e ainda nao resolvida (conta no limite de exposicao)."""
        self._pendentes[intencao.id] = _Pendente(intencao.braco, lado, intencao.quantidade)
        self.contab.registrar_abertura(intencao.braco, t_ns)

    def liberar_pendente(self, intencao_id: str) -> None:
        self._pendentes.pop(intencao_id, None)

    def pendentes_braco(self, braco: str) -> int:
        return sum(p.quantidade for p in self._pendentes.values() if p.braco == braco)

    def registrar_trade(self, braco: str, liquido_brl: float, quantidade: int, t_ns: int) -> EstadoBraco:
        e = self.contab.registrar_trade(braco, liquido_brl, quantidade, t_ns)
        self.verificar_automatico(braco, t_ns)
        return e

    def registrar_parcial(self, braco: str, liquido_brl: float, quantidade: int, t_ns: int) -> EstadoBraco:
        """Parcial de posicao que continua aberta: so P&L (dia/semana/drawdown); o episodio fecha em
        `registrar_trade` (perdas seguidas, pausa e martingale contam o episodio, nao cada pedaco)."""
        e = self.contab.registrar_parcial(braco, liquido_brl, quantidade, t_ns)
        self.verificar_automatico(braco, t_ns)
        return e

    def marcar_a_mercado(self, braco: str, pnl_aberto_brl: float, t_ns: int) -> None:
        self.contab.marcar_a_mercado(braco, pnl_aberto_brl, t_ns)
        self.verificar_automatico(braco, t_ns)

    def verificar_automatico(self, braco: str, t_ns: int) -> bool:
        """Gatilhos automaticos do kill switch ligados a contabilidade (perda diaria, drawdown)."""
        e = self.contab.estado(braco, t_ns)
        a = self.kill.checar_perda_diaria(e.pnl_dia_total, self.cfg.perda_max_dia_brl, t_ns)
        b = self.kill.checar_drawdown(e.drawdown, self.cfg.dd_max_brl, t_ns)
        if self.mercado.dado_critico:
            self.kill.dado_critico("estado de mercado sinalizou dado critico", t_ns)
        return a or b or self.kill.ativo

    # ------------------------------------------------------------------ janelas / zeragem ----------
    def limites_janela_s(self, d: dt.date) -> tuple[int, int, int]:
        """(entradas_de, entradas_ate, zeragem) em segundos do dia: o mais restritivo entre config e grade."""
        g = self._grade(d)
        de = max(hhmm_para_s(self.cfg.entradas_de), hhmm_para_s(g.abertura))
        ate = min(hhmm_para_s(self.cfg.entradas_ate), hhmm_para_s(g.entradas_ate))
        zer = min(hhmm_para_s(self.cfg.zeragem), hhmm_para_s(g.zeragem))
        return de, ate, zer

    def dentro_da_janela(self, t_ns: int) -> bool:
        if self.sessao is not None:
            return self.sessao.estado(t_ns, self.cfg.entradas_de, self.cfg.entradas_ate, self.cfg.zeragem).entradas
        d = dia_sp(t_ns)
        if not self._eh_pregao(d):
            return False
        de, ate, _ = self.limites_janela_s(d)
        s = segundos_do_dia(t_ns)
        return de <= s < ate

    def em_rolagem(self, t_ns: int) -> bool:
        if self.mercado.rolagem:
            return True
        v = self.instrumento.vencimento
        return bool(v) and self._dia(t_ns) >= dt.date.fromisoformat(v)  # type: ignore[arg-type]

    def precisa_zerar(self, posicao: Posicao, t_ns: int) -> tuple[bool, str]:
        """Politica de zeragem forcada: o chamador deve emitir saida se (True, motivo)."""
        if posicao.quantidade <= 0:
            return False, ""
        if self.kill.ativo:
            return True, "kill_switch"
        if self.sessao is not None:
            st = self.sessao.estado(t_ns, self.cfg.entradas_de, self.cfg.entradas_ate, self.cfg.zeragem)
            if not st.aberto:
                return True, "fora_do_pregao"
            if st.zerar:
                return True, "zeragem"
            if self.em_rolagem(t_ns):
                return True, "rolagem"
            if self.mercado.limite_oscilacao:
                return True, "limite_oscilacao"
            return False, ""
        d = dia_sp(t_ns)
        if not self._eh_pregao(d):
            return True, "fora_do_pregao"
        if segundos_do_dia(t_ns) >= self.limites_janela_s(d)[2]:
            return True, "zeragem"
        if self.em_rolagem(t_ns):
            return True, "rolagem"
        if self.mercado.limite_oscilacao:
            return True, "limite_oscilacao"
        return False, ""

    def evento_proximo(self, t_ns: int) -> EventoAgenda | None:
        janela = int(self.cfg.bloqueio_evento_min * 60e9)
        for ev in self.eventos:
            if abs(t_ns - ev.t_ns) <= janela:
                return ev
        return None

    # ------------------------------------------------------------------ envelope / entradas --------
    def zeragem_ns(self, t_ns: int) -> int:
        """Instante (ns UTC) da zeragem forcada do dia de `t_ns`: o mais cedo entre a zeragem da config/grade e,
        em sessoes 24h, `zeragem_antes_fechamento_min` antes do proximo fechamento/rollover. Pode estar no passado
        (ja na zeragem)."""
        if self.sessao is not None:
            ses = self.sessao
            loc = ses.local(t_ns)
            g = self._grade_sessao(loc.date())
            zer_s = min(hhmm_para_s(self.cfg.zeragem), hhmm_para_s(g.zeragem))
            cands = [dt.datetime.combine(loc.date(), dt.time(), tzinfo=ses.tz) + dt.timedelta(seconds=zer_s)]
            prox = ses.proximo_fechamento(t_ns)
            if prox is not None:
                cands.append(prox - dt.timedelta(minutes=ses.bolsa.zeragem_antes_fechamento_min))
            return int(min(cands).timestamp()) * NS
        d = dia_sp(t_ns)
        zer_s = self.limites_janela_s(d)[2]
        inst = dt.datetime.combine(d, dt.time(), tzinfo=SP) + dt.timedelta(seconds=zer_s)
        return int(inst.timestamp()) * NS

    def _grade_sessao(self, d: dt.date) -> SessaoPregao:
        ses = self.sessao
        assert ses is not None
        grade: Callable[[dt.date], SessaoPregao] = getattr(ses, "_grade", ses.bolsa.sessao_em)
        return grade(d)

    def stop_min_pontos(self) -> float:
        """Menor stop de entrada aceito: STOP_MIN_TICKS ticks do instrumento."""
        return STOP_MIN_TICKS * self.instrumento.tick_pontos

    def dist_min_ajuste_pontos(self) -> float:
        """Distancia minima stop/alvo ajustado -> preco executavel: DIST_MIN_STOP_TICKS ticks + spread corrente."""
        spread = self.mercado.spread_pts if math.isfinite(self.mercado.spread_pts) else 0.0
        return DIST_MIN_STOP_TICKS * self.instrumento.tick_pontos + max(spread, 0.0)

    def entradas_possiveis(self, braco: str, t_ns: int) -> tuple[str, ...]:
        """Motivos de veto de ENTRADA que nao dependem da intencao (vazio = alguma entrada ainda pode passar).
        Fonte unica: `_motivos_entrada` comeca por esta lista. Nao muda estado (alem de virar o dia da sessao)."""
        cfg, ins = self.cfg, self.instrumento
        m: list[str] = []
        if self.kill.ativo:
            m.append("kill_switch_ativo")
        if not self._aberto(t_ns):
            m.append("fora_do_pregao")
        elif not self.dentro_da_janela(t_ns):
            m.append("fora_da_janela_de_entradas")
        ev = self.evento_proximo(t_ns)
        if ev is not None:
            m.append(f"evento_calendario:{ev.nome}")
        mk = self.mercado
        if not mk.feed_ok:
            m.append("feed_parado")
        if mk.dado_critico:
            m.append("dado_critico")
        if mk.vol_extrema:
            m.append("regime_extremo_volatilidade")
        if mk.leilao:
            m.append("leilao")
        if mk.limite_oscilacao:
            m.append("limite_de_oscilacao")
        if self.em_rolagem(t_ns):
            m.append("rolagem_de_contrato")
        if self.pendentes_braco(braco) > 0:
            m.append("ordem_de_entrada_pendente")
        fator = self.fator_conversao(t_ns)
        if fator is None:
            m.append(motivo_sem_cotacao(ins.moeda, ins.moeda_conta))

        est = self.contab.estado(braco, t_ns)
        semana = est.pnl_semana + est.pnl_aberto
        if est.pnl_dia_total <= -cfg.perda_max_dia_brl:
            m.append("perda_diaria_atingida")
        if semana <= -cfg.perda_max_semana_brl:
            m.append("perda_semanal_atingida")
        if est.drawdown >= cfg.dd_max_brl:
            m.append("drawdown_maximo")
        if est.operacoes_dia >= cfg.operacoes_dia_max:
            m.append("operacoes_dia_maximo")
        if est.perdas_seguidas >= cfg.perdas_seguidas_max:
            m.append("perdas_seguidas_maximo")
        if est.ultima_perda_ns is not None and t_ns - est.ultima_perda_ns < cfg.pausa_apos_perda_s * 1e9:
            m.append("pausa_apos_perda")
        if fator is not None:
            pior_min = perda_pior_caso_brl(cfg, self.stop_min_pontos(), 1, ins.ponto_valor_brl * fator)
            if (pior_min > cfg.perda_max_operacao_brl + _EPS
                    or est.pnl_dia_total - pior_min < -cfg.perda_max_dia_brl - _EPS
                    or semana - pior_min < -cfg.perda_max_semana_brl - _EPS):
                m.append("orcamento_de_perda_insuficiente")
        return tuple(dict.fromkeys(m))

    def envelope(self, braco: str, t_ns: int, *, posicao: Posicao | None = None, margem_s: int = 0
                 ) -> EnvelopeDecisao:
        """Envelope DERIVADO dos limites (pontos/unidades/segundos), so leitura, com as MESMAS contas do veto: um
        stop igual a `stop_max_pontos_por_q[q-1]` passa e um tick a mais e vetado. `tempo_max_s` e o tempo TOTAL
        desde a abertura (da posicao aberta em `posicao`, ou de uma nova agora): min(teto do usuario, ate a
        zeragem - `margem_s`). Nenhum valor em dinheiro sai daqui. Falha interna => envelope fechado."""
        try:
            return self._envelope(braco, t_ns, posicao, margem_s)
        except Exception as e:  # noqa: BLE001 - falha segura
            return EnvelopeDecisao(unidades_max=0, stop_max_pontos_por_q=(), stop_min_pontos=self.stop_min_pontos(),
                                   dist_min_ajuste_pontos=self.dist_min_ajuste_pontos(), tempo_max_s=0,
                                   entradas_possiveis=False, motivos_sem_entrada=(f"erro_interno:{type(e).__name__}",))

    def _envelope(self, braco: str, t_ns: int, posicao: Posicao | None, margem_s: int) -> EnvelopeDecisao:
        cfg, ins = self.cfg, self.instrumento
        motivos = list(self.entradas_possiveis(braco, t_ns))
        aberta = posicao is not None and posicao.quantidade > 0
        if aberta:
            motivos.append("posicao_aberta")
        est = self.contab.estado(braco, t_ns)
        fator = self.fator_conversao(t_ns)
        pv_conta = ins.ponto_valor_brl * fator if fator is not None else 0.0
        stops = stop_max_por_quantidade(
            cfg, pv_conta, self.unidades_max, cfg.perda_max_dia_brl + est.pnl_dia_total,
            cfg.perda_max_semana_brl + est.pnl_semana + est.pnl_aberto, tick=ins.tick_pontos)
        base_ns = posicao.aberta_ns if posicao is not None and aberta and posicao.aberta_ns > 0 else t_ns
        ate_zeragem = (self.zeragem_ns(t_ns) - base_ns) / NS - max(margem_s, 0)
        tempo = max(0, min(cfg.tempo_max_posicao_s, math.floor(ate_zeragem)))
        return EnvelopeDecisao(unidades_max=self.unidades_max, stop_max_pontos_por_q=stops,
                               stop_min_pontos=self.stop_min_pontos(),
                               dist_min_ajuste_pontos=self.dist_min_ajuste_pontos(), tempo_max_s=int(tempo),
                               entradas_possiveis=not motivos, motivos_sem_entrada=tuple(dict.fromkeys(motivos)))

    # ------------------------------------------------------------------ stop -----------------------
    def validar_stop(self, posicao: Posicao, novo_stop: float | None) -> bool:
        """Stop so aperta (e nunca e removido)."""
        if posicao.lado is None:
            return False
        return stop_so_aperta(posicao.lado, posicao.stop, novo_stop)

    # ------------------------------------------------------------------ avaliacao ------------------
    def avaliar(self, intencao: Intencao, posicao: Posicao, t_ns: int) -> DecisaoRisco:
        try:
            motivos = self._motivos(intencao, posicao, t_ns)
        except Exception as e:  # falha segura
            motivos = [f"erro_interno:{type(e).__name__}"]
        dec = DecisaoRisco(intencao_id=intencao.id, aprovada=not motivos, motivos=tuple(motivos))
        if self.auditoria is not None and (motivos or intencao.acao in (Acao.COMPRAR, Acao.VENDER)):
            self.auditoria.registrar("risco_decisao", {
                "intencao_id": intencao.id, "braco": intencao.braco,
                "acao": str(getattr(intencao.acao, "value", intencao.acao)),
                "aprovada": dec.aprovada, "motivos": list(dec.motivos), "t_ns": t_ns})
        return dec

    def _motivos(self, it: Intencao, pos: Posicao, t_ns: int) -> list[str]:
        a = it.acao
        if a == Acao.NAO_OPERAR:
            return []
        if a == Acao.ENCERRAR:  # zerar nunca e vetado; so fica preso a posicao vista em t0 quando ela foi informada
            return ["posicao_diferente_da_vista"] if it.posicao_ticket not in (None, pos.ticket) else []
        if a == Acao.MANTER:
            return self._motivos_ajuste(it, pos, t_ns)
        if a in (Acao.COMPRAR, Acao.VENDER):
            return self._motivos_entrada(it, pos, t_ns)
        return ["acao_desconhecida"]  # fail-closed: acao nao tratada explicitamente nunca vira entrada

    # ------------------------------------------------------------------ gestao (rechecagem) --------
    def _motivos_gestao(self, regras: Iterable[RegraCompilada | RegraGestao], q: int) -> list[str]:
        """Rechecagem ESTRUTURAL, independente do validador da IA: tipo no conjunto fechado; parcial so com q >= 2
        e 1 <= quantidade < q; distancias >= distancia minima de ajuste; numeros finitos e nao negativos."""
        m: list[str] = []
        dist = self.dist_min_ajuste_pontos()
        for r in regras:
            tipo = str(r.tipo)
            erro = tipo not in TIPOS_GESTAO
            for campo in _CAMPOS_NUMERICOS_GESTAO:
                v = getattr(r, campo, None)
                if v is not None and not (math.isfinite(v) and v >= 0):
                    erro = True
            if tipo == "parcial" and not (q >= 2 and r.quantidade is not None and 1 <= r.quantidade < q):
                erro = True
            if tipo != "parcial" and r.quantidade is not None:
                erro = True  # so a parcial mexe em quantidade
            d = r.distancia_pontos
            if d is not None and math.isfinite(d) and d < dist - _EPS:
                erro = True
            if tipo in ("trailing", "apertar_apos") and d is None:
                erro = True
            if erro:
                m.append(f"gestao_invalida:{tipo}")
        return m

    # ------------------------------------------------------------------ MANTER (ajuste) ------------
    def _motivos_ajuste(self, it: Intencao, pos: Posicao, t_ns: int) -> list[str]:
        """MANTER: sem `ajuste`, caminho legado (stop_pontos medido do preco de referencia, so aperta); com `ajuste`,
        niveis em PONTOS DE RESULTADO desde o preco medio. Reduzir e apertar nunca sao vetados por janela, kill ou
        limites; com kill ativo, so eles passam. Nada aumenta a posicao."""
        m: list[str] = []
        sem_pos = pos.quantidade <= 0 or pos.lado is None
        if it.posicao_ticket is not None and it.posicao_ticket != pos.ticket:
            m.append("posicao_diferente_da_vista")
        if it.quantidade_t0 is not None and it.quantidade_t0 != pos.quantidade:
            m.append("posicao_diferente_da_vista")
        aj = it.ajuste
        if aj is None:
            if m:
                return list(dict.fromkeys(m))
            return self._motivos_manter_legado(it, pos)
        if sem_pos:
            m.append("posicao_diferente_da_vista")
        if m:
            return list(dict.fromkeys(m))
        assert pos.lado is not None
        lado, tick = pos.lado, self.instrumento.tick_pontos
        sinal = 1.0 if lado == Lado.COMPRA else -1.0
        dist = self.dist_min_ajuste_pontos()
        kill = self.kill.ativo
        ref = it.preco_referencia
        ref_ok = ref > 0 and math.isfinite(ref)
        pm = pos.preco_medio

        if aj.stop_resultado_pontos is not None:
            r = aj.stop_resultado_pontos
            if not math.isfinite(r) or not (pm > 0 and math.isfinite(pm)):
                m.append("stop_invalido")
            elif not ref_ok:
                m.append("preco_referencia_invalido")
            else:
                # arredondado em direcao ao preco (aperta): para cima na compra, para baixo na venda
                nivel = _na_grade(pm + sinal * r, tick, "cima" if lado == Lado.COMPRA else "baixo")
                if not stop_so_aperta(lado, pos.stop, nivel):
                    m.append("stop_so_pode_apertar")
                if not inv_distancia_minima(lado, nivel, ref, dist):
                    m.append("stop_colado_no_preco")
        if aj.alvo_resultado_pontos is not None:
            r = aj.alvo_resultado_pontos
            if kill:
                m.append("kill_ativo_so_aperto")
            if not math.isfinite(r) or r <= 0 or not (pm > 0 and math.isfinite(pm)):
                m.append("alvo_invalido")
            elif not ref_ok:
                m.append("preco_referencia_invalido")
            else:
                nivel = _na_grade(pm + sinal * r, tick, "perto")
                folga = (nivel - ref) * sinal
                if folga <= 0:
                    m.append("alvo_invalido")
                elif folga < dist - _EPS:
                    m.append("alvo_colado_ao_preco")
        if aj.tempo_max_total_s is not None:
            tt = aj.tempo_max_total_s
            if kill:
                m.append("kill_ativo_so_aperto")
            if pos.aberta_ns <= 0:
                m.append("abertura_desconhecida")
            elif tt <= 0 or tt > self.cfg.tempo_max_posicao_s or pos.aberta_ns + tt * NS > self.zeragem_ns(t_ns):
                m.append("tempo_max_acima_do_limite")
            elif (t_ns - pos.aberta_ns) >= tt * NS:
                m.append("tempo_ja_vencido")
        if aj.reduzir_quantidade is not None:
            rq = aj.reduzir_quantidade
            if not 1 <= rq < pos.quantidade:
                m.append("reducao_invalida")
        if aj.substituir_gestao is not None:
            if kill:
                m.append("kill_ativo_so_aperto")
            m.extend(self._motivos_gestao(aj.substituir_gestao, pos.quantidade))
        if it.gestao is not None:
            if kill:
                m.append("kill_ativo_so_aperto")
            m.extend(self._motivos_gestao(it.gestao.regras, pos.quantidade))
        return list(dict.fromkeys(m))

    def _motivos_manter_legado(self, it: Intencao, pos: Posicao) -> list[str]:
        """MANTER com stop_pontos em posicao aberta = pedido de revisao do stop: so aperta e nunca colado no preco
        (>= DIST_MIN_STOP_TICKS ticks + spread do preco de referencia)."""
        if it.stop_pontos is None or pos.quantidade <= 0 or pos.lado is None:
            return []
        if not math.isfinite(it.stop_pontos) or it.stop_pontos <= 0 or it.preco_referencia <= 0:
            return ["stop_invalido"]
        novo = it.preco_referencia - it.stop_pontos if pos.lado == Lado.COMPRA else it.preco_referencia + it.stop_pontos
        m = [] if self.validar_stop(pos, novo) else ["stop_so_pode_apertar"]
        if it.stop_pontos < self.dist_min_ajuste_pontos() - _EPS:
            m.append("stop_colado_no_preco")
        return m

    # ------------------------------------------------------------------ entrada --------------------
    def _motivos_entrada(self, it: Intencao, pos: Posicao, t_ns: int) -> list[str]:
        cfg, ins = self.cfg, self.instrumento
        lado = Lado.COMPRA if it.acao == Acao.COMPRAR else Lado.VENDA  # so COMPRAR/VENDER chegam aqui
        est = self.contab.estado(it.braco, t_ns)
        # motivos que nao dependem da intencao (kill, regime, janela, cotacao, orcamento, cadencia): fonte unica
        m: list[str] = list(self.entradas_possiveis(it.braco, t_ns))
        if t_ns > it.validade_ns:
            m.append("intencao_expirada")

        # tamanho e exposicao
        q = it.quantidade
        umax = self.unidades_max  # contratos_max ou lotes minimos que cabem em volume_max_lotes
        if q < 1:
            m.append("quantidade_invalida")
        if q > umax:
            m.append("contratos_acima_do_maximo")
        if pos.quantidade > 0:
            if pos.lado == lado:
                m.append("sem_aumento_de_posicao")  # cobre preco medio e piramidacao
            else:
                m.append("inversao_proibida_use_encerrar")
        pend = self.pendentes_braco(it.braco)
        if pos.quantidade + pend + max(q, 0) > umax:
            m.append("exposicao_acima_do_maximo")
        if est.ultima_foi_perda and q > est.ultimo_tamanho > 0:
            m.append("martingale_proibido")

        # moeda: limites estao na MOEDA DA CONTA; P&L do simbolo convertido pela cotacao do instante
        fator = self.fator_conversao(t_ns)
        if cfg.nocional_max_conta > 0 and fator is not None and it.preco_referencia > 0:
            nocional = it.preco_referencia * ins.ponto_valor_brl * max(q, 1) * fator
            if nocional > cfg.nocional_max_conta + 1e-9:
                m.append("nocional_acima_do_maximo")

        # stop e perda por operacao
        sp = it.stop_pontos
        if sp is None or not math.isfinite(sp) or sp <= 0:
            m.append("stop_obrigatorio")
            pior = cfg.perda_max_operacao_brl  # sem stop: pior caso desconhecido, trata como limite
        else:
            if sp > cfg.stop_max_pontos:
                m.append("stop_acima_do_maximo")
            if sp < self.stop_min_pontos() - _EPS:
                m.append("stop_abaixo_do_minimo")
            if fator is None:
                pior = cfg.perda_max_operacao_brl  # sem cotacao: pior caso desconhecido (ja vetado acima)
            else:
                pior = perda_pior_caso_brl(cfg, sp, max(q, 1), ins.ponto_valor_brl * fator)
                if pior > cfg.perda_max_operacao_brl + 1e-9:
                    m.append("perda_por_operacao_acima_do_maximo")
        if it.alvo_pontos is not None:
            if not math.isfinite(it.alvo_pontos) or it.alvo_pontos <= 0:
                m.append("alvo_invalido")
            elif it.alvo_pontos < ins.tick_pontos - _EPS:
                m.append("alvo_abaixo_de_um_tick")
        if not (it.preco_referencia > 0 and math.isfinite(it.preco_referencia)):
            m.append("preco_referencia_invalido")

        # tempo maximo da posicao: dentro do teto do usuario e antes da zeragem
        if it.tempo_max_s is not None:
            tm = it.tempo_max_s
            if tm <= 0 or tm > cfg.tempo_max_posicao_s or t_ns + tm * NS > self.zeragem_ns(t_ns):
                m.append("tempo_max_acima_do_limite")

        # gestao automatica declarada (ja compilada): rechecagem estrutural
        if it.gestao is not None:
            m.extend(self._motivos_gestao(it.gestao.regras, max(q, 0)))

        # perdas acumuladas (orcamento restante precisa comportar o pior caso desta operacao)
        if "perda_diaria_atingida" not in m and est.pnl_dia_total - pior < -cfg.perda_max_dia_brl - _EPS:
            m.append("perda_diaria_excederia_limite")
        semana = est.pnl_semana + est.pnl_aberto
        if "perda_semanal_atingida" not in m and semana - pior < -cfg.perda_max_semana_brl - _EPS:
            m.append("perda_semanal_excederia_limite")
        return list(dict.fromkeys(m))
