from __future__ import annotations

import argparse
import sys
from pathlib import Path

from data_agent.config import ROOT, load_env_file
from data_agent.store import RateStore
from data_agent.web import serve


DATABASE = ROOT / "data" / "exchange_rates.sqlite3"
OFFICIAL_WORKBOOK = ROOT / "data" / "tipo_cambio_bccr_2026.xlsx"


def create_store() -> RateStore:
    store = RateStore(DATABASE)
    initialize_store(store, OFFICIAL_WORKBOOK)
    return store


def initialize_store(store: RateStore, workbook: Path) -> int:
    if not workbook.is_file():
        raise FileNotFoundError(f"No se encontró el Excel oficial: {workbook}")
    count = store.import_excel(workbook, source="BCCR (Excel oficial 2026)")
    print(f"Se cargaron/actualizaron {count} observaciones del Excel oficial del BCCR.")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prototipo Data Agent USD/CRC")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve", help="Inicia el prototipo web local")
    serve_parser.add_argument("--port", type=int, default=8765, help="Puerto local del servidor web")
    import_parser = subparsers.add_parser("import", help="Importa datos desde un Excel o CSV")
    import_parser.add_argument("file", type=Path, help="Archivo .xlsx o .csv")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
    command = args.command or "serve"
    try:
        store = create_store()
        if command == "serve":
            port = getattr(args, "port", 8765)
            serve(store, port=port)
        elif command == "import":
            file = args.file
            if file.suffix.lower() == ".xlsx":
                count = store.import_excel(file, source=f"Excel importado: {file.name}")
            elif file.suffix.lower() == ".csv":
                count = store.import_csv(file, source=f"CSV importado: {file.name}")
            else:
                raise ValueError("El formato debe ser .xlsx o .csv.")
            print(f"Se importaron/actualizaron {count} observaciones desde {file}.")
        else:
            raise ValueError("Comando no reconocido.")
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
