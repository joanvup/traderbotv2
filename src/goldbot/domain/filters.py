"""Filtros previos a operar: datos frescos, spread, noticias, sesión y límites."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Protocol

from goldbot.domain.interfaces import Clock, EconomicCalendar
from goldbot.domain.types import (
    AccountState,
    CalendarHealth,
    ContextSnapshot,
    DailyCounters,
    DataStatus,
    FilterCode,
    FilterOutcome,
    FilterResult,
    FilterSettings,
    Signal,
    SymbolSpec,
)

LOGGER = logging.getLogger(__name__)

_IMPACT_RANK = {"none": 0, "low": 1, "medium": 2, "moderate": 2, "high": 3}


class DailyLedger(Protocol):
    """Fuente de contadores diarios; en vivo se adapta de `daily_stats`."""

    def counters(self, trading_day: date) -> DailyCounters: ...


@dataclass
class _Day:
    entries_used: int = 0
    realized_pnl: float = 0.0
    day_start_equity: float | None = None


@dataclass
class InMemoryDailyLedger:
    """Ledger determinista para backtest y tests; el bot en vivo usa el StateStore."""

    _days: dict[date, _Day] = field(default_factory=dict)

    def counters(self, trading_day: date) -> DailyCounters:
        day = self._days.get(trading_day, _Day())
        return DailyCounters(
            trading_day=trading_day,
            entries_used=day.entries_used,
            realized_pnl=day.realized_pnl,
            day_start_equity=day.day_start_equity,
        )

    def ensure_day(self, trading_day: date, equity: float) -> None:
        day = self._days.setdefault(trading_day, _Day())
        if day.day_start_equity is None:
            day.day_start_equity = equity

    def record_entry(self, trading_day: date) -> None:
        self._days.setdefault(trading_day, _Day()).entries_used += 1

    def record_pnl(self, trading_day: date, pnl: float) -> None:
        self._days.setdefault(trading_day, _Day()).realized_pnl += pnl


class PreTradeFilterChain:
    """Cadena de filtros que debe pasar toda señal antes de llegar al RiskManager."""

    def __init__(
        self,
        settings: FilterSettings,
        calendar: EconomicCalendar | None,
        clock: Clock,
        daily: DailyLedger,
    ) -> None:
        self._settings = settings
        self._calendar = calendar
        self._clock = clock
        self._daily = daily
        self._windows = tuple(
            (time.fromisoformat(start), time.fromisoformat(end))
            for start, _, end in (window.partition("-") for window in settings.entry_windows)
        )

    def evaluate(
        self,
        ctx: ContextSnapshot,
        signal: Signal,
        account: AccountState,
        spec: SymbolSpec,
    ) -> FilterOutcome:
        """Devuelve todos los resultados (no corta en el primero) y el motivo principal."""
        results: list[FilterResult] = [
            self._demo_guard(account),
            self._data_freshness(ctx),
            self._session(ctx),
            self._spread(ctx),
            self._news(ctx),
            self._max_trades(ctx),
            self._daily_loss(ctx, account),
        ]
        blocked = any(result.blocked for result in results)
        primary = next(
            (result.message for result in results if result.blocked), None
        )
        return FilterOutcome(results=tuple(results), blocked=blocked, primary_reason=primary)

    # -- filtros --------------------------------------------------------------

    @staticmethod
    def _demo_guard(account: AccountState) -> FilterResult:
        blocked = not account.is_demo
        return FilterResult(
            code=FilterCode.DEMO_GUARD,
            blocked=blocked,
            observed=account.is_demo,
            threshold=True,
            message="cuenta no demo: operación bloqueada" if blocked else "cuenta demo",
        )

    @staticmethod
    def _data_freshness(ctx: ContextSnapshot) -> FilterResult:
        decision = ctx.freshness.get(ctx.decision_timeframe)
        label = ctx.decision_timeframe.value
        stale = [
            timeframe.value
            for timeframe, fresh in ctx.freshness.items()
            if fresh.status is not DataStatus.OK
        ]
        blocked = decision is None or decision.status is not DataStatus.OK
        if decision is None:
            detail = f"sin datos del marco de decisión {label}"
        elif blocked:
            detail = f"{label} en estado {decision.status.value}"
        elif stale:
            # Contexto atrasado (típico las primeras horas del lunes): se informa, no bloquea.
            detail = f"datos frescos (contexto atrasado: {', '.join(stale)})"
        else:
            detail = "datos frescos"
        return FilterResult(
            code=FilterCode.DATA_STALE,
            blocked=blocked,
            observed={timeframe.value: fresh.status.value for timeframe, fresh in ctx.freshness.items()},
            threshold=f"{label} OK",
            message=detail,
        )

    def _session(self, ctx: ContextSnapshot) -> FilterResult:
        server = self._clock.to_server(ctx.decision_time_utc)
        moment = server.time()
        allowed = any(_in_window(moment, start, end) for start, end in self._windows)
        rollover = self._rollover_blocked(moment) if self._settings.avoid_rollover else False
        blocked = not allowed or rollover
        if rollover:
            message = f"rollover del servidor ({server:%H:%M})"
        elif not allowed:
            message = f"fuera de horario de entradas ({server:%H:%M} servidor)"
        else:
            message = f"sesión permitida ({server:%H:%M} servidor)"
        return FilterResult(
            code=FilterCode.SESSION,
            blocked=blocked,
            observed=server.strftime("%H:%M"),
            threshold=list(self._settings.entry_windows),
            message=message,
        )

    def _spread(self, ctx: ContextSnapshot) -> FilterResult:
        current = float(ctx.timing.spread_points)
        mean = float(ctx.timing.spread_mean_points)
        threshold = self._settings.max_spread_multiplier * mean
        blocked = mean > 0.0 and current > threshold
        return FilterResult(
            code=FilterCode.SPREAD,
            blocked=blocked,
            observed=current,
            threshold=round(threshold, 2),
            message=(
                f"spread {current:.0f} pts > {threshold:.0f} pts "
                f"({self._settings.max_spread_multiplier:.1f}× media {mean:.0f})"
                if blocked
                else f"spread {current:.0f} pts dentro de {threshold:.0f}"
            ),
        )

    def _news(self, ctx: ContextSnapshot) -> FilterResult:
        if self._settings.news_window_before_min == 0 and self._settings.news_window_after_min == 0:
            return FilterResult(
                code=FilterCode.NEWS,
                blocked=False,
                observed="disabled",
                threshold=None,
                message="filtro de noticias desactivado",
            )
        if self._calendar is None:
            blocked = self._settings.calendar_unavailable_policy == "block"
            return FilterResult(
                code=FilterCode.NEWS,
                blocked=blocked,
                observed="sin calendario",
                threshold=self._settings.news_min_impact,
                message="calendario no configurado" if blocked else "calendario no disponible (allow)",
            )
        before = self._settings.news_window_before_min
        after = self._settings.news_window_after_min
        try:
            window = self._calendar.window(
                ctx.decision_time_utc - timedelta(minutes=before),
                ctx.decision_time_utc + timedelta(minutes=after),
                self._settings.news_currencies,
                # Se piden todos los impactos y la política se aplica aquí, para
                # que `news_critical_event_codes` pueda elevar un evento menor.
                "none",
            )
        except Exception as exc:  # noqa: BLE001 - el filtro nunca debe tumbar el ciclo
            LOGGER.warning("calendario no disponible: %s", exc)
            blocked = self._settings.calendar_unavailable_policy == "block"
            return FilterResult(
                code=FilterCode.NEWS,
                blocked=blocked,
                observed=str(exc),
                threshold=self._settings.news_min_impact,
                message=f"calendario inaccesible: {exc}" if blocked else "calendario inaccesible (allow)",
            )

        critical = set(self._settings.news_critical_event_codes)
        ranked = _IMPACT_RANK.get(self._settings.news_min_impact.lower(), 3)
        events = [
            event
            for event in window.events
            if _IMPACT_RANK.get(event.impact.lower(), 0) >= ranked
            or (event.event_code and event.event_code in critical)
        ]
        health = window.health
        if health is not CalendarHealth.OK:
            blocked = self._settings.calendar_unavailable_policy == "block"
            return FilterResult(
                code=FilterCode.NEWS,
                blocked=blocked,
                observed=health.value,
                threshold=self._settings.news_min_impact,
                message=(
                    f"calendario {health.value.lower()}: sin datos fiables"
                    if blocked
                    else f"calendario {health.value.lower()} (allow)"
                ),
            )
        blocked = bool(events)
        detail = "; ".join(f"{event.currency} {event.title} {event.ts_utc:%H:%M}Z" for event in events[:3])
        return FilterResult(
            code=FilterCode.NEWS,
            blocked=blocked,
            observed=[event.title for event in events],
            threshold=f"{self._settings.news_min_impact} ±{before}/{after} min",
            message=f"noticias en ventana: {detail}" if blocked else "sin noticias relevantes",
        )

    def _max_trades(self, ctx: ContextSnapshot) -> FilterResult:
        counters = self._daily.counters(self._clock.trading_day(ctx.decision_time_utc))
        blocked = counters.entries_used >= self._settings.max_trades_day
        return FilterResult(
            code=FilterCode.MAX_TRADES,
            blocked=blocked,
            observed=counters.entries_used,
            threshold=self._settings.max_trades_day,
            message=(
                f"máximo de entradas del día alcanzado ({counters.entries_used})"
                if blocked
                else f"entradas del día {counters.entries_used}/{self._settings.max_trades_day}"
            ),
        )

    def _daily_loss(self, ctx: ContextSnapshot, account: AccountState) -> FilterResult:
        counters = self._daily.counters(self._clock.trading_day(ctx.decision_time_utc))
        start = counters.day_start_equity
        if start is None or start <= 0.0:
            return FilterResult(
                code=FilterCode.DAILY_LOSS,
                blocked=False,
                observed=None,
                threshold=self._settings.daily_loss_pct,
                message="equity de inicio de día desconocido",
            )
        loss_pct = (start - account.equity) / start * 100.0
        blocked = loss_pct >= self._settings.daily_loss_pct
        return FilterResult(
            code=FilterCode.DAILY_LOSS,
            blocked=blocked,
            observed=round(loss_pct, 2),
            threshold=self._settings.daily_loss_pct,
            message=(
                f"pérdida diaria {loss_pct:.2f}% ≥ {self._settings.daily_loss_pct:.2f}%"
                if blocked
                else f"pérdida diaria {loss_pct:.2f}%"
            ),
        )

    # -- utilidades -----------------------------------------------------------

    def _rollover_blocked(self, moment: time) -> bool:
        buffer = min(self._settings.rollover_buffer_min, 59)
        if buffer <= 0:
            return False
        return moment >= time(23, 60 - buffer) or moment < time(0, buffer)


def _in_window(moment: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end
