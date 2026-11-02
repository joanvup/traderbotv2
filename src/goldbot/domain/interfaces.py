"""Protocolos de los componentes del bot. El dominio depende de estas interfaces, no del broker."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from goldbot.domain.events import MarketEvent
from goldbot.domain.types import (
    AccountState,
    BrokerHealth,
    Candle,
    CommandRecord,
    CommandStatus,
    CommandType,
    ContextSnapshot,
    CycleResult,
    DataFreshness,
    DealRecord,
    DecisionRecord,
    EventRecord,
    FilterOutcome,
    KillSwitchState,
    LogLevel,
    ManageAction,
    ModelInfo,
    OrderRequest,
    OrderResult,
    PositionState,
    RegimeState,
    RiskDecision,
    Selection,
    Session,
    Signal,
    StoredPosition,
    SymbolSpec,
    Tick,
    Timeframe,
    TradeRecord,
)

if TYPE_CHECKING:
    import pandas as pd


class BrokerAdapter(Protocol):
    """Adaptador de broker intercambiable: MT5 en vivo, simulador en backtest."""

    def connect(self) -> BrokerHealth: ...
    def shutdown(self) -> None: ...
    def health(self) -> BrokerHealth: ...
    def account(self) -> AccountState: ...
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...
    def server_time(self) -> datetime: ...

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        *,
        closed_only: bool = True,
        as_of_utc: datetime | None = None,
    ) -> Sequence[Candle]: ...

    def get_last_tick(self, symbol: str) -> Tick: ...
    def positions(self, magic: int) -> Sequence[PositionState]: ...
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    def modify_position(
        self,
        position_id: int,
        *,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult: ...

    def close_position(self, position_id: int, *, volume: float | None = None) -> OrderResult: ...
    def close_all(self, magic: int) -> Sequence[OrderResult]: ...

    def closed_deals(
        self,
        *,
        since_utc: datetime,
        until_utc: datetime,
        position_id: int | None = None,
    ) -> Sequence[DealRecord]: ...


class RegimeDetector(Protocol):
    """Clasificador de régimen con probabilidades, confianza e histéresis."""

    @property
    def is_fitted(self) -> bool: ...

    @property
    def warmup_bars(self) -> int: ...

    def fit(self, features: pd.DataFrame, *, train_end_utc: datetime) -> ModelInfo: ...

    def predict(
        self,
        features: pd.DataFrame,
        *,
        state: Any | None = None,
    ) -> RegimeState: ...

    def clone(self) -> RegimeDetector: ...


class Playbook(Protocol):
    """Estrategia concreta con interfaz común; `evaluate` puede abstenerse devolviendo None."""

    playbook_id: str

    def evaluate(self, ctx: ContextSnapshot, regime: RegimeState) -> Signal | None: ...
    def manage(
        self,
        position: PositionState,
        ctx: ContextSnapshot,
        regime: RegimeState,
    ) -> ManageAction: ...


class PlaybookSelector(Protocol):
    def select(self, ctx: ContextSnapshot, regime: RegimeState) -> Selection: ...


class PreTradeFilters(Protocol):
    def evaluate(
        self,
        ctx: ContextSnapshot,
        signal: Signal,
        account: AccountState,
        spec: SymbolSpec,
    ) -> FilterOutcome: ...


class RiskManager(Protocol):
    def size(
        self,
        signal: Signal,
        ctx: ContextSnapshot,
        account: AccountState,
        spec: SymbolSpec,
    ) -> RiskDecision: ...


class TradeManager(Protocol):
    def on_bar(
        self,
        position: PositionState,
        ctx: ContextSnapshot,
        regime: RegimeState,
    ) -> Sequence[ManageAction]: ...


class EconomicCalendar(Protocol):
    """Calendario económico; en vivo usa MT5 nativo, en backtest una caché reproducible."""

    def window(
        self,
        start_utc: datetime,
        end_utc: datetime,
        currencies: Sequence[str],
        min_impact: str,
    ) -> Any: ...

    def is_blackout(
        self,
        ts_utc: datetime,
        before: timedelta,
        after: timedelta,
    ) -> Any: ...


class Clock(Protocol):
    """Reloj inyectable: UTC ↔ hora del servidor, día operativo y sesión."""

    def now_utc(self) -> datetime: ...
    def to_utc(self, server_dt: datetime, *, at_utc: datetime | None = None) -> datetime: ...
    def to_server(self, ts_utc: datetime) -> datetime: ...
    def server_offset(self, at_utc: datetime) -> timedelta: ...
    def trading_day(self, ts_utc: datetime) -> date: ...
    def session_at(self, ts_utc: datetime) -> Session: ...
    def sleep_until(self, ts_utc: datetime) -> None: ...


class StateStore(Protocol):
    """Persistencia SQLite: el bot escribe, el dashboard lee y encola comandos."""

    def initialize(self) -> int: ...
    def write_bot_state(self, state: Mapping[str, Any]) -> None: ...
    def read_bot_state(self) -> Mapping[str, Any] | None: ...

    def write_equity_snapshot(
        self,
        *,
        account_login: int,
        ts_utc: datetime,
        balance: float,
        equity: float,
        margin_free: float,
        floating_pnl: float,
        positions_count: int,
        source: str,
    ) -> None: ...

    def write_event(self, event: EventRecord) -> None: ...
    def write_decision(self, record: DecisionRecord) -> None: ...
    def upsert_position(self, position: PositionState) -> None: ...
    def read_open_positions(self) -> Mapping[int, StoredPosition]: ...
    def delete_position(self, ticket: int) -> None: ...
    def write_trade(self, trade: TradeRecord) -> None: ...

    def write_log_line(
        self,
        ts_utc: datetime,
        level: LogLevel,
        logger_name: str,
        message: str,
        *,
        cycle_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None: ...

    def enqueue_command(
        self,
        command: CommandType,
        args: Mapping[str, Any],
        *,
        idempotency_key: str,
        origin: str,
        ttl_s: int,
    ) -> str: ...

    def claim_next_command(self, *, now_utc: datetime) -> CommandRecord | None: ...
    def complete_command(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        result: Mapping[str, Any] | None = None,
    ) -> None: ...

    def record_order_attempt(
        self,
        *,
        ts_utc: datetime,
        decision_id: str,
        request: Mapping[str, Any],
        retcode: int | None,
        retcode_name: str | None,
        retryable: bool | None,
        result: Mapping[str, Any] | None,
    ) -> None: ...

    def record_history_sync(
        self,
        key: str,
        *,
        last_sync_utc: datetime,
        last_deal_ts_utc: datetime | None,
    ) -> None: ...

    def apply_retention(self, *, retention_days: int, log_lines_retention: int) -> None: ...


class DecisionLogger(Protocol):
    def log(self, record: DecisionRecord) -> None: ...
    def close(self) -> None: ...


class KillSwitch(Protocol):
    def evaluate(
        self,
        *,
        health: BrokerHealth,
        account: AccountState,
        freshness: Mapping[Timeframe, DataFreshness],
    ) -> KillSwitchState: ...


class BotRuntime(Protocol):
    """Contrato del bucle: el mismo `step` procesa eventos en vivo y en backtest."""

    def step(self, event: MarketEvent) -> CycleResult: ...
