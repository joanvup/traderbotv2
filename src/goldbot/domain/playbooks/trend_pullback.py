"""Playbook de continuación de tendencia con entrada en retroceso a zona de valor."""

from __future__ import annotations

from goldbot.domain.playbooks.base import BasePlaybook
from goldbot.domain.types import (
    ContextSnapshot,
    Regime,
    RegimeState,
    Side,
    Signal,
    Timeframe,
)


class TrendPullbackPlaybook(BasePlaybook):
    """Solo opera a favor del sesgo D1/H4 y con régimen de tendencia confirmado."""

    playbook_id = "trend_pullback"
    supported_regimes = frozenset({Regime.TREND_UP, Regime.TREND_DOWN})
    defaults = {
        "pullback_atr": 0.8,
        "sl_atr": 1.5,
        "rr": 2.0,
        "min_bias_strength": 0.2,
        "max_stop_atr": 2.5,
    }

    def evaluate(self, ctx: ContextSnapshot, regime: RegimeState) -> Signal | None:
        if regime.label not in self.supported_regimes:
            return None
        if regime.confidence < self.min_confidence:
            return None
        side = Side.BUY if regime.label is Regime.TREND_UP else Side.SELL
        atr = self.atr(ctx)
        entry = float(ctx.last_price)
        if atr <= 0.0 or entry <= 0.0:
            return None
        if not self.aligned(ctx, side, min_strength=self.param("min_bias_strength", 0.2)):
            return None

        value = self.bias(ctx, Timeframe.M15)
        if value is None:
            return None
        pullback = self.param("pullback_atr", 0.8) * atr
        if side is Side.BUY:
            if entry > value.ema_fast + pullback:
                return None  # extendido: sin retroceso no hay entrada
            if entry < value.ema_slow - 0.25 * atr:
                return None  # perdió la EMA lenta: la tendencia ya no sostiene
            if ctx.timing.momentum <= 0.0:
                return None
            rationale = (
                f"Tendencia alcista {regime.label.value} ({regime.confidence:.2f}); "
                f"retroceso a EMA20 M15 ({value.ema_fast:.2f}) con momentum "
                f"{ctx.timing.momentum:+.2f} ATR"
            )
        else:
            if entry < value.ema_fast - pullback:
                return None
            if entry > value.ema_slow + 0.25 * atr:
                return None
            if ctx.timing.momentum >= 0.0:
                return None
            rationale = (
                f"Tendencia bajista {regime.label.value} ({regime.confidence:.2f}); "
                f"retroceso a EMA20 M15 ({value.ema_fast:.2f}) con momentum "
                f"{ctx.timing.momentum:+.2f} ATR"
            )

        stop = self.structural_stop(
            ctx,
            side,
            entry=entry,
            atr=atr,
            sl_atr=self.param("sl_atr", 1.5),
            max_stop_atr=self.param("max_stop_atr", 2.5),
        )
        risk = abs(entry - stop)
        reward = self.param("rr", 2.0) * risk
        take_profit = entry + reward if side is Side.BUY else entry - reward
        return self.make_signal(
            ctx,
            regime,
            side=side,
            entry=entry,
            stop_loss=stop,
            take_profit=take_profit,
            confidence=regime.confidence,
            rationale=rationale,
            invalidation_price=value.ema_slow,
        )

