"""Eventos que alimentan el bucle del bot; la misma secuencia se usa en vivo y en backtest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from goldbot.domain.types import Candle, CommandRecord, Tick, Timeframe


@dataclass(frozen=True, slots=True)
class BarEvent:
    """Una vela recién cerrada del timeframe indicado."""

    symbol: str
    timeframe: Timeframe
    candle: Candle
    received_at_utc: datetime


@dataclass(frozen=True, slots=True)
class TickEvent:
    """Tick de mercado, usado para gestión fina y detección de spread."""

    symbol: str
    tick: Tick
    received_at_utc: datetime


@dataclass(frozen=True, slots=True)
class TimerEvent:
    """Latido periódico para revisar salud, filtros y comandos."""

    now_utc: datetime


@dataclass(frozen=True, slots=True)
class CommandEvent:
    """Comando reclamado de la tabla `commands` (pausa, reanudar, cerrar todo)."""

    command: CommandRecord
    received_at_utc: datetime


MarketEvent = BarEvent | TickEvent | TimerEvent | CommandEvent
