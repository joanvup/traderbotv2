"""Conexión al terminal MT5 con import perezoso y health check."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goldbot.config import Mt5Credentials
from goldbot.domain.interfaces import Clock
from goldbot.domain.types import BrokerHealth

LOGGER = logging.getLogger(__name__)


def load_mt5() -> Any:
    """Importa MetaTrader5 solo cuando se ejecuta el bot real, nunca al importar el paquete."""
    import MetaTrader5 as mt5  # noqa: PLC0415 - import perezoso intencional

    return mt5


class Mt5Connection:
    """Mantiene una única sesión con el terminal; MT5 no es thread-safe."""

    def __init__(
        self,
        clock: Clock,
        symbol: str,
        *,
        terminal_path: str | Path | None = None,
        timeout_ms: int = 60_000,
        credentials: Mt5Credentials | None = None,
    ) -> None:
        self._clock = clock
        self._symbol = symbol
        self._terminal_path = Path(terminal_path) if terminal_path else None
        self._timeout_ms = timeout_ms
        self._credentials = credentials
        self._mt5: Any | None = None
        self._connected = False
        self._latency_ms: float | None = None

    @property
    def api(self) -> Any:
        """Módulo MetaTrader5 ya inicializado."""
        if self._mt5 is None or not self._connected:
            raise RuntimeError("MT5 no está inicializado; llama a connect() primero")
        return self._mt5

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> BrokerHealth:
        """Inicializa el terminal; usa login de `.env` solo si está completo."""
        mt5 = load_mt5()
        kwargs: dict[str, Any] = {"timeout": self._timeout_ms}
        if self._terminal_path is not None:
            kwargs["path"] = str(self._terminal_path)
        credentials = self._credentials
        if (
            credentials is not None
            and credentials.login is not None
            and credentials.password is not None
            and credentials.server is not None
        ):
            kwargs["login"] = credentials.login
            kwargs["password"] = credentials.password.get_secret_value()
            kwargs["server"] = credentials.server

        started = time.perf_counter()
        ok = bool(mt5.initialize(**kwargs))
        self._latency_ms = (time.perf_counter() - started) * 1000.0
        self._mt5 = mt5
        self._connected = ok
        if not ok:
            LOGGER.error("initialize de MT5 falló: %s", mt5.last_error())
            self._mt5 = None
            return self._health()
        LOGGER.info(
            "MT5 inicializado (build %s, terminal %s, %.0f ms)",
            mt5.version()[1] if mt5.version() else "?",
            mt5.terminal_info().name if mt5.terminal_info() else "?",
            self._latency_ms,
        )
        return self._health()

    def shutdown(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
        self._mt5 = None
        self._connected = False

    def health(self) -> BrokerHealth:
        """Lee terminal_info()/account_info(); expone algo trading y trade_allowed."""
        return self._health()

    def _health(self) -> BrokerHealth:
        now = self._clock.now_utc()
        if not self._connected or self._mt5 is None:
            return BrokerHealth(
                connected=False,
                terminal_connected=False,
                algo_trading_enabled=False,
                account_trade_allowed=False,
                latency_ms=self._latency_ms,
                last_error="sin conexión con el terminal",
                checked_at_utc=now,
            )

        terminal = self._mt5.terminal_info()
        account = self._mt5.account_info()
        connected = terminal is not None and account is not None
        return BrokerHealth(
            connected=connected,
            terminal_connected=bool(terminal.connected) if terminal is not None else False,
            algo_trading_enabled=bool(terminal.trade_allowed) if terminal is not None else False,
            account_trade_allowed=(
                bool(account.trade_allowed and account.trade_expert) if account is not None else False
            ),
            latency_ms=self._latency_ms,
            last_error=None if connected else str(self._mt5.last_error()),
            checked_at_utc=now,
        )

    def server_time(self) -> datetime:
        """Hora naïve del servidor según la regla NY+7h; se convierte a UTC con `UtcClock`."""
        return self._clock.to_server(self._clock.now_utc())

    def tick_offset_check(self) -> float | None:
        """Diferencia (s) entre el último tick y la hora de servidor esperada.

        Cerca de 0 confirma la regla horaria; un valor muy negativo suele significar
        mercado cerrado (tick viejo), no un error de configuración.
        """
        if not self._connected or self._mt5 is None:
            return None
        tick = self._mt5.symbol_info_tick(self._symbol)
        if tick is None or not tick.time:
            return None
        tick_wall = datetime.fromtimestamp(int(tick.time), UTC)
        expected = self._clock.to_server(self._clock.now_utc()).replace(tzinfo=UTC)
        return (tick_wall - expected).total_seconds()
