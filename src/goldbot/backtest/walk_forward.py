"""Validación walk-forward: entrenar en A, probar en B y desplazar la ventana.

La regla que justifica esta fase: prohibido optimizar sobre todo el histórico y vender eso
como resultado. Cada pliegue corta el histórico en dos mitades —el modelo sólo ve la
primera— y el tramo de test de un pliegue nunca pisa el de otro. Al final se agrupan TODAS
las operaciones de todos los pliegues para el desglose por régimen y playbook, porque la
muestra de un solo mes no sostiene ninguna conclusión.

Dos decisiones que conviene tener presentes al leer el informe:

- El motor recibe el histórico COMPLETO en cada pliegue. Las etiquetas de régimen salen de
  una histéresis que arranca en la primera vela, así que recortar el feed cambiaría las
  etiquetas del principio de cada ventana de test. Lo que avanza es `train_end_utc`, no los
  datos.
- "Ventana de entrenamiento" tiene dos sentidos que aquí se informan por separado: los días
  de corte del pliegue (`backtest.train_window_days`) y las velas que de verdad consume el
  detector (`regime.train_window_bars` más colchón), que es lo mismo que consume en vivo.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from goldbot.backtest.engine import BacktestConfig, BacktestEngine
from goldbot.backtest.metrics import (
    BacktestResult,
    GroupStats,
    by_playbook,
    by_regime,
    r_histogram,
    regime_playbook_grid,
    summarize,
    t_statistic,
)
from goldbot.config import AppConfig
from goldbot.domain.types import Candle, Timeframe, TradeRecord

EngineRunner = Callable[["FoldWindow"], BacktestResult]


@dataclass(frozen=True, slots=True)
class FoldWindow:
    """Un pliegue: el corte entre lo que el modelo puede ver y lo que se evalúa."""

    index: int
    train_start_utc: datetime
    train_end_utc: datetime
    test_start_utc: datetime
    test_end_utc: datetime
    train_bars: int = 0
    test_bars: int = 0

    @property
    def label(self) -> str:
        return f"pliegue {self.index + 1}"


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """Resultado de un pliegue, incluido el caso en que el motor se negó a correrlo."""

    window: FoldWindow
    result: BacktestResult | None = None
    error: str | None = None
    calendar_coverage_pct: float = 0.0

    @property
    def ran(self) -> bool:
        return self.result is not None

    @property
    def trades(self) -> tuple[TradeRecord, ...]:
        return self.result.trades if self.result is not None else ()


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """El informe de la Fase 6: pliegues, agregado agrupado y estabilidad entre pliegues."""

    folds: tuple[WalkForwardFold, ...]
    aggregate: GroupStats
    by_regime: Mapping[str, GroupStats] = field(default_factory=dict)
    by_playbook: Mapping[str, GroupStats] = field(default_factory=dict)
    grid: Mapping[tuple[str, str], GroupStats] = field(default_factory=dict)
    fold_expectancy_r: tuple[float, ...] = ()
    fold_trades: tuple[int, ...] = ()
    profitable_folds: int = 0
    tested_days: int = 0
    uncovered_days: int = 0
    discarded_tail_days: int = 0
    initial_balance: float = 0.0
    model_versions: tuple[str, ...] = ()
    news_filter_active: bool = True
    min_cell_trades: int = 10
    train_window_days: int = 0
    test_window_days: int = 0
    step_days: int = 0

    @property
    def ran_folds(self) -> tuple[WalkForwardFold, ...]:
        return tuple(fold for fold in self.folds if fold.ran)

    @property
    def failed_folds(self) -> tuple[WalkForwardFold, ...]:
        return tuple(fold for fold in self.folds if not fold.ran)

    @property
    def expectancy_median_r(self) -> float:
        return median(self.fold_expectancy_r) if self.fold_expectancy_r else 0.0

    @property
    def expectancy_t(self) -> float:
        """t de la expectancy agrupada: por debajo de ~2 no hay ventaja demostrada."""
        return t_statistic(self.aggregate)

    @property
    def all_trades(self) -> tuple[TradeRecord, ...]:
        return tuple(trade for fold in self.folds for trade in fold.trades)

    @property
    def history_start_utc(self) -> datetime | None:
        """Primera vela que cualquiera de los pliegues usa como entrenamiento."""
        return min((fold.window.train_start_utc for fold in self.folds), default=None)

    @property
    def history_end_utc(self) -> datetime | None:
        return max((fold.window.test_end_utc for fold in self.folds), default=None)

    @property
    def min_fold_trades(self) -> int:
        return min(self.fold_trades, default=0)

    @property
    def max_fold_trades(self) -> int:
        return max(self.fold_trades, default=0)

    @property
    def calendar_coverage_pct(self) -> float:
        """Media de la cobertura del calendario sobre los pliegues corridos."""
        covers = [fold.calendar_coverage_pct for fold in self.ran_folds]
        return sum(covers) / len(covers) if covers else 0.0

    @property
    def no_trade_days_pct(self) -> float:
        """De los días con vela de decisión, en cuántos no se abrió ninguna posición.

        Operar cero es un resultado válido, así que esto se informa y no se esconde. Los tramos
        de test no se solapan: la media ponderada por `decision_days` de cada pliegue sale
        exactamente (ΣD − ΣT) / ΣD, que es el porcentaje agrupado.
        """
        ran = [fold.result for fold in self.ran_folds if fold.result is not None]
        days = sum(result.decision_days for result in ran)
        if days <= 0:
            return 0.0
        weighted = sum(result.decision_days * result.metrics.no_trade_days_pct for result in ran)
        return weighted / days

    @property
    def r_distribution(self) -> dict[str, int]:
        """Las R de todos los pliegues juntas, en los mismos tramos que usa el corte único."""
        return r_histogram(self.all_trades)


class WalkForwardRunner:
    """Nunca optimiza sobre todo el histórico: cada tramo prueba datos no vistos."""

    def __init__(
        self,
        *,
        train_window_days: int,
        test_window_days: int,
        step_days: int,
        min_cell_trades: int | None = None,
    ) -> None:
        if test_window_days > train_window_days:
            raise ValueError("la ventana de test no puede superar la de entrenamiento")
        if min(step_days, train_window_days, test_window_days) < 1:
            raise ValueError("las ventanas del walk-forward deben ser positivas")
        if min_cell_trades is not None and min_cell_trades < 1:
            raise ValueError("min_cell_trades debe ser positivo")
        self._train_window_days = train_window_days
        self._test_window_days = test_window_days
        self._step_days = step_days
        # Sin override explícito manda el YAML: `backtest.min_cell_trades`.
        self._min_cell_trades = min_cell_trades

    # -- geometría ------------------------------------------------------------

    def plan(self, bars: Sequence[Candle]) -> tuple[list[FoldWindow], int]:
        """Corta el histórico en pliegues y devuelve (ventanas, días de cola descartados).

        Se ancla en la primera vela y avanza de `step_days` en `step_days`. Sólo se conservan
        los pliegues cuyo tramo de test está **completo** dentro del histórico: un tramo
        recortado sesga hacia abajo el número de operaciones y no es comparable.
        """
        if not bars:
            raise ValueError("no hay velas para planificar el walk-forward")
        first = bars[0].open_time_utc
        last = bars[-1].open_time_utc
        available_days = (last.date() - first.date()).days + 1
        if available_days < self._train_window_days + self._test_window_days:
            return [], available_days - (self._train_window_days + self._test_window_days)

        opens = [bar.open_time_utc for bar in bars]
        windows: list[FoldWindow] = []
        start = first
        while True:
            train_end = start + timedelta(days=self._train_window_days)
            test_end = train_end + timedelta(days=self._test_window_days)
            if test_end > last:
                break
            windows.append(
                FoldWindow(
                    index=len(windows),
                    train_start_utc=start,
                    train_end_utc=train_end,
                    test_start_utc=train_end,
                    test_end_utc=test_end,
                    train_bars=_count(opens, start, train_end),
                    test_bars=_count(opens, train_end, test_end),
                )
            )
            start += timedelta(days=self._step_days)

        discarded = max(0, (last.date() - windows[-1].test_end_utc.date()).days)
        return windows, discarded

    # -- ejecución ------------------------------------------------------------

    def run(
        self,
        *,
        candles: Mapping[Timeframe, Sequence[Candle]],
        config: AppConfig,
        calendar: Any | None = None,
        engine_runner: EngineRunner | None = None,
    ) -> WalkForwardReport:
        """Corre cada pliegue con su propio motor y agrupa sin mezclar train con test."""
        decision_timeframe = Timeframe(config.data.decision_timeframe)
        bars = list(candles.get(decision_timeframe, ()))
        windows, discarded = self.plan(bars)
        if not windows:
            raise ValueError(
                f"el histórico ({len(bars)} velas) no alcanza un pliegue de "
                f"{self._train_window_days}+{self._test_window_days} días"
            )

        runner = engine_runner or self._engine_runner(candles, config, calendar)
        folds: list[WalkForwardFold] = []
        for window in windows:
            try:
                result = runner(window)
            except ValueError as error:
                # Un pliegue que el motor rechaza (sin velas de test, sin filas de
                # entrenamiento) se informa como tal: no se descarta en silencio.
                folds.append(WalkForwardFold(window=window, error=str(error)))
                continue
            folds.append(
                WalkForwardFold(
                    window=window,
                    result=result,
                    calendar_coverage_pct=_coverage(calendar, window),
                )
            )
        return _build_report(
            folds,
            config=config,
            discarded_tail_days=max(0, discarded),
            min_cell_trades=(
                config.backtest.min_cell_trades
                if self._min_cell_trades is None
                else self._min_cell_trades
            ),
            news_filter_active=calendar is not None,
            train_window_days=self._train_window_days,
            test_window_days=self._test_window_days,
            step_days=self._step_days,
        )

    def _engine_runner(
        self,
        candles: Mapping[Timeframe, Sequence[Candle]],
        config: AppConfig,
        calendar: Any | None,
    ) -> EngineRunner:
        """Un `BacktestEngine` nuevo por pliegue: el libro diario y el reloj no se heredan."""

        def run_window(window: FoldWindow) -> BacktestResult:
            engine = BacktestEngine(
                config=config,
                candles=candles,
                calendar=calendar,
                train_end_utc=window.train_end_utc,
                backtest=BacktestConfig.from_app(config),
                start_utc=window.test_start_utc,
                end_utc=window.test_end_utc,
            )
            return engine.run()

        return run_window


def _count(opens: Sequence[datetime], start: datetime, end: datetime) -> int:
    """Velas con `start <= open_time < end`, asumiendo el orden de la caché."""
    return bisect_left(opens, end) - bisect_left(opens, start)


def _coverage(calendar: Any | None, window: FoldWindow) -> float:
    """Porcentaje del tramo de test cubierto por el calendario económico local.

    No es un adorno: con `filters.calendar_unavailable_policy: block`, fuera de cobertura el
    filtro de noticias bloquea por ausencia de datos, así que un pliegue al 0 % de cobertura
    no mide la estrategia, mide la falta de calendario.
    """
    if calendar is None:
        return 0.0
    coverage_start = getattr(calendar, "coverage_start_utc", None)
    coverage_end = getattr(calendar, "coverage_end_utc", None)
    if coverage_start is None or coverage_end is None:
        return 0.0
    total = (window.test_end_utc - window.test_start_utc).total_seconds()
    if total <= 0:
        return 0.0
    overlap = (
        min(window.test_end_utc, coverage_end) - max(window.test_start_utc, coverage_start)
    ).total_seconds()
    return max(0.0, min(100.0, overlap / total * 100.0))


def _build_report(
    folds: Sequence[WalkForwardFold],
    *,
    config: AppConfig,
    discarded_tail_days: int,
    min_cell_trades: int,
    news_filter_active: bool,
    train_window_days: int,
    test_window_days: int,
    step_days: int,
) -> WalkForwardReport:
    initial_balance = config.backtest.initial_balance
    pooled = tuple(trade for fold in folds for trade in fold.trades)
    ran = [fold.result for fold in folds if fold.result is not None]
    tested = _tested_days(folds)
    span = _span_days(folds)
    return WalkForwardReport(
        folds=tuple(folds),
        aggregate=summarize(pooled, key="pooled", initial_balance=initial_balance),
        by_regime=by_regime(pooled),
        by_playbook=by_playbook(pooled),
        grid=regime_playbook_grid(pooled),
        fold_expectancy_r=tuple(float(result.metrics.expectancy_r) for result in ran),
        fold_trades=tuple(int(result.metrics.trades) for result in ran),
        profitable_folds=sum(1 for result in ran if result.metrics.net_profit_usd > 0.0),
        tested_days=tested,
        uncovered_days=max(0, span - tested),
        discarded_tail_days=discarded_tail_days,
        initial_balance=initial_balance,
        model_versions=tuple(sorted({result.model_version for result in ran})),
        news_filter_active=news_filter_active,
        min_cell_trades=min_cell_trades,
        train_window_days=train_window_days,
        test_window_days=test_window_days,
        step_days=step_days,
    )


def _tested_days(folds: Sequence[WalkForwardFold]) -> int:
    """Días naturales dentro de algún tramo de test efectivamente corrido."""
    days: set[date] = set()
    for fold in folds:
        result = fold.result
        if result is None or result.start_utc is None or result.end_utc is None:
            continue
        current = result.start_utc.date()
        last = result.end_utc.date()
        while current <= last:
            days.add(current)
            current += timedelta(days=1)
    return len(days)


def _span_days(folds: Sequence[WalkForwardFold]) -> int:
    ran = [fold for fold in folds if fold.result is not None]
    if not ran:
        return 0
    return max(0, (ran[-1].window.test_end_utc - ran[0].window.test_start_utc).days)
