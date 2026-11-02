"""Kill switch latcheado: una vez disparado exige reset manual registrado.

Comportamiento duros de la Fase 4:
- Es un latch: una vez que `latched=True`, las condiciones de disparo siguientes ya no
  lo vuelven a "disparar"; solo un reset manual explícito (desde el dashboard con token)
  lo libera. El reset se registra como evento `KILL_RESET` con su origen.
- El orden de prioridad de las condiciones es fijo:
    1. CUENTA NO DEMO (imposible, es el candado más alto)
    2. TERMINAL DESCONECTADO (el bot no puede ni leer ni ejecutar), tras
       `disconnect_tolerance_polls` lecturas seguidas sin enlace
    3. TRADING ALGORÍTMICO DESHABILITADO EN EL TERMINAL (retcode 10027)
    4. EQUITY FLOOR (el capital está por debajo del mínimo de supervivencia)
    5. MARGEN LIBRE CRÍTICO (por debajo de `safety.min_free_margin_pct` del equity)
    6. ERRORES REPETIDOS DE EJECUCIÓN (contador de fallos consecutivos en la capa de
       ejecución; se resetea al volver a ejecutar una orden con éxito)
- Datos desactualizados (STALE): NO latchea. Cuando el feed está STALE, las entradas
  se bloquean en el filtro de frescura del marco de decisión y en el guard del tick
  (`guards.assert_feed_fresh`); el kill switch solo se dispara
  si el marco de decisión está MISSING (sin velas en absoluto), porque el mercado está
  cerrado (fin de semana/feriado) y el terminal reporta una edad creciente sin error
  real. Esta es una decisión deliberada: un latch por STALE cada fin de semana
  obligaría a un reset manual constante y bloquearía posiciones que ya tienen su SL/TP
  protegido en el broker. Los marcos de contexto (H1/H4/D1) nunca latchean ni bloquean
  entradas: se publican en el panel como información.
- El kill switch NO se activa por la pausa del dashboard; la pausa es un bloqueo de
  nuevas entradas, el kill switch es un cierre de emergencia.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from goldbot.domain.types import (
    AccountState,
    BrokerHealth,
    DataFreshness,
    DataStatus,
    KillSwitchReason,
    KillSwitchState,
    Timeframe,
)


@dataclass(frozen=True, slots=True)
class KillSwitchSettings:
    """Umbrales del kill switch; todos provienen de la sección `safety` del YAML."""

    equity_floor_usd: float = 800.0
    max_consecutive_execution_errors: int = 3
    # Edad máxima que tolera un framework de decisión MISSING antes de matar.
    # (La mera presencia de STALE no mata: véase el docstring del módulo.)
    missing_grace_s: float = 600.0
    # Margen libre mínimo como % del equity. 0.0 desactiva el candado.
    min_free_margin_pct: float = 0.0
    # Lecturas seguidas del terminal sin enlace antes de latchear. Con 1, un `terminal_info()`
    # que devuelve None una sola vez (el terminal reiniciándose, ocupado durante un `order_send`)
    # bloqueaba el día y exigía reset manual. Las entradas ya están cortadas durante la caída
    # por otra vía: las lecturas del ciclo lanzan y el backoff impide operar.
    disconnect_tolerance_polls: int = 3


class LatchedKillSwitch:
    """Prioriza sobre la pausa; el reset se registra como evento y comando.

    El latch se escribe en el StateStore (persistencia) para sobrevivir reinicios del
    proceso: si el bot se mata en el que disparó el kill, el reinicio no puede "olvidar"
    el latch y volver a operar. Por eso `reset()` solo debe responder a un comando
    explícito del operador con token de escritura.
    """

    def __init__(self, settings: KillSwitchSettings | None = None) -> None:
        self._settings = settings or KillSwitchSettings()
        self._state = KillSwitchState(latched=False, reason=None, message="", since_utc=None)
        self._consecutive_errors = 0
        # Racha de lecturas de salud sin enlace; ver `disconnect_tolerance_polls`.
        self._disconnect_polls = 0
        # Historial de resets para auditoría (últimos N orígenes y momentos).
        self._resets: list[tuple[str, datetime]] = []

    @property
    def state(self) -> KillSwitchState:
        """Estado actual del kill switch (latched + motivo + cuándo disparó)."""
        return self._state

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    def evaluate(
        self,
        *,
        health: BrokerHealth,
        account: AccountState,
        freshness: Mapping[Timeframe, DataFreshness],
        decision_timeframe: Timeframe | None = None,
    ) -> KillSwitchState:
        """Evalúa las condiciones de disparo. Si ya está latcheado, lo mantiene (latch).

        Se recorre el orden de prioridad fijo y, en encontrar una condición, se escribe
        el latch con motivo, mensaje y momento. No se re-evalúa un latch ya activo.

        `decision_timeframe` indica de qué marco habla la frescura: sin él tendríamos que
        adivinar el orden de un Mapping, y matar por el framework equivocado (o por el
        D1, que siempre parece "viejo") es peor que no matar.
        """
        if self._state.latched:
            # Un kill switch ya activo solo se relaja desde reset(), nunca por re-evaluación.
            return self._state

        now = health.checked_at_utc
        # 1) Cuenta no demo: imposible que el bot haya llegado aquí en Fase 4 porque el
        #    guard se dispara en el adaptador, pero lo mantenemos como primer nivel.
        if not account.is_demo:
            self._latch(KillSwitchReason.NON_DEMO_ACCOUNT, "cuenta no DEMO", now)
            return self._state

        # 2) Terminal desconectado: no hay forma de operar ni siquiera de cerrar lo abierto.
        #    Se toleran varias lecturas seguidas antes de latchear: un `terminal_info()` que
        #    falla un instante no debe bloquear el día y exigir reset manual.
        if not (health.connected and health.terminal_connected):
            self._disconnect_polls += 1
            detail = "el terminal no está conectado"
            if not health.connected:
                detail = "el terminal MT5 no responde"
            if self._disconnect_polls >= self._settings.disconnect_tolerance_polls:
                self._latch(
                    KillSwitchReason.TERMINAL_DISCONNECTED,
                    f"{self._disconnect_polls} lecturas seguidas sin enlace: {detail}",
                    now,
                )
            return self._state
        self._disconnect_polls = 0

        # 3) Algo trading deshabilitado en el terminal (retcodes 10017/10026/10027): las
        #    órdenes se rechazan sistemáticamente, el bot necesita que el operador decida.
        if not health.algo_trading_enabled:
            self._latch(KillSwitchReason.ALGO_TRADING_OFF, "trading algorítmico deshabilitado", now)
            return self._state

        # 4) Equity por debajo del umbral mínimo de supervivencia.
        if account.equity < self._settings.equity_floor_usd:
            self._latch(
                KillSwitchReason.EQUITY_FLOOR,
                f"equity {account.equity:.2f} por debajo del mínimo de {self._settings.equity_floor_usd:.2f}",
                now,
            )
            return self._state

        # 5) Margen libre por debajo del mínimo. Si el bot sigue abriendo, el broker llega al
        #    stop out y cierra posiciones sin su permiso; mejor detenerse uno mismo.
        margin_floor = self._settings.min_free_margin_pct
        if margin_floor > 0.0 and account.equity > 0.0:
            free_pct = account.margin_free / account.equity * 100.0
            if free_pct < margin_floor:
                self._latch(
                    KillSwitchReason.MARGIN_CRITICAL,
                    f"margen libre {free_pct:.1f}% del equity, por debajo del mínimo de "
                    f"{margin_floor:.1f}%",
                    now,
                )
                return self._state

        # 6) Datos MISSING (ausencia total de velas) por encima de una gracia.
        #    STALE por solo edad elevada no latchea: el mercado está cerrado (fin de
        #    semana, feriado) y el terminal sigue conectado con precios viejos, no rota.
        item = self._decision_freshness(freshness, decision_timeframe)
        if item is not None and item.status is DataStatus.MISSING:
            age = item.age_s if item.age_s is not None else float("inf")
            if age > self._settings.missing_grace_s:
                self._latch(
                    KillSwitchReason.STALE_DATA,
                    f"el marco de decisión {item.timeframe.value} "
                    f"no trae velas desde hace {age:.0f} s",
                    now,
                )
                return self._state

        # 7) Errores de ejecución repetidos (el contador lo maneja la capa de ejecución;
        #    este método solo lee el resultado reportado).
        return self._state

    @staticmethod
    def _decision_freshness(
        freshness: Mapping[Timeframe, DataFreshness],
        decision_timeframe: Timeframe | None,
    ) -> DataFreshness | None:
        """Frescura del marco de decisión, o None si todavía no se midió."""
        if decision_timeframe is not None:
            return freshness.get(decision_timeframe)
        return next(iter(freshness.values()), None)

    def force_latch(
        self, reason: KillSwitchReason, message: str, *, now_utc: datetime
    ) -> KillSwitchState:
        """Reactiva un latch persistido tras un reinicio del proceso.

        El latch vive en el StateStore; al arrancar, el bot lo restaura aquí para que un
        reinicio no "olvide" la emergencia ni permita operar de nuevo sin reset manual.
        """
        self._latch(reason, message, now_utc)
        return self._state

    def _latch(self, reason: KillSwitchReason, message: str, now_utc: datetime) -> None:
        """Escribe el latch con el motivo y el momento."""
        self._state = KillSwitchState(
            latched=True,
            reason=reason,
            message=message,
            since_utc=now_utc,
        )
        self._consecutive_errors = 0

    def note_execution_result(self, success: bool, *, now_utc: datetime) -> None:
        """La capa de ejecución notifica cada intento de orden: un fallo suma, un éxito
        limpia el contador.

        `now_utc` lo aporta el reloj del bot: usar `datetime.now()` aquí mezclaría la hora
        local de Windows con el resto de marcas UTC del registro.
        """
        if success:
            self._consecutive_errors = 0
            return
        self._consecutive_errors += 1
        if self._consecutive_errors >= self._settings.max_consecutive_execution_errors:
            self._latch(
                KillSwitchReason.EXECUTION_ERRORS,
                f"{self._consecutive_errors} errores consecutivos en la ejecución de órdenes",
                now_utc,
            )

    def reset(self, *, now_utc: datetime | None = None, origin: str) -> KillSwitchState:
        """Reset manual auditado; solo el operador (o un proceso autorizado) puede
        reanudar tras un latcheo. El `origin` debe ser "DASHBOARD" (comando con token) o
        una cadena auditada equivalente; nunca se auto-resetea."""
        if now_utc is None:
            now_utc = datetime.now(UTC)
        if not self._state.latched:
            # Ya no está latcheado: no hace nada, no inventa un motivo.
            return self._state
        self._state = KillSwitchState(
            latched=False, reason=None, message="", since_utc=None
        )
        self._disconnect_polls = 0
        self._resets.append((origin, now_utc))
        return self._state

    @property
    def reset_history(self) -> list[tuple[str, datetime]]:
        return list(self._resets)
