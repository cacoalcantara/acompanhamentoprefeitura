"""Invariantes de risco como funcoes puras, checaveis por propriedade (hypothesis)."""
from __future__ import annotations

from collections.abc import Sequence

from ..core.tipos import Lado


def exposicao_total(qtd_posicao: int, pendentes_entrada: Sequence[int]) -> int:
    """|posicao liquida| + pendentes de entrada (qualquer sentido: ordens opostas podem preencher em sequencia)."""
    return abs(qtd_posicao) + sum(abs(q) for q in pendentes_entrada)


def inv_exposicao(qtd_posicao: int, pendentes_entrada: Sequence[int], contratos_max: int = 1) -> bool:
    return exposicao_total(qtd_posicao, pendentes_entrada) <= contratos_max


def inv_posicao_assinada(posicao_assinada: int, pendentes_assinados: Sequence[int], contratos_max: int = 1) -> bool:
    """Versao com sinal (compra +, venda -): |liquida + pendentes no mesmo sentido| e o pior caso <= max.

    Pior caso: posicao mais todas as pendentes que a aumentam; as que reduzem nao contam.
    """
    pior = abs(posicao_assinada)
    for p in pendentes_assinados:
        if posicao_assinada == 0 or (p > 0) == (posicao_assinada > 0):
            pior += abs(p)
    return pior <= contratos_max


def inv_sem_martingale(tamanhos: Sequence[int], perdas: Sequence[bool]) -> bool:
    """tamanhos[i] e o tamanho da operacao i; perdas[i] diz se terminou em perda. Nunca cresce apos perda."""
    for i in range(1, len(tamanhos)):
        if perdas[i - 1] and tamanhos[i] > tamanhos[i - 1]:
            return False
    return True


def stop_so_aperta(lado: Lado, stop_antigo: float | None, stop_novo: float | None) -> bool:
    """Stop so pode aproximar-se do preco (subir na compra, descer na venda). Nunca remover."""
    if stop_novo is None:
        return False
    if stop_antigo is None:
        return True
    return stop_novo >= stop_antigo if lado == Lado.COMPRA else stop_novo <= stop_antigo


def inv_entrada_permitida(entrou: bool, dentro_da_janela: bool, kill_ativo: bool) -> bool:
    """Nenhuma entrada fora da janela ou com kill switch ativo."""
    return (not entrou) or (dentro_da_janela and not kill_ativo)


def inv_sem_aumento(qtd_antes: int, qtd_depois: int) -> bool:
    """Sem aumentar posicao: uma posicao aberta nunca cresce (sem preco medio nem piramidacao)."""
    return qtd_antes == 0 or qtd_depois <= qtd_antes


def inv_perda_operacao(stop_pontos: float, folga_pontos: float, quantidade: int, ponto_valor_brl: float,
                       perda_max_brl: float) -> bool:
    return (stop_pontos + folga_pontos) * quantidade * ponto_valor_brl <= perda_max_brl + 1e-9


def inv_reducao_so_reduz(qs: Sequence[int]) -> bool:
    """Quantidades sucessivas de uma posicao (episodio): nunca crescem e nunca ficam negativas."""
    return all(q >= 0 for q in qs) and all(b <= a for a, b in zip(qs, qs[1:], strict=False))


def inv_stop_monotono(lado: Lado, seq: Sequence[float | None]) -> bool:
    """Sequencia de stops de uma posicao: cada novo stop so aperta (e nenhum e removido depois de existir)."""
    return all(stop_so_aperta(lado, a, b) for a, b in zip(seq, seq[1:], strict=False))


def inv_distancia_minima(lado: Lado, stop: float, preco_saida: float, dist: float) -> bool:
    """Stop do lado certo e a pelo menos `dist` pontos do preco executavel de saida (bid na compra, ask na venda)."""
    folga = preco_saida - stop if lado == Lado.COMPRA else stop - preco_saida
    return folga >= dist - 1e-9


def inv_tempo_no_teto(aberta_ns: int, prazo_ns: int, teto_s: int) -> bool:
    """Prazo da posicao (instante, ns) nunca alem de `teto_s` segundos desde a abertura (ns)."""
    return prazo_ns - aberta_ns <= teto_s * 1_000_000_000
