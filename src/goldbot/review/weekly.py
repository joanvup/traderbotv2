"""Revisión semanal de la operación en DEMO: agrega el StateStore y audita los candados.

La Fase 7 no consiste en mirar el dashboard un día, sino en poder responder cada semana con
datos persistidos: qué hizo el bot, por qué no hizo más, si los candados funcionaron y si el
enlace con el broker fue fiable. Este módulo lee el SQLite **en modo solo lectura** (una
revisión nunca debe poder escribir en la base que alimenta el trading) y devuelve un objeto
inmutable con los agregados y las auditorías.

Dos decisiones de diseño importan para la honestidad del informe:

- Los candados se auditan contra la **configuración vigente**, no contra un valor recordado:
  si el YAML dice 1 % de riesgo y 2 operaciones al día, eso es lo que se comprueba.
- La ausencia de operación es un resultado. Un informe con cero decisiones no es un fallo del
  script: puede ser fin de semana, pausa o filtro de noticias, y se dice tal cual.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from goldbot.backtest.metrics import (
    GroupStats,
    has_dispersion,
    r_histogram,
    summarize,
    t_statistic,
)
from goldbot.config import AppConfig
from goldbot.domain.types import ExitReason, ExitReasonSource, Regime, Side, TradeRecord
from goldbot.engine.data_service import build_clock
from goldbot.infra.time import UtcClock

AuditStatus = Literal["OK", "WARN", "FALLO"]

#: Hueco mínimo del feed de equity que empieza a contar como hueco (los snapshots van por minuto).
GAP_MIN_S = 300.0
#: A partir de este hueco no tolerado la revisión lo considera un fallo del enlace.
GAP_FAILURE_S = 1800.0
#: Tolerancia numérica para comparar porcentajes de riesgo: no se opera "casi" por encima.
RISK_EPSILON_PCT = 1e-9
#: Retcode de MT5 que significa "orden ejecutada" (TRADE_RETCODE_DONE).
RETCODE_DONE = 10004

#: Eventos que la revisión lista uno por uno en vez de contarlos únicamente.
_NOTABLE_EVENTS = frozenset(
    {"KILL_SWITCH", "KILL_RESET", "ERROR", "RECONNECT", "PAUSE", "RESUME", "CLOSED", "RECONCILE"}
)


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """Una comprobación dura sobre la semana, con lo observado y su veredicto."""

    code: str
    status: AuditStatus
    observed: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DayActivity:
    """Qué pasó en un día operativo visto por el reloj del servidor."""

    trading_day: date
    decisions: int
    entries: int
    actions: Mapping[str, int]
    blocked_by_filter: Mapping[str, int]
    closed_trades: int
    net_profit_usd: float
    realized_pnl: float
    equity_end: float | None
    daily_lock_latched: bool


@dataclass(frozen=True, slots=True)
class FeedGap:
    """Hueco entre dos snapshots de equity consecutivos."""

    start_utc: datetime
    end_utc: datetime
    duration_s: float
    tolerated: bool


@dataclass(frozen=True, slots=True)
class WeeklyReview:
    """Todo lo que la revisión semanal sabe de la ventana mirada."""

    generated_at_utc: datetime
    window_days: int
    start_utc: datetime
    end_utc: datetime
    db_path: str
    server_zone: str
    account: Mapping[str, Any]
    bot_state: Mapping[str, Any]
    days: tuple[DayActivity, ...]
    decisions_total: int
    actions: Mapping[str, int]
    blocked_by_filter: Mapping[str, int]
    regimes_at_entry: Mapping[str, int]
    playbooks_selected: Mapping[str, int]
    confidence_avg: float | None
    confidence_min: float | None
    low_confidence_decisions: int
    duplicated_bars: int
    restarts: int
    decision_days: int
    entry_days: int
    no_trade_days_pct: float
    trades: tuple[TradeRecord, ...]
    group: GroupStats
    group_dispersion: bool
    group_t: float
    r_distribution: Mapping[str, int]
    exit_reasons: Mapping[str, int]
    exit_sources: Mapping[str, int]
    risk_pct_actual_max: float | None
    events_by_type: Mapping[str, int]
    notable_events: tuple[Mapping[str, Any], ...]
    equity_points: int
    gaps: tuple[FeedGap, ...]
    order_attempts: int
    order_failures: int
    retcodes: Mapping[str, int]
    log_levels: Mapping[str, int]
    open_positions: int
    audits: tuple[AuditFinding, ...]
    notes: tuple[str, ...]

    @property
    def failed_audits(self) -> tuple[AuditFinding, ...]:
        """Solo las comprobaciones que no pasaron; la CLI sale con código distinto de cero."""
        return tuple(finding for finding in self.audits if finding.status == "FALLO")

    @property
    def warning_audits(self) -> tuple[AuditFinding, ...]:
        return tuple(finding for finding in self.audits if finding.status == "WARN")


@dataclass(frozen=True, slots=True)
class _DecisionView:
    """Una fila de `decisions` ya interpretada: acción, régimen, filtros y riesgo."""

    ts_utc: datetime | None
    bar_time_utc: str | None
    action: str
    playbook_id: str | None
    confidence: float | None
    blocked_codes: tuple[str, ...]
    risk_pct_actual: float | None
    risk_approved: bool | None

    @property
    def is_entry(self) -> bool:
        """Una entrada que el RiskManager no rechazó; lo demás se registró y no contó."""
        return self.action == "ENTER" and self.risk_approved is not False


class WeeklyReviewCollector:
    """Reúne los datos de la ventana y ejecuta las auditorías de la Fase 7."""

    def __init__(
        self,
        config: AppConfig,
        *,
        db_path: str | Path | None = None,
        window_days: int = 7,
        now_utc: datetime | None = None,
    ) -> None:
        if window_days < 1:
            raise ValueError("la ventana de revisión ha de ser de al menos un día")
        self._config = config
        self._db_path = Path(db_path or config.app.db_path)
        self._window_days = window_days
        self._clock: UtcClock = build_clock(config)
        self._now = (now_utc or datetime.now(UTC)).astimezone(UTC)

    def collect(self) -> WeeklyReview:
        """Lee la base en solo lectura y devuelve el informe completo de la ventana."""
        start = self._now - timedelta(days=self._window_days)
        with _open_read_only(self._db_path) as connection:
            rows = _fetch_window(connection, start)
        decisions = tuple(_decision_view(row) for row in rows["decisions"])
        trades = _trade_records(rows["trades"])
        days = self._day_activities(decisions, trades, rows["daily"])
        gaps = _feed_gaps(
            [stamp for stamp in (_parse_ts(row["ts_utc"]) for row in rows["equity"]) if stamp],
            clock=self._clock,
        )
        return self._build(decisions, trades, days, gaps, rows, start)

    # -- agregados -----------------------------------------------------------

    def _day_activities(
        self,
        decisions: Sequence[_DecisionView],
        trades: Sequence[TradeRecord],
        daily_rows: Sequence[sqlite3.Row],
    ) -> tuple[DayActivity, ...]:
        """Un `DayActivity` por día operativo con decisiones, operaciones o estadística propia."""
        by_day: dict[date, dict[str, Any]] = {}

        def bucket(day: date) -> dict[str, Any]:
            return by_day.setdefault(
                day,
                {
                    "decisions": 0,
                    "entries": 0,
                    "actions": Counter(),
                    "blocked": Counter(),
                    "trades": 0,
                    "net": 0.0,
                    "realized": None,
                    "equity_end": None,
                    "latched": False,
                },
            )

        for decision in decisions:
            if decision.ts_utc is None:
                continue
            entry = bucket(self._clock.trading_day(decision.ts_utc))
            entry["decisions"] += 1
            entry["actions"][decision.action] += 1
            for code in decision.blocked_codes:
                entry["blocked"][code] += 1
            if decision.is_entry:
                entry["entries"] += 1

        for trade in trades:
            entry = bucket(self._clock.trading_day(trade.close_time_utc))
            entry["trades"] += 1
            entry["net"] += trade.net_profit

        for row in daily_rows:
            parsed = _parse_date(row["trading_day"])
            if parsed is None:
                continue
            entry = bucket(parsed)
            entry["realized"] = float(row["realized_pnl"] or 0.0)
            entry["equity_end"] = float(row["equity_end"]) if row["equity_end"] is not None else None
            entry["latched"] = int(row["daily_lock_latched"] or 0) == 1

        activities: list[DayActivity] = []
        for day in sorted(by_day):
            data = by_day[day]
            activities.append(
                DayActivity(
                    trading_day=day,
                    decisions=int(data["decisions"]),
                    entries=int(data["entries"]),
                    actions=dict(data["actions"]),
                    blocked_by_filter=dict(data["blocked"]),
                    closed_trades=int(data["trades"]),
                    net_profit_usd=float(data["net"]),
                    realized_pnl=float(data["realized"] or 0.0),
                    equity_end=data["equity_end"],
                    daily_lock_latched=bool(data["latched"]),
                )
            )
        return tuple(activities)

    # -- construcción del informe --------------------------------------------

    def _build(
        self,
        decisions: tuple[_DecisionView, ...],
        trades: tuple[TradeRecord, ...],
        days: tuple[DayActivity, ...],
        gaps: tuple[FeedGap, ...],
        rows: Mapping[str, Any],
        start: datetime,
    ) -> WeeklyReview:
        actions = Counter(decision.action for decision in decisions if decision.action)
        blocked = Counter(code for decision in decisions for code in decision.blocked_codes)
        regimes = Counter(
            trade.regime_at_entry.value for trade in trades if trade.regime_at_entry is not None
        )
        playbooks = Counter(decision.playbook_id for decision in decisions if decision.playbook_id)
        confidences = [decision.confidence for decision in decisions if decision.confidence]
        low_conf = sum(
            1
            for decision in decisions
            if decision.confidence is not None
            and decision.confidence < self._config.regime.confidence_min
        )
        bar_counts = Counter(
            decision.bar_time_utc for decision in decisions if decision.bar_time_utc
        )
        duplicated = sum(count - 1 for count in bar_counts.values() if count > 1)
        events = [dict(row) for row in rows["events"]]
        events_by_type = Counter(str(event.get("type")) for event in events)
        restarts = int(events_by_type.get("STARTUP", 0))

        decision_days = len({day.trading_day for day in days if day.decisions > 0})
        entry_days = len({day.trading_day for day in days if day.entries > 0})
        no_trade_pct = (
            0.0
            if decision_days == 0
            else 100.0 * (decision_days - entry_days) / decision_days
        )

        group = summarize(
            trades, key="ventana", initial_balance=self._config.backtest.initial_balance
        )
        risk_pcts = [
            decision.risk_pct_actual
            for decision in decisions
            if decision.risk_pct_actual is not None
        ]
        retcodes = Counter(
            str(row["retcode_name"] or row["retcode"]) for row in rows["attempts"]
        )
        failures = sum(1 for row in rows["attempts"] if int(row["retcode"] or 0) != RETCODE_DONE)
        log_levels = {str(row["level"]): int(row["n"]) for row in rows["log_levels"]}
        bot_state = rows["bot_state"]
        account = _load_json(bot_state.get("account_json")) or {}
        notable = tuple(
            {
                "ts_utc": event.get("ts_utc"),
                "type": event.get("type"),
                "level": event.get("level"),
                "message": event.get("message"),
            }
            for event in events
            if event.get("type") in _NOTABLE_EVENTS
        )

        notes: list[str] = [
            f"`log_lines` solo retiene {self._config.storage.log_lines_retention} líneas: el "
            "conteo por nivel refleja lo que aún queda en la base, no la semana completa."
        ]
        if not decisions:
            notes.append(
                "Sin decisiones en la ventana: el bot no llegó a cerrar una vela de decisión "
                "(parado, en pausa o sin enlace fiable) o la ventana no cubre días de mercado."
            )
        if decisions and not trades:
            notes.append(
                "Hubo decisiones pero ninguna operación cerrada reconciliada en la ventana: "
                "lo escrito aquí es el DecisionLog, no el resultado."
            )
        if self._config.filters.calendar_unavailable_policy == "block":
            notes.append(
                "Con `calendar_unavailable_policy: block`, un calendario sin cobertura bloquea "
                "toda candidata: muchos rechazos por NEWS pueden ser falta de datos, no criterio."
            )

        audits = self._audit(decisions, trades, days, gaps, rows, events_by_type, bot_state, failures)
        return WeeklyReview(
            generated_at_utc=self._now,
            window_days=self._window_days,
            start_utc=start,
            end_utc=self._now,
            db_path=str(self._db_path),
            server_zone=self._config.app.server_timezone,
            account=account,
            bot_state={
                key: bot_state.get(key)
                for key in (
                    "state",
                    "mode",
                    "paused",
                    "feed_status",
                    "kill_switch_latched",
                    "kill_reason",
                    "server_offset_min",
                    "last_cycle_at_utc",
                    "last_heartbeat_utc",
                    "resolved_symbol",
                    "magic",
                    "app_version",
                    "config_hash",
                )
            },
            days=tuple(days),
            decisions_total=len(decisions),
            actions=dict(actions),
            blocked_by_filter=dict(blocked),
            regimes_at_entry=dict(regimes),
            playbooks_selected=dict(playbooks),
            confidence_avg=sum(confidences) / len(confidences) if confidences else None,
            confidence_min=min(confidences) if confidences else None,
            low_confidence_decisions=low_conf,
            duplicated_bars=duplicated,
            restarts=restarts,
            decision_days=decision_days,
            entry_days=entry_days,
            no_trade_days_pct=no_trade_pct,
            trades=trades,
            group=group,
            group_dispersion=has_dispersion(group),
            group_t=t_statistic(group),
            r_distribution=r_histogram(trades),
            exit_reasons={
                reason.value: count for reason, count in Counter(trade.exit_reason for trade in trades).items()
            },
            exit_sources={
                source.value: count
                for source, count in Counter(trade.exit_reason_source for trade in trades).items()
            },
            risk_pct_actual_max=max(risk_pcts) if risk_pcts else None,
            events_by_type=dict(events_by_type),
            notable_events=notable,
            equity_points=len(rows["equity"]),
            gaps=tuple(gaps),
            order_attempts=len(rows["attempts"]),
            order_failures=failures,
            retcodes=dict(retcodes),
            log_levels=log_levels,
            open_positions=len(rows["positions"]),
            audits=audits,
            notes=tuple(notes),
        )

    # -- auditorías ----------------------------------------------------------

    def _audit(
        self,
        decisions: tuple[_DecisionView, ...],
        trades: tuple[TradeRecord, ...],
        days: tuple[DayActivity, ...],
        gaps: tuple[FeedGap, ...],
        rows: Mapping[str, Any],
        events_by_type: Mapping[str, int],
        bot_state: Mapping[str, Any],
        order_failures: int,
    ) -> tuple[AuditFinding, ...]:
        """Comprueba los candados duros de la especificación contra los datos de la semana."""
        risk = self._config.risk
        findings: list[AuditFinding] = []

        is_demo = str(bot_state.get("mode") or "") == "DEMO" or _account_flag(bot_state, "is_demo")
        findings.append(
            AuditFinding(
                code="DEMO",
                status="OK" if is_demo else "FALLO",
                observed=(
                    f"modo={bot_state.get('mode') or 'n/d'} · cuenta="
                    f"{_account_value(bot_state, 'login') or 'n/d'} · "
                    f"servidor={_account_value(bot_state, 'server') or 'n/d'}"
                ),
                detail="" if is_demo else "el StateStore no registra una cuenta DEMO",
            )
        )

        max_entries = max((day.entries for day in days), default=0)
        entries_used = max((int(row["entries_used"] or 0) for row in rows["daily"]), default=0)
        observed_entries = max(max_entries, entries_used)
        findings.append(
            AuditFinding(
                code="MAX_ENTRADAS_DIA",
                status="OK" if observed_entries <= risk.max_trades_day else "FALLO",
                observed=(
                    f"{observed_entries} entrada(s) en el día con más "
                    f"(límite {risk.max_trades_day})"
                ),
                detail=""
                if observed_entries <= risk.max_trades_day
                else "se violó el candado de operaciones diarias",
            )
        )

        without_sl = [str(trade.position_id) for trade in trades if trade.sl is None]
        without_sl += [str(row["ticket"]) for row in rows["positions"] if row["sl"] is None]
        findings.append(
            AuditFinding(
                code="SL_OBLIGATORIO",
                status="OK" if not without_sl else "FALLO",
                observed=f"{len(without_sl)} posición(es) sin stop registrado",
                detail="" if not without_sl else f"ticket(s): {', '.join(without_sl)}",
            )
        )

        risk_pcts = [
            decision.risk_pct_actual
            for decision in decisions
            if decision.risk_pct_actual is not None
        ]
        over = [value for value in risk_pcts if value > risk.risk_pct + RISK_EPSILON_PCT]
        findings.append(
            AuditFinding(
                code="RIESGO_POR_OPERACION",
                status="FALLO" if over else ("OK" if risk_pcts else "WARN"),
                observed=(
                    f"máximo {max(risk_pcts):.4f} % (límite {risk.risk_pct} %)"
                    if risk_pcts
                    else "sin decisiones con riesgo calculado en la ventana"
                ),
                detail=(
                    f"{len(over)} decisión(es) abrieron por encima del riesgo configurado"
                    if over
                    else "el riesgo real coincide con el configurado"
                    if risk_pcts
                    else "no hubo entradas que auditar"
                ),
            )
        )

        latched = [str(row["trading_day"]) for row in rows["daily"] if int(row["daily_lock_latched"] or 0)]
        findings.append(
            AuditFinding(
                code="CANDADO_DIARIO",
                status="WARN" if latched else "OK",
                observed=f"día(s) bloqueado(s) por pérdida: {', '.join(latched) or 'ninguno'}",
                detail=(
                    f"límite {risk.daily_loss_pct} % del equity del día; revisar si hubo "
                    "entradas después del bloqueo"
                    if latched
                    else "ningún día llegó al límite de pérdida"
                ),
            )
        )

        floor = risk.equity_floor_usd
        below_days = [
            str(row["trading_day"])
            for row in rows["daily"]
            if row["equity_end"] is not None
            and float(row["equity_end"]) < floor
            and not int(row["daily_lock_latched"] or 0)
        ]
        equity_now = _account_value(bot_state, "equity")
        below_now = equity_now is not None and float(equity_now) < floor
        findings.append(
            AuditFinding(
                code="EQUITY_FLOOR",
                status="FALLO" if (below_days or below_now) else "OK",
                observed=(
                    f"equity {float(equity_now):.2f} USD · suelo {floor:.2f} USD · "
                    f"día(s) por debajo sin bloqueo: {', '.join(below_days) or 'ninguno'}"
                    if equity_now is not None
                    else f"suelo {floor:.2f} USD sin equity registrado que comparar"
                ),
                detail="por debajo del suelo el kill switch tiene que estar latcheado",
            )
        )

        margin_floor = self._config.safety.min_free_margin_pct
        margin_pcts = [
            (parsed, float(row["margin_free"]) / float(row["equity"]) * 100.0)
            for row in rows["equity"]
            if (parsed := _parse_ts(row["ts_utc"])) is not None
            and row["margin_free"] is not None
            and float(row["equity"]) > 0.0
        ]
        low_margin = [(stamp, pct) for stamp, pct in margin_pcts if pct < margin_floor]
        entries_after_margin_low = max(
            (_entries_after(decisions, stamp) for stamp, _ in low_margin), default=0
        )
        findings.append(
            AuditFinding(
                code="MARGEN_LIBRE",
                status=(
                    "FALLO"
                    if entries_after_margin_low
                    else ("WARN" if low_margin or not margin_pcts else "OK")
                ),
                observed=(
                    f"margen libre mínimo observado "
                    f"{min((pct for _, pct in margin_pcts), default=0.0):.1f}% · "
                    f"umbral {margin_floor:.1f}% · {len(low_margin)} snapshot(s) por debajo · "
                    f"{len(margin_pcts)} con margen registrado"
                ),
                detail=(
                    ""
                    if margin_pcts and not low_margin
                    else (
                        "los snapshots no traen `margin_free`: no se puede auditar el margen"
                        if not margin_pcts
                        else f"{entries_after_margin_low} entrada(s) con el margen ya por debajo del umbral"
                    )
                ),
            )
        )

        fatal = [gap for gap in gaps if not gap.tolerated and gap.duration_s >= GAP_FAILURE_S]
        minor = [gap for gap in gaps if not gap.tolerated and gap.duration_s < GAP_FAILURE_S]
        stale = int(events_by_type.get("FEED_STALE", 0))
        no_feed = not rows["equity"]
        findings.append(
            AuditFinding(
                code="HUECOS_FEED",
                status="FALLO"
                if fatal or no_feed
                else ("WARN" if minor or stale else "OK"),
                observed=(
                    f"{len(fatal)} hueco(s) ≥ {int(GAP_FAILURE_S // 60)} min · "
                    f"{len(minor)} menor(es) · {stale} aviso(s) FEED_STALE · "
                    f"{len(rows['equity'])} snapshot(s)"
                ),
                detail="los huecos se informan, nunca se interpolan",
            )
        )

        reconnects = int(events_by_type.get("RECONNECT", 0))
        findings.append(
            AuditFinding(
                code="RECONEXIONES",
                status="WARN" if reconnects >= 3 else "OK",
                observed=f"{reconnects} evento(s) RECONNECT en la ventana",
                detail="el backoff reconecta; muchas reconexiones son microcaídas del enlace",
            )
        )

        error_events = int(events_by_type.get("ERROR", 0))
        error_logs = _log_level(rows["log_levels"], "ERROR")
        findings.append(
            AuditFinding(
                code="ERRORES",
                status="WARN" if (error_events or error_logs) else "OK",
                observed=f"{error_events} evento(s) ERROR · {error_logs} línea(s) de log en ERROR",
                detail="un error aislado no para el bot; varios seguidos sí, por el kill switch",
            )
        )

        findings.append(
            AuditFinding(
                code="RETCODES",
                status="WARN" if order_failures else "OK",
                observed=f"{order_failures} intento(s) de orden sin retcode {RETCODE_DONE}",
                detail="el reintento está contemplado; conviene ver qué retcode fue",
            )
        )

        kill = int(events_by_type.get("KILL_SWITCH", 0))
        latched_now = int(bot_state.get("kill_switch_latched") or 0) == 1
        entries_after_kill = _entries_after_kill(decisions, rows["events"])
        findings.append(
            AuditFinding(
                code="KILL_SWITCH",
                status="FALLO" if entries_after_kill else ("WARN" if (kill or latched_now) else "OK"),
                observed=(
                    f"{kill} activación(es) · latcheado ahora={'sí' if latched_now else 'no'} · "
                    f"entradas después del kill={entries_after_kill}"
                ),
                detail=""
                if not (kill or latched_now)
                else f"motivo registrado: {bot_state.get('kill_reason') or 'n/d'}",
            )
        )

        repeats = self._repeated_bars(decisions)
        findings.append(
            AuditFinding(
                code="DECISIONES_DUPLICADAS",
                status="WARN" if repeats else "OK",
                observed=(
                    f"{repeats} fila(s) repetida(s) sobre velas de decisión ya vistas "
                    f"({len(decisions)} fila(s) en total)"
                ),
                detail=(
                    "varios procesos sobre la misma vela (reinicios o `--once`): no cambian el "
                    "sentido del informe, pero dicen cuántas veces se reinició el bot"
                    if repeats
                    else "cada vela de decisión quedó registrada una sola vez"
                ),
            )
        )

        findings.append(
            AuditFinding(
                code="REGISTRO",
                status="WARN" if not decisions else "OK",
                observed=f"{len(decisions)} decisión(es) en {self._window_days} día(s)",
                detail=(
                    "sin decisiones el DecisionLog no dice nada de la semana"
                    if not decisions
                    else ""
                ),
            )
        )
        return tuple(findings)

    @staticmethod
    def _repeated_bars(decisions: Sequence[_DecisionView]) -> int:
        counts = Counter(decision.bar_time_utc for decision in decisions if decision.bar_time_utc)
        return sum(count - 1 for count in counts.values() if count > 1)


# -- auxiliares --------------------------------------------------------------


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    """Abre la base en modo `ro`: la revisión no puede escribir en el estado del bot."""
    if not db_path.exists():
        raise FileNotFoundError(f"no existe la base {db_path}; el bot todavía no dejó StateStore")
    connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _fetch_window(connection: sqlite3.Connection, start: datetime) -> dict[str, Any]:
    """Trae las filas de la ventana. `log_lines` se agrega en SQL porque es la tabla voluminosa."""
    since = start.isoformat().replace("+00:00", "Z")
    columns = (
        "decision_id",
        "ts_utc",
        "bar_time_utc",
        "action",
        "playbook_id",
        "regime_json",
        "filters_json",
        "risk_json",
        "rationale",
    )
    return {
        "decisions": connection.execute(
            f"select {', '.join(columns)} from decisions "
            "where substr(ts_utc, 1, 19) >= substr(?, 1, 19) order by ts_utc",
            (since,),
        ).fetchall(),
        "trades": connection.execute(
            "select * from trades where substr(close_time_utc, 1, 19) >= substr(?, 1, 19) "
            "order by close_time_utc",
            (since,),
        ).fetchall(),
        "daily": connection.execute(
            "select * from daily_stats where substr(trading_day, 1, 10) >= substr(?, 1, 10) "
            "order by trading_day",
            (since,),
        ).fetchall(),
        "events": connection.execute(
            "select ts_utc, source, type, level, message, payload_json from events "
            "where substr(ts_utc, 1, 19) >= substr(?, 1, 19) order by ts_utc",
            (since,),
        ).fetchall(),
        "equity": connection.execute(
            "select ts_utc, balance, equity, margin_free, floating_pnl, positions_count, source "
            "from equity_snapshots where substr(ts_utc, 1, 19) >= substr(?, 1, 19) order by ts_utc",
            (since,),
        ).fetchall(),
        "attempts": connection.execute(
            "select ts_utc, decision_id, retcode, retcode_name, retryable from order_attempts "
            "where substr(ts_utc, 1, 19) >= substr(?, 1, 19) order by ts_utc",
            (since,),
        ).fetchall(),
        "log_levels": connection.execute(
            "select level, count(*) as n from log_lines "
            "where substr(ts_utc, 1, 19) >= substr(?, 1, 19) group by level",
            (since,),
        ).fetchall(),
        "positions": connection.execute("select ticket, sl, tp, opened_at_utc from positions").fetchall(),
        "bot_state": dict(
            connection.execute("select * from bot_state where id = 1").fetchone() or {}
        ),
    }


def _decision_view(row: sqlite3.Row) -> _DecisionView:
    regime = _load_json(row["regime_json"]) or {}
    filters = _load_json(row["filters_json"]) or {}
    risk = _load_json(row["risk_json"]) or {}
    results = filters.get("results", []) if isinstance(filters, Mapping) else []
    blocked = tuple(
        str(item.get("code"))
        for item in results
        if isinstance(item, Mapping) and item.get("blocked")
    )
    confidence = regime.get("confidence") if isinstance(regime, Mapping) else None
    return _DecisionView(
        ts_utc=_parse_ts(row["ts_utc"]),
        bar_time_utc=row["bar_time_utc"],
        action=str(row["action"] or ""),
        playbook_id=row["playbook_id"],
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        blocked_codes=blocked,
        risk_pct_actual=_as_float(risk.get("risk_pct_actual") if isinstance(risk, Mapping) else None),
        risk_approved=risk.get("approved") if isinstance(risk, Mapping) else None,
    )


def _trade_records(rows: Iterable[sqlite3.Row]) -> tuple[TradeRecord, ...]:
    """Filas de `trades` -> `TradeRecord`, para reutilizar las métricas del backtest."""
    records: list[TradeRecord] = []
    for row in rows:
        opened = _parse_ts(row["open_time_utc"])
        closed = _parse_ts(row["close_time_utc"])
        if opened is None or closed is None:
            continue
        records.append(
            TradeRecord(
                position_id=int(row["position_id"]),
                symbol=str(row["symbol"] or ""),
                side=Side(str(row["side"])),
                volume=float(row["volume"] or 0.0),
                entry_price=float(row["entry_price"] or 0.0),
                exit_price=float(row["exit_price"] or 0.0),
                sl=_as_float(row["sl"]),
                tp=_as_float(row["tp"]),
                open_time_utc=opened,
                close_time_utc=closed,
                entry_deal_id=row["entry_deal_id"],
                close_deal_id=row["close_deal_id"],
                commission=float(row["commission"] or 0.0),
                swap=float(row["swap"] or 0.0),
                net_profit=float(row["net_profit"] or 0.0),
                r_multiple=_as_float(row["r_multiple"]),
                duration_s=int(row["duration_s"] or 0),
                exit_reason=ExitReason(str(row["exit_reason"])),
                exit_reason_source=ExitReasonSource(str(row["exit_reason_source"])),
                playbook_id=row["playbook_id"],
                regime_at_entry=Regime(str(row["regime_at_entry"])) if row["regime_at_entry"] else None,
                decision_id_entry=row["decision_id_entry"],
                reconciled_at_utc=_parse_ts(row["reconciled_at_utc"]),
            )
        )
    return tuple(records)


def _feed_gaps(stamps: Sequence[datetime], *, clock: UtcClock) -> tuple[FeedGap, ...]:
    """Huecos entre snapshots: un fin de semana de mercado cerrado no es un hueco."""
    gaps: list[FeedGap] = []
    for previous, current in zip(stamps, stamps[1:], strict=False):
        duration = (current - previous).total_seconds()
        if duration < GAP_MIN_S:
            continue
        gaps.append(
            FeedGap(
                start_utc=previous,
                end_utc=current,
                duration_s=duration,
                tolerated=_spans_weekend(previous, current, clock=clock),
            )
        )
    return tuple(gaps)


def _spans_weekend(start_utc: datetime, end_utc: datetime, *, clock: UtcClock) -> bool:
    """Verdadero si algún día del tramo, en hora del servidor, es sábado o domingo."""
    day = clock.trading_day(start_utc)
    last = clock.trading_day(end_utc)
    cursor = day
    while cursor <= last:
        if cursor.weekday() >= 5:
            return True
        cursor = cursor + timedelta(days=1)
    return False


def _entries_after(decisions: Sequence[_DecisionView], moment: datetime | None) -> int:
    """Entradas registradas después de un momento: si hay alguna, el candado no valió."""
    if moment is None:
        return 0
    return sum(
        1
        for decision in decisions
        if decision.is_entry and decision.ts_utc and decision.ts_utc > moment
    )


def _entries_after_kill(decisions: Sequence[_DecisionView], events: Sequence[sqlite3.Row]) -> int:
    """Entradas posteriores al último KILL_SWITCH."""
    kill_ts = next(
        (
            parsed
            for row in reversed(list(events))
            if str(row["type"]) == "KILL_SWITCH" and (parsed := _parse_ts(row["ts_utc"]))
        ),
        None,
    )
    return _entries_after(decisions, kill_ts)


def _account_value(bot_state: Mapping[str, Any], key: str) -> Any:
    account = _load_json(bot_state.get("account_json"))
    if isinstance(account, Mapping):
        return account.get(key)
    return None


def _account_flag(bot_state: Mapping[str, Any], key: str) -> bool:
    return _account_value(bot_state, key) is True


def _log_level(levels: Sequence[sqlite3.Row], level: str) -> int:
    for row in levels:
        if str(row["level"]) == level:
            return int(row["n"])
    return 0


def _parse_ts(value: Any) -> datetime | None:
    """Marca ISO con sufijo `Z` de la base -> `datetime` con zona; `None` si no es legible."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_json(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
