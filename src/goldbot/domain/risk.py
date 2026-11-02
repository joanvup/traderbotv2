"""Cálculo de lote y validación pura de órdenes con las especificaciones reales del símbolo.

Reglas duros de la Fase 4 (no negociables):
- El lote se calcula a partir del riesgo en dinero (% del equity) y de la distancia
  real del stop loss en puntos/ticks. Nada se hardcodea.
- El volumen se redondea SIEMPRE hacia abajo al volumen_step para que el riesgo
  real no supere nunca el riesgo configurado.
- Si el riesgo al lote mínimo supera el permitido, NO se opera.
- SL/TP se validan contra las especificaciones reales del símbolo (stops_level,
  freeze_level, volumen, tick_size, filling permitida) antes de tocar el terminal.
"""

from __future__ import annotations

import math
from typing import NamedTuple

from goldbot.domain.types import (
    AccountState,
    ContextSnapshot,
    OrderRequest,
    RiskDecision,
    RiskSettings,
    Side,
    Signal,
    SymbolSpec,
    Tick,
    ValidationIssue,
    filling_mask_bit,
)


class _MoneyMath(NamedTuple):
    """Conversión entre dinero, distancia del stop y volumen, específica del símbolo."""

    price_per_lot_per_price_unit: float
    """USD que gana/pierde 1.0 lote por 1.0 unidad de precio (tick_value / tick_size)."""
    volume_step: float
    volume_min: float
    volume_max: float
    point: float
    tick_size: float
    stops_level_points: int
    freeze_level_points: int


def _money_math(spec: SymbolSpec) -> _MoneyMath | None:
    """Construye los parámetros de conversión del símbolo o rechaza si no son medibles."""
    if spec.tick_size <= 0.0 or spec.trade_tick_value_profit <= 0.0:
        # XAUUSD real tiene tick_size=0.01 y trade_tick_value=1.0 -> 100/1.0 por precio unit.
        # Si no hay tick_value registrado, no podemos calcular lotes con riesgo real.
        return None
    return _MoneyMath(
        price_per_lot_per_price_unit=spec.trade_tick_value_profit / spec.tick_size,
        volume_step=spec.volume_step,
        volume_min=spec.volume_min,
        volume_max=spec.volume_max,
        point=spec.point,
        tick_size=spec.tick_size,
        stops_level_points=spec.stops_level_points,
        freeze_level_points=spec.freeze_level_points,
    )


