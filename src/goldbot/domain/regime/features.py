"""Características causales para los detectores de régimen.

Todas las columnas de la fila `t` usan únicamente velas con cierre <= `t`. No hay
ventanas centradas, ni normalización con estadísticas futuras: un cambio en velas
posteriores no puede alterar filas anteriores.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import numpy as np
import pandas as pd

from goldbot.domain.types import Candle, RegimeSettings

FEATURE_COLUMNS: tuple[str, ...] = (
    "ret_1",
    "log_range",
    "atr",
    "atr_pct",
    "adx",
    "slope_norm",
    "efficiency",
    "vol_ratio",
)


def validate_candles(candles: Sequence[Candle]) -> tuple[Candle, ...]:
    """Solo velas cerradas, ordenadas y sin duplicados por hora de apertura."""
    unique: dict[datetime, Candle] = {}
    for candle in candles:
        if not candle.is_closed:
            raise ValueError("las características usan solo velas cerradas")
        unique[candle.open_time_utc] = candle
    ordered = tuple(unique[key] for key in sorted(unique))
    if not ordered:
        raise ValueError("se requiere al menos una vela cerrada")
    return ordered


def _index(candles: Sequence[Candle]) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex([candle.open_time_utc for candle in candles])
    return index if index.tz is not None else index.tz_localize("UTC")


def compute_features(candles: Sequence[Candle], settings: RegimeSettings) -> pd.DataFrame:
    """DataFrame indexado por apertura UTC con las columnas de `FEATURE_COLUMNS`."""
    series = validate_candles(candles)
    close = pd.Series([candle.close for candle in series], index=_index(series), dtype="float64")
    high = pd.Series([candle.high for candle in series], index=close.index, dtype="float64")
    low = pd.Series([candle.low for candle in series], index=close.index, dtype="float64")

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(
        alpha=1.0 / settings.atr_period,
        adjust=False,
        min_periods=settings.atr_period,
    ).mean()
    atr_pct = atr.rolling(
        settings.atr_percentile_window,
        min_periods=settings.atr_percentile_window,
    ).rank(pct=True)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    smoothed_tr = true_range.ewm(
        alpha=1.0 / settings.adx_period,
        adjust=False,
        min_periods=settings.adx_period,
    ).mean()
    plus_di = 100.0 * plus_dm.ewm(
        alpha=1.0 / settings.adx_period,
        adjust=False,
        min_periods=settings.adx_period,
    ).mean() / smoothed_tr
    minus_di = 100.0 * minus_dm.ewm(
        alpha=1.0 / settings.adx_period,
        adjust=False,
        min_periods=settings.adx_period,
    ).mean() / smoothed_tr
    denominator = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    adx = dx.ewm(
        alpha=1.0 / settings.adx_period,
        adjust=False,
        min_periods=settings.adx_period,
    ).mean()

    ema_fast = close.ewm(
        span=settings.ema_fast, adjust=False, min_periods=settings.ema_fast
    ).mean()
    slope_norm = (ema_fast - ema_fast.shift(settings.slope_lookback)) / atr.replace(0.0, np.nan)

    lookback = settings.slope_lookback
    net_move = (close - close.shift(lookback)).abs()
    path = close.diff().abs().rolling(lookback, min_periods=lookback).sum()
    efficiency = net_move / path.replace(0.0, np.nan)

    long_atr = atr.rolling(settings.atr_period * 4, min_periods=settings.atr_period).mean()
    volatility_ratio = atr / long_atr.replace(0.0, np.nan)

    return pd.DataFrame(
        {
            "ret_1": np.log(close / previous_close),
            "log_range": np.log(high / low.replace(0.0, np.nan)),
            "atr": atr,
            "atr_pct": atr_pct,
            "adx": adx,
            "slope_norm": slope_norm,
            "efficiency": efficiency,
            "vol_ratio": volatility_ratio,
        },
        index=close.index,
    )
