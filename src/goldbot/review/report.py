"""Informe de la revisión semanal: texto para leer y JSON para comparar semanas."""

from __future__ import annotations

from typing import Any

from goldbot.review.weekly import AuditFinding, WeeklyReview

_LINE = "=" * 72


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/d"
    if value == float("inf"):
        return "inf"
    return f"{value:.{digits}f}"


def _counts(mapping: dict[str, int], *, width: int = 16) -> list[str]:
    if not mapping:
        return []
    return [f"  {key:<{width}} {value:>6}" for key, value in sorted(mapping.items(), key=lambda item: -item[1])]


class WeeklyReportBuilder:
    """Pinta la revisión semanal en columnas monoespaciadas, sin decimales fingidos."""

    def build(self, review: WeeklyReview, *, title: str = "REVISIÓN SEMANAL — operación en DEMO") -> str:
        lines = [
            _LINE,
            title,
            _LINE,
            f"Ventana            : {review.start_utc:%Y-%m-%d %H:%M} -> {review.end_utc:%Y-%m-%d %H:%M} UTC "
            f"({review.window_days} días)",
            f"Base               : {review.db_path} (solo lectura)",
            f"Reloj del servidor : {review.server_zone} · offset "
            f"{_fmt(review.bot_state.get('server_offset_min') and float(review.bot_state['server_offset_min']) / 60.0, 1)} h",
            f"Cuenta             : {_account_line(review)}",
            f"Estado             : {_state_line(review)}",
            _equity_line(review),
            "",
        ]
        lines.extend(_days_section(review))
        lines.extend(_activity_section(review))
        lines.extend(_trades_section(review))
        lines.extend(_health_section(review))
        lines.extend(_audit_section(review.audits))
        lines.extend(_reading_section(review))
        for note in review.notes:
            lines.append(f"  nota: {note}")
        lines.extend([_LINE, ""])
        return "\n".join(lines)


def _account_line(review: WeeklyReview) -> str:
    account = review.account
    demo = "DEMO" if account.get("is_demo") else "NO ES DEMO"
    parts = [str(account.get("login") or "n/d"), str(account.get("server") or "servidor n/d"), demo]
    balance = account.get("balance")
    if balance is not None:
        parts.append(f"balance {_fmt(float(balance))} {account.get('currency') or ''}".strip())
    return " · ".join(parts)


def _state_line(review: WeeklyReview) -> str:
    state = review.bot_state
    return (
        f"{state.get('state') or 'n/d'} · pausa={'sí' if state.get('paused') else 'no'} · "
        f"feed={state.get('feed_status') or 'n/d'} · kill switch="
        f"{'ACTIVO (' + str(state.get('kill_reason')) + ')' if state.get('kill_switch_latched') else 'off'} · "
        f"último ciclo {state.get('last_cycle_at_utc') or 'n/d'}"
    )


def _equity_line(review: WeeklyReview) -> str:
    account = review.account
    equity = account.get("equity")
    if equity is None:
        return "Equity             : n/d"
    return (
        f"Equity             : {_fmt(float(equity))} USD · margen libre "
        f"{_fmt(float(account.get('margin_free') or 0.0))} USD · "
        f"posiciones abiertas {review.open_positions}"
    )


def _days_section(review: WeeklyReview) -> list[str]:
    lines = ["", "Por día operativo (hora del servidor):"]
    if not review.days:
        return lines + ["  (la ventana no dejó ningún día con actividad registrada)"]
    lines.append(
        f"  {'día':<12}{'dec.':>7}{'ent.':>6}{'cerr.':>7}{'net USD':>10}"
        f"{'realiz USD':>12}{'equity fin':>12}  lock  bloqueos"
    )
    for day in review.days:
        blocked = ", ".join(f"{code}:{count}" for code, count in sorted(day.blocked_by_filter.items()))
        lines.append(
            f"  {day.trading_day.isoformat():<12}{day.decisions:>7}{day.entries:>6}"
            f"{day.closed_trades:>7}{_fmt(day.net_profit_usd):>10}"
            f"{_fmt(day.realized_pnl):>12}{_fmt(day.equity_end):>12}"
            f"  {'sí' if day.daily_lock_latched else 'no':<5}{blocked or '-'}"
        )
    return lines


