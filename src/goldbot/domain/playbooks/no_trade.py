"""Playbook nulo: abstenerse es una decisión explícita y de primera clase."""

from __future__ import annotations

from goldbot.domain.playbooks.base import BasePlaybook
from goldbot.domain.types import (
    ContextSnapshot,
    ManageAction,
    ManageKind,
    PositionState,
    RegimeState,
    Signal,
)


class NoTradePlaybook(BasePlaybook):
    """Nunca genera señal; su rationale explica la abstención en el DecisionLog."""

    playbook_id = "no_trade"

    def evaluate(self, ctx: ContextSnapshot, regime: RegimeState) -> Signal | None:
        return None

    def manage(
        self,
        position: PositionState,
        ctx: ContextSnapshot,
        regime: RegimeState,
    ) -> ManageAction:
        return ManageAction(kind=ManageKind.HOLD, reason="NoTrade no gestiona posiciones abiertas")
