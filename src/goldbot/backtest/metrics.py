"""Métricas del backtest: expectancy, profit factor, drawdown y desglose por régimen."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from goldbot.domain.types import TradeRecord


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Resultado agregado; el informe debe incluir también el desglose por playbook."""

    trades: int
    expectancy_r: float
    profit_factor: float
    max_drawdown_usd: float
    max_drawdown_pct: float = 0.0
    r_distribution: Mapping[str, int] = field(default_factory=dict)
    by_regime: Mapping[str, float] = field(default_factory=dict)
    by_playbook: Mapping[str, float] = field(default_factory=dict)
    no_trade_days_pct: float = 0.0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_profit_usd: float = 0.0
    gross_profit_usd: float = 0.0
    gross_loss_usd: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    max_consecutive_losses: int = 0


@dataclass(frozen=True, slots=True)
class BacktestResult:
    metrics: BacktestMetrics
    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[tuple[datetime, float], ...] = ()
    decisions: int = 0
    policy_counts: Mapping[str, int] = field(default_factory=dict)
    block_counts: Mapping[str, int] = field(default_factory=dict)
    blocked_by_filter: Mapping[str, int] = field(default_factory=dict)
    regime_counts: Mapping[str, int] = field(default_factory=dict)
    playbook_counts: Mapping[str, int] = field(default_factory=dict)
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    model_version: str = ""
    # Días del tramo de prueba con al menos una vela de decisión; el denominador de `no_trade_days_pct`.
    decision_days: int = 0


_R_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<=-1", float("-inf"), -1.0),
    ("-1..0", -1.0, 0.0),
    ("0..1", 0.0, 1.0),
    ("1..2", 1.0, 2.0),
    (">=2", 2.0, float("inf")),
)


def _r_bucket(value: float) -> str:
    for label, low, high in _R_BUCKETS:
        if label == "<=-1":
            if value <= -1.0:
                return label
        elif low <= value < high:
            return label
    return ">=2"


def r_histogram(trades: Sequence[TradeRecord]) -> dict[str, int]:
    """Las R repartidas en los tramos de siempre, en orden y sin tramos vacíos.

    Es público porque el walk-forward agrupa operaciones de varios pliegues y necesita los
    mismos tramos: dos informes con etiquetas distintas no son comparables.
    """
    counts: Counter[str] = Counter()
    for trade in trades:
        if trade.r_multiple is not None:
            counts[_r_bucket(trade.r_multiple)] += 1
    return {label: counts[label] for label, _, _ in _R_BUCKETS if counts[label] > 0}


