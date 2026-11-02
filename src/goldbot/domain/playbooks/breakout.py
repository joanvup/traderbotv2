"""Playbook de ruptura de rango con confirmación de volatilidad y cierre de vela."""

from __future__ import annotations

from goldbot.domain.playbooks.base import BasePlaybook
from goldbot.domain.types import (
    ContextSnapshot,
    Level,
    Regime,
    RegimeState,
    Side,
    Signal,
)


class BreakoutPlaybook(BasePlaybook):
    """Exige cierre fuera del rango y expansión de ATR; evita rupturas en rango."""

    playbook_id = "breakout"
    supported_regimes = frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.HIGH_VOLATILITY}
    )
    defaults = {
        "buffer_atr": 0.15,
        "sl_atr": 1.2,
        "rr": 2.0,
        "max_extension_atr": 2.0,
        "min_range_atr": 0.6,
        "min_atr_percentile": 0.5,
        "hv_momentum": 0.25,
    }

    def evaluate(self, ctx: ContextSnapshot, regime: RegimeState) -> Signal | None:
        if regime.label not in self.supported_regimes:
            return None
        if regime.confidence < self.min_confidence:
            return None
        atr = self.atr(ctx)
        entry = float(ctx.last_price)
        if atr <= 0.0 or entry <= 0.0:
            return None

        if regime.label is Regime.TREND_UP:
            side = Side.BUY
        elif regime.label is Regime.TREND_DOWN:
            side = Side.SELL
        else:
            momentum = ctx.timing.momentum
            threshold = self.param("hv_momentum", 0.25)
            if momentum >= threshold:
                side = Side.BUY
            elif momentum <= -threshold:
                side = Side.SELL
            else:
                return None

        if ctx.timing.last_bar_range < self.param("min_range_atr", 0.6) * atr:
            return None
        if ctx.timing.atr_percentile < self.param("min_atr_percentile", 0.5):
            return None

        level = self._broken_level(ctx, side, entry)
        if level is None:
            return None
        buffer = self.param("buffer_atr", 0.15) * atr
        extension = entry - level.price if side is Side.BUY else level.price - entry
        if extension < buffer:
            return None
        if extension > self.param("max_extension_atr", 2.0) * atr:
            return None  # ruptura ya agotada

        stop = self.structural_stop(
            ctx,
            side,
            entry=entry,
            atr=atr,
            sl_atr=self.param("sl_atr", 1.2),
            max_stop_atr=2.5,
        )
        risk = abs(entry - stop)
        reward = self.param("rr", 2.0) * risk
        take_profit = entry + reward if side is Side.BUY else entry - reward
        rationale = (
            f"Ruptura {side.value} del nivel {level.price:.2f} ({level.kind}, "
            f"{level.touches} toques) con rango {ctx.timing.last_bar_range:.2f} y "
            f"percentil ATR {ctx.timing.atr_percentile:.2f}; régimen {regime.label.value} "
            f"{regime.confidence:.2f}"
        )
        return self.make_signal(
            ctx,
            regime,
            side=side,
            entry=entry,
            stop_loss=stop,
            take_profit=take_profit,
            confidence=regime.confidence,
            rationale=rationale,
            invalidation_price=level.price,
        )

    def _broken_level(
        self, ctx: ContextSnapshot, side: Side, entry: float
    ) -> Level | None:
        """Nivel de referencia roto: día previo o swing relevante, siempre por detrás."""
        structure = ctx.structure
        if side is Side.BUY and structure.prev_day_high is not None:
            if entry > structure.prev_day_high:
                return Level(
                    kind="PREV_DAY_HIGH",
                    price=structure.prev_day_high,
                    strength=1.0,
                    touches=1,
                    bar_time_utc=None,
                )
        if side is Side.SELL and structure.prev_day_low is not None:
            if entry < structure.prev_day_low:
                return Level(
                    kind="PREV_DAY_LOW",
                    price=structure.prev_day_low,
                    strength=1.0,
                    touches=1,
                    bar_time_utc=None,
                )

        candidates = [
            level
            for level in structure.levels
            if level.kind in ("SUPPORT", "RESISTANCE")
            and (level.price < entry if side is Side.BUY else level.price > entry)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.price) if side is Side.BUY else min(
            candidates, key=lambda item: item.price
        )
