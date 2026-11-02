"""Prueba de ejecución de la Fase 4: el `Bot` completo sobre el simulador DEMO.

Ninguna orden real se envía aquí: todo corre contra `SimulatorBrokerAdapter` con velas
controladas una a una. Es la evidencia de que el ciclo completo funciona:

    entrada con SL en el broker → gestión (breakeven/trailing/parcial idempotente y
    persistida) → el SL salta en el servidor → reconciliación de deals en `trades` +
    `daily_stats` → candado del día → backoff con el enlace caído → latch del kill
    switch que sobrevive al reinicio y solo suelta un reset manual.

El núcleo de decisión se sustituye por un `PolicyDecision` fijo: la cadena
selector→playbook→filtros ya se verificó en la Fase 3; aquí se prueba la parte del
ciclo que decide, ejecuta, gestiona, reconcilia y se bloquea.

Precios de referencia (todos derivados de la primera vela, close 2400.00, spread 20 pts,
slippage 2 pts, comisión 3.50 USD/lote/lado):
    ask de entrada        2400.10
    SL / TP de la señal   2396.10 / 2408.10  (riesgo 4.00 → 400 USD por lote)
    lote a 1% de 1000 USD 0.02               (0.025 redondeado HACIA ABAJO)
    fill real             2400.22            → riesgo inicial 4.12 (8.24 USD)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit._snapshots import NOW, regime, signal, snapshot, spec

from goldbot.adapters.simulator import SimulatorBrokerAdapter
from goldbot.config import AppConfig
from goldbot.domain.events import CommandEvent
from goldbot.domain.risk import AtrRiskManager
from goldbot.domain.trade_manager import AtrStructureTradeManager
from goldbot.domain.types import (
    Candle,
    CommandType,
    DataFreshness,
    DataStatus,
    ManagementSettings,
    PolicyDecision,
    Regime,
    RiskSettings,
    Selection,
    Side,
    Timeframe,
)
from goldbot.engine.analysis import AnalysisResult
from goldbot.engine.bot import Bot
from goldbot.engine.execution import ExecutionEngine
from goldbot.engine.kill_switch import KillSwitchSettings, LatchedKillSwitch
from goldbot.infra.state_store import SqliteDailyLedger, SqliteStateStore
from goldbot.infra.time import UtcClock, parse_session_windows

MAGIC = 20261030
BASE = 2_400.0
SPREAD_POINTS = 20.0

# El día operativo: NOW (12:00 UTC) son las 15:00 en el servidor Pepperstone (NY+7).
DAY = NOW.date()


class _FixedClock(UtcClock):
    """Reloj anclado a NOW.

    El simulador marca sus deals con el reloj del bot: con `datetime.now(UTC)` real los
    deals caerían fuera de la ventana de reconciliación (`now-3d .. now`) o dentro por
    los pelos, y la prueba del SL dependería del instante en que corre pytest.
    """

    def now_utc(self) -> datetime:
        return NOW

    def sleep_until(self, ts_utc: datetime) -> None:
        """En tests no se espera nunca: el backoff se comprueba por `retry_after_utc`."""
        return None


class _FlakySim(SimulatorBrokerAdapter):
    """Simulador con un interruptor de enlace: `account()` deja de responder a demanda."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.enlace_caido = False

    def account(self):
        if self.enlace_caido:
            raise ConnectionError("el terminal no responde")
        return super().account()


class _StubCore:
    """Núcleo de decisión fijo (la cadena real se probó en la Fase 3)."""

    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision

    def decide(self, ctx, regime_state, account_state, symbol_spec) -> PolicyDecision:
        return self.decision


def _clock() -> _FixedClock:
    return _FixedClock(
        "America/New_York",
        server_shift=timedelta(hours=7),
        session_windows=parse_session_windows(
            {
                "ASIA": ["03:00-11:00"],
                "LONDON": ["11:00-19:00"],
                "NEW_YORK": ["16:00-00:00"],
            }
        ),
    )


def _bar(close: float, low: float, high: float, *, open_price: float | None = None) -> Candle:
    """Vela cerrada de 5 minutos que termina justo antes de NOW."""
    close_time = NOW - timedelta(minutes=5)
    return Candle(
        open_time_utc=close_time - timedelta(minutes=5),
        close_time_utc=close_time,
        open=open_price if open_price is not None else close,
        high=high,
        low=low,
        close=close,
        tick_volume=100,
        spread_points=SPREAD_POINTS,
        is_closed=True,
        received_at_utc=close_time,
        source="TEST",
    )


