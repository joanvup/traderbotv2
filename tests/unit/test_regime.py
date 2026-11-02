"""Características causales y detectores de régimen (reglas, HMM y mixto)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from _synthetic import make_candles

from goldbot.domain.regime import create_regime_detector
from goldbot.domain.regime.base import HysteresisTracker
from goldbot.domain.regime.blend import BlendedRegimeDetector
from goldbot.domain.regime.features import FEATURE_COLUMNS, compute_features
from goldbot.domain.regime.hmm import HmmRegimeDetector
from goldbot.domain.regime.rules import RulesRegimeDetector
from goldbot.domain.types import Regime, RegimeSettings, Timeframe

NOW = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)
_SETTINGS = RegimeSettings(train_window_bars=250, atr_percentile_window=100)


def _candles(*, drift: float = 0.0, wave: float = 0.0, noise: float = 0.4, count: int = 500):
    return make_candles(
        Timeframe.M5, count, end_utc=NOW, drift=drift, wave=wave, noise=noise
    )


def _fit(detector, candles, settings=_SETTINGS):
    features = compute_features(candles, settings)
    detector.fit(features, train_end_utc=candles[-1].close_time_utc)
    return features


def test_features_are_causal() -> None:
    candles = _candles(drift=0.2, noise=0.8, count=360)
    short = compute_features(candles[:300], _SETTINGS)
    long = compute_features(candles, _SETTINGS)
    pd.testing.assert_frame_equal(short, long.iloc[: len(short)])


def test_features_have_expected_columns_and_types() -> None:
    features = compute_features(_candles(), _SETTINGS)
    assert tuple(features.columns) == FEATURE_COLUMNS
    assert features.index.tz is not None
    assert features["atr"].dropna().gt(0.0).all()
    assert features["atr_pct"].dropna().between(0.0, 1.0).all()


def test_rules_detect_uptrend_and_downtrend() -> None:
    for drift, expected in ((0.7, Regime.TREND_UP), (-0.7, Regime.TREND_DOWN)):
        settings = replace(_SETTINGS, method="rules")
        detector = create_regime_detector(settings)
        candles = _candles(drift=drift, noise=0.3)
        features = _fit(detector, candles, settings)
        states = detector.predict_batch(features)
        assert states[-1].label is expected
        assert sum(state.label is expected for state in states[-100:]) >= 70
        assert sum(states[-1].probs.probs.values()) == pytest.approx(1.0)


def test_hysteresis_requires_consecutive_confirmations() -> None:
    tracker = HysteresisTracker(bars=3)
    assert tracker.update(Regime.RANGE, NOW)[0] is Regime.RANGE
    for _ in range(2):
        assert tracker.update(Regime.TREND_UP, NOW)[0] is Regime.RANGE
    current, held = tracker.update(Regime.TREND_UP, NOW)
    assert current is Regime.TREND_UP
    assert held == 1


def test_low_confidence_yields_unknown() -> None:
    detector = RulesRegimeDetector(_SETTINGS)
    unclear = pd.Series(
        {"TREND_UP": 0.5, "TREND_DOWN": 0.2, "RANGE": 0.2, "HIGH_VOLATILITY": 0.1},
    )
    state = detector._finalize(pd.Timestamp(NOW), unclear)
    assert state.label is Regime.UNKNOWN
    assert state.probs.confidence == pytest.approx(0.5)

    clear = pd.Series(
        {"TREND_UP": 0.8, "TREND_DOWN": 0.1, "RANGE": 0.05, "HIGH_VOLATILITY": 0.05},
    )
    assert detector._finalize(pd.Timestamp(NOW), clear).label is Regime.TREND_UP


def test_hmm_fit_predict_and_clone() -> None:
    settings = RegimeSettings(
        method="hmm", n_states=3, train_window_bars=250, atr_percentile_window=100
    )
    candles = _candles(wave=2.5, noise=1.0, count=500)
    detector = HmmRegimeDetector(settings)
    features = _fit(detector, candles, settings)

    assert detector.is_fitted
    assert detector.model_info is not None
    assert detector.model_info.n_states == 3
    probs = detector.probabilities(features)
    assert np.allclose(probs.sum(axis=1).to_numpy(), 1.0)
    states = detector.predict_batch(features)
    assert len(states) >= 200
    assert all(isinstance(state.label, Regime) for state in states)
    assert states[-1].probs.method == "hmm"

    clone = detector.clone()
    assert not clone.is_fitted


def test_blend_falls_back_to_rules_when_hmm_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = replace(_SETTINGS, method="both")
    candles = _candles(drift=0.5, noise=0.4)
    features = compute_features(candles, settings)
    detector = BlendedRegimeDetector(settings)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("hmm roto")

    monkeypatch.setattr(HmmRegimeDetector, "fit", boom)
    info = detector.fit(features, train_end_utc=candles[-1].close_time_utc)
    assert detector.is_fitted
    assert detector.hmm_active is False
    assert info.name == "rules"
    states = detector.predict_batch(features)
    assert states[-1].label is Regime.TREND_UP


def test_hmm_probabilities_are_causal() -> None:
    settings = RegimeSettings(
        method="hmm", n_states=3, train_window_bars=250, atr_percentile_window=100
    )
    candles = _candles(drift=0.2, wave=1.5, noise=0.8, count=600)
    features = compute_features(candles, settings)
    detector = HmmRegimeDetector(settings)
    detector.fit(features, train_end_utc=candles[349].close_time_utc)

    window = features.loc[features.index > pd.Timestamp(candles[349].close_time_utc)]
    full = detector.probabilities(window)
    prefix = detector.probabilities(window.iloc[:80])
    pd.testing.assert_frame_equal(full.iloc[:80], prefix)
    assert np.allclose(full.sum(axis=1).to_numpy(), 1.0)


def test_blend_reports_combined_model_info() -> None:
    settings = replace(_SETTINGS, method="both")
    candles = _candles(drift=0.5)
    detector = BlendedRegimeDetector(settings)
    info = detector.fit(
        compute_features(candles, settings), train_end_utc=candles[-1].close_time_utc
    )
    assert detector.hmm_active is True
    assert info.name == "blend"
    assert info.version == "rules-1+hmm-1-4"
    assert len(info.params_hash) == 16


def test_factory_creates_expected_detectors() -> None:
    assert isinstance(create_regime_detector(_SETTINGS), BlendedRegimeDetector)
    assert isinstance(
        create_regime_detector(RegimeSettings(method="rules")), RulesRegimeDetector
    )
    assert isinstance(create_regime_detector(RegimeSettings(method="hmm")), HmmRegimeDetector)