class AtrRiskManager:
    """Calcula el lote a partir del riesgo en dinero y la distancia real al stop.

    Ejemplo (XAUUSD, Pepperstone, 1% del equity = 10 USD, stop a 4.0 USD):
    - price_per_lot_per_price_unit = 100 (1.0 lote gana 100 USD por 1.0 punto de XAU)
    - risk_distance = 4.0 -> riesgo por lote = 400 USD
    - lots_raw = 10 / 400 = 0.025 -> redondeado hacia abajo a step 0.01 -> 0.02 lotes
    - riesgo real = 0.02 * 400 = 8.0 USD = 0.8% del equity (nunca más del 1%)
    """

    def __init__(self, settings: RiskSettings) -> None:
        self._settings = settings

    def size(
        self,
        signal: Signal,
        ctx: ContextSnapshot,
        account: AccountState,
        spec: SymbolSpec,
    ) -> RiskDecision:
        """Redondea el volumen hacia abajo al step; si el riesgo excede el máximo, rechaza.

        La distancia al stop es la del playbook (ATR/estructura), nunca en pips fijos.
        Se usa el equity de la cuenta (regla: riesgo % del equity, no del balance).
        El stop distance se valida en `validate_order` y aquí solo se convierte a puntos.
        """
        mm = _money_math(spec)
        if mm is None:
            return RiskDecision(
                approved=False,
                lots=None,
                risk_money=None,
                risk_pct_actual=None,
                sl_distance_price=0.0,
                sl_distance_points=0.0,
                reason="el símbolo no reporta tick_size/tick_value para calcular lotes",
                spec_hash=spec.spec_hash,
                equity_at_decision=account.equity,
            )

        risk_distance = abs(signal.entry_price - signal.stop_loss)
        if risk_distance <= 0.0:
            return RiskDecision(
                approved=False,
                lots=None,
                risk_money=None,
                risk_pct_actual=None,
                sl_distance_price=risk_distance,
                sl_distance_points=0.0,
                reason="distancia al stop inválida (<= 0); no hay riesgo definido para operar",
                spec_hash=spec.spec_hash,
                equity_at_decision=account.equity,
            )

        if account.equity <= 0.0:
            return RiskDecision(
                approved=False,
                lots=None,
                risk_money=None,
                risk_pct_actual=None,
                sl_distance_price=risk_distance,
                sl_distance_points=0.0,
                reason="equity no positivo; no se puede calcular el riesgo en dinero",
                spec_hash=spec.spec_hash,
                equity_at_decision=account.equity,
            )

        # 1) Cuánto gana/pierde 1.0 lote por 1.0 unidad de precio del símbolo.
        risk_per_lot = risk_distance * mm.price_per_lot_per_price_unit
        if risk_per_lot <= 0.0:
            return RiskDecision(
                approved=False,
                lots=None,
                risk_money=None,
                risk_pct_actual=None,
                sl_distance_price=risk_distance,
                sl_distance_points=0.0,
                reason="riesgo por lote calculado en 0; el símbolo no es negociable con este riesgo",
                spec_hash=spec.spec_hash,
                equity_at_decision=account.equity,
            )

        # 2) Volumen ideal a riesgo configurado: risk_pct% del equity sobre risk_distance.
        risk_money = account.equity * self._settings.risk_pct / 100.0
        lots_raw = risk_money / risk_per_lot

        # 3) Redondeo MANDATORIO hacia abajo al volumen_step. Consecuencia clave:
        #    el riesgo REAL nunca supera el riesgo CONFIGURADO.
        steps_down = math.floor(lots_raw / mm.volume_step + 1e-9)
        if steps_down < 1:
            lots = 0.0
        else:
            lots = round(steps_down * mm.volume_step, 8)

        if lots < mm.volume_min:
            # El lote mínimo del broker no cabe dentro del riesgo permitido -> NO operamos.
            # Esta es la salida más importante de la Fase 4: nunca operar a riesgo > máximo
            # por culpa del redondeo de lote.
            actual_risk = mm.volume_min * risk_per_lot
            pct_actual = actual_risk / account.equity * 100.0
            return RiskDecision(
                approved=False,
                lots=None,
                risk_money=None,
                risk_pct_actual=None,
                sl_distance_price=risk_distance,
                sl_distance_points=0.0,
                reason=(
                    f"el lote mínimo ({mm.volume_min} lotes) implica {pct_actual:.2f}% de riesgo, "
                    f"por encima del máximo ({self._settings.risk_pct}%): NO operamos"
                ),
                spec_hash=spec.spec_hash,
                equity_at_decision=account.equity,
            )

        if lots > mm.volume_max:
            # Muy raro (solo si el riesgo deseado es descomensurado); recortar para no
            # sobrepasar el tope del broker.
            lots = math.floor(mm.volume_max / mm.volume_step + 1e-9)
            lots = round(lots * mm.volume_step, 8)

        # 4) Riesgo real con el lote redondeado (siempre <= al configurado por el redondeo
        #    hacia abajo).
        actual_risk = lots * risk_per_lot
        risk_pct_actual = actual_risk / account.equity * 100.0

        return RiskDecision(
            approved=True,
            lots=lots,
            risk_money=risk_money,
            risk_pct_actual=risk_pct_actual,
            sl_distance_price=risk_distance,
            sl_distance_points=risk_distance / mm.point,
            reason=(
                f"{risk_pct_actual:.2f}% del equity (${actual_risk:.2f}); "
                f"lote {lots} después de redondear hacia abajo al step {mm.volume_step}"
            ),
            spec_hash=spec.spec_hash,
            equity_at_decision=account.equity,
        )