def _decision(side: Side, *, entry: float, stop_loss: float, take_profit: float) -> PolicyDecision:
    selection = Selection(
        playbook_id="trend_pullback", scores={}, rationale="test", fallback_no_trade=False
    )
    return PolicyDecision(
        selection=selection,
        signal=signal(
            side=side, entry=entry, stop_loss=stop_loss, take_profit=take_profit
        ),
        filters=None,
        action="ENTER",
        rationale="entrada de prueba",
    )


@dataclass
class _Rig:
    bot: Bot
    core: _StubCore
    sim: _FlakySim
    store: SqliteStateStore
    daily: SqliteDailyLedger
    clock: _FixedClock
    analysis: AnalysisResult
    kill_switch: LatchedKillSwitch
    db_path: Path
    entry_price: float
    stop_loss: float
    take_profit: float


def _rig(tmp_path: Path, *, risk_pct: float = 1.0, daily_loss_pct: float = 2.0) -> _Rig:
    symbol_spec = spec()
    clk = _clock()
    first_bar = _bar(BASE, BASE - 0.5, BASE + 0.5)
    sim = _FlakySim(
        {Timeframe.M5: (first_bar,)},
        symbol_spec,
        initial_balance=1_000.0,
        clock=clk.now_utc,
        slippage_points=2,
        commission_per_lot_side_usd=3.5,
    )
    sim.connect()
    sim.advance(first_bar)

    db_path = tmp_path / "state.db"
    store = SqliteStateStore(db_path, clk)
    store.initialize()
    daily = SqliteDailyLedger(db_path, clock=clk)

    config = AppConfig(risk={"risk_pct": risk_pct, "daily_loss_pct": daily_loss_pct})
    ask = sim.get_last_tick("XAUUSD").ask
    core = _StubCore(
        _decision(Side.BUY, entry=ask, stop_loss=ask - 4.0, take_profit=ask + 8.0)
    )

    trade_manager = AtrStructureTradeManager(ManagementSettings())
    trade_manager.set_symbol_limits(
        spec_point=symbol_spec.point,
        volume_step=symbol_spec.volume_step,
        volume_min=symbol_spec.volume_min,
    )
    kill_switch = LatchedKillSwitch(
        KillSwitchSettings(
            equity_floor_usd=config.risk.equity_floor_usd,
            max_consecutive_execution_errors=config.safety.max_consecutive_execution_errors,
        )
    )
    execution = ExecutionEngine(
        sim,
        AtrRiskManager(RiskSettings(risk_pct=config.risk.risk_pct)),
        clk,
        retry_backoff_s=0.0,
    )
    analysis = AnalysisResult(
        snapshot=snapshot(),
        regime=regime(Regime.TREND_UP),
        model_info=None,
        retrained=False,
    )
    bot = Bot(
        config=config,
        broker=sim,
        store=store,
        clock=clk,
        trade_manager=trade_manager,
        kill_switch=kill_switch,
        core=core,
        execution=execution,
        analysis_provider=lambda: analysis,
        daily=daily,
        magic=MAGIC,
        symbol="XAUUSD",
        spec=symbol_spec,
    )
    return _Rig(
        bot=bot,
        core=core,
        sim=sim,
        store=store,
        daily=daily,
        clock=clk,
        analysis=analysis,
        kill_switch=kill_switch,
        db_path=db_path,
        entry_price=ask,
        stop_loss=ask - 4.0,
        take_profit=ask + 8.0,
    )


