"""MarketAnalyzer y su integración opcional con el servicio de datos."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _synthetic import make_candles

from goldbot.config import AppConfig
from goldbot.domain.types import (
    AccountState,
    BrokerHealth,
    Candle,
    ContextSnapshot,
    Regime,
    RegimeProbabilities,
    RegimeState,
    Session,
    StructureState,
    SymbolSpec,
    Tick,
    Timeframe,
    TimingState,
)
from goldbot.engine.analysis import AnalysisResult, MarketAnalyzer
from goldbot.engine.data_service import DataService
from goldbot.infra.state_store import SqliteStateStore
from goldbot.infra.time import UtcClock, parse_session_windows

NOW = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)
_DURATIONS = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}
_SESSIONS = {"ASIA": ["03:00-11:00"], "LONDON": ["11:00-19:00"], "NEW_YORK": ["16:00-00:00"]}


class MutableClock(UtcClock):
    def __init__(self, now: datetime) -> None:
        super().__init__(
            "America/New_York", session_windows=parse_session_windows(_SESSIONS)
        )
        self._now = now

    def now_utc(self) -> datetime:
        return self._now

    def sleep_until(self, ts_utc: datetime) -> None:
        self._now = ts_utc


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
    def __init__(self, clock: MutableClock, series: dict[Timeframe, tuple[Candle, ...]]) -> None:
        self._clock = clock
        self.series = series
        self.resolved_symbol = "XAUUSD"
        self._connected = False

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
            equity=1002.0,
            margin_free=900.0,
            leverage=500,
            is_demo=True,
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
            time_utc=self._clock.now_utc() - timedelta(seconds=1),
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
            "regime": {
                "method": "both",
                "train_window_bars": 500,
                "atr_percentile_window": 100,
                "retrain_every_bars": 50,
                "n_states": 4,
            },
        }
    )


def _market_series() -> dict[Timeframe, tuple[Candle, ...]]:
    return {
        Timeframe.M5: make_candles(Timeframe.M5, 900, end_utc=NOW, drift=0.3, wave=1.0, noise=0.8),
        Timeframe.M15: make_candles(Timeframe.M15, 400, end_utc=NOW, drift=0.15, noise=0.6),
        Timeframe.H1: make_candles(Timeframe.H1, 600, end_utc=NOW, drift=0.1, noise=0.6),
        Timeframe.H4: make_candles(Timeframe.H4, 400, end_utc=NOW, drift=0.1, noise=0.6),
        Timeframe.D1: make_candles(Timeframe.D1, 300, end_utc=NOW, drift=0.1, noise=0.6),
    }


def test_market_analyzer_fits_and_tracks_new_bars(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    broker = FakeBroker(clock, _market_series())
    analyzer = MarketAnalyzer(config=_config(tmp_path), broker=broker, clock=clock)

    info = analyzer.warm_up()
    assert info is not None
    assert analyzer.detector.is_fitted

    first = analyzer.analyze(NOW)
    assert first.snapshot.decision_timeframe is Timeframe.M5
    assert first.snapshot.bar_time_utc == broker.series[Timeframe.M5][-1].open_time_utc
    assert first.snapshot.session is Session.NEW_YORK
    assert first.regime.label in set(Regime)
    assert first.snapshot.model_versions
    assert first.snapshot.snapshot_hash

    last = broker.series[Timeframe.M5][-1]
    new_bar = Candle(
        open_time_utc=last.close_time_utc,
        close_time_utc=last.close_time_utc + _DURATIONS[Timeframe.M5],
        open=last.close,
        high=last.close + 1.5,
        low=last.close - 1.0,
        close=last.close + 0.8,
        tick_volume=120,
        spread_points=16.0,
        is_closed=True,
        received_at_utc=last.close_time_utc + _DURATIONS[Timeframe.M5],
        source="TEST",
    )
    broker.series[Timeframe.M5] = (*broker.series[Timeframe.M5], new_bar)
    clock._now = NOW + _DURATIONS[Timeframe.M5]

    second = analyzer.analyze(clock.now_utc())
    assert second.snapshot.bar_time_utc == new_bar.open_time_utc
    assert second.snapshot.snapshot_hash != first.snapshot.snapshot_hash


# -- integración con DataService (analizador falso, sin MT5) ------------------


def _snapshot(now: datetime, digest: str) -> ContextSnapshot:
    return ContextSnapshot(
        decision_time_utc=now,
        data_cutoff_utc=now,
        bar_time_utc=now - _DURATIONS[Timeframe.M5],
        decision_timeframe=Timeframe.M5,
        bias={},
        structure=StructureState(
            swings=(),
            levels=(),
            prev_day_high=None,
            prev_day_low=None,
            prev_day_range=None,
            day_open=None,
            range_position=None,
            nearest_level_distance=None,
        ),
        timing=TimingState(
            atr=1.5,
            atr_percentile=0.5,
            spread_points=15.0,
            last_bar_range=1.2,
            momentum=0.1,
            volatility_ratio=1.0,
        ),
        session=Session.LONDON,
        freshness={},
        snapshot_hash=digest,
        config_hash="cfg",
        model_versions={"stub": "v1"},
    )


def _regime(label: Regime, now: datetime) -> RegimeState:
    return RegimeState(
        label=label,
        probs=RegimeProbabilities(
            probs={regime: (0.7 if regime is label else 0.1) for regime in Regime},
            confidence=0.7,
            method="stub",
        ),
        confidence=0.7,
        confirmed_bars=3,
        decided_at_utc=now,
    )


class StubAnalyzer:
    def __init__(self, labels: list[Regime]) -> None:
        self._labels = deque(labels)
        self.calls = 0

    def warm_up(self) -> None:
        return None

    def analyze(self, now_utc: datetime | None = None) -> AnalysisResult:
        label = self._labels.popleft() if len(self._labels) > 1 else self._labels[0]
        self.calls += 1
        now = now_utc or NOW
        return AnalysisResult(
            snapshot=_snapshot(now, f"hash-{self.calls}"),
            regime=_regime(label, now),
            model_info=None,
            retrained=False,
        )


def _flat_series(timeframe: Timeframe, count: int) -> tuple[Candle, ...]:
    duration = _DURATIONS[timeframe]
    last_close = NOW - duration
    candles: list[Candle] = []
    for index in range(count):
        close = last_close - duration * (count - 1 - index)
        candles.append(
            Candle(
                open_time_utc=close - duration,
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
    return tuple(candles)


def test_data_service_publishes_context_and_regime_changes(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    broker = FakeBroker(
        clock, {timeframe: _flat_series(timeframe, 10) for timeframe in Timeframe}
    )
    config = AppConfig.model_validate(
        {
            "app": {
                "db_path": str(tmp_path / "state.db"),
                "data_dir": str(tmp_path / "data"),
                "log_dir": str(tmp_path / "logs"),
            },
            "data": {"warmup_bars": {timeframe.value: 5 for timeframe in Timeframe}},
        }
    )
    store = SqliteStateStore(tmp_path / "state.db", clock)
    stub = StubAnalyzer([Regime.TREND_UP, Regime.TREND_DOWN])
    service = DataService(
        config=config, broker=broker, store=store, clock=clock, analyzer=stub  # type: ignore[arg-type]
    )

    service.startup()
    with sqlite3.connect(tmp_path / "state.db") as conn:
        context_json, regime_json = conn.execute(
            "SELECT context_json, regime_json FROM bot_state"
        ).fetchone()
    context = json.loads(context_json)
    assert context["snapshot"]["snapshot_hash"] == "hash-1"
    assert context["session"] == "LONDON"
    assert json.loads(regime_json)["label"] == "TREND_UP"

    newest = broker.series[Timeframe.M5][-1]
    broker.series[Timeframe.M5] = (
        *broker.series[Timeframe.M5],
        Candle(
            open_time_utc=newest.close_time_utc,
            close_time_utc=newest.close_time_utc + _DURATIONS[Timeframe.M5],
            open=newest.close,
            high=newest.close + 1.0,
            low=newest.close - 1.0,
            close=newest.close + 0.5,
            tick_volume=110,
            spread_points=16.0,
            is_closed=True,
            received_at_utc=newest.close_time_utc + _DURATIONS[Timeframe.M5],
            source="TEST",
        ),
    )
    clock._now = NOW + _DURATIONS[Timeframe.M5]
    result = service.poll_once()

    assert any(timeframe is Timeframe.M5 for timeframe, _ in result.new_bars)
    assert stub.calls == 2
    with sqlite3.connect(tmp_path / "state.db") as conn:
        regime_json = conn.execute("SELECT regime_json FROM bot_state").fetchone()[0]
        changes = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'REGIME_CHANGE'"
        ).fetchone()[0]
    assert json.loads(regime_json)["label"] == "TREND_DOWN"
    assert changes == 1
