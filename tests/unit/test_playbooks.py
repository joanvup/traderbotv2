"""Playbooks: condiciones de entrada, stops estructurales y abstención."""

from __future__ import annotations

import pytest
from _snapshots import NOW, bias, regime, snapshot, structure, timing

from goldbot.domain.playbooks.breakout import BreakoutPlaybook
from goldbot.domain.playbooks.no_trade import NoTradePlaybook
from goldbot.domain.playbooks.range_fade import RangeFadePlaybook
from goldbot.domain.playbooks.trend_pullback import TrendPullbackPlaybook
from goldbot.domain.types import (
    Level,
    PlaybookSettings,
    Regime,
    Side,
    Swing,
    Timeframe,
)

_SETTINGS = PlaybookSettings(enabled=True, min_confidence=0.6)


def _trend_snapshot(
    *,
    side: Side,
    entry: float,
    momentum: float,
    swings: tuple[Swing, ...] = (),
):
    ema_fast = 2_400.0
    ema_slow = 2_390.0 if side is Side.BUY else 2_410.0
    bias_map = {
        Timeframe.M15: bias(
            Timeframe.M15,
            side,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
        ),
        Timeframe.H1: bias(Timeframe.H1, side, strength=0.6),
        Timeframe.H4: bias(Timeframe.H4, None),
        Timeframe.D1: bias(Timeframe.D1, None),
    }
    return snapshot(
        bias_map=bias_map,
        timing_state=timing(atr=4.0, momentum=momentum),
        structure_state=structure(swings=swings, range_position=0.5),
        last_price=entry,
    )


def test_trend_pullback_buys_the_retest_and_sets_structural_brackets() -> None:
    playbook = TrendPullbackPlaybook(_SETTINGS)
    ctx = _trend_snapshot(side=Side.BUY, entry=2_400.0, momentum=0.8)
    result = playbook.evaluate(ctx, regime(Regime.TREND_UP, confidence=0.8))

    assert result is not None
    assert result.side is Side.BUY
    assert result.playbook_id == "trend_pullback"
    assert result.stop_loss < result.entry_price < result.take_profit
    assert result.stop_loss == pytest.approx(2_394.0)
    assert result.take_profit == pytest.approx(2_412.0)
    assert result.invalidation_price == pytest.approx(2_390.0)
    assert result.valid_until_bar_utc > result.created_at_utc
    assert "retroceso" in result.rationale


def test_trend_pullback_is_deterministic_for_the_same_snapshot() -> None:
    playbook = TrendPullbackPlaybook(_SETTINGS)
    ctx = _trend_snapshot(side=Side.BUY, entry=2_400.0, momentum=0.8)
    first = playbook.evaluate(ctx, regime(Regime.TREND_UP, confidence=0.8))
    second = playbook.evaluate(ctx, regime(Regime.TREND_UP, confidence=0.8))
    assert first is not None and second is not None
    assert first.signal_id == second.signal_id


def test_trend_pullback_skips_extended_price_and_low_confidence() -> None:
    playbook = TrendPullbackPlaybook(_SETTINGS)
    extended = _trend_snapshot(side=Side.BUY, entry=2_410.0, momentum=0.8)
    assert playbook.evaluate(extended, regime(Regime.TREND_UP, confidence=0.8)) is None
    assert playbook.evaluate(extended, regime(Regime.TREND_UP, confidence=0.5)) is None
    assert playbook.evaluate(extended, regime(Regime.RANGE, confidence=0.9)) is None


def test_trend_pullback_sells_a_bearish_retest() -> None:
    playbook = TrendPullbackPlaybook(_SETTINGS)
    ctx = _trend_snapshot(side=Side.SELL, entry=2_400.0, momentum=-0.8)
    result = playbook.evaluate(ctx, regime(Regime.TREND_DOWN, confidence=0.8))
    assert result is not None
    assert result.side is Side.SELL
    assert result.take_profit < result.entry_price < result.stop_loss


def test_structural_stop_uses_the_nearest_valid_swing() -> None:
    playbook = TrendPullbackPlaybook(_SETTINGS)
    swing = Swing(
        kind="LOW",
        price=2_396.5,
        bar_time_utc=NOW,
        bars_ago=3,
    )
    ctx = _trend_snapshot(side=Side.BUY, entry=2_400.0, momentum=0.8, swings=(swing,))
    result = playbook.evaluate(ctx, regime(Regime.TREND_UP, confidence=0.8))
    assert result is not None
    assert result.stop_loss == pytest.approx(2_396.5 - 0.4)


