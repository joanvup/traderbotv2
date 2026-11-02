"""Reloj del bot: UTC canónico, conversión a hora del servidor y día operativo.

Pepperstone usa GMT+3 mientras Estados Unidos está en horario de verano y GMT+2 el
resto del año. Eso equivale a `America/New_York + 7 h` todo el año, así que el
servidor se modela con esa regla en vez de con una zona europea.

Nota: durante la hora del cambio de hora de EE. UU. (domingo, mercado cerrado) la
hora del servidor es ambigua; se resuelve con `fold=0`, sin impacto operativo.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from goldbot.domain.types import Session

SessionWindows = Mapping[Session, Sequence[tuple[dt_time, dt_time]]]

# Nueva York gana en el solape Londres/NY; Londres gana en el solape Asia/Londres.
_SESSION_PRIORITY: tuple[Session, ...] = (Session.NEW_YORK, Session.LONDON, Session.ASIA)


def _parse_window(window: str) -> tuple[dt_time, dt_time]:
    start_raw, separator, end_raw = window.partition("-")
    if not separator:
        raise ValueError(f"ventana horaria inválida: {window!r} (esperado HH:MM-HH:MM)")
    try:
        return dt_time.fromisoformat(start_raw), dt_time.fromisoformat(end_raw)
    except ValueError as exc:
        raise ValueError(f"ventana horaria inválida: {window!r}") from exc


def parse_session_windows(spec: Mapping[str, Sequence[str]]) -> dict[Session, tuple[tuple[dt_time, dt_time], ...]]:
    """Convierte `{"ASIA": ["03:00-11:00"], ...}` en ventanas tipadas."""
    parsed: dict[Session, tuple[tuple[dt_time, dt_time], ...]] = {}
    for name, windows in spec.items():
        try:
            session = Session[name.upper()]
        except KeyError as exc:
            raise ValueError(f"sesión desconocida: {name!r}") from exc
        parsed[session] = tuple(_parse_window(window) for window in windows)
    return parsed


def _in_window(moment: dt_time, start: dt_time, end: dt_time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


class UtcClock:
    """Reloj inyectable: convierte hora de servidor ↔ UTC y calcula el día operativo."""

    def __init__(
        self,
        server_zone: str = "America/New_York",
        *,
        server_shift: timedelta = timedelta(hours=7),
        server_offset: timedelta | None = None,
        session_windows: SessionWindows | None = None,
    ) -> None:
        self._server_zone = server_zone
        self._tz = ZoneInfo(server_zone)
        self._server_shift = server_shift
        self._server_offset = server_offset
        self._sessions = dict(session_windows or {})

    @property
    def server_zone(self) -> str:
        return self._server_zone

    @property
    def server_shift(self) -> timedelta:
        return self._server_shift

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def to_utc(self, server_dt: datetime, *, at_utc: datetime | None = None) -> datetime:
        """Convierte una marca naïve de MT5 (hora de servidor) a UTC."""
        if server_dt.tzinfo is not None:
            return server_dt.astimezone(UTC)
        if self._server_offset is not None:
            return (server_dt - self._server_offset).replace(tzinfo=UTC)
        wall = (server_dt - self._server_shift).replace(tzinfo=self._tz)
        return wall.astimezone(UTC)

    def to_server(self, ts_utc: datetime) -> datetime:
        """Devuelve la hora naïve del servidor (lo que espera la API de MT5)."""
        if ts_utc.tzinfo is None:
            raise ValueError("to_server exige un datetime con zona horaria")
        if self._server_offset is not None:
            return (ts_utc + self._server_offset).replace(tzinfo=None)
        wall = ts_utc.astimezone(self._tz).replace(tzinfo=None)
        return wall + self._server_shift

    def server_offset(self, at_utc: datetime) -> timedelta:
        """Offset vigente del servidor respecto a UTC (+2 h o +3 h)."""
        if self._server_offset is not None:
            return self._server_offset
        return self.to_server(at_utc) - at_utc.replace(tzinfo=None)

    def trading_day(self, ts_utc: datetime) -> date:
        """Día operativo según la hora del servidor (clave de `daily_stats`)."""
        return self.to_server(ts_utc).date()

    def session_at(self, ts_utc: datetime) -> Session:
        """Sesión Asia/Londres/NY según hora del servidor; CLOSED fuera de ventanas."""
        if ts_utc.tzinfo is None:
            raise ValueError("session_at exige un datetime con zona horaria")
        server_time = self.to_server(ts_utc).time()
        for session in _SESSION_PRIORITY:
            for start, end in self._sessions.get(session, ()):
                if _in_window(server_time, start, end):
                    return session
        return Session.CLOSED

    def sleep_until(self, ts_utc: datetime) -> None:
        remaining = (ts_utc - self.now_utc()).total_seconds()
        if remaining > 0:
            time.sleep(remaining)
