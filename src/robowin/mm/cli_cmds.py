"""`robowin mm demo`: market making EXPERIMENTAL no simulador, em dados SINTETICOS rotulados."""
from __future__ import annotations

import time

from ..core.instrumentos import argumentos_ativo, perfil_do_cli
from ..dados.sintetico import ROTULO_SINTETICO, ConfigSintetico, gerar
from .avellaneda_stoikov import ParamsAS
from .config import ConfigMM
from .cotacao import CotadorMM
from .markouts import markouts, serie_meio
from .simulacao import simular_mm

AVISO = (f"*** {ROTULO_SINTETICO} ***\n"
         "Market making de varejo via MT5 e EXPERIMENTAL: fila no fim, spread de 1 tick, tarifa ~0,5 tick por ida e\n"
         "volta e selecao adversa provavel. Roda SO no simulador (fila conservadora); demo_mm desligada por padrao.")


def _demo(a) -> int:
    print(AVISO)
    if a.demo_mm:
        print("ERRO: 'mm demo' roda SO no simulador. A ponte com a conta DEMO e o comando 'robowin mm ao-vivo' "
              "(exige gateway, [mm].demo_mm = true e espelhamento exclusivo).")
        return 2
    cfg = ConfigMM(demo_mm=False)
    perfil = perfil_do_cli(a)
    ins = perfil.instrumento(f"SINT_{perfil.simbolo_base}")
    dados = gerar(ConfigSintetico.do_perfil(perfil, seed=a.seed, n_ticks=a.ticks, sinal_forca=a.sinal))
    cot = CotadorMM(params=ParamsAS.do_instrumento(ins), cfg=cfg)
    r = simular_mm(dados.ticks, dados.livros, cot, instrumento=ins, seed=a.seed, cfg=cfg)
    t, m = serie_meio(dados.ticks)
    _, resumos, capt = markouts(r.fills, t, m)
    print(f"decisoes: {r.n_decisoes}; fills: {len(r.fills)}; mensagens: {r.mensagens}")
    print(f"estoque maximo |q|: {r.max_abs_estoque} (limite 1); estoque final: {r.estoque_final}")
    print(f"resultado liquido realizado (R$, apos tarifas): {r.liquido_brl:+.2f}")
    print(f"captura inicial media: {capt:+.2f} pontos")
    for s in resumos:
        print(f"markout {s.horizonte_s:>4.0f}s: media {s.media_pts:+.2f} pts (n={s.n}, sem dados={s.n_sem_dados})")
    print("Markout medio negativo = selecao adversa. Nada acima e evidencia de lucro.")
    return 0


def _ao_vivo(a) -> int:
    """MM na conta DEMO via gateway (SOMENTE demo). Exige [mm].demo_mm = true (ou --demo-mm) e o gateway em --modo-mm."""
    import asyncio
    import time
    from pathlib import Path

    from ..core.config import ErroConfig, caminho_flag_emergencia, carregar_toml, dir_dados
    from ..core.instrumentos import perfil_da_config
    from ..core.relogio import RelogioReal
    from ..decisao.nucleo import FonteZMQ
    from ..decisao.simulacao import abrir_banco
    from ..execucao.ponte_gateway import TravaEspelho
    from ..mt5.canais import ClienteComandosZMQ
    from ..risco.politica import risco_do_ativo
    from .ao_vivo import MMAoVivo
    from .ponte_demo import ConfigPonteMM, MMDemoRecusado, PonteMMDemo, validar_config_mm

    print(AVISO)
    cfg = carregar_toml(a.config) if Path(a.config).is_file() else {}
    if not (a.demo_mm or (cfg.get("mm") or {}).get("demo_mm")):
        print("ERRO: demo_mm desligada ([mm].demo_mm = false). Ligue-a no TOML ou use --demo-mm.")
        return 2
    rel = RelogioReal()
    fonte = cliente = banco = ponte = None
    try:
        validar_config_mm(cfg)
        perfil = perfil_da_config(cfg, a.ativo)  # ao vivo: perfil a_confirmar BLOQUEIA
        ins = perfil.instrumento(a.simbolo or perfil.simbolo_base)
        risco, _ = risco_do_ativo(perfil, a.risco, a.aprovacao, ao_vivo=True)
        banco, registro, caminho, _ = abrir_banco(Path(a.saida), rel, f"mm-{int(time.time())}")
        cot = CotadorMM(params=ParamsAS.do_instrumento(ins), cfg=ConfigMM(demo_mm=True))
        fonte, cliente = FonteZMQ(a.pub), ClienteComandosZMQ(a.cmd)
        ponte = PonteMMDemo(cot, ins, perfil, cliente, rel, risco, ConfigPonteMM(stop_pontos=perfil.stop_padrao_pontos),
                            arquivo_emergencia=caminho_flag_emergencia(a.config, a.arquivo_emergencia),
                            trava=TravaEspelho(dir_dados(a.config) / "ESPELHO.lock"), registro=registro)
        ponte.iniciar()
    except (ErroConfig, MMDemoRecusado) as e:
        print(f"ERRO (MM na demo recusado): {e}")
        for x in (fonte, cliente, banco):
            if x is not None:
                x.fechar()
        return 2
    print(f"MM NA CONTA DEMO via gateway ({a.pub}/{a.cmd}); banco {caminho}. Ordens SOMENTE em conta DEMO. Ctrl+C encerra.")
    laco = MMAoVivo(ponte, cot, fonte, rel, heartbeat=lambda: cliente.enviar_sem_resposta({"tipo": "heartbeat"}))
    try:
        asyncio.run(laco.executar(duracao_s=a.duracao_s))
    except KeyboardInterrupt:
        pass
    finally:
        fonte.fechar()
        cliente.fechar()
        banco.fechar()
    print(f"resumo: {ponte.resumo()}")
    return 0


def registrar(sub):
    p = sub.add_parser("mm", help="market making experimental (SIMULACAO)").add_subparsers(dest="mm_cmd")
    d = p.add_parser("demo", help="demonstracao em dados sinteticos rotulados")
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--ticks", type=int, default=30_000)
    d.add_argument("--sinal", type=float, default=0.0)
    d.add_argument("--demo-mm", action="store_true", help="recusado aqui: a ponte e o comando 'mm ao-vivo'")
    argumentos_ativo(d)
    d.set_defaults(func=_demo)

    v = p.add_parser("ao-vivo", help="MM EXPERIMENTAL na conta DEMO via gateway (atras de [mm].demo_mm)")
    v.add_argument("--config", default="config/robowin.toml")
    v.add_argument("--ativo", default=None)
    v.add_argument("--simbolo", default=None)
    v.add_argument("--risco", default="config/risco.toml")
    v.add_argument("--aprovacao", default="config/aprovacoes/risco-v1.toml")
    v.add_argument("--demo-mm", action="store_true", help="liga a flag demo_mm so nesta execucao")
    v.add_argument("--pub", default="tcp://127.0.0.1:5555")
    v.add_argument("--cmd", default="tcp://127.0.0.1:5556")
    v.add_argument("--arquivo-emergencia", default=None)
    v.add_argument("--saida", default=f"dados_locais/mm/sessao-{int(time.time())}")
    v.add_argument("--duracao-s", type=float, default=None)
    v.set_defaults(func=_ao_vivo)
