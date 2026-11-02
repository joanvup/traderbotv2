"""Caché CSV.gz de velas cerradas: escritura atómica y round-trip."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goldbot.domain.types import Candle, Timeframe
from goldbot.infra import candle_cache


def _candles(count: int) -> tuple[Candle, ...]:
    start = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    result = []
    for index in range(count):
        opened = start + timedelta(minutes=5 * index)
        result.append(
            Candle(
                open_time_utc=opened,
                close_time_utc=opened + timedelta(minutes=5),
                open=2400.0 + index,
                high=2401.0 + index,
                low=2399.0 + index,
                close=2400.5 + index,
                tick_volume=100 + index,
                spread_points=17.0,
                is_closed=True,
                received_at_utc=opened + timedelta(minutes=5),
                source="TEST",
            )
        )
    return tuple(result)


def test_round_trip(tmp_path: Path) -> None:
    path = candle_cache.cache_path(tmp_path, "XAUUSD", Timeframe.M5)
    assert path.suffixes == [".csv", ".gz"]
    written = candle_cache.save_candles(path, _candles(3))
    assert written == 3
    loaded = candle_cache.load_candles(path)
    assert loaded == _candles(3)
    assert candle_cache.last_closed(path) == loaded[-1]


def test_forming_candle_is_rejected(tmp_path: Path) -> None:
    path = candle_cache.cache_path(tmp_path, "XAUUSD", Timeframe.M5)
    forming = _candles(1)[0]
    forming = Candle(
        open_time_utc=forming.open_time_utc,
        close_time_utc=forming.close_time_utc,
        open=forming.open,
        high=forming.high,
        low=forming.low,
        close=forming.close,
        tick_volume=forming.tick_volume,
        spread_points=forming.spread_points,
        is_closed=False,
        received_at_utc=forming.received_at_utc,
        source="TEST",
    )
    with pytest.raises(ValueError, match="cerradas"):
        candle_cache.save_candles(path, [forming])


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        candle_cache.load_candles(tmp_path / "nope.csv.gz")
