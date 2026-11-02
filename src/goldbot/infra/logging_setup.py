"""Logging estructurado: JSONL rotado en disco y espejo en la tabla `log_lines`."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from goldbot.domain.types import LogLevel

_DB_LEVELS = {
    logging.DEBUG: LogLevel.DEBUG,
    logging.INFO: LogLevel.INFO,
    logging.WARNING: LogLevel.WARNING,
    logging.ERROR: LogLevel.ERROR,
    logging.CRITICAL: LogLevel.ERROR,
}

_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def _iso_utc(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonlFormatter(logging.Formatter):
    """Una línea JSON por registro, con `fields`/`cycle_id` opcionales del llamador."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts_utc": _iso_utc(datetime.fromtimestamp(record.created, UTC)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cycle_id = getattr(record, "cycle_id", None)
        if cycle_id:
            payload["cycle_id"] = cycle_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class StateStoreHandler(logging.Handler):
    """Espeja los registros a `log_lines`; nunca propaga errores al logging."""

    def __init__(self, store: Any, *, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._store = store
        self._busy = False
        self._formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            ts = datetime.fromtimestamp(record.created, UTC)
            fields = getattr(record, "fields", None)
            extra: dict[str, Any] | None = None
            if isinstance(fields, dict) and fields:
                extra = fields
            if record.exc_info:
                extra = {**(extra or {}), "exception": self._formatter.formatException(record.exc_info)}
            self._store.write_log_line(
                ts,
                _DB_LEVELS.get(record.levelno, LogLevel.INFO),
                record.name,
                record.getMessage(),
                cycle_id=getattr(record, "cycle_id", None),
                extra=extra,
            )
        except Exception:  # noqa: BLE001 - el logging jamás debe romper el flujo del bot
            self.handleError(record)
        finally:
            self._busy = False


def configure_logging(
    log_dir: str | Path,
    *,
    level: str = "INFO",
    jsonl_filename: str = "bot.jsonl",
    console: bool = True,
) -> Path:
    """Configura el logger raíz y devuelve la ruta del archivo JSONL."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / jsonl_filename

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(level.upper())
    formatter = JsonlFormatter()

    file_handler = RotatingFileHandler(
        path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(stream)

    logging.captureWarnings(True)
    return path


def attach_state_store(store: Any, *, level: int = logging.INFO) -> StateStoreHandler:
    """Añade el espejo a SQLite; devuelve el handler para poder retirarlo en tests."""
    handler = StateStoreHandler(store, level=level)
    logging.getLogger().addHandler(handler)
    return handler


__all__ = [
    "JsonlFormatter",
    "StateStoreHandler",
    "attach_state_store",
    "configure_logging",
]
