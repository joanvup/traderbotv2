"""Migraciones incrementales basadas en PRAGMA user_version y archivos SQL numerados."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

SQL_DIR = Path(__file__).parent / "sql"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def available_migrations(sql_dir: Path = SQL_DIR) -> list[tuple[int, Path]]:
    """Lista (versión, archivo) ordenados; ignora archivos que no cumplen el patrón."""
    migrations: list[tuple[int, Path]] = []
    for path in sql_dir.glob("*.sql"):
        match = _MIGRATION_NAME.match(path.name)
        if match:
            migrations.append((int(match.group(1)), path))
    return sorted(migrations, key=lambda item: item[0])


def apply_migrations(conn: sqlite3.Connection, sql_dir: Path = SQL_DIR) -> int:
    """Aplica todas las migraciones pendientes y devuelve la versión final."""
    installed = current_version(conn)
    for version, path in available_migrations(sql_dir):
        if version <= installed:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {int(version)}")
        installed = version
    return installed
