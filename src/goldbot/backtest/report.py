"""Informe del backtest por régimen y playbook, en texto y tablas."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from typing import Any

from goldbot.backtest.metrics import (
    BacktestMetrics,
    BacktestResult,
    GroupStats,
    has_dispersion,
    t_statistic,
)
from goldbot.backtest.walk_forward import WalkForwardFold, WalkForwardReport


def _fmt(value: float, digits: int = 2) -> str:
    if value == float("inf"):
        return "inf"
    return f"{value:.{digits}f}"


class ReportBuilder:
    """Genera el informe final incluyendo días sin operar y honestidad estadística."""

    def build(
        self,
        result: BacktestResult,
        *,
        title: str = "BACKTEST — señal aislada (0,01 lote, una posición a la vez)",
    ) -> str:
        metrics = result.metrics
        lines: list[str] = [
            "=" * 72,
            title,
            "=" * 72,
            f"Ventana            : {result.start_utc} -> {result.end_utc}",
            f"Modelo de régimen  : {result.model_version or 'n/d'}",
            f"Decisiones         : {result.decisions}",
            "",
            "Acciones del núcleo de decisión:",
        ]
        for action, count in sorted(result.policy_counts.items()):
            lines.append(f"  {action:<10} {count:>6}")
        if result.regime_counts:
            lines.append("Régimen en velas de decisión:")
            for regime, count in sorted(result.regime_counts.items()):
                lines.append(f"  {regime:<16} {count:>6}")
        if result.playbook_counts:
            lines.append("Playbooks seleccionados:")
            for playbook_id, count in sorted(result.playbook_counts.items()):
                lines.append(f"  {playbook_id:<16} {count:>6}")
        if result.block_counts:
            lines.append("Bloqueos por filtro (una decisión puede acumular varios):")
            for code, count in sorted(result.block_counts.items()):
                lines.append(f"  {code:<12} {count:>6}")
        lines.extend(
            [
                "",
                "Resultados:",
                f"  Operaciones        {metrics.trades}",
                f"  Ganadoras/perdedoras {metrics.wins}/{metrics.losses} "
                f"(acierto {_fmt(metrics.win_rate, 1)}%)",
                f"  Expectancy         {_fmt(metrics.expectancy_r)} R",
                f"  Profit factor      {_fmt(metrics.profit_factor)}",
                f"  Mejor/peor media   {_fmt(metrics.avg_win_r)} R / {_fmt(metrics.avg_loss_r)} R",
                f"  PnL neto           {_fmt(metrics.net_profit_usd)} USD "
                f"(bruto +{_fmt(metrics.gross_profit_usd)} / -{_fmt(abs(metrics.gross_loss_usd))})",
                f"  Máx. drawdown      {_fmt(metrics.max_drawdown_usd)} USD "
                f"({_fmt(metrics.max_drawdown_pct)}%)",
                f"  Pérdidas seguidas  {metrics.max_consecutive_losses}",
                f"  Días sin operar    {_fmt(metrics.no_trade_days_pct, 1)}%",
            ]
        )
        if metrics.r_distribution:
            lines.append("  Distribución R     "
                         + ", ".join(f"{key}:{value}" for key, value in metrics.r_distribution.items()))
        if result.trades:
            exits = Counter(trade.exit_reason.value for trade in result.trades)
            lines.append("  Motivos de salida  " + ", ".join(f"{k}:{v}" for k, v in exits.items()))
            lines.append("")
            lines.append("Expectancy por régimen (R):")
            for key, value in sorted(metrics.by_regime.items()):
                lines.append(f"  {key:<16} {_fmt(value)}")
            lines.append("Expectancy por playbook (R):")
            for key, value in sorted(metrics.by_playbook.items()):
                lines.append(f"  {key:<16} {_fmt(value)}")
        else:
            lines.extend(
                [
                    "",
                    "Sin operaciones: no se fuerza ninguna entrada. Abstenerse es un",
                    "resultado válido; este backtest no demuestra ventaja estadística.",
                ]
            )
        lines.extend(
            [
                "",
                "Lectura honesta: pocas operaciones => conclusiones débiles. No ajustar",
                "parámetros para 'mejorar' el resultado; la muestra agrupada del",
                "walk-forward (Fase 6) es la que sostiene o tumba esta estrategia.",
                "=" * 72,
            ]
        )
        return "\n".join(lines)


def result_to_jsonable(result: BacktestResult, *, include_equity: bool = True) -> dict[str, Any]:
    metrics = result.metrics
    return {
        "start_utc": result.start_utc.isoformat() if result.start_utc else None,
        "end_utc": result.end_utc.isoformat() if result.end_utc else None,
        "model_version": result.model_version,
        "decisions": result.decisions,
        "policy_counts": dict(result.policy_counts),
        "block_counts": dict(result.block_counts),
        "regime_counts": dict(result.regime_counts),
        "playbook_counts": dict(result.playbook_counts),
        "metrics": _metrics_to_jsonable(metrics),
        "trades": [_trade_to_jsonable(trade) for trade in result.trades],
        "equity_curve": (
            [{"ts_utc": ts.isoformat(), "equity": equity} for ts, equity in result.equity_curve]
            if include_equity
            else []
        ),
    }


def _metrics_to_jsonable(metrics: BacktestMetrics) -> dict[str, Any]:
    return {
        "trades": metrics.trades,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "win_rate": metrics.win_rate,
        "expectancy_r": metrics.expectancy_r,
        "profit_factor": metrics.profit_factor,
        "net_profit_usd": metrics.net_profit_usd,
        "gross_profit_usd": metrics.gross_profit_usd,
        "gross_loss_usd": metrics.gross_loss_usd,
        "avg_win_r": metrics.avg_win_r,
        "avg_loss_r": metrics.avg_loss_r,
        "max_drawdown_usd": metrics.max_drawdown_usd,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "max_consecutive_losses": metrics.max_consecutive_losses,
        "no_trade_days_pct": metrics.no_trade_days_pct,
        "r_distribution": dict(metrics.r_distribution),
        "by_regime": dict(metrics.by_regime),
        "by_playbook": dict(metrics.by_playbook),
    }


def _trade_to_jsonable(trade: Any) -> dict[str, Any]:
    return {
        "position_id": trade.position_id,
        "side": trade.side.value,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "open_time_utc": trade.open_time_utc.isoformat(),
        "close_time_utc": trade.close_time_utc.isoformat(),
        "net_profit": trade.net_profit,
        "r_multiple": trade.r_multiple,
        "exit_reason": trade.exit_reason.value,
        "playbook_id": trade.playbook_id,
        "regime_at_entry": trade.regime_at_entry.value if trade.regime_at_entry else None,
    }


# ---------------------------------------------------------------------------
# Fase 6 — informe del walk-forward
# ---------------------------------------------------------------------------

_RULE = "=" * 72


def _day(value: object) -> str:
    """Día natural de un `datetime` o de un ISO-8601, sin hora ni zona."""
    if value is None:
        return "n/d"
    text = getattr(value, "isoformat", None)
    return str(text())[:10] if callable(text) else str(value)[:10]


def _conclusive(stats: GroupStats, min_cell_trades: int) -> str:
    return "ok" if stats.trades >= min_cell_trades else "no concluyente"


def _t_cell(stats: GroupStats) -> str:
    """`n/a` cuando las R no varían: el estadístico sería un artefacto, no una señal."""
    return _fmt(t_statistic(stats)) if has_dispersion(stats) else "n/a"


def _group_row(stats: GroupStats, *, label_width: int, min_cell_trades: int) -> str:
    return (
        f"  {stats.key:<{label_width}} {stats.trades:>4}  {_fmt(stats.expectancy_r):>8} "
        f"{_t_cell(stats):>6} {_fmt(stats.profit_factor):>6} "
        f"{stats.max_consecutive_losses:>4} {_fmt(stats.max_drawdown_usd):>8} "
        f"{_fmt(stats.net_profit_usd):>9}  {_conclusive(stats, min_cell_trades)}"
    )


def _group_table(
    title: str,
    groups: Collection[GroupStats],
    *,
    label: str,
    min_cell_trades: int,
    label_width: int = 28,
) -> list[str]:
    if not groups:
        return [f"{title}:", "  (sin operaciones que desglosar)", ""]
    header = (
        f"  {label:<{label_width}}  ops   exp R      t     PF  racha "
        f"  DD USD   net USD  veredicto"
    )
    rows = [
        _group_row(stats, label_width=label_width, min_cell_trades=min_cell_trades)
        for stats in groups
    ]
    return [f"{title}:", header, *rows, ""]


def _fold_rows(folds: Sequence[WalkForwardFold]) -> list[str]:
    lines = [
        "Por pliegue:",
        "  #  fin-train  tramo de prueba                velas   ops    exp R"
        "      PF   DD USD   DD %  cal",
    ]
    for fold in folds:
        window = fold.window
        span = f"{_day(window.test_start_utc)} -> {_day(window.test_end_utc)}"
        if fold.result is None:
            reason = fold.error or "sin resultado"
            lines.append(f"  {window.index + 1:>2}  {_day(window.train_end_utc):<10}  {span:<26}  "
                         f"RECHAZADO: {reason}")
            continue
        metrics = fold.result.metrics
        lines.append(
            f"  {window.index + 1:>2}  {_day(window.train_end_utc):<10}  {span:<26}  "
            f"{window.test_bars:>6} {metrics.trades:>4} {metrics.expectancy_r:>8.2f} "
            f"{_fmt(metrics.profit_factor):>7} {_fmt(metrics.max_drawdown_usd):>8} "
            f"{_fmt(metrics.max_drawdown_pct, 1):>6}  {fold.calendar_coverage_pct:>3.0f}%"
        )
    lines.append("")
    return lines


class WalkForwardReportBuilder:
    """El informe que pide la Fase 6: pliegues, agrupado y desglose régimen × playbook.

    Nada aquí endulza el resultado: si la muestra no da para concluir, se dice, y las celdas
    con pocas operaciones se marcan como no concluyentes en vez de interpretarse.
    """

    def build(self, report: WalkForwardReport) -> str:
        lines: list[str] = [
            _RULE,
            "WALK-FORWARD FASE 6 — entrenar en A, probar en B y desplazar la ventana",
            _RULE,
            f"Histórico        : {_day(report.history_start_utc)} -> "
            f"{_day(report.history_end_utc)}",
            f"Ventanas         : entreno {report.train_window_days} d · "
            f"prueba {report.test_window_days} d · paso {report.step_days} d",
            f"Pliegues         : {len(report.ran_folds)} evaluados · "
            f"{len(report.failed_folds)} rechazados · cola sin evaluar "
            f"{report.discarded_tail_days} d",
            f"Días de prueba   : {report.tested_days} "
            f"({report.uncovered_days} d entre pliegues no cubiertos)",
            f"Modelo de régimen: {', '.join(report.model_versions) or 'n/d'}",
            f"Filtro noticias  : {'ACTIVO' if report.news_filter_active else 'desactivado'} · "
            f"cobertura media del calendario {_fmt(report.calendar_coverage_pct, 0)}%",
            "",
        ]
        lines.extend(_fold_rows(report.folds))
        lines.extend(self._aggregate_section(report))
        lines.extend(self._stability_section(report))
        min_cells = report.min_cell_trades
        lines.extend(
            _group_table(
                "Desglose por régimen",
                report.by_regime.values(),
                label="régimen",
                min_cell_trades=min_cells,
                label_width=22,
            )
        )
        lines.extend(
            _group_table(
                "Desglose por playbook",
                report.by_playbook.values(),
                label="playbook",
                min_cell_trades=min_cells,
                label_width=22,
            )
        )
        lines.extend(
            _group_table(
                "Cuadrícula régimen × playbook",
                report.grid.values(),
                label="celda",
                min_cell_trades=min_cells,
            )
        )
        lines.extend(self._verdict(report))
        lines.append(_RULE)
        return "\n".join(lines)

    def _aggregate_section(self, report: WalkForwardReport) -> list[str]:
        metrics = report.aggregate
        distribution = ", ".join(
            f"{bucket}:{count}" for bucket, count in report.r_distribution.items()
        )
        return [
            "Agrupado (todas las operaciones de todos los pliegues):",
            f"  Operaciones        {metrics.trades}",
            f"  Ganadoras/perdedoras {metrics.wins}/{metrics.losses} "
            f"(acierto {_fmt(metrics.win_rate_pct, 1)}%)",
            f"  Expectancy         {_fmt(metrics.expectancy_r)} R "
            f"(t = {_t_cell(metrics)} · sigma {_fmt(metrics.std_r)} R)",
            f"  Profit factor      {_fmt(metrics.profit_factor)}",
            f"  Mejor/peor media   {_fmt(metrics.avg_win_r)} R / {_fmt(metrics.avg_loss_r)} R",
            f"  PnL neto           {_fmt(metrics.net_profit_usd)} USD",
            f"  Máx. drawdown      {_fmt(metrics.max_drawdown_usd)} USD",
            f"  Pérdidas seguidas  {metrics.max_consecutive_losses}",
            f"  Días sin operar    {_fmt(report.no_trade_days_pct, 1)}%",
            f"  Distribución R     {distribution or 'n/d (sin operaciones)'}",
            "",
        ]

    @staticmethod
    def _stability_section(report: WalkForwardReport) -> list[str]:
        expectancies = ", ".join(f"{value:.2f}" for value in report.fold_expectancy_r) or "n/d"
        trades = ", ".join(str(value) for value in report.fold_trades) or "n/d"
        return [
            "Estabilidad entre pliegues:",
            f"  Expectancy por pliegue (R): {expectancies}",
            f"  Mediana                   : {_fmt(report.expectancy_median_r)} R",
            f"  Operaciones por pliegue   : {trades} "
            f"(min {report.min_fold_trades} / max {report.max_fold_trades})",
            f"  Pliegues rentables        : {report.profitable_folds} "
            f"de {len(report.ran_folds)}",
            "",
        ]

    @staticmethod
    def _verdict(report: WalkForwardReport) -> list[str]:
        """Lo que se puede y lo que NO se puede afirmar con estos números."""
        metrics = report.aggregate
        t_value = t_statistic(metrics)
        lines = ["Lectura honesta:"]
        if metrics.trades == 0:
            lines.append(
                "  · Ninguna operación en todo el walk-forward: el playbook no dispara",
            )
            lines.append(
                "    sobre estos datos y este informe no demuestra NINGUNA ventaja.",
            )
        elif abs(t_value) < 2.0:
            lines.append(
                f"  · {metrics.trades} operaciones agrupadas y |t| = {_fmt(abs(t_value))} < 2:",
            )
            lines.append(
                "    la diferencia de la expectancy con cero no es estadísticamente",
            )
            lines.append(
                "    significativa. No hay ventaja demostrada, ni buena ni mala.",
            )
        else:
            lines.append(
                f"  · {metrics.trades} operaciones con |t| = {_fmt(abs(t_value))} >= 2:",
            )
            lines.append(
                f"    la expectancy de {_fmt(metrics.expectancy_r)} R sí se sostiene "
                f"sobre cero",
            )
            lines.append(
                f"    ({'positiva' if metrics.expectancy_r > 0 else 'negativa'}), "
                "aunque sigue siendo"
            )
            lines.append("    una sola estrategia sobre un único símbolo.")
        if report.failed_folds:
            lines.append(
                f"  · {len(report.failed_folds)} pliegues rechazados por el motor: se listan",
            )
            lines.append("    arriba con su motivo, no se han borrado del recuento.")
        if report.fold_trades and report.min_fold_trades == report.max_fold_trades:
            lines.append(
                "  · Todos los pliegues dan el mismo número de operaciones: la aparente",
            )
            lines.append("    estabilidad es que no pasa nada en ninguno.")
        if report.news_filter_active and report.calendar_coverage_pct < 50.0:
            lines.append(
                f"  · El calendario sólo cubre {_fmt(report.calendar_coverage_pct, 0)}% de los",
            )
            lines.append(
                "    tramos de prueba y la política es 'block': fuera de cobertura el",
            )
            lines.append(
                "    filtro de noticias bloquea decisiones que no miden la estrategia,",
            )
            lines.append("    sino la falta de datos.")
        lines.append(
            f"  · Celdas con menos de {report.min_cell_trades} operaciones marcadas como"
            " 'no",
        )
        lines.append("    concluyente': no se interpretan y no se ajustan parámetros")
        lines.append("    para que salgan mejor.")
        lines.append("  · En la columna t, 'n/a' significa que las R de ese grupo salen casi")
        lines.append("    todas del mismo stop: sin varianza el estadístico no demuestra nada")
        lines.append("    y preferimos no pintar un número grande y falso.")
        lines.append("")
        return lines


def walk_forward_to_jsonable(report: WalkForwardReport) -> dict[str, Any]:
    """El mismo informe en JSON: pliegues, agregado y cuadrícula régimen × playbook."""

    def group(stats: GroupStats) -> dict[str, Any]:
        return {
            "key": stats.key,
            "trades": stats.trades,
            "wins": stats.wins,
            "losses": stats.losses,
            "win_rate_pct": stats.win_rate_pct,
            "expectancy_r": stats.expectancy_r,
            "std_r": stats.std_r,
            "t_statistic": t_statistic(stats),
            "has_dispersion": has_dispersion(stats),
            "profit_factor": stats.profit_factor,
            "net_profit_usd": stats.net_profit_usd,
            "avg_win_r": stats.avg_win_r,
            "avg_loss_r": stats.avg_loss_r,
            "max_consecutive_losses": stats.max_consecutive_losses,
            "max_drawdown_usd": stats.max_drawdown_usd,
            "with_r": stats.with_r,
            "conclusive": stats.trades >= report.min_cell_trades,
        }

    return {
        "kind": "walk_forward",
        "windows": {
            "train_days": report.train_window_days,
            "test_days": report.test_window_days,
            "step_days": report.step_days,
        },
        "history_start_utc": _iso(report.history_start_utc),
        "history_end_utc": _iso(report.history_end_utc),
        "tested_days": report.tested_days,
        "uncovered_days": report.uncovered_days,
        "no_trade_days_pct": report.no_trade_days_pct,
        "r_distribution": dict(report.r_distribution),
        "discarded_tail_days": report.discarded_tail_days,
        "initial_balance": report.initial_balance,
        "model_versions": list(report.model_versions),
        "news_filter_active": report.news_filter_active,
        "calendar_coverage_pct": report.calendar_coverage_pct,
        "min_cell_trades": report.min_cell_trades,
        "aggregate": group(report.aggregate),
        "by_regime": [group(stats) for stats in report.by_regime.values()],
        "by_playbook": [group(stats) for stats in report.by_playbook.values()],
        "grid": [
            {
                "regime": regime,
                "playbook": playbook,
                **group(stats),
            }
            for (regime, playbook), stats in report.grid.items()
        ],
        "fold_expectancy_r": list(report.fold_expectancy_r),
        "fold_trades": list(report.fold_trades),
        "expectancy_median_r": report.expectancy_median_r,
        "profitable_folds": report.profitable_folds,
        "folds": [
            {
                "index": fold.window.index,
                "train_start_utc": fold.window.train_start_utc.isoformat(),
                "train_end_utc": fold.window.train_end_utc.isoformat(),
                "test_start_utc": fold.window.test_start_utc.isoformat(),
                "test_end_utc": fold.window.test_end_utc.isoformat(),
                "train_bars": fold.window.train_bars,
                "test_bars": fold.window.test_bars,
                "calendar_coverage_pct": fold.calendar_coverage_pct,
                "error": fold.error,
                "result": (
                    result_to_jsonable(fold.result, include_equity=False)
                    if fold.result is not None
                    else None
                ),
            }
            for fold in report.folds
        ],
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
