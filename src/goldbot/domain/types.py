"""Tipos de dominio puros: enums y dataclasses inmutables, sin dependencias del broker.

Convención temporal: todas las marcas de tiempo son `datetime` con zona UTC. La hora
del servidor solo se usa en los bordes (adaptador y dashboard) y se convierte en `Clock`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Timeframe(StrEnum):
    """Timeframes soportados por el bot."""

    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Session(StrEnum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    CLOSED = "CLOSED"


class Regime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class BotState(StrEnum):
    OPERATIVO = "OPERATIVO"
    PAUSADO = "PAUSADO"
    BLOQUEADO = "BLOQUEADO"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    KILLED = "KILLED"
    ERROR = "ERROR"


class DataStatus(StrEnum):
    OK = "OK"
    STALE = "STALE"
    MISSING = "MISSING"


class CalendarHealth(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class EventSource(StrEnum):
    BOT = "BOT"
    MT5 = "MT5"
    DASHBOARD = "DASHBOARD"


class EventType(StrEnum):
    STARTUP = "STARTUP"
    SHUTDOWN = "SHUTDOWN"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    ENTRY = "ENTRY"
    REVIEW = "REVIEW"
    CLOSED = "CLOSED"
    RECONNECT = "RECONNECT"
    KILL_SWITCH = "KILL_SWITCH"
    KILL_RESET = "KILL_RESET"
    ERROR = "ERROR"
    COMMAND = "COMMAND"
    CONFIG = "CONFIG"
    FEED_STALE = "FEED_STALE"
    REGIME_CHANGE = "REGIME_CHANGE"
    RECONCILE = "RECONCILE"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class CommandType(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CLOSE_ALL = "CLOSE_ALL"
    RESET_KILL_SWITCH = "RESET_KILL_SWITCH"


class CommandStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class ExitReason(StrEnum):
    SL = "SL"
    TP = "TP"
    BOT = "BOT"
    BREAKEVEN = "BREAKEVEN"
    TRAILING = "TRAILING"
    TIME = "TIME"
    REGIME = "REGIME"
    EXTERNAL = "EXTERNAL"


class ExitReasonSource(StrEnum):
    BOT = "BOT"
    SL = "SL"
    TP = "TP"
    EXTERNAL = "EXTERNAL"
    RECON = "RECON"


class OrderErrorCategory(StrEnum):
    NONE = "NONE"
    RETRYABLE = "RETRYABLE"
    FATAL = "FATAL"
    ALGO_DISABLED = "ALGO_DISABLED"
    INVALID_REQUEST = "INVALID_REQUEST"
    REJECTED = "REJECTED"


class ManageKind(StrEnum):
    HOLD = "HOLD"
    MOVE_SL_BREAKEVEN = "MOVE_SL_BREAKEVEN"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    TRAIL_SL = "TRAIL_SL"
    CLOSE = "CLOSE"


class Urgency(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class FilterCode(StrEnum):
    DATA_STALE = "DATA_STALE"
    KILL_SWITCH = "KILL_SWITCH"
    DEMO_GUARD = "DEMO_GUARD"
    SPREAD = "SPREAD"
    NEWS = "NEWS"
    SESSION = "SESSION"
    MAX_TRADES = "MAX_TRADES"
    DAILY_LOSS = "DAILY_LOSS"
    SPEC_INVALID = "SPEC_INVALID"


class KillSwitchReason(StrEnum):
    TERMINAL_DISCONNECTED = "TERMINAL_DISCONNECTED"
    EXECUTION_ERRORS = "EXECUTION_ERRORS"
    EQUITY_FLOOR = "EQUITY_FLOOR"
    MARGIN_CRITICAL = "MARGIN_CRITICAL"
    STALE_DATA = "STALE_DATA"
    ALGO_TRADING_OFF = "ALGO_TRADING_OFF"
    NON_DEMO_ACCOUNT = "NON_DEMO_ACCOUNT"
    MANUAL = "MANUAL"


# ---------------------------------------------------------------------------
# Mercado y especificaciones
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candle:
    """Vela OHLC. `is_closed=False` marca la vela en formación y nunca debe decidir."""

    open_time_utc: datetime
    close_time_utc: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread_points: float
    is_closed: bool
    received_at_utc: datetime
    source: str


@dataclass(frozen=True, slots=True)
class Tick:
    time_utc: datetime
    bid: float
    ask: float
    last: float
    spread_points: float


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Especificaciones reales leídas con symbol_info(); nunca se asumen constantes."""

    resolved_name: str
    requested_name: str
    digits: int
    point: float
    tick_size: float
    trade_tick_value_loss: float
    trade_tick_value_profit: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int
    allowed_fillings: int
    filling_mode: int
    trade_mode: int
    currency_profit: str
    spec_hash: str
    fetched_at_utc: datetime


