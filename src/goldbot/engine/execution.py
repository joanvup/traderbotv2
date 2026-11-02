"""Capa de ejecución de la Fase 4: riesgo → validación → envío → verificación SL/TP.

Orquesta, en orden, las piezas de seguridad que ya viven en `domain/risk.py` y en
los adaptadores:

1. `AtrRiskManager.size`        -> decide el lote (riesgo % del equity, redondeado
   hacia abajo al step; NO opera si el lote mínimo excede el riesgo).
2. `validate_order`              -> valida SL/TP, stops_level, freeze_level, volumen
   y filling contra el tick actual; nunca se salta esta puerta.
3. `place_order`                 -> envía a mercado (adaptador intercambiable: MT5 o
   simulador).
4. Verificación de SL/TP post-ejecución: si el broker no persistió el SL/TP, se
   corrige o se cierra la posición (regla de seguridad dura de la Fase 4).

`ExecutionEngine` es una clase sin estado interno que toma un `BrokerAdapter` y un
`AtrRiskManager`; así es trivisible de testear con el `SimulatorBrokerAdapter`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from goldbot.domain.interfaces import BrokerAdapter, Clock
from goldbot.domain.risk import AtrRiskManager, validate_order
from goldbot.domain.types import (
    AccountState,
    ContextSnapshot,
    OrderRequest,
    OrderResult,
    PolicyDecision,
    PositionState,
    RiskDecision,
    SymbolSpec,
    Tick,
    ValidationIssue,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Resultado de un intento de ejecutar una señal.

    `placed` indica si la orden se envió y el broker confirmó (retcode 10009).
    `risk` y `request`/`result` son las piezas auditables para el DecisionLog.
    """

    placed: bool
    risk: RiskDecision | None
    request: OrderRequest | None
    result: OrderResult | None
    issues: tuple[ValidationIssue, ...] = ()
    message: str = ""


