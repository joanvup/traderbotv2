"""Backtest: feed causal, ejecución conservadora, métricas y contrato del motor."""

from __future__ import annotations

from datetime import timedelta

import pytest
from _snapshots import NOW
from _synthetic import market_candles

from goldbot.backtest.engine import BacktestConfig, BacktestEngine, _OpenPosition
from goldbot.backtest.feed import HistoricalFeed
from goldbot.backtest.metrics import compute_metrics
from goldbot.config import AppConfig
from goldbot.domain.playbooks.base import BasePlaybook
from goldbot.domain.playbooks.no_trade import NoTradePlaybook
from goldbot.domain.types import (
    Candle,
    ExitReason,
    ExitReasonSource,
    PlaybookSettings,
    Regime,
    Side,
    Signal,
    Timeframe,
    TradeRecord,
)
from goldbot.infra.calendar import StaticEconomicCalendar


class _AlwaysPlaybook(BasePlaybook):
    playbook_id = "always"
    supported_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.RANGE, Regime.HIGH_VOLATILITY}
    )

    def evaluate(self, ctx, regime):
        atr = self.atr(ctx)
        entry = float(ctx.last_price)
        if atr <= 0.0 or entry <= 0.0:
            return None
        stop = entry - 1.0 * atr
        target = entry + 2.0 * atr
        return self.make_signal(
            ctx,
            regime,
            side=Side.BUY,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            confidence=max(regime.confidence, 0.5),
            rationale="entrada de test mecánica",
            invalidation_price=None,
        )


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "regime": {
                "method": "rules",
                "confidence_min": 0.3,
                "train_window_bars": 500,
                "hysteresis_bars": 1,
            },
            "data": {
                "warmup_bars": {"M5": 600, "M15": 400, "H1": 300, "H4": 100, "D1": 60},
            },
            "sessions": {"entries": ["00:00-23:59"], "avoid_rollover": False},
            "management": {"time_stop_bars": 24},
            "filters": {"news_window_before_min": 0, "news_window_after_min": 0},
        }
    )


def _candles():
    return market_candles(end_utc=NOW, m5_bars=1_200)


def _calendar(candles):
    decision = candles[Timeframe.M5]
    return StaticEconomicCalendar(
        (),
        coverage_start_utc=decision[0].open_time_utc - timedelta(days=1),
        coverage_end_utc=decision[-1].close_time_utc + timedelta(days=1),
    )


def _playbooks():
    return {
        "always": _AlwaysPlaybook(
            PlaybookSettings(enabled=True, min_confidence=0.0, params={})
        ),
        "no_trade": NoTradePlaybook(PlaybookSettings()),
    }


def _engine() -> BacktestEngine:
    candles = _candles()
    train_end = candles[Timeframe.M5][600].close_time_utc
    return BacktestEngine(
        config=_config(),
        candles=candles,
        calendar=_calendar(candles),
        train_end_utc=train_end,
        playbooks=_playbooks(),
        backtest=BacktestConfig(initial_balance=1_000.0, slippage_points=2),
    )


def test_feed_only_returns_candles_closed_before_the_cutoff() -> None:
    candles = _candles()
    feed = HistoricalFeed(candles, limits={Timeframe.M5: 100})
    cutoff = candles[Timeframe.M5][-1].close_time_utc
    slice_ = feed.slice_at(cutoff)
    assert len(slice_[Timeframe.M5]) <= 100
    assert all(candle.close_time_utc <= cutoff for candle in slice_[Timeframe.M5])
    earlier = feed.slice_at(candles[Timeframe.M5][-10].close_time_utc)
    assert earlier[Timeframe.M5][-1].close_time_utc <= candles[Timeframe.M5][-10].close_time_utc