def test_breakout_requires_closed_bar_out_of_the_previous_range() -> None:
    playbook = BreakoutPlaybook(_SETTINGS)
    ctx = snapshot(
        structure_state=structure(prev_day_high=2_400.0, prev_day_low=2_380.0),
        timing_state=timing(atr=4.0, last_bar_range=6.0, atr_percentile=0.7),
        last_price=2_405.0,
    )
    result = playbook.evaluate(ctx, regime(Regime.TREND_UP, confidence=0.8))
    assert result is not None
    assert result.side is Side.BUY
    assert result.stop_loss < result.entry_price < result.take_profit
    assert "Ruptura" in result.rationale
    assert result.invalidation_price == pytest.approx(2_400.0)


def test_breakout_rejects_a_quiet_bar_inside_the_range() -> None:
    playbook = BreakoutPlaybook(_SETTINGS)
    ctx = snapshot(
        structure_state=structure(prev_day_high=2_400.0, prev_day_low=2_380.0),
        timing_state=timing(atr=4.0, last_bar_range=1.0, atr_percentile=0.7),
        last_price=2_405.0,
    )
    assert playbook.evaluate(ctx, regime(Regime.TREND_UP, confidence=0.8)) is None


def test_breakout_uses_a_level_from_structure_when_there_is_no_previous_day_break() -> None:
    playbook = BreakoutPlaybook(_SETTINGS)
    level = Level(
        kind="RESISTANCE",
        price=2_403.0,
        strength=0.8,
        touches=3,
        bar_time_utc=NOW,
    )
    ctx = snapshot(
        structure_state=structure(levels=(level,), prev_day_high=None, prev_day_low=None),
        timing_state=timing(atr=4.0, last_bar_range=6.0, atr_percentile=0.7),
        last_price=2_405.0,
    )
    result = playbook.evaluate(ctx, regime(Regime.HIGH_VOLATILITY, confidence=0.8))
    assert result is not None
    assert result.invalidation_price == pytest.approx(2_403.0)


def test_range_fade_buys_the_lower_edge_with_midpoint_target() -> None:
    playbook = RangeFadePlaybook(PlaybookSettings(enabled=True, min_confidence=0.7))
    ctx = snapshot(
        bias_map={Timeframe.H1: bias(Timeframe.H1, None)},
        structure_state=structure(
            prev_day_high=2_420.0, prev_day_low=2_380.0, range_position=0.1
        ),
        timing_state=timing(atr=4.0, momentum=-0.2),
        last_price=2_385.0,
    )
    result = playbook.evaluate(ctx, regime(Regime.RANGE, confidence=0.8))
    assert result is not None
    assert result.side is Side.BUY
    assert result.stop_loss == pytest.approx(2_381.0)
    assert result.take_profit == pytest.approx(2_400.0)
    assert result.invalidation_price == pytest.approx(2_380.0)


def test_range_fade_vetoes_a_strong_opposite_higher_timeframe_bias() -> None:
    playbook = RangeFadePlaybook(PlaybookSettings(enabled=True, min_confidence=0.7))
    ctx = snapshot(
        bias_map={Timeframe.H1: bias(Timeframe.H1, Side.SELL, strength=0.6)},
        structure_state=structure(
            prev_day_high=2_420.0, prev_day_low=2_380.0, range_position=0.1
        ),
        last_price=2_385.0,
    )
    assert playbook.evaluate(ctx, regime(Regime.RANGE, confidence=0.8)) is None


def test_no_trade_never_produces_a_signal() -> None:
    playbook = NoTradePlaybook(PlaybookSettings())
    assert playbook.evaluate(snapshot(), regime(Regime.TREND_UP)) is None


def test_make_signal_rejects_incoherent_brackets() -> None:
    playbook = TrendPullbackPlaybook(_SETTINGS)
    ctx = snapshot()
    assert (
        playbook.make_signal(
            ctx,
            regime(Regime.TREND_UP),
            side=Side.BUY,
            entry=2_400.0,
            stop_loss=2_401.0,
            take_profit=2_410.0,
            confidence=0.8,
            rationale="inválida",
            invalidation_price=None,
        )
        is None
    )


def test_base_playbook_holds_the_position_by_default() -> None:
    playbook = NoTradePlaybook(PlaybookSettings())
    action = playbook.manage(
        position=_dummy_position(),
        ctx=snapshot(),
        regime=regime(Regime.RANGE),
    )
    assert action.kind.value == "HOLD"


def _dummy_position():
    from _snapshots import signal as make_signal

    from goldbot.domain.types import PositionState

    base = make_signal()
    return PositionState(
        ticket=1,
        position_id=1,
        side=base.side,
        volume=0.01,
        entry_price=base.entry_price,
        current_price=base.entry_price,
        sl=base.stop_loss,
        tp=base.take_profit,
        pnl_money=0.0,
        initial_risk_price=base.risk_distance,
        r_multiple=0.0,
        playbook_id=base.playbook_id,
        regime_at_entry=base.regime_at_signal,
        decision_id_entry=base.signal_id,
        breakeven_done=False,
        partials_done=False,
        last_action=None,
        opened_at_utc=NOW,
    )
