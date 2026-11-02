"""Detector combinado: mezcla reglas y HMM, con degradación segura a reglas."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from goldbot.domain.regime.base import BaseRegimeDetector
from goldbot.domain.regime.hmm import HmmRegimeDetector
from goldbot.domain.regime.rules import RulesRegimeDetector
from goldbot.domain.types import ModelInfo, RegimeSettings

LOGGER = logging.getLogger(__name__)


class BlendedRegimeDetector(BaseRegimeDetector):
    """Promedio 50/50 de reglas y HMM; si el HMM falla, opera solo con reglas."""

    method = "both"

    def __init__(self, settings: RegimeSettings) -> None:
        super().__init__(settings)
        self._rules = RulesRegimeDetector(settings)
        self._hmm = HmmRegimeDetector(settings)
        self._hmm_active = False

    @property
    def hmm_active(self) -> bool:
        return self._hmm_active

    @property
    def warmup_bars(self) -> int:
        return max(self._rules.warmup_bars, self._hmm.warmup_bars)

    def fit(self, features: pd.DataFrame, *, train_end_utc: datetime) -> ModelInfo:
        rules_info = self._rules.fit(features, train_end_utc=train_end_utc)
        try:
            hmm_info = self._hmm.fit(features, train_end_utc=train_end_utc)
        except Exception:  # noqa: BLE001 - el HMM nunca debe tumbar el bot
            LOGGER.warning("HMM no disponible; se usan solo reglas", exc_info=True)
            self._hmm_active = False
            self._model_info = rules_info
        else:
            self._hmm_active = True
            digest = hashlib.sha256(
                f"{rules_info.params_hash}|{hmm_info.params_hash}".encode()
            ).hexdigest()[:16]
            self._model_info = ModelInfo(
                name="blend",
                version=f"{rules_info.version}+{hmm_info.version}",
                trained_from_utc=min(rules_info.trained_from_utc, hmm_info.trained_from_utc),
                trained_until_utc=max(
                    rules_info.trained_until_utc, hmm_info.trained_until_utc
                ),
                n_states=hmm_info.n_states,
                features=hmm_info.features,
                score=hmm_info.score,
                params_hash=digest,
            )
        self._fitted = True
        return self._model_info

    def probabilities(self, features: pd.DataFrame) -> pd.DataFrame:
        if not self._fitted:
            raise RuntimeError("BlendedRegimeDetector.predict antes de fit")
        rules = self._rules.probabilities(features)
        if not self._hmm_active:
            return rules
        hmm = self._hmm.probabilities(features).reindex(rules.index).fillna(0.0)
        combined = (rules + hmm) / 2.0
        total = combined.sum(axis=1)
        return combined.div(total.replace(0.0, np.nan), axis=0).fillna(
            1.0 / combined.shape[1]
        )

    def clone(self) -> BlendedRegimeDetector:
        return BlendedRegimeDetector(self._settings)
