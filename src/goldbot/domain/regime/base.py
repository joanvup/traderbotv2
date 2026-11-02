"""Base común de los detectores de régimen: probabilidades, histéresis y contrato."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from typing import ClassVar

import pandas as pd

from goldbot.domain.types import (
    HysteresisState,
    ModelInfo,
    Regime,
    RegimeProbabilities,
    RegimeSettings,
    RegimeState,
)

REGIME_ORDER: tuple[Regime, ...] = (
    Regime.TREND_UP,
    Regime.TREND_DOWN,
    Regime.RANGE,
    Regime.HIGH_VOLATILITY,
)


def settings_hash(settings: RegimeSettings) -> str:
    payload = json.dumps(asdict(settings), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class HysteresisTracker:
    """Un cambio de régimen exige `bars` confirmaciones consecutivas."""

    def __init__(self, bars: int) -> None:
        self._bars = max(1, bars)
        self.current: Regime | None = None
        self.candidate: Regime | None = None
        self.streak = 0
        self.held = 0

    def load(self, state: HysteresisState) -> None:
        self.current = state.current
        self.candidate = state.candidate
        self.streak = state.streak
        self.held = max(1, state.streak)

    def update(self, candidate: Regime, now: datetime) -> tuple[Regime, int]:
        if self.current is None:
            self.current = candidate
            self.held = 1
            self.candidate = None
            self.streak = 0
        elif candidate == self.current:
            self.held += 1
            self.candidate = None
            self.streak = 0
        else:
            if candidate == self.candidate:
                self.streak += 1
            else:
                self.candidate = candidate
                self.streak = 1
            if self.streak >= self._bars:
                self.current = candidate
                self.candidate = None
                self.streak = 0
                self.held = 1
            else:
                self.held += 1
        return self.current, self.held


class BaseRegimeDetector:
    """Detector con histograma de probabilidades causal y etiqueta con histéresis."""

    method: ClassVar[str] = "base"

    def __init__(self, settings: RegimeSettings) -> None:
        self._settings = settings
        self._fitted = False
        self._tracker = HysteresisTracker(settings.hysteresis_bars)
        self._model_info: ModelInfo | None = None

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def warmup_bars(self) -> int:
        return self._settings.train_window_bars

    @property
    def model_info(self) -> ModelInfo | None:
        return self._model_info

    def fit(self, features: pd.DataFrame, *, train_end_utc: datetime) -> ModelInfo:
        """Entrena el modelo usando exclusivamente datos con cierre <= `train_end_utc`."""
        raise NotImplementedError("fit no implementado")

    def probabilities(self, features: pd.DataFrame) -> pd.DataFrame:
        """Probabilidades por régimen (una fila por vela), sin aplicar histéresis."""
        raise NotImplementedError("probabilities no implementado")

    def predict(self, features: pd.DataFrame, *, state: HysteresisState | None = None) -> RegimeState:
        """Devuelve probabilidades, etiqueta con histéresis y confianza."""
        if state is not None:
            self._tracker.load(state)
        frame = self._clean_probabilities(self.probabilities(features))
        return self._finalize(frame.index[-1], frame.iloc[-1])

    def predict_batch(self, features: pd.DataFrame) -> list[RegimeState]:
        """Serie completa de estados con la misma histéresis; reinicia el tracker."""
        frame = self._clean_probabilities(self.probabilities(features))
        self._tracker = HysteresisTracker(self._settings.hysteresis_bars)
        return [self._finalize(index, row) for index, row in frame.iterrows()]

    def clone(self) -> BaseRegimeDetector:
        raise NotImplementedError("clone no implementado")

    # -- utilidades compartidas ---------------------------------------------

    def _clean(self, features: pd.DataFrame) -> pd.DataFrame:
        if features.empty:
            raise ValueError("features vacío")
        frame = features.dropna()
        if frame.empty:
            raise ValueError("features sin filas completas; amplía el histórico")
        if frame.index.tz is None:
            frame = frame.copy()
            frame.index = frame.index.tz_localize("UTC")
        return frame

    def _clean_probabilities(self, probs: pd.DataFrame) -> pd.DataFrame:
        if probs.empty:
            raise ValueError("probabilidades vacías")
        frame = probs.dropna()
        if frame.empty:
            raise ValueError("probabilidades sin filas completas")
        if frame.index.tz is None:
            frame = frame.copy()
            frame.index = frame.index.tz_localize("UTC")
        return frame

    def _finalize(self, index: pd.Timestamp, row: pd.Series) -> RegimeState:
        probs = {regime: float(row.get(regime.value, 0.0)) for regime in REGIME_ORDER}
        total = sum(max(0.0, value) for value in probs.values())
        probs = {regime: (max(0.0, value) / total if total > 0 else 1.0 / len(probs)) for regime, value in probs.items()}
        candidate = max(probs, key=probs.__getitem__)
        current, held = self._tracker.update(candidate, index.to_pydatetime())
        confidence = probs.get(current, 0.0)
        label = Regime.UNKNOWN if confidence < self._settings.confidence_min else current
        return RegimeState(
            label=label,
            probs=RegimeProbabilities(probs=probs, confidence=max(probs.values()), method=self.method),
            confidence=confidence,
            confirmed_bars=held,
            decided_at_utc=index.to_pydatetime(),
        )

    @staticmethod
    def _empty_frame(index: pd.Index) -> pd.DataFrame:
        return pd.DataFrame({regime.value: 0.0 for regime in REGIME_ORDER}, index=index)
