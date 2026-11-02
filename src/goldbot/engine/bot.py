"""Bucle principal del bot (Fase 4): la misma secuencia de ciclo sirve en vivo y en tests.

El `Bot` orquesta la cadena completa sobre el último análisis del mercado:

    contexto+régimen → DecisionCore (selector→playbook→filtros)
                     → ExecutionEngine (riesgo→validación→envío→verificación SL/TP)
                     → TradeManager (breakeven/parciales/trailing/time-stop/régimen)
                     → KillSwitch (latch de emergencia)

El análisis (contexto + régimen) lo produce un componente externo (el `MarketAnalyzer`
de la Fase 2, que ya corre dentro del `DataService`). Para que el `Bot` sea comprobable
en tests sin terminal, el constructor acepta un `analysis_provider`: un callable que
devuelve `AnalysisResult | None` (el último snapshot + régimen). En vivo se le pasa
`data_service.analysis`; en tests, una lambda con un snapshot fijo.

Reglas duras que se aplican AQUÍ (no en los filtros, porque son del ciclo):
- `MAX_TRADES` y `DAILY_LOSS` los revisa la cadena de filtros, pero el bloqueo diario
  persistido (`daily_lock_latched`) solo se consulta aquí: sobrevive a un reinicio.
- Nunca se abre una segunda posición: si hay posición propia, el ciclo es de gestión.
- Un fallo leyendo cuenta/tick/posiciones NO puede tirar el proceso: se registra, se
  aplica backoff creciente y no se abren operaciones hasta recuperar datos fiables.
- El PnL realizado lo manda la reconciliación de deals, no el PnL flotante: un SL que
  salta en el broker sin que el bot lo vea sigue siendo pérdida del día.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from typing import Any

from goldbot import __version__
from goldbot.config import compute_config_hash
from goldbot.domain.decision_core import DecisionCore
from goldbot.domain.events import BarEvent, CommandEvent, MarketEvent, TimerEvent
from goldbot.domain.interfaces import (
    BrokerAdapter,
    Clock,
    DecisionLogger,
    StateStore,
    TradeManager,
)
from goldbot.domain.types import (
    AccountState,
    BrokerHealth,
    CommandRecord,
    CommandStatus,
    CommandType,
    ContextSnapshot,
    CycleResult,
    DataStatus,
    DecisionRecord,
    EventRecord,
    EventSource,
    EventType,
    ExitReason,
    ExitReasonSource,
    KillSwitchReason,
    KillSwitchState,
    LogLevel,
    ManageAction,
    ManageKind,
    PolicyDecision,
    PositionState,
    RegimeState,
    Side,
    StoredPosition,
    SymbolSpec,
    TradeRecord,
)
from goldbot.engine.analysis import regime_to_jsonable
from goldbot.engine.guards import GuardViolation, assert_feed_fresh
from goldbot.engine.kill_switch import LatchedKillSwitch

LOGGER = logging.getLogger(__name__)

# Backoff del enlace con el broker: espera creciente ante fallos de lectura/envío.
_BACKOFF_STEPS_S = (1.0, 2.0, 5.0, 10.0, 20.0, 60.0)


class Bot:
    """Orquesta contexto → régimen → selección → filtros → riesgo → gestión.

    Constructor:
        analysis_provider: callable que devuelve el último `AnalysisResult | None`
            (contexto + régimen) del motor de datos.
        core: cadena de decisión (selector → playbook → filtros).
        execution: capa de ejecución (riesgo → validación → envío → verificación).
        broker/store/clock/...: piezas compartidas.
        magic: magic number de las órdenes propias.
        daily: ledger diario (en vivo `SqliteDailyLedger`, en tests el de memoria).
    """

    def __init__(
        self,
        *,
        config: Any,
        broker: BrokerAdapter,
        store: StateStore,
        clock: Clock,
        trade_manager: TradeManager,
        kill_switch: LatchedKillSwitch,
        core: DecisionCore,
        execution: Any,
        analysis_provider: Callable[[], Any],
        daily: Any,
        magic: int,
        symbol: str,
        spec: SymbolSpec,
        decision_logger: DecisionLogger | None = None,
        data_poll: Callable[[], DataStatus] | None = None,
    ) -> None:
        self._config = config
        self._broker = broker
        self._store = store
        self._clock = clock
        self._trade_manager = trade_manager
        self._kill_switch = kill_switch
        self._core = core
        self._execution = execution
        self._analysis_provider = analysis_provider
        self._daily = daily
        self._magic = magic
        self._symbol = symbol
        self._spec = spec
        self._decision_logger = decision_logger
        # Inyeccion del servicio de datos en el bucle en vivo: sin ella `run_forever` leeria
        # siempre el snapshot con el que arranco el proceso (no hay velas nuevas que decidan).
        self._data_poll = data_poll
        # Estado del enlace segun la ultima lectura de velas; el panel no puede pintar "OK"
        # porque el ciclo de salud lo escriba, tiene que reflejar la antiguedad real.
        self._feed_status = "OK"
        self._config_hash = compute_config_hash(config)
        self._paused = False
        self._cycle_seq = 0
        self._kill_state: KillSwitchState = kill_switch.state
        # Última salud leída del broker: el dashboard la publica como chip "algo trading"
        # y "enlace"; si no hay lectura nueva, muestra la antigüedad en vez de inventarla.
        self._last_health: BrokerHealth | None = None
        # Última decisión ENTER ya ejecutada (evita re-abrir en el mismo snapshot).
        self._executed_decision_id: str | None = None
        # Backoff de datos: nº de lecturas fallidas seguidas y cuándo se reintenta.
        self._data_errors = 0
        self._retry_after_utc: datetime | None = None

    @property
    def paused(self) -> bool:
        return self._paused

    # -- arranque ------------------------------------------------------------

    def restore_kill_latch(self) -> None:
        """Re-latchea el kill switch guardado en el StateStore tras un reinicio.

        El latch es persistente por diseño: si el proceso se mató con la emergencia
        activada, el reinicio NO puede volver a operar hasta un reset manual del
        operador (comando con token desde el dashboard).
        """
        state = self._store.read_bot_state()
        if state is None or not bool(state.get("kill_switch_latched")):
            return
        raw_reason = str(state.get("kill_reason") or "")
        reason = next(
            (item for item in KillSwitchReason if item.value == raw_reason),
            KillSwitchReason.MANUAL,
        )
        self._kill_state = self._kill_switch.force_latch(
            reason,
            f"latch restaurado del estado persistido ({raw_reason or 'motivo desconocido'})",
            now_utc=self._clock.now_utc(),
        )
        self._event(
            EventType.KILL_SWITCH,
            LogLevel.WARNING,
            "kill switch restaurado del estado persistido: hace falta reset manual",
            {"reason": reason.value},
        )

    # -- ciclo ---------------------------------------------------------------

    def step(self, event: MarketEvent) -> CycleResult:
        """Procesa un evento (barra de decisión, comando o timer) y devuelve el resultado.

        Reglas del ciclo:
        - Los comandos (pausa/reanudar/reset/cerrar todo) se procesan en cualquier evento.
        - Con una barra de decisión cerrada y SIN posición abierta: decide y ejecuta.
        - Con una posición abierta: gestiona (TradeManager) en cada barra.
        - Un `TimerEvent` solo revisa salud/kill switch y publica estado.
        """
        if isinstance(event, CommandEvent):
            self._cycle_seq += 1
            self._handle_command(event.command)
            return _result(f"c{self._cycle_seq}", "COMMAND", ("command",))

        if isinstance(event, BarEvent):
            return self.run_cycle(self._analysis_provider())

        self._cycle_seq += 1
        self._health_cycle(f"c{self._cycle_seq}")
        return _result(f"c{self._cycle_seq}", "HEALTH", ("health",))

    def run_cycle(self, analysis: Any) -> CycleResult:
        """Un ciclo completo sobre el último análisis: reconciliar → gestionar → decidir."""
        self._cycle_seq += 1
        cycle_id = f"c{self._cycle_seq}"
        if analysis is None:
            return _result(cycle_id, "NO_DATA", ())

        snapshot: ContextSnapshot = analysis.snapshot
        regime: RegimeState = analysis.regime
        now = self._clock.now_utc()
        trading_day = self._clock.trading_day(snapshot.decision_time_utc)

        # 1) Lecturas del broker. Un fallo aquí NO lanza: es exactamente el caso
        #    "se cae el enlace", que debe registrar, esperar con backoff y no operar.
        try:
            account = self._broker.account()
            positions = self._read_positions()
            health = self._broker.health()
        except Exception as exc:
            return self._note_data_failure(cycle_id, exc, now)
        self._last_health = health
        self._note_data_ok()

        # 2) El día operativo necesita su fila con el equity de arranque para que el
        #    filtro de pérdida diaria tenga referencia (ensure_day no pisa la existente).
        #    El cierre se reescribe en cada ciclo: cuando el día deja de ser hoy, la última
        #    escritura queda congelada y ese es exactamente su equity de cierre. Sin esta
        #    línea `daily_stats.equity_end` es NULL para siempre, el dashboard pinta el día
        #    sin cierre y el auditor EQUITY_FLOOR descarta la fila por pedir `equity_end`
        #    no nulo: el suelo de equity nunca se contrastaba día a día.
        self._daily.ensure_day(trading_day, account.equity)
        self._daily.record_equity_end(trading_day, account.equity)

        # 3) Kill switch: prioridad sobre todo lo demás.
        self._sync_kill_state(
            self._kill_switch.evaluate(
                health=health,
                account=account,
                freshness=snapshot.freshness,
                decision_timeframe=snapshot.decision_timeframe,
            )
        )

        # 4) Reconciliación de deals: posiciones que ya no existen en el broker
        #    (SL/TP/externo) se cierran en el ledger ANTES de decidir nada. Devuelve los
        #    tickets desaparecidos para que la gestión no intente operar sobre una
        #    posición que ya no existe (un modify rechazaría y contaría como error de
        #    ejecución, y tres errores seguidos latchean el kill switch).
        gone = self._reconcile_closed(positions, trading_day)
        positions = [pos for pos in positions if pos.ticket not in gone]

        # 5) Con posición propia: solo gestión (nunca una segunda entrada). Va ANTES
        #    de los candados de entrada: bloquear el día no puede dejar una operación
        #    viva sin mover el stop a su favor.
        if positions:
            self._manage_positions(positions, snapshot, regime)
            self._write_state(snapshot, regime, account, self._bot_state_str(), self._feed_status)
            return _result(cycle_id, "MANAGE", ("manage",))

        # 6) Bloqueo diario persistido: no se abren operaciones nuevas hoy.
        if self._daily.is_daily_locked(trading_day):
            self._write_state(snapshot, regime, account, "DAILY_LOCK", feed_status=self._feed_status)
            return _result(cycle_id, "DAILY_LOCK", ("daily_lock",))

        # 7) Un kill latcheado bloquea entradas nuevas (la gestión de lo abierto ya se
        #    hizo arriba: seguir moviendo el SL a favor es estrictamente defensivo).
        if self._kill_state.latched:
            self._write_state(snapshot, regime, account, "KILLED", feed_status=self._feed_status)
            return _result(
                cycle_id, "KILLED", ("kill",), error=self._kill_state.message
            )

        if self._paused:
            self._write_state(snapshot, regime, account, "PAUSADO", feed_status=self._feed_status)
            return _result(cycle_id, "PAUSED", ("paused",))

        # 8) Decisión y ejecución.
        decision = self._core.decide(snapshot, regime, account, self._spec)
        decision_id = _decision_id(snapshot, decision)
        if decision.action == "ENTER" and decision.signal is not None:
            if self._executed_decision_id == decision_id:
                return _result(cycle_id, "ENTER", ("already_executed",), decision_id=decision_id)
            return self._execute(decision, decision_id, snapshot, regime, account, cycle_id)

        self._log_decision(decision, snapshot, regime, decision_id)
        self._write_state(snapshot, regime, account, self._bot_state_str(), self._feed_status)
        return _result(cycle_id, decision.action, ("decide",), decision_id=decision_id)

    # -- entrada -------------------------------------------------------------

    def _execute(
        self,
        decision: PolicyDecision,
        decision_id: str,
        snapshot: ContextSnapshot,
        regime: RegimeState,
        account: AccountState,
        cycle_id: str,
    ) -> CycleResult:
        """Ejecuta la señal y registra la entrada solo si el broker la confirmó."""
        try:
            tick = self._broker.get_last_tick(self._symbol)
        except Exception as exc:
            return self._note_data_failure(cycle_id, exc, self._clock.now_utc())

        # Puerta de frescura de la entrada: el marco sobre el que se decidió y la edad del
        # tick con el que se va a llenar. Rechazar aquí NO es un error de ejecución (no se
        # envió ninguna orden, no cuenta para el latch de errores) ni activa el backoff.
        try:
            assert_feed_fresh(
                snapshot.freshness,
                decision_timeframe=snapshot.decision_timeframe,
                tick_age_s=(self._clock.now_utc() - tick.time_utc).total_seconds(),
                max_tick_age_s=self._config.data.max_feed_age_s,
            )
        except GuardViolation as exc:
            self._log_decision(decision, snapshot, regime, decision_id)
            self._event(
                EventType.FEED_STALE,
                LogLevel.WARNING,
                f"entrada descartada: {exc}",
                {"decision_id": decision_id, "bar_time_utc": snapshot.bar_time_utc.isoformat()},
            )
            self._write_state(
                snapshot, regime, account, self._bot_state_str(), feed_status=DataStatus.STALE.value
            )
            return _result(
                cycle_id, "NO_TRADE", ("feed_stale",), decision_id=decision_id, error=str(exc)
            )

        result = self._execution.execute_signal(
            decision,
            snapshot,
            account,
            self._spec,
            tick,
            magic=self._magic,
            decision_id=decision_id,
        )
        self._kill_switch.note_execution_result(result.placed, now_utc=self._clock.now_utc())
        signal = decision.signal
        order_result = result.result
        self._store.record_order_attempt(
            ts_utc=self._clock.now_utc(),
            decision_id=decision_id,
            request=asdict(result.request) if result.request is not None else {},
            retcode=None if order_result is None else order_result.retcode,
            retcode_name=None if order_result is None else order_result.retcode_name,
            retryable=None if order_result is None else order_result.retryable,
            result=asdict(order_result) if order_result is not None else None,
        )
        self._log_decision(decision, snapshot, regime, decision_id, risk=result.risk)

        if not result.placed:
            self._write_state(snapshot, regime, account, self._bot_state_str(), self._feed_status)
            return _result(
                cycle_id, "ENTER", ("entry_failed",), decision_id=decision_id, error=result.message
            )

        self._executed_decision_id = decision_id
        self._daily.record_entry(self._clock.trading_day(snapshot.decision_time_utc))
        self._register_opened_position(decision_id)
        self._event(
            EventType.ENTRY,
            LogLevel.INFO,
            f"entrada ejecutada: {signal.side.value if signal else ''}",
            {"decision_id": decision_id, "lots": result.risk.lots if result.risk else None},
        )
        self._write_state(snapshot, regime, account, self._bot_state_str(), self._feed_status)
        return _result(cycle_id, "ENTER", ("entry",), decision_id=decision_id)

    def _register_opened_position(self, decision_id: str) -> None:
        """Guarda la foto de la posición recién abierta para poder reconciliarla después."""
        try:
            for pos in self._broker.positions(self._magic):
                if pos.decision_id_entry == decision_id or pos.decision_id_entry is None:
                    self._store.upsert_position(replace(pos, decision_id_entry=decision_id))
        except Exception:
            LOGGER.exception("no se pudo registrar la posición abierta")

    # -- gestión -------------------------------------------------------------

    def _read_positions(self) -> list[PositionState]:
        """Posiciones propias hidratadas con el estado de gestión persistido.

        El broker no sabe si ESTE bot ya hizo el breakeven o un parcial; sin hidratarlo
        desde el StateStore, un reinicio repetiría la acción sobre la misma operación.
        """
        stored = self._store.read_open_positions()
        hydrated: list[PositionState] = []
        for pos in self._broker.positions(self._magic):
            info = stored.get(pos.ticket)
            breakeven = bool(info.breakeven_done) if info else False
            partials = int(info.partials_done) if info else 0
            playbook = pos.playbook_id or (info.playbook_id if info else "") or ""
            inicial = _riesgo_inicial(pos, info)
            hydrated.append(
                replace(
                    pos,
                    playbook_id=playbook,
                    regime_at_entry=pos.regime_at_entry or (info.regime_at_entry if info else None),
                    decision_id_entry=pos.decision_id_entry
                    or (info.decision_id_entry if info else None),
                    initial_risk_price=inicial,
                    r_multiple=_r_desde(pos, inicial),
                    opened_at_utc=pos.opened_at_utc or (info.opened_at_utc if info else pos.opened_at_utc),
                    breakeven_done=breakeven or _sl_en_breakeven(pos),
                    partials_done=partials,
                )
            )
        return hydrated

    def _manage_positions(
        self,
        positions: list[PositionState],
        snapshot: ContextSnapshot,
        regime: RegimeState,
    ) -> None:
        for pos in positions:
            current: PositionState | None = pos
            for action in self._trade_manager.on_bar(pos, snapshot, regime):
                if action.kind is ManageKind.HOLD or current is None:
                    continue
                current = self._apply_action(current, action)

    def _apply_action(
        self,
        pos: PositionState,
        action: ManageAction,
    ) -> PositionState | None:
        """Ejecuta una acción de gestión en el broker y persiste la nueva foto.

        Devuelve la posición actualizada (para que las siguientes acciones de la misma
        vela vean el estado real) o None si la posición ya no existe o el broker falló.
        """
        if action.kind is ManageKind.CLOSE:
            result = self._broker.close_position(pos.ticket, volume=pos.volume)
            if not result.filled:
                self._event(
                    EventType.ERROR,
                    LogLevel.ERROR,
                    f"cierre rechazado por el broker: {result.retcode_name} ({result.retcode})",
                    {"ticket": pos.ticket, "reason": action.reason},
                )
                self._kill_switch.note_execution_result(False, now_utc=self._clock.now_utc())
                return None
            self._kill_switch.note_execution_result(True, now_utc=self._clock.now_utc())
            # NO borramos la fila aquí: la reconciliación del ciclo siguiente lee los
            # deals reales del broker y escribe el historial con el PnL neto exacto.
            self._store.upsert_position(replace(pos, last_action=action.kind.value))
            self._event(
                EventType.CLOSED,
                LogLevel.INFO,
                f"cierre enviado: {action.reason}",
                {"ticket": pos.ticket, "price": result.price},
            )
            return None

        if action.kind is ManageKind.PARTIAL_CLOSE:
            volume = action.close_volume
            if volume is None or volume <= 0.0:
                return pos
            result = self._broker.close_position(pos.ticket, volume=volume)
            if not result.filled:
                self._event(
                    EventType.ERROR,
                    LogLevel.WARNING,
                    f"parcial rechazado: {result.retcode_name} ({result.retcode})",
                    {"ticket": pos.ticket, "volume": volume},
                )
                return pos
            self._kill_switch.note_execution_result(True, now_utc=self._clock.now_utc())
            self._event(
                EventType.REVIEW,
                LogLevel.INFO,
                f"parcial ({volume} lotes): {action.reason}",
                {"ticket": pos.ticket, "volume": volume},
            )
            # El PnL del parcial NO se anota aquí: `_reconcile_closed` suma TODOS los
            # deals (IN + todos los OUT) de la posición cuando muere y escribe una sola
            # fila en `trades`. Anotarlo dos veces inflaría la pérdida/ganancia del día.
            return self._reload(pos, last_action=action.kind.value, partials_delta=1)

        if action.new_sl is None:
            return pos
        result = self._broker.modify_position(pos.ticket, sl=action.new_sl)
        if not result.filled:
            self._event(
                EventType.ERROR,
                LogLevel.WARNING,
                f"no se pudo mover el SL: {result.retcode_name} ({result.retcode})",
                {"ticket": pos.ticket, "sl": action.new_sl},
            )
            return pos
        self._kill_switch.note_execution_result(True, now_utc=self._clock.now_utc())
        updated = self._reload(
            pos,
            last_action=action.kind.value,
            sl=action.new_sl,
            breakeven_done=pos.breakeven_done
            or action.kind is ManageKind.MOVE_SL_BREAKEVEN,
        )
        self._event(
            EventType.REVIEW,
            LogLevel.INFO,
            f"{action.kind.value} a {action.new_sl:.5f}: {action.reason}",
            {"ticket": pos.ticket, "new_sl": action.new_sl},
        )
        return updated

    def _reload(
        self,
        pos: PositionState,
        *,
        last_action: str,
        sl: float | None = None,
        breakeven_done: bool | None = None,
        partials_delta: int = 0,
    ) -> PositionState | None:
        """Vuelve a leer la posición del broker y persiste la foto con sus banderas."""
        try:
            fresh = next(
                (p for p in self._broker.positions(self._magic) if p.ticket == pos.ticket),
                None,
            )
        except Exception:
            LOGGER.exception("no se pudo releer la posición %s", pos.ticket)
            return None
        if fresh is None:
            # Desapareció entre acciones (la cerró el broker): que la reconcilie el ciclo
            # siguiente, cuando el deal OUT ya esté en el histórico.
            return None
        updated = replace(
            fresh,
            playbook_id=pos.playbook_id,
            regime_at_entry=pos.regime_at_entry,
            decision_id_entry=pos.decision_id_entry,
            initial_risk_price=pos.initial_risk_price,
            r_multiple=_r_desde(fresh, pos.initial_risk_price),
            opened_at_utc=pos.opened_at_utc,
            last_action=last_action,
            sl=fresh.sl if fresh.sl else sl,
            breakeven_done=pos.breakeven_done if breakeven_done is None else breakeven_done,
            partials_done=pos.partials_done + partials_delta,
        )
        self._store.upsert_position(updated)
        return updated

    # -- reconciliación de deals --------------------------------------------

    def _reconcile_closed(
        self, open_positions: list[PositionState], trading_day: date
    ) -> set[int]:
        """Cierra en el ledger las posiciones propias que ya no están en el broker.

        Es la prueba de por qué murió una operación: si el SL saltó en el servidor
        mientras el bot estaba ocupado (o reiniciado), nada más lo va a registrar. Se
        buscan los deals OUT de esa posición y se acumula su PnL neto real.

        Devuelve los tickets desaparecidos: aunque aún no haya deal (latencia del
        histórico) tampoco deben gestionarse, porque el broker ya no los muestra.
        """
        known = self._store.read_open_positions()
        if not known:
            return set()
        open_tickets = {pos.ticket for pos in open_positions}
        gone = {ticket for ticket in known if ticket not in open_tickets}
        if not gone:
            return set()
        now = self._clock.now_utc()
        for ticket in sorted(gone):
            stored = known[ticket]
            try:
                deals = list(
                    self._broker.closed_deals(
                        since_utc=now - timedelta(days=3),
                        until_utc=now,
                        position_id=ticket,
                    )
                )
            except Exception:
                LOGGER.exception("no se pudo reconciliar la posición %s", ticket)
                continue
            outs = [deal for deal in deals if deal.entry in ("out", "inout")]
            if not outs:
                # El broker ya no la muestra pero aún no hay deal OUT en el histórico:
                # puede ser latencia. Se deja registrada para reintentar el ciclo próximo.
                continue
            net = sum(d.profit + d.commission + d.swap + d.fee for d in deals)
            reason, source = _exit_from_deals(outs)
            self._daily.record_pnl(trading_day, net)
            self._store.write_trade(
                TradeRecord(
                    position_id=ticket,
                    symbol=self._spec.resolved_name,
                    side=stored.side,
                    volume=stored.volume,
                    entry_price=stored.entry_price,
                    exit_price=outs[-1].price,
                    sl=stored.sl,
                    tp=stored.tp,
                    open_time_utc=stored.opened_at_utc,
                    close_time_utc=outs[-1].time_utc,
                    entry_deal_id=next((d.deal_ticket for d in deals if d.entry == "in"), None),
                    close_deal_id=outs[-1].deal_ticket,
                    commission=sum(d.commission for d in deals),
                    swap=sum(d.swap for d in deals),
                    net_profit=net,
                    r_multiple=_r_from_net(stored, net, self._spec),
                    duration_s=max(0, int((outs[-1].time_utc - stored.opened_at_utc).total_seconds())),
                    exit_reason=reason,
                    exit_reason_source=source,
                    playbook_id=stored.playbook_id,
                    regime_at_entry=stored.regime_at_entry,
                    decision_id_entry=stored.decision_id_entry,
                    reconciled_at_utc=now,
                )
            )
            self._store.delete_position(ticket)
            self._event(
                EventType.RECONCILE,
                LogLevel.INFO,
                f"posición {ticket} cerrada en el broker ({reason.value}): {net:+.2f} USD",
                {"ticket": ticket, "net": round(net, 2), "deals": len(outs)},
            )
        self._apply_daily_loss_lock(trading_day)
        return gone

    def _apply_daily_loss_lock(self, trading_day: date) -> None:
        """Engancha el bloqueo del día si la pérdida realizada alcanzó el máximo.

        Se mide contra el equity de inicio de día guardado en `daily_stats`, y el latch
        persiste: al día siguiente el reloj operativo cambia de fila y el candado se
        re-evalúa solo.
        """
        counters = self._daily.counters(trading_day)
        start = counters.day_start_equity
        if start is None or start <= 0.0:
            return
        try:
            equity = self._broker.account().equity
        except Exception:
            return
        loss_pct = (start - equity) / start * 100.0
        if loss_pct < self._config.risk.daily_loss_pct:
            return
        if self._daily.is_daily_locked(trading_day):
            return
        self._daily.mark_daily_lock(trading_day)
        self._event(
            EventType.KILL_SWITCH,
            LogLevel.ERROR,
            f"pérdida diaria {loss_pct:.2f}% >= {self._config.risk.daily_loss_pct:.2f}%: "
            "día bloqueado hasta el siguiente",
            {"loss_pct": round(loss_pct, 2)},
        )

    # -- salud y comandos ----------------------------------------------------

    def _sync_kill_state(self, state: KillSwitchState) -> KillSwitchState:
        """Guarda el estado del kill switch y publica el latcheo en el histórico.

        El latch solo se veía en el chip del panel: reconstruir por qué el día quedó
        bloqueado exigía mirar el estado *actual*, y en cuanto el proceso se reiniciaba ya
        no estaba el motivo original. El evento deja motivo y momento en `events`, que es
        lo que la revisión semanal audita. Se emite solo en el flanco de subida, para no
        repetir un ERROR cada 5 s mientras el candado sigue puesto.
        """
        if state.latched and not self._kill_state.latched:
            reason = state.reason.value if state.reason is not None else "MANUAL"
            self._event(
                EventType.KILL_SWITCH,
                LogLevel.ERROR,
                f"kill switch latcheado ({reason}): {state.message}",
                {
                    "reason": reason,
                    "since_utc": state.since_utc.isoformat() if state.since_utc else None,
                },
            )
        self._kill_state = state
        return state

    def _note_data_failure(self, cycle_id: str, exc: Exception, now: datetime) -> CycleResult:
        """Registra un fallo de enlace, aplica backoff creciente y bloquea nuevas entradas."""
        self._data_errors += 1
        delay = _BACKOFF_STEPS_S[min(self._data_errors - 1, len(_BACKOFF_STEPS_S) - 1)]
        self._retry_after_utc = now + timedelta(seconds=delay)
        self._sync_kill_state(self._kill_switch.state)
        self._event(
            EventType.RECONNECT,
            LogLevel.ERROR,
            f"enlace con el broker caído ({type(exc).__name__}: {exc}); reintento en {delay:.0f} s",
            {"errors": self._data_errors, "backoff_s": delay},
        )
        LOGGER.warning("broker inaccesible (%d seguidos): %s", self._data_errors, exc)
        self._write_state(None, None, None, "SIN_ENLACE", feed_status="ERROR")
        return _result(cycle_id, "NO_BROKER", ("backoff",), error=str(exc))

    def _note_data_ok(self) -> None:
        """Un ciclo con lecturas correctas corta la racha de fallos y el backoff."""
        if self._data_errors:
            self._event(
                EventType.RECONNECT,
                LogLevel.INFO,
                f"enlace con el broker recuperado tras {self._data_errors} intentos",
            )
        self._data_errors = 0
        self._retry_after_utc = None

    @property
    def retry_after_utc(self) -> datetime | None:
        """Momento a partir del cual vuelve a intentarse el enlace (None si está sano)."""
        return self._retry_after_utc

    def _health_cycle(self, cycle_id: str) -> None:
        """Revisa salud, kill switch y publica estado en el StateStore."""
        try:
            account = self._broker.account()
        except Exception as exc:
            self._note_data_failure(cycle_id, exc, self._clock.now_utc())
            return
        self._note_data_ok()
        health = self._broker.health()
        self._last_health = health
        # Sin freshly leído, la frescura del último snapshot es la que vale.
        analysis = self._analysis_provider()
        freshness = analysis.snapshot.freshness if analysis is not None else {}
        timeframe = analysis.snapshot.decision_timeframe if analysis is not None else None
        self._sync_kill_state(
            self._kill_switch.evaluate(
                health=health,
                account=account,
                freshness=freshness,
                decision_timeframe=timeframe,
            )
        )
        self._write_state(
            analysis.snapshot if analysis is not None else None,
            analysis.regime if analysis is not None else None,
            account,
            self._bot_state_str(),
            self._feed_status,
        )

    def _handle_command(self, command: CommandRecord) -> None:
        if command.command is CommandType.PAUSE:
            self._paused = True
            self._store.complete_command(command.command_id, status=CommandStatus.DONE)
            self._event(EventType.PAUSE, LogLevel.INFO, "bot pausado (solo bloquea nuevas entradas)")
        elif command.command is CommandType.RESUME:
            self._paused = False
            self._store.complete_command(command.command_id, status=CommandStatus.DONE)
            self._event(EventType.RESUME, LogLevel.INFO, "bot reanudado")
        elif command.command is CommandType.RESET_KILL_SWITCH:
            self._kill_state = self._kill_switch.reset(
                now_utc=self._clock.now_utc(), origin=command.origin or "DASHBOARD"
            )
            self._store.complete_command(command.command_id, status=CommandStatus.DONE)
            self._event(
                EventType.KILL_RESET,
                LogLevel.INFO,
                "kill switch reseteado por el operador",
                {"origin": command.origin},
            )
        elif command.command is CommandType.CLOSE_ALL:
            results = self._broker.close_all(self._magic)
            failed = [item for item in results if not item.filled]
            for item in results:
                if item.filled:
                    continue
                self._event(
                    EventType.ERROR,
                    LogLevel.ERROR,
                    f"cierre de emergencia rechazado: {item.retcode_name}",
                    {"retcode": item.retcode},
                )
            self._store.complete_command(
                command.command_id,
                status=CommandStatus.DONE if not failed else CommandStatus.FAILED,
            )
            self._event(
                EventType.CLOSED,
                LogLevel.INFO,
                "cierre de todas las posiciones por comando",
                {"results": len(results), "failed": len(failed)},
            )

    # -- registro ------------------------------------------------------------

    def _log_decision(
        self,
        decision: PolicyDecision,
        snapshot: ContextSnapshot,
        regime: RegimeState,
        decision_id: str,
        *,
        risk: Any = None,
    ) -> None:
        """Escribe la decisión en el JSONL y en SQLite para poder auditar el ciclo."""
        try:
            record = DecisionRecord(
                decision_id=decision_id,
                cycle_id=f"c{self._cycle_seq}",
                ts_utc=snapshot.decision_time_utc,
                bar_time_utc=snapshot.bar_time_utc,
                action=decision.action,
                snapshot=snapshot,
                regime=regime,
                playbook_id=decision.selection.playbook_id,
                signal=decision.signal,
                filters=decision.filters,
                risk=risk,
                rationale=decision.rationale,
                latency_ms=0,
                config_hash=snapshot.config_hash,
                model_versions=snapshot.model_versions,
            )
            # SQLite SIEMPRE: el panel "cerebro" del dashboard lee la tabla, y el JSONL es
            # un espejo opcional. Sin este orden, una ejecución sin logger dejaría el panel
            # vacío aunque el bot sí registró su decisión.
            self._store.write_decision(record)
            if self._decision_logger is not None:
                self._decision_logger.log(record)
        except Exception:
            LOGGER.exception("no se pudo registrar la decisión %s", decision_id)

    def _write_state(
        self,
        snapshot: ContextSnapshot | None,
        regime: RegimeState | None,
        account: AccountState | None,
        state: str,
        feed_status: str,
    ) -> None:
        try:
            self._store.write_bot_state(
                {
                    "state": state,
                    "paused": self._paused,
                    "mode": "DEMO" if account is None or account.is_demo else "LIVE",
                    "requested_symbol": self._symbol,
                    "resolved_symbol": self._spec.resolved_name,
                    "magic": self._magic,
                    "account": asdict(account) if account is not None else None,
                    "health": asdict(self._last_health) if self._last_health is not None else None,
                    "context": snapshot.to_jsonable() if snapshot is not None else None,
                    "regime": regime_to_jsonable(regime) if regime is not None else None,
                    "playbook": None,
                    "filters": None,
                    "feed_status": feed_status,
                    "last_cycle_at_utc": self._clock.now_utc(),
                    "cycle_seq": self._cycle_seq,
                    "last_heartbeat_utc": self._clock.now_utc(),
                    "kill_switch_latched": self._kill_state.latched,
                    "kill_reason": self._kill_state.reason,
                    "config_hash": self._config_hash,
                    "app_version": __version__,
                    "server_offset_min": round(
                        self._clock.server_offset(self._clock.now_utc()).total_seconds() / 60
                    ),
                }
            )
        except Exception:
            LOGGER.exception("no se pudo publicar el estado del bot")

    def _bot_state_str(self) -> str:
        if self._kill_state.latched:
            return "KILLED"
        if self._paused:
            return "PAUSADO"
        return "OPERATIVO"

    def _event(
        self, event_type: EventType, level: LogLevel, message: str, payload: Any = None
    ) -> None:
        try:
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
        except Exception:
            LOGGER.exception("no se pudo registrar el evento %s", message)

    # -- bucle en vivo -------------------------------------------------------

    def run_forever(self, *, poll_interval_s: float = 5.0, max_cycles: int | None = None) -> None:
        """Bucle en vivo: comandos, vela nueva → ciclo, y heartbeat periódico.

        Con el enlace caído no se decide ni se opera: se reintenta con espera creciente
        (`_note_data_failure`) y se re-conecta el adaptador antes del siguiente ciclo.

        El `data_poll` es lo que trae velas nuevas al bucle. Sin él, `analysis_provider`
        devuelve para siempre el snapshot cargado en el arranque y el bot mira el mercado
        congelado: ciclo que sube, decisiones que no.
        """
        last_bar_seen: datetime | None = None
        executed = 0
        try:
            while max_cycles is None or executed < max_cycles:
                executed += 1
                now = self._clock.now_utc()

                command = self._store.claim_next_command(now_utc=now)
                if command is not None:
                    self.step(CommandEvent(command=command, received_at_utc=now))

                if self._retry_after_utc is not None and now < self._retry_after_utc:
                    self._clock.sleep_until(self._retry_after_utc)
                    if not self._reconnect():
                        continue

                if self._data_poll is not None:
                    try:
                        self._feed_status = self._data_poll().value
                    except Exception as exc:  # noqa: BLE001 - el enlace MT5 lanza crudo
                        self._note_data_failure("poll", exc, self._clock.now_utc())
                        continue

                analysis = self._analysis_provider()
                bar_time = analysis.snapshot.bar_time_utc if analysis is not None else None
                if bar_time is not None and bar_time != last_bar_seen:
                    last_bar_seen = bar_time
                    self.run_cycle(analysis)
                else:
                    self.step(TimerEvent(now_utc=self._clock.now_utc()))

                self._clock.sleep_until(self._clock.now_utc() + timedelta(seconds=poll_interval_s))
        except KeyboardInterrupt:
            LOGGER.info("interrumpido por el operador")

    def _reconnect(self) -> bool:
        """Reintenta el enlace con el terminal; un fallo aquí vuelve a atrasar el ciclo."""
        try:
            health = self._broker.connect()
        except Exception as exc:
            self._note_data_failure("reconnect", exc, self._clock.now_utc())
            return False
        if not health.connected:
            return False
        self._note_data_ok()
        return True


def _result(
    cycle_id: str,
    action: str,
    actions_taken: tuple[str, ...],
    *,
    decision_id: str | None = None,
    error: str | None = None,
) -> CycleResult:
    return CycleResult(
        cycle_id=cycle_id,
        action=action,
        decision_id=decision_id,
        actions_taken=actions_taken,
        error=error,
    )


def _sl_en_breakeven(pos: PositionState) -> bool:
    """True si el SL del broker ya está al menos en el precio de entrada.

    Permite deducir el breakeven aunque el flag persistido se haya perdido: si el stop
    ya protege la operación, repetir la orden no aporta nada.
    """
    if pos.sl is None:
        return False
    if pos.side.value == "BUY":
        return pos.sl >= pos.entry_price
    return pos.sl <= pos.entry_price


def _riesgo_inicial(pos: PositionState, info: StoredPosition | None) -> float:
    """Riesgo inicial de la operación, congelado en la BD cuando el bot la conoce.

    El adaptador de MT5 no puede leerlo del broker —`TradePosition` no lo guarda— y lo
    re-deriva como `abs(sl - entry)` en cada lectura. Nada más abrirse eso es exacto, pero
    en cuanto el bot mueve el SL a breakeven el derivado se hunde (3,66 USD pasan a 0,30 en
    una posición real) y con él el divisor de R: la gestión leería ~12 R y cerraría media
    posición creyendo que la operación corrió 1,5 R. La fila escrita al abrir es lo único
    que conserva el valor de la entrada; la lectura del broker sólo sirve para una posición
    adoptada, sin fila propia.
    """
    if info is not None and info.initial_risk_price > 0.0:
        return info.initial_risk_price
    return pos.initial_risk_price


def _r_desde(pos: PositionState, inicial: float) -> float:
    """R de la posición con el riesgo inicial dado, sobre el precio que acaba de leer el broker.

    acompaña a `_riesgo_inicial`: si se cambia el divisor, el R que pinta el panel y el que
    se guarda en `positions` tienen que salir del mismo reparto.
    """
    if inicial <= 0.0:
        return pos.r_multiple
    direction = 1.0 if pos.side is Side.BUY else -1.0
    return (pos.current_price - pos.entry_price) * direction / inicial


def _r_from_net(stored: StoredPosition, net: float, spec: SymbolSpec) -> float | None:
    """R múltiplo final: PnL neto (comisiones incluidas) entre el riesgo inicial en USD."""
    if stored.initial_risk_price <= 0.0 or stored.volume <= 0.0:
        return None
    risk_money = stored.initial_risk_price * spec.contract_size * stored.volume
    if risk_money <= 0.0:
        return None
    return net / risk_money


def _exit_from_deals(outs: list[Any]) -> tuple[ExitReason, ExitReasonSource]:
    """Clasifica el motivo de salida a partir del comentario y lado de los deals OUT."""
    comments = " ".join(str(deal.comment or "") for deal in outs).upper()
    if "SL" in comments:
        return ExitReason.SL, ExitReasonSource.SL
    if "TP" in comments:
        return ExitReason.TP, ExitReasonSource.TP
    if "GOLDBOT" in comments or "CLOSE" in comments:
        return ExitReason.BOT, ExitReasonSource.BOT
    return ExitReason.EXTERNAL, ExitReasonSource.EXTERNAL


def _decision_id(snapshot: ContextSnapshot, decision: PolicyDecision) -> str:
    payload = (
        f"{snapshot.snapshot_hash}|{decision.action}|"
        f"{decision.signal.signal_id if decision.signal else decision.selection.playbook_id}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
