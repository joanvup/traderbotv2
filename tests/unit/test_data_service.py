"""Servicio de datos de Fase 1 con un broker falso: estado, eventos y frescura."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goldbot.config import AppConfig
from goldbot.domain.types import (
    AccountState,
    BrokerHealth,
    Candle,
    SymbolSpec,
    Tick,
    Timeframe,
)
from goldbot.engine.data_service import DataService
from goldbot.infra.state_store import SqliteStateStore
from goldbot.infra.time import UtcClock

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
_DURATIONS = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}
_WARMUP = 5


class FakeClock(UtcClock):
    def __init__(self, now: datetime = NOW) -> None:
        super().__init__("America/New_York")
        self._now = now

    def now_utc(self) -> datetime:
        return self._now

    def sleep_until(self, ts_utc: datetime) -> None:
        self._now = ts_utc


def _series(timeframe: Timeframe, count: int) -> list[Candle]:
    duration = _DURATIONS[timeframe]
    last_close = NOW - duration
    candles: list[Candle] = []
    for index in range(count):
        close = last_close - duration * (count - 1 - index)
        opened = close - duration
        candles.append(
            Candle(
                open_time_utc=opened,
                close_time_utc=close,
                open=2400.0,
                high=2401.0,
                low=2399.0,
                close=2400.5,
                tick_volume=100,
                spread_points=17.0,
                is_closed=True,
                received_at_utc=close,
                source="TEST",
            )
        )
    return candles


def _spec(now: datetime) -> SymbolSpec:
    return SymbolSpec(
        resolved_name="XAUUSD",
        requested_name="XAUUSD",
        digits=2,
        point=0.01,
        tick_size=0.01,
        trade_tick_value_loss=1.0,
        trade_tick_value_profit=1.0,
        contract_size=100.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        stops_level_points=0,
        freeze_level_points=0,
        allowed_fillings=2,
        filling_mode=2,
        trade_mode=4,
        currency_profit="USD",
        spec_hash="spec-hash",
        fetched_at_utc=now,
    )


class FakeBroker:
    def __init__(
        self,
        clock: FakeClock,
        *,
        demo: bool = True,
        tick_age_s: float = 1.0,
    ) -> None:
        self._clock = clock
        self._demo = demo
        self._tick_age_s = tick_age_s
        self._connected = False
        self.resolved_symbol = "XAUUSD"
        self.series = {
            timeframe: _series(timeframe, _WARMUP + 5) for timeframe in Timeframe
        }

    def connect(self) -> BrokerHealth:
        self._connected = True
        return self.health()

    def shutdown(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=self._connected,
            terminal_connected=self._connected,
            algo_trading_enabled=True,
            account_trade_allowed=True,
            latency_ms=1.0,
            last_error=None if self._connected else "sin conexión",
            checked_at_utc=self._clock.now_utc(),
        )

    def account(self) -> AccountState:
        return AccountState(
            login=61623438,
            server="Pepperstone-Demo",
            currency="USD",
            balance=1000.0,
            equity=1004.0,
            margin_free=900.0,
            leverage=500,
            is_demo=self._demo,
            trade_allowed=True,
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return _spec(self._clock.now_utc())

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        *,
        closed_only: bool = True,
        as_of_utc: datetime | None = None,
    ) -> tuple[Candle, ...]:
        as_of = as_of_utc or self._clock.now_utc()
        usable = [
            candle
            for candle in self.series[timeframe]
            if candle.is_closed and candle.close_time_utc <= as_of
        ]
        return tuple(usable[-count:])

    def get_last_tick(self, symbol: str) -> Tick:
        return Tick(
            time_utc=self._clock.now_utc() - timedelta(seconds=self._tick_age_s),
            bid=2400.0,
            ask=2400.17,
            last=2400.1,
            spread_points=17.0,
        )

    def positions_count(self) -> int:
        return 0


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "app": {
                "db_path": str(tmp_path / "state.db"),
                "data_dir": str(tmp_path / "data"),
                "log_dir": str(tmp_path / "logs"),
            },
            "data": {"warmup_bars": {tf.value: _WARMUP for tf in Timeframe}},
        }
    )


def _service(tmp_path: Path, **broker_kwargs: object) -> tuple[DataService, FakeBroker, FakeClock]:
    clock = FakeClock()
    broker = FakeBroker(clock, **broker_kwargs)
    store = SqliteStateStore(tmp_path / "state.db", clock)
    service = DataService(config=_config(tmp_path), broker=broker, store=store, clock=clock)
    return service, broker, clock


def test_startup_downloads_and_publishes_state(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    report = service.startup()
    assert report.resolved_symbol == "XAUUSD"
    assert report.is_demo is True
    assert report.server_offset_min == 180
    assert report.bars_cached == {tf.value: _WARMUP for tf in Timeframe}

    with sqlite3.connect(tmp_path / "state.db") as conn:
        state = conn.execute(
            "SELECT state, mode, resolved_symbol, feed_status, cycle_seq, server_offset_min "
            "FROM bot_state WHERE id = 1"
        ).fetchone()
        event_types = [row[0] for row in conn.execute("SELECT type FROM events")]
        snapshots = conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
        syncs = conn.execute("SELECT COUNT(*) FROM history_sync").fetchone()[0]
    assert state == ("OPERATIVO", "DEMO", "XAUUSD", "OK", 1, 180)
    assert "STARTUP" in event_types
    assert snapshots == 1
    assert syncs == len(Timeframe)
    for timeframe in Timeframe:
        assert (tmp_path / "data" / "cache" / "XAUUSD" / f"{timeframe.value}.csv.gz").exists()


def test_non_demo_account_is_rejected(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path, demo=False)
    with pytest.raises(RuntimeError, match="DEMO"):
        service.startup()
    with sqlite3.connect(tmp_path / "state.db") as conn:
        types = [row[0] for row in conn.execute("SELECT type FROM events")]
    assert "KILL_SWITCH" in types


def test_poll_detects_stale_feed_and_new_bars(tmp_path: Path) -> None:
    service, broker, _ = _service(tmp_path)
    service.startup()

    healthy = service.poll_once()
    assert healthy.connected is True
    assert healthy.overall_status.value == "OK"

    broker._tick_age_s = 3_600.0
    stale = service.poll_once()
    assert stale.overall_status.value == "STALE"
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT feed_status FROM bot_state").fetchone()[0] == "STALE"
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'FEED_STALE'"
        ).fetchone()[0] == 1

    newest = broker.series[Timeframe.M5][-1]
    broker.series[Timeframe.M5].append(
        Candle(
            open_time_utc=newest.close_time_utc,
            close_time_utc=newest.close_time_utc + _DURATIONS[Timeframe.M5],
            open=2400.0,
            high=2402.0,
            low=2399.5,
            close=2401.5,
            tick_volume=120,
            spread_points=18.0,
            is_closed=True,
            received_at_utc=newest.close_time_utc + _DURATIONS[Timeframe.M5],
            source="TEST",
        )
    )
    broker._tick_age_s = 1.0
    updated = service.poll_once()
    assert any(timeframe is Timeframe.M5 for timeframe, _ in updated.new_bars)
    from goldbot.infra import candle_cache

    cached = candle_cache.load_candles(
        candle_cache.cache_path(tmp_path / "data" / "cache", "XAUUSD", Timeframe.M5)
    )
    assert cached[-1].open_time_utc == newest.close_time_utc
    assert cached[-1].close == 2401.5


def test_poll_reports_disconnected_without_crashing(tmp_path: Path) -> None:
    service, broker, _ = _service(tmp_path)
    service.startup()
    broker.shutdown()
    result = service.poll_once()
    assert result.connected is False
    assert result.overall_status.value == "STALE"
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT state FROM bot_state").fetchone()[0] == "RECONNECTING"


def test_el_contexto_atrasado_no_cierra_la_puerta_de_entradas(tmp_path: Path) -> None:
    """D1/H4 sin vela cerrada nueva (lunes tras el fin de semana) no bloquean entradas."""
    service, broker, _ = _service(tmp_path)
    service.startup()
    broker.series[Timeframe.D1] = broker.series[Timeframe.D1][:2]
    broker.series[Timeframe.H4] = broker.series[Timeframe.H4][:2]

    result = service.poll_once()

    atrasados = {item.timeframe.value for item in result.feed if item.status.value == "STALE"}
    assert atrasados == {"D1", "H4"}
    assert result.overall_status.value == "OK"
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT feed_status FROM bot_state").fetchone()[0] == "OK"
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE type = 'FEED_STALE'").fetchone()[0]
            == 0
        )


def test_el_marco_de_decision_atrasado_si_cierra_la_puerta(tmp_path: Path) -> None:
    service, broker, _ = _service(tmp_path)
    service.startup()
    broker.series[Timeframe.M5] = broker.series[Timeframe.M5][:2]

    result = service.poll_once()

    assert result.overall_status.value == "STALE"
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT feed_status FROM bot_state").fetchone()[0] == "STALE"
        message = conn.execute(
            "SELECT message FROM events WHERE type = 'FEED_STALE'"
        ).fetchone()[0]
    assert message == "entradas bloqueadas: M5 en estado STALE"


def test_startup_without_history_reuses_cache(tmp_path: Path) -> None:
    first, _, _ = _service(tmp_path)
    first.startup()
    first.shutdown()

    second, _, _ = _service(tmp_path)
    report = second.startup(download_history=False)
    assert report.bars_cached == {}
    result = second.poll_once()
    assert result.new_bars == ()
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_sync").fetchone()[0] == len(Timeframe)
