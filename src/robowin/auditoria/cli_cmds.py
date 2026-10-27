from robowin.auditoria.cadeia import verificar_diretorio


def _verificar(a):
    ok, msgs = verificar_diretorio(a.dir)
    print("\n".join(msgs) or "sem arquivos")
    return 0 if ok else 1


def registrar(sub):
    p = sub.add_parser("auditoria", help="verifica a cadeia de hash da auditoria")
    p.add_argument("--dir", default="dados_locais/auditoria")
    p.set_defaults(func=_verificar)
