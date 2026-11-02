"""Caché local de velas cerradas en CSV gzip, reproducible y sin dependencias extra.

Se eligió CSV.gz en vez de parquet porque `pyarrow` no está instalado en el entorno.
"""

from __future__ import annotations

import csv
import gzip
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from goldbot.domain.types import Candle, Timeframe

_FIELDS = (
    "open_time_utc",
    "close_time_utc",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread_points",
    "is_closed",
    "received_at_utc",
    "source",
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def cache_path(cache_dir: str | Path, symbol: str, timeframe: Timeframe) -> Path:
    return Path(cache_dir) / symbol / f"{timeframe.value}.csv.gz"


def save_candles(path: str | Path, candles: Iterable[Candle]) -> int:
    """Escribe la serie completa de forma atómica y devuelve el número de velas."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    written = 0
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        writer.writeheader()
        for candle in candles:
            if not candle.is_closed:
                raise ValueError("la caché solo admite velas cerradas")
            writer.writerow(
                {
                    "open_time_utc": _iso(candle.open_time_utc),
                    "close_time_utc": _iso(candle.close_time_utc),
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "tick_volume": candle.tick_volume,
                    "spread_points": candle.spread_points,
                    "is_closed": 1,
                    "received_at_utc": _iso(candle.received_at_utc),
                    "source": candle.source,
                }
            )
            written += 1
    tmp.replace(target)
    return written


def load_candles(path: str | Path) -> tuple[Candle, ...]:
    """Lee la caché; lanza FileNotFoundError si aún no existe."""
    target = Path(path)
    candles: list[Candle] = []
    with gzip.open(target, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            candles.append(
                Candle(
                    open_time_utc=_parse_ts(row["open_time_utc"]),
                    close_time_utc=_parse_ts(row["close_time_utc"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=int(row["tick_volume"]),
                    spread_points=float(row["spread_points"]),
                    is_closed=row["is_closed"] == "1",
                    received_at_utc=_parse_ts(row["received_at_utc"]),
                    source=row["source"],
                )
            )
    return tuple(candles)


def last_closed(path: str | Path) -> Candle | None:
    """Última vela de la caché sin cargarla entera (fin de semana, reinicios)."""
    candles = load_candles(path)
    return candles[-1] if candles else None
