"""Feed histórico de velas que produce la misma secuencia de eventos que el bot en vivo.

`slice_at` entrega, para cada timeframe, las últimas velas cerradas con
`close_time_utc <= cutoff`. Es la única puerta por la que el backtest ve datos:
si una vela aún no había cerrado en el instante de decisión, no existe para el
contexto (anti look-ahead).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import datetime

from goldbot.domain.types import Candle, Timeframe


def _closed_sorted(candles: Sequence[Candle]) -> tuple[Candle, ...]:
    unique: dict[datetime, Candle] = {}
    for candle in candles:
        if candle.is_closed:
            unique[candle.open_time_utc] = candle
    return tuple(unique[key] for key in sorted(unique))


class HistoricalFeed:
    """Velas cerradas indexadas por tiempo de cierre, listas para cortes causales."""

    def __init__(
        self,
        candles: Mapping[Timeframe, Sequence[Candle]],
        *,
        decision_timeframe: Timeframe = Timeframe.M5,
        limits: Mapping[Timeframe, int] | None = None,
    ) -> None:
        self._decision_timeframe = decision_timeframe
        self._series = {
            timeframe: _closed_sorted(series) for timeframe, series in candles.items()
        }
        if not self._series.get(decision_timeframe):
            raise ValueError(
                f"el feed necesita velas cerradas de {decision_timeframe.value}"
            )
        self._close_times = {
            timeframe: [candle.close_time_utc for candle in series]
            for timeframe, series in self._series.items()
        }
        self._limits = dict(limits or {})

    @property
    def decision_timeframe(self) -> Timeframe:
        return self._decision_timeframe

    @property
    def decision_bars(self) -> tuple[Candle, ...]:
        return self._series[self._decision_timeframe]

    def first_close_utc(self) -> datetime:
        return self._series[self._decision_timeframe][0].close_time_utc

    def last_close_utc(self) -> datetime:
        return self._series[self._decision_timeframe][-1].close_time_utc

    def slice_at(self, cutoff_utc: datetime) -> dict[Timeframe, tuple[Candle, ...]]:
        """Velas cerradas en o antes de `cutoff_utc`, recortadas al warmup configurado."""
        result: dict[Timeframe, tuple[Candle, ...]] = {}
        for timeframe, series in self._series.items():
            position = bisect_right(self._close_times[timeframe], cutoff_utc)
            limit = self._limits.get(timeframe, 0)
            window = series[:position]
            result[timeframe] = window[-limit:] if limit > 0 else window
        return result
