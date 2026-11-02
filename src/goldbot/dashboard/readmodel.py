"""Modelo de lectura del dashboard: únicamente `SELECT` sobre la base que escribe el bot.

El dashboard es un proceso SEPARADO del bot: nunca corre en su bucle, nunca llama a MT5 y
nunca escribe estado de trading. Su única escritura admitida es encolar un comando en la
tabla `commands`, y eso ocurre en `api.py` a través del StateStore, no aquí.

Regla de honestidad de datos: si una fuente no se actualizó, se devuelve su antigüedad y
el panel la pinta como "dato atrasado"; nunca se rellena ni se interpola.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from goldbot.domain.interfaces import Clock

# Columnas con JSON que conviene decodificar antes de serializar hacia el navegador.
_JSON_FIELDS = (
    "account_json",
    "health_json",
    "context_json",
    "regime_json",
    "playbook_json",
    "filters_json",
    "probs_json",
    "signal_json",
    "risk_json",
    "model_versions_json",
    "snapshot_json",
    "args_json",
    "result_json",
    "payload_json",
    "extra_json",
    "request_json",
)


def _parse_iso(value: Any) -> datetime | None:
    """Convierte una marca ISO-8601 con sufijo `Z` en `datetime` con zona, o None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class DashboardReadModel:
    """Consultas puntuales y cortas; cada llamada abre y cierra su conexión de lectura."""

    def __init__(self, db_path: str | Path, clock: Clock, *, stale_after_s: int = 15) -> None:
        self._db_path = Path(db_path)
        self._clock = clock
        self._stale_after_s = stale_after_s

    # -- infraestructura de lectura ------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Abre la base en solo lectura; si el WAL no lo permite, abre normal y no escribe.

        `mode=ro` puede fallar si el bot murió dejando el `-wal` sin `-shm` o si el
        directorio no es escribible: en ese caso el dashboard tiene que seguir pintando
        el último dato conocido, así que se abre una conexión ordinaria de la que solo
        se leen filas.
        """
        uri = f"file:{self._db_path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
        except sqlite3.OperationalError:
            conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not self._db_path.exists():
            # El bot aún no ha creado la base: es "sin datos", no un error. Y el dashboard,
            # que es solo lector, no debe ser quien cree el fichero ni el WAL.
            return []
        conn = self._connect()
        try:
            fetched = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for row in fetched:
            item = dict(row)
            for field in _JSON_FIELDS:
                raw = item.get(field)
                if isinstance(raw, str) and raw:
                    try:
                        item[field] = json.loads(raw)
                    except json.JSONDecodeError:
                        item[field] = None
            out.append(item)
        return out

    def db_reachable(self) -> bool:
        """True si la base existe y responde; el dashboard degrada en vez de fallar."""
        if not self._db_path.exists():
            return False
        try:
            self._rows("SELECT 1 AS ok")
        except sqlite3.Error:
            return False
        return True

    # -- estado y decisión ----------------------------------------------------

    def bot_state(self) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM bot_state WHERE id = 1")
        return rows[0] if rows else None

    def latest_decision(self) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM decisions ORDER BY ts_utc DESC, rowid DESC LIMIT 1")
        return rows[0] if rows else None

    def decisions(self, limit: int = 30) -> list[dict[str, Any]]:
        """Últimas decisiones, incluidas las abstenciones: es el DecisionLog del panel."""
        return self._rows(
            """
            SELECT decision_id, cycle_id, ts_utc, bar_time_utc, action, snapshot_hash,
                   config_hash, playbook_id, rationale, latency_ms, regime_json, probs_json,
                   filters_json, signal_json, risk_json
              FROM decisions
             ORDER BY ts_utc DESC, rowid DESC
             LIMIT ?
            """,
            (int(limit),),
        )

    # -- posiciones, operaciones y días ---------------------------------------

    def open_positions(self) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT ticket, position_id, side, volume, entry_price, sl, tp, opened_at_utc,
                   playbook_id, regime_at_entry, decision_id_entry, initial_risk_price,
                   r_multiple, last_action, breakeven_done, partials_done, updated_at_utc,
                   current_price, pnl_money
              FROM positions
             ORDER BY opened_at_utc
            """
        )

    def trades(self, days: int = 30) -> list[dict[str, Any]]:
        since = (self._clock.now_utc() - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        return self._rows(
            """
            SELECT position_id, symbol, side, volume, entry_price, exit_price, sl, tp,
                   open_time_utc, close_time_utc, commission, swap, net_profit, r_multiple,
                   duration_s, exit_reason, exit_reason_source, playbook_id, regime_at_entry,
                   decision_id_entry, reconciled_at_utc
              FROM trades
             WHERE close_time_utc >= ?
             ORDER BY close_time_utc DESC
            """,
            (since,),
        )

    def daily_stats(self, days: int = 14) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT trading_day, tz, realized_pnl, equity_start, equity_end, entries_used,
                   wins, losses, gross_profit, gross_loss, daily_lock_latched, updated_at_utc
              FROM daily_stats
             ORDER BY trading_day DESC
             LIMIT ?
            """,
            (int(days),),
        )

    def day_row(self, trading_day: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM daily_stats WHERE trading_day = ?", (trading_day,))
        return rows[0] if rows else None

    # -- series para gráficos --------------------------------------------------

    def equity_grid(self, hours: int = 24) -> dict[str, Any]:
        """Rejilla de minutos de las últimas `hours` con `None` donde no hay dato.

        Los huecos se devuelven como `None` a propósito: uPlot los pinta como cortes de
        línea, que es exactamente el requisito de "la caída del enlace se ve, no se
        interpola".
        """
        now = self._clock.now_utc()
        start = (now - timedelta(hours=hours)).replace(second=0, microsecond=0)
        end = now.replace(second=0, microsecond=0)
        start_iso = start.isoformat().replace("+00:00", "Z")
        rows = self._rows(
            """
            SELECT ts_utc_minute, balance, equity, margin_free, floating_pnl,
                   positions_count, source
              FROM equity_snapshots
             WHERE ts_utc_minute >= ?
             ORDER BY ts_utc_minute
            """,
            (start_iso,),
        )
        by_minute = {str(row["ts_utc_minute"]): row for row in rows}

        xs: list[float] = []
        ys: list[float | None] = []
        minute = start
        while minute <= end:
            iso = minute.isoformat().replace("+00:00", "Z")
            xs.append(minute.timestamp())
            row = by_minute.get(iso)
            ys.append(None if row is None else float(row["equity"]))
            minute += timedelta(minutes=1)
        filled = [y for y in ys if y is not None]
        return {
            "x": xs,
            "equity": ys,
            "first_ts_utc": rows[0]["ts_utc_minute"] if rows else None,
            "last_ts_utc": rows[-1]["ts_utc_minute"] if rows else None,
            "min_equity": min(filled) if filled else None,
            "max_equity": max(filled) if filled else None,
            "last_equity": filled[-1] if filled else None,
            # Minutos de la rejilla con dato: la diferencia con `len(xs)` son los huecos reales.
            "points": len(filled),
        }

    def trade_markers(self, hours: int = 24) -> list[dict[str, Any]]:
        """Entradas y salidas dentro de la ventana del gráfico de equity."""
        since = (self._clock.now_utc() - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        markers: list[dict[str, Any]] = []
        for row in self._rows(
            """
            SELECT open_time_utc, close_time_utc, side, volume, net_profit
              FROM trades
             WHERE open_time_utc >= ? OR (close_time_utc IS NOT NULL AND close_time_utc >= ?)
            """,
            (since, since),
        ):
            markers.append(
                {
                    "kind": "entry",
                    "ts_utc": row["open_time_utc"],
                    "side": row["side"],
                    "volume": row["volume"],
                }
            )
            if row["close_time_utc"]:
                markers.append(
                    {
                        "kind": "exit",
                        "ts_utc": row["close_time_utc"],
                        "side": row["side"],
                        "volume": row["volume"],
                        "net_profit": row["net_profit"],
                    }
                )
        for row in self._rows(
            "SELECT opened_at_utc, side, volume FROM positions WHERE opened_at_utc >= ?",
            (since,),
        ):
            markers.append(
                {"kind": "entry", "ts_utc": row["opened_at_utc"], "side": row["side"], "volume": row["volume"]}
            )
        markers.sort(key=lambda item: str(item["ts_utc"]))
        return markers

    # -- feed, eventos, log e historial --------------------------------------

    def events(self, limit: int = 60) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT id, ts_utc, source, type, level, message, correlation_id, payload_json
              FROM events
             ORDER BY id DESC
             LIMIT ?
            """,
            (int(limit),),
        )

    def last_event_ts(self, event_type: str) -> str | None:
        """Marca del último evento de ese tipo; base honesta del uptime del proceso."""
        rows = self._rows(
            "SELECT ts_utc FROM events WHERE type = ? ORDER BY id DESC LIMIT 1",
            (event_type,),
        )
        return str(rows[0]["ts_utc"]) if rows else None

    def log_lines(self, limit: int = 160) -> list[dict[str, Any]]:
        rows = self._rows(
            """
            SELECT id, ts_utc, level, logger, cycle_id, message, extra_json
              FROM log_lines
             ORDER BY id DESC
             LIMIT ?
            """,
            (int(limit),),
        )
        # El panel se pinta en orden cronológico ascendente, con la última al pie.
        rows.reverse()
        return rows

    def commands(self, limit: int = 20) -> list[dict[str, Any]]:
        """Últimos comandos con su estado: permite ver si la pausa ya fue procesada."""
        return self._rows(
            """
            SELECT command_id, idempotency_key, command, args_json, origin, status,
                   created_at_utc, claimed_at_utc, completed_at_utc, expires_at_utc, result_json
              FROM commands
             ORDER BY seq DESC
             LIMIT ?
            """,
            (int(limit),),
        )

    def last_history_sync(self) -> dict[str, Any] | None:
        rows = self._rows("SELECT key, last_sync_utc, last_deal_ts_utc FROM history_sync")
        if not rows:
            return None
        best = max(rows, key=lambda row: str(row.get("last_sync_utc") or ""))
        return best

    def order_attempts(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT id, ts_utc, decision_id, retcode, retcode_name, retryable,
                   request_json, result_json
              FROM order_attempts
             ORDER BY id DESC
             LIMIT ?
            """,
            (int(limit),),
        )

    def counts(self) -> dict[str, int]:
        """Filas por tabla: útil para el pie y para detectar una base recién creada."""
        tables = (
            "events",
            "decisions",
            "trades",
            "positions",
            "equity_snapshots",
            "log_lines",
            "order_attempts",
            "commands",
        )
        out: dict[str, int] = {}
        for table in tables:
            try:
                row = self._rows(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - nombres fijos
            except sqlite3.Error:
                continue
            out[table] = int(row[0]["n"]) if row else 0
        return out

    # -- antigüedad ------------------------------------------------------------

    def age_s(self, ts: Any, *, now: datetime | None = None) -> float | None:
        """Segundos desde esa marca; None si la fuente nunca escribió ese dato."""
        moment = _parse_iso(ts) if not isinstance(ts, datetime) else ts
        if moment is None:
            return None
        delta = ((now or self._clock.now_utc()) - moment).total_seconds()
        return max(0.0, delta)

    def stale_flag(
        self, ts: Any, *, now: datetime | None = None, after_s: int | None = None
    ) -> dict[str, Any]:
        """Triple `as_of` / `age_s` / `stale` que consume cada panel del frontend.

        `after_s` permite medir un bloque con el umbral de su propia cadencia: el de las
        escrituras por poll (5 s) no se puede juzgar con el mismo reloj que el de la decisión,
        que solo se escribe al cerrar la vela. Sin ese parámetro rige el umbral del dashboard.
        """
        limite = self._stale_after_s if after_s is None else after_s
        age = self.age_s(ts, now=now)
        if isinstance(ts, datetime):
            as_of = ts.astimezone(UTC).isoformat().replace("+00:00", "Z")
        elif isinstance(ts, str) and ts:
            as_of = ts
        else:
            as_of = None
        return {"as_of_utc": as_of, "age_s": age, "stale": age is None or age > limite}
