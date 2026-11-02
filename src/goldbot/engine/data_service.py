"""Servicio de datos de solo lectura (Fase 1): conexión, histórico y snapshots.

No envía órdenes: conecta con MT5, descarga velas cerradas a la caché local, registra
salud, eventos y equity en el StateStore y mantiene `bot_state` al día para el dashboard.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from goldbot import __version__
from goldbot.adapters.mt5.connection import Mt5Connection
from goldbot.adapters.mt5_adapter import Mt5BrokerAdapter
from goldbot.config import AppConfig, Mt5Credentials, compute_config_hash
from goldbot.domain.interfaces import BrokerAdapter, Clock, StateStore
from goldbot.domain.types import (
    AccountState,
    DataStatus,
    EventRecord,
    EventSource,
    EventType,
    LogLevel,
    Regime,
    Timeframe,
)
from goldbot.engine.analysis import AnalysisResult, MarketAnalyzer, regime_to_jsonable
from goldbot.infra import candle_cache
from goldbot.infra.state_store import SqliteStateStore
from goldbot.infra.time import UtcClock, parse_session_windows

LOGGER = logging.getLogger(__name__)

_MAX_BACKOFF_S = 60.0
_LOG_METHODS = {
    LogLevel.DEBUG: "debug",
    LogLevel.INFO: "info",
    LogLevel.WARNING: "warning",
    LogLevel.ERROR: "error",
}


@dataclass(frozen=True, slots=True)
class FeedStatus:
    """Frescura de un timeframe según la última vela cerrada disponible."""

    timeframe: Timeframe
    status: DataStatus
    last_bar_close_utc: datetime | None
    age_s: float | None


@dataclass(frozen=True, slots=True)
class PollResult:
    """Resumen de un ciclo de sondeo; no contiene decisiones de trading."""

    ts_utc: datetime
    connected: bool
    sequence: int
    feed: tuple[FeedStatus, ...]
    overall_status: DataStatus
    tick_age_s: float | None
    new_bars: tuple[tuple[Timeframe, datetime], ...]


@dataclass(frozen=True, slots=True)
class StartupReport:
    requested_symbol: str
    resolved_symbol: str
    spec_hash: str
    account_login: int
    is_demo: bool
    server_offset_min: int
    bars_cached: Mapping[str, int]


def build_clock(config: AppConfig) -> UtcClock:
    """Reloj único del proceso, alineado con la regla horaria del servidor."""
    return UtcClock(
        config.app.server_timezone,
        server_shift=timedelta(hours=config.app.server_shift_hours),
        session_windows=parse_session_windows(
            {
                "ASIA": config.sessions.asia,
                "LONDON": config.sessions.london,
                "NEW_YORK": config.sessions.new_york,
            }
        ),
    )


class DataService:
    """Bucle read-only de la Fase 1: datos fiables y trazabilidad, sin ejecución."""

    def __init__(
        self,
        *,
        config: AppConfig,
        broker: Mt5BrokerAdapter,
        store: StateStore,
        clock: Clock,
        app_version: str = __version__,
        analyzer: MarketAnalyzer | None = None,
    ) -> None:
        self._config = config
        self._broker = broker
        self._store = store
        self._clock = clock
        self._app_version = app_version
        self._analyzer = analyzer
        self._analysis: AnalysisResult | None = None
        self._analysis_disabled = False
        self._last_regime_label: Regime | None = None
        self._config_hash = compute_config_hash(config)
        self._cache_dir = Path(config.app.data_dir) / "cache"
        self._timeframes = tuple(
            dict.fromkeys(
                Timeframe(name)
                for name in (
                    config.data.decision_timeframe,
                    *config.data.entry_timeframes,
                    *config.data.context_timeframes,
                )
            )
        )
        self._warmup = {Timeframe(name): count for name, count in config.data.warmup_bars.items()}
        self._stale_after = {
            Timeframe(name): seconds for name, seconds in config.data.stale_after_s.items()
        }
        self._decision_tf = Timeframe(config.data.decision_timeframe)
        self._sequence = 0
        self._account: AccountState | None = None
        self._resolved_symbol: str | None = None
        self._last_bar_open: dict[Timeframe, datetime] = {}
        self._last_feed: dict[Timeframe, FeedStatus] = {}
        self._last_overall: DataStatus | None = None
        self._was_connected: bool | None = None
        self._started = False

    # -- ciclo de vida -------------------------------------------------------

    @property
    def timeframes(self) -> tuple[Timeframe, ...]:
        """Timeframes configurados, sin duplicados y en orden de decisión."""
        return self._timeframes

    @property
    def broker(self) -> BrokerAdapter:
        """Adaptador de broker activo (para que el `Bot` de la Fase 4 lo reutilice)."""
        return self._broker

    def startup(self, *, download_history: bool = True) -> StartupReport:
        """Conecta, valida la cuenta demo, descarga histórico y publica el estado."""
        self._store.initialize()
        self._store.apply_retention(
            retention_days=self._config.storage.retention_days,
            log_lines_retention=self._config.storage.log_lines_retention,
        )

        health = self._broker.connect()
        if not health.connected:
            self._event(
                EventType.ERROR,
                LogLevel.ERROR,
                f"no se pudo conectar con MT5: {health.last_error}",
            )
            raise RuntimeError(f"MT5 no disponible: {health.last_error}")
        if not health.algo_trading_enabled:
            LOGGER.warning("algo trading deshabilitado en el terminal (solo lectura en Fase 1)")
            self._event(
                EventType.ERROR,
                LogLevel.WARNING,
                "algo trading deshabilitado en el terminal; no se podrá ejecutar en fases posteriores",
            )

        account = self._broker.account()
        self._guard_demo(account)
        self._account = account

        spec = self._broker.symbol_spec(self._config.mt5.symbol)
        self._resolved_symbol = self._broker.resolved_symbol or spec.resolved_name
        if download_history:
            bars = self.download_history()
        else:
            self._prime_from_cache()
            bars = {}

        now = self._clock.now_utc()
        self._snapshot_equity(account, source="STARTUP", now=now)
        self._start_analysis(now)
        self._sequence = 1
        self._write_state(account, state="OPERATIVO", feed_status="OK", now=now)
        self._event(
            EventType.STARTUP,
            LogLevel.INFO,
            f"servicio de datos iniciado sobre {self._resolved_symbol} (solo lectura)",
            {
                "symbol_requested": self._config.mt5.symbol,
                "symbol_resolved": self._resolved_symbol,
                "spec_hash": spec.spec_hash,
                "account_login": account.login,
                "server": account.server,
                "is_demo": account.is_demo,
                "app_version": self._app_version,
                "config_hash": self._config_hash,
            },
        )
        self._started = True
        return StartupReport(
            requested_symbol=self._config.mt5.symbol,
            resolved_symbol=self._resolved_symbol,
            spec_hash=spec.spec_hash,
            account_login=account.login,
            is_demo=account.is_demo,
            server_offset_min=self._server_offset_min(now),
            bars_cached=bars,
        )

    def shutdown(self) -> None:
        """Cierra MT5 y deja constancia en eventos."""
        if self._started:
            self._event(EventType.SHUTDOWN, LogLevel.INFO, "servicio de datos detenido")
        self._broker.shutdown()
        self._started = False

    # -- datos ---------------------------------------------------------------

    def download_history(
        self,
        timeframes: tuple[Timeframe, ...] | None = None,
        *,
        counts: Mapping[Timeframe, int] | None = None,
    ) -> dict[str, int]:
        """Descarga velas cerradas y reescribe la caché de cada timeframe."""
        if self._resolved_symbol is None:
            raise RuntimeError("resuelve el símbolo con symbol_spec() antes de descargar histórico")
        selected = timeframes or self._timeframes
        written: dict[str, int] = {}
        now = self._clock.now_utc()
        for timeframe in selected:
            count = (counts or self._warmup)[timeframe]
            candles = self._broker.get_candles(
                self._config.mt5.symbol, timeframe, count, closed_only=True, as_of_utc=now
            )
            if not candles:
                raise RuntimeError(f"MT5 no devolvió velas cerradas para {timeframe.value}")
            path = candle_cache.cache_path(self._cache_dir, self._resolved_symbol, timeframe)
            written[timeframe.value] = candle_cache.save_candles(path, candles)
            self._last_bar_open[timeframe] = candles[-1].open_time_utc
            self._store.record_history_sync(
                f"candles:{self._resolved_symbol}:{timeframe.value}",
                last_sync_utc=now,
                last_deal_ts_utc=None,
            )
            LOGGER.info(
                "%s %s: %d velas cerradas en caché (última %s)",
                self._resolved_symbol,
                timeframe.value,
                len(candles),
                candles[-1].close_time_utc.isoformat(),
            )
        return written

    def _prime_from_cache(self) -> None:
        """Recupera la última vela cacheada para no re-descargar histórico al arrancar."""
        for timeframe in self._timeframes:
            path = candle_cache.cache_path(self._cache_dir, self._resolved_symbol or "", timeframe)
            try:
                last = candle_cache.last_closed(path)
            except FileNotFoundError:
                continue
            if last is not None:
                self._last_bar_open[timeframe] = last.open_time_utc

    def poll_once(self) -> PollResult:
        """Un ciclo: salud, frescura de velas, tick y snapshot de equity."""
        now = self._clock.now_utc()
        health = self._broker.health()
        if not health.connected:
            if self._was_connected is not False:
                self._event(
                    EventType.ERROR,
                    LogLevel.WARNING,
                    f"MT5 desconectado: {health.last_error}",
                )
            self._was_connected = False
            self._write_state(
                self._account,
                state="RECONNECTING",
                feed_status="STALE",
                now=now,
            )
            return PollResult(
                ts_utc=now,
                connected=False,
                sequence=self._sequence,
                feed=tuple(self._last_feed.values()),
                overall_status=DataStatus.STALE,
                tick_age_s=None,
                new_bars=(),
            )

        account = self._broker.account()
        self._guard_demo(account)
        self._account = account
        self._was_connected = True

        previous = self._last_overall
        feed, new_bars = self._refresh_feed(now)
        tick_age = self._tick_age(now)
        overall = self._overall(feed, tick_age)
        self._last_overall = overall

        self._snapshot_equity(account, source="PERIODIC", now=now)
        if not self._analysis_disabled and self._analyzer is not None:
            if any(timeframe is self._decision_tf for timeframe, _ in new_bars):
                self._run_analysis(now)
        self._sequence += 1
        self._write_state(account, state="OPERATIVO", feed_status=overall.value, now=now)

        if overall != DataStatus.OK and previous != overall:
            self._event(
                EventType.FEED_STALE,
                LogLevel.WARNING,
                f"entradas bloqueadas: {self._gate_reason(feed, tick_age)}",
                {
                    "timeframes": [
                        f"{item.timeframe.value}:{item.status.value}" for item in feed
                    ],
                    "tick_age_s": tick_age,
                },
            )
        return PollResult(
            ts_utc=now,
            connected=True,
            sequence=self._sequence,
            feed=feed,
            overall_status=overall,
            tick_age_s=tick_age,
            new_bars=new_bars,
        )

    def refresh_bars(self, *, now: datetime | None = None) -> DataStatus:
        """Refresca velas y equity, recalcula el análisis, sin publicar estado.

        Lo usa el bucle de ejecución de la Fase 4: ahí el `Bot` es el único que escribe
        `bot_state` (latch del kill switch, pausa, salud del terminal). Si el servicio
        publicara también su estado, borraría el latch y la salud entre las dos escrituras
        del mismo ciclo y el panel mostraría un bot sin candados durante unos milisegundos.
        Aquí solo se aporta la parte de datos; el estado general lo decide el bot.

        El equity no cae dentro de esa exclusión: `equity_snapshots` es histórico, no
        estado vivo. El modo `--execute` se saltaba esta escritura y la única fila del
        proceso era la STARTUP del arranque, así que durante horas el gráfico de 24 h
        mostraba un número congelado y la revisión semanal auditaba `MARGEN_LIBRE` con una
        sola muestra tomada antes de que hubiera posiciones abiertas. Se escribe aquí, con
        la misma cadencia de minuto que `poll_once`.
        """
        moment = now or self._clock.now_utc()
        account = self._broker.account()
        self._guard_demo(account)
        self._account = account
        feed, new_bars = self._refresh_feed(moment)
        overall = self._overall(feed, self._tick_age(moment))
        self._last_overall = overall
        self._snapshot_equity(account, source="PERIODIC", now=moment)
        if not self._analysis_disabled and self._analyzer is not None:
            if any(timeframe is self._decision_tf for timeframe, _ in new_bars):
                self._run_analysis(moment)
        return overall

    def run(
        self,
        *,
        poll_interval_s: float = 5.0,
        max_cycles: int | None = None,
    ) -> None:
        """Bucle de sondeo con reconexión y backoff exponencial."""
        if not self._started:
            self.startup()
        backoff = 1.0
        cycles = 0
        try:
            while max_cycles is None or cycles < max_cycles:
                result = self.poll_once()
                if result.connected:
                    backoff = 1.0
                else:
                    backoff = self._attempt_reconnect(backoff)
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                self._clock.sleep_until(self._clock.now_utc() + timedelta(seconds=poll_interval_s))
        finally:
            self.shutdown()

    # -- internos ------------------------------------------------------------

    def _refresh_feed(
        self, now: datetime
    ) -> tuple[tuple[FeedStatus, ...], tuple[tuple[Timeframe, datetime], ...]]:
        statuses: list[FeedStatus] = []
        new_bars: list[tuple[Timeframe, datetime]] = []
        for timeframe in self._timeframes:
            candles = self._broker.get_candles(
                self._config.mt5.symbol, timeframe, 2, closed_only=True, as_of_utc=now
            )
            if not candles:
                status = FeedStatus(timeframe, DataStatus.MISSING, None, None)
                statuses.append(status)
                self._last_feed[timeframe] = status
                continue
            last = candles[-1]
            age = (now - last.close_time_utc).total_seconds()
            healthy = age <= self._stale_after[timeframe]
            status = FeedStatus(
                timeframe,
                DataStatus.OK if healthy else DataStatus.STALE,
                last.close_time_utc,
                age,
            )
            statuses.append(status)
            self._last_feed[timeframe] = status
            if self._last_bar_open.get(timeframe) != last.open_time_utc:
                self.download_history(timeframes=(timeframe,))
                new_bars.append((timeframe, last.open_time_utc))
        return tuple(statuses), tuple(new_bars)

    def _tick_age(self, now: datetime) -> float | None:
        try:
            tick = self._broker.get_last_tick(self._config.mt5.symbol)
        except RuntimeError as exc:
            LOGGER.debug("sin tick para %s: %s", self._config.mt5.symbol, exc)
            return None
        return (now - tick.time_utc).total_seconds()

    def _overall(self, feed: tuple[FeedStatus, ...], tick_age: float | None) -> DataStatus:
        """Estado global = puerta de entradas: solo el marco de decisión y la edad del tick.

        Los marcos de contexto se siguen midiendo y publicando (`feed`, el bloque de
        frescura del panel), pero NO degradan el estado global. Su última vela cerrada
        queda automáticamente a decenas de horas cada apertura del lunes -el daily no
        cierra hasta la medianoche del servidor- y gatear por ellos habría dejado al bot
        sin entradas válidas durante horas sin que hubiera ningún dato malo.
        """
        decision = self._decision_status(feed)
        if decision is None or decision is DataStatus.MISSING:
            return DataStatus.MISSING
        if decision is DataStatus.STALE:
            return DataStatus.STALE
        if tick_age is None or tick_age > self._config.data.max_feed_age_s:
            return DataStatus.STALE
        return DataStatus.OK

    def _decision_status(self, feed: tuple[FeedStatus, ...]) -> DataStatus | None:
        for item in feed:
            if item.timeframe is self._decision_tf:
                return item.status
        return None

    def _gate_reason(self, feed: tuple[FeedStatus, ...], tick_age: float | None) -> str:
        """Texto del evento FEED_STALE: qué cerró la puerta de entradas."""
        label = self._decision_tf.value
        decision = self._decision_status(feed)
        if decision is None:
            return f"{label} sin medir"
        if decision is not DataStatus.OK:
            return f"{label} en estado {decision.value}"
        if tick_age is None:
            return "sin tick"
        return f"tick atrasado: {tick_age:.0f} s (máximo {self._config.data.max_feed_age_s:.0f} s)"

    def _guard_demo(self, account: AccountState) -> None:
        if self._config.safety.require_demo and not account.is_demo:
            self._event(
                EventType.KILL_SWITCH,
                LogLevel.ERROR,
                "cuenta no DEMO: el servicio se niega a continuar",
                {"account_login": account.login, "server": account.server},
            )
            raise RuntimeError(
                f"cuenta {account.login} de {account.server} no es DEMO; abortando por seguridad"
            )

    def _attempt_reconnect(self, delay: float) -> float:
        LOGGER.warning("reintentando conexión MT5 en %.0f s", delay)
        time.sleep(delay)
        self._broker.shutdown()
        health = self._broker.connect()
        if health.connected:
            self._event(EventType.RECONNECT, LogLevel.INFO, "conexión MT5 restablecida")
            return 1.0
        self._event(
            EventType.ERROR,
            LogLevel.ERROR,
            f"reconexión fallida: {health.last_error}",
        )
        return min(delay * 2.0, _MAX_BACKOFF_S)

    @property
    def analysis(self) -> AnalysisResult | None:
        """Último snapshot de contexto y régimen, o None si el análisis está apagado."""
        return self._analysis

    def _start_analysis(self, now: datetime) -> None:
        if self._analyzer is None or self._analysis_disabled:
            return
        try:
            self._analyzer.warm_up()
        except Exception as exc:  # noqa: BLE001 - el servicio de datos sigue operativo
            self._analysis_disabled = True
            LOGGER.exception("no se pudo entrenar el detector de régimen")
            self._event(
                EventType.ERROR,
                LogLevel.WARNING,
                f"análisis de mercado deshabilitado: {exc}",
                {"stage": "warm_up"},
            )
            return
        self._run_analysis(now)

    def _run_analysis(self, now: datetime) -> None:
        if self._analyzer is None or self._analysis_disabled:
            return
        try:
            result = self._analyzer.analyze(now)
        except Exception as exc:  # noqa: BLE001 - un fallo de análisis no detiene los datos
            self._analysis_disabled = True
            LOGGER.exception("fallo del análisis de mercado")
            self._event(
                EventType.ERROR,
                LogLevel.WARNING,
                f"análisis de mercado deshabilitado: {exc}",
                {"stage": "analyze"},
            )
            return
        self._analysis = result
        previous = self._last_regime_label
        self._last_regime_label = result.regime.label
        if previous is None:
            LOGGER.info(
                "régimen inicial %s (confianza %.2f, %d velas confirmadas)",
                result.regime.label.value,
                result.regime.confidence,
                result.regime.confirmed_bars,
            )
            return
        if result.regime.label is not previous:
            self._event(
                EventType.REGIME_CHANGE,
                LogLevel.INFO,
                f"régimen {previous.value} → {result.regime.label.value}",
                {
                    "from": previous.value,
                    "to": result.regime.label.value,
                    "confidence": result.regime.confidence,
                    "confirmed_bars": result.regime.confirmed_bars,
                    "snapshot_hash": result.snapshot.snapshot_hash,
                    "bar_time_utc": result.snapshot.bar_time_utc.isoformat(),
                },
            )

    def _snapshot_equity(self, account: AccountState, *, source: str, now: datetime) -> None:
        floating = account.equity - account.balance
        self._store.write_equity_snapshot(
            account_login=account.login,
            ts_utc=now,
            balance=account.balance,
            equity=account.equity,
            margin_free=account.margin_free,
            floating_pnl=floating,
            positions_count=self._broker.positions_count(),
            source=source,
        )

    def _write_state(
        self,
        account: AccountState | None,
        *,
        state: str,
        feed_status: str,
        now: datetime,
    ) -> None:
        offset_min = self._server_offset_min(now)
        context = {
            "feed": {
                item.timeframe.value: {
                    "status": item.status.value,
                    "last_bar_close_utc": (
                        item.last_bar_close_utc.isoformat()
                        if item.last_bar_close_utc is not None
                        else None
                    ),
                    "age_s": item.age_s,
                }
                for item in self._last_feed.values()
            },
            "server_offset_min": offset_min,
        }
        regime_payload: dict[str, object] | None = None
        if self._analysis is not None:
            snapshot = self._analysis.snapshot
            context["snapshot"] = snapshot.to_jsonable()
            context["session"] = snapshot.session.value
            context["model"] = (
                {
                    "name": self._analysis.model_info.name,
                    "version": self._analysis.model_info.version,
                    "params_hash": self._analysis.model_info.params_hash,
                }
                if self._analysis.model_info is not None
                else None
            )
            regime_payload = regime_to_jsonable(self._analysis.regime)
        self._store.write_bot_state(
            {
                "state": state,
                "paused": False,
                "mode": "DEMO" if account is None or account.is_demo else "LIVE",
                "requested_symbol": self._config.mt5.symbol,
                "resolved_symbol": self._resolved_symbol,
                "magic": self._config.app.magic,
                "account": asdict(account) if account is not None else None,
                "context": context,
                "regime": regime_payload,
                "playbook": None,
                "filters": None,
                "feed_status": feed_status,
                "last_cycle_at_utc": now,
                "cycle_seq": self._sequence,
                "last_heartbeat_utc": now,
                "kill_switch_latched": False,
                "kill_reason": None,
                "config_hash": self._config_hash,
                "app_version": self._app_version,
                "server_offset_min": offset_min,
            }
        )

    def _server_offset_min(self, now: datetime) -> int:
        return int(self._clock.server_offset(now).total_seconds() // 60)

    def _event(
        self,
        event_type: EventType,
        level: LogLevel,
        message: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        self._store.write_event(
            EventRecord(
                ts_utc=self._clock.now_utc(),
                source=EventSource.BOT,
                type=event_type,
                level=level,
                message=message,
                payload=payload,
            )
        )
        getattr(LOGGER, _LOG_METHODS.get(level, "info"))("%s", message)


def build_broker(config: AppConfig, clock: Clock) -> Mt5BrokerAdapter:
    """Adaptador MT5 real con el magic y el filling de la config.

    Separado del servicio porque la prueba de la ruta de orden (`scripts/live_round_trip.py`)
    necesita el mismo adaptador sin arrancar el bucle de datos ni entrenar el régimen.
    """
    connection = Mt5Connection(
        clock,
        config.mt5.symbol,
        terminal_path=config.mt5.terminal_path,
        timeout_ms=config.mt5.timeout_ms,
        credentials=Mt5Credentials.from_env(),
    )
    return Mt5BrokerAdapter(
        connection,
        clock,
        deviation_points=config.execution.deviation_points,
        symbol_override=config.mt5.symbol_override,
        filling_policy=config.execution.filling_policy,
        # `closed_deals` filtra por el magic del adaptador: con el 0 por defecto la
        # reconciliación no vería ningún deal propio.
        magic=config.app.magic,
        symbol=config.mt5.symbol,
    )


def build_data_service(
    config: AppConfig,
    *,
    clock: Clock | None = None,
    store: StateStore | None = None,
    broker: Mt5BrokerAdapter | None = None,
    app_version: str = __version__,
    enable_analysis: bool = True,
) -> DataService:
    """Compone el servicio real; permite inyectar dobles en tests."""
    resolved_clock = clock or build_clock(config)
    resolved_store = store or SqliteStateStore(config.app.db_path, resolved_clock)
    resolved_broker = broker or build_broker(config, resolved_clock)
    analyzer = (
        MarketAnalyzer(config=config, broker=resolved_broker, clock=resolved_clock)
        if enable_analysis
        else None
    )
    return DataService(
        config=config,
        broker=resolved_broker,
        store=resolved_store,
        clock=resolved_clock,
        app_version=app_version,
        analyzer=analyzer,
    )
