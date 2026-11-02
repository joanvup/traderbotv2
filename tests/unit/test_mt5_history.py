"""Descarga de velas: solo cerradas, `as_of` y recorte al contrato de `count`."""

from __future__ import annotations

from datetime import UTC, datetime

from goldbot.adapters.mt5 import history
from goldbot.domain.types import Timeframe
from goldbot.infra.time import UtcClock


class FakeMt5:
    TIMEFRAME_M5 = 5

    def __init__(self, rows: list[dict[str, float]]) -> None:
        self.rows = rows
        self.requested: list[int] = []

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> list:
        self.requested.append(count)
        return self.rows

    def last_error(self) -> tuple[int, str]:
        return (0, "ok")


def _row(server_hour: int, server_minute: int) -> dict[str, float]:
    wall = datetime(2026, 7, 15, server_hour, server_minute, tzinfo=UTC)
    return {
        "time": wall.timestamp(),
        "open": 2400.0,
        "high": 2401.0,
        "low": 2399.0,
        "close": 2400.5,
        "tick_volume": 100,
        "spread": 17,
    }


def test_forming_bar_is_dropped_and_count_honoured() -> None:
    clock = UtcClock("America/New_York")
    mt5 = FakeMt5([_row(14, 45), _row(14, 50), _row(14, 55)])
    as_of = datetime(2026, 7, 15, 11, 58, tzinfo=UTC)

    candles = history.fetch_candles(
        mt5, "XAUUSD", Timeframe.M5, 2, clock, as_of_utc=as_of
    )

    assert mt5.requested == [3]
    assert len(candles) == 2
    assert candles[-1].open_time_utc == datetime(2026, 7, 15, 11, 50, tzinfo=UTC)
    assert candles[-1].close_time_utc == datetime(2026, 7, 15, 11, 55, tzinfo=UTC)
    assert all(candle.is_closed for candle in candles)
    assert candles[-1].source == "MT5"


def test_as_of_discards_future_bars() -> None:
    clock = UtcClock("America/New_York")
    mt5 = FakeMt5([_row(14, 45), _row(14, 50), _row(14, 55)])
    as_of = datetime(2026, 7, 15, 11, 52, tzinfo=UTC)

    candles = history.fetch_candles(
        mt5, "XAUUSD", Timeframe.M5, 5, clock, as_of_utc=as_of
    )

    assert [candle.open_time_utc for candle in candles] == [
        datetime(2026, 7, 15, 11, 45, tzinfo=UTC)
    ]
