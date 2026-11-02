"""Construcción del ContextSnapshot multi-timeframe a partir de velas cerradas.

Regla anti look-ahead: solo entran velas cerradas cuyo `close_time_utc` no supera
`now_utc`. El snapshot resultante es inmutable, serializable y lleva `snapshot_hash`
para poder reproducir exactamente lo que vio el bot al decidir.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldbot.domain.types import (
    Bias,
    Candle,
    ContextSettings,
    ContextSnapshot,
    DataFreshness,
    DataStatus,
    Level,
    Session,
    Side,
    StructureState,
    Swing,
    Timeframe,
    TimingState,
)

_TF_SECONDS: dict[Timeframe, float] = {
    Timeframe.M5: 300.0,
    Timeframe.M15: 900.0,
    Timeframe.H1: 3_600.0,
    Timeframe.H4: 14_400.0,
    Timeframe.D1: 86_400.0,
}

_MIN_BIAS_SCORE = 0.15
_MAX_LEVEL_TOUCHES = 4.0


def compute_snapshot_hash(payload: Mapping[str, Any]) -> str:
    """Huella canónica SHA-256 de un snapshot serializable (fechas incluidas)."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _closed_candles(candles: Sequence[Candle], now_utc: datetime) -> tuple[Candle, ...]:
    """Velas cerradas, ordenadas, sin duplicados y nunca posteriores a `now_utc`."""
    unique: dict[datetime, Candle] = {}
    for candle in candles:
        if not candle.is_closed:
            continue
        if candle.close_time_utc > now_utc:
            continue
        unique[candle.open_time_utc] = candle
    return tuple(unique[key] for key in sorted(unique))


def _series(candles: Sequence[Candle]) -> tuple[pd.Series, pd.Series, pd.Series]:
    index = pd.DatetimeIndex([candle.open_time_utc for candle in candles])
    if index.tz is None:
        index = index.tz_localize("UTC")
    close = pd.Series([candle.close for candle in candles], index=index, dtype="float64")
    high = pd.Series([candle.high for candle in candles], index=index, dtype="float64")
    low = pd.Series([candle.low for candle in candles], index=index, dtype="float64")
    return close, high, low


def _wilder_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _atr_value(close: pd.Series, high: pd.Series, low: pd.Series, period: int) -> float:
    atr = _wilder_atr(high, low, close, period)
    value = atr.iloc[-1]
    if pd.isna(value) or value <= 0.0:
        fallback = float((high - low).tail(max(1, min(period, len(close)))).mean())
        return fallback if math.isfinite(fallback) and fallback > 0.0 else 0.0
    return float(value)


def _bias(candles: Sequence[Candle], timeframe: Timeframe, settings: ContextSettings) -> Bias:
    close, high, low = _series(candles)
    last_close = float(close.iloc[-1])
    needed = settings.ema_slow + settings.slope_lookback + 1
    if len(close) < needed:
        return Bias(
            timeframe=timeframe,
            direction=None,
            strength=0.0,
            ema_fast=last_close,
            ema_slow=last_close,
            slope_normalized=0.0,
            rationale=f"histórico insuficiente ({len(close)}/{needed} velas)",
        )

    ema_fast = close.ewm(span=settings.ema_fast, adjust=False, min_periods=settings.ema_fast).mean()
    ema_slow = close.ewm(span=settings.ema_slow, adjust=False, min_periods=settings.ema_slow).mean()
    atr = _wilder_atr(high, low, close, settings.atr_period)
    atr_last = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
    if atr_last <= 0.0:
        return Bias(
            timeframe=timeframe,
            direction=None,
            strength=0.0,
            ema_fast=float(ema_fast.iloc[-1]),
            ema_slow=float(ema_slow.iloc[-1]),
            slope_normalized=0.0,
            rationale="ATR nulo; sesgo indeterminado",
        )

    separation = float(ema_fast.iloc[-1] - ema_slow.iloc[-1]) / atr_last
    slope = float(ema_fast.iloc[-1] - ema_fast.iloc[-1 - settings.slope_lookback]) / atr_last
    score = 0.6 * math.tanh(separation) + 0.4 * math.tanh(slope)
    direction: Side | None = None
    if score > _MIN_BIAS_SCORE:
        direction = Side.BUY
    elif score < -_MIN_BIAS_SCORE:
        direction = Side.SELL
    rationale = (
        f"EMA{settings.ema_fast} {'sobre' if separation >= 0 else 'bajo'} "
        f"EMA{settings.ema_slow} ({separation:+.2f} ATR); "
        f"pendiente {slope:+.2f} ATR"
    )
    return Bias(
        timeframe=timeframe,
        direction=direction,
        strength=min(1.0, abs(score)),
        ema_fast=float(ema_fast.iloc[-1]),
        ema_slow=float(ema_slow.iloc[-1]),
        slope_normalized=slope,
        rationale=rationale,
    )