# Un filling vive en dos espacios de valores distintos y confundarlos rompe el envío:
# `symbol_info().filling_mode` es un BITMASK (1 FOK / 2 IOC / 4 RETURN) mientras que
# `MqlTradeRequest.type_filling` usa ORDER_FILLING_* (0 FOK / 1 IOC / 2 RETURN / 3 BOC).
# El dominio solo necesita traducir el valor de la orden al bit del símbolo.
_FILLING_REQUEST_TO_MASK: dict[int, int] = {0: 1, 1: 2, 2: 4, 3: 4}


def filling_mask_bit(request_value: int) -> int:
    """Bit del bitmask del símbolo que corresponde a un `type_filling` de la orden."""
    return _FILLING_REQUEST_TO_MASK.get(int(request_value), 0)


@dataclass(frozen=True, slots=True)
class AccountState:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    is_demo: bool
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class BrokerHealth:
    connected: bool
    terminal_connected: bool
    algo_trading_enabled: bool
    account_trade_allowed: bool
    latency_ms: float | None
    last_error: str | None
    checked_at_utc: datetime


@dataclass(frozen=True, slots=True)
class DealRecord:
    """Deal individual de MT5; los cierres parciales generan varios deals OUT."""

    deal_ticket: int
    position_id: int
    order_ticket: int | None
    entry: str
    side: Side
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    time_utc: datetime
    comment: str


# ---------------------------------------------------------------------------
# Contexto
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bias:
    timeframe: Timeframe
    direction: Side | None
    strength: float
    ema_fast: float
    ema_slow: float
    slope_normalized: float
    rationale: str


@dataclass(frozen=True, slots=True)
class Swing:
    kind: Literal["HIGH", "LOW"]
    price: float
    bar_time_utc: datetime
    bars_ago: int


@dataclass(frozen=True, slots=True)
class Level:
    kind: Literal["SUPPORT", "RESISTANCE", "PREV_DAY_HIGH", "PREV_DAY_LOW", "ROUND"]
    price: float
    strength: float
    touches: int
    bar_time_utc: datetime | None


@dataclass(frozen=True, slots=True)
class StructureState:
    swings: tuple[Swing, ...]
    levels: tuple[Level, ...]
    prev_day_high: float | None
    prev_day_low: float | None
    prev_day_range: float | None
    day_open: float | None
    range_position: float | None
    nearest_level_distance: float | None


@dataclass(frozen=True, slots=True)
class TimingState:
    atr: float
    atr_percentile: float
    spread_points: float
    last_bar_range: float
    momentum: float
    volatility_ratio: float
    spread_mean_points: float = 0.0


