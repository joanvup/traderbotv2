"""Motor de backtest: misma cadena de decisión que el bot, ejecución conservadora.

Reglas de simulación (Fase 3):
- Se decide al cierre de cada vela de decisión y se entra en la apertura siguiente,
  pagando spread (el de la vela de señal) y slippage.
- Si en una misma vela se tocan stop y objetivo, se asume primero el stop.
- Una sola posición a la vez; sin promediar, sin grid y sin aumentar lote.
- El lote es fijo (0,01) para medir en R; la gestión fina llega en la Fase 4.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from goldbot.backtest.broker_sim import SimulatedAccount, default_symbol_spec
from goldbot.backtest.clock import SimulatedClock
from goldbot.backtest.feed import HistoricalFeed
from goldbot.backtest.metrics import BacktestResult, compute_metrics
from goldbot.config import AppConfig, compute_config_hash
from goldbot.domain.context import ContextBuilder
from goldbot.domain.decision_core import DecisionCore
from goldbot.domain.filters import InMemoryDailyLedger
from goldbot.domain.interfaces import EconomicCalendar
from goldbot.domain.playbooks.base import BasePlaybook
from goldbot.domain.regime import create_regime_detector
from goldbot.domain.regime.base import BaseRegimeDetector
from goldbot.domain.regime.features import compute_features
from goldbot.domain.types import (
    AccountState,
    Candle,
    DecisionRecord,
    ExitReason,
    ExitReasonSource,
    PolicyDecision,
    RegimeState,
    Side,
    Signal,
    SymbolSpec,
    Timeframe,
    TradeRecord,
)
from goldbot.engine.analysis import build_context_settings, build_regime_settings
from goldbot.engine.decisions import build_decision_core
from goldbot.infra.time import parse_session_windows


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Costes y supuestos de simulación; se rellenan desde la sección `backtest`."""

    initial_balance: float = 1_000.0
    commission_per_lot_side_usd: float = 3.5
    slippage_points: int = 2
    volume: float = 0.01

    @classmethod
    def from_app(cls, config: AppConfig) -> BacktestConfig:
        return cls(
            initial_balance=config.backtest.initial_balance,
            commission_per_lot_side_usd=config.backtest.commission_per_lot_side_usd,
            slippage_points=config.backtest.slippage_points,
        )


@dataclass
class _OpenPosition:
    signal: Signal
    entry_price: float
    volume: float
    risk_money: float
    opened_at_utc: datetime
    bars_held: int = 0

    @property
    def side(self) -> Side:
        return self.signal.side

    def floating(self, price: float, contract_size: float) -> float:
        direction = 1.0 if self.side is Side.BUY else -1.0
        return (price - self.entry_price) * direction * contract_size * self.volume


@dataclass
class _PendingEntry:
    signal: Signal
    spread_points: float