def _reboot(
    rig: _Rig,
    kill_switch: LatchedKillSwitch | None = None,
    *,
    data_poll=None,
    analysis_provider=None,
) -> Bot:
    """Segundo proceso sobre la MISMA base de datos y el MISMO broker simulado."""
    symbol_spec = spec()
    clk = rig.clock
    store = SqliteStateStore(rig.db_path, clk)
    daily = SqliteDailyLedger(rig.db_path, clock=clk)
    config = AppConfig()
    trade_manager = AtrStructureTradeManager(ManagementSettings())
    trade_manager.set_symbol_limits(
        spec_point=symbol_spec.point,
        volume_step=symbol_spec.volume_step,
        volume_min=symbol_spec.volume_min,
    )
    return Bot(
        config=config,
        broker=rig.sim,
        store=store,
        clock=clk,
        trade_manager=trade_manager,
        # Kill switch NUEVO: nace sin latchear, como en un reinicio real del proceso.
        kill_switch=kill_switch
        or LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=config.risk.equity_floor_usd)),
        core=_StubCore(
            _decision(
                Side.BUY,
                entry=rig.entry_price,
                stop_loss=rig.stop_loss,
                take_profit=rig.take_profit,
            )
        ),
        execution=ExecutionEngine(
            rig.sim,
            AtrRiskManager(RiskSettings(risk_pct=config.risk.risk_pct)),
            clk,
            retry_backoff_s=0.0,
        ),
        analysis_provider=analysis_provider or (lambda: rig.analysis),
        daily=daily,
        magic=MAGIC,
        symbol="XAUUSD",
        spec=symbol_spec,
        data_poll=data_poll,
    )


def _rows(db_path: Path, query: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(query))


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------


def test_entrada_abre_posicion_con_sl_en_el_broker(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    result = rig.bot.run_cycle(rig.analysis)

    assert result.action == "ENTER"
    assert result.actions_taken == ("entry",)

    positions = rig.sim.positions(MAGIC)
    assert len(positions) == 1
    opened = positions[0]
    # 1% de 1000 USD sobre 400 USD de riesgo por lote = 0.025 -> 0.02 (hacia abajo).
    assert opened.volume == pytest.approx(0.02)
    assert opened.entry_price == pytest.approx(2_400.22)
    # El SL viaja EN la orden: la posición nunca nace desprotegida.
    assert opened.sl == pytest.approx(2_396.10)
    assert opened.tp == pytest.approx(2_408.10)

    stored = rig.store.read_open_positions()
    assert list(stored) == [opened.ticket]
    foto = stored[opened.ticket]
    assert foto.decision_id_entry == result.decision_id
    assert foto.playbook_id == "trend_pullback"
    assert foto.initial_risk_price == pytest.approx(4.12)
    assert foto.breakeven_done is False
    assert foto.partials_done == 0

    assert rig.daily.counters(DAY).entries_used == 1
    intentos = _rows(rig.db_path, "SELECT retcode, retcode_name FROM order_attempts")
    assert [(r["retcode"], r["retcode_name"]) for r in intentos] == [
        (10009, "TRADE_RETCODE_DONE")
    ]
    estado = rig.store.read_bot_state()
    assert estado["state"] == "OPERATIVO"
    assert estado["mode"] == "DEMO"
    assert estado["magic"] == MAGIC


def test_la_entrada_no_se_repite_en_el_ciclo_siguiente(tmp_path: Path) -> None:
    """Con posición propia el ciclo es de gestión: nunca se abre una segunda operación."""
    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)
    ticket = rig.sim.positions(MAGIC)[0].ticket

    result = rig.bot.run_cycle(rig.analysis)

    assert result.action == "MANAGE"
    assert [p.ticket for p in rig.sim.positions(MAGIC)] == [ticket]
    assert len(_rows(rig.db_path, "SELECT id FROM order_attempts")) == 1


def test_sin_orden_no_hay_posicion(tmp_path: Path) -> None:
    """Un SL del lado equivocado lo bloquea `validate_order` antes de tocar el broker."""
    rig = _rig(tmp_path)
    rig.core.decision = _decision(
        Side.BUY,
        entry=rig.entry_price,
        stop_loss=rig.entry_price + 1.0,
        take_profit=rig.take_profit,
    )

    result = rig.bot.run_cycle(rig.analysis)

    assert result.action == "ENTER"
    assert result.actions_taken == ("entry_failed",)
    assert not rig.sim.positions(MAGIC)
    assert rig.store.read_open_positions() == {}
    assert rig.daily.counters(DAY).entries_used == 0
    intentos = _rows(rig.db_path, "SELECT retcode FROM order_attempts")
    assert intentos[0]["retcode"] is None


