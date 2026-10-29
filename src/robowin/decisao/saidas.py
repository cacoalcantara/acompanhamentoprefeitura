"""UM dono de saida por posicao (plano 5.2, passo 8).

Stop e alvo sao executados pelo simulador de execucao (equivalente ao SL/TP no servidor, ultima
instancia). Todas as DEMAIS saidas (tempo-limite, ENCERRAR, zeragem, kill switch) passam por este registro:
a posicao so tem UMA reivindicacao de saida por vez; reivindicacoes concorrentes sao recusadas e contadas.
A reivindicacao vive ate a posicao fechar (ou a ordem de saida terminar sem fechar).

Nenhuma saida depende de IA: o dono e sempre codigo deterministico.

Braco planejador (docs/16): ``gestao`` (regra de gestao que encerra, ex.: ``sem_progresso``) e ``parcial`` (reducao
da quantidade, da IA ou automatica). A reducao so acontece SEM dono vigente: ela reivindica a posicao como qualquer
saida e a reivindicacao e liberada quando a parcial executa (ou a ordem termina sem executar).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# prioridade informativa (a primeira reivindicacao vence; a prioridade so e usada para registrar o conflito)
MOTIVOS_SAIDA = ("kill", "zeragem", "tempo", "encerrar", "gestao", "parcial")


@dataclass(frozen=True)
class Reivindicacao:
    braco: str
    ticket: int | None
    motivo: str
    t_ns: int
    intencao_id: str = ""


@dataclass
class GestorSaidas:
    _dono: dict[str, Reivindicacao] = field(default_factory=dict)
    recusadas: list[tuple[Reivindicacao, Reivindicacao]] = field(default_factory=list)  # (nova, vigente), ate 100
    n_recusadas: int = 0

    def reivindicar(self, braco: str, ticket: int | None, motivo: str, t_ns: int, intencao_id: str = "") -> bool:
        """True se esta reivindicacao virou a dona da saida desta posicao."""
        nova = Reivindicacao(braco, ticket, motivo, t_ns, intencao_id)
        atual = self._dono.get(braco)
        if atual is not None and atual.ticket == ticket:
            self.n_recusadas += 1
            if len(self.recusadas) < 100:
                self.recusadas.append((nova, atual))
            return False
        self._dono[braco] = nova
        return True

    def dono(self, braco: str) -> Reivindicacao | None:
        return self._dono.get(braco)

    def liberar(self, braco: str) -> Reivindicacao | None:
        return self._dono.pop(braco, None)

    def tem_dono(self, braco: str) -> bool:
        return braco in self._dono

    def liberar_se(self, braco: str, motivo: str, intencao_id: str | None = None) -> Reivindicacao | None:
        """Libera a reivindicacao SO se ela for deste motivo (e desta intencao, se informada): a parcial executada
        ou recusada nunca solta a reivindicacao de outra saida."""
        atual = self._dono.get(braco)
        if atual is None or atual.motivo != motivo or (intencao_id is not None and atual.intencao_id != intencao_id):
            return None
        return self._dono.pop(braco)
