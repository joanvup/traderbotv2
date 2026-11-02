"""La regla horaria de Pepperstone: America/New_York + 7 h (GMT+3/GMT+2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from goldbot.domain.types import Session
from goldbot.infra.time import UtcClock, parse_session_windows


def test_server_offset_is_gmt_plus_2_in_winter() -> None:
    clock = UtcClock("America/New_York")
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert clock.server_offset(winter) == timedelta(hours=2)
    assert clock.to_server(winter) == datetime(2026, 1, 15, 14, 0)


def test_server_offset_is_gmt_plus_3_in_us_summer() -> None:
    clock = UtcClock("America/New_York")
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert clock.server_offset(summer) == timedelta(hours=3)
    assert clock.to_server(summer) == datetime(2026, 7, 15, 15, 0)


def test_to_utc_inverts_to_server() -> None:
    clock = UtcClock("America/New_York")
    server = datetime(2026, 7, 15, 15, 0)
    assert clock.to_utc(server) == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_mismatch_week_europe_athens_would_be_wrong() -> None:
    # Semana en que EE. UU. ya cambió y Europa no: el servidor sigue en GMT+3
    # mientras Europa está en GMT+2. La regla NY+7h lo refleja.
    clock = UtcClock("America/New_York")
    ts = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    assert clock.server_offset(ts) == timedelta(hours=3)


def test_trading_day_uses_server_date() -> None:
    clock = UtcClock("America/New_York")
    # 22:30 UTC en verano = 01:30 del día siguiente en el servidor.
    ts = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)
    assert clock.trading_day(ts).isoformat() == "2026-07-16"


def test_explicit_offset_override_wins() -> None:
    clock = UtcClock("America/New_York", server_offset=timedelta(hours=3))
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert clock.to_server(winter) == datetime(2026, 1, 15, 15, 0)
    assert clock.to_utc(datetime(2026, 1, 15, 15, 0)) == winter


def _session_clock() -> UtcClock:
    return UtcClock(
        "America/New_York",
        session_windows=parse_session_windows(
            {
                "ASIA": ["03:00-11:00"],
                "LONDON": ["11:00-19:00"],
                "NEW_YORK": ["16:00-00:00"],
            }
        ),
    )


def test_session_at_uses_server_time_windows() -> None:
    clock = _session_clock()
    # UTC 00:00 en verano = 03:00 del servidor -> Asia.
    assert clock.session_at(datetime(2026, 7, 15, 0, 0, tzinfo=UTC)) is Session.ASIA
    # UTC 09:00 = 12:00 servidor -> Londres.
    assert clock.session_at(datetime(2026, 7, 15, 9, 0, tzinfo=UTC)) is Session.LONDON
    # UTC 14:00 = 17:00 servidor -> solape, gana Nueva York.
    assert clock.session_at(datetime(2026, 7, 15, 14, 0, tzinfo=UTC)) is Session.NEW_YORK
    # UTC 20:00 = 23:00 servidor -> sigue Nueva York (ventana hasta 00:00).
    assert clock.session_at(datetime(2026, 7, 15, 20, 0, tzinfo=UTC)) is Session.NEW_YORK
    # UTC 22:00 = 01:00 servidor -> CLOSED.
    assert clock.session_at(datetime(2026, 7, 15, 22, 0, tzinfo=UTC)) is Session.CLOSED


def test_session_at_requires_timezone() -> None:
    with pytest.raises(ValueError, match="zona horaria"):
        _session_clock().session_at(datetime(2026, 7, 15, 12, 0))


def test_parse_session_windows_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="ventana horaria inválida"):
        parse_session_windows({"ASIA": ["25:00-26:00"]})
    with pytest.raises(ValueError, match="sesión desconocida"):
        parse_session_windows({"TOKYO": ["03:00-11:00"]})