def _detect_swings(
    candles: Sequence[Candle], *, lookback: int, max_swings: int
) -> list[Swing]:
    """Fractales: un swing en `i` solo usa barras cerradas hasta `i + lookback`."""
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    last = len(candles) - 1
    swings: list[Swing] = []
    for index in range(lookback, last - lookback + 1):
        window_high = highs[index - lookback : index + lookback + 1]
        window_low = lows[index - lookback : index + lookback + 1]
        if highs[index] >= max(window_high):
            swings.append(
                Swing(
                    kind="HIGH",
                    price=float(highs[index]),
                    bar_time_utc=candles[index].open_time_utc,
                    bars_ago=last - index,
                )
            )
        if lows[index] <= min(window_low):
            swings.append(
                Swing(
                    kind="LOW",
                    price=float(lows[index]),
                    bar_time_utc=candles[index].open_time_utc,
                    bars_ago=last - index,
                )
            )
    swings.sort(key=lambda swing: swing.bars_ago)
    return swings[:max_swings]


def _cluster_levels(
    swings: Sequence[Swing],
    *,
    price: float,
    tolerance: float,
    max_levels: int,
) -> list[Level]:
    """Agrupa swings cercanos en niveles y los recorta a los más próximos al precio."""
    clusters: list[list[Swing]] = []
    for swing in sorted(swings, key=lambda item: item.price):
        if clusters and abs(swing.price - _mean_price(clusters[-1])) <= tolerance:
            clusters[-1].append(swing)
        else:
            clusters.append([swing])

    levels: list[Level] = []
    for cluster in clusters:
        level_price = _mean_price(cluster)
        touches = len(cluster)
        newest = max(cluster, key=lambda item: item.bar_time_utc)
        levels.append(
            Level(
                kind="SUPPORT" if level_price <= price else "RESISTANCE",
                price=level_price,
                strength=min(1.0, touches / _MAX_LEVEL_TOUCHES),
                touches=touches,
                bar_time_utc=newest.bar_time_utc,
            )
        )
    levels.sort(key=lambda item: abs(item.price - price))
    return levels[:max_levels]


def _mean_price(cluster: Sequence[Swing]) -> float:
    return float(np.mean([swing.price for swing in cluster]))


def _structure(
    candles: Mapping[Timeframe, tuple[Candle, ...]],
    decision_timeframe: Timeframe,
    settings: ContextSettings,
) -> StructureState:
    base_timeframe = Timeframe.H1 if candles.get(Timeframe.H1) else decision_timeframe
    base = candles.get(base_timeframe, ())
    recent = base[-settings.structure_lookback_bars :]
    if recent:
        close, high, low = _series(recent)
        price = float(close.iloc[-1])
        atr = _atr_value(close, high, low, settings.atr_period)
    else:
        price = 0.0
        atr = 0.0

    swings = _detect_swings(
        recent, lookback=settings.swing_lookback, max_swings=settings.max_swings
    )
    tolerance = atr * settings.level_tolerance_atr if atr > 0.0 else 0.0
    levels = (
        _cluster_levels(swings, price=price, tolerance=tolerance, max_levels=settings.max_levels)
        if tolerance > 0.0
        else []
    )

    prev_day = candles.get(Timeframe.D1, ())
    prev_day_high: float | None = None
    prev_day_low: float | None = None
    prev_day_range: float | None = None
    day_open: float | None = None
    if prev_day:
        last_day = prev_day[-1]
        prev_day_high = float(last_day.high)
        prev_day_low = float(last_day.low)
        prev_day_range = prev_day_high - prev_day_low
        levels.extend(
            [
                Level(
                    kind="PREV_DAY_HIGH",
                    price=prev_day_high,
                    strength=1.0,
                    touches=1,
                    bar_time_utc=last_day.open_time_utc,
                ),
                Level(
                    kind="PREV_DAY_LOW",
                    price=prev_day_low,
                    strength=1.0,
                    touches=1,
                    bar_time_utc=last_day.open_time_utc,
                ),
            ]
        )
        intraday = [
            candle for candle in base if candle.open_time_utc >= last_day.close_time_utc
        ]
        if intraday:
            day_open = float(intraday[0].open)

    if settings.round_level_step > 0.0 and price > 0.0:
        step = settings.round_level_step
        lower = math.floor(price / step) * step
        for candidate in (lower, lower + step):
            levels.append(
                Level(
                    kind="ROUND",
                    price=float(candidate),
                    strength=0.5,
                    touches=1,
                    bar_time_utc=None,
                )
            )

    levels.sort(key=lambda item: abs(item.price - price))
    levels = levels[: settings.max_levels]
    nearest = min((abs(item.price - price) for item in levels), default=None)

    range_position: float | None = None
    if prev_day_high is not None and prev_day_low is not None and prev_day_range and price > 0.0:
        position = (price - prev_day_low) / prev_day_range
        range_position = float(min(1.0, max(0.0, position)))

    return StructureState(
        swings=tuple(swings),
        levels=tuple(levels),
        prev_day_high=prev_day_high,
        prev_day_low=prev_day_low,
        prev_day_range=prev_day_range,
        day_open=day_open,
        range_position=range_position,
        nearest_level_distance=nearest,
    )