def validate_order(
    request: OrderRequest,
    spec: SymbolSpec,
    tick: Tick,
    *,
    require_sl: bool = True,
    require_tp: bool = False,
) -> list[ValidationIssue]:
    """Valida SL/TP, stops_level, freeze_level, volumen y filling antes de enviar a MT5.

    Esta es la puerta de seguridad entre el núcleo de decisión y el terminal: ninguna
    orden con problemas pasa de aquí. Los problemas se reportan como `ValidationIssue`
    con severidad "ERROR" (bloqueante) o "WARNING" (información para auditoría).

    Se comparan SL/TP contra las cotizaciones actuales (bid/ask) del tick de decisión,
    no contra el precio de entrada del playbook: el bot opera a mercado, así que el SL
    debe estar del lado correcto RELATIVO AL PRECIO DE EJECUCIÓN.

    Reglas:
    - volumen dentro de [volume_min, volume_max] y múltiplo exacto de volume_step;
    - SL presente y alineado al lado (BUY: bajo el bid; SELL: por encima del ask);
      TP solo si require_tp=True y alineado al lado opuesto;
    - distancia stop >= stops_level_points * point (el broker no permite stops dentro
      del stops_level; con stops_level=0 en Pepperstone la distancia la impone el bot);
    - no cruzar SL con TP (no puede haber SL a ambos lados de la misma orden);
    - el filling solicitado (si se fija) debe estar permitido por el bitmask del símbolo;
    - SL/TP redondeados a un múltiplo de tick_size (advertencia, no bloqueo);
    - SL != TP != 0.
    """
    issues: list[ValidationIssue] = []

    if request.symbol != spec.resolved_name and request.symbol != spec.requested_name:
        issues.append(
            ValidationIssue(
                code="INVALID_SYMBOL",
                message=(
                    f"símbolo {request.symbol!r} no coincide con el símbolo resuelto "
                    f"{spec.resolved_name!r} (pedido: {spec.requested_name!r})"
                ),
                severity="ERROR",
            )
        )

    if request.magic <= 0:
        issues.append(
            ValidationIssue(code="INVALID_MAGIC", message="magic inválido (debe ser > 0)", severity="ERROR")
        )

    # --- Volumen ------------------------------------------------------------------
    if request.volume <= 0.0:
        issues.append(
            ValidationIssue(
                code="INVALID_VOLUME",
                message="volumen debe ser positivo",
                severity="ERROR",
            )
        )
    else:
        if request.volume < spec.volume_min - 1e-12:
            issues.append(
                ValidationIssue(
                    code="VOLUME_TOO_SMALL",
                    message=f"volumen {request.volume} por debajo del mínimo de {spec.volume_min}",
                    severity="ERROR",
                )
            )
        if request.volume > spec.volume_max + 1e-12:
            issues.append(
                ValidationIssue(
                    code="VOLUME_TOO_LARGE",
                    message=f"volumen {request.volume} por encima del máximo de {spec.volume_max}",
                    severity="ERROR",
                )
            )
        diff = abs(round(request.volume / spec.volume_step) * spec.volume_step - request.volume)
        if diff > 1e-9:
            issues.append(
                ValidationIssue(
                    code="VOLUME_NOT_ALIGNED",
                    message=(
                        f"volumen {request.volume} no es múltiplo exacto del step {spec.volume_step}"
                    ),
                    severity="ERROR",
                )
            )

    # --- SL -----------------------------------------------------------------------
    # Operar sin stop está prohibido por la especificación, así que un SL ausente o
    # no positivo es SIEMPRE bloqueante, aunque quien llame pase `require_sl=False`
    # (ese flag solo controla el caso teórico de una gestión sin SL).
    if request.sl is None or float(request.sl) <= 0.0:
        issues.append(
            ValidationIssue(
                code="MISSING_SL" if request.sl is None else "INVALID_SL",
                message="SL obligatorio: el bot nunca envía una orden sin stop loss",
                severity="ERROR",
            )
        )

    # --- TP -----------------------------------------------------------------------
    if request.tp is None:
        if require_tp:
            issues.append(
                ValidationIssue(code="MISSING_TP", message="TP obligatorio", severity="ERROR")
            )
    elif request.tp <= 0.0:
        issues.append(
            ValidationIssue(code="INVALID_TP", message="TP debe ser positivo", severity="ERROR")
        )

    if request.sl is not None and request.tp is not None:
        if request.sl == request.tp:
            issues.append(
                ValidationIssue(
                    code="SL_EQUALS_TP", message="SL y TP no pueden ser iguales", severity="ERROR"
                )
            )

    # --- Alineación y distancias relativas al precio actual -----------------------
    ask = float(tick.ask)
    bid = float(tick.bid)

    side = request.side
    sl = float(request.sl) if request.sl is not None else None
    tp = float(request.tp) if request.tp is not None else None

    def dist(p1: float, p2: float) -> float:
        return abs(p1 - p2)

    if side is Side.BUY and sl is not None:
        if sl >= ask:
            issues.append(
                ValidationIssue(
                    code="SL_WRONG_SIDE",
                    message=f"SL {sl:.5f} no está por debajo del ask {ask:.5f} para una compra",
                    severity="ERROR",
                )
            )
        else:
            distance_points = (ask - sl) / spec.point
            if spec.stops_level_points > 0 and distance_points < spec.stops_level_points:
                issues.append(
                    ValidationIssue(
                        code="BELOW_STOPS_LEVEL",
                        message=(
                            f"SL {sl:.5f} está a {distance_points:.1f} pts del precio actual, "
                            f"por debajo del stops_level de {spec.stops_level_points} pts"
                        ),
                        severity="ERROR",
                    )
                )
            if spec.freeze_level_points > 0 and distance_points <= spec.freeze_level_points:
                issues.append(
                    ValidationIssue(
                        code="WITHIN_FREEZE_LEVEL",
                        message=(
                            f"SL {sl:.5f} está dentro del freeze_level ({distance_points:.1f} <= "
                            f"{spec.freeze_level_points} pts): el broker no permite moverlo"
                        ),
                        severity="WARNING",
                    )
                )
    elif side is Side.SELL and sl is not None:
        if sl <= bid:
            issues.append(
                ValidationIssue(
                    code="SL_WRONG_SIDE",
                    message=f"SL {sl:.5f} no está por encima del bid {bid:.5f} para una venta",
                    severity="ERROR",
                )
            )
        else:
            distance_points = (sl - bid) / spec.point
            if spec.stops_level_points > 0 and distance_points < spec.stops_level_points:
                issues.append(
                    ValidationIssue(
                        code="BELOW_STOPS_LEVEL",
                        message=(
                            f"SL {sl:.5f} está a {distance_points:.1f} pts del precio actual, "
                            f"por debajo del stops_level de {spec.stops_level_points} pts"
                        ),
                        severity="ERROR",
                    )
                )
            if spec.freeze_level_points > 0 and distance_points <= spec.freeze_level_points:
                issues.append(
                    ValidationIssue(
                        code="WITHIN_FREEZE_LEVEL",
                        message=(
                            f"SL {sl:.5f} está dentro del freeze_level ({distance_points:.1f} <= "
                            f"{spec.freeze_level_points} pts): el broker no permite moverlo"
                        ),
                        severity="WARNING",
                    )
                )

    if side is Side.BUY and tp is not None:
        if tp <= ask:
            issues.append(
                ValidationIssue(
                    code="TP_WRONG_SIDE",
                    message=f"TP {tp:.5f} no está por encima del ask {ask:.5f} para una compra",
                    severity="ERROR",
                )
            )
        elif sl is not None and tp <= sl:
            issues.append(
                ValidationIssue(
                    code="TP_BELOW_SL",
                    message=f"TP {tp:.5f} no puede estar por debajo del SL {sl:.5f}",
                    severity="ERROR",
                )
            )
    elif side is Side.SELL and tp is not None:
        if tp >= bid:
            issues.append(
                ValidationIssue(
                    code="TP_WRONG_SIDE",
                    message=f"TP {tp:.5f} no está por debajo del bid {bid:.5f} para una venta",
                    severity="ERROR",
                )
            )
        elif sl is not None and tp >= sl:
            issues.append(
                ValidationIssue(
                    code="TP_ABOVE_SL",
                    message=f"TP {tp:.5f} no puede estar por encima del SL {sl:.5f}",
                    severity="ERROR",
                )
            )

    # --- Filling ------------------------------------------------------------------
    # `request.filling` es el VALOR que se envía en type_filling (ORDER_FILLING_*:
    # 0 FOK / 1 IOC / 2 RETURN); `spec.allowed_fillings` es el BITMASK del símbolo.
    # Hay que traducir antes de intersectar: comparar directamente confunde espacios.
    filling = request.filling
    if filling is not None:
        bit = filling_mask_bit(filling)
        if bit == 0:
            issues.append(
                ValidationIssue(
                    code="FILLING_NOT_ALLOWED",
                    message=f"valor de filling {filling} desconocido para el símbolo",
                    severity="ERROR",
                )
            )
        elif spec.allowed_fillings & bit == 0:
            issues.append(
                ValidationIssue(
                    code="FILLING_NOT_ALLOWED",
                    message=(
                        f"filling {filling} (bit {bit}) no está en el bitmask permitido "
                        f"del símbolo ({spec.allowed_fillings})"
                    ),
                    severity="ERROR",
                )
            )

    # --- Redondeo de SL/TP a tick_size (advertencia) ------------------------------
    for label, price in (("SL", sl), ("TP", tp)):
        if price is None:
            continue
        # `price % tick_size` en coma flotante binaria nunca da 0 exacto (3872.38 % 0.01
        # = 2.9e-14), así que la comparación directa marcaría como desalineado cualquier
        # precio válido. Se mide la distancia a la rejilla, como con el volumen.
        grid_diff = abs(round(price / spec.tick_size) * spec.tick_size - price)
        if grid_diff > 1e-9:
            issues.append(
                ValidationIssue(
                    code=f"{label}_NOT_TICK_ALIGNED",
                    message=f"{label} {price:.5f} no es múltiplo de tick_size {spec.tick_size} "
                    f"(el broker podría redondear el valor que recibas)",
                    severity="WARNING",
                )
            )

    return issues