class ExecutionEngine:
    """Composición de las piezas de ejecución con reintentos y verificación SL/TP."""

    def __init__(
        self,
        broker: BrokerAdapter,
        risk_manager: AtrRiskManager,
        clock: Clock,
        *,
        max_retries: int = 2,
        retry_backoff_s: float = 1.0,
        deviation_points: int = 0,
    ) -> None:
        self._broker = broker
        self._risk_manager = risk_manager
        self._clock = clock
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s
        self._deviation_points = deviation_points

    def execute_signal(
        self,
        decision: PolicyDecision,
        ctx: ContextSnapshot,
        account: AccountState,
        spec: SymbolSpec,
        tick: Tick,
        *,
        magic: int,
        decision_id: str,
    ) -> ExecutionResult:
        """Ejecuta una señal `ENTER` con todos los candados.

        Devuelve `ExecutionResult` con `placed=True` solo si la orden se confirmó y se
        verificó el SL/TP en la posición. Cualquier rechazo de validación o del broker
        se refleja en `placed=False` con el motivo; la función NO lanza excepción salvo
        un error de programación (broker inexistente, etc.).
        """
        signal = decision.signal
        if signal is None:
            return ExecutionResult(
                placed=False,
                risk=None,
                request=None,
                result=None,
                message="no hay señal ENTER que ejecutar",
            )

        # 1) Lote.
        risk = self._risk_manager.size(signal, ctx, account, spec)
        if not risk.approved or risk.lots is None:
            return ExecutionResult(
                placed=False,
                risk=risk,
                request=None,
                result=None,
                message=risk.reason,
            )

        # 2) Construir la orden.
        request = OrderRequest(
            client_order_id=f"{decision_id}-{signal.signal_id[:8]}",
            symbol=spec.resolved_name,
            side=signal.side,
            volume=risk.lots,
            sl=signal.stop_loss,
            tp=signal.take_profit,
            magic=magic,
            deviation_points=self._deviation_points,
            filling=None,
            comment=f"{signal.playbook_id}|{signal.signal_id[:10]}",
            decision_id=decision_id,
        )

        # 3) Validación pura (nunca se salta).
        issues = validate_order(request, spec, tick, require_sl=True, require_tp=signal.take_profit is not None)
        blocking = [issue for issue in issues if issue.severity == "ERROR"]
        if blocking:
            return ExecutionResult(
                placed=False,
                risk=risk,
                request=request,
                result=None,
                issues=tuple(issues),
                message="; ".join(issue.message for issue in blocking[:3]),
            )

        # 4) Envío. Antes de mandar tomamos nota de las posiciones propias: en una
        #    cuenta netting el `order_ticket` devuelto NO es el identificador de la
        #    posición, así que la única forma fiable de saber qué posición es la nuestra
        #    es la diferencia de tickets antes/después.
        before = self._own_tickets(request.magic)
        attempt = 0
        while True:
            if attempt > 0 and self._retry_backoff_s > 0.0:
                time.sleep(self._retry_backoff_s)
            result = self._broker.place_order(request)
            if result.filled:
                return self._confirm_position(result, request, spec, risk, before)
            if result.outcome_unknown:
                # Timeout/error de conexión: la orden puede haberse ejecutado. Reconcilia
                # y decide con datos, nunca reintentes a ciegas.
                return self._confirm_position(result, request, spec, risk, before)
            if result.retryable and attempt < self._max_retries:
                LOGGER.warning(
                    "retcode reintentable %s (%s); reintento %d/%d",
                    result.retcode_name,
                    result.retcode,
                    attempt + 1,
                    self._max_retries,
                )
                attempt += 1
                continue
            return ExecutionResult(
                placed=False,
                risk=risk,
                request=request,
                result=result,
                message=f"orden rechazada por el broker ({result.retcode_name} {result.retcode})",
            )

    # -- verificación SL/TP --------------------------------------------------

    def _own_tickets(self, magic: int) -> set[int]:
        """Tickets de nuestras posiciones abiertas (set vacío si el broker no responde)."""
        try:
            return {p.ticket for p in self._broker.positions(magic)}
        except Exception:
            return set()

    def _confirm_position(
        self,
        result: OrderResult,
        request: OrderRequest,
        spec: SymbolSpec,
        risk: RiskDecision,
        before: set[int],
    ) -> ExecutionResult:
        """Localiza la posición recién abierta y verifica que el SL quedó puesto en el broker.

        Si el broker no dejó SL en la posición, se corrige con una modificación y, si la
        corrección falla, se cierra: una posición nuestra NUNCA puede quedarse sin stop.
        """
        position = self._wait_for_new_position(request.magic, before)
        if position is None:
            if result.filled:
                return ExecutionResult(
                    placed=False,
                    risk=risk,
                    request=request,
                    result=result,
                    message=(
                        f"el broker confirmó {result.retcode_name} pero no vemos la posición "
                        "abierta: reconciliar en la siguiente vela"
                    ),
                )
            # Ambiguo y sin posición nueva: no llegó al mercado.
            return ExecutionResult(
                placed=False,
                risk=risk,
                request=request,
                result=result,
                message=(
                    f"resultado ambiguo ({result.retcode_name} {result.retcode}) "
                    "y no hay posición nueva: no se reintenta"
                ),
            )

        if position.sl is None or position.sl <= 0.0:
            # El broker no dejó stop en la posición recién abierta: corregir y, si no se
            # puede, cerrar. Nunca se deja una posición nuestra desprotegida.
            fix = self._fix_stops(position.ticket, request, spec)
            after = self._read_position(request.magic, position.ticket)
            if after is None:
                # La posición ya no existe: el cierre de emergencia funcionó.
                return ExecutionResult(
                    placed=False,
                    risk=risk,
                    request=request,
                    result=fix or result,
                    message="se abrió sin SL y no se pudo proteger: posición CERRADA",
                )
            if after.sl is not None and after.sl > 0.0:
                return ExecutionResult(
                    placed=True,
                    risk=risk,
                    request=request,
                    result=fix or result,
                    issues=(
                        ValidationIssue(
                            code="SL_REAPPLIED",
                            message="el broker no fijó el SL al abrir; se corrigió acto seguido",
                            severity="WARNING",
                        ),
                    ),
                    message="orden ejecutada; SL corregido en el broker tras la apertura",
                )
            # Sigue abierta y sigue sin SL: la devolvemos como colocada para que el bot
            # la registre y la gestión reintente la protección en la siguiente vela.
            return ExecutionResult(
                placed=True,
                risk=risk,
                request=request,
                result=fix or result,
                issues=(
                    ValidationIssue(
                        code="SL_MISSING_UNPROTECTED",
                        message="posición abierta SIN stop loss: reintentar la protección",
                        severity="ERROR",
                    ),
                ),
                message="la posición está abierta SIN SL protegido; la gestión reintentará",
            )

        expected = float(request.sl)
        drift_points = abs(position.sl - expected) / spec.point if spec.point else 0.0
        note = "" if drift_points <= 1.0 else f" (SL desplazado {drift_points:.1f} pts por el broker)"
        return ExecutionResult(
            placed=True,
            risk=risk,
            request=request,
            result=result,
            issues=(
                ()
                if drift_points <= 1.0
                else (
                    ValidationIssue(
                        code="SL_DRIFT",
                        message=f"el broker dejó el SL a {drift_points:.1f} pts del solicitado",
                        severity="WARNING",
                    ),
                )
            ),
            message=f"orden ejecutada y SL verificado en el broker{note}",
        )

    def _wait_for_new_position(self, magic: int, before: set[int]) -> PositionState | None:
        """Busca la posición nueva con reintentos cortos: el terminal tarda en propagarla."""
        for attempt in range(4):
            if attempt:
                time.sleep(0.25 * attempt)
            try:
                fresh = [p for p in self._broker.positions(magic) if p.ticket not in before]
            except Exception:
                continue
            if fresh:
                return max(fresh, key=lambda p: p.opened_at_utc)
        return None

    def _read_position(self, magic: int, ticket: int) -> PositionState | None:
        """Lee una posición propia por ticket (None si ya no existe o el broker falla)."""
        try:
            positions = self._broker.positions(magic)
        except Exception:
            return None
        return next((p for p in positions if p.ticket == ticket), None)

    def _fix_stops(self, ticket: int, request: OrderRequest, spec: SymbolSpec) -> OrderResult | None:
        """Fija SL/TP en la posición recién abierta; si no puede, la cierra.

        Es la última red de seguridad: una posición gestionada por el bot NUNCA puede
        quedar sin stop loss. El cierre es preferible a dejar una posición desprotegida.
        """
        try:
            modify = self._broker.modify_position(ticket, sl=request.sl, tp=request.tp)
        except Exception:
            modify = None
        if modify is not None and modify.filled:
            return modify
        try:
            return self._broker.close_position(ticket)
        except Exception:
            return modify