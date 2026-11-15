import sys

from heru import api, atualizar, baixar, exportar, zips


def status(argv):
    chave = api.carregar_map_key()
    if not chave:
        sys.exit("sem MAP_KEY: crie .env com MAP_KEY=sua_chave")
    atual = api.status(chave)
    print({k: atual.get(k) for k in ("current_transactions", "transaction_limit", "transaction_interval")})
    print(api.disponibilidade(chave).to_string(index=False))


COMANDOS = {"historico": baixar.main, "zips": zips.main, "exportar": exportar.main, "atualizar": atualizar.main,
            "status": status}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMANDOS:
        sys.exit(f"uso: python -m heru {{{','.join(COMANDOS)}}} [opções]")
    COMANDOS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
