"""Modelos de configuración del bot, cargados desde YAML y validados con pydantic v2."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

TimeframeName = Literal["M5", "M15", "H1", "H4", "D1"]
ImpactLevel = Literal["low", "medium", "moderate", "high"]
LoopbackHost = Literal["127.0.0.1", "localhost", "::1"]
_FILLING_POLICY = Literal["auto", "ioc", "fok", "return"]

_SESSION_WINDOW = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d")


class Section(BaseModel):
    """Base de todas las secciones: prohíbe claves desconocidas e inmutabilidad."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(Section):
    """Identidad del bot, regla horaria del servidor y rutas locales."""

    name: str = "goldbot"
    # Pepperstone: hora del servidor = America/New_York + 7 h
    # (GMT+3 con el horario de verano de EE. UU., GMT+2 sin él).
    server_timezone: str = "America/New_York"
    server_shift_hours: float = Field(default=7.0, ge=-12.0, le=14.0)
    magic: int = Field(default=20261030, ge=1, le=2_147_483_647)
    db_path: Path = Path("data/state.db")
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")

    @field_validator("server_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:  # noqa: BLE001 - se reempaqueta con contexto
            raise ValueError(f"zona horaria IANA desconocida: {value!r}") from exc
        return value


class Mt5Section(Section):
    """Parámetros de conexión al terminal; las credenciales viven en .env."""

    terminal_path: Path | None = None
    symbol: str = "XAUUSD"
    symbol_override: str | None = None
    timeout_ms: int = Field(default=60_000, ge=1_000, le=600_000)
    use_terminal_session: bool = True


class RiskSection(Section):
    """Límites duros de riesgo. `max_trades_day` nunca puede superar 2."""

    risk_pct: float = Field(default=1.0, gt=0.0, le=5.0)
    daily_loss_pct: float = Field(default=2.0, gt=0.0, le=10.0)
    max_trades_day: int = Field(default=2, ge=0, le=2)
    equity_floor_usd: float = Field(default=800.0, ge=0.0)


class SessionsSection(Section):
    """Ventanas de entrada y de sesión expresadas en hora del servidor.

    Las ventanas de sesión son informativas para el contexto; las de `entries`
    gobiernan el filtro de horario de fases posteriores.
    """

    entries: list[str] = Field(default_factory=lambda: ["07:00-20:00"])
    avoid_rollover: bool = True
    # Horas de servidor (GMT+3/GMT+2) de cada sesión de liquidez.
    asia: list[str] = Field(default_factory=lambda: ["03:00-11:00"])
    london: list[str] = Field(default_factory=lambda: ["11:00-19:00"])
    new_york: list[str] = Field(default_factory=lambda: ["16:00-00:00"])

    @field_validator("entries", "asia", "london", "new_york")
    @classmethod
    def _valid_windows(cls, values: list[str]) -> list[str]:
        for window in values:
            if not _SESSION_WINDOW.fullmatch(window):
                raise ValueError(f"ventana horaria inválida: {window!r} (esperado HH:MM-HH:MM)")
        return values


def _default_entry_timeframes() -> list[TimeframeName]:
    return ["M5", "M15"]


def _default_context_timeframes() -> list[TimeframeName]:
    return ["M15", "H1", "H4", "D1"]


def _default_warmup_bars() -> dict[TimeframeName, int]:
    return {"M5": 600, "M15": 400, "H1": 600, "H4": 400, "D1": 300}


def _default_stale_after_s() -> dict[TimeframeName, int]:
    return {"M5": 360, "M15": 1_000, "H1": 4_000, "H4": 16_000, "D1": 90_000}


class DataSection(Section):
    """Timeframes usados y umbrales de frescura de datos por timeframe."""

    decision_timeframe: TimeframeName = "M5"
    entry_timeframes: list[TimeframeName] = Field(default_factory=_default_entry_timeframes)
    context_timeframes: list[TimeframeName] = Field(
        default_factory=_default_context_timeframes
    )
    warmup_bars: dict[TimeframeName, int] = Field(default_factory=_default_warmup_bars)
    max_feed_age_s: int = Field(default=120, ge=5)
    stale_after_s: dict[TimeframeName, int] = Field(default_factory=_default_stale_after_s)

    @model_validator(mode="after")
    def _required_timeframes_present(self) -> Self:
        required = set(self.context_timeframes) | set(self.entry_timeframes)
        required.add(self.decision_timeframe)
        missing_warmup = required - set(self.warmup_bars)
        missing_stale = required - set(self.stale_after_s)
        if missing_warmup:
            raise ValueError(f"faltan warmup_bars para: {sorted(missing_warmup)}")
        if missing_stale:
            raise ValueError(f"faltan stale_after_s para: {sorted(missing_stale)}")
        return self


class RegimeSection(Section):
    """Umbrales del clasificador de régimen (reglas, HMM y histéresis)."""

    method: Literal["rules", "hmm", "both"] = "both"
    confidence_min: float = Field(default=0.6, ge=0.0, le=1.0)
    hysteresis_bars: int = Field(default=3, ge=1, le=20)
    n_states: int = Field(default=4, ge=2, le=6)
    train_window_bars: int = Field(default=5_000, ge=500)
    retrain_every_bars: int = Field(default=500, ge=50)
    adx_period: int = Field(default=14, ge=2, le=200)
    atr_period: int = Field(default=14, ge=2, le=200)
    atr_percentile_window: int = Field(default=200, ge=20)
    atr_high_percentile: float = Field(default=0.8, ge=0.5, le=0.99)
    ema_fast: int = Field(default=20, ge=2)
    ema_slow: int = Field(default=50, ge=3)
    slope_lookback: int = Field(default=20, ge=2)
    min_efficiency: float = Field(default=0.35, ge=0.0, le=1.0)
    # Construcción del contexto multi-timeframe (estructura y timing).
    swing_lookback: int = Field(default=3, ge=1, le=20)
    level_tolerance_atr: float = Field(default=0.25, gt=0.0, le=2.0)
    momentum_lookback: int = Field(default=5, ge=2, le=100)
    structure_lookback_bars: int = Field(default=200, ge=50)
    max_swings: int = Field(default=12, ge=2, le=100)
    max_levels: int = Field(default=8, ge=2, le=50)
    round_level_step: float = Field(default=10.0, gt=0.0)

    @model_validator(mode="after")
    def _ema_order(self) -> Self:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast debe ser menor que ema_slow")
        return self


class PlaybookConfig(Section):
    """Activación, confianza mínima y parámetros libres de un playbook."""

    enabled: bool = True
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    params: dict[str, float] = Field(default_factory=dict)


def _default_playbooks() -> dict[str, PlaybookConfig]:
    return {
        "trend_pullback": PlaybookConfig(
            min_confidence=0.6, params={"pullback_atr": 0.8, "sl_atr": 1.5, "rr": 2.0}
        ),
        "breakout": PlaybookConfig(
            min_confidence=0.6, params={"buffer_atr": 0.15, "sl_atr": 1.2, "rr": 2.0}
        ),
        "range_fade": PlaybookConfig(
            min_confidence=0.7, params={"edge_atr": 0.3, "sl_atr": 1.0, "rr": 1.5}
        ),
        "no_trade": PlaybookConfig(enabled=True, min_confidence=0.0, params={}),
    }


class FiltersSection(Section):
    """Filtros previos a operar: spread, noticias, sesión y calendario."""

    max_spread_multiplier: float = Field(default=2.5, gt=0.0)
    spread_lookback_bars: int = Field(default=100, ge=10)
    news_window_before_min: int = Field(default=30, ge=0, le=240)
    news_window_after_min: int = Field(default=30, ge=0, le=240)
    # El calendario de Pepperstone no publica "high": su máximo es "moderate".
    news_min_impact: ImpactLevel = "moderate"
    news_currencies: list[str] = Field(default_factory=lambda: ["USD"])
    # Códigos de evento que bloquean aunque su impacto sea menor (p. ej. FOMC).
    news_critical_event_codes: list[str] = Field(default_factory=list)
    # null = <APPDATA>\MetaQuotes\Terminal\Common\Files\goldbot\calendar_events.csv
    calendar_bridge_path: str | None = None
    calendar_max_age_s: int = Field(default=180, gt=0)
    calendar_unavailable_policy: Literal["block", "allow"] = "block"
    # Minutos bloqueados alrededor del cambio de día del servidor (rollover).
    rollover_buffer_min: int = Field(default=15, ge=0, le=120)


class ExecutionSection(Section):
    """Parámetros de envío de órdenes y reintentos."""

    deviation_points: int = Field(default=20, ge=0)
    filling_policy: _FILLING_POLICY = "auto"
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_s: float = Field(default=1.0, ge=0.0)


class ManagementSection(Section):
    """Reglas de gestión de la posición viva (BE, parciales, trailing, tiempo)."""

    breakeven_at_r: float = Field(default=1.0, gt=0.0)
    partial_at_r: float = Field(default=1.5, gt=0.0)
    partial_close_pct: float = Field(default=0.5, gt=0.0, le=1.0)
    trailing_atr_mult: float = Field(default=1.5, gt=0.0)
    time_stop_bars: int = Field(default=48, ge=1)
    regime_exit_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    atr_sl_mult: float = Field(default=1.5, gt=0.0)


class DashboardSection(Section):
    """Servidor local del dashboard: solo loopback y polling configurable."""

    host: LoopbackHost = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    token_env: str = "DASHBOARD_TOKEN"
    stale_after_s: int = Field(default=15, ge=1)
    refresh_s: int = Field(default=3, ge=1, le=60)
    close_all_enabled: bool = True
    # `CLOSE_ALL` caduca: cerrar posiciones horas después de que el operador lo pidió no
    # es lo que él pidió. Pausa, reanudar y reset del kill switch no caducan nunca.
    close_all_ttl_s: int = Field(default=120, ge=10, le=3600)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("el dashboard solo puede escuchar en loopback (127.0.0.1)")
        return value


class BacktestSection(Section):
    """Supuestos realistas del backtest y ventanas walk-forward."""

    initial_balance: float = Field(default=1_000.0, gt=0.0)
    commission_per_lot_side_usd: float = Field(default=3.5, ge=0.0)
    slippage_points: int = Field(default=2, ge=0)
    train_window_days: int = Field(default=180, ge=30)
    test_window_days: int = Field(default=30, ge=7)
    step_days: int = Field(default=30, ge=7)
    # A partir de cuántas operaciones una celda del informe (régimen × playbook) dice algo.
    # Por debajo, el informe la marca como "no concluyente" en vez de razonar sobre ella.
    min_cell_trades: int = Field(default=10, ge=2)

    @model_validator(mode="after")
    def _windows_coherent(self) -> Self:
        if self.test_window_days > self.train_window_days:
            raise ValueError("test_window_days no puede superar train_window_days")
        return self


class StorageSection(Section):
    """Retención de datos en SQLite y del feed de log."""

    retention_days: int = Field(default=90, ge=1)
    log_lines_retention: int = Field(default=2_000, ge=100)


class SafetySection(Section):
    """Candados de seguridad: demo obligatoria y umbrales del kill switch."""

    allow_live: bool = False
    require_demo: bool = True
    max_consecutive_execution_errors: int = Field(default=3, ge=1)
    # Segundos que tarda el marco de decisión en `MISSING` en latchear el kill switch.
    missing_grace_s: float = Field(default=600.0, ge=10.0)
    # Margen libre mínimo, en % del equity, antes de latchear. 0.0 desactiva el candado.
    min_free_margin_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    # Lecturas de salud sin enlace antes de latchear TERMINAL_DISCONNECTED. Con 1, un
    # `terminal_info()` que falla un instante bloquea el día y exige reset manual.
    disconnect_tolerance_polls: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _demo_guard_coherent(self) -> Self:
        if self.allow_live and self.require_demo:
            raise ValueError("allow_live=true exige require_demo=false (doble candado)")
        if not self.allow_live and not self.require_demo:
            raise ValueError("si allow_live=false, require_demo debe ser true")
        return self


class AppConfig(Section):
    """Configuración completa del bot."""

    app: AppSection = Field(default_factory=AppSection)
    mt5: Mt5Section = Field(default_factory=Mt5Section)
    risk: RiskSection = Field(default_factory=RiskSection)
    sessions: SessionsSection = Field(default_factory=SessionsSection)
    data: DataSection = Field(default_factory=DataSection)
    regime: RegimeSection = Field(default_factory=RegimeSection)
    playbooks: dict[str, PlaybookConfig] = Field(default_factory=_default_playbooks)
    filters: FiltersSection = Field(default_factory=FiltersSection)
    execution: ExecutionSection = Field(default_factory=ExecutionSection)
    management: ManagementSection = Field(default_factory=ManagementSection)
    dashboard: DashboardSection = Field(default_factory=DashboardSection)
    backtest: BacktestSection = Field(default_factory=BacktestSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    safety: SafetySection = Field(default_factory=SafetySection)

    @model_validator(mode="after")
    def _safety_enabled(self) -> Self:
        if "no_trade" not in self.playbooks:
            raise ValueError("el playbook no_trade es obligatorio")
        return self


class Mt5Credentials(BaseModel):
    """Credenciales opcionales leídas del entorno; nunca se serializan ni se registran."""

    model_config = ConfigDict(frozen=True)

    login: int | None = None
    password: SecretStr | None = None
    server: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.login is not None and self.password is not None and self.server is not None

    @classmethod
    def from_env(cls) -> Mt5Credentials:
        login_raw = os.getenv("MT5_LOGIN", "").strip()
        password = os.getenv("MT5_PASSWORD") or None
        server = os.getenv("MT5_SERVER") or None
        return cls(
            login=int(login_raw) if login_raw else None,
            password=SecretStr(password) if password else None,
            server=server,
        )


def load_config(path: str | Path) -> AppConfig:
    """Carga y valida el YAML de configuración. Lanza FileNotFoundError si no existe."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"la configuración debe ser un mapa YAML: {config_path}")
    return AppConfig.model_validate(raw)


def compute_config_hash(config: AppConfig) -> str:
    """Huella canónica de la configuración, para auditar cada decisión."""
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_environment(dotenv_path: str | Path = ".env") -> None:
    """Carga .env sin sobrescribir variables ya presentes en el entorno."""
    from dotenv import load_dotenv

    load_dotenv(dotenv_path, override=False)
