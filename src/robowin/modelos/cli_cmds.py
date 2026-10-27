"""`robowin treinar --dados sinteticos`: demonstracao ROTULADA do pipeline completo (SIMULACAO)."""
from __future__ import annotations

import numpy as np

from .demo_sintetico import ROTULO, gerar, montar_amostras
from .microestrutura import CriterioOOS, ModeloMicroestrutura, metricas_oos
from .registro import RegistroModelos
from .walkforward import executar_walkforward

NS = 1_000_000_000
AVISO = f"*** DADOS {ROTULO}S: demonstracao do pipeline. Nao prova lucratividade nem gera selo 'validado'. ***"


def _treinar(a) -> int:
    if a.dados != "sinteticos":
        print("somente --dados sinteticos esta disponivel nesta versao (dados reais exigem coleta propria)")
        return 2
    print(AVISO)
    ticks, livros = gerar(n_ticks=a.ticks, seed=a.seed, forca_sinal=0.0 if a.sem_sinal else a.forca_sinal,
                          n_dias=a.dias)
    am = montar_amostras(ticks, livros)
    criterio = CriterioOOS()
    embargo = 60 * NS
    print(f"amostras: {len(am)}; criterio pre-registrado (hash): {criterio.hash()[:12]}")

    wf = executar_walkforward(am, lambda: ModeloMicroestrutura(a.tipo), n_dobras=4, embargo_ns=embargo)
    sub = am.subconjunto(wf.idx_oos)
    m_wf = metricas_oos(wf.pred.loc[wf.idx_oos].reset_index(drop=True), sub,
                        _prior(am.subconjunto(wf.dobras[0].idx_treino)), criterio)
    print("walk-forward (purga+embargo) OOS:", {k: round(v, 4) for k, v in m_wf.items()})

    corte = int(len(am) * 0.7)
    treino = am.subconjunto(np.arange(corte))
    t_ini_hold = int(am.t_ns[corte])
    treino = treino.subconjunto(np.flatnonzero(treino.t_fim_ns <= t_ini_hold - embargo))
    hold = am.subconjunto(np.arange(corte, len(am)))
    modelo = ModeloMicroestrutura(a.tipo)
    modelo.treinar(treino, origem="sintetico", criterio=criterio)
    m = modelo.avaliar_oos(hold, criterio)
    print("holdout (avaliado uma vez):", {k: round(v, 4) for k, v in m.items()})
    print(f"status do modelo: {modelo.status}  (dados sinteticos nunca recebem 'validado_oos')")

    reg = RegistroModelos(a.registro)
    versao = f"s{a.seed}-{modelo.hash_dados[:8]}"
    reg.registrar_modelo(modelo, versao)
    reg.registrar_oos(modelo.nome, versao, "holdout-sintetico", m, inicio_segmento_ns=t_ini_hold, criterio=criterio)
    print(f"registrado: {modelo.nome}@{versao} em {a.registro}")
    return 0


def _prior(am) -> dict[str, np.ndarray]:
    return {lado: np.bincount(am.y(lado), minlength=3) / len(am) for lado in ("compra", "venda")}


def registrar(sub):
    p = sub.add_parser("treinar", help="treina o modelo de microestrutura (demonstracao com dados sinteticos)")
    p.add_argument("--dados", default="sinteticos", choices=["sinteticos"])
    p.add_argument("--tipo", default="lightgbm", choices=["lightgbm", "logistica"])
    p.add_argument("--ticks", type=int, default=120_000)
    p.add_argument("--dias", type=int, default=4)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--forca-sinal", type=float, default=0.1)
    p.add_argument("--sem-sinal", action="store_true", help="gera ruido puro (sem edge)")
    p.add_argument("--registro", default="dados_locais/modelos/registro.json")
    p.set_defaults(func=_treinar)
