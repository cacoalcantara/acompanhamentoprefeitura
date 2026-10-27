"""Motor de risco: veto final, deterministico e independente de IA.

`avaliar` nunca levanta excecao: qualquer erro interno vira veto (falha segura). Saidas (ENCERRAR) e
apertos de stop nunca sao bloqueados; entradas passam por todas as regras e todos os motivos de veto
sao coletados (auditoria completa), nao so o primeiro.
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..core import calendario_b3
from ..core.protocolos import RegistroAuditoria
from ..core.tipos import (
    Acao,
    Base,
    ConfigRisco,
    DecisaoRisco,
    Instrumento,
    Intencao,
    Lado,
    Posicao,
    SessaoPregao,
)
from .invariantes import stop_so_aperta
from .kill_switch import KillSwitch
from .politica import perda_pior_caso_brl, validar_coerencia
from .sessao import ContabilidadeSessao, EstadoBraco
from .tempo import dia_sp, hhmm_para_s, segundos_do_dia


class EstadoMercado(Base):
    """Flags de regime alimentadas por dados/features (nao pela IA)."""
    feed_ok: bool = True
    dado_critico: bool = False
    vol_extrema: bool = False
    leilao: bool = False
    limite_oscilacao: bool = False
    rolagem: bool = False


class EventoAgenda(Base):
    t_ns: int
    nome: str = "evento"


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
    ) -> None:
        self.cfg = validar_coerencia(cfg)
        self.instrumento = instrumento
        self.kill = kill_switch or KillSwitch()
        self.contab = contabilidade or ContabilidadeSessao()
        self.auditoria = auditoria
        self.eventos: list[EventoAgenda] = list(eventos)
        self.mercado = EstadoMercado()
        self._grade = grade
        self._eh_pregao = eh_pregao
        self._pendentes: dict[str, _Pendente] = {}

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
        return bool(v) and dia_sp(t_ns) >= dt.date.fromisoformat(v)  # type: ignore[arg-type]

    def precisa_zerar(self, posicao: Posicao, t_ns: int) -> tuple[bool, str]:
        """Politica de zeragem forcada: o chamador deve emitir saida se (True, motivo)."""
        if posicao.quantidade <= 0:
            return False, ""
        if self.kill.ativo:
            return True, "kill_switch"
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
                "intencao_id": intencao.id, "braco": intencao.braco, "acao": intencao.acao.value,
                "aprovada": dec.aprovada, "motivos": list(dec.motivos), "t_ns": t_ns})
        return dec

    def _motivos(self, it: Intencao, pos: Posicao, t_ns: int) -> list[str]:
        if it.acao in (Acao.NAO_OPERAR, Acao.ENCERRAR):
            return []  # reduzir/zerar exposicao nunca e vetado
        if it.acao == Acao.MANTER:
            return self._motivos_manter(it, pos)
        return self._motivos_entrada(it, pos, t_ns)

    def _motivos_manter(self, it: Intencao, pos: Posicao) -> list[str]:
        """MANTER com stop_pontos em posicao aberta = pedido de revisao do stop: so aperta."""
        if it.stop_pontos is None or pos.quantidade <= 0 or pos.lado is None:
            return []
        if not math.isfinite(it.stop_pontos) or it.stop_pontos <= 0 or it.preco_referencia <= 0:
            return ["stop_invalido"]
        novo = it.preco_referencia - it.stop_pontos if pos.lado == Lado.COMPRA else it.preco_referencia + it.stop_pontos
        return [] if self.validar_stop(pos, novo) else ["stop_so_pode_apertar"]

    def _motivos_entrada(self, it: Intencao, pos: Posicao, t_ns: int) -> list[str]:
        cfg, ins = self.cfg, self.instrumento
        m: list[str] = []
        lado = Lado.COMPRA if it.acao == Acao.COMPRAR else Lado.VENDA
        est = self.contab.estado(it.braco, t_ns)

        # kill switch / regime / janela
        if self.kill.ativo:
            m.append("kill_switch_ativo")
        if not self._eh_pregao(dia_sp(t_ns)):
            m.append("fora_do_pregao")
        elif not self.dentro_da_janela(t_ns):
            m.append("fora_da_janela_de_entradas")
        if t_ns > it.validade_ns:
            m.append("intencao_expirada")
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

        # tamanho e exposicao
        q = it.quantidade
        if q < 1:
            m.append("quantidade_invalida")
        if q > cfg.contratos_max:
            m.append("contratos_acima_do_maximo")
        if pos.quantidade > 0:
            if pos.lado == lado:
                m.append("sem_aumento_de_posicao")  # cobre preco medio e piramidacao
            else:
                m.append("inversao_proibida_use_encerrar")
        pend = self.pendentes_braco(it.braco)
        if pend > 0:
            m.append("ordem_de_entrada_pendente")
        if pos.quantidade + pend + max(q, 0) > cfg.contratos_max:
            m.append("exposicao_acima_do_maximo")
        if est.ultima_foi_perda and q > est.ultimo_tamanho > 0:
            m.append("martingale_proibido")

        # stop e perda por operacao
        sp = it.stop_pontos
        if sp is None or not math.isfinite(sp) or sp <= 0:
            m.append("stop_obrigatorio")
            pior = cfg.perda_max_operacao_brl  # sem stop: pior caso desconhecido, trata como limite
        else:
            if sp > cfg.stop_max_pontos:
                m.append("stop_acima_do_maximo")
            pior = perda_pior_caso_brl(cfg, sp, max(q, 1), ins.ponto_valor_brl)
            if pior > cfg.perda_max_operacao_brl + 1e-9:
                m.append("perda_por_operacao_acima_do_maximo")
        if it.alvo_pontos is not None and (not math.isfinite(it.alvo_pontos) or it.alvo_pontos <= 0):
            m.append("alvo_invalido")
        if not (it.preco_referencia > 0 and math.isfinite(it.preco_referencia)):
            m.append("preco_referencia_invalido")

        # perdas acumuladas (orcamento restante precisa comportar o pior caso desta operacao)
        if est.pnl_dia_total <= -cfg.perda_max_dia_brl:
            m.append("perda_diaria_atingida")
        elif est.pnl_dia_total - pior < -cfg.perda_max_dia_brl:
            m.append("perda_diaria_excederia_limite")
        if est.pnl_semana + est.pnl_aberto <= -cfg.perda_max_semana_brl:
            m.append("perda_semanal_atingida")
        elif est.pnl_semana + est.pnl_aberto - pior < -cfg.perda_max_semana_brl:
            m.append("perda_semanal_excederia_limite")
        if est.drawdown >= cfg.dd_max_brl:
            m.append("drawdown_maximo")

        # cadencia
        if est.operacoes_dia >= cfg.operacoes_dia_max:
            m.append("operacoes_dia_maximo")
        if est.perdas_seguidas >= cfg.perdas_seguidas_max:
            m.append("perdas_seguidas_maximo")
        if est.ultima_perda_ns is not None and t_ns - est.ultima_perda_ns < cfg.pausa_apos_perda_s * 1e9:
            m.append("pausa_apos_perda")
        return list(dict.fromkeys(m))
