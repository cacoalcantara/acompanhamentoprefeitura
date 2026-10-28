"""`robowin treinar --dados sinteticos`: demonstracao ROTULADA do pipeline completo (SIMULACAO)."""
from __future__ import annotations

import numpy as np

from robowin.core.config import ErroConfig
from robowin.core.instrumentos import argumentos_ativo, perfil_do_cli

from .demo_sintetico import ROTULO, gerar, montar_amostras
from .microestrutura import CriterioOOS, ModeloMicroestrutura, metricas_oos
from .persistencia import caminho_registro_ativo, carregar_versao, registrar_e_salvar
from .registro import RegistroModelos
from .rotulagem import ConfigBarreiras, Custos
from .walkforward import executar_walkforward

NS = 1_000_000_000
AVISO = f"*** DADOS {ROTULO}S: demonstracao do pipeline. Nao prova lucratividade nem gera selo 'validado'. ***"


def _treinar(a) -> int:
    if a.dados != "sinteticos":
        print("somente --dados sinteticos esta disponivel nesta versao (dados reais exigem coleta propria)")
        return 2
    print(AVISO)
    try:
        perfil = perfil_do_cli(a)
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    ticks, livros = gerar(perfil, n_ticks=a.ticks, seed=a.seed, forca_sinal=0.0 if a.sem_sinal else a.forca_sinal,
                          n_dias=a.dias)
    barreiras, custos = ConfigBarreiras.do_perfil(perfil), Custos.do_perfil(perfil)
    am = montar_amostras(ticks, livros, barreiras, custos)
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
    modelo.ativo = perfil.simbolo_base
    m = modelo.avaliar_oos(hold, criterio)
    print("holdout (avaliado uma vez):", {k: round(v, 4) for k, v in m.items()})
    print(f"status do modelo: {modelo.status}  (dados sinteticos nunca recebem 'validado_oos')")

    registro = a.registro or str(caminho_registro_ativo(perfil.simbolo_base))
    reg = RegistroModelos(registro)
    versao = f"s{a.seed}-{modelo.hash_dados[:8]}"
    arq = registrar_e_salvar(reg, modelo, versao, barreiras=barreiras, custos=custos, perfil_hash=perfil.hash())
    reg.registrar_oos(modelo.nome, versao, "holdout-sintetico", m, inicio_segmento_ns=t_ini_hold, criterio=criterio)
    recarregado = carregar_versao(reg, modelo.nome, versao, perfil.simbolo_base, perfil.hash())  # ida e volta conferida
    if not modelo.prever_lote(hold.X).equals(recarregado.prever_lote(hold.X)):
        print("ERRO: o artefato recarregado nao reproduz as previsoes do modelo treinado")
        return 1
    print(f"registrado: {modelo.nome}@{versao} (ativo {perfil.simbolo_base}) em {registro}; artefato: {arq}")
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
    p.add_argument("--registro", default=None,
                   help="arquivo do registro (padrao: dados_locais/modelos/<ATIVO>/registro.json)")
    argumentos_ativo(p)
    p.set_defaults(func=_treinar)
