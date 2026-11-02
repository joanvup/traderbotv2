"""Filtros previos: demo, frescura, sesión, spread, noticias y límites diarios."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from _snapshots import (
    NOW,
    account,
    clock,
    server_time_utc,
    signal,
    snapshot,
    spec,
    timing,
)

from goldbot.domain.filters import InMemoryDailyLedger, PreTradeFilterChain
from goldbot.domain.types import (
    CalendarEvent,
    CalendarHealth,
    DataFreshness,
    DataStatus,
    FilterCode,
    FilterSettings,
    Timeframe,
)
from goldbot.infra.calendar import StaticEconomicCalendar

_ACCOUNT = account()
_SPEC = spec()


def _settings(**overrides) -> FilterSettings:
    base = FilterSettings(
        max_spread_multiplier=2.5,
        news_window_before_min=30,
        news_window_after_min=30,
        news_min_impact="moderate",
        news_currencies=("USD",),
        calendar_unavailable_policy="block",
        entry_windows=("07:00-20:00",),
        avoid_rollover=True,
        rollover_buffer_min=15,
        max_trades_day=2,
        daily_loss_pct=2.0,
    )
    return replace(base, **overrides)


def _calendar(*events: CalendarEvent, now=NOW) -> StaticEconomicCalendar:
    return StaticEconomicCalendar(
        events,
        coverage_start_utc=now - timedelta(days=2),
        coverage_end_utc=now + timedelta(days=2),
        health=CalendarHealth.OK,
        fetched_at_utc=now,
    )


def _chain(
    settings: FilterSettings,
    *,
    calendar: StaticEconomicCalendar | None,
    ledger: InMemoryDailyLedger | None = None,
) -> PreTradeFilterChain:
    return PreTradeFilterChain(settings, calendar, clock(), ledger or InMemoryDailyLedger())


def _blocked_codes(outcome) -> set[FilterCode]:
    return {result.code for result in outcome.results if result.blocked}


def test_all_filters_pass_for_a_clean_demo_setup() -> None:
    outcome = _chain(_settings(), calendar=_calendar()).evaluate(
        snapshot(), signal(), _ACCOUNT, _SPEC
    )
    assert outcome.blocked is False
    assert outcome.primary_reason is None
    assert {result.code for result in outcome.results} == {
        FilterCode.DEMO_GUARD,
        FilterCode.DATA_STALE,
        FilterCode.SESSION,
        FilterCode.SPREAD,
        FilterCode.NEWS,
        FilterCode.MAX_TRADES,
        FilterCode.DAILY_LOSS,
    }


def test_live_account_is_rejected() -> None:
    outcome = _chain(_settings(), calendar=_calendar()).evaluate(
        snapshot(), signal(), account(is_demo=False), _SPEC
    )
    assert outcome.blocked is True
    assert FilterCode.DEMO_GUARD in _blocked_codes(outcome)


def test_stale_decision_timeframe_blocks_even_if_others_are_fresh() -> None:
    freshness = {
        Timeframe.M5: DataFreshness(Timeframe.M5, NOW, 900.0, DataStatus.STALE),
        Timeframe.H1: DataFreshness(Timeframe.H1, NOW, 60.0, DataStatus.OK),
    }
    outcome = _chain(_settings(), calendar=_calendar()).evaluate(
        snapshot(freshness=freshness), signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.DATA_STALE in _blocked_codes(outcome)


def test_contexto_atrasado_no_bloquea_pero_se_informa() -> None:
    """Reabrir el lunes: el daily sigue atrasado horas y hay que poder operar con M5 fresco."""
    freshness = {
        Timeframe.M5: DataFreshness(Timeframe.M5, NOW, 60.0, DataStatus.OK),
        Timeframe.D1: DataFreshness(Timeframe.D1, NOW, 5_400.0, DataStatus.STALE),
        Timeframe.H4: DataFreshness(Timeframe.H4, None, None, DataStatus.MISSING),
    }
    outcome = _chain(_settings(), calendar=_calendar()).evaluate(
        snapshot(freshness=freshness), signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.DATA_STALE not in _blocked_codes(outcome)
    message = next(
        result.message for result in outcome.results if result.code is FilterCode.DATA_STALE
    )
    assert "contexto atrasado" in message


def test_entries_only_inside_the_server_window() -> None:
    early = snapshot(now=server_time_utc(5))
    outcome = _chain(_settings(), calendar=_calendar()).evaluate(
        early, signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.SESSION in _blocked_codes(outcome)
    assert "fuera de horario" in next(
        result.message for result in outcome.results if result.code is FilterCode.SESSION
    )


def test_rollover_window_blocks_trading_around_server_midnight() -> None:
    late = snapshot(now=server_time_utc(23, 50))
    settings = _settings(entry_windows=("00:00-23:59",), avoid_rollover=True)
    outcome = _chain(settings, calendar=_calendar()).evaluate(
        late, signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.SESSION in _blocked_codes(outcome)
    assert "rollover" in next(
        result.message for result in outcome.results if result.code is FilterCode.SESSION
    )


def test_spread_wider_than_the_multiple_is_blocked() -> None:
    wide = snapshot(timing_state=timing(spread_points=80.0, spread_mean_points=20.0))
    outcome = _chain(_settings(), calendar=_calendar()).evaluate(
        wide, signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.SPREAD in _blocked_codes(outcome)


def test_relevant_news_in_the_window_blocks_the_entry() -> None:
    event = CalendarEvent(
        event_id="1",
        ts_utc=NOW,
        currency="USD",
        impact="high",
        title="FOMC Statement",
        event_code="fomc",
    )
    outcome = _chain(_settings(), calendar=_calendar(event)).evaluate(
        snapshot(), signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.NEWS in _blocked_codes(outcome)


def test_critical_event_code_blocks_even_with_low_impact() -> None:
    event = CalendarEvent(
        event_id="2", ts_utc=NOW, currency="USD", impact="low", title="FOMC", event_code="fomc"
    )
    settings = _settings(news_critical_event_codes=("fomc",))
    outcome = _chain(settings, calendar=_calendar(event)).evaluate(
        snapshot(), signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.NEWS in _blocked_codes(outcome)


def test_low_impact_event_without_critical_code_is_ignored() -> None:
    event = CalendarEvent(
        event_id="3", ts_utc=NOW, currency="USD", impact="low", title="Minor", event_code="minor"
    )
    settings = _settings(news_critical_event_codes=("fomc",))
    outcome = _chain(settings, calendar=_calendar(event)).evaluate(
        snapshot(), signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.NEWS not in _blocked_codes(outcome)


def test_calendar_unavailable_policy_block_and_allow() -> None:
    blocked = _chain(_settings(), calendar=None).evaluate(snapshot(), signal(), _ACCOUNT, _SPEC)
    allowed = _chain(_settings(calendar_unavailable_policy="allow"), calendar=None).evaluate(
        snapshot(), signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.NEWS in _blocked_codes(blocked)
    assert FilterCode.NEWS not in _blocked_codes(allowed)


def test_news_filter_can_be_disabled() -> None:
    settings = _settings(news_window_before_min=0, news_window_after_min=0)
    outcome = _chain(settings, calendar=None).evaluate(snapshot(), signal(), _ACCOUNT, _SPEC)
    assert FilterCode.NEWS not in _blocked_codes(outcome)


def test_daily_trade_limit_counts_entries() -> None:
    ledger = InMemoryDailyLedger()
    day = clock().trading_day(NOW)
    ledger.record_entry(day)
    ledger.record_entry(day)
    outcome = _chain(_settings(), calendar=_calendar(), ledger=ledger).evaluate(
        snapshot(), signal(), _ACCOUNT, _SPEC
    )
    assert FilterCode.MAX_TRADES in _blocked_codes(outcome)


def test_daily_loss_limit_latches_until_the_next_day() -> None:
    ledger = InMemoryDailyLedger()
    day = clock().trading_day(NOW)
    ledger.ensure_day(day, 1_000.0)
    ledger.record_pnl(day, -25.0)
    outcome = _chain(_settings(), calendar=_calendar(), ledger=ledger).evaluate(
        snapshot(), signal(), account(equity=975.0), _SPEC
    )
    assert FilterCode.DAILY_LOSS in _blocked_codes(outcome)
    assert "2.50%" in next(
        result.message
        for result in outcome.results
        if result.code is FilterCode.DAILY_LOSS and result.blocked
    )


def test_primary_reason_reports_the_first_blocking_filter() -> None:
    outcome = _chain(_settings(), calendar=_calendar()).evaluate(
        snapshot(), signal(), account(is_demo=False), _SPEC
    )
    assert outcome.primary_reason is not None
    assert "demo" in outcome.primary_reason
