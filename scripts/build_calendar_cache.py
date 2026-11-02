"""Congela la caché reproducible del calendario (CSV del puente MQL5) para backtest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from goldbot.infra.calendar import (
    default_bridge_path,
    default_history_path,
    write_calendar_snapshot,
)

DEFAULT_TARGET = Path("data/cache/calendar_snapshot.csv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_calendar_cache",
        description="Caché del calendario económico para backtest",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        metavar="CSV",
        help="CSV del puente; se puede repetir. Por defecto, el en vivo y el histórico",
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help=f"ruta de la caché (por defecto, {DEFAULT_TARGET})",
    )
    args = parser.parse_args(argv)

    if args.source:
        sources = [Path(item) for item in args.source]
    else:
        sources = [default_bridge_path(), default_history_path()]
    missing = [path for path in sources if not path.exists()]
    for path in missing:
        print(f"AVISO: no existe {path}; se omite.", file=sys.stderr)
    presentes = [path for path in sources if path.exists()]
    if not presentes:
        print(
            "ERROR: ningún CSV del puente existe; abre MetaTrader 5 con el EA "
            "GoldbotCalendarExport adjunto.",
            file=sys.stderr,
        )
        return 2

    target = Path(args.target)
    total, start, end = write_calendar_snapshot(target, presentes)
    if start is None or end is None:
        print(f"ERROR: {target} escrita vacía; los CSV fuente no traen eventos.", file=sys.stderr)
        return 1
    dias = (end - start).total_seconds() / 86400.0
    print(
        f"Caché: {target} · {total} eventos · cobertura "
        f"{start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC"
    )
    print(f"Días cubiertos: {dias:.0f} · fuentes: " + ", ".join(str(path) for path in presentes))
    if dias < 180:
        print(
            "AVISO: la cobertura del calendario sigue siendo corta para un walk-forward de 2 "
            "años; los pliegues fuera de ella quedan DEGRADED.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