def _analysis_with(rig: _Rig, **status_by_timeframe: DataStatus) -> AnalysisResult:
    """El análisis del rig con la frescura medida de algunos marcos cambiada."""
    freshness = dict(rig.analysis.snapshot.freshness)
    for name, status in status_by_timeframe.items():
        timeframe = Timeframe(name)
        age = 300.0 if status is DataStatus.OK else 5_400.0
        freshness[timeframe] = DataFreshness(
            timeframe,
            rig.clock.now_utc() - timedelta(seconds=age),
            age,
            status,
        )
    return replace(rig.analysis, snapshot=replace(rig.analysis.snapshot, freshness=freshness))


def test_el_contexto_atrasado_no_impide_la_entrada(tmp_path: Path) -> None:
    """Lunes tras el fin de semana: H1/H4/D1 sin vela cerrada nueva y M5 fresco → se opera.

    El estado global del feed solía ser el peor timeframe configurado, así que el panel
    anunciaba entradas bloqueadas durante horas mientras el motor las habría abierto.
    """
    rig = _rig(tmp_path)
    analysis = _analysis_with(
        rig, D1=DataStatus.MISSING, H4=DataStatus.STALE, H1=DataStatus.STALE
    )

    result = rig.bot.run_cycle(analysis)

    assert result.action == "ENTER"
    assert len(rig.sim.positions(MAGIC)) == 1


