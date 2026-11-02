"""Interfaz base de los playbooks y utilidades comunes de entrada."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from typing import ClassVar

from goldbot.domain.types import (
    Bias,
    ContextSnapshot,
    ManageAction,
    ManageKind,
    PlaybookSettings,
    PositionState,
    Regime,
    RegimeState,
    Side,
    Signal,
    Timeframe,
)

_TF_DELTA: dict[Timeframe, timedelta] = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


class BasePlaybook:
    """Un playbook evalúa el contexto y gestiona sus propias posiciones.

    `evaluate` devuelve una señal o None (abstenerse es una respuesta válida). Las
    entradas usan solo el snapshot: no hay acceso a velas posteriores ni a la cuenta.
    """

    playbook_id: ClassVar[str] = ""
    supported_regimes: ClassVar[frozenset[Regime]] = frozenset()
    defaults: ClassVar[Mapping[str, float]] = {}

    def __init__(self, settings: PlaybookSettings) -> None:
        self._settings = settings
        self._params: dict[str, float] = {**self.defaults, **dict(settings.params)}

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @property
    def min_confidence(self) -> float:
        return self._settings.min_confidence

    def param(self, name: str, default: float = 0.0) -> float:
        return float(self._params.get(name, default))

    def evaluate(self, ctx: ContextSnapshot, regime: RegimeState) -> Signal | None:
        """Devuelve una señal o None para abstenerse (primera clase)."""
        raise NotImplementedError(f"evaluate no implementado para {self.playbook_id}")

    def manage(
        self,
        position: PositionState,
        ctx: ContextSnapshot,
        regime: RegimeState,
    ) -> ManageAction:
        """La gestión fina vive en el TradeManager (Fase 4); aquí se mantiene la posición."""
        return ManageAction(
            kind=ManageKind.HOLD,
            reason="gestión delegada al TradeManager",
            decided_at_utc=ctx.decision_time_utc,
        )

    # -- utilidades de entrada ------------------------------------------------

    @staticmethod
    def bias(ctx: ContextSnapshot, timeframe: Timeframe) -> Bias | None:
        return ctx.bias.get(timeframe)

    @staticmethod
    def atr(ctx: ContextSnapshot) -> float:
        return max(0.0, float(ctx.timing.atr))

    def aligned(self, ctx: ContextSnapshot, side: Side, *, min_strength: float) -> bool:
        """Exige H1 a favor y que D1/H4 no estén en contra (evita operar contra el fondo)."""
        reference = self.bias(ctx, Timeframe.H1)
        if reference is None or reference.direction is not side:
            return False
        if reference.strength < min_strength:
            return False
        opposite = Side.SELL if side is Side.BUY else Side.BUY
        for timeframe in (Timeframe.D1, Timeframe.H4):
            higher = self.bias(ctx, timeframe)
            if higher is not None and higher.direction is opposite:
                return False
        return True

    def structural_stop(
        self,
        ctx: ContextSnapshot,
        side: Side,
        *,
        entry: float,
        atr: float,
        sl_atr: float,
        max_stop_atr: float,
    ) -> float:
        """Stop ATR, acercado a un swing relevante si queda dentro de límites razonables."""
        fallback = entry - sl_atr * atr if side is Side.BUY else entry + sl_atr * atr
        minimum = 0.5 * atr
        maximum = max_stop_atr * atr
        candidates: list[float] = []
        for swing in ctx.structure.swings:
            distance = (
                entry - swing.price if side is Side.BUY else swing.price - entry
            )
            if minimum <= distance <= maximum:
                price = swing.price - 0.1 * atr if side is Side.BUY else swing.price + 0.1 * atr
                candidates.append(price)
        if not candidates:
            return fallback
        # Para BUY el stop más cercano es el precio más alto; para SELL, el más bajo.
        return max(candidates) if side is Side.BUY else min(candidates)

    def make_signal(
        self,
        ctx: ContextSnapshot,
        regime: RegimeState,
        *,
        side: Side,
        entry: float,
        stop_loss: float,
        take_profit: float,
        confidence: float,
        rationale: str,
        invalidation_price: float | None,
    ) -> Signal | None:
        risk = abs(entry - stop_loss)
        if risk <= 0.0 or abs(take_profit - entry) <= 0.0:
            return None
        if side is Side.BUY and not (stop_loss < entry < take_profit):
            return None
        if side is Side.SELL and not (take_profit < entry < stop_loss):
            return None
        digest = hashlib.sha1(
            (
                f"{self.playbook_id}|{ctx.snapshot_hash}|{side.value}|"
                f"{entry:.5f}|{stop_loss:.5f}|{take_profit:.5f}"
            ).encode()
        ).hexdigest()[:16]
        duration = _TF_DELTA[ctx.decision_timeframe]
        return Signal(
            signal_id=f"{self.playbook_id}-{digest}",
            playbook_id=self.playbook_id,
            side=side,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_distance=risk,
            confidence=confidence,
            regime_at_signal=regime.label,
            created_at_utc=ctx.decision_time_utc,
            # La entrada solo es válida en la siguiente vela cerrada.
            valid_until_bar_utc=ctx.bar_time_utc + 2 * duration,
            invalidation_price=invalidation_price,
            rationale=rationale,
            inputs_hash=ctx.snapshot_hash,
        )
