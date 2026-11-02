"""Implementación SQLite del StateStore compartido entre bot y dashboard."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from goldbot.domain.decision_log import decision_payload
from goldbot.domain.interfaces import Clock
from goldbot.domain.types import (
    CommandRecord,
    CommandStatus,
    CommandType,
    DailyCounters,
    DecisionRecord,
    EventRecord,
    LogLevel,
    PositionState,
    Regime,
    RiskDecision,
    Side,
    StoredPosition,
    TradeRecord,
)
from goldbot.infra.db.connection import connect, transaction
from goldbot.infra.migrations import apply_migrations

# Epoch de referencia para filas sin marca de apertura (nunca es un horario válido).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_BOT_STATE_COLUMNS = (
    "state",
    "paused",
    "mode",
    "requested_symbol",
    "resolved_symbol",
    "magic",
    "account_json",
    "health_json",
    "context_json",
    "regime_json",
    "playbook_json",
    "filters_json",
    "feed_status",
    "last_cycle_at_utc",
    "cycle_seq",
    "last_heartbeat_utc",
    "kill_switch_latched",
    "kill_reason",
    "config_hash",
    "app_version",
    "server_offset_min",
    "updated_at_utc",
)

_JSON_COLUMNS = {
    "account_json",
    "health_json",
    "context_json",
    "regime_json",
    "playbook_json",
    "filters_json",
}

# Columnas (sin la clave de conflicto) de las tablas escritas por el bot. Se declaran
# como constantes para que el INSERT ... ON CONFLICT DO UPDATE se genere siempre igual.
_DECISION_COLUMNS = (
    "cycle_id",
    "ts_utc",
    "bar_time_utc",
    "action",
    "snapshot_hash",
    "config_hash",
    "model_versions_json",
    "regime_json",
    "probs_json",
    "playbook_id",
    "signal_json",
    "filters_json",
    "risk_json",
    "rationale",
    "latency_ms",
    "snapshot_json",
)

_POSITION_COLUMNS = (
    "position_id",
    "side",
    "volume",
    "entry_price",
    "sl",
    "tp",
    "opened_at_utc",
    "playbook_id",
    "regime_at_entry",
    "decision_id_entry",
    "initial_risk_price",
    "r_multiple",
    "last_action",
    "updated_at_utc",
    "breakeven_done",
    "partials_done",
    "current_price",
    "pnl_money",
)

_TRADE_COLUMNS = (
    "symbol",
    "side",
    "volume",
    "entry_price",
    "exit_price",
    "sl",
    "tp",
    "open_time_utc",
    "close_time_utc",
    "entry_deal_id",
    "close_deal_id",
    "commission",
    "swap",
    "net_profit",
    "r_multiple",
    "duration_s",
    "exit_reason",
    "exit_reason_source",
    "playbook_id",
    "regime_at_entry",
    "decision_id_entry",
    "reconciled_at_utc",
)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("las marcas de tiempo deben incluir zona horaria")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso_utc(value: str) -> datetime:
    """Convierte una marca ISO-8601 con sufijo `Z` guardada en SQLite en `datetime` con zona."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("las marcas de tiempo deben incluir zona horaria")
    return parsed.astimezone(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _upsert_sql(table: str, key_column: str, columns: tuple[str, ...]) -> str:
    """`INSERT ... ON CONFLICT DO UPDATE` sobre columnas fijas del esquema (nunca dinámicas)."""
    names = ", ".join((key_column, *columns))
    placeholders = ", ".join(["?"] * (len(columns) + 1))
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns)
    return (
        f"INSERT INTO {table} ({names}) VALUES ({placeholders}) "  # noqa: S608 - columnas fijas
        f"ON CONFLICT({key_column}) DO UPDATE SET {updates}"
    )


def _risk_payload(risk: RiskDecision | None) -> dict[str, Any] | None:
    """Serializa la decisión de riesgo, que solo viaja en la tabla (no en el JSONL)."""
    if risk is None:
        return None
    return {
        "approved": risk.approved,
        "lots": risk.lots,
        "risk_money": risk.risk_money,
        "risk_pct_actual": risk.risk_pct_actual,
        "sl_distance_price": risk.sl_distance_price,
        "sl_distance_points": risk.sl_distance_points,
        "reason": risk.reason,
        "spec_hash": risk.spec_hash,
        "equity_at_decision": risk.equity_at_decision,
    }


