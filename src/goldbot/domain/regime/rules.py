"""Detector de régimen por reglas: ADX, percentil de ATR, pendiente de EMA y eficiencia."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from goldbot.domain.regime.base import REGIME_ORDER, BaseRegimeDetector, settings_hash
from goldbot.domain.regime.features import FEATURE_COLUMNS
from goldbot.domain.types import ModelInfo, RegimeSettings

_ADX_TREND_FLOOR = 20.0
_ADX_TREND_SPAN = 25.0


def _clip(values: pd.Series, low: float = 0.0, high: float = 1.0) -> pd.Series:
    return values.clip(lower=low, upper=high)


class RulesRegimeDetector(BaseRegimeDetector):
    """Línea base determinista; sirve para validar el HMM y como fallback si falla."""

    method = "rules"

    def __init__(self, settings: RegimeSettings) -> None:
        super().__init__(settings)

    @property
    def warmup_bars(self) -> int:
        settings = self._settings
        return max(
            settings.atr_percentile_window,
            settings.adx_period * 2,
            settings.ema_slow + settings.slope_lookback,
            settings.atr_period * 4,
        ) + 5

    def fit(self, features: pd.DataFrame, *, train_end_utc: datetime) -> ModelInfo:
        frame = self._clean(features)
        frame = frame[frame.index <= pd.Timestamp(train_end_utc)]
        if frame.empty:
            raise ValueError("no hay features anteriores a train_end_utc")
        self._fitted = True
        self._model_info = ModelInfo(
            name="rules",
            version="rules-1",
            trained_from_utc=frame.index[0].to_pydatetime(),
            trained_until_utc=frame.index[-1].to_pydatetime(),
            n_states=len(REGIME_ORDER),
            features=FEATURE_COLUMNS,
            score=None,
            params_hash=settings_hash(self._settings),
        )
        return self._model_info

    def probabilities(self, features: pd.DataFrame) -> pd.DataFrame:
        if not self._fitted:
            raise RuntimeError("RulesRegimeDetector.predict antes de fit")
        missing = [column for column in FEATURE_COLUMNS if column not in features.columns]
        if missing:
            raise ValueError(f"faltan columnas de features: {missing}")
        frame = features.copy()
        efficiency_target = max(self._settings.min_efficiency, 1e-6)
        trend = _clip(
            (frame["adx"] - _ADX_TREND_FLOOR) / _ADX_TREND_SPAN
        ) * _clip(frame["efficiency"] / efficiency_target)
        threshold = self._settings.atr_high_percentile
        vol_span = max(1.0 - threshold, 1e-6)
        high_vol = _clip((frame["atr_pct"] - threshold) / vol_span)

        # Sigmoide de la pendiente (en ATR) para repartir el peso direccional.
        up_share = 1.0 / (1.0 + np.exp(-frame["slope_norm"].clip(-6.0, 6.0)))
        up = trend * up_share
        down = trend * (1.0 - up_share)
        ranging = (1.0 - trend) * (1.0 - 0.5 * high_vol)

        scores = pd.DataFrame(
            {
                "TREND_UP": up,
                "TREND_DOWN": down,
                "RANGE": ranging,
                "HIGH_VOLATILITY": high_vol,
            },
            index=frame.index,
        ).clip(lower=0.0)
        total = scores.sum(axis=1)
        empty = total.isna() | (total <= 0.0)
        scores = scores.div(total.replace(0.0, np.nan), axis=0)
        scores.loc[empty, :] = 1.0 / len(REGIME_ORDER)
        return scores

    def clone(self) -> RulesRegimeDetector:
        return RulesRegimeDetector(self._settings)
