"""Escrituras del StateStore y logging JSONL + espejo en `log_lines`."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from _snapshots import regime, signal, snapshot

from goldbot.domain.filters import DailyLedger, InMemoryDailyLedger
from goldbot.domain.types import (
    CommandStatus,
    CommandType,
    DailyCounters,
    DecisionRecord,
    EventRecord,
    EventSource,
    EventType,
    ExitReason,
    ExitReasonSource,
    FilterCode,
    FilterOutcome,
    FilterResult,
    LogLevel,
    PositionState,
    Regime,
    RiskDecision,
    Side,
    TradeRecord,
)
from goldbot.infra.logging_setup import attach_state_store, configure_logging
from goldbot.infra.migrations import available_migrations
from goldbot.infra.state_store import SqliteDailyLedger, SqliteStateStore

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
LATEST_VERSION = available_migrations()[-1][0]
TRADING_DAY = date(2026, 7, 15)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now


def _store(tmp_path: Path) -> SqliteStateStore:
    store = SqliteStateStore(tmp_path / "state.db", FixedClock())
    assert store.initialize() == LATEST_VERSION
    return store


def _count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


def _rows(db_path: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(query, params))


def _decision(
    *,
    decision_id: str = "dec-1",
    with_signal: bool = True,
    with_filters: bool = True,
    with_risk: bool = True,
    rationale: str = "sin setup claro",
) -> DecisionRecord:
    filters = (
        FilterOutcome(
            results=(
                FilterResult(
                    code=FilterCode.SPREAD,
                    blocked=False,
                    observed=20.0,
                    threshold=45.0,
                    message="spread dentro de rango",
                ),
            ),
            blocked=False,
            primary_reason=None,
        )
        if with_filters
        else None
    )
    risk = (
        RiskDecision(
            approved=True,
            lots=0.01,
            risk_money=10.0,
            risk_pct_actual=1.0,
            sl_distance_price=4.0,
            sl_distance_points=400.0,
            reason="riesgo dentro del límite",
            spec_hash="spec-1",
            equity_at_decision=1_000.0,
        )
        if with_risk
        else None
    )
    return DecisionRecord(
        decision_id=decision_id,
        cycle_id="cycle-1",
        ts_utc=NOW,
        bar_time_utc=NOW - timedelta(minutes=5),
        action="ENTER" if with_signal else "NO_TRADE",
        snapshot=snapshot(),
        regime=regime(Regime.TREND_UP),
        playbook_id="trend_pullback",
        signal=signal() if with_signal else None,
        filters=filters,
        risk=risk,
        rationale=rationale,
        latency_ms=42,
        config_hash="cfg-test",
        model_versions={"rules": "rules-1"},
    )


def _position(*, ticket: int = 555, sl: float | None = 2_396.0, r_multiple: float = 0.5) -> PositionState:
    return PositionState(
        ticket=ticket,
        position_id=98_765,
        side=Side.BUY,
        volume=0.01,
        entry_price=2_400.0,
        current_price=2_402.0,
        sl=sl,
        tp=2_408.0,
        pnl_money=2.0,
        initial_risk_price=4.0,
        r_multiple=r_multiple,
        playbook_id="trend_pullback",
        regime_at_entry=Regime.TREND_UP,
        decision_id_entry="dec-1",
        opened_at_utc=NOW - timedelta(minutes=30),
    )


def _trade(*, position_id: int = 98_765, net_profit: float = 6.5) -> TradeRecord:
    return TradeRecord(
        position_id=position_id,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.01,
        entry_price=2_400.0,
        exit_price=2_406.5,
        sl=2_396.0,
        tp=2_408.0,
        open_time_utc=NOW - timedelta(minutes=30),
        close_time_utc=NOW,
        entry_deal_id=111,
        close_deal_id=222,
        commission=-0.2,
        swap=-0.05,
        net_profit=net_profit,
        r_multiple=1.6,
        duration_s=1_800,
        exit_reason=ExitReason.BOT,
        exit_reason_source=ExitReasonSource.BOT,
        playbook_id="trend_pullback",
        regime_at_entry=Regime.TREND_UP,
        decision_id_entry="dec-1",
        reconciled_at_utc=NOW,
    )


def test_bot_state_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_bot_state(
        {
            "state": "OPERATIVO",
            "paused": False,
            "mode": "DEMO",
            "requested_symbol": "XAUUSD",
            "resolved_symbol": "XAUUSD",
            "magic": 20261030,
            "account": {"login": 61623438, "equity": 1000.0},
            "context": {"feed": {"M5": "OK"}},
            "feed_status": "OK",
            "last_cycle_at_utc": NOW,
            "cycle_seq": 7,
            "last_heartbeat_utc": NOW,
            "kill_switch_latched": False,
            "config_hash": "deadbeef",
            "app_version": "0.1.0",
            "server_offset_min": 180,
        }
    )
    state = store.read_bot_state()
    assert state is not None
    assert state["cycle_seq"] == 7
    assert state["mode"] == "DEMO"
    assert state["resolved_symbol"] == "XAUUSD"
    assert state["server_offset_min"] == 180
    assert state["account_json"]["login"] == 61623438
    assert state["paused"] is False
    assert state["kill_switch_latched"] is False
    assert state["last_cycle_at_utc"] == "2026-07-15T12:00:00Z"

    store.write_bot_state({"mode": "DEMO", "state": "PAUSADO", "paused": True})
    assert _count(tmp_path / "state.db", "bot_state") == 1
    updated = store.read_bot_state()
    assert updated is not None
    assert updated["paused"] is True


def test_equity_snapshot_is_unique_per_minute(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for equity in (1000.0, 1003.5):
        store.write_equity_snapshot(
            account_login=61623438,
            ts_utc=NOW + timedelta(seconds=17),
            balance=1000.0,
            equity=equity,
            margin_free=900.0,
            floating_pnl=equity - 1000.0,
            positions_count=0,
            source="PERIODIC",
        )
    db = tmp_path / "state.db"
    assert _count(db, "equity_snapshots") == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT equity FROM equity_snapshots").fetchone()[0] == 1003.5


def test_events_log_lines_and_retention(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_event(
        EventRecord(
            ts_utc=NOW,
            source=EventSource.BOT,
            type=EventType.STARTUP,
            level=LogLevel.INFO,
            message="hola",
            payload={"symbol": "XAUUSD"},
        )
    )
    store.write_log_line(NOW, LogLevel.INFO, "test", "línea", extra={"k": 1})
    store.record_history_sync("candles:XAUUSD:M5", last_sync_utc=NOW, last_deal_ts_utc=None)

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        payload = conn.execute("SELECT payload_json FROM events").fetchone()[0]
        extra = conn.execute("SELECT extra_json FROM log_lines").fetchone()[0]
        sync = conn.execute("SELECT last_sync_utc FROM history_sync").fetchone()[0]
    assert json.loads(payload) == {"symbol": "XAUUSD"}
    assert json.loads(extra) == {"k": 1}
    assert sync == "2026-07-15T12:00:00Z"

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events (ts_utc, source, type, level, message) VALUES (?, 'BOT', 'ERROR', 'INFO', 'viejo')",
            ("2025-01-01T00:00:00Z",),
        )
        for index in range(150):
            conn.execute(
                "INSERT INTO log_lines (ts_utc, level, message) VALUES (?, 'INFO', ?)",
                (f"2026-07-15T12:00:{index % 60:02d}Z", f"l{index}"),
            )
    store.apply_retention(retention_days=90, log_lines_retention=100)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM log_lines").fetchone()[0] == 100


def test_jsonl_and_state_store_handler(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = logging.getLogger()
    previous = list(root.handlers)
    try:
        path = configure_logging(tmp_path / "logs", level="DEBUG", console=False)
        attach_state_store(store)
        logging.getLogger("goldbot.test").info(
            "mensaje estructurado", extra={"cycle_id": "c-1", "fields": {"symbol": "XAUUSD"}}
        )
        for handler in root.handlers:
            handler.flush()
        line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert line["level"] == "INFO"
        assert line["logger"] == "goldbot.test"
        assert line["cycle_id"] == "c-1"
        assert line["symbol"] == "XAUUSD"
        with sqlite3.connect(tmp_path / "state.db") as conn:
            row = conn.execute("SELECT level, cycle_id, message FROM log_lines").fetchone()
        assert row == ("INFO", "c-1", "mensaje estructurado")
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in previous:
            root.addHandler(handler)


# ---------------------------------------------------------------------------
# Decisiones, posiciones, operaciones e intentos de orden
# ---------------------------------------------------------------------------


def test_decision_round_trip_full(tmp_path: Path) -> None:
    """Ida y vuelta completa de una decisión con señal, filtros y riesgo."""
    store = _store(tmp_path)
    store.write_decision(_decision())
    db = tmp_path / "state.db"
    assert _count(db, "decisions") == 1
    rows = _rows(db, "SELECT * FROM decisions")
    row = rows[0]
    assert row["decision_id"] == "dec-1"
    assert row["cycle_id"] == "cycle-1"
    assert row["ts_utc"] == "2026-07-15T12:00:00Z"
    assert row["bar_time_utc"] == "2026-07-15T11:55:00Z"
    assert row["action"] == "ENTER"
    assert row["snapshot_hash"] == snapshot().snapshot_hash
    assert row["config_hash"] == "cfg-test"
    assert row["latency_ms"] == 42
    assert row["rationale"] == "sin setup claro"

    signal_payload = json.loads(row["signal_json"])
    assert signal_payload["signal_id"] == "trend_pullback-test"
    assert signal_payload["side"] == "BUY"
    assert signal_payload["created_at_utc"] == "2026-07-15T12:00:00+00:00"

    filters_payload = json.loads(row["filters_json"])
    assert filters_payload["blocked"] is False
    assert filters_payload["results"][0]["code"] == "SPREAD"

    risk_payload = json.loads(row["risk_json"])
    assert risk_payload["approved"] is True
    assert risk_payload["lots"] == 0.01

    regime_payload = json.loads(row["regime_json"])
    assert regime_payload["label"] == "TREND_UP"
    assert json.loads(row["probs_json"]) == {"TREND_UP": 0.8}

    # La huella en la tabla es la misma que la del JSONL de decisiones.
    assert json.loads(row["model_versions_json"]) == {"rules": "rules-1"}
    snapshot_payload = json.loads(row["snapshot_json"])
    assert snapshot_payload["decision_timeframe"] == "M5"
    assert snapshot_payload["snapshot_hash"] == row["snapshot_hash"]


def test_decision_without_signal_filters_or_risk(tmp_path: Path) -> None:
    """`signal=None`, `filters=None` y `risk=None` no deben romper la escritura."""
    store = _store(tmp_path)
    store.write_decision(
        _decision(
            decision_id="dec-abst",
            with_signal=False,
            with_filters=False,
            with_risk=False,
            rationale="sin setup claro",
        )
    )
    db = tmp_path / "state.db"
    rows = _rows(db, "SELECT signal_json, filters_json, risk_json, action FROM decisions")
    assert rows[0]["signal_json"] is None
    assert rows[0]["filters_json"] is None
    assert rows[0]["risk_json"] is None
    assert rows[0]["action"] == "NO_TRADE"


def test_write_decision_is_idempotent_by_decision_id(tmp_path: Path) -> None:
    """Reintentar un ciclo no duplica la fila ni pisa el rationale original."""
    store = _store(tmp_path)
    store.write_decision(_decision(rationale="motivo original"))
    store.write_decision(_decision(rationale="motivo nuevo tras reintento"))
    db = tmp_path / "state.db"
    assert _count(db, "decisions") == 1
    rows = _rows(db, "SELECT rationale FROM decisions")
    assert rows[0]["rationale"] == "motivo nuevo tras reintento"


def test_naive_datetime_is_rejected_in_decisions(tmp_path: Path) -> None:
    """Un `datetime` ingenuo debe fallar, igual que en el resto del StateStore."""
    store = _store(tmp_path)
    record = _decision()
    with pytest.raises(ValueError, match="zona horaria"):
        store.write_decision(replace(record, ts_utc=record.ts_utc.replace(tzinfo=None)))


def test_upsert_position_updates_instead_of_duplicating(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_position(_position())
    store.upsert_position(_position(sl=2_397.5, r_multiple=0.75))
    db = tmp_path / "state.db"
    assert _count(db, "positions") == 1
    rows = _rows(db, "SELECT sl, r_multiple, updated_at_utc FROM positions")
    assert rows[0]["sl"] == 2_397.5
    assert rows[0]["r_multiple"] == 0.75
    assert rows[0]["updated_at_utc"] == "2026-07-15T12:00:00Z"


def test_read_open_positions_conserva_las_banderas_de_gestion(tmp_path: Path) -> None:
    """El reinicio NO debe poder repetir un breakeven o un parcial ya ejecutados."""
    store = _store(tmp_path)
    store.upsert_position(replace(_position(), breakeven_done=True, partials_done=1))

    reopened = SqliteStateStore(tmp_path / "state.db", FixedClock())
    stored = reopened.read_open_positions()
    assert set(stored) == {555}
    position = stored[555]
    assert position.breakeven_done is True
    assert position.partials_done == 1
    assert position.entry_price == 2_400.0
    assert position.initial_risk_price == 4.0
    assert position.playbook_id == "trend_pullback"
    assert position.regime_at_entry is Regime.TREND_UP
    assert position.opened_at_utc == NOW - timedelta(minutes=30)


def test_read_open_positions_vacio_sin_posiciones(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.read_open_positions() == {}


def test_delete_position_removes_by_ticket(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_position(_position(ticket=555))
    store.upsert_position(_position(ticket=556))
    store.delete_position(555)
    db = tmp_path / "state.db"
    assert _count(db, "positions") == 1
    rows = _rows(db, "SELECT ticket FROM positions")
    assert rows[0]["ticket"] == 556


def test_write_trade_is_idempotent_by_position_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_trade(_trade(net_profit=6.5))
    store.write_trade(_trade(net_profit=6.5))
    db = tmp_path / "state.db"
    assert _count(db, "trades") == 1
    rows = _rows(db, "SELECT net_profit, exit_reason, exit_reason_source FROM trades")
    assert rows[0]["net_profit"] == 6.5
    assert rows[0]["exit_reason"] == "BOT"
    assert rows[0]["exit_reason_source"] == "BOT"


def test_record_order_attempt_persists_request_and_retcode(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_order_attempt(
        ts_utc=NOW,
        decision_id="dec-1",
        request={"symbol": "XAUUSD", "volume": 0.01},
        retcode=10009,
        retcode_name="TRADE_RETCODE_DONE",
        retryable=False,
        result={"deals": [{"ticket": 111}]},
    )
    rows = _rows(
        tmp_path / "state.db",
        "SELECT ts_utc, request_json, retcode, retryable, result_json FROM order_attempts",
    )
    assert rows[0]["ts_utc"] == "2026-07-15T12:00:00Z"
    assert json.loads(rows[0]["request_json"]) == {"symbol": "XAUUSD", "volume": 0.01}
    assert rows[0]["retcode"] == 10009
    assert rows[0]["retryable"] == 0
    assert json.loads(rows[0]["result_json"]) == {"deals": [{"ticket": 111}]}


# ---------------------------------------------------------------------------
# Comandos del dashboard
# ---------------------------------------------------------------------------


def test_command_full_lifecycle(tmp_path: Path) -> None:
    """Encolar, reclamar y completar un comando, con TZ conservada en la salida."""
    store = _store(tmp_path)
    command_id = store.enqueue_command(
        CommandType.CLOSE_ALL,
        {"symbol": "XAUUSD"},
        idempotency_key="k-1",
        origin="dashboard",
        ttl_s=30,
    )
    assert command_id

    claimed = store.claim_next_command(now_utc=NOW)
    assert claimed is not None
    assert claimed.command_id == command_id
    assert claimed.command is CommandType.CLOSE_ALL
    assert claimed.args == {"symbol": "XAUUSD"}
    assert claimed.origin == "dashboard"
    assert claimed.status is CommandStatus.CLAIMED
    assert claimed.created_at_utc == NOW
    assert claimed.expires_at_utc == NOW + timedelta(seconds=30)
    assert claimed.created_at_utc.tzinfo is not None
    assert claimed.expires_at_utc.tzinfo is not None

    # El mismo comando no se reclama dos veces.
    assert store.claim_next_command(now_utc=NOW) is None

    store.complete_command(command_id, status=CommandStatus.DONE, result={"closed": 1})
    rows = _rows(
        tmp_path / "state.db",
        "SELECT status, completed_at_utc, result_json FROM commands WHERE command_id = ?",
        (command_id,),
    )
    assert rows[0]["status"] == "DONE"
    assert rows[0]["completed_at_utc"] == "2026-07-15T12:00:00Z"
    assert json.loads(rows[0]["result_json"]) == {"closed": 1}


def test_command_idempotency_key_returns_existing_command_id(tmp_path: Path) -> None:
    """El doble clic en el dashboard no encola dos veces el mismo comando."""
    store = _store(tmp_path)
    first = store.enqueue_command(
        CommandType.PAUSE, {}, idempotency_key="doble-clic", origin="dashboard", ttl_s=30
    )
    second = store.enqueue_command(
        CommandType.PAUSE, {}, idempotency_key="doble-clic", origin="dashboard", ttl_s=30
    )
    assert second == first
    assert _count(tmp_path / "state.db", "commands") == 1
    # La clave distinta sí encola otro comando.
    third = store.enqueue_command(
        CommandType.PAUSE, {}, idempotency_key="otro", origin="dashboard", ttl_s=30
    )
    assert third != first
    assert _count(tmp_path / "state.db", "commands") == 2


def test_commands_are_claimed_in_fifo_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.enqueue_command(
        CommandType.PAUSE, {}, idempotency_key="a", origin="d", ttl_s=60
    )
    second = store.enqueue_command(
        CommandType.RESUME, {}, idempotency_key="b", origin="d", ttl_s=60
    )
    claimed_first = store.claim_next_command(now_utc=NOW)
    claimed_second = store.claim_next_command(now_utc=NOW)
    assert claimed_first is not None and claimed_first.command_id == first
    assert claimed_second is not None and claimed_second.command_id == second
    assert claimed_first.command is CommandType.PAUSE
    assert claimed_second.command is CommandType.RESUME


def test_command_expires_by_ttl(tmp_path: Path) -> None:
    """Un `PENDING` vencido no se reclama y pasa a `EXPIRED`."""
    store = _store(tmp_path)
    store.enqueue_command(
        CommandType.CLOSE_ALL, {}, idempotency_key="ttl", origin="d", ttl_s=10
    )
    # Antes de vencer se reclama sin problema.
    early = store.claim_next_command(now_utc=NOW + timedelta(seconds=5))
    assert early is not None
    store.complete_command(early.command_id, status=CommandStatus.DONE)

    store.enqueue_command(
        CommandType.PAUSE, {}, idempotency_key="ttl2", origin="d", ttl_s=10
    )
    # Pasado el TTL el comando ya no está disponible y queda EXPIRED.
    assert store.claim_next_command(now_utc=NOW + timedelta(seconds=11)) is None
    rows = _rows(
        tmp_path / "state.db",
        "SELECT status FROM commands WHERE idempotency_key = 'ttl2'",
    )
    assert rows[0]["status"] == "EXPIRED"


def test_claimed_command_expires_and_is_not_reclaimed(tmp_path: Path) -> None:
    """Un comando reclamado pero nunca completado NO se re-ejecuta: queda EXPIRED.

    Es la decisión de seguridad: un `CLOSE_ALL` o un reset del kill switch no debe
    ejecutarse dos veces aunque el bot muera a mitad de la ejecución.
    """
    store = _store(tmp_path)
    command_id = store.enqueue_command(
        CommandType.RESET_KILL_SWITCH, {}, idempotency_key="k-sw", origin="d", ttl_s=10
    )
    claimed = store.claim_next_command(now_utc=NOW)
    assert claimed is not None and claimed.command_id == command_id
    # El bot "muere" sin completar; cuando vuelve, el comando ya caducó.
    assert store.claim_next_command(now_utc=NOW + timedelta(seconds=11)) is None
    rows = _rows(
        tmp_path / "state.db",
        "SELECT status FROM commands WHERE command_id = ?",
        (command_id,),
    )
    assert rows[0]["status"] == "EXPIRED"


def test_complete_command_rejects_pending_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    command_id = store.enqueue_command(
        CommandType.PAUSE, {}, idempotency_key="bad", origin="d", ttl_s=30
    )
    with pytest.raises(ValueError, match="estado final"):
        store.complete_command(command_id, status=CommandStatus.PENDING)


def test_command_without_ttl_never_expires(tmp_path: Path) -> None:
    store = _store(tmp_path)
    command_id = store.enqueue_command(
        CommandType.RESUME, {}, idempotency_key="no-ttl", origin="d", ttl_s=0
    )
    claimed = store.claim_next_command(now_utc=NOW + timedelta(days=30))
    assert claimed is not None
    assert claimed.command_id == command_id
    assert claimed.expires_at_utc is None


# ---------------------------------------------------------------------------
# Ledger diario en SQLite
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> SqliteDailyLedger:
    """Ledger sobre la BD ya migrada: el bot siempre arranca con el esquema creado."""
    _store(tmp_path)
    return SqliteDailyLedger(tmp_path / "state.db", clock=FixedClock())


def test_sqlite_daily_ledger_survives_restart(tmp_path: Path) -> None:
    """El ledger persiste en la misma BD: una instancia nueva ve los contadores."""
    db_path = tmp_path / "state.db"
    first = _ledger(tmp_path)
    # Sin fila todavía, el filtro de pérdida diaria no puede calcular nada.
    assert first.counters(TRADING_DAY) == DailyCounters(trading_day=TRADING_DAY)
    first.ensure_day(TRADING_DAY, 1_000.0)
    first.record_entry(TRADING_DAY)
    first.record_entry(TRADING_DAY)
    first.record_pnl(TRADING_DAY, 25.0)
    first.record_pnl(TRADING_DAY, -10.0)
    first.mark_daily_lock(TRADING_DAY)
    first.record_equity_end(TRADING_DAY, 1_015.0)

    # "Reinicio": una instancia nueva sobre la misma ruta de BD.
    second = SqliteDailyLedger(db_path, clock=FixedClock())
    counters = second.counters(TRADING_DAY)
    assert counters.entries_used == 2
    assert counters.realized_pnl == 15.0
    assert counters.day_start_equity == 1_000.0
    assert second.is_daily_locked(TRADING_DAY) is True
    rows = _rows(db_path, "SELECT * FROM daily_stats WHERE trading_day = '2026-07-15'")
    assert len(rows) == 1
    assert rows[0]["wins"] == 1
    assert rows[0]["losses"] == 1
    assert rows[0]["gross_profit"] == 25.0
    assert rows[0]["gross_loss"] == 10.0
    assert rows[0]["equity_end"] == 1_015.0
    assert rows[0]["updated_at_utc"] == "2026-07-15T12:00:00Z"


def test_ensure_day_is_non_destructive(tmp_path: Path) -> None:
    """`ensure_day` no pisa el `equity_start` ni desengancha un bloqueo del día."""
    ledger = _ledger(tmp_path)
    ledger.ensure_day(TRADING_DAY, 1_000.0)
    ledger.record_entry(TRADING_DAY)
    ledger.record_pnl(TRADING_DAY, -30.0)
    ledger.mark_daily_lock(TRADING_DAY)
    # El rollover vuelve a llamar a `ensure_day` con el equity vigente: no debe borrar nada.
    ledger.ensure_day(TRADING_DAY, 950.0)
    ledger.ensure_day(TRADING_DAY, 900.0)
    assert ledger.counters(TRADING_DAY).day_start_equity == 1_000.0
    assert ledger.counters(TRADING_DAY).entries_used == 1
    assert ledger.counters(TRADING_DAY).realized_pnl == -30.0
    assert ledger.is_daily_locked(TRADING_DAY) is True


def test_daily_lock_defaults_to_false_for_unknown_day(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.is_daily_locked(date(2026, 1, 1)) is False
    ledger.record_entry(date(2026, 1, 1))
    assert ledger.is_daily_locked(date(2026, 1, 1)) is False


def test_ledger_drives_the_daily_loss_filter(tmp_path: Path) -> None:
    """`counters` alimenta el filtro de pérdida diaria igual que en backtest."""
    ledger = _ledger(tmp_path)
    ledger.ensure_day(TRADING_DAY, 1_000.0)
    ledger.record_pnl(TRADING_DAY, -25.0)
    counters = ledger.counters(TRADING_DAY)
    assert counters.day_start_equity == 1_000.0
    assert counters.realized_pnl == -25.0
    # Misma aritmética que `PreTradeFilterChain._daily_loss` con equity = 975.
    start = counters.day_start_equity or 0.0
    loss_pct = (start - 975.0) / start * 100.0
    assert loss_pct == pytest.approx(2.5)


def test_ledger_counters_match_in_memory_ledger(tmp_path: Path) -> None:
    """El ledger SQLite debe comportarse igual que el de referencia en memoria."""
    sqlite_ledger = _ledger(tmp_path)
    memory_ledger = InMemoryDailyLedger()
    for ledger in (sqlite_ledger, memory_ledger):
        ledger.ensure_day(TRADING_DAY, 1_000.0)
        ledger.record_entry(TRADING_DAY)
        ledger.record_pnl(TRADING_DAY, -25.0)
    sqlite_counters = sqlite_ledger.counters(TRADING_DAY)
    memory_counters = memory_ledger.counters(TRADING_DAY)
    assert sqlite_counters.entries_used == memory_counters.entries_used
    assert sqlite_counters.realized_pnl == pytest.approx(memory_counters.realized_pnl)
    assert sqlite_counters.day_start_equity == memory_counters.day_start_equity
    # Un día inexistente se comporta como en memoria: contadores a cero.
    assert sqlite_ledger.counters(date(2026, 1, 2)) == memory_ledger.counters(date(2026, 1, 2))


def test_sqlite_daily_ledger_satisfies_the_daily_ledger_protocol(tmp_path: Path) -> None:
    """`SqliteDailyLedger` puede inyectarse donde el dominio pide `DailyLedger`."""
    checked: DailyLedger = _ledger(tmp_path)
    assert checked.counters(TRADING_DAY) == DailyCounters(trading_day=TRADING_DAY)