def test_el_marco_de_decision_atrasado_descarta_la_entrada(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    analysis = _analysis_with(rig, M5=DataStatus.STALE)

    result = rig.bot.run_cycle(analysis)

    assert result.action == "NO_TRADE"
    assert result.actions_taken == ("feed_stale",)
    assert not rig.sim.positions(MAGIC)
    assert rig.store.read_open_positions() == {}
    assert rig.daily.counters(DAY).entries_used == 0
    assert rig.store.read_bot_state()["feed_status"] == "STALE"
    # Descartar por frescura no es un fallo de ejecución: no acerca el latch del kill switch.
    assert rig.kill_switch.state.latched is False
    eventos = _rows(rig.db_path, "SELECT message FROM events WHERE type = 'FEED_STALE'")
    assert "M5 en estado STALE" in eventos[0]["message"]


def test_un_tick_atrasado_descarta_la_entrada(tmp_path: Path) -> None:
    """El precio de entrada sale del tick: sin cotización reciente no se abre posición."""
    rig = _rig(tmp_path)
    fresco = rig.sim.get_last_tick("XAUUSD")
    rig.sim.get_last_tick = lambda _symbol, _t=fresco: replace(
        _t, time_utc=_t.time_utc - timedelta(minutes=30)
    )

    result = rig.bot.run_cycle(rig.analysis)

    assert result.actions_taken == ("feed_stale",)
    assert not rig.sim.positions(MAGIC)
    assert "tick atrasado" in str(result.error)


# ---------------------------------------------------------------------------
# Gestión
# ---------------------------------------------------------------------------


def test_breakeven_una_sola_vez_y_persistido(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)
    ticket = rig.sim.positions(MAGIC)[0].ticket

    # r = 1.40 (1.40 >= breakeven_at_r 1.0) sin tocar el TP de 2408.10.
    rig.sim.advance(_bar(2_406.0, 2_405.0, 2_406.5))
    rig.bot.run_cycle(rig.analysis)

    # Breakeven + 0.05*ATR (ATR 4.0 = 0.20) sobre la entrada real 2400.22.
    assert rig.sim.positions(MAGIC)[0].sl == pytest.approx(2_400.42)
    foto = rig.store.read_open_positions()[ticket]
    assert foto.breakeven_done is True
    assert foto.partials_done == 0
    assert foto.last_action == "MOVE_SL_BREAKEVEN"

    # La misma vela repetida NO vuelve a mover el SL ni envía una segunda orden SLTP.
    resultado = rig.bot.run_cycle(rig.analysis)
    assert resultado.action == "MANAGE"
    assert rig.sim.positions(MAGIC)[0].sl == pytest.approx(2_400.42)
    assert rig.store.read_open_positions()[ticket].last_action == "MOVE_SL_BREAKEVEN"


def test_trailing_y_parcial_quedan_persistidos(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)
    ticket = rig.sim.positions(MAGIC)[0].ticket

    rig.sim.advance(_bar(2_406.0, 2_405.0, 2_406.5))  # r = 1.40 → breakeven
    rig.bot.run_cycle(rig.analysis)
    assert rig.sim.positions(MAGIC)[0].sl == pytest.approx(2_400.42)

    rig.sim.advance(_bar(2_407.0, 2_406.5, 2_407.5))  # r = 1.65 >= partial_at_r 1.5
    resultado = rig.bot.run_cycle(rig.analysis)

    assert resultado.action == "MANAGE"
    vivas = rig.sim.positions(MAGIC)
    assert len(vivas) == 1
    # Trailing a 1.5*ATR del cierre (2407 - 6) y parcial del 50% (0.02 -> 0.01).
    assert vivas[0].sl == pytest.approx(2_401.00)
    assert vivas[0].volume == pytest.approx(0.01)
    foto = rig.store.read_open_positions()[ticket]
    assert foto.breakeven_done is True
    assert foto.partials_done == 1
    assert foto.last_action == "PARTIAL_CLOSE"

    # El trailing solo arrastra si mejora la protección: 2407 otra vez no mueve nada.
    antes = rig.sim.positions(MAGIC)[0].sl
    rig.bot.run_cycle(rig.analysis)
    assert rig.sim.positions(MAGIC)[0].sl == antes


def test_un_breakeven_en_el_broker_no_reescribe_el_riesgo_inicial_congelado(
    tmp_path: Path,
) -> None:
    """El riesgo inicial es el de la ENTRADA, no el que el terminal vuelve a derivar del SL vivo.

    El adaptador de MT5 calcula `initial_risk_price` como `abs(sl - entry)` en cada lectura
    (en MT5 no existe el riesgo original en la posición). El simulador no lo hace, así que
    aquí se fuerza la misma deriva. Si el bot cree a esa lectura, tras el breakeven el
    divisor de R pasa de 4,12 a 0,20: una posición 0,58 por encima de la entrada se lee como
    2,90 R, supera `partial_at_r` (1,5) y el bot cierra media posición creyendo que la
    operación corrió 2,9 R cuando apenas va en 0,14 R.
    """
    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)
    ticket = rig.sim.positions(MAGIC)[0].ticket
    congelado = rig.store.read_open_positions()[ticket].initial_risk_price
    assert congelado == pytest.approx(4.12)

    rig.sim.advance(_bar(2_406.0, 2_405.0, 2_406.5))  # r = 1,40 → breakeven
    rig.bot.run_cycle(rig.analysis)
    assert rig.sim.positions(MAGIC)[0].sl == pytest.approx(2_400.42)

    sim = rig.sim._positions[ticket]
    sim.initial_risk_price = abs(sim.sl - sim.entry_price)
    assert sim.initial_risk_price == pytest.approx(0.20)

    # Se retira a 2400,80: +0,58 sobre la entrada = 0,14 R reales o 2,90 R con el divisor
    # rederivado del SL.
    rig.sim.advance(_bar(2_400.8, 2_400.5, 2_400.9))
    resultado = rig.bot.run_cycle(rig.analysis)

    hydrated = rig.bot._read_positions()[0]
    assert hydrated.initial_risk_price == pytest.approx(congelado)
    assert hydrated.r_multiple == pytest.approx(0.14, abs=0.01)
    assert resultado.action == "MANAGE"
    # Sin el arreglo, la vela anterior habría cerrado el 50 % de la posición.
    assert rig.sim.positions(MAGIC)[0].volume == pytest.approx(0.02)
    assert rig.store.read_open_positions()[ticket].last_action == "MOVE_SL_BREAKEVEN"


def test_el_bot_deja_de_gestionar_una_posicion_que_ya_no_existe(tmp_path: Path) -> None:
    """Posición desaparecida en el broker: se reconcilia y NO se intenta modificar.

    Un `modify_position` sobre un ticket que ya no existe fallaría, y tres fallos
    seguidos de ejecución latchean el kill switch: el ciclo tiene que descartarla.
    """
    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)
    ticket = rig.sim.positions(MAGIC)[0].ticket

    rig.sim.advance(_bar(2_395.0, 2_394.0, 2_396.0))  # el SL salta en el servidor
    resultado = rig.bot.run_cycle(rig.analysis)

    assert resultado.action != "MANAGE"
    assert not rig.sim.positions(MAGIC)
    assert ticket not in rig.store.read_open_positions()
    assert rig.kill_switch.consecutive_errors == 0
    assert not rig.kill_switch.state.latched


