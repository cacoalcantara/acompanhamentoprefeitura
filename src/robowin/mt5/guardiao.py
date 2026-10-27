"""Guardiao: limites rigidos INDEPENDENTES de IA e de rede, vivendo no gateway.

Garante, mesmo sem o nucleo:
- 1 contrato, janela de horario da sessao, perda diaria, orcamento de mensagens (este no executor);
- sem heartbeat do nucleo por 15 s => bloqueia ordens NOVAS; apos 60 s => zera a posicao demo pelo ticket;
- zera no horario configurado; le o arquivo-flag EMERGENCIA (cancela, zera e TRAVA; rearme so manual);
- reconcilia a cada 10 s (positions_get / orders_get / history_deals_get) classificando deals por `reason`:
  SL/TP/SO = esperado; EXPERT = robo; CLIENT/MOBILE/WEB = manual => alerta + bloqueio;
- todas as saidas passam por `ExecucaoDemo` (unico dono de saida por posicao; o guardiao e o dono forcado).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robowin.core import calendario_b3
from robowin.core.relogio import Relogio
from robowin.core.tipos import ComandoOrdem, ConfigRisco

from . import constantes as C
from .execucao_demo import ExecucaoDemo, GuardaDemoViolada, violacao_de_identidade
from .tempo_servidor import RelogioServidor

Alerta = Callable[[str, dict[str, Any]], None]


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


@dataclass
class ConfigGuardiao:
    contratos_max: int = 1
    entradas_de: str = "09:15"
    entradas_ate: str = "17:30"
    zeragem: str = "17:45"
    perda_max_dia_brl: float = 200.0
    heartbeat_bloqueio_s: float = 15.0
    heartbeat_zera_s: float = 60.0
    reconciliar_a_cada_s: float = 10.0
    arquivo_emergencia: Path | None = None
    magics_permitidos: frozenset[int] = field(default_factory=frozenset)
    exigir_dia_util: bool = True
    exigir_sl: bool = True  # entrada sem stop e recusada
    stop_max_pontos: float = 300.0  # distancia maxima preco->SL (pontos do WIN = unidades de preco)

    @classmethod
    def de_risco(cls, r: ConfigRisco, **kw: Any) -> ConfigGuardiao:
        return cls(contratos_max=r.contratos_max, entradas_de=r.entradas_de, entradas_ate=r.entradas_ate,
                   zeragem=r.zeragem, perda_max_dia_brl=r.perda_max_dia_brl,
                   stop_max_pontos=r.stop_max_pontos, **kw)


@dataclass
class _Bloqueio:
    motivo: str
    dia: str
    persistente: bool  # False => expira na virada do dia (ex.: perda diaria)


class Guardiao:
    def __init__(
        self, executor: ExecucaoDemo, terminal: Any, cfg: ConfigGuardiao, relogio: Relogio,
        relogio_srv: RelogioServidor | None = None, alerta: Alerta | None = None,
        registrar: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.ex, self.t, self.cfg, self.relogio = executor, terminal, cfg, relogio
        self.rs = relogio_srv or executor.rs
        self._alerta_cb = alerta or (lambda tipo, dados: None)
        self._reg = registrar or (lambda ev, dados: None)
        self._t_hb_ns = relogio.agora_ns()
        self._bloqueios: dict[str, _Bloqueio] = {}
        self._deals_vistos: set[int] = set()
        self._ult_reconc_ns = 0
        self._falhas_reconc = 0
        self.alertas: list[tuple[str, dict[str, Any]]] = []
        self.classificacao: dict[str, int] = {"esperado": 0, "robo": 0, "manual": 0, "outro": 0}
        self.pnl_dia_brl = 0.0

    # --- tempo ---------------------------------------------------------------------------------
    def _agora_sp(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.relogio.agora_ns() / 1e9, calendario_b3.SP)

    def _dia(self) -> str:
        return self._agora_sp().date().isoformat()

    def _min_do_dia(self) -> int:
        a = self._agora_sp()
        return a.hour * 60 + a.minute

    def eh_dia_util(self) -> bool:
        return (not self.cfg.exigir_dia_util) or calendario_b3.eh_pregao(self._agora_sp().date())

    def dentro_da_janela_entrada(self) -> bool:
        return self.eh_dia_util() and _hhmm(self.cfg.entradas_de) <= self._min_do_dia() < _hhmm(self.cfg.entradas_ate)

    def hora_de_zerar(self) -> bool:
        return self.eh_dia_util() and self._min_do_dia() >= _hhmm(self.cfg.zeragem)

    # --- heartbeat do nucleo ---------------------------------------------------------------------
    def heartbeat(self) -> None:
        self._t_hb_ns = self.relogio.agora_ns()

    def idade_heartbeat_s(self) -> float:
        return (self.relogio.agora_ns() - self._t_hb_ns) / 1e9

    # --- bloqueios ---------------------------------------------------------------------------------
    def _alerta(self, tipo: str, dados: dict[str, Any]) -> None:
        self.alertas.append((tipo, dados))
        self._reg(f"alerta:{tipo}", dados)
        self._alerta_cb(tipo, dados)

    def _bloquear(self, chave: str, motivo: str, persistente: bool = True) -> None:
        if chave not in self._bloqueios:
            self._bloqueios[chave] = _Bloqueio(motivo, self._dia(), persistente)
            self._alerta(f"bloqueio_{chave}", {"motivo": motivo})

    def bloquear_ate_resolver(self, chave: str, motivo: str) -> None:
        """Bloqueio de entradas que o proprio chamador levanta com `desbloquear(chave)` (ex.: offset do servidor)."""
        self._bloquear(chave, motivo, persistente=True)

    def desbloquear(self, chave: str) -> None:
        self._bloqueios.pop(chave, None)

    def atualizar_relogio_servidor(self, rs: RelogioServidor) -> None:
        self.rs = rs
        self.ex.rs = rs

    def _expirar(self) -> None:
        hoje = self._dia()
        for k in [k for k, b in self._bloqueios.items() if not b.persistente and b.dia != hoje]:
            del self._bloqueios[k]

    @property
    def travado_emergencia(self) -> bool:
        return "emergencia" in self._bloqueios

    def motivos_bloqueio(self) -> list[str]:
        self._expirar()
        out = [f"{k}: {b.motivo}" for k, b in self._bloqueios.items()]
        if self.idade_heartbeat_s() > self.cfg.heartbeat_bloqueio_s:
            out.append(f"heartbeat: sem sinal do nucleo ha {self.idade_heartbeat_s():.0f} s")
        if not self.dentro_da_janela_entrada():
            out.append("horario: fora da janela de entradas")
        if self.hora_de_zerar():
            out.append("horario: apos o horario de zeragem")
        if self.ex.travado:
            out.append(f"executor: {self.ex.travado}")
        if self.ex.bloqueada_incerta:
            out.append("executor: ordem INCERTA pendente")
        return out

    def avaliar_entrada(self, cmd: ComandoOrdem) -> tuple[bool, str]:
        """Veto rigido para ordens NOVAS (saidas sempre passam)."""
        if cmd.tipo == "saida":
            return True, ""
        if cmd.quantidade > self.cfg.contratos_max:
            return False, f"quantidade {cmd.quantidade} acima de {self.cfg.contratos_max}"
        motivos = self.motivos_bloqueio()
        if motivos:
            return False, "; ".join(motivos)
        if self.cfg.exigir_sl:
            return self._validar_stop(cmd)
        return True, ""

    def _validar_stop(self, cmd: ComandoOrdem) -> tuple[bool, str]:
        """Entrada exige SL do lado certo do preco e a no maximo `stop_max_pontos` de distancia."""
        if not cmd.sl:
            return False, "entrada sem stop (sl obrigatorio)"
        compra = cmd.lado.value == "compra"
        ref = cmd.preco if (cmd.tipo == "limitada" and cmd.preco) else None
        if ref is None:
            tk = self.t.symbol_info_tick(self.ex._sym)
            if tk is None:
                return False, "sem cotacao para validar o stop"
            ref = float(tk.ask if compra else tk.bid)
        if (compra and cmd.sl >= ref) or (not compra and cmd.sl <= ref):
            return False, f"sl {cmd.sl} do lado errado do preco {ref}"
        if abs(ref - cmd.sl) > self.cfg.stop_max_pontos + 1e-9:
            return False, f"distancia do stop {abs(ref - cmd.sl):.0f} acima do maximo {self.cfg.stop_max_pontos:.0f}"
        return True, ""

    def rearmar(self, motivo: str) -> None:
        """Rearme MANUAL (com motivo na auditoria). Recusado enquanto o arquivo EMERGENCIA existir."""
        if not motivo.strip():
            raise ValueError("rearmar exige motivo")
        if self.cfg.arquivo_emergencia is not None and self.cfg.arquivo_emergencia.exists():
            raise RuntimeError("arquivo EMERGENCIA ainda existe: remova-o antes de rearmar")
        self._reg("rearme", {"motivo": motivo, "bloqueios": list(self._bloqueios)})
        self._bloqueios.clear()
        self.ex.destravar(motivo)

    # --- zeragem -------------------------------------------------------------------------------------
    def _estado_plano(self) -> tuple[tuple[Any, ...] | None, tuple[Any, ...] | None]:
        return self.t.positions_get(), self.t.orders_get()

    def esta_zerado(self) -> bool:
        ps, os_ = self._estado_plano()
        return ps is not None and os_ is not None and not ps and not os_

    def zerar_tudo(self, motivo: str) -> bool:
        """Cancela pendentes e zera posicoes pelo ticket (via executor). True se terminou zerado."""
        ps, os_ = self._estado_plano()
        try:
            for o in os_ or ():
                self.ex.cancelar_ordem(int(o.ticket), dono="guardiao", prioritaria=True, simbolo=str(o.symbol))
            for p in ps or ():
                self.ex.zerar_posicao(int(p.ticket), dono="guardiao", motivo=motivo, simbolo=str(p.symbol))
        except GuardaDemoViolada as e:
            if violacao_de_identidade(e):
                raise  # troca de conta/conta real: o gateway para
            self._alerta("zeragem_impossivel", {"motivo": str(e)})  # ex.: algotrading desligado; tenta no proximo ciclo
            return False
        return self.esta_zerado()

    def acionar_emergencia(self, origem: str) -> bool:
        """Kill switch: trava (rearme so manual), cancela e zera. Retorna True se confirmado zerado."""
        self._bloquear("emergencia", origem)
        return self.zerar_tudo(f"emergencia: {origem}")

    def confirmacao_emergencia(self) -> dict[str, Any]:
        ps, os_ = self._estado_plano()
        return {"travado": self.travado_emergencia, "posicoes": None if ps is None else len(ps),
                "ordens": None if os_ is None else len(os_),
                "zerado": ps is not None and os_ is not None and not ps and not os_}

    # --- reconciliacao -------------------------------------------------------------------------------
    def _inicio_do_dia_ns(self) -> int:
        a = self._agora_sp().replace(hour=0, minute=0, second=0, microsecond=0)
        return int(a.timestamp() * 1e9)

    def reconciliar(self) -> dict[str, Any]:
        self._ult_reconc_ns = self.relogio.agora_ns()
        ps, os_ = self._estado_plano()
        agora = self.relogio.agora_ns()
        deals = self.t.history_deals_get(self.rs.utc_ns_para_dt_servidor(self._inicio_do_dia_ns()),
                                         self.rs.utc_ns_para_dt_servidor(agora + 86_400_000_000_000))
        if ps is None or os_ is None or deals is None:
            self._falhas_reconc += 1
            if self._falhas_reconc >= 3:
                self._bloquear("reconciliacao", "terminal sem resposta a positions/orders/deals", persistente=False)
            return {"ok": False}
        self._falhas_reconc = 0
        novos: list[dict[str, Any]] = []
        for d in deals:
            if int(d.type) not in (C.DEAL_COMPRA, C.DEAL_VENDA) or int(d.ticket) in self._deals_vistos:
                continue
            self._deals_vistos.add(int(d.ticket))
            razao = int(d.reason)
            nome = C.NOMES_RAZAO.get(razao, str(razao))
            info = {"deal": int(d.ticket), "reason": nome, "magic": int(d.magic), "symbol": str(d.symbol),
                    "volume": float(d.volume), "price": float(d.price)}
            if razao in C.RAZOES_ESPERADAS:
                cls = "esperado"
            elif razao == C.RAZAO_EXPERT:
                cls = "robo"
                if self.cfg.magics_permitidos and int(d.magic) not in self.cfg.magics_permitidos:
                    cls = "outro"
                    self._bloquear("divergencia", f"deal EXPERT com magic desconhecido {int(d.magic)}")
            elif razao in C.RAZOES_MANUAIS:
                cls = "manual"
                self._alerta("intervencao_manual", info)
                self._bloquear("manual", f"deal {int(d.ticket)} com reason {nome}: intervencao manual")
            else:
                cls = "outro"
                self._alerta("deal_reason_inesperado", info)
                self._bloquear("divergencia", f"deal {int(d.ticket)} com reason {nome}")
            self.classificacao[cls] += 1
            novos.append({**info, "classe": cls})
        # perda diaria: realizado do dia + flutuante
        ini_ms = self.rs.utc_ns_para_servidor_ms(self._inicio_do_dia_ns())
        realizado = sum(float(d.profit) + float(d.commission) + float(d.swap) + float(d.fee)
                        for d in deals if int(d.time_msc) >= ini_ms)
        flutuante = sum(float(p.profit) for p in ps)
        self.pnl_dia_brl = realizado + flutuante
        if self.pnl_dia_brl <= -abs(self.cfg.perda_max_dia_brl):
            self._bloquear("perda_dia", f"perda do dia R$ {self.pnl_dia_brl:.2f} no limite", persistente=False)
        return {"ok": True, "novos": novos, "pnl_dia_brl": self.pnl_dia_brl, "posicoes": len(ps), "ordens": len(os_)}

    # --- ciclo (chamado ~1x/s pelo gateway) ----------------------------------------------------------
    def ciclo(self) -> dict[str, Any]:
        acoes: list[str] = []
        self.ex.verificar_identidade()  # troca de conta/conta real => levanta; o gateway para
        self._expirar()
        arq = self.cfg.arquivo_emergencia
        if arq is not None and arq.exists() and not self.travado_emergencia:
            self.acionar_emergencia(f"arquivo {arq.name}")
            acoes.append("emergencia_arquivo")
        self.ex.reconciliar_incertas()
        reconc = None
        if (self.relogio.agora_ns() - self._ult_reconc_ns) / 1e9 >= self.cfg.reconciliar_a_cada_s:
            reconc = self.reconciliar()
        precisa_zerar = ""
        if self.travado_emergencia:
            precisa_zerar = "emergencia"
        elif self.idade_heartbeat_s() > self.cfg.heartbeat_zera_s:
            precisa_zerar = "heartbeat do nucleo ausente"
        elif self.hora_de_zerar():
            precisa_zerar = "horario de zeragem"
        elif "perda_dia" in self._bloqueios:
            precisa_zerar = "perda diaria"
        if precisa_zerar:
            ps, os_ = self._estado_plano()
            if ps or os_:
                self.zerar_tudo(precisa_zerar)
                acoes.append(f"zerar:{precisa_zerar}")
        return {"acoes": acoes, "reconciliacao": reconc}

    def estado(self) -> dict[str, Any]:
        return {"bloqueios": self.motivos_bloqueio(), "travado_emergencia": self.travado_emergencia,
                "heartbeat_idade_s": round(self.idade_heartbeat_s(), 3), "pnl_dia_brl": self.pnl_dia_brl,
                "classificacao_deals": dict(self.classificacao), "mensagens_ultimo_min": self.ex.orcamento.usadas()}