@dataclass(frozen=True, slots=True)
class DataFreshness:
    timeframe: Timeframe
    last_bar_close_utc: datetime | None
    age_s: float | None
    status: DataStatus


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Foto inmutable y reproducible del contexto en el instante de decidir."""

    decision_time_utc: datetime
    data_cutoff_utc: datetime
    bar_time_utc: datetime
    decision_timeframe: Timeframe
    bias: Mapping[Timeframe, Bias]
    structure: StructureState
    timing: TimingState
    session: Session
    freshness: Mapping[Timeframe, DataFreshness]
    snapshot_hash: str
    config_hash: str
    model_versions: Mapping[str, str]
    last_price: float = 0.0

    def to_jsonable(self) -> dict[str, Any]:
        """Serializa el snapshot a estructuras JSON para StateStore y DecisionLog."""
        return {
            "decision_time_utc": _iso_utc(self.decision_time_utc),
            "data_cutoff_utc": _iso_utc(self.data_cutoff_utc),
            "bar_time_utc": _iso_utc(self.bar_time_utc),
            "decision_timeframe": self.decision_timeframe.value,
            "bias": {
                timeframe.value: {
                    "timeframe": bias.timeframe.value,
                    "direction": bias.direction.value if bias.direction else None,
                    "strength": bias.strength,
                    "ema_fast": bias.ema_fast,
                    "ema_slow": bias.ema_slow,
                    "slope_normalized": bias.slope_normalized,
                    "rationale": bias.rationale,
                }
                for timeframe, bias in self.bias.items()
            },
            "structure": {
                "swings": [
                    {
                        "kind": swing.kind,
                        "price": swing.price,
                        "bar_time_utc": _iso_utc(swing.bar_time_utc),
                        "bars_ago": swing.bars_ago,
                    }
                    for swing in self.structure.swings
                ],
                "levels": [
                    {
                        "kind": level.kind,
                        "price": level.price,
                        "strength": level.strength,
                        "touches": level.touches,
                        "bar_time_utc": (
                            _iso_utc(level.bar_time_utc) if level.bar_time_utc else None
                        ),
                    }
                    for level in self.structure.levels
                ],
                "prev_day_high": self.structure.prev_day_high,
                "prev_day_low": self.structure.prev_day_low,
                "prev_day_range": self.structure.prev_day_range,
                "day_open": self.structure.day_open,
                "range_position": self.structure.range_position,
                "nearest_level_distance": self.structure.nearest_level_distance,
            },
            "timing": {
                "atr": self.timing.atr,
                "atr_percentile": self.timing.atr_percentile,
                "spread_points": self.timing.spread_points,
                "spread_mean_points": self.timing.spread_mean_points,
                "last_bar_range": self.timing.last_bar_range,
                "momentum": self.timing.momentum,
                "volatility_ratio": self.timing.volatility_ratio,
            },
            "session": self.session.value,
            "freshness": {
                timeframe.value: {
                    "timeframe": fresh.timeframe.value,
                    "last_bar_close_utc": (
                        _iso_utc(fresh.last_bar_close_utc) if fresh.last_bar_close_utc else None
                    ),
                    "age_s": fresh.age_s,
                    "status": fresh.status.value,
                }
                for timeframe, fresh in self.freshness.items()
            },
            "snapshot_hash": self.snapshot_hash,
            "config_hash": self.config_hash,
            "model_versions": dict(self.model_versions),
            "last_price": self.last_price,
        }


# ---------------------------------------------------------------------------
# Régimen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeProbabilities:
    probs: Mapping[Regime, float]
    confidence: float
    method: str


@dataclass(frozen=True, slots=True)
class RegimeState:
    label: Regime
    probs: RegimeProbabilities
    confidence: float
    confirmed_bars: int
    decided_at_utc: datetime


@dataclass(frozen=True, slots=True)
class HysteresisState:
    current: Regime
    candidate: Regime | None
    streak: int
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str
    version: str
    trained_from_utc: datetime
    trained_until_utc: datetime
    n_states: int
    features: tuple[str, ...]
    score: float | None
    params_hash: str


# ---------------------------------------------------------------------------
# Señales y gestión
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    playbook_id: str
    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float | None
    risk_distance: float
    confidence: float
    regime_at_signal: Regime
    created_at_utc: datetime
    valid_until_bar_utc: datetime | None
    invalidation_price: float | None
    rationale: str
    inputs_hash: str | None


@dataclass(frozen=True, slots=True)
class Selection:
    playbook_id: str
    scores: Mapping[str, float]
    rationale: str
    fallback_no_trade: bool


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Resultado del núcleo de decisión: selección, señal, filtros y acción final."""

    selection: Selection
    signal: Signal | None
    filters: FilterOutcome | None
    action: Literal["ENTER", "NO_SETUP", "BLOCKED", "NO_TRADE"]
    rationale: str