def _command_from_row(row: sqlite3.Row, *, status: CommandStatus) -> CommandRecord:
    """Reconstruye el comando desde la fila de `commands` con las marcas ya en zona UTC."""
    args_json = row["args_json"]
    expires_raw = row["expires_at_utc"]
    return CommandRecord(
        command_id=str(row["command_id"]),
        command=CommandType(str(row["command"])),
        args=json.loads(args_json) if args_json else {},
        origin=str(row["origin"]),
        status=status,
        created_at_utc=_parse_iso_utc(str(row["created_at_utc"])),
        expires_at_utc=_parse_iso_utc(str(expires_raw)) if expires_raw else None,
    )


class SqliteStateStore:
    """Canal único de comunicación bot ↔ dashboard, con WAL y comandos idempotentes."""

    def __init__(self, db_path: str | Path, clock: Clock) -> None:
        self._db_path = Path(db_path)
        self._clock = clock

    def initialize(self) -> int:
        """Crea la base si falta, aplica migraciones y devuelve la versión de esquema."""
        conn = connect(self._db_path)
        try:
            return apply_migrations(conn)
        finally:
            conn.close()

    def _execute(self, sql: str, params: Mapping[str, Any] | tuple[Any, ...]) -> None:
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.execute(sql, params)
        finally:
            conn.close()

    # -- escrituras del bot -------------------------------------------------

    def write_bot_state(self, state: Mapping[str, Any]) -> None:
        now = self._clock.now_utc()
        mode = str(state.get("mode", "DEMO"))
        if mode not in {"DEMO", "LIVE"}:
            raise ValueError(f"mode inválido: {mode!r}")

        row: dict[str, Any] = {
            "state": str(state.get("state", "OPERATIVO")),
            "paused": 1 if state.get("paused") else 0,
            "mode": mode,
            "requested_symbol": str(state.get("requested_symbol", "")),
            "resolved_symbol": state.get("resolved_symbol"),
            "magic": int(state.get("magic", 0)),
            "account_json": _json(state["account"]) if state.get("account") is not None else None,
            "health_json": _json(state["health"]) if state.get("health") is not None else None,
            "context_json": _json(state["context"]) if state.get("context") is not None else None,
            "regime_json": _json(state["regime"]) if state.get("regime") is not None else None,
            "playbook_json": _json(state["playbook"]) if state.get("playbook") is not None else None,
            "filters_json": _json(state["filters"]) if state.get("filters") is not None else None,
            "feed_status": state.get("feed_status"),
            "last_cycle_at_utc": state.get("last_cycle_at_utc"),
            "cycle_seq": int(state.get("cycle_seq", 0)),
            "last_heartbeat_utc": state.get("last_heartbeat_utc"),
            "kill_switch_latched": 1 if state.get("kill_switch_latched") else 0,
            "kill_reason": state.get("kill_reason"),
            "config_hash": state.get("config_hash"),
            "app_version": state.get("app_version"),
            "server_offset_min": state.get("server_offset_min"),
            "updated_at_utc": _iso_utc(now),
        }
        for column in ("last_cycle_at_utc", "last_heartbeat_utc"):
            if isinstance(row[column], datetime):
                row[column] = _iso_utc(row[column])

        sql = _upsert_sql("bot_state", "id", _BOT_STATE_COLUMNS)
        params = (1, *(row[column] for column in _BOT_STATE_COLUMNS))
        self._execute(sql, params)

    def read_bot_state(self) -> Mapping[str, Any] | None:
        conn = connect(self._db_path, read_only=True)
        try:
            row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        result = dict(row)
        for column in _JSON_COLUMNS:
            if result.get(column):
                result[column] = json.loads(result[column])
        result["paused"] = bool(result.get("paused"))
        result["kill_switch_latched"] = bool(result.get("kill_switch_latched"))
        return result

    def write_equity_snapshot(
        self,
        *,
        account_login: int,
        ts_utc: datetime,
        balance: float,
        equity: float,
        margin_free: float,
        floating_pnl: float,
        positions_count: int,
        source: str,
    ) -> None:
        if source not in {"PERIODIC", "TRADE", "STARTUP"}:
            raise ValueError(f"source inválido: {source!r}")
        minute = ts_utc.astimezone(UTC).replace(second=0, microsecond=0)
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO equity_snapshots
                        (account_login, ts_utc, ts_utc_minute, balance, equity,
                         margin_free, floating_pnl, positions_count, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_login, ts_utc_minute) DO UPDATE SET
                        ts_utc=excluded.ts_utc,
                        balance=excluded.balance,
                        equity=excluded.equity,
                        margin_free=excluded.margin_free,
                        floating_pnl=excluded.floating_pnl,
                        positions_count=excluded.positions_count,
                        source=excluded.source
                    """,
                    (
                        account_login,
                        _iso_utc(ts_utc),
                        _iso_utc(minute),
                        balance,
                        equity,
                        margin_free,
                        floating_pnl,
                        positions_count,
                        source,
                    ),
                )
        finally:
            conn.close()

    def write_event(self, event: EventRecord) -> None:
        self._execute(
            """
            INSERT INTO events (ts_utc, source, type, level, message, correlation_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso_utc(event.ts_utc),
                event.source.value,
                event.type.value,
                event.level.value,
                event.message,
                event.correlation_id,
                _json(event.payload) if event.payload is not None else None,
            ),
        )

    def write_decision(self, record: DecisionRecord) -> None:
        """Guarda la decisión con la misma huella que la línea JSONL del DecisionLog.

        Es idempotente por `decision_id`: un ciclo interrumpido puede reintentarse sin
        duplicar filas ni cambiar el motivo registrado.
        """
        payload = decision_payload(record)
        regime = payload["regime"]
        probs = regime.get("probs") if isinstance(regime, Mapping) else None
        values: dict[str, Any] = {
            "cycle_id": record.cycle_id,
            "ts_utc": _iso_utc(record.ts_utc),
            "bar_time_utc": _iso_utc(record.bar_time_utc),
            "action": record.action,
            "snapshot_hash": record.snapshot.snapshot_hash,
            "config_hash": record.config_hash,
            "model_versions_json": _json(payload["model_versions"]),
            "regime_json": _json(regime),
            "probs_json": _json(dict(probs) if isinstance(probs, Mapping) else {}),
            "playbook_id": record.playbook_id,
            "signal_json": _json(payload["signal"]) if payload["signal"] is not None else None,
            "filters_json": _json(payload["filters"]) if payload["filters"] is not None else None,
            "risk_json": _json(_risk_payload(record.risk)) if record.risk is not None else None,
            "rationale": record.rationale,
            "latency_ms": record.latency_ms,
            "snapshot_json": _json(payload["snapshot"]),
        }
        ordered = (record.decision_id, *(values[column] for column in _DECISION_COLUMNS))
        self._execute(_upsert_sql("decisions", "decision_id", _DECISION_COLUMNS), ordered)

    def upsert_position(self, position: PositionState) -> None:
        """Inserta o actualiza la foto de una posición abierta, sin duplicar por `ticket`."""
        values = {
            "position_id": position.position_id,
            "side": position.side.value,
            "volume": position.volume,
            "entry_price": position.entry_price,
            "sl": position.sl,
            "tp": position.tp,
            "opened_at_utc": _iso_utc(position.opened_at_utc),
            "playbook_id": position.playbook_id,
            "regime_at_entry": position.regime_at_entry.value
            if position.regime_at_entry is not None
            else None,
            "decision_id_entry": position.decision_id_entry,
            "initial_risk_price": position.initial_risk_price,
            "r_multiple": position.r_multiple,
            "last_action": position.last_action,
            "updated_at_utc": _iso_utc(self._clock.now_utc()),
            "breakeven_done": 1 if position.breakeven_done else 0,
            "partials_done": int(position.partials_done),
            "current_price": position.current_price,
            "pnl_money": position.pnl_money,
        }
        params = (position.ticket, *(values[column] for column in _POSITION_COLUMNS))
        self._execute(_upsert_sql("positions", "ticket", _POSITION_COLUMNS), params)

    def read_open_positions(self) -> dict[int, StoredPosition]:
        """Fotos de las posiciones que el bot sabe abiertas, por ticket.

        Lo consume el `Bot` dos veces: para hidratar las banderas de gestión (MT5 no
        sabe si ya se hizo el breakeven o un parcial) y para cerrar el historial con
        datos de entrada correctos cuando la posición muere en el servidor.
        """
        conn = connect(self._db_path, read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT ticket, side, volume, entry_price, sl, tp, opened_at_utc,
                       playbook_id, regime_at_entry, decision_id_entry,
                       initial_risk_price, r_multiple, last_action,
                       breakeven_done, partials_done
                  FROM positions
                """
            ).fetchall()
        finally:
            conn.close()
        stored: dict[int, StoredPosition] = {}
        for row in rows:
            opened_raw = row["opened_at_utc"]
            stored[int(row["ticket"])] = StoredPosition(
                ticket=int(row["ticket"]),
                side=Side(row["side"]),
                volume=float(row["volume"]),
                entry_price=float(row["entry_price"]),
                sl=float(row["sl"]) if row["sl"] is not None else None,
                tp=float(row["tp"]) if row["tp"] is not None else None,
                opened_at_utc=(
                    datetime.fromisoformat(str(opened_raw)) if opened_raw else _EPOCH
                ),
                playbook_id=row["playbook_id"],
                regime_at_entry=(
                    Regime(str(row["regime_at_entry"])) if row["regime_at_entry"] else None
                ),
                decision_id_entry=row["decision_id_entry"],
                initial_risk_price=float(row["initial_risk_price"] or 0.0),
                r_multiple=float(row["r_multiple"] or 0.0),
                last_action=row["last_action"],
                breakeven_done=bool(row["breakeven_done"]),
                partials_done=int(row["partials_done"] or 0),
            )
        return stored

    def delete_position(self, ticket: int) -> None:
        """Retira la foto de una posición ya cerrada; no borra el historial de `trades`."""
        self._execute("DELETE FROM positions WHERE ticket = ?", (ticket,))

    def write_trade(self, trade: TradeRecord) -> None:
        """Historial conciliado: idempotente por `position_id` (clave primaria)."""
        values = {
            "symbol": trade.symbol,
            "side": trade.side.value,
            "volume": trade.volume,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "sl": trade.sl,
            "tp": trade.tp,
            "open_time_utc": _iso_utc(trade.open_time_utc),
            "close_time_utc": _iso_utc(trade.close_time_utc),
            "entry_deal_id": trade.entry_deal_id,
            "close_deal_id": trade.close_deal_id,
            "commission": trade.commission,
            "swap": trade.swap,
            "net_profit": trade.net_profit,
            "r_multiple": trade.r_multiple,
            "duration_s": trade.duration_s,
            "exit_reason": trade.exit_reason.value,
            "exit_reason_source": trade.exit_reason_source.value,
            "playbook_id": trade.playbook_id,
            "regime_at_entry": trade.regime_at_entry.value
            if trade.regime_at_entry is not None
            else None,
            "decision_id_entry": trade.decision_id_entry,
            "reconciled_at_utc": _iso_utc(trade.reconciled_at_utc)
            if trade.reconciled_at_utc is not None
            else None,
        }
        params = (trade.position_id, *(values[column] for column in _TRADE_COLUMNS))
        self._execute(_upsert_sql("trades", "position_id", _TRADE_COLUMNS), params)

    def write_log_line(
        self,
        ts_utc: datetime,
        level: LogLevel,
        logger_name: str,
        message: str,
        *,
        cycle_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO log_lines (ts_utc, level, logger, cycle_id, message, extra_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _iso_utc(ts_utc),
                level.value,
                logger_name,
                cycle_id,
                message,
                _json(extra) if extra else None,
            ),
        )

    # -- comandos del dashboard ---------------------------------------------

    def enqueue_command(
        self,
        command: CommandType,
        args: Mapping[str, Any],
        *,
        idempotency_key: str,
        origin: str,
        ttl_s: int,
    ) -> str:
        """Encola un comando del dashboard, uno solo por `idempotency_key`.

        Si la clave ya existe se devuelve el `command_id` previo sin insertar nada: así un
        doble clic en el panel no puede producir dos `CLOSE_ALL`. La lectura y la escritura
        van en la misma `BEGIN IMMEDIATE`, de modo que dos escritores no se ven una clave
        "libre" al mismo tiempo.
        """
        if ttl_s < 0:
            raise ValueError("el TTL de un comando no puede ser negativo")
        command_id = uuid4().hex
        created = self._clock.now_utc()
        # `ttl_s == 0` deja `expires_at_utc` nulo: el comando no caduca nunca.
        expires = created + timedelta(seconds=ttl_s) if ttl_s > 0 else None
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                existing = conn.execute(
                    "SELECT command_id FROM commands WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return str(existing["command_id"])
                conn.execute(
                    """
                    INSERT INTO commands
                        (command_id, idempotency_key, command, args_json, origin, status,
                         created_at_utc, expires_at_utc)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        command_id,
                        idempotency_key,
                        CommandType(command).value,
                        _json(dict(args)),
                        origin,
                        _iso_utc(created),
                        _iso_utc(expires) if expires is not None else None,
                    ),
                )
        finally:
            conn.close()
        return command_id

    def claim_next_command(self, *, now_utc: datetime) -> CommandRecord | None:
        """Reclama el comando `PENDING` más antiguo que siga vivo, o `None`.

        Todo en una única `BEGIN IMMEDIATE` corta: primero caduca (`EXPIRED`) lo que ya
        pasó de `expires_at_utc`, y luego se reclama el `PENDING` de `seq` menor.

        Un comando `CLAIMED` que caducó sin completarse pasa a `EXPIRED` y NO vuelve a
        ser elegible: reconsumirlo significaría re-ejecutar una orden de trading (cerrar
        posiciones dos veces, reactivar el kill switch…), así que se prefiere perder el
        comando antes que duplicar el efecto. La ventana de reclamación sin confirmar es
        el TTL, y el dashboard ve el `EXPIRED` y puede reencolar con otra clave.
        """
        now = _iso_utc(now_utc)
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.execute(
                    """
                    UPDATE commands
                       SET status = 'EXPIRED'
                     WHERE status IN ('PENDING', 'CLAIMED')
                       AND expires_at_utc IS NOT NULL
                       AND julianday(expires_at_utc) <= julianday(?)
                    """,
                    (now,),
                )
                row = conn.execute(
                    """
                    SELECT * FROM commands
                     WHERE status = 'PENDING'
                       AND (expires_at_utc IS NULL OR julianday(expires_at_utc) > julianday(?))
                     ORDER BY seq
                     LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                updated = conn.execute(
                    """
                    UPDATE commands
                       SET status = 'CLAIMED', claimed_at_utc = ?
                     WHERE seq = ? AND status = 'PENDING'
                    """,
                    (now, row["seq"]),
                )
                if updated.rowcount != 1:
                    return None
        finally:
            conn.close()
        return _command_from_row(row, status=CommandStatus.CLAIMED)

    def complete_command(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Cierra el comando en `DONE`/`FAILED`; los estados finales ya no se reescriben.

        Un comando `EXPIRED` sí puede cerrarse: si el bot llegó a ejecutarlo, `DONE` es
        más verdad que el acta de caducidad.
        """
        terminal = CommandStatus(status)
        if terminal not in {CommandStatus.DONE, CommandStatus.FAILED}:
            raise ValueError(f"estado final inválido: {terminal.value!r}")
        self._execute(
            """
            UPDATE commands
               SET status = ?, completed_at_utc = ?, result_json = ?
             WHERE command_id = ? AND status NOT IN ('DONE', 'FAILED')
            """,
            (
                terminal.value,
                _iso_utc(self._clock.now_utc()),
                _json(result) if result is not None else None,
                command_id,
            ),
        )

    # -- auditoría y mantenimiento ------------------------------------------

    def record_order_attempt(
        self,
        *,
        ts_utc: datetime,
        decision_id: str,
        request: Mapping[str, Any],
        retcode: int | None,
        retcode_name: str | None,
        retryable: bool | None,
        result: Mapping[str, Any] | None,
    ) -> None:
        """Audita cada intento de ejecución con su retcode, incluido el request enviado."""
        self._execute(
            """
            INSERT INTO order_attempts
                (ts_utc, decision_id, request_json, retcode, retcode_name, retryable, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso_utc(ts_utc),
                decision_id,
                _json(dict(request)),
                retcode,
                retcode_name,
                None if retryable is None else int(retryable),
                _json(result) if result is not None else None,
            ),
        )

    def record_history_sync(
        self,
        key: str,
        *,
        last_sync_utc: datetime,
        last_deal_ts_utc: datetime | None,
    ) -> None:
        self._execute(
            """
            INSERT INTO history_sync (key, last_sync_utc, last_deal_ts_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                last_sync_utc=excluded.last_sync_utc,
                last_deal_ts_utc=excluded.last_deal_ts_utc
            """,
            (
                key,
                _iso_utc(last_sync_utc),
                _iso_utc(last_deal_ts_utc) if last_deal_ts_utc is not None else None,
            ),
        )

    def apply_retention(self, *, retention_days: int, log_lines_retention: int) -> None:
        cutoff = _iso_utc(self._clock.now_utc() - timedelta(days=retention_days))
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.execute("DELETE FROM events WHERE ts_utc < ?", (cutoff,))
                conn.execute("DELETE FROM decisions WHERE ts_utc < ?", (cutoff,))
                conn.execute("DELETE FROM equity_snapshots WHERE ts_utc < ?", (cutoff,))
                conn.execute(
                    """
                    DELETE FROM log_lines WHERE id NOT IN (
                        SELECT id FROM log_lines ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (log_lines_retention,),
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Ledger diario persistente (sobrevive a un reinicio del bot)
# ---------------------------------------------------------------------------


class SqliteDailyLedger:
    """Ledger del día operativo leído/escrito en `daily_stats`.

    Se comporta como `InMemoryDailyLedger` pero persiste en la misma BD que el bot, así
    que los filtros (`MAX_TRADES`, `DAILY_LOSS`) ven los contadores reales del día sin
    necesidad de un estado en memoria que se pierda al reiniciar.
    """

    def __init__(self, db_path: str | Path, *, clock: Clock) -> None:
        self._db_path = Path(db_path)
        self._clock = clock

    # -- lectura -------------------------------------------------------------

    def counters(self, trading_day: date) -> DailyCounters:
        """Contadores del día; `day_start_equity` es `None` si la fila no existe."""
        conn = connect(self._db_path, read_only=True)
        try:
            row = conn.execute(
                """
                SELECT entries_used, realized_pnl, equity_start
                  FROM daily_stats WHERE trading_day = ?
                """,
                (trading_day.isoformat(),),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return DailyCounters(trading_day=trading_day)
        return DailyCounters(
            trading_day=trading_day,
            entries_used=int(row["entries_used"]),
            realized_pnl=float(row["realized_pnl"]),
            day_start_equity=(
                float(row["equity_start"]) if row["equity_start"] is not None else None
            ),
        )

    # -- escritura ----------------------------------------------------------

    def ensure_day(self, trading_day: date, equity: float) -> None:
        """Crea la fila del día con `equity_start` si no existe; nunca pisa la guardada.

        El `ON CONFLICT` no toca `equity_start` ni `daily_lock_latched`: llamarlo cada
        ciclo no borra el arranque del día ni desbloquea un día ya bloqueado.
        """
        self._execute(
            """
            INSERT INTO daily_stats (trading_day, tz, equity_start, updated_at_utc)
            VALUES (?, 'UTC', ?, ?)
            ON CONFLICT(trading_day) DO UPDATE SET
                tz = excluded.tz,
                updated_at_utc = excluded.updated_at_utc
            """,
            (trading_day.isoformat(), float(equity), _iso_utc(self._clock.now_utc())),
        )

    def record_entry(self, trading_day: date) -> None:
        """Suma una entrada del día (crea la fila si el rollover aún no la hizo)."""
        self._execute(
            """
            INSERT INTO daily_stats (trading_day, tz, entries_used, updated_at_utc)
            VALUES (?, 'UTC', 1, ?)
            ON CONFLICT(trading_day) DO UPDATE SET
                entries_used = entries_used + 1,
                updated_at_utc = excluded.updated_at_utc
            """,
            (trading_day.isoformat(), _iso_utc(self._clock.now_utc())),
        )

    def record_pnl(
        self,
        trading_day: date,
        pnl: float,
        *,
        r_multiple: float | None = None,
    ) -> None:
        """Acumula el PnL realizado y cuenta wins/losses por el signo de `pnl`.

        `r_multiple` se acepta por compatibilidad con el ledger del backtest; `daily_stats`
        no guarda esa columna.
        """
        amount = float(pnl)
        wins = 1 if amount > 0.0 else 0
        losses = 1 if amount < 0.0 else 0
        gross_profit = amount if amount > 0.0 else 0.0
        gross_loss = -amount if amount < 0.0 else 0.0
        self._execute(
            """
            INSERT INTO daily_stats (
                trading_day, tz, realized_pnl, wins, losses, gross_profit, gross_loss,
                updated_at_utc
            ) VALUES (?, 'UTC', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trading_day) DO UPDATE SET
                realized_pnl = realized_pnl + excluded.realized_pnl,
                wins = wins + excluded.wins,
                losses = losses + excluded.losses,
                gross_profit = gross_profit + excluded.gross_profit,
                gross_loss = gross_loss + excluded.gross_loss,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                trading_day.isoformat(),
                amount,
                wins,
                losses,
                gross_profit,
                gross_loss,
                _iso_utc(self._clock.now_utc()),
            ),
        )

    def mark_daily_lock(self, trading_day: date) -> None:
        """Engancha el bloqueo del día por pérdida máxima; sobrevive a un reinicio."""
        self._execute(
            """
            INSERT INTO daily_stats (trading_day, tz, daily_lock_latched, updated_at_utc)
            VALUES (?, 'UTC', 1, ?)
            ON CONFLICT(trading_day) DO UPDATE SET
                daily_lock_latched = 1,
                updated_at_utc = excluded.updated_at_utc
            """,
            (trading_day.isoformat(), _iso_utc(self._clock.now_utc())),
        )

    def is_daily_locked(self, trading_day: date) -> bool:
        """True si el día quedó bloqueado por pérdida máxima (aunque el bot reinicie)."""
        conn = connect(self._db_path, read_only=True)
        try:
            row = conn.execute(
                "SELECT daily_lock_latched FROM daily_stats WHERE trading_day = ?",
                (trading_day.isoformat(),),
            ).fetchone()
        finally:
            conn.close()
        return bool(row is not None and row["daily_lock_latched"])

    def record_equity_end(self, trading_day: date, equity: float) -> None:
        """Cierra el equity del día para el gráfico de 14 días del dashboard."""
        self._execute(
            """
            INSERT INTO daily_stats (trading_day, tz, equity_end, updated_at_utc)
            VALUES (?, 'UTC', ?, ?)
            ON CONFLICT(trading_day) DO UPDATE SET
                equity_end = excluded.equity_end,
                updated_at_utc = excluded.updated_at_utc
            """,
            (trading_day.isoformat(), float(equity), _iso_utc(self._clock.now_utc())),
        )

    # -- utilidad ------------------------------------------------------------

    def _execute(self, sql: str, params: tuple[Any, ...]) -> None:
        """Escritura corta con `BEGIN IMMEDIATE`, igual que el StateStore."""
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.execute(sql, params)
        finally:
            conn.close()