def test_stop_is_checked_before_target_on_the_same_bar() -> None:
    engine = _engine()
    position = _OpenPosition(
        signal=Signal(
            signal_id="test",
            playbook_id="always",
            side=Side.BUY,
            entry_price=2_400.0,
            stop_loss=2_395.0,
            take_profit=2_410.0,
            risk_distance=5.0,
            confidence=0.9,
            regime_at_signal=Regime.TREND_UP,
            created_at_utc=NOW,
            valid_until_bar_utc=None,
            invalidation_price=None,
            rationale="",
            inputs_hash=None,
        ),
        entry_price=2_400.0,
        volume=0.01,
        risk_money=5.0,
        opened_at_utc=NOW,
    )
    bar = Candle(
        open_time_utc=NOW,
        close_time_utc=NOW + timedelta(minutes=5),
        open=2_400.0,
        high=2_412.0,
        low=2_394.0,
        close=2_405.0,
        tick_volume=1,
        spread_points=15.0,
        is_closed=True,
        received_at_utc=NOW,
        source="TEST",
    )
    exit_price, reason = engine._exit_price(bar, position, 0.02)
    assert reason is ExitReason.SL
    assert exit_price == pytest.approx(2_395.0 - 0.02)


def test_engine_produces_auditable_trades_without_look_ahead() -> None:
    engine = _engine()
    result = engine.run()

    assert result.metrics.trades >= 1
    assert len(result.equity_curve) > 0
    assert result.decisions == sum(result.policy_counts.values())
    assert result.policy_counts.get("ENTER", 0) >= result.metrics.trades
    assert sum(result.regime_counts.values()) + result.policy_counts.get("NO_DATA", 0) == (
        result.decisions
    )
    assert sum(result.playbook_counts.values()) == result.decisions - result.policy_counts.get(
        "NO_DATA", 0
    )
    assert result.playbook_counts.get("always", 0) > 0
    assert result.start_utc is not None and result.end_utc is not None
    assert result.start_utc > result.end_utc - timedelta(days=3)

    for trade in result.trades:
        assert trade.close_time_utc >= trade.open_time_utc
        assert trade.exit_reason in {ExitReason.SL, ExitReason.TP, ExitReason.TIME}
        assert trade.r_multiple is not None
        assert trade.net_profit == pytest.approx(
            (trade.exit_price - trade.entry_price)
            * (1.0 if trade.side is Side.BUY else -1.0)
            * 100.0
            * trade.volume
            - trade.commission
        )
    assert all(value >= 0.0 for _, value in result.equity_curve)


def test_metrics_compute_expectancy_profit_factor_and_breakdowns() -> None:
    trades = (
        _trade(r=2.0, profit=8.0, regime=Regime.TREND_UP, playbook="trend_pullback", day=1),
        _trade(r=-1.0, profit=-4.0, regime=Regime.RANGE, playbook="range_fade", day=2),
        _trade(r=2.0, profit=8.0, regime=Regime.TREND_UP, playbook="trend_pullback", day=3),
        _trade(r=9.0, profit=36.0, regime=Regime.TREND_DOWN, playbook="breakout", day=4),
    )
    metrics = compute_metrics(trades, initial_balance=1_000.0, total_days=10)
    assert metrics.trades == 4
    assert metrics.expectancy_r == pytest.approx(3.0)
    assert metrics.profit_factor == pytest.approx(52.0 / 4.0)
    assert metrics.win_rate == pytest.approx(75.0)
    assert metrics.by_regime["TREND_UP"] == pytest.approx(2.0)
    assert metrics.by_playbook["trend_pullback"] == pytest.approx(2.0)
    assert metrics.no_trade_days_pct == pytest.approx(60.0)
    assert metrics.r_distribution[">=2"] == 3
    assert metrics.max_drawdown_usd >= 4.0


def _trade(
    *,
    r: float,
    profit: float,
    regime: Regime,
    playbook: str,
    day: int,
) -> TradeRecord:
    opened = NOW + timedelta(days=day)
    return TradeRecord(
        position_id=day,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.01,
        entry_price=2_400.0,
        exit_price=2_400.0 + profit,
        sl=2_396.0,
        tp=2_408.0,
        open_time_utc=opened,
        close_time_utc=opened + timedelta(hours=1),
        entry_deal_id=None,
        close_deal_id=None,
        commission=0.07,
        swap=0.0,
        net_profit=profit,
        r_multiple=r,
        duration_s=3_600,
        exit_reason=ExitReason.TP if profit > 0 else ExitReason.SL,
        exit_reason_source=ExitReasonSource.TP if profit > 0 else ExitReasonSource.SL,
        playbook_id=playbook,
        regime_at_entry=regime,
        decision_id_entry="d",
    )