# ---------------------------------------------------------------------------
# Reconciliación de deals y candado del día
# ---------------------------------------------------------------------------


def test_sl_del_servidor_se_reconcilia_en_trades_y_daily_stats(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    entrada = rig.bot.run_cycle(rig.analysis)
    ticket = rig.sim.positions(MAGIC)[0].ticket

    rig.sim.advance(_bar(2_395.0, 2_394.0, 2_396.0))
    resultado = rig.bot.run_cycle(rig.analysis)

    trades = _rows(rig.db_path, "SELECT * FROM trades")
    assert len(trades) == 1
    cierre = trades[0]
    assert cierre["position_id"] == ticket
    assert cierre["side"] == "BUY"
    assert cierre["volume"] == pytest.approx(0.02)
    assert cierre["exit_price"] == pytest.approx(2_396.08)  # SL - slippage
    assert cierre["exit_reason"] == "SL"
    assert cierre["exit_reason_source"] == "SL"
    assert cierre["decision_id_entry"] == entrada.decision_id
    # -8.28 de precio y -0.14 de comisiones (un lado al abrir, otro al cerrar).
    assert cierre["net_profit"] == pytest.approx(-8.42)
    assert cierre["commission"] == pytest.approx(-0.14)
    assert cierre["r_multiple"] == pytest.approx(-8.42 / 8.24)

    contadores = rig.daily.counters(DAY)
    assert contadores.realized_pnl == pytest.approx(-8.42)
    assert contadores.day_start_equity == pytest.approx(1_000.0)

    eventos = _rows(rig.db_path, "SELECT type, message FROM events ORDER BY id")
    assert any(r["type"] == "RECONCILE" for r in eventos)

    # Con el mismo snapshot ya ejecutado no se re-entra: la pérdida no se "recupera".
    assert resultado.action == "ENTER"
    assert resultado.actions_taken == ("already_executed",)
    assert resultado.decision_id == entrada.decision_id
    assert not rig.sim.positions(MAGIC)
    assert rig.store.read_open_positions() == {}


def test_el_equity_de_cierre_del_dia_se_escribe_en_cada_ciclo(tmp_path: Path) -> None:
    """`daily_stats.equity_end` no queda NULL: es la foto del ciclo, no la del arranque.

    Sin cierre, el gráfico de 14 días del dashboard pinta el día vacío y la revisión
    semanal no puede contrastar `realized_pnl` contra el movimiento del equity. El valor
    que se guarda es el equity que el bot ve al empezar el ciclo, así que tras el SL del
    servidor tiene que reflejar ya la pérdida realizada (balance 1000 − 8.42).
    """
    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)

    dia = _rows(rig.db_path, "SELECT equity_start, equity_end FROM daily_stats")[0]
    assert dia["equity_start"] == pytest.approx(1_000.0)
    assert dia["equity_end"] == pytest.approx(1_000.0), "el primer ciclo arranca sin posición"

    rig.sim.advance(_bar(2_395.0, 2_394.0, 2_396.0))  # el SL salta en el servidor
    rig.bot.run_cycle(rig.analysis)

    dia = _rows(rig.db_path, "SELECT equity_start, equity_end FROM daily_stats")[0]
    # El arranque del día es inamovible; el cierre se reescribe con la equity actual.
    assert dia["equity_start"] == pytest.approx(1_000.0)
    assert dia["equity_end"] == pytest.approx(rig.sim.account().equity)
    assert dia["equity_end"] < dia["equity_start"]
    assert 1_000.0 - float(dia["equity_end"]) == pytest.approx(8.42, abs=0.01)


