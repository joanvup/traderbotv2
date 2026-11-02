"""Guards de arranque y de ciclo: demo, algo trading y frescura del feed."""

from __future__ import annotations

from collections.abc import Mapping

from goldbot.domain.types import AccountState, BrokerHealth, DataFreshness, DataStatus, Timeframe


class GuardViolation(RuntimeError):
    """Se lanza cuando una condición de seguridad impide operar."""


def assert_demo_account(account: AccountState, *, allow_live: bool) -> None:
    """Rechaza operar en cuenta real salvo autorización explícita en config."""
    if not account.is_demo and not allow_live:
        raise GuardViolation(
            f"cuenta {account.login} en {account.server} no es DEMO; ejecución rechazada"
        )
    if not account.trade_allowed:
        raise GuardViolation("el terminal no permite trading para esta cuenta")


def assert_algo_trading(health: BrokerHealth) -> None:
    """El terminal debe tener habilitado el trading algorítmico (retcode 10027)."""
    if not (health.connected and health.terminal_connected):
        raise GuardViolation("terminal MT5 desconectado")
    if not health.algo_trading_enabled:
        raise GuardViolation("trading algorítmico deshabilitado en el terminal")


def assert_feed_fresh(
    freshness: Mapping[Timeframe, DataFreshness],
    *,
    decision_timeframe: Timeframe,
    tick_age_s: float | None,
    max_tick_age_s: float,
) -> None:
    """Bloquea la ENTRADA si el marco de decisión no está fresco o el tick es viejo.

    Solo gatea el marco sobre el que se decide y la edad del último tick. Los marcos de
    contexto (H1/H4/D1) se publican para el panel pero no bloquean: tras reabrir el
    mercado del domingo, su primera vela cerrada de la semana llega horas después, y el
    daily no cierra hasta medianoche del servidor. Exigirlos dejaría al bot sin entradas
    válidas durante todo el lunes sin que haya ningún dato realmente malo, y el precio
    de entrada se calcula sobre el tick, no sobre esas velas.
    """
    item = freshness.get(decision_timeframe)
    if item is None:
        raise GuardViolation(f"sin medición de frescura para {decision_timeframe.value}")
    if item.status is not DataStatus.OK:
        detail = (
            f"última vela cerrada hace {item.age_s:.0f} s"
            if item.age_s is not None
            else "sin velas"
        )
        raise GuardViolation(f"{decision_timeframe.value} en estado {item.status.value} ({detail})")
    if tick_age_s is None or tick_age_s > max_tick_age_s:
        observed = "sin tick" if tick_age_s is None else f"{tick_age_s:.0f} s"
        raise GuardViolation(f"tick atrasado: {observed} (máximo {max_tick_age_s:.0f} s)")
