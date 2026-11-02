"""Composición del análisis de mercado: contexto multi-timeframe + régimen.

`MarketAnalyzer` mantiene en memoria la serie de velas cerradas del timeframe de
decisión, reentrena el detector con cadencia configurada y produce snapshots
reproducibles. No envía órdenes ni depende de MT5 más allá del `BrokerAdapter`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from goldbot.config import AppConfig, compute_config_hash
from goldbot.domain.context import ContextBuilder
from goldbot.domain.interfaces import BrokerAdapter, Clock
from goldbot.domain.regime import create_regime_detector
from goldbot.domain.regime.base import BaseRegimeDetector
from goldbot.domain.regime.features import compute_features
from goldbot.domain.types import (
    Candle,
    ContextSettings,
    ContextSnapshot,
    ModelInfo,
    RegimeSettings,
    RegimeState,
    Timeframe,
)

LOGGER = logging.getLogger(__name__)

_DECISION_BUFFER_BARS = 200


def build_context_settings(config: AppConfig) -> ContextSettings:
    regime = config.regime
    return ContextSettings(
        ema_fast=regime.ema_fast,
        ema_slow=regime.ema_slow,
        atr_period=regime.atr_period,
        swing_lookback=regime.swing_lookback,
        slope_lookback=regime.slope_lookback,
        level_tolerance_atr=regime.level_tolerance_atr,
        atr_percentile_window=regime.atr_percentile_window,
        momentum_lookback=regime.momentum_lookback,
        structure_lookback_bars=regime.structure_lookback_bars,
        max_swings=regime.max_swings,
        max_levels=regime.max_levels,
        round_level_step=regime.round_level_step,
        spread_lookback_bars=config.filters.spread_lookback_bars,
    )


def build_regime_settings(config: AppConfig) -> RegimeSettings:
    regime = config.regime
    return RegimeSettings(
        method=regime.method,
        confidence_min=regime.confidence_min,
        hysteresis_bars=regime.hysteresis_bars,
        n_states=regime.n_states,
        train_window_bars=regime.train_window_bars,
        retrain_every_bars=regime.retrain_every_bars,
        adx_period=regime.adx_period,
        atr_period=regime.atr_period,
        atr_percentile_window=regime.atr_percentile_window,
        atr_high_percentile=regime.atr_high_percentile,
        ema_fast=regime.ema_fast,
        ema_slow=regime.ema_slow,
        slope_lookback=regime.slope_lookback,
        min_efficiency=regime.min_efficiency,
    )


def regime_to_jsonable(state: RegimeState) -> dict[str, object]:
    return {
        "label": state.label.value,
        "confidence": state.confidence,
        "confirmed_bars": state.confirmed_bars,
        "decided_at_utc": state.decided_at_utc.isoformat(),
        "method": state.probs.method,
        "probs": {regime.value: value for regime, value in state.probs.probs.items()},
    }


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    snapshot: ContextSnapshot
    regime: RegimeState
    model_info: ModelInfo | None
    retrained: bool


def _merge(
    existing: Sequence[Candle], incoming: Sequence[Candle], limit: int
) -> tuple[Candle, ...]:
    unique = {candle.open_time_utc: candle for candle in existing}
    for candle in incoming:
        if candle.is_closed:
            unique[candle.open_time_utc] = candle
    ordered = tuple(unique[key] for key in sorted(unique))
    return ordered[-limit:] if limit > 0 else ordered


class MarketAnalyzer:
    """Contexto y régimen sobre el timeframe de decisión, con reentrenamiento causal."""

    def __init__(
        self,
        *,
        config: AppConfig,
        broker: BrokerAdapter,
        clock: Clock,
        detector: BaseRegimeDetector | None = None,
        builder: ContextBuilder | None = None,
    ) -> None:
        self._config = config
        self._broker = broker
        self._clock = clock
        self._settings = build_regime_settings(config)
        self._detector = detector or create_regime_detector(self._settings)
        self._builder = builder or ContextBuilder(
            build_context_settings(config),
            stale_after_s={
                Timeframe(name): float(seconds)
                for name, seconds in config.data.stale_after_s.items()
            },
        )
        self._config_hash = compute_config_hash(config)
        self._decision_timeframe = Timeframe(config.data.decision_timeframe)
        self._timeframes = tuple(
            dict.fromkeys(
                Timeframe(name)
                for name in (
                    config.data.decision_timeframe,
                    *config.data.entry_timeframes,
                    *config.data.context_timeframes,
                )
            )
        )
        self._limits = {
            Timeframe(name): count for name, count in config.data.warmup_bars.items()
        }
        self._history_bars = max(
            self._limits.get(self._decision_timeframe, 0),
            self._settings.train_window_bars
            + self._settings.atr_percentile_window
            + _DECISION_BUFFER_BARS,
        )
        self._limits[self._decision_timeframe] = self._history_bars
        self._series: dict[Timeframe, tuple[Candle, ...]] = {}
        self._features: pd.DataFrame | None = None
        self._bars_since_fit = 0
        self._warmed_up = False

    @property
    def decision_timeframe(self) -> Timeframe:
        return self._decision_timeframe

    @property
    def detector(self) -> BaseRegimeDetector:
        return self._detector

    @property
    def warmup_bars(self) -> int:
        return self._detector.warmup_bars

    def warm_up(self) -> ModelInfo | None:
        """Descarga histórico, calcula características y entrena el detector."""
        now = self._clock.now_utc()
        for timeframe in self._timeframes:
            candles = self._broker.get_candles(
                self._config.mt5.symbol,
                timeframe,
                self._limits[timeframe],
                closed_only=True,
                as_of_utc=now,
            )
            if not candles:
                raise RuntimeError(f"sin velas cerradas de {timeframe.value} para el análisis")
            self._series[timeframe] = _merge((), candles, self._limits[timeframe])

        self._features = self._compute_features()
        last_close = self._series[self._decision_timeframe][-1].close_time_utc
        info = self._detector.fit(self._features, train_end_utc=last_close)
        self._bars_since_fit = 0
        self._warmed_up = True
        LOGGER.info(
            "régimen entrenado: %s %s sobre %d velas (hasta %s)",
            info.name,
            info.version,
            len(self._features),
            info.trained_until_utc.isoformat(),
        )
        return info

    def analyze(self, now_utc: datetime | None = None) -> AnalysisResult:
        """Refresca velas, aplica histéresis y construye el snapshot del instante."""
        if not self._warmed_up or self._features is None:
            raise RuntimeError("MarketAnalyzer.analyze antes de warm_up")
        now = now_utc or self._clock.now_utc()
        if self._refresh(now):
            self._features = self._compute_features()

        retrained = False
        if (
            self._detector.is_fitted
            and self._bars_since_fit >= self._settings.retrain_every_bars
        ):
            last_close = self._series[self._decision_timeframe][-1].close_time_utc
            self._detector.fit(self._features, train_end_utc=last_close)
            self._bars_since_fit = 0
            retrained = True
            LOGGER.info("detector de régimen reentrenado hasta %s", last_close.isoformat())

        states = self._detector.predict_batch(self._features)
        regime = states[-1]
        info = self._detector.model_info
        snapshot = self._builder.build(
            self._series,
            now_utc=now,
            decision_timeframe=self._decision_timeframe,
            session=self._clock.session_at(now),
            config_hash=self._config_hash,
            model_versions={info.name: info.version} if info is not None else {},
        )
        return AnalysisResult(
            snapshot=snapshot,
            regime=regime,
            model_info=info,
            retrained=retrained,
        )

    # -- internos ------------------------------------------------------------

    def _refresh(self, now: datetime) -> bool:
        """Incorpora velas cerradas nuevas; devuelve True si cambió la de decisión."""
        changed_decision = False
        for timeframe in self._timeframes:
            candles = self._broker.get_candles(
                self._config.mt5.symbol,
                timeframe,
                10,
                closed_only=True,
                as_of_utc=now,
            )
            if not candles:
                continue
            previous = self._series.get(timeframe, ())
            merged = _merge(previous, candles, self._limits[timeframe])
            if timeframe is self._decision_timeframe and (
                not previous or merged[-1].open_time_utc != previous[-1].open_time_utc
            ):
                changed_decision = True
                self._bars_since_fit += 1
            self._series[timeframe] = merged
        return changed_decision

    def _compute_features(self) -> pd.DataFrame:
        return compute_features(self._series[self._decision_timeframe], self._settings)
