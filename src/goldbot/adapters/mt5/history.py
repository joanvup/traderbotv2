"""Descarga de velas cerradas y conciliación de deals históricos."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from goldbot.domain.interfaces import Clock
from goldbot.domain.types import Candle, DealRecord, Timeframe

_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.M5: 5 * 60,
    Timeframe.M15: 15 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
}


def _tf_constant(mt5: Any, timeframe: Timeframe) -> int:
    return int(getattr(mt5, f"TIMEFRAME_{timeframe.value}"))


def _rows_to_candles(
    rows: Any,
    timeframe: Timeframe,
    clock: Clock,
    *,
    as_of_utc: datetime,
    received_at_utc: datetime,
    closed_only: bool,
) -> tuple[Candle, ...]:
    duration = timedelta(seconds=_TF_SECONDS[timeframe])
    candles: dict[datetime, Candle] = {}
    for row in rows:
        open_wall = datetime.fromtimestamp(int(row["time"]), UTC).replace(tzinfo=None)
        open_utc = clock.to_utc(open_wall)
        close_utc = open_utc + duration
        is_closed = close_utc <= as_of_utc
        if closed_only and not is_closed:
            continue
        if open_utc > as_of_utc:
            continue
        candles[open_utc] = Candle(
            open_time_utc=open_utc,
            close_time_utc=close_utc,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            tick_volume=int(row["tick_volume"]),
            spread_points=float(row["spread"]),
            is_closed=is_closed,
            received_at_utc=received_at_utc,
            source="MT5",
        )
    return tuple(candles[key] for key in sorted(candles))


def fetch_candles(
    mt5: Any,
    symbol: str,
    timeframe: Timeframe,
    count: int,
    clock: Clock,
    *,
    closed_only: bool = True,
    as_of_utc: datetime | None = None,
) -> tuple[Candle, ...]:
    """Descarta siempre la vela en formación y cualquier dato posterior a `as_of_utc`."""
    if count <= 0:
        raise ValueError("count debe ser positivo")
    # Se pide una vela extra: la última suele estar en formación.
    raw = mt5.copy_rates_from_pos(symbol, _tf_constant(mt5, timeframe), 0, count + 1)
    if raw is None:
        raise RuntimeError(f"copy_rates_from_pos falló para {symbol} {timeframe}: {mt5.last_error()}")
    now_utc = as_of_utc or clock.now_utc()
    candles = _rows_to_candles(
        raw,
        timeframe,
        clock,
        as_of_utc=now_utc,
        received_at_utc=clock.now_utc(),
        closed_only=closed_only,
    )
    # La petición incluye una vela extra para descartar la formación: se recorta al contrato.
    return candles[-count:]


def fetch_candles_range(
    mt5: Any,
    symbol: str,
    timeframe: Timeframe,
    start_utc: datetime,
    end_utc: datetime,
    clock: Clock,
    *,
    closed_only: bool = True,
) -> tuple[Candle, ...]:
    """Velas cerradas en un rango UTC (para la sincronización histórica)."""
    raw = mt5.copy_rates_range(
        symbol,
        _tf_constant(mt5, timeframe),
        clock.to_server(start_utc),
        clock.to_server(end_utc),
    )
    if raw is None:
        raise RuntimeError(f"copy_rates_range falló para {symbol} {timeframe}: {mt5.last_error()}")
    return _rows_to_candles(
        raw,
        timeframe,
        clock,
        as_of_utc=end_utc,
        received_at_utc=clock.now_utc(),
        closed_only=closed_only,
    )


def fetch_deals(
    mt5: Any,
    *,
    since_utc: datetime,
    until_utc: datetime,
    position_id: int | None = None,
) -> tuple[DealRecord, ...]:
    """Reconciliación incremental por position_id, incluyendo comisión y swap."""
    raise NotImplementedError("historial de deals disponible en Fase 4")
