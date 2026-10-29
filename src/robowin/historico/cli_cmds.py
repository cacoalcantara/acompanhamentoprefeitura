"""CLI: ``robowin historico`` (simulacao historica walk-forward causal sobre dados REAIS do armazem).

Exemplo (dados importados com ``robowin dados importar-b3``)::

    robowin historico --ativo WIN --armazem dados_locais/ticks_b3 --dias 10 --saida relatorios/historico

Limites de risco: os do usuario+conta ativos (``--usuario/--conta`` ou ``[geral]``) pela MESMA ``risco_para`` da
simulacao; sem selecao, o arquivo unico ``--risco`` com o registro de aprovacao EFETIVO (``--aprovacao``; padrao
``None`` = ``<pasta do risco>/aprovacoes/risco-v1.toml`` resolvido por ``resolver_aprovacao``, a versao valida mais
recente). Nunca ``risco_do_ativo`` direto nem um registro fixo (C25).
"""
from __future__ import annotations

import argparse
import dataclasses
import time
from pathlib import Path
from typing import Any

from robowin.core.config import ErroConfig, carregar_toml, resolver_aprovacao


def aprovacao_efetiva(aprovacao: str | Path | None, risco: str | Path) -> Path | None:
    """``--aprovacao`` explicito ou o ponteiro padrao ``<pasta do risco>/aprovacoes/risco-v1.toml``, resolvido para a
    versao valida mais recente (``robowin risco aprovar`` grava ``-v<N+1>``; nunca sobrescreve)."""
    base = Path(aprovacao) if aprovacao else Path(risco).parent / "aprovacoes" / "risco-v1.toml"
    return resolver_aprovacao(base, risco)


def _preregistro(a: argparse.Namespace) -> tuple[Any, Path | None, dict[str, str] | None]:
    """(PreRegistro, pasta da cadeia, hashes atuais das variantes) quando ``--preregistro`` foi pedido."""
    if not a.preregistro:
        return None, None, None
    from robowin.avaliacao import preregistro as pre
    pasta = Path(a.preregistro)
    try:
        cadeia = pre.verificar(pasta)
    except pre.ErroPreRegistro as e:
        raise ErroConfig(f"pre-registro invalido em {pasta}: {e}") from e
    escolhido = [p for p in cadeia if a.experimento is None or p.id_experimento == a.experimento]
    if not escolhido:
        raise ErroConfig(f"experimento {a.experimento or '(qualquer)'} nao registrado em {pasta}")
    hashes = pre.variantes_padrao(pre.hashes_componentes(Path(a.config).parent, seed=a.seed))
    return escolhido[-1], pasta, hashes


def _acumulado(a: argparse.Namespace, p: Any, ativo: str,
               hashes: dict[str, str] | None) -> tuple[Path, str, set[Any]] | None:
    """(pasta, assinatura, pregoes ja acumulados) com ``--acumulado``; None sem ele (docs/16 4.3, item 7)."""
    if not getattr(a, "acumulado", None):
        return None
    from robowin.avaliacao import preregistro as pre

    from . import acumulado as ac
    from .runner import provedor_decidido
    variantes = hashes or pre.variantes_padrao(pre.hashes_componentes(Path(a.config).parent, seed=a.seed))
    assin = ac.assinatura(p, ativo, provedor_decidido(p), variantes)
    raiz = Path(a.acumulado)
    return raiz, assin, ac.datas_acumuladas(raiz, assin)


