"""Repositorios de lectura y escritura sobre SQLite (implementación en fases posteriores)."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class DashboardReader:
    """Conexión de solo lectura para el dashboard; nunca escribe estado del bot."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        raise NotImplementedError("lecturas del dashboard disponibles en Fase 5")
