"""Composición de playbooks, selector y filtros a partir de la configuración YAML."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from goldbot.config import AppConfig
from goldbot.domain.decision_core import DecisionCore
from goldbot.domain.filters import DailyLedger, PreTradeFilterChain
from goldbot.domain.interfaces import Clock, EconomicCalendar, Playbook
from goldbot.domain.playbooks.base import BasePlaybook
from goldbot.domain.playbooks.breakout import BreakoutPlaybook
from goldbot.domain.playbooks.no_trade import NoTradePlaybook
from goldbot.domain.playbooks.range_fade import RangeFadePlaybook
from goldbot.domain.playbooks.trend_pullback import TrendPullbackPlaybook
from goldbot.domain.selector import ProbabilityPlaybookSelector
from goldbot.domain.types import FilterSettings, PlaybookSettings

_PLAYBOOK_TYPES: dict[str, type[BasePlaybook]] = {
    "trend_pullback": TrendPullbackPlaybook,
    "breakout": BreakoutPlaybook,
    "range_fade": RangeFadePlaybook,
    "no_trade": NoTradePlaybook,
}


def build_playbooks(config: AppConfig) -> dict[str, BasePlaybook]:
    """Instancia los playbooks declarados en el YAML; un nombre desconocido es un error."""
    playbooks: dict[str, BasePlaybook] = {}
    for playbook_id, section in config.playbooks.items():
        playbook_type = _PLAYBOOK_TYPES.get(playbook_id)
        if playbook_type is None:
            raise ValueError(f"playbook desconocido en la configuración: {playbook_id!r}")
        playbooks[playbook_id] = playbook_type(
            PlaybookSettings(
                enabled=section.enabled,
                min_confidence=section.min_confidence,
                params=dict(section.params),
            )
        )
    if "no_trade" not in playbooks:
        playbooks["no_trade"] = NoTradePlaybook(PlaybookSettings(min_confidence=0.0))
    return playbooks


def build_filter_settings(config: AppConfig) -> FilterSettings:
    filters = config.filters
    return FilterSettings(
        max_spread_multiplier=filters.max_spread_multiplier,
        spread_lookback_bars=filters.spread_lookback_bars,
        news_window_before_min=filters.news_window_before_min,
        news_window_after_min=filters.news_window_after_min,
        news_min_impact=filters.news_min_impact,
        news_currencies=tuple(filters.news_currencies),
        calendar_unavailable_policy=filters.calendar_unavailable_policy,
        news_critical_event_codes=tuple(filters.news_critical_event_codes),
        entry_windows=tuple(config.sessions.entries),
        avoid_rollover=config.sessions.avoid_rollover,
        rollover_buffer_min=filters.rollover_buffer_min,
        max_trades_day=config.risk.max_trades_day,
        daily_loss_pct=config.risk.daily_loss_pct,
    )


def build_selector(
    config: AppConfig, playbooks: Mapping[str, BasePlaybook]
) -> ProbabilityPlaybookSelector:
    """Solo compiten los playbooks habilitados; `no_trade` queda como salida por defecto."""
    active = {
        playbook_id: playbook
        for playbook_id, playbook in playbooks.items()
        if playbook.enabled
    }
    thresholds = {
        playbook_id: float(getattr(playbook, "min_confidence", 0.6))
        for playbook_id, playbook in active.items()
    }
    return ProbabilityPlaybookSelector(
        cast(dict[str, Playbook], active), thresholds=thresholds
    )


def build_decision_core(
    config: AppConfig,
    *,
    playbooks: Mapping[str, BasePlaybook] | None = None,
    calendar: EconomicCalendar | None,
    clock: Clock,
    daily: DailyLedger,
) -> DecisionCore:
    """Cadena completa de decisión con los componentes reales del bot."""
    resolved = playbooks if playbooks is not None else build_playbooks(config)
    return DecisionCore(
        selector=build_selector(config, resolved),
        playbooks=cast(dict[str, Playbook], resolved),
        filters=PreTradeFilterChain(build_filter_settings(config), calendar, clock, daily),
    )
