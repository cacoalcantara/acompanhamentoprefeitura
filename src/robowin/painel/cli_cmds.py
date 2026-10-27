def _painel(a):
    import uvicorn

    from .app import criar_app

    cfg = {}
    try:
        from robowin.core.config import carregar_toml
        cfg = carregar_toml(a.config)
    except OSError:
        pass
    app = criar_app(banco=a.banco, dir_dados=a.dados, config=cfg)
    uvicorn.run(app, host=a.host, port=a.porta, log_level="info")
    return 0


def _demo(a):
    from .demo import popular
    print(f"Banco de demonstração (dados sintéticos) gravado em {popular(a.banco)}")
    return 0


def registrar(sub):
    p = sub.add_parser("painel", help="sobe o painel web local (somente simulação)")
    p.add_argument("--banco", default="dados_locais/robowin.sqlite")
    p.add_argument("--dados", default=None, help="diretório de dados (onde fica o arquivo EMERGENCIA)")
    p.add_argument("--config", default="config/robowin.toml")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--porta", type=int, default=8765)
    p.set_defaults(func=_painel)
    d = sub.add_parser("painel-demo", help="cria banco com dados SINTÉTICOS de demonstração")
    d.add_argument("--banco", default="dados_locais/demo.sqlite")
    d.set_defaults(func=_demo)
