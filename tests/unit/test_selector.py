"""Selector de playbook y composición desde la configuración."""

from __future__ import annotations

import pytest
from _snapshots import regime, snapshot

from goldbot.config import AppConfig
from goldbot.domain.selector import ProbabilityPlaybookSelector
from goldbot.domain.types import Regime, RegimeState
from goldbot.engine.decisions import (
    build_filter_settings,
    build_playbooks,
    build_selector,
)


class _Stub:
    def __init__(self, playbook_id: str, supported: frozenset[Regime], min_confidence: float = 0.6):
        self.playbook_id = playbook_id
        self.supported_regimes = supported
        self.min_confidence = min_confidence
        self.enabled = True

    def evaluate(self, ctx, state):  # pragma: no cover - no se usa en el selector
        return None

    def manage(self, position, ctx, state):  # pragma: no cover
        raise NotImplementedError


def test_highest_probability_above_threshold_wins() -> None:
    playbooks = {
        "trend_pullback": _Stub("trend_pullback", frozenset({Regime.TREND_UP})),
        "breakout": _Stub("breakout", frozenset({Regime.TREND_UP}), 0.9),
        "no_trade": _Stub("no_trade", frozenset()),
    }
    selector = ProbabilityPlaybookSelector(playbooks, fallback_id="no_trade")
    selection = selector.select(
        snapshot(), regime(Regime.TREND_UP, confidence=0.8)
    )
    assert selection.playbook_id == "trend_pullback"
    assert selection.fallback_no_trade is False
    assert selection.scores["breakout"] == pytest.approx(0.8)
    assert "trend_pullback" in selection.rationale


def test_unknown_regime_always_falls_back_to_no_trade() -> None:
    playbooks = {
        "trend_pullback": _Stub("trend_pullback", frozenset({Regime.TREND_UP})),
        "no_trade": _Stub("no_trade", frozenset()),
    }
    selector = ProbabilityPlaybookSelector(playbooks, fallback_id="no_trade")
    selection = selector.select(snapshot(), regime(Regime.UNKNOWN, confidence=0.4))
    assert selection.playbook_id == "no_trade"
    assert selection.fallback_no_trade is True


def test_low_confidence_below_threshold_falls_back() -> None:
    playbooks = {
        "range_fade": _Stub("range_fade", frozenset({Regime.RANGE}), 0.7),
        "no_trade": _Stub("no_trade", frozenset()),
    }
    selector = ProbabilityPlaybookSelector(playbooks, fallback_id="no_trade")
    selection = selector.select(snapshot(), regime(Regime.RANGE, confidence=0.65))
    assert selection.fallback_no_trade is True


def test_selector_ignores_non_supported_regimes() -> None:
    playbooks = {
        "range_fade": _Stub("range_fade", frozenset({Regime.RANGE})),
        "no_trade": _Stub("no_trade", frozenset()),
    }
    selector = ProbabilityPlaybookSelector(playbooks, fallback_id="no_trade")
    state: RegimeState = regime(Regime.TREND_DOWN, confidence=0.9)
    assert selector.select(snapshot(), state).fallback_no_trade is True


def test_build_playbooks_instantiates_the_yaml_set() -> None:
    playbooks = build_playbooks(AppConfig())
    assert set(playbooks) == {"trend_pullback", "breakout", "range_fade", "no_trade"}
    assert all(playbook.enabled for playbook in playbooks.values())


def test_build_playbooks_rejects_unknown_ids() -> None:
    config = AppConfig.model_validate(
        {"playbooks": {"mystery": {"enabled": True}, "no_trade": {"enabled": True}}}
    )
    with pytest.raises(ValueError, match="playbook desconocido"):
        build_playbooks(config)


def test_disabled_playbooks_do_not_compete() -> None:
    config = AppConfig.model_validate(
        {"playbooks": {"trend_pullback": {"enabled": False}, "no_trade": {"enabled": True}}}
    )
    playbooks = build_playbooks(config)
    selector = build_selector(config, playbooks)
    selection = selector.select(snapshot(), regime(Regime.TREND_UP, confidence=0.9))
    assert selection.playbook_id == "no_trade"
    assert selection.scores.get("trend_pullback", 0.0) == 0.0


def test_filter_settings_come_from_the_yaml_sections() -> None:
    config = AppConfig.model_validate(
        {
            "sessions": {"entries": ["08:00-18:00"], "avoid_rollover": False},
            "risk": {"max_trades_day": 1, "daily_loss_pct": 1.5},
            "filters": {
                "max_spread_multiplier": 3.0,
                "news_currencies": ["USD", "EUR"],
                "rollover_buffer_min": 10,
            },
        }
    )
    settings = build_filter_settings(config)
    assert settings.entry_windows == ("08:00-18:00",)
    assert settings.avoid_rollover is False
    assert settings.max_trades_day == 1
    assert settings.daily_loss_pct == pytest.approx(1.5)
    assert settings.max_spread_multiplier == pytest.approx(3.0)
    assert settings.news_currencies == ("USD", "EUR")
    assert settings.rollover_buffer_min == 10