def test_la_perdida_diaria_maxima_bloquea_el_dia(tmp_path: Path) -> None:
    """0.84% perdido con `daily_loss_pct=0.5`: el día queda candado hasta el siguiente."""
    rig = _rig(tmp_path, daily_loss_pct=0.5)
    rig.bot.run_cycle(rig.analysis)

    rig.sim.advance(_bar(2_395.0, 2_394.0, 2_396.0))
    resultado = rig.bot.run_cycle(rig.analysis)

    assert resultado.action == "DAILY_LOCK"
    assert rig.daily.is_daily_locked(DAY) is True
    assert rig.store.read_bot_state()["state"] == "DAILY_LOCK"
    assert not rig.sim.positions(MAGIC)
    assert len(_rows(rig.db_path, "SELECT id FROM order_attempts")) == 1

    eventos = _rows(rig.db_path, "SELECT level, message FROM events ORDER BY id")
    assert any(
        r["level"] == "ERROR" and "día bloqueado" in r["message"] for r in eventos
    )

    # Un reinicio no desbloquea: el candado está en la fila del día operativo.
    segundo = _reboot(rig)
    assert segundo.run_cycle(rig.analysis).action == "DAILY_LOCK"
    assert not rig.sim.positions(MAGIC)


# ---------------------------------------------------------------------------
# Enlace caído
# ---------------------------------------------------------------------------


def test_enlace_caido_no_operay_aplica_backoff(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)
    ticket = rig.sim.positions(MAGIC)[0].ticket

    rig.sim.enlace_caido = True
    resultado = rig.bot.run_cycle(rig.analysis)

    assert resultado.action == "NO_BROKER"
    assert rig.bot.retry_after_utc == NOW + timedelta(seconds=1)
    estado = rig.store.read_bot_state()
    assert estado["state"] == "SIN_ENLACE"
    assert estado["feed_status"] == "ERROR"
    # El backoff es creciente: el segundo fallo espera el doble.
    rig.bot.run_cycle(rig.analysis)
    assert rig.bot.retry_after_utc == NOW + timedelta(seconds=2)
    # Ni una orden nueva durante la caída.
    assert len(_rows(rig.db_path, "SELECT id FROM order_attempts")) == 1

    rig.sim.enlace_caido = False
    recuperado = rig.bot.run_cycle(rig.analysis)
    assert rig.bot.retry_after_utc is None
    assert recuperado.action == "MANAGE"
    assert [p.ticket for p in rig.sim.positions(MAGIC)] == [ticket]


# ---------------------------------------------------------------------------
# Kill switch persistente
# ---------------------------------------------------------------------------


def test_el_latch_del_kill_switch_sobrevive_al_reinicio(tmp_path: Path) -> None:
    from goldbot.domain.types import KillSwitchReason

    rig = _rig(tmp_path)
    rig.kill_switch.force_latch(KillSwitchReason.STALE_DATA, "marco sin velas", now_utc=NOW)

    resultado = rig.bot.run_cycle(rig.analysis)
    assert resultado.action == "KILLED"
    assert not rig.sim.positions(MAGIC)
    assert rig.store.read_bot_state()["kill_switch_latched"] is True

    # Proceso nuevo: nace sin latch en memoria, pero el estado persistido lo reactiva.
    ks_nuevo = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0))
    segundo = _reboot(rig, kill_switch=ks_nuevo)
    assert not ks_nuevo.state.latched
    segundo.restore_kill_latch()
    assert ks_nuevo.state.latched
    tras_reinicio = segundo.run_cycle(rig.analysis)

    assert tras_reinicio.action == "KILLED"
    assert not rig.sim.positions(MAGIC)
    assert len(_rows(rig.db_path, "SELECT id FROM order_attempts")) == 0

    # Solo el operador (comando con token desde el dashboard) lo suelta.
    rig.store.enqueue_command(
        CommandType.RESET_KILL_SWITCH,
        {},
        idempotency_key="reset-1",
        origin="DASHBOARD",
        ttl_s=0,
    )
    comando = rig.store.claim_next_command(now_utc=NOW)
    assert comando is not None
    segundo.step(CommandEvent(command=comando, received_at_utc=NOW))
    assert not ks_nuevo.state.latched
    assert ks_nuevo.reset_history == [("DASHBOARD", NOW)]

    reanudado = segundo.run_cycle(rig.analysis)
    assert reanudado.action == "ENTER"
    assert len(rig.sim.positions(MAGIC)) == 1


