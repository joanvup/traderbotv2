"""Generadores deterministas de velas sintéticas para los tests de la Fase 2."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from goldbot.domain.types import Candle, Timeframe

DELTAS: dict[Timeframe, timedelta] = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


def make_candles(
    timeframe: Timeframe,
    count: int,
    *,
    end_utc: datetime,
    start: float = 2400.0,
    drift: float = 0.0,
    wave: float = 0.0,
    period: float = 40.0,
    noise: float = 0.4,
    seed: int = 7,
    spread_points: float = 15.0,
) -> tuple[Candle, ...]:
    """Serie OHLC causal: la última vela cierra justo antes de `end_utc`."""
    rng = np.random.default_rng(seed)
    delta = DELTAS[timeframe]
    last_close = end_utc - delta
    increments = (
        drift
        + wave * np.sin(2.0 * np.pi * np.arange(count) / period)
        + rng.normal(0.0, noise, count)
    )
    closes = start + np.cumsum(increments)
    opens = np.concatenate(([start], closes[:-1]))
    highs = np.maximum(opens, closes) + 0.2 + np.abs(rng.normal(0.0, 0.2, count))
    lows = np.minimum(opens, closes) - 0.2 - np.abs(rng.normal(0.0, 0.2, count))

    candles: list[Candle] = []
    for index in range(count):
        close_time = last_close - delta * (count - 1 - index)
        open_time = close_time - delta
        candles.append(
            Candle(
                open_time_utc=open_time,
                close_time_utc=close_time,
                open=float(opens[index]),
                high=float(highs[index]),
                low=float(lows[index]),
                close=float(closes[index]),
                tick_volume=100,
                spread_points=spread_points,
                is_closed=True,
                received_at_utc=close_time,
                source="TEST",
            )
        )
    return tuple(candles)


def market_candles(
    *,
    end_utc: datetime,
    m5_bars: int = 1_200,
    drift: float = 0.3,
    wave: float = 0.6,
    noise: float = 0.2,
) -> dict[Timeframe, tuple[Candle, ...]]:
    """Mercado multi-timeframe coherente para tests de integración."""
    day_start = end_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        Timeframe.M5: make_candles(
            Timeframe.M5, m5_bars, end_utc=end_utc, drift=drift, wave=wave, noise=noise
        ),
        Timeframe.M15: make_candles(
            Timeframe.M15, max(200, m5_bars // 4), end_utc=end_utc, drift=drift, wave=wave, noise=noise
        ),
        Timeframe.H1: make_candles(
            Timeframe.H1, 400, end_utc=end_utc, drift=drift, wave=wave, noise=noise
        ),
        Timeframe.H4: make_candles(
            Timeframe.H4, 120, end_utc=end_utc, drift=drift, wave=wave, noise=noise
        ),
        Timeframe.D1: make_candles(
            Timeframe.D1, 60, end_utc=day_start, drift=drift, wave=wave, noise=noise
        ),
    }
