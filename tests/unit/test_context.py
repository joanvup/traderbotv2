"""ContextBuilder: sesgo multi-timeframe, estructura, timing, frescura y hash."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _synthetic import make_candles

from goldbot.domain.context import ContextBuilder, compute_snapshot_hash
from goldbot.domain.types import (
    Candle,
    ContextSettings,
    ContextSnapshot,
    DataStatus,
    Session,
    Timeframe,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
_DAY_START = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
_SETTINGS = ContextSettings()


def _market(*, drift: float = 0.4, wave: float = 0.0, noise: float = 0.3):
    return {
        Timeframe.M5: make_candles(Timeframe.M5, 400, end_utc=NOW, drift=drift, wave=wave, noise=noise),
        Timeframe.M15: make_candles(
            Timeframe.M15, 300, end_utc=NOW, drift=drift, wave=wave, noise=noise
        ),
        Timeframe.H1: make_candles(
            Timeframe.H1, 300, end_utc=NOW, drift=drift, wave=wave, noise=noise
        ),
        Timeframe.H4: make_candles(
            Timeframe.H4, 200, end_utc=NOW, drift=drift, wave=wave, noise=noise
        ),
        Timeframe.D1: make_candles(Timeframe.D1, 90, end_utc=_DAY_START, drift=drift, noise=noise),
    }


def _build(candles, *, now: datetime = NOW, stale: dict[Timeframe, float] | None = None):
    builder = ContextBuilder(_SETTINGS, stale_after_s=stale)
    return builder.build(
        candles,
        now_utc=now,
        decision_timeframe=Timeframe.M5,
        session=Session.LONDON,
        config_hash="cfg-1",
        model_versions={"rules": "rules-1"},
    )


def test_snapshot_is_reproducible_and_marks_decision_bar() -> None:
    candles = _market()
    first = _build(candles)
    second = _build(candles)
    assert first.snapshot_hash == second.snapshot_hash
    assert len(first.snapshot_hash) == 64
    assert first.config_hash == "cfg-1"
    assert first.session is Session.LONDON
    assert first.bar_time_utc == candles[Timeframe.M5][-1].open_time_utc
    assert first.decision_time_utc == NOW
    assert first.model_versions == {"rules": "rules-1"}


def test_bias_follows_the_trend_on_every_timeframe() -> None:
    snapshot = _build(_market(drift=0.5, noise=0.2))
    assert set(snapshot.bias) == set(Timeframe)
    for timeframe, bias in snapshot.bias.items():
        assert bias.direction is not None, timeframe
        assert bias.direction.value == "BUY"
        assert bias.strength > 0.5
        assert bias.ema_fast > bias.ema_slow


def test_structure_detects_swings_levels_and_previous_day() -> None:
    candles = _market(wave=2.0, noise=0.5)
    snapshot = _build(candles)
    structure = snapshot.structure

    assert structure.swings
    assert all(swing.bars_ago >= _SETTINGS.swing_lookback for swing in structure.swings)
    assert structure.levels
    assert {level.kind for level in structure.levels} & {
        "SUPPORT",
        "RESISTANCE",
        "PREV_DAY_HIGH",
        "PREV_DAY_LOW",
        "ROUND",
    }
    last_day = candles[Timeframe.D1][-1]
    assert structure.prev_day_high == last_day.high
    assert structure.prev_day_low == last_day.low
    assert structure.prev_day_range == pytest.approx(last_day.high - last_day.low)
    assert structure.nearest_level_distance is not None
    assert structure.range_position is not None


def test_day_open_comes_from_first_intraday_bar_after_d1_close() -> None:
    candles = _market()
    snapshot = _build(candles)
    last_day = candles[Timeframe.D1][-1]
    intraday = [
        candle
        for candle in candles[Timeframe.H1]
        if candle.open_time_utc >= last_day.close_time_utc
    ]
    assert intraday
    assert snapshot.structure.day_open == intraday[0].open


def test_future_and_forming_candles_do_not_change_the_snapshot() -> None:
    candles = _market()
    reference = _build(candles)
    last = candles[Timeframe.M5][-1]
    forming = Candle(
        open_time_utc=last.close_time_utc,
        close_time_utc=last.close_time_utc + timedelta(minutes=5),
        open=last.close,
        high=last.close + 3.0,
        low=last.close - 3.0,
        close=last.close + 1.0,
        tick_volume=10,
        spread_points=15.0,
        is_closed=False,
        received_at_utc=last.close_time_utc,
        source="TEST",
    )
    future = Candle(
        open_time_utc=NOW,
        close_time_utc=NOW + timedelta(minutes=5),
        open=last.close,
        high=last.close + 5.0,
        low=last.close - 5.0,
        close=last.close + 2.0,
        tick_volume=10,
        spread_points=15.0,
        is_closed=True,
        received_at_utc=NOW,
        source="TEST",
    )
    extended = {**candles, Timeframe.M5: (*candles[Timeframe.M5], forming, future)}
    assert _build(extended).snapshot_hash == reference.snapshot_hash


def test_freshness_marks_stale_and_missing() -> None:
    candles = _market()
    later = NOW + timedelta(hours=2)
    snapshot = _build({**candles, Timeframe.H1: ()}, now=later, stale={Timeframe.M5: 60.0})
    assert snapshot.freshness[Timeframe.H1].status is DataStatus.MISSING
    assert snapshot.freshness[Timeframe.H1].last_bar_close_utc is None
    assert snapshot.freshness[Timeframe.M5].status is DataStatus.STALE
    assert snapshot.freshness[Timeframe.M5].age_s > 60.0


def test_builder_rejects_naive_now_and_missing_decision_bars() -> None:
    candles = _market()
    with pytest.raises(ValueError, match="zona horaria"):
        _build(candles, now=datetime(2026, 7, 15, 12, 0))
    with pytest.raises(ValueError, match="no hay velas cerradas"):
        _build({**candles, Timeframe.M5: ()})


def test_jsonable_roundtrip_reproduces_the_hash() -> None:
    snapshot: ContextSnapshot = _build(_market())
    payload = snapshot.to_jsonable()
    digest = payload.pop("snapshot_hash")
    assert compute_snapshot_hash(payload) == digest == snapshot.snapshot_hash
    assert payload["decision_timeframe"] == "M5"
    assert payload["session"] == "LONDON"
    assert payload["timing"]["atr"] > 0.0
