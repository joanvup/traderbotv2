"""Núcleo de decisión compartido por el bot en vivo y el backtest.

Selector → playbook → filtros previos. Es una función pura del contexto, el
régimen, la cuenta y la especificación del símbolo: el mismo código decide en
vivo y en simulación, de modo que el backtest mide exactamente la lógica que se
ejecuta en cuenta demo.
"""

from __future__ import annotations

from collections.abc import Mapping

from goldbot.domain.interfaces import Playbook, PlaybookSelector, PreTradeFilters
from goldbot.domain.types import (
    AccountState,
    ContextSnapshot,
    PolicyDecision,
    RegimeState,
    SymbolSpec,
)


class DecisionCore:
    """Compone selección, evaluación del playbook y filtros en una sola decisión."""

    def __init__(
        self,
        *,
        selector: PlaybookSelector,
        playbooks: Mapping[str, Playbook],
        filters: PreTradeFilters,
    ) -> None:
        self._selector = selector
        self._playbooks = dict(playbooks)
        self._filters = filters

    def decide(
        self,
        ctx: ContextSnapshot,
        regime: RegimeState,
        account: AccountState,
        spec: SymbolSpec,
    ) -> PolicyDecision:
        """Devuelve la acción final junto con la señal y el detalle de filtros."""
        selection = self._selector.select(ctx, regime)
        if selection.fallback_no_trade:
            return PolicyDecision(
                selection=selection,
                signal=None,
                filters=None,
                action="NO_TRADE",
                rationale=selection.rationale,
            )

        playbook = self._playbooks.get(selection.playbook_id)
        if playbook is None or not getattr(playbook, "enabled", True):
            return PolicyDecision(
                selection=selection,
                signal=None,
                filters=None,
                action="NO_TRADE",
                rationale=f"playbook {selection.playbook_id} no disponible o desactivado",
            )

        signal = playbook.evaluate(ctx, regime)
        if signal is None:
            return PolicyDecision(
                selection=selection,
                signal=None,
                filters=None,
                action="NO_SETUP",
                rationale=f"{selection.playbook_id} seleccionado pero sin setup válido",
            )

        outcome = self._filters.evaluate(ctx, signal, account, spec)
        if outcome.blocked:
            return PolicyDecision(
                selection=selection,
                signal=signal,
                filters=outcome,
                action="BLOCKED",
                rationale=outcome.primary_reason or "filtro bloqueante",
            )
        return PolicyDecision(
            selection=selection,
            signal=signal,
            filters=outcome,
            action="ENTER",
            rationale=signal.rationale,
        )
