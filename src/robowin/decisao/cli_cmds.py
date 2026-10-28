"""CLI: ``robowin simular`` (sessao completa sobre dados SINTETICOS) e ``robowin nucleo`` (ao vivo via gateway).

Nenhum comando envia ordem real. ``simular`` usa o provedor FALSO por padrao (``--provedor real`` so com
chaves de API no ambiente); ``nucleo`` consome os ticks/livros do gateway por ZeroMQ.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from robowin.core.config import ErroConfig, carregar_toml


def _params(a: argparse.Namespace) -> Any:
    from .simulacao import ParamsSimulacao
    return ParamsSimulacao(
        seed=a.seed, n_ticks=a.n_ticks, sinal_forca=a.sinal_forca, saida=Path(a.saida), provedor=a.provedor,
        config=Path(a.config), catalogo=Path(a.catalogo), risco=Path(a.risco), aprovacao=Path(a.aprovacao),
        theta_brl=a.theta, com_condicional=not a.sem_condicional, p_timeout=a.p_timeout, p_http=a.p_http,
        passo_s=a.passo_s, checagem_s=a.checagem_s, ativo=a.ativo, odds_sessoes=getattr(a, "odds_sessoes", 0),
        registro_modelos=Path(a.registro_modelos) if a.registro_modelos else None, tipo_modelo=a.tipo_modelo,
        idade_max_dado_s=a.idade_max_dado_s if a.idade_max_dado_s is not None else 60.0)


def _cmd_simular(a: argparse.Namespace) -> int:
    from .simulacao import executar_sessao, formatar_resumo, montar_sessao
    p = _params(a)
    try:
        s = montar_sessao(p)
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    ativo = s.orq.ativo
    print(f"SIMULACAO / SINTETICO: sessao completa em tempo simulado (nenhuma ordem real). Ativo: {ativo}.")
    if s.orq.perfil.a_confirmar:
        print(f"ATENCAO: perfil {ativo} a_confirmar ({'; '.join(s.orq.perfil.a_confirmar_itens)}). "
              "Resultado so de simulacao, rotulado 'ativo_a_confirmar'; operar esta BLOQUEADO ate confirmar.")
    try:
        resumo = asyncio.run(executar_sessao(s))
    finally:
        s.banco.fechar()
    print(formatar_resumo(resumo, rotulo_ia=s.rotulo_dados, caminho_banco=s.caminho_banco,
                          dir_auditoria=s.dir_auditoria, moeda=s.orq.perfil.moeda))
    print(f"\nPainel: robowin painel --banco {s.caminho_banco}")
    return 0


def _modelo_do_ativo(ativo: str, tipo: str, registro: str | None, perfil_hash: str | None = None) -> Any:
    """Campeao do ATIVO (ou, sem campeao, a versao em_validacao mais recente: exploratoria, rotulada). Sem artefato
    valido (ausente, adulterado, de outro ativo...) devolve modelo `nao_treinado`: o braco quant NAO opera."""
    from robowin.modelos.microestrutura import ModeloMicroestrutura
    from robowin.modelos.persistencia import caminho_registro_ativo, carregar_para_ativo
    res = carregar_para_ativo(registro or caminho_registro_ativo(ativo), f"microestrutura_{tipo}", ativo, perfil_hash)
    if res.modelo is None:
        print(f"AVISO: sem modelo carregavel para {ativo} ({res.motivo}); braco quant 'nao_treinado' (nao opera).")
        m = ModeloMicroestrutura(tipo)
        m.ativo = ativo
        return m
    print(f"modelo {res.modelo.nome}@{res.versao} ({res.papel}; status {res.modelo.status}; dados "
          f"{res.modelo.origem_dados}) carregado para {ativo}.")
    return res.modelo


def _cmd_nucleo(a: argparse.Namespace) -> int:
    from robowin.core.config import caminho_flag_emergencia
    from robowin.core.instrumentos import perfil_da_config
    from robowin.core.relogio import RelogioReal
    from robowin.dados.ticks import data_sp

    from .nucleo import FonteZMQ, Nucleo, piscina_odds_nucleo
    from .simulacao import ParamsSimulacao, abrir_banco, construir_orquestrador, formatar_resumo

    p = ParamsSimulacao(
        saida=Path(a.saida), provedor=a.provedor, config=Path(a.config), catalogo=Path(a.catalogo),
        risco=Path(a.risco), aprovacao=Path(a.aprovacao), passo_s=a.passo_s, checagem_s=a.checagem_s, ativo=a.ativo,
        idade_max_dado_s=a.idade_max_dado_s if a.idade_max_dado_s is not None else 15.0,
        com_condicional=not a.sem_condicional, arquivo_emergencia=caminho_flag_emergencia(a.config, a.arquivo_emergencia))
    rel = RelogioReal()
    sessao_id = f"nucleo-{int(time.time())}"
    try:
        cfg_geral = carregar_toml(a.config) if Path(a.config).is_file() else {}
        perfil = perfil_da_config(cfg_geral, a.ativo)  # ao vivo: perfil a_confirmar BLOQUEIA (PerfilNaoConfirmado)
        banco, registro, caminho, dir_aud = abrir_banco(p.saida, rel, sessao_id)
        modelo = _modelo_do_ativo(perfil.simbolo_base, a.tipo_modelo, a.registro_modelos, perfil.hash())
        # Maquina do Tempo: so sessoes ANTERIORES ao pregao corrente, do mesmo ativo; sem base => sem avaliador
        odds = piscina_odds_nucleo(cfg_geral, perfil, data_sp(rel.agora_ns()), config_path=a.config)
        m = construir_orquestrador(
            p, rel=rel, registro=registro, ins=perfil.instrumento(a.simbolo or perfil.simbolo_base),
            modelo=modelo, statarb=None, precos_aux=None, sintetico=False,
            modo_tempo="real", sessao_id=sessao_id, t_ref_ns=rel.agora_ns(), piscina_odds=odds.piscina)
        odds.registrar(registro, rel.agora_ns())
        print(odds.detalhe())
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    fonte = FonteZMQ(a.pub)
    cliente = None
    if a.cmd and not a.sem_heartbeat:
        from robowin.mt5.canais import ClienteComandosZMQ
        cliente = ClienteComandosZMQ(a.cmd)
    espelho = monitor = None
    espelhar = (a.espelhar if a.espelhar is not None else str(cfg_geral.get("geral", {}).get("espelhar", "") or "")).strip()
    if cliente is not None:
        from .espelho import MonitorGateway
        monitor = MonitorGateway(m.orq.registro, arquivo_flag=p.arquivo_emergencia)
    if espelhar:  # desligado por padrao; UM braco por vez; so conta demo (ver `decisao/espelho.py`)
        from robowin.core.config import dir_dados
        from robowin.execucao.ponte_gateway import TravaEspelho

        from .espelho import ConfigEspelho, EspelhoDemo, EspelhoRecusado, validar_config_espelhamento
        try:
            validar_config_espelhamento(cfg_geral, espelhar, canal_ok=cliente is not None)
            espelho = EspelhoDemo(m.orq, ConfigEspelho(braco=espelhar), cliente,
                                  trava=TravaEspelho(dir_dados(a.config) / "ESPELHO.lock"))
            espelho.iniciar()
        except (EspelhoRecusado, ErroConfig) as e:
            print(f"ERRO (espelhamento recusado): {e}")
            fonte.fechar()
            if cliente is not None:
                cliente.fechar()
            banco.fechar()
            return 2
    nucleo = Nucleo(m.orq, fonte, heartbeat=(lambda: cliente.enviar_sem_resposta({"tipo": "heartbeat"}))
                    if cliente else None, espelho=espelho, monitor=monitor)
    print(f"SIMULACAO: nucleo ao vivo em {a.pub} (comandos {a.cmd or '-'}); braco(s): "
          f"{', '.join(m.orq.contas)}. Nenhuma ordem real; Ctrl+C encerra.")
    if espelho is not None:
        print(f"ESPELHAMENTO NA CONTA DEMO: braco '{espelhar}' (SL obrigatorio; mesmo veto de risco; um braco por vez). "
              "Ordens SOMENTE em conta DEMO, via gateway.")
    m.orq.iniciar(m.manifesto, t_ns=rel.agora_ns())

    async def rodar() -> None:
        try:
            await nucleo.executar(duracao_s=a.duracao_s)
        finally:
            await m.orq.encerrar(m.manifesto)

    try:
        asyncio.run(rodar())
    except KeyboardInterrupt:
        pass
    finally:
        fonte.fechar()
        if cliente is not None:
            cliente.fechar()
        banco.fechar()
    duble = bool(nucleo.estado_gateway.get("duble"))
    rotulo = "SIMULACAO - nucleo ao vivo, ordens SOMENTE no simulador interno" + (
        " / DADOS DO DUBLE (SINTETICO)" if duble else "")
    print(formatar_resumo(
        m.orq.resumo(), rotulo_ia="real" if m.real else "falso", caminho_banco=caminho, dir_auditoria=dir_aud,
        rotulo_dados=rotulo, extra=f"ticks processados: {m.orq.ticks_processados}; fora de ordem: "
        f"{m.orq.fora_de_ordem}; lacunas de sequencia: {fonte.lacunas}; mensagens: {nucleo.mensagens}"
        + (f"\nespelho (demo): {espelho.resumo()}" if espelho is not None else "")))
    return 0


def _comuns(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provedor", choices=["falso", "real"], default="falso",
                   help="falso (padrao, rotulado) ou real (SDKs oficiais; exige chaves de API no ambiente)")
    p.add_argument("--config", default="config/robowin.toml")
    p.add_argument("--ativo", default=None,
                   help="simbolo_base do perfil em config/instrumentos.toml (padrao: [geral].simbolo_base)")
    p.add_argument("--catalogo", default="config/modelos.toml")
    p.add_argument("--risco", default="config/risco.toml")
    p.add_argument("--aprovacao", default="config/aprovacoes/risco-v1.toml")
    p.add_argument("--sem-condicional", action="store_true", help="so as variantes de modo imediato")
    p.add_argument("--passo-s", type=float, default=15.0, help="periodo dos bracos sem IA")
    p.add_argument("--checagem-s", type=float, default=15.0, help="a cada quanto os gatilhos sao avaliados")
    p.add_argument("--idade-max-dado-s", type=float, default=None,
                   help="dado mais velho que isto bloqueia entradas (padrao: 60 no simular, 15 no nucleo)")


def registrar(sub: Any) -> None:
    s = sub.add_parser("simular", help="sessao completa em tempo simulado sobre dados SINTETICOS")
    _comuns(s)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--n-ticks", type=int, default=12_000, help="ticks por pregao sintetico (treino + sessao)")
    s.add_argument("--sinal-forca", type=float, default=0.05)
    s.add_argument("--odds-sessoes", type=int, default=0,
                   help="N > 0: gera N pregoes sinteticos anteriores e liga a Maquina do Tempo (probabilidades do "
                        "plano como evidencia ao revisor; veto so com [decisao].veto_por_odds = true)")
    s.add_argument("--saida", default=f"dados_locais/simulacao/sessao-{int(time.time())}")
    s.add_argument("--registro-modelos", default=None,
                   help="registro de modelos do ativo: usa o artefato persistido em vez de treinar na hora")
    s.add_argument("--tipo-modelo", choices=["lightgbm", "logistica"], default="logistica")
    s.add_argument("--theta", type=float, default=0.0, help="theta (R$) do braco quant")
    s.add_argument("--p-timeout", type=float, default=0.03, help="fracao de timeouts injetados no provedor falso")
    s.add_argument("--p-http", type=float, default=0.02, help="fracao de erros 429 injetados no provedor falso")
    s.set_defaults(func=_cmd_simular)

    n = sub.add_parser("nucleo", help="nucleo ao vivo: consome o gateway via ZeroMQ (simulacao, sem ordens reais)")
    _comuns(n)
    n.add_argument("--registro-modelos", default=None,
                   help="padrao: dados_locais/modelos/<ATIVO>/registro.json (sem artefato valido: quant nao_treinado)")
    n.add_argument("--tipo-modelo", choices=["lightgbm", "logistica"], default="lightgbm")
    n.add_argument("--pub", default="tcp://127.0.0.1:5555")
    n.add_argument("--cmd", default="tcp://127.0.0.1:5556", help="ROUTER do gateway (heartbeat); '' desliga")
    n.add_argument("--sem-heartbeat", action="store_true")
    n.add_argument("--espelhar", default=None, metavar="BRACO",
                   help="espelha UM braco na conta DEMO via gateway (padrao: [geral].espelhar; vazio desliga)")
    n.add_argument("--simbolo", default=None, help="simbolo da corretora (padrao: o simbolo_base do ativo)")
    n.add_argument("--saida", default=f"dados_locais/nucleo/sessao-{int(time.time())}")
    n.add_argument("--arquivo-emergencia", default=None,
                   help="padrao: <[geral].dados do robowin.toml>/EMERGENCIA (mesmo caminho do painel e do gateway)")
    n.add_argument("--duracao-s", type=float, default=None)
    n.set_defaults(func=_cmd_nucleo)
