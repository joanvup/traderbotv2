"""Broker simulado del backtest: especificación del símbolo y contabilidad de la cuenta."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from goldbot.domain.types import AccountState, SymbolSpec


def default_symbol_spec(
    *,
    symbol: str = "XAUUSD",
    digits: int = 2,
    point: float = 0.01,
    contract_size: float = 100.0,
    tick_value_usd: float = 1.0,
    volume_min: float = 0.01,
    volume_max: float = 50.0,
    volume_step: float = 0.01,
) -> SymbolSpec:
    """Especificación de XAUUSD medida en Pepperstone (Fase 1).

    En vivo, los adaptadores leen esto con `symbol_info()`; aquí solo se usa para
    simulaciones offline y queda etiquetada como tal en `spec_hash`.
    """
    payload = f"backtest|{symbol}|{digits}|{point}|{contract_size}|{tick_value_usd}"
    return SymbolSpec(
        resolved_name=symbol,
        requested_name=symbol,
        digits=digits,
        point=point,
        tick_size=point,
        trade_tick_value_loss=tick_value_usd,
        trade_tick_value_profit=tick_value_usd,
        contract_size=contract_size,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        stops_level_points=0,
        freeze_level_points=0,
        allowed_fillings=2,
        filling_mode=2,
        trade_mode=4,
        currency_profit="USD",
        spec_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        fetched_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )


@dataclass(frozen=True, slots=True)
class SimulatedAccount:
    """Cuenta demo simulada con equity actualizable por el motor."""

    balance: float = 1_000.0
    currency: str = "USD"
    login: int = 0
    server: str = "backtest"

    def state(self, *, equity: float, margin_free: float) -> AccountState:
        return AccountState(
            login=self.login,
            server=self.server,
            currency=self.currency,
            balance=self.balance,
            equity=equity,
            margin_free=margin_free,
            leverage=100,
            is_demo=True,
            trade_allowed=True,
        )