@dataclass(frozen=True, slots=True)
class StoredPosition:
    """Foto de una posición abierta guardada por el bot (tabla `positions`).

    El broker no sabe qué acciones tomó ESTE bot sobre la posición, así que el
    StateStore es la fuente del `breakeven_done`/`partials_done` y de los datos de
    entrada necesarios para cerrar el historial cuando la operación muere en el
    servidor (SL/TP) sin que el bot la vea cerrada.
    """

    ticket: int
    side: Side
    volume: float
    entry_price: float
    sl: float | None
    tp: float | None
    opened_at_utc: datetime
    playbook_id: str | None
    regime_at_entry: Regime | None
    decision_id_entry: str | None
    initial_risk_price: float
    r_multiple: float
    last_action: str | None
    breakeven_done: bool
    partials_done: int


@dataclass(frozen=True, slots=True)
class ManageAction:
    kind: ManageKind
    reason: str
    new_sl: float | None = None
    close_volume: float | None = None
    urgency: Urgency = Urgency.NORMAL
    decided_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class PositionState:
    ticket: int
    position_id: int
    side: Side
    volume: float
    entry_price: float
    current_price: float
    sl: float | None
    tp: float | None
    pnl_money: float
    initial_risk_price: float
    r_multiple: float
    playbook_id: str
    regime_at_entry: Regime | None
    decision_id_entry: str | None
    opened_at_utc: datetime
    breakeven_done: bool = False
    partials_done: int = 0
    last_action: str | None = None


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    volume: float
    sl: float
    tp: float | None
    magic: int
    deviation_points: int
    filling: int | None
    comment: str
    decision_id: str


@dataclass(frozen=True, slots=True)
class OrderResult:
    retcode: int
    retcode_name: str
    order_ticket: int | None
    deal_ticket: int | None
    request_id: int | None
    retryable: bool
    category: OrderErrorCategory
    price: float | None
    raw: Mapping[str, Any]

    @property
    def filled(self) -> bool:
        """True si el broker llenó la orden (10009 DONE o 10010 DONE_PARTIAL).

        Un DONE_PARTIAL SÍ abre posición (con volumen reducido): tratarlo como fallo
        dejaría una posición huérfana sin gestionar.
        """
        return self.retcode in (10009, 10010)

    @property
    def outcome_unknown(self) -> bool:
        """True si no sabemos si la orden llegó al mercado: hay que reconciliar.

        La categoría `FATAL` la usan los retcodes de timeout/error/conexión y el
        "terminal no devolvió respuesta". Reintentar sin reconciliar duplicaría la
        posición, y asumirla fallida dejaría una operación viva sin proteger.
        """
        return self.category is OrderErrorCategory.FATAL


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    severity: Literal["ERROR", "WARNING"] = "ERROR"


# ---------------------------------------------------------------------------
# Riesgo y filtros
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    lots: float | None
    risk_money: float | None
    risk_pct_actual: float | None
    sl_distance_price: float
    sl_distance_points: float
    reason: str
    spec_hash: str
    equity_at_decision: float


@dataclass(frozen=True, slots=True)
class FilterResult:
    code: FilterCode
    blocked: bool
    observed: Any
    threshold: Any
    message: str


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    results: tuple[FilterResult, ...]
    blocked: bool
    primary_reason: str | None


# ---------------------------------------------------------------------------
# Operaciones cerradas y estado del sistema
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeRecord:
    position_id: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    exit_price: float
    sl: float | None
    tp: float | None
    open_time_utc: datetime
    close_time_utc: datetime
    entry_deal_id: int | None
    close_deal_id: int | None
    commission: float
    swap: float
    net_profit: float
    r_multiple: float | None
    duration_s: int
    exit_reason: ExitReason
    exit_reason_source: ExitReasonSource
    playbook_id: str | None
    regime_at_entry: Regime | None
    decision_id_entry: str | None
    reconciled_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class DailyStats:
    trading_day: date
    timezone: str
    realized_pnl: float
    equity_start: float | None
    equity_end: float | None
    entries_used: int
    wins: int
    losses: int
    gross_profit: float
    gross_loss: float
    daily_lock_latched: bool


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    latched: bool
    reason: KillSwitchReason | None
    message: str
    since_utc: datetime | None


@dataclass(frozen=True, slots=True)
class CycleResult:
    cycle_id: str
    action: str
    decision_id: str | None
    actions_taken: tuple[str, ...]
    error: str | None