def test_el_latcheo_publica_un_evento_kill_switch_una_sola_vez(tmp_path: Path) -> None:
    """El latcheo tiene que quedar en el histórico, no solo en el chip del panel.

    Sin el evento, reconstruir por qué el día se bloqueó dependía de leer el estado
    *actual* del proceso; tras un reinicio el motivo original ya no estaba. Y como el
    ciclo de salud corre cada poll, un ERROR repetido cada 5 s llenaría el registro.
    """
    from goldbot.domain.types import KillSwitchReason

    rig = _rig(tmp_path)
    rig.kill_switch.force_latch(
        KillSwitchReason.EQUITY_FLOOR, "equity 700,00 por debajo del mínimo", now_utc=NOW
    )

    assert rig.bot.run_cycle(rig.analysis).action == "KILLED"
    assert rig.bot.run_cycle(rig.analysis).action == "KILLED"

    eventos = _rows(
        rig.db_path, "SELECT level, message FROM events WHERE type = 'KILL_SWITCH'"
    )
    assert len(eventos) == 1
    assert eventos[0]["level"] == "ERROR"
    assert "EQUITY_FLOOR" in eventos[0]["message"]
    assert "equity 700,00 por debajo del mínimo" in eventos[0]["message"]


# ---------------------------------------------------------------------------
# Bucle en vivo: sin sondeo de velas no hay decisiones nuevas
# ---------------------------------------------------------------------------


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_run_forever_solo_decide_sobre_velas_nuevas(tmp_path: Path) -> None:
    """El bucle en vivo tiene que preguntar al servicio de datos en cada vuelta.

    Es el fallo que dejó el bot mirando la última vela del viernes con el mercado
    abierto: `run_forever` leía `analysis_provider` (el snapshot del arranque) y nadie
    descargaba velas, así que el contador de ciclos subía y `decisions.jsonl` no crecía.
    """
    rig = _rig(tmp_path)
    box: dict[str, AnalysisResult | None] = {"analysis": rig.analysis}
    seen: list[int] = []

    def poll() -> DataStatus:
        seen.append(len(seen) + 1)
        box["analysis"] = AnalysisResult(
            snapshot=snapshot(now=NOW + timedelta(minutes=5 * seen[-1])),
            regime=regime(Regime.TREND_UP),
            model_info=None,
            retrained=False,
        )
        # La última vuelta llega con el atraso del feed: hay que publicarlo, no fijar "OK".
        return DataStatus.OK if len(seen) < 3 else DataStatus.STALE

    bot = _reboot(rig, data_poll=poll, analysis_provider=lambda: box["analysis"])
    bot.run_forever(poll_interval_s=0.0, max_cycles=3)

    assert seen == [1, 2, 3], "el bucle no sondea el servicio de datos en cada vuelta"
    estado = rig.store.read_bot_state()
    esperada = NOW + timedelta(minutes=15) - timedelta(minutes=5)
    assert estado["context_json"]["bar_time_utc"] == _iso_utc(esperada)
    assert estado["feed_status"] == "STALE"


def test_run_forever_sin_sondeo_no_ade_de_la_vela_del_arranque(tmp_path: Path) -> None:
    """Regreso del mismo fallo: sin `data_poll` cuatro ciclos producen una sola decisión."""
    rig = _rig(tmp_path)
    _reboot(rig).run_forever(poll_interval_s=0.0, max_cycles=4)

    estado = rig.store.read_bot_state()
    assert estado["cycle_seq"] > 1
    assert estado["context_json"]["bar_time_utc"] == _iso_utc(rig.analysis.snapshot.bar_time_utc)
    assert len(_rows(rig.db_path, "SELECT cycle_id FROM decisions")) == 1


def test_run_forever_aplica_backoff_si_el_sondeo_falla(tmp_path: Path) -> None:
    """Un fallo descargando velas es el caso "se cae el enlace": registrar, esperar, no operar."""

    def poll() -> DataStatus:
        raise ConnectionError("copy_rates_from_pos falló para XAUUSD")

    rig = _rig(tmp_path)
    bot = _reboot(rig, data_poll=poll)
    bot.run_forever(poll_interval_s=0.0, max_cycles=2)

    assert bot.retry_after_utc is not None
    estado = rig.store.read_bot_state()
    assert estado["state"] == "SIN_ENLACE"
    assert estado["feed_status"] == "ERROR"
    assert len(_rows(rig.db_path, "SELECT cycle_id FROM decisions")) == 0
