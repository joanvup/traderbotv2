"""Descarga el histórico de velas cerradas a la caché local (Fase 1)."""

from __future__ import annotations

import argparse
import logging

from goldbot.config import load_config, load_environment
from goldbot.domain.types import Timeframe
from goldbot.engine.data_service import build_clock, build_data_service
from goldbot.infra.logging_setup import attach_state_store, configure_logging
from goldbot.infra.state_store import SqliteStateStore

LOGGER = logging.getLogger("goldbot.sync_history")
_TIMEFRAMES = [timeframe.value for timeframe in Timeframe]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sync_history", description="Sincroniza histórico MT5")
    parser.add_argument("--config", required=True, help="ruta al YAML de configuración")
    parser.add_argument(
        "--timeframe",
        action="append",
        choices=_TIMEFRAMES,
        help="timeframe a descargar (repetible; por defecto, todos los de la config)",
    )
    parser.add_argument("--count", type=int, default=None, help="velas a pedir por timeframe")
    parser.add_argument("--level", default="INFO", help="nivel de logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment()
    config = load_config(args.config)
    configure_logging(config.app.log_dir, level=args.level, console=True)

    clock = build_clock(config)
    store = SqliteStateStore(config.app.db_path, clock)
    attach_state_store(store)
    service = build_data_service(config, clock=clock, store=store, enable_analysis=False)

    timeframes = tuple(Timeframe(name) for name in args.timeframe) if args.timeframe else None
    if args.count is not None:
        counts = {tf: args.count for tf in (timeframes or service.timeframes)}
    else:
        counts = None

    try:
        report = service.startup(download_history=False)
        print(f"Símbolo resuelto: {report.resolved_symbol} · cuenta {report.account_login}")
        results = service.download_history(timeframes, counts=counts)
        for name, count in results.items():
            print(f"  {name}: {count} velas cerradas")
    except KeyboardInterrupt:
        LOGGER.info("interrumpido por el operador")
    except Exception:
        LOGGER.exception("la sincronización falló")
        return 2
    finally:
        service.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
