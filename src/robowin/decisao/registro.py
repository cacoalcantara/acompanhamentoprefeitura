"""Gravacao no BancoSQLite (esquema de core.esquema_banco) e na auditoria (cadeia de hash).

Todas as linhas seguem as colunas congeladas do esquema; o painel e a avaliacao leem exatamente isto.
O ``sessao_id`` prefixa os ids para que duas sessoes no mesmo banco nao se sobrescrevam.
"""
from __future__ import annotations

import itertools
import json
from typing import Any

from robowin.core.protocolos import Banco, RegistroAuditoria
from robowin.core.tipos import CarimboLatencia, ChamadaLLM, Execucao, ManifestoExperimento, Trade
from robowin.execucao.oms import OrdemOMS
from robowin.risco.tempo import dia_sp

ORIGEM_SINTETICO = "dados_sinteticos"  # mesma origem que o painel usa para acender a faixa SINTETICO


def _json(obj: Any) -> str | None:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


class Registro:
    def __init__(self, banco: Banco, auditoria: RegistroAuditoria | None, sessao_id: str) -> None:
        self.banco = banco
        self.auditoria = auditoria
        self.sessao_id = sessao_id
        self._n = itertools.count(1)
        self.contagem: dict[str, int] = {}

    def novo_id(self, prefixo: str) -> str:
        return f"{self.sessao_id}-{prefixo}-{next(self._n):07d}"

    def _ins(self, tabela: str, linha: dict[str, Any]) -> None:
        self.banco.inserir(tabela, linha)
        self.contagem[tabela] = self.contagem.get(tabela, 0) + 1

    def auditar(self, tipo: str, dados: dict[str, Any]) -> None:
        if self.auditoria is not None:
            self.auditoria.registrar(tipo, dados)

    # ---- decisoes e IA ------------------------------------------------------------------------
    def decisao(self, *, id: str, t_ns: int, braco: str, modo: str, gatilho: str, acao: str,
                intencao: Any = None, proposta: Any = None, revisao: Any = None, risco: Any = None,
                resultado: str, motivos: tuple[str, ...] | list[str] = (), hash_dossie: str = "") -> None:
        linha = {"id": id, "t_ns": t_ns, "braco": braco, "modo": modo, "gatilho": gatilho, "acao": acao,
                 "intencao_json": _json(intencao), "proposta_json": _json(proposta),
                 "revisao_json": _json(revisao), "risco_json": _json(risco), "resultado": resultado,
                 "motivos": ";".join(motivos), "hash_dossie": hash_dossie}
        self._ins("decisoes", linha)
        self.auditar("decisao", {k: v for k, v in linha.items() if k != "t_ns"} | {"t_decisao_ns": t_ns})

    def chamada(self, ch: ChamadaLLM, *, braco: str, t_ns: int, id: str | None = None) -> str:
        cid = id or self.novo_id("llm")
        self._ins("chamadas_llm", {
            "id": cid, "t_ns": t_ns, "braco": braco, "papel": ch.papel, "modelo_pedido": ch.modelo_pedido,
            "modelo_servido": ch.modelo_servido, "request_id": ch.request_id, "versao_api": ch.versao_api,
            "hash_prompt": ch.hash_prompt, "hash_dossie": ch.hash_dossie, "stop_reason": ch.stop_reason,
            "categoria_recusa": ch.categoria_recusa, "tokens_entrada": ch.tokens_entrada,
            "tokens_cache": ch.tokens_cache, "tokens_saida": ch.tokens_saida, "custo_usd": ch.custo_usd,
            "ttft_ms": ch.ttft_ms, "total_ms": ch.total_ms, "valida": int(ch.valida), "erro": ch.erro,
            "resposta_bruta": ch.resposta_bruta})
        self.auditar("chamada_llm", {"id": cid, "braco": braco, **ch.model_dump(mode="json")})
        return cid

    def latencia(self, c: CarimboLatencia, *, t_ns: int, modelo: str = "", effort: str = "") -> None:
        self._ins("latencias", {"t_ns": t_ns, "braco": c.braco, "etapa": c.etapa, "dur_ms": c.dur_ms,
                                "injetado": int(c.injetado), "modelo": modelo, "effort": effort})

    # ---- risco, incidentes ----------------------------------------------------------------------
    def evento_risco(self, *, t_ns: int, braco: str, tipo: str, detalhe: str = "", critico: bool = False) -> None:
        self._ins("eventos_risco", {"t_ns": t_ns, "braco": braco, "tipo": tipo, "detalhe": detalhe,
                                    "critico": int(critico)})
        self.auditar("evento_risco", {"t_ns": t_ns, "braco": braco, "tipo": tipo, "detalhe": detalhe,
                                      "critico": critico})

    def incidente(self, *, t_ns: int, origem: str, severidade: str, detalhe: str) -> None:
        self._ins("incidentes", {"t_ns": t_ns, "origem": origem, "severidade": severidade, "detalhe": detalhe})
        self.auditar("incidente", {"t_ns": t_ns, "origem": origem, "severidade": severidade, "detalhe": detalhe})

    # ---- execucao ---------------------------------------------------------------------------------
    def ordem(self, o: OrdemOMS, *, simulada: bool = True) -> None:
        self._ins("ordens", {
            "id": o.id, "t_ns": o.t_criacao_ns, "braco": o.braco, "intencao_id": o.intencao_id,
            "lado": o.cmd.lado.value, "tipo": o.cmd.tipo, "quantidade": o.cmd.quantidade, "preco": o.cmd.preco,
            "estado": o.estado.value, "ticket": o.ticket, "retcode": o.retcode, "simulada": int(simulada)})

    def execucao(self, ex: Execucao, *, ordem_id: str) -> None:
        self._ins("execucoes", {"id": self.novo_id("exe"), "ordem_id": ordem_id, "t_ns": ex.t_ns,
                                "preco": ex.preco, "quantidade": ex.quantidade, "taxas_brl": ex.taxas_brl,
                                "simulada": int(ex.simulada)})

    def trade(self, tr: Trade, *, motivo_saida: str | None = None) -> None:
        self._ins("trades", {
            "id": tr.id, "braco": tr.braco, "lado": tr.lado.value, "entrada": tr.entrada, "saida": tr.saida,
            "t_entrada_ns": tr.t_entrada_ns, "t_saida_ns": tr.t_saida_ns, "bruto_brl": tr.bruto_brl,
            "tarifas_brl": tr.tarifas_brl, "corretagem_brl": tr.corretagem_brl, "slippage_brl": tr.slippage_brl,
            "liquido_brl": tr.liquido_brl, "motivo_saida": motivo_saida or tr.motivo_saida,
            "dia": dia_sp(tr.t_saida_ns).isoformat()})
        self.auditar("trade", {**tr.model_dump(mode="json"), "liquido_brl": tr.liquido_brl,
                               "motivo_saida": motivo_saida or tr.motivo_saida})

    def posicao(self, *, braco: str, t_ns: int, lado: str | None, quantidade: int, preco_medio: float,
                stop: float | None, alvo: float | None) -> None:
        self._ins("posicoes", {"braco": braco, "t_ns": t_ns, "lado": lado, "quantidade": quantidade,
                               "preco_medio": preco_medio, "stop": stop, "alvo": alvo})

    def equity(self, *, braco: str, t_ns: int, equity_brl: float, drawdown_brl: float) -> None:
        self._ins("equity", {"braco": braco, "t_ns": t_ns, "equity_brl": equity_brl, "drawdown_brl": drawdown_brl})

    def orcamento(self, *, dia: str, braco: str, gasto_usd: float, reservado_usd: float = 0.0) -> None:
        self._ins("orcamento_ia", {"dia": dia, "braco": braco, "gasto_usd": gasto_usd, "reservado_usd": reservado_usd})

    # ---- experimento e modelos ---------------------------------------------------------------------
    def experimento(self, man: ManifestoExperimento, *, t_ns: int, status: str) -> None:
        self._ins("experimentos", {"id": man.id, "criado_ns": t_ns, "manifesto_json": _json(man), "status": status})
        self.auditar("experimento", {"manifesto": man.model_dump(mode="json"), "status": status})

    def modelo(self, *, nome: str, versao: str, status: str, hash_dados: str = "", hash_codigo: str = "",
               treinado_ate_ns: int = 0, metricas: dict[str, float] | None = None, ativo: bool = True) -> None:
        self._ins("modelos", {"nome": nome, "versao": versao, "status": status, "hash_dados": hash_dados,
                              "hash_codigo": hash_codigo, "treinado_ate_ns": treinado_ate_ns,
                              "metricas_json": json.dumps(metricas or {}), "ativo": int(ativo)})
