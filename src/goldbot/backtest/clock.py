"""Reloj simulado para el backtest: avanza solo con los datos del feed.

Hereda la conversión servidor ↔ UTC del reloj real para que los filtros de sesión
y el día operativo se comporten exactamente igual que en la cuenta demo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from datetime import time as dt_time

from goldbot.domain.types import Session
from goldbot.infra.time import UtcClock

SessionWindows = Mapping[Session, Sequence[tuple[dt_time, dt_time]]]


class SimulatedClock(UtcClock):
    """Reloj determinista; nunca lee la hora real durante el backtest."""

    def __init__(
        self,
        start_utc: datetime,
        *,
        server_zone: str = "America/New_York",
        server_shift: timedelta = timedelta(hours=7),
        server_offset: timedelta | None = None,
        session_windows: SessionWindows | None = None,
    ) -> None:
        if start_utc.tzinfo is None:
            raise ValueError("start_utc debe incluir zona horaria")
        super().__init__(
            server_zone,
            server_shift=server_shift,
            server_offset=server_offset,
            session_windows=session_windows,
        )
        self._now = start_utc

    def now_utc(self) -> datetime:
        return self._now

    def advance_to(self, ts_utc: datetime) -> None:
        if ts_utc.tzinfo is None:
            raise ValueError("ts_utc debe incluir zona horaria")
        if ts_utc < self._now:
            raise ValueError("el reloj simulado no puede retroceder")
        self._now = ts_utc

    def sleep_until(self, ts_utc: datetime) -> None:
        """En simulación no se duerme: el avance lo controla el motor."""
