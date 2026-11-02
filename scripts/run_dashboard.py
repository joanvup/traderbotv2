"""Arranca el dashboard: proceso separado del bot, escuchando solo en loopback."""

from __future__ import annotations

import argparse
import os
import sys

from goldbot.config import load_config, load_environment

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_dashboard", description="Dashboard local de goldbot")
    parser.add_argument("--config", required=True, help="ruta al YAML de configuración")
    parser.add_argument(
        "--check",
        action="store_true",
        help="construye la app, comprueba la base y sale (no abre puerto)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment()
    config = load_config(args.config)

    host = config.dashboard.host
    port = config.dashboard.port
    if host not in _LOOPBACK:
        print(f"ERROR: el dashboard solo puede escuchar en loopback, no en {host!r}.", file=sys.stderr)
        return 2

    from goldbot.dashboard.app import create_app
    from goldbot.dashboard.readmodel import DashboardReadModel
    from goldbot.engine.data_service import build_clock

    clock = build_clock(config)
    read_model = DashboardReadModel(
        config.app.db_path, clock, stale_after_s=config.dashboard.stale_after_s
    )
    reachable = read_model.db_reachable()
    if not reachable:
        print(
            f"AVISO: la base {config.app.db_path} no existe todavía; el dashboard arranca "
            "en vacío y pintará 'sin datos' hasta que el bot escriba.",
            file=sys.stderr,
        )
    if not os.getenv(config.dashboard.token_env):
        print(
            f"AVISO: falta {config.dashboard.token_env} en el entorno; los comandos de "
            "escritura (Pausa, Reanudar, Cerrar todo) quedan BLOQUEADOS con 401.",
            file=sys.stderr,
        )

    app = create_app(db_path=config.app.db_path, config=config, clock=clock)

    if args.check:
        # `app.routes` en FastAPI 0.162 devuelve envoltorios `_IncludedRouter` sin `path`,
        # así que el inventario legible de endpoints sólo lo da el esquema OpenAPI.
        paths = sorted(app.openapi()["paths"])
        print(f"app construida · base alcanzable={reachable} · endpoints={len(paths)}")
        for path in paths:
            print(f"  {path}")
        return 0

    import uvicorn

    print(f"goldbot dashboard en http://{host}:{port} (solo loopback) · base={config.app.db_path}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