def _timing(candles: Sequence[Candle], settings: ContextSettings) -> TimingState:
    close, high, low = _series(candles)
    atr = _wilder_atr(high, low, close, settings.atr_period)
    atr_last = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
    if atr_last <= 0.0:
        atr_last = float((high - low).tail(max(1, min(settings.atr_period, len(close)))).mean())
        if not math.isfinite(atr_last):
            atr_last = 0.0

    atr_percentile = atr.rolling(
        settings.atr_percentile_window, min_periods=settings.atr_percentile_window
    ).rank(pct=True)
    percentile_last = atr_percentile.iloc[-1]
    if pd.isna(percentile_last):
        percentile_last = 0.5

    lookback = settings.momentum_lookback
    momentum = 0.0
    if len(close) > lookback and atr_last > 0.0:
        momentum = float(close.iloc[-1] - close.iloc[-1 - lookback]) / atr_last

    long_atr = atr.rolling(
        settings.atr_period * 4, min_periods=settings.atr_period
    ).mean()
    ratio_last = long_atr.iloc[-1]
    volatility_ratio = (
        float(atr.iloc[-1] / ratio_last)
        if pd.notna(ratio_last) and ratio_last > 0.0 and pd.notna(atr.iloc[-1])
        else 1.0
    )

    last = candles[-1]
    window = min(settings.spread_lookback_bars, len(candles))
    spread_mean = (
        float(sum(candle.spread_points for candle in candles[-window:]) / window)
        if window > 0
        else 0.0
    )
    return TimingState(
        atr=float(atr_last),
        atr_percentile=float(percentile_last),
        spread_points=float(last.spread_points),
        last_bar_range=float(last.high - last.low),
        momentum=momentum,
        volatility_ratio=volatility_ratio,
        spread_mean_points=spread_mean,
    )


def _freshness(
    candles: Mapping[Timeframe, tuple[Candle, ...]],
    *,
    now_utc: datetime,
    stale_after_s: Mapping[Timeframe, float],
) -> dict[Timeframe, DataFreshness]:
    result: dict[Timeframe, DataFreshness] = {}
    for timeframe, series in candles.items():
        if not series:
            result[timeframe] = DataFreshness(timeframe, None, None, DataStatus.MISSING)
            continue
        last_close = series[-1].close_time_utc
        age = (now_utc - last_close).total_seconds()
        threshold = stale_after_s.get(timeframe, 3.0 * _TF_SECONDS.get(timeframe, 300.0))
        status = DataStatus.OK if age <= threshold else DataStatus.STALE
        result[timeframe] = DataFreshness(timeframe, last_close, age, status)
    return result


class ContextBuilder:
    """Combina sesgo multi-timeframe, estructura intradía y timing en un snapshot inmutable."""

    def __init__(
        self,
        settings: ContextSettings,
        *,
        stale_after_s: Mapping[Timeframe, float] | None = None,
    ) -> None:
        self._settings = settings
        self._stale_after_s = dict(stale_after_s or {})

    def build(
        self,
        candles: Mapping[Timeframe, Sequence[Candle]],
        *,
        now_utc: datetime,
        decision_timeframe: Timeframe,
        session: Session = Session.CLOSED,
        config_hash: str = "",
        model_versions: Mapping[str, str] | None = None,
    ) -> ContextSnapshot:
        """Construye el snapshot usando solo velas cerradas con cierre <= `now_utc`."""
        if now_utc.tzinfo is None:
            raise ValueError("now_utc debe tener zona horaria")
        prepared = {
            timeframe: _closed_candles(series, now_utc)
            for timeframe, series in candles.items()
        }
        decision = prepared.get(decision_timeframe, ())
        if not decision:
            raise ValueError(
                f"no hay velas cerradas de {decision_timeframe.value} anteriores a {now_utc.isoformat()}"
            )

        bias = {
            timeframe: _bias(series, timeframe, self._settings)
            for timeframe, series in prepared.items()
            if len(series) >= 2
        }
        structure = _structure(prepared, decision_timeframe, self._settings)
        timing = _timing(decision, self._settings)
        freshness = _freshness(
            prepared, now_utc=now_utc, stale_after_s=self._stale_after_s
        )
        decision_bar = decision[-1]

        snapshot = ContextSnapshot(
            decision_time_utc=now_utc,
            data_cutoff_utc=now_utc,
            bar_time_utc=decision_bar.open_time_utc,
            decision_timeframe=decision_timeframe,
            bias=bias,
            structure=structure,
            timing=timing,
            session=session,
            freshness=freshness,
            snapshot_hash="",
            config_hash=config_hash,
            model_versions=dict(model_versions or {}),
            last_price=float(decision_bar.close),
        )
        payload = snapshot.to_jsonable()
        payload.pop("snapshot_hash", None)
        return replace(snapshot, snapshot_hash=compute_snapshot_hash(payload))
