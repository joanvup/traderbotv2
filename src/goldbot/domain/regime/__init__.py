"""Detectores de régimen de mercado."""

from __future__ import annotations

from goldbot.domain.regime.base import BaseRegimeDetector
from goldbot.domain.regime.blend import BlendedRegimeDetector
from goldbot.domain.regime.hmm import HmmRegimeDetector
from goldbot.domain.regime.rules import RulesRegimeDetector
from goldbot.domain.types import RegimeSettings


def create_regime_detector(settings: RegimeSettings) -> BaseRegimeDetector:
    """Fábrica según `regime.method`; todos comparten el contrato y la histéresis."""
    if settings.method == "rules":
        return RulesRegimeDetector(settings)
    if settings.method == "hmm":
        return HmmRegimeDetector(settings)
    if settings.method == "both":
        return BlendedRegimeDetector(settings)
    raise ValueError(f"método de régimen desconocido: {settings.method!r}")


__all__ = [
    "BaseRegimeDetector",
    "BlendedRegimeDetector",
    "HmmRegimeDetector",
    "RulesRegimeDetector",
    "create_regime_detector",
]
