"""Detector de régimen mediante HMM gaussiano sobre características normalizadas.

Notas de diseño:
- `hmmlearn` se importa de forma perezosa para no exigirlo al importar el paquete.
- La normalización usa las estadísticas de la ventana de entrenamiento (congeladas),
  nunca estadísticas globales del conjunto completo: no hay look-ahead.
- La correspondencia estado oculto→régimen se deriva de las medias del modelo.
- La inferencia usa filtrado forward (solo pasado): reproducible en backtest y sin
  look-ahead, a diferencia de `predict_proba`, que suaviza con datos posteriores.
- El reentrenamiento es walk-forward con `fit(..., train_end_utc)` y `clone()`.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import asdict
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from goldbot.domain.regime.base import REGIME_ORDER, BaseRegimeDetector
from goldbot.domain.regime.features import FEATURE_COLUMNS
from goldbot.domain.types import ModelInfo, Regime, RegimeSettings

_MIN_TRAIN_ROWS = 50
# Pendiente+retorno medios mínimos (en ATR) para etiquetar un estado como tendencia.
_MIN_TREND_DIRECTIONAL = 1.0


def _logsumexp(values: np.ndarray, *, axis: int) -> np.ndarray:
    """Log-sum-exp estable en NumPy, sin depender de SciPy."""
    peak = np.max(values, axis=axis, keepdims=True)
    peak = np.where(np.isfinite(peak), peak, 0.0)
    return np.squeeze(peak, axis=axis) + np.log(
        np.sum(np.exp(values - peak), axis=axis)
    )


def _normalize_log(values: np.ndarray) -> np.ndarray:
    return values - _logsumexp(values, axis=0)


def _map_states(
    model: Any,
    mean: np.ndarray,
    std: np.ndarray,
    *,
    atr_high_percentile: float,
) -> dict[int, Regime]:
    """Traduce estados ocultos a regímenes usando las medias sin normalizar.

    Un estado solo es tendencia si su dirección media supera `_MIN_TREND_DIRECTIONAL`
    y su eficiencia media está por encima de la media de todos los estados; si no,
    se queda como rango. Así un estado de volatilidad con pendiente leve no se
    disfraza de tendencia.
    """
    raw_means = np.asarray(model.means_, dtype=float) * std + mean
    direction = (
        raw_means[:, list(FEATURE_COLUMNS).index("ret_1")]
        + raw_means[:, list(FEATURE_COLUMNS).index("slope_norm")]
    )
    efficiency = raw_means[:, list(FEATURE_COLUMNS).index("efficiency")]
    atr_pct = raw_means[:, list(FEATURE_COLUMNS).index("atr_pct")]
    efficiency_floor = float(np.mean(efficiency))

    up_candidates = [
        state
        for state in range(len(raw_means))
        if direction[state] >= _MIN_TREND_DIRECTIONAL
        and efficiency[state] >= efficiency_floor
    ]
    down_candidates = [
        state
        for state in range(len(raw_means))
        if direction[state] <= -_MIN_TREND_DIRECTIONAL
        and efficiency[state] >= efficiency_floor
    ]
    mapping: dict[int, Regime] = {}
    if up_candidates:
        mapping[max(up_candidates, key=lambda state: direction[state])] = Regime.TREND_UP
    if down_candidates:
        mapping[min(down_candidates, key=lambda state: direction[state])] = Regime.TREND_DOWN

    remaining = [state for state in range(len(raw_means)) if state not in mapping]
    volatile_candidates = [
        state for state in remaining if atr_pct[state] >= atr_high_percentile
    ]
    if volatile_candidates:
        volatile = max(volatile_candidates, key=lambda state: atr_pct[state])
        mapping[volatile] = Regime.HIGH_VOLATILITY
        remaining = [state for state in remaining if state != volatile]
    for state in remaining:
        mapping[state] = Regime.RANGE
    return mapping


class HmmRegimeDetector(BaseRegimeDetector):
    """HMM de 3-4 estados con probabilidades posteriores y nivel de confianza."""

    method = "hmm"

    def __init__(self, settings: RegimeSettings) -> None:
        super().__init__(settings)
        self._model: Any = None
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._state_to_regime: dict[int, Regime] = {}

    @property
    def warmup_bars(self) -> int:
        return self._settings.train_window_bars + self._settings.atr_percentile_window

    def fit(self, features: pd.DataFrame, *, train_end_utc: datetime) -> ModelInfo:
        self._require_columns(features)
        frame = self._clean(features)
        frame = frame[frame.index <= pd.Timestamp(train_end_utc)]
        if len(frame) < _MIN_TRAIN_ROWS:
            raise ValueError(
                f"el HMM necesita al menos {_MIN_TRAIN_ROWS} filas; hay {len(frame)}"
            )
        matrix = frame.loc[:, list(FEATURE_COLUMNS)].to_numpy(dtype=float)
        self._mean = matrix.mean(axis=0)
        std = matrix.std(axis=0)
        self._std = np.where(std > 1e-12, std, 1.0)
        normalized = self._normalize(frame)

        from hmmlearn.hmm import GaussianHMM  # noqa: PLC0415 - import perezoso

        model = GaussianHMM(
            n_components=self._settings.n_states,
            covariance_type="diag",
            n_iter=200,
            tol=1e-4,
            random_state=42,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            model.fit(normalized)
        score = float(model.score(normalized))
        self._model = model
        self._state_to_regime = _map_states(
            model,
            self._mean,
            self._std,
            atr_high_percentile=self._settings.atr_high_percentile,
        )
        self._fitted = True
        self._model_info = ModelInfo(
            name="hmm",
            version=f"hmm-1-{self._settings.n_states}",
            trained_from_utc=frame.index[0].to_pydatetime(),
            trained_until_utc=frame.index[-1].to_pydatetime(),
            n_states=int(self._settings.n_states),
            features=FEATURE_COLUMNS,
            score=score,
            params_hash=self._params_hash(model),
        )
        return self._model_info

    def probabilities(self, features: pd.DataFrame) -> pd.DataFrame:
        if not self._fitted or self._model is None:
            raise RuntimeError("HmmRegimeDetector.predict antes de fit")
        self._require_columns(features)
        frame = features.dropna()
        if frame.empty:
            return self._empty_frame(features.index)
        normalized = self._normalize(frame)
        posterior = self._forward_filter(normalized)
        scores = pd.DataFrame(0.0, index=frame.index, columns=[regime.value for regime in REGIME_ORDER])
        for state, regime in self._state_to_regime.items():
            scores[regime.value] += posterior[:, state]
        total = scores.sum(axis=1)
        return scores.div(total.replace(0.0, np.nan), axis=0).fillna(1.0 / len(REGIME_ORDER))

    def clone(self) -> HmmRegimeDetector:
        return HmmRegimeDetector(self._settings)

    # -- internos ------------------------------------------------------------

    def _forward_filter(self, normalized: np.ndarray) -> np.ndarray:
        """Probabilidades filtradas (solo pasado), nunca suavizadas con velas futuras.

        `predict_proba` de hmmlearn aplica forward-backward sobre toda la secuencia: en
        un backtest usaría datos posteriores a cada vela. Aquí se propaga solo `alpha_t`.
        """
        model = self._model
        means = np.asarray(model.means_, dtype=float)
        covars = np.asarray(model.covars_, dtype=float)
        # hmmlearn 0.3.x expone las covarianzas diagonales como matrices (k, d, d).
        variances = np.diagonal(covars, axis1=1, axis2=2) if covars.ndim == 3 else covars
        variances = np.clip(variances, 1e-12, None)
        start = np.asarray(model.startprob_, dtype=float)
        transmat = np.asarray(model.transmat_, dtype=float)
        log_start = np.log(np.clip(start, 1e-300, None))
        log_trans = np.log(np.clip(transmat, 1e-300, None))
        log_norm = -0.5 * (
            normalized.shape[1] * np.log(2.0 * np.pi)
            + np.log(variances).sum(axis=1)[None, :]
            + ((normalized[:, None, :] - means[None, :, :]) ** 2 / variances[None, :, :]).sum(axis=2)
        )
        posterior = np.empty((normalized.shape[0], means.shape[0]), dtype=float)
        alpha = _normalize_log(log_start + log_norm[0])
        posterior[0] = np.exp(alpha)
        for index in range(1, normalized.shape[0]):
            predicted = _logsumexp(alpha[:, None] + log_trans, axis=0)
            alpha = _normalize_log(predicted + log_norm[index])
            posterior[index] = np.exp(alpha)
        return posterior

    @staticmethod
    def _require_columns(features: pd.DataFrame) -> None:
        missing = [column for column in FEATURE_COLUMNS if column not in features.columns]
        if missing:
            raise ValueError(f"faltan columnas de features: {missing}")

    def _normalize(self, frame: pd.DataFrame) -> np.ndarray:
        if self._mean is None or self._std is None:
            raise RuntimeError("HMM sin estadísticas de normalización")
        matrix = frame.loc[:, list(FEATURE_COLUMNS)].to_numpy(dtype=float)
        return np.clip((matrix - self._mean) / self._std, -10.0, 10.0)

    def _params_hash(self, model: Any) -> str:
        payload = {
            "settings": asdict(self._settings),
            "means": np.round(np.asarray(model.means_), 4).tolist(),
            "transmat": np.round(
                np.asarray(model.transmat_), 4
            ).tolist(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
