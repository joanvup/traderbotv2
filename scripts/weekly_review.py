"""Revisión semanal de la operación en DEMO (Fase 7): agrega el StateStore y audita los candados.

Lee la base SQLite **en modo solo lectura**, así que puede ejecutarse con el bot funcionando sin
interferir con él.

Uso típico:
    python scripts/weekly_review.py --config config/config.example.yaml
    python scripts/weekly_review.py --config config/config.example.yaml --days 14
    python scripts/weekly_review.py --config config/config.example.yaml --json reports/semana.json
    python scripts/weekly_review.py --config config/config.example.yaml --until 2026-07-15T12:00:00Z

Códigos de salida: 0 sin fallos duros, 2 con al menos un FALLO de auditoría, 3 si no hay base
que revisar y 4 si los argumentos no son válidos. Distintos de cero permiten automatizar la
revisión (task scheduled) sin leer texto.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from goldbot.config import load_config, load_environment
from goldbot.review.report import WeeklyReportBuilder, weekly_review_to_jsonable
from goldbot.review.weekly import WeeklyReviewCollector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weekly_review",
        description="Revisión semanal de la operación en DEMO",
    )
    parser.add_argument("--config", required=True, help="ruta al YAML de configuración")
    parser.add_argument(
        "--days", type=int, default=7, help="días hacia atrás desde ahora (por defecto, 7)"
    )
    parser.add_argument(
        "--db", default=None, help="ruta al StateStore (por defecto, app.db_path del YAML)"
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="ruta donde escribir el informe en JSON además de imprimir el texto",
    )
    parser.add_argument(
        "--until",
        dest="until_utc",
        default=None,
        help=(
            "fin de la ventana en ISO UTC (por defecto, ahora); sirve para repasar una semana "
            "ya cerrada, p. ej. 2026-07-15T12:00:00Z"
        ),
    )
    parser.add_argument(
        "--title",
        default=None,
        help="cabecera del informe (por defecto incluye la longitud de la ventana)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    load_environment()
    config = load_config(args.config)

    if args.days < 1:
        print("ERROR: --days ha de ser 1 o mayor.", file=sys.stderr)
        return 4

    now_utc = datetime.now(UTC)
    if args.until_utc:
        try:
            parsed = datetime.fromisoformat(args.until_utc.replace("Z", "+00:00"))
        except ValueError:
            print(
                f"ERROR: --until no es una marca ISO UTC legible: {args.until_utc!r}",
                file=sys.stderr,
            )
            return 4
        now_utc = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    db_path = Path(args.db or config.app.db_path)
    if not db_path.exists():
        print(
            f"ERROR: no existe el StateStore {db_path}. El bot aún no escribió estado; "
            "la revisión semanal necesita al menos un arranque con conexión a MT5.",
            file=sys.stderr,
        )
        return 3

    collector = WeeklyReviewCollector(
        config, db_path=db_path, window_days=args.days, now_utc=now_utc
    )
    review = collector.collect()

    title = args.title
    if title is None:
        title = (
            "REVISIÓN SEMANAL — operación en DEMO"
            if args.days == 7
            else f"REVISIÓN DE {args.days} DÍAS — operación en DEMO"
        )
    print(WeeklyReportBuilder().build(review, title=title))

    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(weekly_review_to_jsonable(review), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON escrito en {out}")

    failed = review.failed_audits
    if failed:
        print(
            f"\n{len(failed)} auditoría(s) en FALLO: "
            + ", ".join(finding.code for finding in failed),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