def _cmd(a: argparse.Namespace) -> int:
    from robowin.core.instrumentos import perfil_da_config
    from robowin.risco.usuarios import ErroUsuario, risco_para, selecao_de

    from .relatorio import escrever
    from .runner import SELO, ErroHistorico, executar_historico, params_de_config
    cfg = carregar_toml(a.config) if Path(a.config).is_file() else {}
    bracos = tuple(x.strip() for x in a.bracos.split(",") if x.strip()) if a.bracos else None
    aprov = aprovacao_efetiva(a.aprovacao, a.risco)
    try:
        sel = selecao_de(a.config, a.usuario, a.conta, a.usuarios)
        pr, dir_pr, hashes = _preregistro(a)
        p = params_de_config(
            cfg, armazem=Path(a.armazem), saida=Path(a.saida), ativo=a.ativo, dias=a.dias, bracos=bracos,
            provedor=a.provedor, seed=a.seed, compactar_ms=a.compactar_ms, dias_treino=a.dias_treino,
            tipo_modelo=a.tipo_modelo, orcamento_usd=a.orcamento_usd, n_boot=a.n_boot,
            com_condicional=not a.sem_condicional, config=Path(a.config), catalogo=Path(a.catalogo),
            risco=Path(a.risco), aprovacao=aprov, usuario=sel.usuario,
            conta_negociacao=sel.chave.texto if sel.chave is not None else None,
            dir_usuarios=Path(a.usuarios) if a.usuarios else None, contrafactuais=a.contrafactuais or None,
            cotacao_usd_conta=a.cotacao_usd_conta)
        print(f"*** {SELO} ***")
        print(f"Limites de risco: {sel.descricao()}.")
        perfil = perfil_da_config(cfg, a.ativo, permitir_a_confirmar=True)
        acum = _acumulado(a, p, perfil.simbolo_base, hashes)
        if acum is not None:
            p = dataclasses.replace(p, pular_datas=frozenset(acum[2]))
        res = executar_historico(p, ao_progresso=print)
        if acum is not None:
            from . import acumulado as ac
            raiz, assin, _ = acum
            novos = ac.salvar(raiz, assin, res.dias)
            res = ac.mesclar(res, ac.carregar(raiz, assin))
            print(f"amostra acumulada em {raiz / assin}: {len(novos)} pregao(oes) novo(s), {len(res.dias)} no total")
        risco, _, _ = risco_para(sel, perfil, p.risco, p.aprovacao, ao_vivo=False)
        caminhos = escrever(res, perfil, risco, preregistro=pr, dir_preregistro=dir_pr, hashes_variantes=hashes)
    except (ErroHistorico, ErroConfig, ErroUsuario) as e:
        print(f"ERRO: {e}")
        return 2
    print(caminhos["consolidado_md"].read_text(encoding="utf-8"))
    print(f"relatorios: {caminhos['consolidado_md']} e {Path(p.saida) / 'dias'}/<pregao>/relatorio.md")
    return 0


def registrar(sub: Any) -> None:
    from robowin.risco.usuarios import adicionar_args
    p = sub.add_parser("historico", help="simulacao historica walk-forward CAUSAL sobre dados REAIS (ex.: negocios B3)")
    p.add_argument("--armazem", required=True, help="armazem de ticks REAIS (robowin dados importar-b3 --armazem)")
    p.add_argument("--saida", default=f"dados_locais/historico/execucao-{int(time.time())}")
    p.add_argument("--ativo", default=None, help="simbolo_base do perfil (padrao: [geral].simbolo_base)")
    p.add_argument("--dias", type=int, default=None, help="simula os ultimos N pregoes do armazem (padrao: todos)")
    p.add_argument("--bracos", default=None,
                   help="ids separados por virgula (ex.: quant,claude_gpt); os controles entram sempre")
    p.add_argument("--provedor", choices=["auto", "falso", "real"], default="auto",
                   help="auto (padrao): real so com chave de API para todos os provedores dos papeis; senao FALSO")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--compactar-ms", type=float, default=None,
                   help="janela da compactacao causal de corridas (padrao [historico].compactar_ms; 0 desliga)")
    p.add_argument("--dias-treino", type=int, default=None, help="pregoes anteriores no treino (padrao [historico])")
    p.add_argument("--tipo-modelo", choices=["lightgbm", "logistica"], default="logistica")
    p.add_argument("--orcamento-usd", type=float, default=None, help="sobrepoe [ia].orcamento_mensal_usd")
    p.add_argument("--n-boot", type=int, default=1000)
    p.add_argument("--sem-condicional", action="store_true", help="so as variantes de modo imediato")
    p.add_argument("--cotacao-usd-conta", type=float, default=None, metavar="FATOR",
                   help="cotacao registrada USD -> moeda da conta para descontar o custo de API por pregao (H1-H3)")
    p.add_argument("--contrafactuais", action="store_true",
                   help="calcula os contrafactuais do planejador por pregao (so diagnostico; nunca altera limites)")
    p.add_argument("--preregistro", default=None, metavar="PASTA",
                   help="cadeia de pre-registro (ex.: config/experimentos): o selo recusa pregao <= registro, hash "
                        "divergente e FALSO")
    p.add_argument("--experimento", default=None, help="id do experimento na cadeia (padrao: o ultimo registrado)")
    p.add_argument("--acumulado", default=None, metavar="PASTA",
                   help="acumula os pregoes simulados entre execucoes (mesma assinatura: codigo, config, seed) e "
                        "consolida a uniao; pregoes ja acumulados nao sao simulados de novo")
    p.add_argument("--config", default="config/robowin.toml")
    p.add_argument("--catalogo", default="config/modelos.toml")
    p.add_argument("--risco", default="config/risco.toml")
    p.add_argument("--aprovacao", default=None,
                   help="registro de aprovacao (padrao: <pasta do risco>/aprovacoes/risco-v1.toml, resolvido para a "
                        "versao valida mais recente)")
    adicionar_args(p)
    p.set_defaults(func=_cmd)