@dataclass(frozen=True, slots=True)
class EventRecord:
    ts_utc: datetime
    source: EventSource
    type: EventType
    level: LogLevel
    message: str
    correlation_id: str | None = None
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command_id: str
    command: CommandType
    args: Mapping[str, Any]
    origin: str
    status: CommandStatus
    created_at_utc: datetime
    expires_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Línea auditable de decisión, equivalente a una línea del DecisionLog JSONL."""

    decision_id: str
    cycle_id: str
    ts_utc: datetime
    bar_time_utc: datetime
    action: str
    snapshot: ContextSnapshot
    regime: RegimeState
    playbook_id: str
    signal: Signal | None
    filters: FilterOutcome | None
    risk: RiskDecision | None
    rationale: str
    latency_ms: int
    config_hash: str
    model_versions: Mapping[str, str]


# ---------------------------------------------------------------------------
# Calendario económico
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_id: str
    ts_utc: datetime
    currency: str
    impact: str
    title: str
    event_code: str = ""
    value_id: str = ""

    @property
    def occurrence_key(self) -> str:
        """`event_id` es el tipo de evento (se repite cada mes); la ocurrencia es el value_id."""
        return self.value_id or f"{self.event_id}@{int(self.ts_utc.timestamp())}"


@dataclass(frozen=True, slots=True)
class CalendarWindow:
    events: tuple[CalendarEvent, ...]
    health: CalendarHealth
    fetched_at_utc: datetime


@dataclass(frozen=True, slots=True)
class CalendarBlock:
    blocked: bool
    events: tuple[CalendarEvent, ...]
    reason: str | None


# ---------------------------------------------------------------------------
# Ajustes inmutables de cada componente (mapeados desde el YAML en el arranque)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextSettings:
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    swing_lookback: int = 3
    slope_lookback: int = 20
    level_tolerance_atr: float = 0.25
    atr_percentile_window: int = 200
    momentum_lookback: int = 5
    structure_lookback_bars: int = 200
    max_swings: int = 12
    max_levels: int = 8
    round_level_step: float = 10.0
    spread_lookback_bars: int = 100


@dataclass(frozen=True, slots=True)
class RegimeSettings:
    method: Literal["rules", "hmm", "both"] = "both"
    confidence_min: float = 0.6
    hysteresis_bars: int = 3
    n_states: int = 4
    train_window_bars: int = 5_000
    retrain_every_bars: int = 500
    adx_period: int = 14
    atr_period: int = 14
    atr_percentile_window: int = 200
    atr_high_percentile: float = 0.8
    ema_fast: int = 20
    ema_slow: int = 50
    slope_lookback: int = 20
    min_efficiency: float = 0.35


@dataclass(frozen=True, slots=True)
class FilterSettings:
    max_spread_multiplier: float = 2.5
    spread_lookback_bars: int = 100
    news_window_before_min: int = 30
    news_window_after_min: int = 30
    news_min_impact: str = "high"
    news_currencies: tuple[str, ...] = ("USD",)
    calendar_unavailable_policy: Literal["block", "allow"] = "block"
    news_critical_event_codes: tuple[str, ...] = ()
    entry_windows: tuple[str, ...] = ("07:00-20:00",)
    avoid_rollover: bool = True
    rollover_buffer_min: int = 15
    max_trades_day: int = 2
    daily_loss_pct: float = 2.0


@dataclass(frozen=True, slots=True)
class DailyCounters:
    """Contadores del día operativo que consumen los filtros (en vivo salen de SQLite)."""

    trading_day: date
    entries_used: int = 0
    realized_pnl: float = 0.0
    day_start_equity: float | None = None


@dataclass(frozen=True, slots=True)
class RiskSettings:
    risk_pct: float = 1.0
    daily_loss_pct: float = 2.0
    max_trades_day: int = 2
    equity_floor_usd: float = 800.0


@dataclass(frozen=True, slots=True)
class ManagementSettings:
    breakeven_at_r: float = 1.0
    partial_at_r: float = 1.5
    partial_close_pct: float = 0.5
    trailing_atr_mult: float = 1.5
    time_stop_bars: int = 48
    regime_exit_confidence: float = 0.7
    atr_sl_mult: float = 1.5


@dataclass(frozen=True, slots=True)
class PlaybookSettings:
    enabled: bool = True
    min_confidence: float = 0.6
    params: Mapping[str, float] = field(default_factory=dict)
