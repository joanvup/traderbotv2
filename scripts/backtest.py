"""Backtest de playbooks sobre la caché local de velas.

Uso típico:
    python scripts/backtest.py --config config/config.example.yaml
    python scripts/backtest.py --config config/config.example.yaml --method rules
    python scripts/backtest.py --config config/config.example.yaml --walk-forward
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldbot.backtest.engine import BacktestConfig, BacktestEngine
from goldbot.backtest.report import (
    ReportBuilder,
    WalkForwardReportBuilder,
    result_to_jsonable,
    walk_forward_to_jsonable,
)
from goldbot.backtest.walk_forward import WalkForwardRunner
from goldbot.config import AppConfig, load_config
from goldbot.domain.decision_log import JsonlDecisionLogger
from goldbot.domain.types import Timeframe
from goldbot.infra.calendar import StaticEconomicCalendar, default_bridge_path, default_history_path
from goldbot.infra.candle_cache import cache_path, load_candles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backtest",
        description="Backtest de playbooks (Fase 3) y validación walk-forward (Fase 6)",
    )
    parser.add_argument("--config", required=True, help="ruta al YAML de configuración")
    parser.add_argument("--symbol", default=None, help="símbolo de la caché (por defecto, el de la config)")
    parser.add_argument("--cache-dir", default="data/cache", help="directorio de la caché CSV.gz")
    parser.add_argument(
        "--calendar",
        nargs="+",
        default=None,
        metavar="CSV",
        help="uno o más CSV del puente de calendario (se unen por ocurrencia); 'none' lo "
        "deja sin fuente y con la política 'block' bloquea toda candidata",
    )
    parser.add_argument(
        "--train-bars",
        type=int,
        default=8_000,
        help="velas de decisión usadas para entrenar el régimen",
    )
    parser.add_argument("--start", default=None, help="inicio ISO del tramo de prueba")
    parser.add_argument("--end", default=None, help="fin ISO del tramo de prueba")
    parser.add_argument(
        "--method",
        choices=("rules", "hmm", "both"),
        default=None,
        help="fuerza el detector de régimen para comparar métodos",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Fase 6: entrenar y probar por pliegues en vez de un único corte",
    )
    parser.add_argument("--train-days", type=int, default=None, help="días de la ventana de entrenamiento")
    parser.add_argument("--test-days", type=int, default=None, help="días de la ventana de prueba")
    parser.add_argument("--step-days", type=int, default=None, help="desplazamiento entre pliegues")
    parser.add_argument("--jsonl", default=None, help="ruta opcional del DecisionLog JSONL")
    parser.add_argument(
        "--report",
        default=None,
        help="ruta del informe JSON (por defecto, reports/backtest_phase3.json "
        "o reports/walk_forward.json con --walk-forward)",
    )
    return parser


def _instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _load_market(config: AppConfig, cache_dir: str, symbol: str) -> dict[Timeframe, tuple[Any, ...]]:
    timeframes = tuple(
        dict.fromkeys(
            Timeframe(name)
            for name in (
                config.data.decision_timeframe,
                *config.data.entry_timeframes,
                *config.data.context_timeframes,
            )
        )
    )
    market: dict[Timeframe, tuple[Any, ...]] = {}
    for timeframe in timeframes:
        path = cache_path(cache_dir, symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(f"falta la caché {path}; ejecuta scripts/sync_history.py")
        market[timeframe] = load_candles(path)
    return market


def _calendar(paths_argument: list[str] | None) -> StaticEconomicCalendar | None:
    if paths_argument and paths_argument[0] == "none":
        return None
    if paths_argument:
        paths = [Path(item) for item in paths_argument]
    else:
        paths = [default_bridge_path(), default_history_path()]
    presentes = [path for path in paths if path.exists()]
    if not presentes:
        print(
            f"AVISO: calendario no encontrado en "
            f"{', '.join(str(path) for path in paths)}; el filtro de noticias bloqueará.",
            file=sys.stderr,
        )
        return None
    return StaticEconomicCalendar.from_csv(presentes)


def _walk_forward(
    args: argparse.Namespace,
    config: AppConfig,
    market: dict[Timeframe, tuple[Any, ...]],
    calendar: StaticEconomicCalendar | None,
    decision_timeframe: Timeframe,
) -> int:
    """Fase 6: pliegues sucesivos, cada uno entrenado antes de su propio tramo de prueba."""
    if args.start or args.end or args.jsonl:
        print(
            "AVISO: con --walk-forward se ignoran --start/--end/--jsonl: los tramos los fijan "
            "los pliegues.",
            file=sys.stderr,
        )
    train_days = args.train_days or config.backtest.train_window_days
    test_days = args.test_days or config.backtest.test_window_days
    step_days = args.step_days or config.backtest.step_days
    if calendar is None and config.filters.calendar_unavailable_policy == "block":
        print(
            "AVISO: sin calendario y calendar_unavailable_policy=block el filtro de noticias "
            "rechaza toda candidata: este estudio no medirá la estrategia, solo la ausencia de "
            "datos. Pasa --calendar <csv> con cobertura suficiente para el tramo de prueba.",
            file=sys.stderr,
        )
    runner = WalkForwardRunner(
        train_window_days=train_days,
        test_window_days=test_days,
        step_days=step_days,
    )
    bars = market[decision_timeframe]
    windows, discarded = runner.plan(bars)
    print(
        f"Caché {decision_timeframe.value}: {len(bars)} velas "
        f"({bars[0].open_time_utc:%Y-%m-%d} -> {bars[-1].open_time_utc:%Y-%m-%d})"
    )
    if not windows:
        print(
            f"ERROR: no alcanza ni un pliegue de {train_days}+{test_days} días "
            f"({discarded} días de margen). Descarga más histórico con scripts/sync_history.py.",
            file=sys.stderr,
        )
        return 2
    print(f"Pliegues planificados: {len(windows)} · cola sin evaluar {max(0, discarded)} días")
    for window in windows:
        print(
            f"  {window.label}: entrena hasta {window.train_end_utc:%Y-%m-%d}, prueba "
            f"{window.test_start_utc:%Y-%m-%d} -> {window.test_end_utc:%Y-%m-%d} "
            f"({window.test_bars} velas)"
        )

    def run_window(window: Any) -> Any:
        started = time.monotonic()
        engine = BacktestEngine(
            config=config,
            candles=market,
            calendar=calendar,
            train_end_utc=window.train_end_utc,
            backtest=BacktestConfig.from_app(config),
            start_utc=window.test_start_utc,
            end_utc=window.test_end_utc,
        )
        result = engine.run()
        print(
            f"  {window.label}: {result.metrics.trades} operaciones en "
            f"{time.monotonic() - started:.0f}s",
            file=sys.stderr,
        )
        return result

    report = runner.run(candles=market, config=config, calendar=calendar, engine_runner=run_window)
    print()
    print(WalkForwardReportBuilder().build(report))
    target = Path(args.report or "reports/walk_forward.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(walk_forward_to_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Informe JSON: {target}")
    if report.aggregate.trades < 20:
        print(
            "AVISO: menos de 20 operaciones agrupadas entre todos los pliegues; no hay base "
            "estadística para concluir ventaja.",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.method is not None:
        config = config.model_copy(
            update={"regime": config.regime.model_copy(update={"method": args.method})}
        )
    symbol = args.symbol or config.mt5.symbol
    decision_timeframe = Timeframe(config.data.decision_timeframe)

    market = _load_market(config, args.cache_dir, symbol)
    bars = market[decision_timeframe]
    calendar = _calendar(args.calendar)

    if args.walk_forward:
        return _walk_forward(args, config, market, calendar, decision_timeframe)

    if args.train_bars >= len(bars):
        print(
            f"ERROR: --train-bars {args.train_bars} ≥ velas disponibles ({len(bars)})",
            file=sys.stderr,
        )
        return 2
    train_end = bars[args.train_bars - 1].close_time_utc

    start = _instant(args.start)
    end = _instant(args.end)
    if calendar is not None:
        coverage_start = calendar.coverage_start_utc
        coverage_end = calendar.coverage_end_utc
        print(
            f"Calendario: {len(calendar.events)} eventos · cobertura {coverage_start} -> {coverage_end}"
        )
        if start is None and coverage_start is not None and coverage_start > train_end:
            start = coverage_start
        if end is None and coverage_end is not None and coverage_end > train_end:
            end = coverage_end

    logger = JsonlDecisionLogger(args.jsonl) if args.jsonl else None
    engine = BacktestEngine(
        config=config,
        candles=market,
        calendar=calendar,
        train_end_utc=train_end,
        backtest=BacktestConfig.from_app(config),
        start_utc=start,
        end_utc=end,
        decision_logger=logger,
    )
    try:
        result = engine.run()
    finally:
        if logger is not None:
            logger.close()

    report = ReportBuilder().build(result)
    print(report)
    target = Path(args.report or "reports/backtest_phase3.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result_to_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Informe JSON: {target}")
    if result.metrics.trades < 20:
        print(
            "AVISO: menos de 20 operaciones; no hay base estadística para concluir ventaja.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
