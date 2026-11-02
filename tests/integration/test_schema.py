"""El esquema SQLite debe crearse completo, en WAL y de forma idempotente."""

from __future__ import annotations

from pathlib import Path

from goldbot.infra.db.connection import connect
from goldbot.infra.migrations import apply_migrations, available_migrations, current_version

# La versión esperada es la del último archivo de migración instalado, no un número fijo:
# añadir una migración nueva no debe romper este contrato.
LATEST_VERSION = available_migrations()[-1][0]

EXPECTED_TABLES = {
    "bot_state",
    "equity_snapshots",
    "events",
    "decisions",
    "positions",
    "trades",
    "commands",
    "daily_stats",
    "log_lines",
    "history_sync",
    "calendar_events",
    "order_attempts",
}


def _open(db_path: Path):
    return connect(db_path)


def test_schema_creates_all_tables_and_version(tmp_path: Path) -> None:
    conn = _open(tmp_path / "state.db")
    try:
        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == LATEST_VERSION
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert EXPECTED_TABLES <= tables
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(positions)")}
        assert {"breakeven_done", "partials_done"} <= columns
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_events_ts" in indexes
        assert "idx_trades_close_time" in indexes
        assert "idx_commands_status_seq" in indexes
    finally:
        conn.close()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    conn = _open(tmp_path / "state.db")
    try:
        assert apply_migrations(conn) == LATEST_VERSION
        assert apply_migrations(conn) == LATEST_VERSION
    finally:
        conn.close()


def test_database_uses_wal(tmp_path: Path) -> None:
    conn = _open(tmp_path / "state.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        conn.close()
