"""Selección del playbook según las probabilidades del régimen y el contexto."""

from __future__ import annotations

from collections.abc import Mapping

from goldbot.domain.interfaces import Playbook
from goldbot.domain.types import (
    ContextSnapshot,
    Regime,
    RegimeState,
    Selection,
)


class ProbabilityPlaybookSelector:
    """Elige el playbook con mayor puntuación; si nadie supera el umbral, NoTrade."""

    def __init__(
        self,
        playbooks: Mapping[str, Playbook],
        *,
        thresholds: Mapping[str, float] | None = None,
        fallback_id: str = "no_trade",
    ) -> None:
        self._playbooks = dict(playbooks)
        self._thresholds = dict(thresholds or {})
        self._fallback_id = fallback_id

    def select(self, ctx: ContextSnapshot, regime: RegimeState) -> Selection:
        """Puntúa cada playbook compatible con el régimen y aplica el umbral de confianza."""
        scored: dict[str, float] = {}
        for playbook_id, playbook in self._playbooks.items():
            if playbook_id == self._fallback_id:
                continue
            supported: frozenset[Regime] = getattr(playbook, "supported_regimes", frozenset())
            if regime.label is Regime.UNKNOWN or regime.label not in supported:
                scored[playbook_id] = 0.0
                continue
            scored[playbook_id] = float(regime.probs.probs.get(regime.label, 0.0))

        best_id: str | None = None
        best_score = 0.0
        for playbook_id, score in scored.items():
            threshold = self._thresholds.get(
                playbook_id,
                float(getattr(self._playbooks[playbook_id], "min_confidence", 0.6)),
            )
            if score > 0.0 and score >= threshold and score > best_score:
                best_id, best_score = playbook_id, score

        if best_id is None:
            detail = ", ".join(
                f"{playbook_id} {score:.2f}" for playbook_id, score in scored.items()
            )
            rationale = (
                f"sin setup: régimen {regime.label.value} "
                f"({regime.confidence:.2f}); puntuaciones {detail or 'ninguna'}"
            )
            return Selection(
                playbook_id=self._fallback_id,
                scores=scored,
                rationale=rationale,
                fallback_no_trade=True,
            )

        threshold = self._thresholds.get(
            best_id, float(getattr(self._playbooks[best_id], "min_confidence", 0.6))
        )
        alternatives = ", ".join(
            f"{playbook_id} {score:.2f}"
            for playbook_id, score in scored.items()
            if playbook_id != best_id
        )
        rationale = (
            f"{best_id} elegido para {regime.label.value} "
            f"({best_score:.2f} ≥ {threshold:.2f})"
            + (f"; alternativas: {alternatives}" if alternatives else "")
        )
        return Selection(
            playbook_id=best_id,
            scores=scored,
            rationale=rationale,
            fallback_no_trade=False,
        )