def _activity_section(review: WeeklyReview) -> list[str]:
    lines = [
        "",
        "Actividad del núcleo de decisión:",
        f"  Decisiones         {review.decisions_total}",
        f"  Días con decisión  {review.decision_days} · con entrada {review.entry_days} "
        f"· sin operar {_fmt(review.no_trade_days_pct, 1)} %",
        f"  Velas de decisión con más de una fila: {review.duplicated_bars} "
        f"(reinicios en la ventana: {review.restarts})",
        "  Acciones:",
    ]
    lines.extend(_counts(dict(review.actions), width=12) or ["    (ninguna)"])
    lines.append("  Bloqueos por filtro (una decisión puede acumular varios):")
    lines.extend(_counts(dict(review.blocked_by_filter), width=12) or ["    (ninguno)"])
    if review.playbooks_selected:
        lines.append("  Playbooks seleccionados:")
        lines.extend(_counts(dict(review.playbooks_selected)))
    if review.regimes_at_entry:
        lines.append("  Régimen en la entrada:")
        lines.extend(_counts(dict(review.regimes_at_entry)))
    lines.append(
        f"  Confianza del régimen: media {_fmt(review.confidence_avg, 4)} · "
        f"mínima {_fmt(review.confidence_min, 4)} · decisiones por debajo del umbral: "
        f"{review.low_confidence_decisions}"
    )
    return lines


def _trades_section(review: WeeklyReview) -> list[str]:
    group = review.group
    lines = ["", f"Operaciones cerradas en la ventana ({group.trades}):"]
    if not review.trades:
        lines.append("  (ninguna operación cerrada y reconciliada en la ventana)")
        return lines
    lines.extend(
        [
            f"  Ganadoras/perdedoras {group.wins}/{group.losses} (acierto {_fmt(group.win_rate_pct, 1)} %)",
            f"  Expectancy           {_fmt(group.expectancy_r)} R "
            f"(t = {('n/a' if not review.group_dispersion else _fmt(review.group_t))} · "
            f"sigma {_fmt(group.std_r)} R)",
            f"  Profit factor        {_fmt(group.profit_factor)}",
            f"  PnL neto             {_fmt(group.net_profit_usd)} USD · máx. drawdown "
            f"{_fmt(group.max_drawdown_usd)} USD",
            f"  Pérdidas seguidas    {group.max_consecutive_losses}",
            f"  Distribución R       "
            f"{', '.join(f'{bucket}:{count}' for bucket, count in review.r_distribution.items()) or 'n/d'}",
            f"  Riesgo real máximo   {_fmt(review.risk_pct_actual_max, 4)} % del equity",
            "  Motivos de salida:",
        ]
    )
    lines.extend(_counts(dict(review.exit_reasons), width=12))
    lines.append("  Quién registró el motivo:")
    lines.extend(_counts(dict(review.exit_sources), width=12))
    lines.append(f"  {'cierre':<18}{'ticket':>9}{'lado':>6}{'lote':>6}{'R':>8}{'net USD':>10}  salida       playbook/régimen")
    for trade in review.trades:
        lines.append(
            f"  {trade.close_time_utc:%Y-%m-%d %H:%M}    {trade.position_id:>9}"
            f"{trade.side.value:>6}{_fmt(trade.volume, 2):>6}"
            f"{_fmt(trade.r_multiple):>8}{_fmt(trade.net_profit):>10}"
            f"  {trade.exit_reason.value:<12}{trade.playbook_id or 'n/d'}/{trade.regime_at_entry.value if trade.regime_at_entry else 'n/d'}"
        )
    return lines


