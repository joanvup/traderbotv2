"""Playbook de reversión en extremos de rango, solo con régimen RANGE de alta confianza."""

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


class RangeFadePlaybook(BasePlaybook):
    """Busca agotamiento en los bordes del rango; nunca promedia a la baja."""

    playbook_id = "range_fade"
    supported_regimes = frozenset({Regime.RANGE})
    defaults = {
        "sl_atr": 1.0,
        "rr": 1.5,
        "entry_position": 0.2,
        "veto_bias_strength": 0.4,
    }

    def evaluate(self, ctx: ContextSnapshot, regime: RegimeState) -> Signal | None:
        if regime.label is not Regime.RANGE:
            return None
        if regime.confidence < self.min_confidence:
            return None
        atr = self.atr(ctx)
        entry = float(ctx.last_price)
        position = ctx.structure.range_position
        if atr <= 0.0 or entry <= 0.0 or position is None:
            return None

        edge = self.param("entry_position", 0.2)
        if position <= edge:
            side = Side.BUY
        elif position >= 1.0 - edge:
            side = Side.SELL
        else:
            return None

        # Un rango solo se desvanece si el fondo no está empujando en contra.
        h1 = self.bias(ctx, Timeframe.H1)
        opposite = Side.SELL if side is Side.BUY else Side.BUY
        if (
            h1 is not None
            and h1.direction is opposite
            and h1.strength >= self.param("veto_bias_strength", 0.4)
        ):
            return None

        sl_atr = self.param("sl_atr", 1.0)
        stop = entry - sl_atr * atr if side is Side.BUY else entry + sl_atr * atr
        risk = abs(entry - stop)
        take_profit = self._target(ctx, side, entry, risk)
        invalidation = (
            ctx.structure.prev_day_low if side is Side.BUY else ctx.structure.prev_day_high
        )
        rationale = (
            f"Rango {regime.label.value} {regime.confidence:.2f}; precio en el "
            f"{position:.0%} del rango del día previo, entrada {side.value} "
            f"contra el extremo"
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
            invalidation_price=invalidation,
        )

    def _target(
        self, ctx: ContextSnapshot, side: Side, entry: float, risk: float
    ) -> float:
        """Busca el centro del rango; si queda demasiado cerca, usa el múltiplo R."""
        reward = self.param("rr", 1.5) * risk
        fallback = entry + reward if side is Side.BUY else entry - reward
        low = ctx.structure.prev_day_low
        high = ctx.structure.prev_day_high
        if low is None or high is None:
            return fallback
        middle = (low + high) / 2.0
        distance = middle - entry if side is Side.BUY else entry - middle
        if distance >= 0.5 * risk:
            return middle
        return fallback