class BacktestEngine:
    """Ejecuta la cadena selectora → playbook → filtros sobre velas históricas."""

    def __init__(
        self,
        *,
        config: AppConfig,
        candles: Mapping[Timeframe, Sequence[Candle]],
        calendar: EconomicCalendar | None,
        train_end_utc: datetime,
        backtest: BacktestConfig | None = None,
        detector: BaseRegimeDetector | None = None,
        playbooks: Mapping[str, BasePlaybook] | None = None,
        spec: SymbolSpec | None = None,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
        decision_logger: Any | None = None,
        session_windows: Mapping[Any, Sequence[tuple[Any, Any]]] | None = None,
    ) -> None:
        self._config = config
        self._calendar = calendar
        self._train_end = train_end_utc
        self._limits = {Timeframe(name): bars for name, bars in config.data.warmup_bars.items()}
        self._decision_timeframe = Timeframe(config.data.decision_timeframe)
        self._feed = HistoricalFeed(
            candles, decision_timeframe=self._decision_timeframe, limits=self._limits
        )
        self._clock = SimulatedClock(
            start_utc or self._feed.first_close_utc(),
            server_zone=config.app.server_timezone,
            server_shift=timedelta(hours=config.app.server_shift_hours),
            session_windows=session_windows
            or parse_session_windows(
                {
                    "ASIA": config.sessions.asia,
                    "LONDON": config.sessions.london,
                    "NEW_YORK": config.sessions.new_york,
                }
            ),
        )
        self._start = start_utc
        self._end = end_utc
        self._backtest = backtest or BacktestConfig.from_app(config)
        self._spec = spec or default_symbol_spec(symbol=config.mt5.symbol)
        self._detector = detector or create_regime_detector(build_regime_settings(config))
        self._builder = ContextBuilder(
            build_context_settings(config),
            stale_after_s={
                Timeframe(name): float(seconds)
                for name, seconds in config.data.stale_after_s.items()
            },
        )
        self._ledger = InMemoryDailyLedger()
        self._core: DecisionCore = build_decision_core(
            config,
            playbooks=playbooks,
            calendar=calendar,
            clock=self._clock,
            daily=self._ledger,
        )
        self._account = SimulatedAccount(balance=self._backtest.initial_balance)
        self._config_hash = compute_config_hash(config)
        self._logger = decision_logger
        self._states: dict[datetime, RegimeState] = {}

    # -- ciclo ----------------------------------------------------------------

    def run(self) -> BacktestResult:
        """Entrena hasta `train_end_utc`, prueba después y devuelve resultados auditables."""
        self._prepare_model()
        bars = self._test_bars()
        if not bars:
            raise ValueError("no hay velas de prueba después de train_end_utc")

        cash = self._backtest.initial_balance
        pending: _PendingEntry | None = None
        position: _OpenPosition | None = None
        trades: list[TradeRecord] = []
        equity_curve: list[tuple[datetime, float]] = []
        policy_counts: Counter[str] = Counter()
        block_counts: Counter[str] = Counter()
        regime_counts: Counter[str] = Counter()
        playbook_counts: Counter[str] = Counter()
        decision_days: set[Any] = set()
        position_seq = 0

        contract = self._spec.contract_size
        point = self._spec.point
        slippage = self._backtest.slippage_points * point
        time_stop_bars = self._config.management.time_stop_bars

        for bar in bars:
            self._clock.advance_to(bar.close_time_utc)
            trading_day = self._clock.trading_day(bar.close_time_utc)
            decision_days.add(trading_day)

            # 1) Entrada pendiente: se ejecuta en la apertura de esta vela.
            if pending is not None and position is None:
                position = self._open(pending, bar, slippage, point)
                if position is not None:
                    self._ledger.record_entry(trading_day)
                pending = None

            # 2) Salidas de la posición abierta dentro de esta vela.
            if position is not None:
                position.bars_held += 1
                exit_price, reason = self._exit_price(bar, position, slippage)
                if exit_price is not None:
                    trade, net = self._close(
                        position, exit_price, bar.close_time_utc, reason, position_seq
                    )
                    position_seq += 1
                    cash += net
                    trades.append(trade)
                    self._ledger.record_pnl(trading_day, net)
                    position = None
                elif position.bars_held >= time_stop_bars:
                    exit_price = (
                        bar.close - slippage
                        if position.side is Side.BUY
                        else bar.close + slippage
                    )
                    trade, net = self._close(
                        position, exit_price, bar.close_time_utc, ExitReason.TIME, position_seq
                    )
                    position_seq += 1
                    cash += net
                    trades.append(trade)
                    self._ledger.record_pnl(trading_day, net)
                    position = None

            equity = cash + (
                position.floating(bar.close, contract) if position is not None else 0.0
            )

            # 3) Decisión al cierre de la vela si no hay posición ni entrada pendiente.
            if position is None and pending is None:
                regime = self._regime_for(bar)
                if regime is None:
                    policy_counts["NO_DATA"] += 1
                else:
                    regime_counts[regime.label.value] += 1
                    decision = self._decide(bar, regime, equity, trading_day)
                    policy_counts[decision.action] += 1
                    playbook_counts[decision.selection.playbook_id] += 1
                    if decision.filters is not None:
                        for result in decision.filters.results:
                            if result.blocked:
                                block_counts[result.code.value] += 1
                    if decision.action == "ENTER" and decision.signal is not None:
                        pending = _PendingEntry(
                            signal=decision.signal, spread_points=bar.spread_points
                        )

            equity_curve.append((bar.close_time_utc, equity))

        metrics = compute_metrics(
            trades,
            initial_balance=self._backtest.initial_balance,
            total_days=len(decision_days),
        )
        return BacktestResult(
            metrics=metrics,
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            decisions=sum(policy_counts.values()),
            policy_counts=dict(policy_counts),
            block_counts=dict(block_counts),
            regime_counts=dict(regime_counts),
            playbook_counts=dict(playbook_counts),
            start_utc=bars[0].close_time_utc,
            end_utc=bars[-1].close_time_utc,
            model_version=self._detector.model_info.version
            if self._detector.model_info is not None
            else "",
            decision_days=len(decision_days),
        )

    # -- preparación ----------------------------------------------------------

    def _prepare_model(self) -> None:
        settings = build_regime_settings(self._config)
        features = compute_features(self._feed.decision_bars, settings)
        if self._train_end.tzinfo is None:
            raise ValueError("train_end_utc debe incluir zona horaria")
        # Igual que en vivo: el detector solo ve la ventana de entrenamiento, no todo el histórico.
        history_bars = max(
            self._config.data.warmup_bars.get(self._decision_timeframe.value, 0),
            settings.train_window_bars + settings.atr_percentile_window + 200,
        )
        window = features[features.index <= self._train_end].tail(history_bars)
        if window.empty:
            raise ValueError("no hay filas de entrenamiento antes de train_end_utc")
        self._detector.fit(window, train_end_utc=self._train_end)
        self._states = {
            state.decided_at_utc: state
            for state in self._detector.predict_batch(features)
        }

    def _test_bars(self) -> list[Candle]:
        start = self._start
        end = self._end
        return [
            bar
            for bar in self._feed.decision_bars
            if bar.open_time_utc > self._train_end
            and (start is None or bar.close_time_utc >= start)
            and (end is None or bar.close_time_utc < end)
        ]

    # -- decisiones -----------------------------------------------------------

    def _regime_for(self, bar: Candle) -> RegimeState | None:
        return self._states.get(bar.open_time_utc)

    def _decide(
        self,
        bar: Candle,
        regime: RegimeState,
        equity: float,
        trading_day: Any,
    ) -> PolicyDecision:
        context = self._builder.build(
            self._feed.slice_at(bar.close_time_utc),
            now_utc=bar.close_time_utc,
            decision_timeframe=self._decision_timeframe,
            session=self._clock.session_at(bar.close_time_utc),
            config_hash=self._config_hash,
            model_versions=(
                {self._detector.model_info.name: self._detector.model_info.version}
                if self._detector.model_info is not None
                else {}
            ),
        )
        self._ledger.ensure_day(trading_day, equity)
        account: AccountState = self._account.state(equity=equity, margin_free=equity)
        decision = self._core.decide(context, regime, account, self._spec)
        if self._logger is not None:
            self._logger.log(
                DecisionRecord(
                    decision_id=_decision_id(context.snapshot_hash, decision),
                    cycle_id=f"bt-{bar.open_time_utc.isoformat()}",
                    ts_utc=bar.close_time_utc,
                    bar_time_utc=bar.open_time_utc,
                    action=decision.action,
                    snapshot=context,
                    regime=regime,
                    playbook_id=decision.selection.playbook_id,
                    signal=decision.signal,
                    filters=decision.filters,
                    risk=None,
                    rationale=decision.rationale,
                    latency_ms=0,
                    config_hash=self._config_hash,
                    model_versions=context.model_versions,
                )
            )
        return decision

    # -- ejecución simulada ---------------------------------------------------

    def _open(
        self,
        pending: _PendingEntry,
        bar: Candle,
        slippage: float,
        point: float,
    ) -> _OpenPosition | None:
        signal = pending.signal
        spread = pending.spread_points * point
        if signal.side is Side.BUY:
            fill = bar.open + spread + slippage
            if fill <= signal.stop_loss or (
                signal.take_profit is not None and fill >= signal.take_profit
            ):
                return None
        else:
            fill = bar.open - slippage
            if fill >= signal.stop_loss or (
                signal.take_profit is not None and fill <= signal.take_profit
            ):
                return None
        risk_money = abs(fill - signal.stop_loss) * self._spec.contract_size * self._backtest.volume
        if risk_money <= 0.0:
            return None
        return _OpenPosition(
            signal=signal,
            entry_price=fill,
            volume=self._backtest.volume,
            risk_money=risk_money,
            opened_at_utc=bar.open_time_utc,
        )

    def _exit_price(
        self, bar: Candle, position: _OpenPosition, slippage: float
    ) -> tuple[float | None, ExitReason]:
        signal = position.signal
        if position.side is Side.BUY:
            if bar.low <= signal.stop_loss:
                return signal.stop_loss - slippage, ExitReason.SL
            if signal.take_profit is not None and bar.high >= signal.take_profit:
                return signal.take_profit, ExitReason.TP
        else:
            if bar.high >= signal.stop_loss:
                return signal.stop_loss + slippage, ExitReason.SL
            if signal.take_profit is not None and bar.low <= signal.take_profit:
                return signal.take_profit, ExitReason.TP
        return None, ExitReason.TIME

    def _close(
        self,
        position: _OpenPosition,
        exit_price: float,
        close_time_utc: datetime,
        reason: ExitReason,
        position_seq: int,
    ) -> tuple[TradeRecord, float]:
        direction = 1.0 if position.side is Side.BUY else -1.0
        gross = (
            (exit_price - position.entry_price)
            * direction
            * self._spec.contract_size
            * position.volume
        )
        commission = 2.0 * self._backtest.commission_per_lot_side_usd * position.volume
        net = gross - commission
        r_multiple = net / position.risk_money if position.risk_money > 0.0 else None
        source = (
            ExitReasonSource(reason.value)
            if reason in (ExitReason.SL, ExitReason.TP)
            else ExitReasonSource.BOT
        )
        trade = TradeRecord(
            position_id=position_seq + 1,
            symbol=self._spec.resolved_name,
            side=position.side,
            volume=position.volume,
            entry_price=position.entry_price,
            exit_price=exit_price,
            sl=position.signal.stop_loss,
            tp=position.signal.take_profit,
            open_time_utc=position.opened_at_utc,
            close_time_utc=close_time_utc,
            entry_deal_id=None,
            close_deal_id=None,
            commission=commission,
            swap=0.0,
            net_profit=net,
            r_multiple=r_multiple,
            duration_s=int((close_time_utc - position.opened_at_utc).total_seconds()),
            exit_reason=reason,
            exit_reason_source=source,
            playbook_id=position.signal.playbook_id,
            regime_at_entry=position.signal.regime_at_signal,
            decision_id_entry=position.signal.signal_id,
        )
        return trade, net


def _decision_id(snapshot_hash: str, decision: PolicyDecision) -> str:
    payload = (
        f"{snapshot_hash}|{decision.action}|"
        f"{decision.signal.signal_id if decision.signal else decision.selection.playbook_id}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