def _health_section(review: WeeklyReview) -> list[str]:
    tolerated = sum(1 for gap in review.gaps if gap.tolerated)
    lines = [
        "",
        "Salud del enlace y de la ejecución:",
        f"  Snapshots de equity {review.equity_points} · huecos {len(review.gaps)} "
        f"({tolerated} de fin de semana)",
        f"  Intentos de orden   {review.order_attempts} · sin retcode DONE {review.order_failures}",
        f"  Niveles de log      "
        f"{', '.join(f'{level}:{count}' for level, count in sorted(review.log_levels.items())) or 'n/d'}",
        f"  Eventos             "
        f"{', '.join(f'{etype}:{count}' for etype, count in sorted(review.events_by_type.items())) or 'n/d'}",
    ]
    worst = sorted((gap for gap in review.gaps if not gap.tolerated), key=lambda gap: -gap.duration_s)[:3]
    for gap in worst:
        lines.append(
            f"    hueco {gap.start_utc:%Y-%m-%d %H:%M} -> {gap.end_utc:%Y-%m-%d %H:%M} UTC "
            f"({gap.duration_s / 60.0:.0f} min)"
        )
    for event in review.notable_events[-8:]:
        lines.append(f"    {event['ts_utc']}  {event['type']:<11} {event['message'] or ''}")
    return lines


def _audit_section(audits: tuple[AuditFinding, ...]) -> list[str]:
    lines = ["", "Auditoría de candados:"]
    order = {"FALLO": 0, "WARN": 1, "OK": 2}
    for finding in sorted(audits, key=lambda item: (order[item.status], item.code)):
        lines.append(f"  [{finding.status:<5}] {finding.code:<24} {finding.observed}")
        if finding.status != "OK" and finding.detail:
            lines.append(f"          └ {finding.detail}")
    return lines


def _reading_section(review: WeeklyReview) -> list[str]:
    group = review.group
    lines = ["", "Lectura de la semana:"]
    failed = review.failed_audits
    if failed:
        lines.append(
            f"  · {len(failed)} comprobación(es) dura(s) FALLARON: "
            f"{', '.join(item.code for item in failed)}. Esto no es un matiz estadístico: "
            "un candado que no se cumple hay que arreglarlo antes de seguir operando."
        )
    if not review.decisions_total:
        lines.append(
            "  · No hubo ni una decisión en la ventana. Puede ser fin de semana, pausa o "
            "enlace caído; sea lo que sea, la revisión no puede afirmar nada del comportamiento."
        )
        return lines
    if not review.trades:
        lines.append(
            "  · Cero operaciones cerradas: el DecisionLog explica el porqué, pero esta semana "
            "no aporta nada al resultado económico."
        )
    else:
        if review.group_dispersion and abs(review.group_t) >= 2.0:
            direction = "positiva" if group.expectancy_r > 0 else "negativa"
            lines.append(
                f"  · La expectancy de la semana es {_fmt(group.expectancy_r)} R con |t| = "
                f"{_fmt(abs(review.group_t))}: se sostiene sobre cero, en dirección {direction}, "
                f"sobre {group.trades} operación(es). Una semana no valida una estrategia."
            )
        else:
            lines.append(
                f"  · Con {group.trades} operación(es) y |t| < 2 no hay base estadística para "
                "concluir nada, ni en un sentido ni en el contrario."
            )
    if review.no_trade_days_pct >= 50.0 and review.decision_days:
        lines.append(
            f"  · El {_fmt(review.no_trade_days_pct, 1)} % de los días con vela de decisión "
            "terminaron sin operar. Es un resultado, no un fallo: el candado de 2 operaciones "
            "diarias y los filtros existen para abstenerse."
        )
    news = int(review.blocked_by_filter.get("NEWS", 0))
    if news and news == max(review.blocked_by_filter.values()):
        lines.append(
            f"  · El filtro que más bloqueó fue NEWS ({news} decisiones). Si el calendario "
            "exportado no cubre la semana, eso es falta de datos y no criterio del bot: "
            "reexportar con `InpDaysBack` grande antes de leer nada en el informe."
        )
    if review.risk_pct_actual_max is None and group.trades:
        lines.append(
            "  · Hay operaciones sin riesgo registrado en la decisión: la auditoría de 1 % "
            "no tiene con qué comprobarse, hay que mirar el origen del trade."
        )
    if not review.warning_audits and not failed:
        lines.append("  · Todas las comprobaciones duras pasaron.")
    return lines