def _expectancy(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _max_drawdown(profits: Sequence[float], *, initial_balance: float) -> tuple[float, float]:
    equity = initial_balance
    peak = initial_balance
    worst_usd = 0.0
    worst_pct = 0.0
    for profit in profits:
        equity += profit
        peak = max(peak, equity)
        worst_usd = max(worst_usd, peak - equity)
        if peak > 0.0:
            worst_pct = max(worst_pct, (peak - equity) / peak * 100.0)
    return worst_usd, worst_pct


def _max_consecutive_losses(profits: Sequence[float]) -> int:
    best = current = 0
    for profit in profits:
        if profit < 0.0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def compute_metrics(
    trades: Sequence[TradeRecord],
    *,
    initial_balance: float,
    total_days: int,
) -> BacktestMetrics:
    """Calcula métricas sin ocultar resultados malos ni optimizar sobre el histórico."""
    ordered = sorted(trades, key=lambda trade: trade.close_time_utc)
    profits = [trade.net_profit for trade in ordered]
    r_values = [trade.r_multiple for trade in ordered if trade.r_multiple is not None]
    wins = [profit for profit in profits if profit > 0.0]
    losses = [profit for profit in profits if profit < 0.0]
    gross_profit = float(sum(wins))
    gross_loss = float(sum(losses))
    profit_factor = (
        gross_profit / abs(gross_loss)
        if gross_loss < 0.0
        else (float("inf") if gross_profit > 0.0 else 0.0)
    )
    win_rs = [value for value in r_values if value > 0.0]
    loss_rs = [value for value in r_values if value < 0.0]

    distribution = r_histogram(ordered)

    by_regime: dict[str, list[float]] = defaultdict(list)
    by_playbook: dict[str, list[float]] = defaultdict(list)
    for trade in ordered:
        if trade.r_multiple is None:
            continue
        if trade.regime_at_entry is not None:
            by_regime[trade.regime_at_entry.value].append(trade.r_multiple)
        by_playbook[trade.playbook_id or "desconocido"].append(trade.r_multiple)

    traded_days = {trade.close_time_utc.date() for trade in ordered}
    no_trade_days_pct = (
        max(0.0, (total_days - len(traded_days)) / total_days * 100.0) if total_days > 0 else 0.0
    )
    drawdown_usd, drawdown_pct = _max_drawdown(profits, initial_balance=initial_balance)

    return BacktestMetrics(
        trades=len(ordered),
        expectancy_r=_expectancy(r_values),
        profit_factor=profit_factor,
        max_drawdown_usd=drawdown_usd,
        max_drawdown_pct=drawdown_pct,
        r_distribution=dict(distribution),
        by_regime={key: _expectancy(values) for key, values in by_regime.items()},
        by_playbook={key: _expectancy(values) for key, values in by_playbook.items()},
        no_trade_days_pct=no_trade_days_pct,
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(profits) * 100.0 if profits else 0.0,
        net_profit_usd=float(sum(profits)),
        gross_profit_usd=gross_profit,
        gross_loss_usd=gross_loss,
        avg_win_r=_expectancy(win_rs),
        avg_loss_r=_expectancy(loss_rs),
        max_consecutive_losses=_max_consecutive_losses(profits),
    )


# ---------------------------------------------------------------------------
# Desglose: Fase 6 necesita algo más que la expectancy media por grupo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupStats:
    """Resumen de un subconjunto de operaciones (un régimen, un playbook, una celda).

    `max_drawdown_usd` se camina desde cero: dentro de un grupo no hay balance propio, y lo
    que interesa es cuánto se separó la curva de ese grupo de su propio máximo.
    """

    key: str
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    expectancy_r: float
    std_r: float
    profit_factor: float
    net_profit_usd: float
    avg_win_r: float
    avg_loss_r: float
    max_consecutive_losses: int
    max_drawdown_usd: float
    with_r: int


def _std(values: Sequence[float]) -> float:
    """Desviación típica muestral; 0.0 con menos de dos valores."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def summarize(
    trades: Sequence[TradeRecord], *, key: str = "todas", initial_balance: float = 0.0
) -> GroupStats:
    """Agrupa unas operaciones dadas en las mismas cifras para cualquier recorte."""
    ordered = sorted(trades, key=lambda trade: trade.close_time_utc)
    profits = [trade.net_profit for trade in ordered]
    r_values = [trade.r_multiple for trade in ordered if trade.r_multiple is not None]
    wins = [profit for profit in profits if profit > 0.0]
    losses = [profit for profit in profits if profit < 0.0]
    gross_profit = float(sum(wins))
    gross_loss = float(sum(losses))
    win_rs = [value for value in r_values if value > 0.0]
    loss_rs = [value for value in r_values if value < 0.0]
    drawdown_usd, _ = _max_drawdown(profits, initial_balance=initial_balance)
    return GroupStats(
        key=key,
        trades=len(ordered),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=len(wins) / len(profits) * 100.0 if profits else 0.0,
        expectancy_r=_expectancy(r_values),
        std_r=_std(r_values),
        profit_factor=(
            gross_profit / abs(gross_loss)
            if gross_loss < 0.0
            else (float("inf") if gross_profit > 0.0 else 0.0)
        ),
        net_profit_usd=float(sum(profits)),
        avg_win_r=_expectancy(win_rs),
        avg_loss_r=_expectancy(loss_rs),
        max_consecutive_losses=_max_consecutive_losses(profits),
        max_drawdown_usd=drawdown_usd,
        with_r=len(r_values),
    )


def _t_statistic(stats: GroupStats) -> float:
    """t de la expectancy sobre cero: con n corto y varianza alta no vale nada."""
    if not has_dispersion(stats):
        return 0.0
    return stats.expectancy_r / (stats.std_r / math.sqrt(stats.with_r))


def has_dispersion(stats: GroupStats) -> bool:
    """Si las R salen todas del mismo stop, la desviación típica es ~0 y el t se dispara.

    Nueve pérdidas a -1 R darían un |t| de miles: no es una señal, es un artefacto de una
    muestra sin varianza. El informe lo pinta como `n/a` en lugar de fingir que demuestra algo.
    """
    return stats.with_r >= 2 and stats.std_r >= 0.05


def t_statistic(stats: GroupStats) -> float:
    """Acceso público al estadístico t de un grupo (el informe lo imprime con n)."""
    return _t_statistic(stats)


def breakdown(
    trades: Sequence[TradeRecord],
    *,
    extract: Callable[[TradeRecord], str | None],
    fallback: str = "sin etiqueta",
) -> dict[str, GroupStats]:
    """Un `GroupStats` por clave, ordenado de más a menos operaciones."""
    buckets: dict[str, list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        buckets[extract(trade) or fallback].append(trade)
    groups = {name: summarize(items, key=name) for name, items in buckets.items()}
    return dict(sorted(groups.items(), key=lambda item: (-item[1].trades, item[0])))


def regime_key(trade: TradeRecord) -> str | None:
    return trade.regime_at_entry.value if trade.regime_at_entry is not None else None


def playbook_key(trade: TradeRecord) -> str | None:
    return trade.playbook_id or None


def by_regime(trades: Sequence[TradeRecord]) -> dict[str, GroupStats]:
    return breakdown(trades, extract=regime_key, fallback="sin régimen")


def by_playbook(trades: Sequence[TradeRecord]) -> dict[str, GroupStats]:
    return breakdown(trades, extract=playbook_key, fallback="sin playbook")


def regime_playbook_grid(
    trades: Sequence[TradeRecord],
) -> dict[tuple[str, str], GroupStats]:
    """Cada celda régimen × playbook, con su n: la cuadrícula es el informe de la Fase 6."""
    buckets: dict[tuple[str, str], list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        cell = (
            regime_key(trade) or "sin régimen",
            playbook_key(trade) or "sin playbook",
        )
        buckets[cell].append(trade)
    cells = {name: summarize(items, key=f"{name[0]} × {name[1]}") for name, items in buckets.items()}
    return dict(sorted(cells.items(), key=lambda item: (-item[1].trades, item[0])))
