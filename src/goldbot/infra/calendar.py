"""Calendario económico: puente CSV del calendario nativo de MT5 y caché para backtest.

La API Python de MetaTrader5 no expone `mt5.calendar_*`; el calendario nativo se
exporta desde el terminal con el EA MQL5 `mt5/GoldbotCalendarExport.mq5` a
`<Terminal>\\Common\\Files\\goldbot\\calendar_events.csv`, y este módulo lo lee
validando la frescura del fichero y el `schema_version` del export. El mismo EA
publica `calendar_history.csv`, una ventana mucho más profunda y refresco lento
pensada para el backtest; `load_calendar_events_multi` une ambos por ocurrencia.

Para backtest se usa `StaticEconomicCalendar`, construido a partir del mismo CSV
(`load_calendar_events`). Si una consulta cae fuera de la cobertura exportada, la
ventana se marca `DEGRADED`: el filtro de noticias debe decidir con una política
explícita (`block`/`allow`) en lugar de asumir que no había noticias.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from goldbot.domain.types import CalendarBlock, CalendarEvent, CalendarHealth, CalendarWindow

BRIDGE_SUBFOLDER = "goldbot"
BRIDGE_FILENAME = "calendar_events.csv"
BRIDGE_HISTORY_FILENAME = "calendar_history.csv"
SCHEMA_VERSION = "1"

_IMPACT_RANK = {"none": 0, "low": 1, "medium": 2, "moderate": 2, "high": 3}


def _common_files_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "MetaQuotes" / "Terminal" / "Common" / "Files" / BRIDGE_SUBFOLDER


def default_bridge_path() -> Path:
    """Ruta por defecto del CSV en la carpeta común de todos los terminales MT5."""
    return _common_files_folder() / BRIDGE_FILENAME


def default_history_path() -> Path:
    """CSV profundo del mismo EA: ventana de años, refresco horario, solo para backtest."""
    return _common_files_folder() / BRIDGE_HISTORY_FILENAME


def _from_epoch(value: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _rank(value: str) -> int:
    return _IMPACT_RANK.get(value.strip().lower(), 0)


def _row_to_event(row: dict[str, str]) -> CalendarEvent | None:
    ts = _from_epoch(row.get("event_time_utc_epoch") or "")
    if ts is None:
        return None
    return CalendarEvent(
        event_id=(row.get("event_id") or "").strip(),
        ts_utc=ts,
        currency=(row.get("currency") or "").strip().upper(),
        impact=(row.get("impact_name") or "none").strip().lower(),
        title=(row.get("event_name") or "").strip(),
        event_code=(row.get("event_code") or "").strip(),
        value_id=(row.get("value_id") or "").strip(),
    )


def _read_bridge(target: Path) -> tuple[list[CalendarEvent], datetime | None]:
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        if reader.fieldnames is None or "event_time_utc_epoch" not in reader.fieldnames:
            raise ValueError(f"cabecera inválida en {target}")
        events: list[CalendarEvent] = []
        exported: datetime | None = None
        for row in reader:
            version = (row.get("schema_version") or "").strip()
            if version != SCHEMA_VERSION:
                raise ValueError(f"schema_version no soportado: {version!r}")
            event = _row_to_event(row)
            if event is None:
                continue
            if exported is None:
                exported = _from_epoch(row.get("export_gmt_epoch") or "")
            events.append(event)
    return events, exported


def load_calendar_events(path: str | Path) -> tuple[tuple[CalendarEvent, ...], datetime | None]:
    """Lee todos los eventos del CSV puente y el instante de exportación.

    Lanza `FileNotFoundError`, `ValueError` (cabecera/esquema) o `csv.Error`.
    """
    events, exported = _read_bridge(Path(path))
    return tuple(events), exported


def load_calendar_events_multi(
    paths: Iterable[str | Path],
) -> tuple[tuple[CalendarEvent, ...], datetime | None]:
    """Une varios CSV del puente (el en vivo y el histórico) por ocurrencia.

    `event_id` identifica el *tipo* de evento y se repite cada mes, así que la clave de
    deduplicación es el `value_id`. El primer fichero que trae una ocurrencia gana: se
    pasan en orden de frescura (en vivo primero) para conservar los valores más recientes.
    La marca de exportación devuelta es la más nueva de las presentes.
    """
    ordered = [Path(candidate) for candidate in paths]
    seen: dict[str, CalendarEvent] = {}
    exported: datetime | None = None
    found = False
    for target in ordered:
        if not target.exists():
            continue
        found = True
        events, file_exported = _read_bridge(target)
        for event in events:
            seen.setdefault(event.occurrence_key, event)
        if file_exported is not None and (exported is None or file_exported > exported):
            exported = file_exported
    if not found:
        raise FileNotFoundError(ordered[0] if ordered else "<sin ficheros>")
    return tuple(seen.values()), exported


def _filter_events(
    events: Iterable[CalendarEvent],
    start_utc: datetime,
    end_utc: datetime,
    currencies: Sequence[str],
    min_impact: str,
) -> tuple[CalendarEvent, ...]:
    allowed = {currency.strip().upper() for currency in currencies if currency.strip()}
    floor = _rank(min_impact)
    selected = [
        event
        for event in events
        if start_utc <= event.ts_utc <= end_utc
        and (not allowed or event.currency in allowed)
        and _rank(event.impact) >= floor
    ]
    return tuple(sorted(selected, key=lambda event: (event.ts_utc, event.event_id)))


class CsvBridgeEconomicCalendar:
    """Lee el CSV que exporta el EA MQL5 y rechaza datos más viejos que `max_age_s`."""

    def __init__(
        self,
        csv_path: str | Path | None = None,
        *,
        max_age_s: int = 180,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._csv_path = Path(csv_path) if csv_path is not None else default_bridge_path()
        self._max_age_s = max_age_s
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def csv_path(self) -> Path:
        return self._csv_path

    @property
    def max_age_s(self) -> int:
        return self._max_age_s

    def window(
        self,
        start_utc: datetime,
        end_utc: datetime,
        currencies: Sequence[str],
        min_impact: str,
    ) -> CalendarWindow:
        now = self._now()
        try:
            events, exported = load_calendar_events(self._csv_path)
        except FileNotFoundError:
            return CalendarWindow(events=(), health=CalendarHealth.UNAVAILABLE, fetched_at_utc=now)
        except (ValueError, csv.Error):
            return CalendarWindow(events=(), health=CalendarHealth.UNAVAILABLE, fetched_at_utc=now)

        age = (now - exported).total_seconds() if exported is not None else None
        health = CalendarHealth.OK
        if exported is None:
            health = CalendarHealth.DEGRADED
        elif age is not None and age > self._max_age_s:
            health = CalendarHealth.DEGRADED
        elif not events:
            health = CalendarHealth.DEGRADED
        return CalendarWindow(
            events=_filter_events(events, start_utc, end_utc, currencies, min_impact),
            health=health,
            fetched_at_utc=exported or now,
        )

    def is_blackout(
        self,
        ts_utc: datetime,
        before: timedelta,
        after: timedelta,
    ) -> CalendarBlock:
        window = self.window(ts_utc - before, ts_utc + after, (), "moderate")
        if window.health is not CalendarHealth.OK:
            return CalendarBlock(
                blocked=False,
                events=window.events,
                reason=f"calendario {window.health.value.lower()}",
            )
        return CalendarBlock(
            blocked=bool(window.events),
            events=window.events,
            reason="noticia de impacto en la ventana" if window.events else None,
        )


class StaticEconomicCalendar:
    """Calendario inmutable para backtest y tests, con cobertura declarada."""

    def __init__(
        self,
        events: Iterable[CalendarEvent],
        *,
        coverage_start_utc: datetime | None = None,
        coverage_end_utc: datetime | None = None,
        health: CalendarHealth = CalendarHealth.OK,
        fetched_at_utc: datetime | None = None,
    ) -> None:
        self._events = tuple(events)
        times = [event.ts_utc for event in self._events]
        self._coverage_start = coverage_start_utc or (min(times) if times else None)
        self._coverage_end = coverage_end_utc or (max(times) if times else None)
        self._health = health
        self._fetched_at = fetched_at_utc or datetime.now(UTC)

    @classmethod
    def from_csv(cls, path: str | Path | Iterable[str | Path]) -> StaticEconomicCalendar:
        """Calendario a partir de uno o varios CSV del puente (el histórico se une al en vivo)."""
        if isinstance(path, (str, Path)):
            events, exported = load_calendar_events(path)
        else:
            events, exported = load_calendar_events_multi(path)
        return cls(events, fetched_at_utc=exported)

    @property
    def coverage_start_utc(self) -> datetime | None:
        return self._coverage_start

    @property
    def coverage_end_utc(self) -> datetime | None:
        return self._coverage_end

    @property
    def events(self) -> tuple[CalendarEvent, ...]:
        return self._events

    def window(
        self,
        start_utc: datetime,
        end_utc: datetime,
        currencies: Sequence[str],
        min_impact: str,
    ) -> CalendarWindow:
        health = self._health
        if health is CalendarHealth.OK and (
            self._coverage_start is None
            or self._coverage_end is None
            or start_utc < self._coverage_start
            or end_utc > self._coverage_end
        ):
            health = CalendarHealth.DEGRADED
        return CalendarWindow(
            events=_filter_events(self._events, start_utc, end_utc, currencies, min_impact),
            health=health,
            fetched_at_utc=self._fetched_at,
        )

    def is_blackout(
        self,
        ts_utc: datetime,
        before: timedelta,
        after: timedelta,
    ) -> CalendarBlock:
        window = self.window(ts_utc - before, ts_utc + after, (), "moderate")
        if window.health is not CalendarHealth.OK:
            return CalendarBlock(
                blocked=False,
                events=window.events,
                reason=f"calendario {window.health.value.lower()}",
            )
        return CalendarBlock(
            blocked=bool(window.events),
            events=window.events,
            reason="noticia de impacto en la ventana" if window.events else None,
        )


_CACHE_FIELDS = ("value_id", "event_id", "ts_utc_epoch", "currency", "impact", "title", "event_code")

# Subconjunto del dialecto del puente: basta para que un CSV congelado se lea con
# `load_calendar_events` sin reescribir columnas que el backtest no usa.
_SNAPSHOT_HEADER = (
    "schema_version",
    "value_id",
    "event_id",
    "event_time_utc_epoch",
    "export_gmt_epoch",
    "currency",
    "impact_name",
    "event_name",
    "event_code",
)


def _save_cache(path: Path, events: Iterable[CalendarEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CACHE_FIELDS, delimiter=";")
        writer.writeheader()
        for event in sorted(events, key=lambda item: (item.ts_utc, item.event_id)):
            writer.writerow(
                {
                    "value_id": event.value_id,
                    "event_id": event.event_id,
                    "ts_utc_epoch": int(event.ts_utc.timestamp()),
                    "currency": event.currency,
                    "impact": event.impact,
                    "title": event.title,
                    "event_code": event.event_code,
                }
            )
    tmp.replace(path)


def _load_cache(path: Path) -> tuple[CalendarEvent, ...]:
    if not path.exists():
        return ()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        events: list[CalendarEvent] = []
        for row in reader:
            ts = _from_epoch(row.get("ts_utc_epoch") or "")
            if ts is None:
                continue
            events.append(
                CalendarEvent(
                    event_id=(row.get("event_id") or "").strip(),
                    ts_utc=ts,
                    currency=(row.get("currency") or "").strip().upper(),
                    impact=(row.get("impact") or "none").strip().lower(),
                    title=(row.get("title") or "").strip(),
                    event_code=(row.get("event_code") or "").strip(),
                    value_id=(row.get("value_id") or "").strip(),
                )
            )
    return tuple(events)


def write_calendar_snapshot(
    target: str | Path,
    sources: Iterable[str | Path],
) -> tuple[int, datetime | None, datetime | None]:
    """Congela la unión de los CSV del puente en un CSV del mismo dialecto.

    Se escribe con las cabeceras del puente para que `backtest --calendar` lo lea tal
    cual: un estudio walk-forward debe poder repetirse sobre el calendario exacto que
    vio, y el CSV del puente cambia cada minuto.
    """
    events, exported = load_calendar_events_multi(sources)
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SNAPSHOT_HEADER, delimiter=";")
        writer.writeheader()
        stamp = int((exported or datetime.now(UTC)).timestamp())
        for event in sorted(events, key=lambda item: (item.ts_utc, item.event_id)):
            writer.writerow(
                {
                    "schema_version": SCHEMA_VERSION,
                    "value_id": event.value_id,
                    "event_id": event.event_id,
                    "event_time_utc_epoch": int(event.ts_utc.timestamp()),
                    "export_gmt_epoch": stamp,
                    "currency": event.currency,
                    "impact_name": event.impact,
                    "event_name": event.title,
                    "event_code": event.event_code,
                }
            )
    tmp.replace(path)
    times = [event.ts_utc for event in events]
    return len(events), (min(times) if times else None), (max(times) if times else None)


class CachedEconomicCalendar:
    """Envuelve una fuente y persiste eventos para backtests reproducibles.

    Si la fuente falla, responde con la caché local marcada `DEGRADED`; el filtro
    aplica entonces su política de calendario no disponible.
    """

    def __init__(self, source: Any, cache_path: str | Path) -> None:
        self._source = source
        self._cache_path = Path(cache_path)

    def window(
        self,
        start_utc: datetime,
        end_utc: datetime,
        currencies: Sequence[str],
        min_impact: str,
    ) -> CalendarWindow:
        try:
            result = self._source.window(start_utc, end_utc, currencies, min_impact)
        except Exception:  # noqa: BLE001 - la caché existe precisamente para esto
            result = None
        if result is not None and result.health is CalendarHealth.OK:
            self._merge(result.events)
            return result

        cached = _load_cache(self._cache_path)
        return CalendarWindow(
            events=_filter_events(cached, start_utc, end_utc, currencies, min_impact),
            health=CalendarHealth.DEGRADED,
            fetched_at_utc=datetime.now(UTC),
        )

    def is_blackout(
        self,
        ts_utc: datetime,
        before: timedelta,
        after: timedelta,
    ) -> CalendarBlock:
        window = self.window(ts_utc - before, ts_utc + after, (), "moderate")
        return CalendarBlock(
            blocked=window.health is CalendarHealth.OK and bool(window.events),
            events=window.events,
            reason=None if window.health is CalendarHealth.OK else f"calendario {window.health.value.lower()}",
        )

    def _merge(self, events: Iterable[CalendarEvent]) -> None:
        merged = {event.occurrence_key: event for event in _load_cache(self._cache_path)}
        changed = False
        for event in events:
            if event.occurrence_key not in merged:
                merged[event.occurrence_key] = event
                changed = True
        if changed:
            _save_cache(self._cache_path, merged.values())