def weekly_review_to_jsonable(review: WeeklyReview) -> dict[str, Any]:
    """La misma revisión en estructura, para comparar semana a semana o alimentar un panel."""
    group = review.group
    return {
        "generated_at_utc": review.generated_at_utc.isoformat(),
        "window_days": review.window_days,
        "start_utc": review.start_utc.isoformat(),
        "end_utc": review.end_utc.isoformat(),
        "db_path": review.db_path,
        "server_zone": review.server_zone,
        "account": dict(review.account),
        "bot_state": dict(review.bot_state),
        "days": [
            {
                "trading_day": day.trading_day.isoformat(),
                "decisions": day.decisions,
                "entries": day.entries,
                "actions": dict(day.actions),
                "blocked_by_filter": dict(day.blocked_by_filter),
                "closed_trades": day.closed_trades,
                "net_profit_usd": day.net_profit_usd,
                "realized_pnl": day.realized_pnl,
                "equity_end": day.equity_end,
                "daily_lock_latched": day.daily_lock_latched,
            }
            for day in review.days
        ],
        "decisions_total": review.decisions_total,
        "decision_days": review.decision_days,
        "entry_days": review.entry_days,
        "no_trade_days_pct": review.no_trade_days_pct,
        "duplicated_bars": review.duplicated_bars,
        "restarts": review.restarts,
        "actions": dict(review.actions),
        "blocked_by_filter": dict(review.blocked_by_filter),
        "playbooks_selected": dict(review.playbooks_selected),
        "regimes_at_entry": dict(review.regimes_at_entry),
        "confidence": {
            "avg": review.confidence_avg,
            "min": review.confidence_min,
            "below_threshold": review.low_confidence_decisions,
        },
        "risk_pct_actual_max": review.risk_pct_actual_max,
        "metrics": {
            "trades": group.trades,
            "wins": group.wins,
            "losses": group.losses,
            "win_rate_pct": group.win_rate_pct,
            "expectancy_r": group.expectancy_r,
            "std_r": group.std_r,
            "t_statistic": review.group_t,
            "has_dispersion": review.group_dispersion,
            "profit_factor": group.profit_factor,
            "net_profit_usd": group.net_profit_usd,
            "avg_win_r": group.avg_win_r,
            "avg_loss_r": group.avg_loss_r,
            "max_consecutive_losses": group.max_consecutive_losses,
            "max_drawdown_usd": group.max_drawdown_usd,
            "with_r": group.with_r,
        },
        "r_distribution": dict(review.r_distribution),
        "exit_reasons": dict(review.exit_reasons),
        "exit_sources": dict(review.exit_sources),
        "trades": [
            {
                "position_id": trade.position_id,
                "side": trade.side.value,
                "volume": trade.volume,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "sl": trade.sl,
                "tp": trade.tp,
                "open_time_utc": trade.open_time_utc.isoformat(),
                "close_time_utc": trade.close_time_utc.isoformat(),
                "net_profit": trade.net_profit,
                "r_multiple": trade.r_multiple,
                "duration_s": trade.duration_s,
                "exit_reason": trade.exit_reason.value,
                "exit_reason_source": trade.exit_reason_source.value,
                "playbook_id": trade.playbook_id,
                "regime_at_entry": trade.regime_at_entry.value if trade.regime_at_entry else None,
                "decision_id_entry": trade.decision_id_entry,
            }
            for trade in review.trades
        ],
        "events_by_type": dict(review.events_by_type),
        "notable_events": [dict(event) for event in review.notable_events],
        "equity_points": review.equity_points,
        "gaps": [
            {
                "start_utc": gap.start_utc.isoformat(),
                "end_utc": gap.end_utc.isoformat(),
                "duration_s": gap.duration_s,
                "tolerated": gap.tolerated,
            }
            for gap in review.gaps
        ],
        "order_attempts": review.order_attempts,
        "order_failures": review.order_failures,
        "retcodes": dict(review.retcodes),
        "log_levels": dict(review.log_levels),
        "open_positions": review.open_positions,
        "audits": [
            {
                "code": finding.code,
                "status": finding.status,
                "observed": finding.observed,
                "detail": finding.detail,
            }
            for finding in review.audits
        ],
        "notes": list(review.notes),
    }
