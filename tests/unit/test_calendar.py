"""Calendario económico: CSV puente, frescura, cobertura y caché de respaldo."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goldbot.domain.types import CalendarEvent, CalendarHealth
from goldbot.infra.calendar import (
    CachedEconomicCalendar,
    CsvBridgeEconomicCalendar,
    StaticEconomicCalendar,
    load_calendar_events,
    load_calendar_events_multi,
    write_calendar_snapshot,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
_HEADER = (
    "schema_version",
    "value_id",
    "event_id",
    "event_time_utc_epoch",
    "export_gmt_epoch",
    "currency",
    "impact",
    "impact_name",
    "event_name",
    "event_code",
)


def _write_csv(path: Path, rows: list[dict[str, str]], *, export: datetime) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_HEADER, delimiter=";")
        writer.writeheader()
        for row in rows:
            payload = {field: "" for field in _HEADER}
            payload.update(
                {
                    "schema_version": "1",
                    "export_gmt_epoch": str(int(export.timestamp())),
                    "currency": "USD",
                    "impact": "2",
                    "impact_name": "moderate",
                    "event_code": "event",
                }
            )
            payload.update(row)
            writer.writerow(payload)
    return path


def _row(
    event_id: str,
    ts: datetime,
    *,
    currency: str = "USD",
    impact: str = "moderate",
    value_id: str | None = None,
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "value_id": value_id if value_id is not None else f"v{event_id}",
        "event_time_utc_epoch": str(int(ts.timestamp())),
        "currency": currency,
        "impact_name": impact,
        "event_name": f"Evento {event_id}",
    }


def test_load_calendar_events_parses_the_bridge_schema(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "calendar.csv",
        [_row("1", NOW, impact="high"), _row("2", NOW + timedelta(hours=1))],
        export=NOW,
    )
    events, exported = load_calendar_events(path)
    assert len(events) == 2
    assert events[0].ts_utc == NOW
    assert events[0].impact == "high"
    assert exported == NOW


def test_window_filters_by_time_currency_and_impact(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "calendar.csv",
        [
            _row("1", NOW, impact="high"),
            _row("2", NOW, currency="EUR", impact="high"),
            _row("3", NOW + timedelta(hours=1), impact="low"),
            _row("4", NOW + timedelta(days=5), impact="high"),
        ],
        export=NOW,
    )
    calendar = CsvBridgeEconomicCalendar(path, max_age_s=180, now=lambda: NOW + timedelta(seconds=30))
    window = calendar.window(NOW - timedelta(hours=1), NOW + timedelta(hours=2), ("USD",), "moderate")
    assert window.health is CalendarHealth.OK
    assert [event.event_id for event in window.events] == ["1"]


def test_stale_export_is_degraded(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "calendar.csv", [_row("1", NOW)], export=NOW)
    calendar = CsvBridgeEconomicCalendar(
        path, max_age_s=180, now=lambda: NOW + timedelta(minutes=10)
    )
    window = calendar.window(NOW, NOW + timedelta(hours=1), ("USD",), "none")
    assert window.health is CalendarHealth.DEGRADED


def test_missing_or_invalid_files_are_unavailable(tmp_path: Path) -> None:
    missing = CsvBridgeEconomicCalendar(tmp_path / "missing.csv", now=lambda: NOW)
    assert missing.window(NOW, NOW, ("USD",), "none").health is CalendarHealth.UNAVAILABLE

    invalid = tmp_path / "invalid.csv"
    invalid.write_text("foo;bar\n1;2\n", encoding="utf-8")
    broken = CsvBridgeEconomicCalendar(invalid, now=lambda: NOW)
    assert broken.window(NOW, NOW, ("USD",), "none").health is CalendarHealth.UNAVAILABLE


def test_empty_export_is_degraded(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "calendar.csv", [], export=NOW)
    calendar = CsvBridgeEconomicCalendar(path, now=lambda: NOW)
    window = calendar.window(NOW, NOW + timedelta(hours=1), ("USD",), "none")
    assert window.health is CalendarHealth.DEGRADED


def test_static_calendar_degrades_outside_its_coverage() -> None:
    event = CalendarEvent("1", NOW, "USD", "high", "FOMC")
    calendar = StaticEconomicCalendar(
        (event,),
        coverage_start_utc=NOW - timedelta(days=1),
        coverage_end_utc=NOW + timedelta(days=1),
    )
    inside = calendar.window(NOW - timedelta(hours=1), NOW + timedelta(hours=1), ("USD",), "none")
    outside = calendar.window(
        NOW - timedelta(days=5), NOW - timedelta(days=4), ("USD",), "none"
    )
    assert inside.health is CalendarHealth.OK
    assert len(inside.events) == 1
    assert outside.health is CalendarHealth.DEGRADED


def test_cached_calendar_serves_the_last_good_export(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.csv"
    source = StaticEconomicCalendar(
        (CalendarEvent("1", NOW, "USD", "high", "FOMC"),),
        coverage_start_utc=NOW - timedelta(days=1),
        coverage_end_utc=NOW + timedelta(days=1),
    )
    cached = CachedEconomicCalendar(source, cache_path)
    first = cached.window(NOW - timedelta(hours=1), NOW + timedelta(hours=1), ("USD",), "none")
    assert first.health is CalendarHealth.OK
    assert cache_path.exists()

    class _Broken:
        def window(self, *args, **kwargs):
            raise RuntimeError("sin terminal")

    fallback = CachedEconomicCalendar(_Broken(), cache_path)
    second = fallback.window(
        NOW - timedelta(hours=1), NOW + timedelta(hours=1), ("USD",), "none"
    )
    assert second.health is CalendarHealth.DEGRADED
    assert [event.event_id for event in second.events] == ["1"]


def test_el_mismo_evento_repetido_es_una_ocurrencia_distinta(tmp_path: Path) -> None:
    """`event_id` es el tipo (NFP se repite cada mes); lo que diferencia es el `value_id`."""
    live = _write_csv(
        tmp_path / "live.csv",
        [_row("700", NOW, value_id="v1"), _row("700", NOW - timedelta(days=30), value_id="v2")],
        export=NOW,
    )
    events, _ = load_calendar_events(live)
    assert len(events) == 2
    assert {event.occurrence_key for event in events} == {"v1", "v2"}


def test_union_en_vivo_mas_historico_sin_duplicar_y_con_el_reloj_mas_nuevo(tmp_path: Path) -> None:
    live = _write_csv(
        tmp_path / "live.csv",
        [_row("1", NOW, value_id="a"), _row("2", NOW - timedelta(days=10), value_id="b")],
        export=NOW,
    )
    history = _write_csv(
        tmp_path / "history.csv",
        [
            _row("2", NOW - timedelta(days=10), value_id="b"),
            _row("3", NOW - timedelta(days=400), impact="high", value_id="c"),
        ],
        export=NOW - timedelta(hours=5),
    )
    events, exported = load_calendar_events_multi([live, history])
    assert len(events) == 3
    assert exported == NOW
    coverage = StaticEconomicCalendar.from_csv([live, history])
    assert len(coverage.events) == 3
    assert coverage.coverage_start_utc == NOW - timedelta(days=400)
    assert coverage.coverage_end_utc == NOW


def test_sin_valor_id_la_clave_de_ocurrencia_usa_evento_y_hora(tmp_path: Path) -> None:
    sin_ids = tmp_path / "sin_ids.csv"
    with sin_ids.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_HEADER, delimiter=";")
        writer.writeheader()
        for ts in (NOW, NOW - timedelta(days=30)):
            writer.writerow(
                {
                    "schema_version": "1",
                    "value_id": "",
                    "event_id": "700",
                    "event_time_utc_epoch": str(int(ts.timestamp())),
                    "export_gmt_epoch": str(int(NOW.timestamp())),
                    "currency": "USD",
                    "impact_name": "high",
                    "event_name": "NFP",
                    "event_code": "nfp",
                }
            )
    events, _ = load_calendar_events(sin_ids)
    assert len(events) == 2
    assert len({event.occurrence_key for event in events}) == 2


def test_la_union_exige_al_menos_un_fichero(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_calendar_events_multi([tmp_path / "no.csv"])


def test_la_caches_de_respaldo_no_pierde_la_ocurrencia_nueva(tmp_path: Path) -> None:
    """Mismo `event_id` en dos meses: la caché de respaldo debe conservar las dos."""
    cache = tmp_path / "cache.csv"
    enero = CalendarEvent("700", NOW - timedelta(days=60), "USD", "high", "NFP", value_id="v-ene")
    febrero = CalendarEvent("700", NOW, "USD", "high", "NFP", value_id="v-feb")
    for event in (enero, febrero):
        fuente = StaticEconomicCalendar(
            (event,),
            coverage_start_utc=NOW - timedelta(days=365),
            coverage_end_utc=NOW + timedelta(days=1),
        )
        CachedEconomicCalendar(fuente, cache).window(event.ts_utc, event.ts_utc, ("USD",), "none")

    class _Broken:
        def window(self, *args, **kwargs):
            raise RuntimeError("sin terminal")

    respaldo = CachedEconomicCalendar(_Broken(), cache).window(
        NOW - timedelta(days=61), NOW + timedelta(hours=1), ("USD",), "none"
    )
    assert respaldo.health is CalendarHealth.DEGRADED
    assert len(respaldo.events) == 2
    assert {event.value_id for event in respaldo.events} == {"v-ene", "v-feb"}


def test_write_calendar_snapshot_se_lee_con_el_cargador_del_puente(tmp_path: Path) -> None:
    live = _write_csv(tmp_path / "live.csv", [_row("1", NOW, value_id="a")], export=NOW)
    history = _write_csv(
        tmp_path / "history.csv",
        [_row("9", NOW - timedelta(days=500), impact="high", value_id="z")],
        export=NOW - timedelta(days=1),
    )
    target = tmp_path / "snapshot.csv"
    total, start, end = write_calendar_snapshot(target, [live, history])
    assert (total, start, end) == (
        2,
        NOW - timedelta(days=500),
        NOW,
    )
    recargados, exported = load_calendar_events(target)
    assert len(recargados) == 2
    assert exported == NOW
    calendario = StaticEconomicCalendar.from_csv(target)
    assert calendario.coverage_start_utc == NOW - timedelta(days=500)
    inside = calendario.window(
        NOW - timedelta(days=500), NOW - timedelta(days=499), ("USD",), "moderate"
    )
    assert inside.health is CalendarHealth.OK
    assert len(inside.events) == 1
