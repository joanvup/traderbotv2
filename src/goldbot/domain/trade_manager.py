"""Gestión de la posición viva: breakeven, parciales, trailing, tiempo y régimen.

Todas las reglas se basan en ATR y estructura, y ninguna de ellas se basa en
números fijos (pips o dólares): el stop es dinámico y se recalcula cada vela.

Criterios aplicados en orden de prioridad (devueltos en ese orden):
  1. RÉGIMEN EN CONTRA (alta confianza): cierra la posición (regla de salida
     por tiempo y sesgo, no por pérdida).
  2. TIME STOP: la operación está dormida más allá de `time_stop_bars`.
  3. BREAKEVEN: el precio ha recorrido `breakeven_at_r` R y el SL no está puesto
     en breakeven todavía -> mueve el SL a breakeven + buffer.
  4. TRAILING: una vez armado el breakeven, arrastra el SL con ATR (y lo acerca
     a un swing si mejora el precio del trailing).
  5. PARCIALES: a `partial_at_r` R cierra un % del volumen si queda al menos el
     volumen mínimo.
  6. HOLD: no hay acción (acción por defecto cada vela).

Orden interno de las acciones devueltas: primero el movimiento de SL (breakeven
o trailing, nunca ambos a la vez), luego el parcial, y solo si ninguna de las
anteriores cierra la posición se devuelve HOLD. Si hay un CIERRE (régimen en
contra) es la ÚNICA acción devuelta, junto con HOLD si se especifica.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from goldbot.domain.types import (
    ContextSnapshot,
    ManageAction,
    ManageKind,
    ManagementSettings,
    PositionState,
    Regime,
    RegimeState,
    Side,
    Timeframe,
    Urgency,
)

_TIMEFRAME_MINUTES: dict[Timeframe, float] = {
    Timeframe.M5: 5.0,
    Timeframe.M15: 15.0,
    Timeframe.H1: 60.0,
    Timeframe.H4: 240.0,
    Timeframe.D1: 1440.0,
}


def _bars_since_open(ctx: ContextSnapshot, position: PositionState) -> float:
    """Barras (fracción) transcurridas desde la apertura, según el marco de decisión."""
    minutes = _TIMEFRAME_MINUTES.get(ctx.decision_timeframe, 60.0)
    return (ctx.decision_time_utc - position.opened_at_utc).total_seconds() / (minutes * 60.0)


def _r_multiple(position: PositionState) -> float:
    """R múltiple actual, recalcado desde los datos de la posición."""
    initial = position.initial_risk_price
    current = position.current_price
    entry = position.entry_price
    if initial <= 0.0:
        return position.r_multiple
    direction = 1.0 if position.side is Side.BUY else -1.0
    return (current - entry) * direction / initial


def _improves_sl(new_sl: float | None, old_sl: float | None, side: Side) -> bool:
    """Devuelve True si un nuevo SL mejora la protección (más cerca del precio)."""
    if new_sl is None or old_sl is None:
        return False
    if side is Side.BUY:
        return new_sl > old_sl
    return new_sl < old_sl


def _align_price(price: float, spec_point: float) -> float:
    """Redondea un precio al punto/tick_size más cercano (no cambia el diseño; solo evita
    órdenes que el broker rechazaría por redondeo)."""
    return round(price / spec_point) * spec_point


class AtrStructureTradeManager:
    """Aplica reglas de gestión basadas en ATR y estructura, registrando cada motivo.

    La gestión es estrictamente defensiva y conservadora: prioriza la protección del
    capital sobre la maximización de ganancias. Las reglas se aplican en orden de
    prioridad arriba descrito y cada decisión se registra con su motivo exacto.
    """

    def __init__(self, settings: ManagementSettings) -> None:
        self._settings = settings
        self._spec_point: float | None = None
        self._volume_step: float | None = None
        self._volume_min: float | None = None

    def set_symbol_limits(
        self, *, spec_point: float, volume_step: float, volume_min: float
    ) -> None:
        """La capa de ejecución inyecta la resolución del símbolo y los límites de volumen
        del broker; el TradeManager los necesita para redondear SL/TP y validar parciales."""
        self._spec_point = spec_point
        self._volume_step = volume_step
        self._volume_min = volume_min

    def on_bar(
        self,
        position: PositionState,
        ctx: ContextSnapshot,
        regime: RegimeState,
    ) -> Sequence[ManageAction]:
        """Decide la gestión de la posición al cierre de esta vela.

        Parámetros:
            position: estado actual de la posición (precio actual marcado al instante,
                SL/TP en vigor, volumen residual, acciones previas del día).
            ctx: snapshot del instante de decisión (ATR, sesión, marco de decisión).
            regime: régimen actual del mercado y su confianza.

        Se asume que la posición pertenece a este bot (verificable por el ticket o
        por el comentario de la orden en MT5); si la gestión devuelve CIERRE, la
        orden de salida la construye la capa de ejecución con el volumen residual.
        """
        side = position.side
        atr = max(0.0, float(ctx.timing.atr))
        if atr <= 0.0:
            # Sin ATR válido: solo HOLD, nunca muevas un SL sin medir el riesgo.
            return (ManageAction(
                kind=ManageKind.HOLD,
                reason="ATR no válido (<= 0); no se modifica el SL",
                decided_at_utc=ctx.decision_time_utc,
            ),)

        # 1) RÉGIMEN EN CONTRA (salida defensiva por cambio de dirección).
        #    Solo con la confianza configurada: un cambio de etiqueta con confianza
        #    baja es ruido de rango y cerrar ahí convierte volatilidad en pérdidas.
        if (
            regime.label == (Regime.TREND_DOWN if side is Side.BUY else Regime.TREND_UP)
            and regime.confidence >= self._settings.regime_exit_confidence
        ):
            return (
                ManageAction(
                    kind=ManageKind.CLOSE,
                    reason=(
                        f"el mercado está en {regime.label.value} "
                        f"(confianza {regime.confidence:.2f} >= "
                        f"{self._settings.regime_exit_confidence:.2f}, "
                        f"{regime.confirmed_bars} velas confirmadas)"
                        f" y la posición está abierta en la dirección contraria"
                    ),
                    decided_at_utc=ctx.decision_time_utc,
                ),
            )

        # 2) TIME STOP (operación dormida: el capital se puede liberar).
        #    Solo cierra si la operación NO ha progresado: si ya recorrió el tramo de
        #    breakeven, la salida la manda el SL arrastrado, no el reloj.
        bars_held = _bars_since_open(ctx, position)
        if (
            bars_held >= self._settings.time_stop_bars
            and _r_multiple(position) < self._settings.breakeven_at_r
        ):
            return (
                ManageAction(
                    kind=ManageKind.CLOSE,
                    reason=(
                        f"time stop: {bars_held:.0f} barras y solo {_r_multiple(position):.2f} R "
                        f"(< {self._settings.breakeven_at_r:.2f} R)"
                    ),
                    decided_at_utc=ctx.decision_time_utc,
                ),
            )

        current_sl = position.sl
        new_r = _r_multiple(position)
        r = self._settings

        if self._spec_point is None:
            raise RuntimeError(
                "el TradeManager no conoce el punto del símbolo; "
                "invoca set_symbol_limits() antes de usarlo"
            )
        spec_point = self._spec_point

        def in_profit() -> bool:
            return new_r >= 0.0

        # 3) BREAKEVEN.
        actions: list[ManageAction] = []
        new_sl: float | None = current_sl
        if in_profit() and not position.breakeven_done and new_r >= r.breakeven_at_r:
            buffer_price = 0.05 * atr
            be_sl = (
                position.entry_price + buffer_price
                if side is Side.BUY
                else position.entry_price - buffer_price
            )
            be_sl = _align_price(be_sl, spec_point)
            if new_sl is None or _improves_sl(be_sl, new_sl, side):
                actions.append(
                    ManageAction(
                        kind=ManageKind.MOVE_SL_BREAKEVEN,
                        reason=f"la operación ha recorrido {new_r:.2f} R; stop a breakeven + buffer",
                        new_sl=be_sl,
                        decided_at_utc=ctx.decision_time_utc,
                    )
                )
                new_sl = be_sl

        # 4) TRAILING por ATR, solo si esta vela NO ya movió el SL al breakeven: dos
        #    movimientos de SL en la misma barra significan dos órdenes sobre la misma
        #    posición, y la segunda puede fallar por freeze_level o desproteger la vela.
        trailing_armed = (not actions) and in_profit() and new_r >= r.breakeven_at_r
        if trailing_armed:
            trail = (
                position.current_price - r.trailing_atr_mult * atr
                if side is Side.BUY
                else position.current_price + r.trailing_atr_mult * atr
            )
            trail = _align_price(trail, spec_point)
            if new_sl is None or _improves_sl(trail, new_sl, side):
                actions.append(
                    ManageAction(
                        kind=ManageKind.TRAIL_SL,
                        reason=(
                            f"trailing activo (recorridos {new_r:.2f} R); stop arrastrado "
                            f"a {trail:.5f}"
                        ),
                        new_sl=trail,
                        urgency=Urgency.NORMAL,
                        decided_at_utc=ctx.decision_time_utc,
                    )
                )
                new_sl = trail

        # 5) PARCIALES (cierra un % si la operación está ganando y queda volumen mínimo).
        if in_profit() and position.partials_done == 0 and new_r >= r.partial_at_r:
            if self._volume_step is None or self._volume_min is None:
                raise RuntimeError(
                    "el TradeManager no conoce los límites de volumen del símbolo; "
                    "invoca set_symbol_limits() antes de usar el parcial"
                )
            step = self._volume_step
            volume_min = self._volume_min
            # Redondeo SIEMPRE hacia abajo: redondear al step más cercano cerraría más
            # volumen del pensado (y podría dejar la posición por debajo del mínimo).
            target = position.volume * r.partial_close_pct
            close_vol = round(math.floor(target / step + 1e-9) * step, 8)
            remaining = round(position.volume - close_vol, 8)
            if close_vol >= volume_min and remaining >= volume_min:
                actions.append(
                    ManageAction(
                        kind=ManageKind.PARTIAL_CLOSE,
                        reason=(
                            f"la operación ha recorrido {new_r:.2f} R; "
                            f"cierra {close_vol} lotes ({r.partial_close_pct:.0%}) a ganancias"
                        ),
                        close_volume=close_vol,
                        decided_at_utc=ctx.decision_time_utc,
                    )
                )

        # 6) Si no hubo ninguna acción: HOLD para auditar la vela.
        if not actions:
            actions.append(
                ManageAction(
                    kind=ManageKind.HOLD,
                    reason="sin acciones de gestión; la posición se mantiene",
                    decided_at_utc=ctx.decision_time_utc,
                )
            )
        return tuple(actions)
