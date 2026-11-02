"""Constructores deterministas de snapshots, regímenes y cuentas para los tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from goldbot.domain.context import compute_snapshot_hash
from goldbot.domain.types import (
    AccountState,
    Bias,
    ContextSnapshot,
    DataFreshness,
    DataStatus,
    Level,
    Regime,
    RegimeProbabilities,
    RegimeState,
    Session,
    Side,
    Signal,
    StructureState,
    Swing,
    SymbolSpec,
    Timeframe,
    TimingState,
)
from goldbot.infra.time import UtcClock, parse_session_windows

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def bias(
    timeframe: Timeframe,
    direction: Side | None,
    *,
    strength: float = 0.6,
    ema_fast: float = 2_400.0,
    ema_slow: float = 2_390.0,
    slope: float = 0.2,
) -> Bias:
    return Bias(
        timeframe=timeframe,
        direction=direction,
        strength=strength,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        slope_normalized=slope,
        rationale="test",
    )


def structure(
    *,
    swings: tuple[Swing, ...] = (),
    levels: tuple[Level, ...] = (),
    prev_day_high: float | None = 2_420.0,
    prev_day_low: float | None = 2_380.0,
    range_position: float | None = 0.5,
) -> StructureState:
    day_range = (
        prev_day_high - prev_day_low
        if prev_day_high is not None and prev_day_low is not None
        else None
    )
    return StructureState(
        swings=swings,
        levels=levels,
        prev_day_high=prev_day_high,
        prev_day_low=prev_day_low,
        prev_day_range=day_range,
        day_open=2_398.0,
        range_position=range_position,
        nearest_level_distance=5.0,
    )


def timing(
    *,
    atr: float = 4.0,
    atr_percentile: float = 0.7,
    spread_points: float = 20.0,
    last_bar_range: float = 6.0,
    momentum: float = 0.8,
    volatility_ratio: float = 1.2,
    spread_mean_points: float = 18.0,
) -> TimingState:
    return TimingState(
        atr=atr,
        atr_percentile=atr_percentile,
        spread_points=spread_points,
        last_bar_range=last_bar_range,
        momentum=momentum,
        volatility_ratio=volatility_ratio,
        spread_mean_points=spread_mean_points,
    )


def snapshot(
    *,
    bias_map: Mapping[Timeframe, Bias] | None = None,
    structure_state: StructureState | None = None,
    timing_state: TimingState | None = None,
    freshness: Mapping[Timeframe, DataFreshness] | None = None,
    session: Session = Session.LONDON,
    now: datetime = NOW,
    last_price: float = 2_400.0,
) -> ContextSnapshot:
    resolved_bias = dict(
        bias_map
        if bias_map is not None
        else {
            Timeframe.M15: bias(Timeframe.M15, Side.BUY, ema_fast=2_400.0, ema_slow=2_390.0),
            Timeframe.H1: bias(Timeframe.H1, Side.BUY),
            Timeframe.H4: bias(Timeframe.H4, Side.BUY, strength=0.5),
            Timeframe.D1: bias(Timeframe.D1, Side.BUY, strength=0.4),
        }
    )
    resolved_freshness = dict(
        freshness
        if freshness is not None
        else {
            timeframe: DataFreshness(timeframe, now - timedelta(minutes=5), 300.0, DataStatus.OK)
            for timeframe in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)
        }
    )
    base = ContextSnapshot(
        decision_time_utc=now,
        data_cutoff_utc=now,
        bar_time_utc=now - timedelta(minutes=5),
        decision_timeframe=Timeframe.M5,
        bias=resolved_bias,
        structure=structure_state or structure(),
        timing=timing_state or timing(),
        session=session,
        freshness=resolved_freshness,
        snapshot_hash="",
        config_hash="cfg-test",
        model_versions={"rules": "rules-1"},
        last_price=last_price,
    )
    payload = base.to_jsonable()
    payload.pop("snapshot_hash", None)
    return replace(base, snapshot_hash=compute_snapshot_hash(payload))


def regime(
    label: Regime,
    *,
    confidence: float = 0.8,
    method: str = "rules",
    probs: Mapping[Regime, float] | None = None,
    now: datetime = NOW,
) -> RegimeState:
    resolved = dict(probs) if probs is not None else {label: confidence}
    return RegimeState(
        label=label,
        probs=RegimeProbabilities(probs=resolved, confidence=confidence, method=method),
        confidence=confidence,
        confirmed_bars=4,
        decided_at_utc=now,
    )


def account(
    *,
    equity: float = 1_000.0,
    balance: float | None = None,
    is_demo: bool = True,
    margin_free: float | None = None,
) -> AccountState:
    return AccountState(
        login=61623438,
        server="Pepperstone-Demo",
        currency="USD",
        balance=balance if balance is not None else equity,
        equity=equity,
        margin_free=equity if margin_free is None else margin_free,
        leverage=100,
        is_demo=is_demo,
        trade_allowed=True,
    )


def spec() -> SymbolSpec:
    from goldbot.backtest.broker_sim import default_symbol_spec

    return default_symbol_spec()


def signal(
    *,
    side: Side = Side.BUY,
    entry: float = 2_400.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    playbook_id: str = "trend_pullback",
    now: datetime = NOW,
) -> Signal:
    stop = stop_loss if stop_loss is not None else (entry - 4.0 if side is Side.BUY else entry + 4.0)
    target = (
        take_profit
        if take_profit is not None
        else (entry + 8.0 if side is Side.BUY else entry - 8.0)
    )
    return Signal(
        signal_id=f"{playbook_id}-test",
        playbook_id=playbook_id,
        side=side,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
        risk_distance=abs(entry - stop),
        confidence=0.8,
        regime_at_signal=Regime.TREND_UP if side is Side.BUY else Regime.TREND_DOWN,
        created_at_utc=now,
        valid_until_bar_utc=now + timedelta(minutes=10),
        invalidation_price=None,
        rationale="señal de test",
        inputs_hash="snap-test",
    )


def clock() -> UtcClock:
    return UtcClock(
        "America/New_York",
        server_shift=timedelta(hours=7),
        session_windows=parse_session_windows(
            {
                "ASIA": ["03:00-11:00"],
                "LONDON": ["11:00-19:00"],
                "NEW_YORK": ["16:00-00:00"],
            }
        ),
    )


def server_time_utc(hour: int, minute: int = 0) -> datetime:
    """UTC que corresponde a una hora del servidor (NY EDT +7 = UTC+3) en julio."""
    return datetime(2026, 7, 15, hour, minute, tzinfo=UTC) - timedelta(hours=3)


__all__ = [
    "NOW",
    "Side",
    "Timeframe",
    "account",
    "bias",
    "clock",
    "regime",
    "server_time_utc",
    "signal",
    "snapshot",
    "spec",
    "structure",
    "timing",
]
