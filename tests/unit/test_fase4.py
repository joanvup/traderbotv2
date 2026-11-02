"""Tests de la Fase 4: riesgo, gestión de posición, kill switch y ejecución.

Cubren las piezas de seguridad duras del bot:
- AtrRiskManager: cálculo de lote, redondeo hacia abajo, rechazo por lote mínimo.
- validate_order: SL/TP, volumen, stops_level, filling antes de enviar.
- AtrStructureTradeManager: breakeven, trailing, time stop, régimen en contra.
- LatchedKillSwitch: latch, condiciones y reset auditado.
- ExecutionEngine sobre el simulador: orden ejecutada, validación bloquea.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.unit._snapshots import NOW, account, clock, regime, signal, snapshot, spec

from goldbot.domain.risk import AtrRiskManager, validate_order
from goldbot.domain.trade_manager import AtrStructureTradeManager
from goldbot.domain.types import (
    ManageKind,
    ManagementSettings,
    OrderRequest,
    Regime,
    RiskSettings,
    Side,
    Tick,
)
from goldbot.engine.execution import ExecutionEngine
from goldbot.engine.kill_switch import KillSwitchSettings, LatchedKillSwitch

# ---------------------------------------------------------------------------
# AtrRiskManager
# ---------------------------------------------------------------------------


def test_risk_lote_redondea_hacia_abajo_al_step():
    """El lote se redondea hacia abajo al step para que el riesgo real nunca exceda el configurado."""
    s = signal(side=Side.BUY, entry=2_400.0, stop_loss=2_396.0)  # riesgo 4.0 USD/precio
    rm = AtrRiskManager(RiskSettings(risk_pct=1.0))
    ctx = snapshot()
    acct = account(equity=1_000.0)
    sp = spec()
    decision = rm.size(s, ctx, acct, sp)
    assert decision.approved
    assert decision.lots is not None
    # Riesgo por lote = 4.0 * 100 = 400 USD; riesgo deseado = 10 USD -> 0.025 lotes
    # redondeado hacia abajo al step 0.01 -> 0.02 lotes -> riesgo real 8 USD = 0.8%.
    assert decision.lots == pytest.approx(0.02)
    assert decision.risk_pct_actual is not None
    assert decision.risk_pct_actual <= 1.0


def test_risk_rechaza_si_lote_minimo_excede_el_riesgo():
    """Si el lote mínimo del broker implica un riesgo > máximo, NO se opera."""
    s = replace(signal(side=Side.BUY, entry=2_400.0, stop_loss=2_350.0), stop_loss=2_350.0)
    rm = AtrRiskManager(RiskSettings(risk_pct=1.0))
    acct = account(equity=100.0)
    decision = rm.size(s, snapshot(), acct, spec())
    # riesgo por lote = 50 * 100 = 5000 USD; riesgo deseado = 1 USD -> 0.0002 lotes
    # < step 0.01 -> se rechaza (nunca operar a riesgo mayor por redondeo).
    assert not decision.approved
    assert decision.lots is None


def test_risk_distancia_stop_cero_rechaza():
    s = signal(side=Side.BUY, entry=2_400.0, stop_loss=2_400.0)
    rm = AtrRiskManager(RiskSettings(risk_pct=1.0))
    decision = rm.size(s, snapshot(), account(equity=1_000.0), spec())
    assert not decision.approved


# ---------------------------------------------------------------------------
# validate_order
# ---------------------------------------------------------------------------


def test_validate_order_ok_con_sl():
    request = OrderRequest(
        client_order_id="c1",
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.01,
        sl=2_395.0,
        tp=2_410.0,
        magic=1,
        deviation_points=20,
        filling=None,
        comment="test",
        decision_id="d1",
    )
    tick = Tick(time_utc=NOW, bid=2_399.5, ask=2_400.0, last=2_400.0, spread_points=20.0)
    issues = validate_order(request, spec(), tick)
    blocking = [i for i in issues if i.severity == "ERROR"]
    assert not blocking


def test_validate_order_rechaza_sl_lado_invertido():
    request = OrderRequest(
        client_order_id="c1",
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.01,
        sl=2_401.0,  # SL por ENCIMA del ask para una compra -> inválido
        tp=2_410.0,
        magic=1,
        deviation_points=20,
        filling=None,
        comment="test",
        decision_id="d1",
    )
    tick = Tick(time_utc=NOW, bid=2_399.5, ask=2_400.0, last=2_400.0, spread_points=20.0)
    issues = validate_order(request, spec(), tick)
    assert any(i.code == "SL_WRONG_SIDE" for i in issues)


def test_validate_order_rechaza_volumen_no_alineado():
    request = OrderRequest(
        client_order_id="c1",
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.015,  # no es múltiplo exacto del step 0.01
        sl=2_395.0,
        tp=2_410.0,
        magic=1,
        deviation_points=20,
        filling=None,
        comment="test",
        decision_id="d1",
    )
    tick = Tick(time_utc=NOW, bid=2_399.5, ask=2_400.0, last=2_400.0, spread_points=20.0)
    issues = validate_order(request, spec(), tick)
    assert any(i.code == "VOLUME_NOT_ALIGNED" for i in issues)


def test_validate_order_sl_alineado_no_avisa_a_pesar_del_ruido_flotante():
    """Un SL legítimo a 2 decimales no puede generar SL_NOT_TICK_ALIGNED.

    Con precios de la magnitud real de XAUUSD (~3876), `price % 0.01` devuelve restos
    tipo 2.9e-14 y la comprobación exacta marcaba como desalineado cualquier precio.
    """
    tick = Tick(time_utc=NOW, bid=3_876.61, ask=3_876.78, last=3_876.78, spread_points=17.0)

    def sl_issues(stop: float) -> list[str]:
        request = OrderRequest(
            client_order_id="c1",
            symbol="XAUUSD",
            side=Side.BUY,
            volume=0.02,
            sl=stop,
            tp=None,
            magic=1,
            deviation_points=20,
            filling=None,
            comment="test",
            decision_id="d1",
        )
        return [i.code for i in validate_order(request, spec(), tick)]

    assert "SL_NOT_TICK_ALIGNED" not in sl_issues(3_872.38)
    assert "SL_NOT_TICK_ALIGNED" in sl_issues(3_872.375)


def test_validate_order_requiere_sl():
    request = OrderRequest(
        client_order_id="c1",
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.01,
        sl=None,
        tp=2_410.0,
        magic=1,
        deviation_points=20,
        filling=None,
        comment="test",
        decision_id="d1",
    )
    tick = Tick(time_utc=NOW, bid=2_399.5, ask=2_400.0, last=2_400.0, spread_points=20.0)
    issues = validate_order(request, spec(), tick)
    assert any(i.code == "MISSING_SL" for i in issues)


# ---------------------------------------------------------------------------
# AtrStructureTradeManager
# ---------------------------------------------------------------------------


def _position(
    side=Side.BUY,
    *,
    sl,
    entry=2_400.0,
    current=2_400.0,
    opened=None,
    volume=0.02,
    breakeven_done=False,
    partials_done=0,
):
    from goldbot.domain.types import PositionState

    return PositionState(
        ticket=1,
        position_id=1,
        side=side,
        volume=volume,
        entry_price=entry,
        current_price=current,
        sl=sl,
        tp=None,
        pnl_money=0.0,
        initial_risk_price=abs(entry - sl),
        r_multiple=0.0,
        playbook_id="trend_pullback",
        regime_at_entry=Regime.RANGE,
        decision_id_entry="d1",
        opened_at_utc=opened or NOW,
        breakeven_done=breakeven_done,
        partials_done=partials_done,
    )


def _ctx(now=NOW):
    return snapshot(now=now)


def test_manage_breakeven():
    m = AtrStructureTradeManager(ManagementSettings(breakeven_at_r=1.0))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    pos = _position(side=Side.BUY, sl=2_396.0, entry=2_400.0, current=2_404.0)
    actions = m.on_bar(pos, _ctx(), regime(Regime.RANGE))
    kinds = [a.kind for a in actions]
    assert ManageKind.MOVE_SL_BREAKEVEN in kinds
    breakeven = next(a for a in actions if a.kind is ManageKind.MOVE_SL_BREAKEVEN)
    assert breakeven.new_sl is not None
    assert breakeven.new_sl >= pos.entry_price


def test_manage_time_stop():
    m = AtrStructureTradeManager(ManagementSettings(time_stop_bars=10))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    opened = NOW - timedelta(hours=1)  # 12 velas de 5 min
    pos = _position(side=Side.SELL, sl=2_404.0, entry=2_400.0, current=2_400.0, opened=opened)
    actions = m.on_bar(pos, _ctx(), regime(Regime.RANGE))
    assert any(a.kind is ManageKind.CLOSE for a in actions)


def test_el_time_stop_no_cuenta_minutos_como_segundos():
    """En DEMO real la posición se cerró a los 7 min: 419 s / 5 daba "84 barras".

    La tabla de marcos está en MINUTOS; dividir los segundos entre ese número multiplicaba
    por 60 la edad de la operación y el time stop saltaba casi al instante.
    """
    m = AtrStructureTradeManager(ManagementSettings(time_stop_bars=10, breakeven_at_r=0.5))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    opened = NOW - timedelta(minutes=7)  # 1,4 velas de 5 min, no 84
    pos = _position(side=Side.SELL, sl=2_404.0, entry=2_400.0, current=2_400.0, opened=opened)

    actions = m.on_bar(pos, _ctx(), regime(Regime.RANGE))

    assert not any(a.kind is ManageKind.CLOSE for a in actions)


def test_manage_regimen_en_contra():
    m = AtrStructureTradeManager(ManagementSettings())
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    pos = _position(side=Side.BUY, sl=2_396.0, entry=2_400.0, current=2_400.0)
    actions = m.on_bar(pos, _ctx(), regime(Regime.TREND_DOWN, confidence=0.9))
    assert any(a.kind is ManageKind.CLOSE for a in actions)


def test_manage_trailing():
    m = AtrStructureTradeManager(ManagementSettings(breakeven_at_r=0.5, trailing_atr_mult=1.5))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    # ATR de 4.0 (snapshot por defecto). El breakeven ya se hizo en una vela previa:
    # el trailing solo se arma sobre una posición ya protegida.
    pos = _position(
        side=Side.BUY, sl=2_400.2, entry=2_400.0, current=2_410.0, breakeven_done=True
    )
    actions = m.on_bar(pos, _ctx(), regime(Regime.RANGE))
    kinds = [a.kind for a in actions]
    assert ManageKind.TRAIL_SL in kinds


def test_manage_un_solo_movimiento_de_sl_por_vela():
    """Breakeven y trailing en la misma vela = dos órdenes SLTP seguidas: solo una."""
    m = AtrStructureTradeManager(ManagementSettings(breakeven_at_r=0.5, trailing_atr_mult=1.5))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    pos = _position(side=Side.BUY, sl=2_396.0, entry=2_400.0, current=2_410.0)
    acciones = [
        a for a in m.on_bar(pos, _ctx(), regime(Regime.RANGE)) if a.kind in (
            ManageKind.MOVE_SL_BREAKEVEN,
            ManageKind.TRAIL_SL,
        )
    ]
    assert len(acciones) == 1
    assert acciones[0].kind is ManageKind.MOVE_SL_BREAKEVEN


def test_manage_time_stop_no_cierra_operacion_ganadora():
    """El reloj no corta una operación que ya recorrió el tramo de breakeven."""
    m = AtrStructureTradeManager(ManagementSettings(time_stop_bars=10, breakeven_at_r=0.5))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    opened = NOW - timedelta(hours=1)  # 12 velas de 5 min > time_stop_bars
    pos = _position(
        side=Side.BUY,
        sl=2_400.2,
        entry=2_400.0,
        current=2_404.0,
        opened=opened,
        breakeven_done=True,
    )
    assert not any(a.kind is ManageKind.CLOSE for a in m.on_bar(pos, _ctx(), regime(Regime.RANGE)))


def test_manage_parcial_no_deja_posicion_bajo_el_minimo():
    """0.01 lotes con parcial del 50% cerraría todo o dejaría 0.005: no hay parcial."""
    m = AtrStructureTradeManager(ManagementSettings(partial_at_r=1.5, partial_close_pct=0.5))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    pos = _position(side=Side.BUY, sl=2_396.0, entry=2_400.0, current=2_406.0, volume=0.01)
    assert ManageKind.PARTIAL_CLOSE not in [a.kind for a in m.on_bar(pos, _ctx(), regime(Regime.RANGE))]


def test_manage_no_cierra_con_cambio_de_regimen_de_baja_confianza():
    """Una etiqueta contraria con confianza baja es ruido: la salida la manda el SL."""
    m = AtrStructureTradeManager(ManagementSettings(regime_exit_confidence=0.7))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    pos = _position(side=Side.BUY, sl=2_396.0, entry=2_400.0, current=2_400.0)
    actions = m.on_bar(pos, _ctx(), regime(Regime.TREND_DOWN, confidence=0.4))
    assert not any(a.kind is ManageKind.CLOSE for a in actions)


def test_manage_parcial():
    m = AtrStructureTradeManager(ManagementSettings(partial_at_r=1.5, partial_close_pct=0.5))
    m.set_symbol_limits(spec_point=0.01, volume_step=0.01, volume_min=0.01)
    pos = _position(side=Side.BUY, sl=2_396.0, entry=2_400.0, current=2_406.0, volume=0.02)
    actions = m.on_bar(pos, _ctx(), regime(Regime.RANGE))
    kinds = [a.kind for a in actions]
    assert ManageKind.PARTIAL_CLOSE in kinds
    parcial = next(a for a in actions if a.kind is ManageKind.PARTIAL_CLOSE)
    assert parcial.close_volume == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# LatchedKillSwitch
# ---------------------------------------------------------------------------


def _health(connected=True):
    from goldbot.domain.types import BrokerHealth

    return BrokerHealth(
        connected=connected,
        terminal_connected=connected,
        algo_trading_enabled=True,
        account_trade_allowed=True,
        latency_ms=0.0,
        last_error=None,
        checked_at_utc=NOW,
    )


def test_kill_switch_latch_y_reset():
    ks = LatchedKillSwitch()
    account_state = account(equity=100.0)  # por debajo del floor 800
    state = ks.evaluate(health=_health(), account=account_state, freshness={})
    assert state.latched
    assert state.reason is not None
    # Latch: re-evaluar con cuenta ya sana no lo relaja.
    state2 = ks.evaluate(health=_health(), account=account(equity=2_000.0), freshness={})
    assert state2.latched
    # Reset auditado.
    reset = ks.reset(now_utc=NOW, origin="DASHBOARD")
    assert not reset.latched


def test_kill_switch_equity_floor():
    ks = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0))
    state = ks.evaluate(health=_health(), account=account(equity=799.0), freshness={})
    assert state.latched
    assert state.reason.name == "EQUITY_FLOOR"


def test_kill_switch_no_dispara_con_salud_sana():
    ks = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0))
    state = ks.evaluate(health=_health(), account=account(equity=1_000.0), freshness={})
    assert not state.latched


def test_kill_switch_dispara_si_terminal_desconectado():
    ks = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0))
    for _ in range(3):  # la tolerancia por defecto son 3 lecturas
        state = ks.evaluate(
            health=_health(connected=False), account=account(equity=1_000.0), freshness={}
        )
    assert state.latched
    assert state.reason.name == "TERMINAL_DISCONNECTED"
    assert "3 lecturas seguidas" in state.message


def test_el_kill_switch_tolera_lecturas_sueltas_sin_enlace():
    """Un `terminal_info()` que falla un instante no debe bloquear el día y exigir reset."""
    ks = LatchedKillSwitch(
        KillSwitchSettings(equity_floor_usd=800.0, disconnect_tolerance_polls=3)
    )
    sano = account(equity=1_000.0)

    assert not ks.evaluate(
        health=_health(connected=False), account=sano, freshness={}
    ).latched
    assert not ks.evaluate(
        health=_health(connected=False), account=sano, freshness={}
    ).latched
    assert ks.evaluate(
        health=_health(connected=False), account=sano, freshness={}
    ).latched


def test_una_lectura_con_enlace_corta_la_racha_de_caida():
    ks = LatchedKillSwitch(
        KillSwitchSettings(equity_floor_usd=800.0, disconnect_tolerance_polls=3)
    )
    sano = account(equity=1_000.0)
    caido = _health(connected=False)

    ks.evaluate(health=caido, account=sano, freshness={})
    ks.evaluate(health=caido, account=sano, freshness={})
    ks.evaluate(health=_health(), account=sano, freshness={})  # recupera el enlace
    for _ in range(2):
        assert not ks.evaluate(health=caido, account=sano, freshness={}).latched
    assert ks.evaluate(health=caido, account=sano, freshness={}).latched


def test_kill_switch_dispara_por_margen_libre_bajo():
    ks = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0, min_free_margin_pct=30.0))
    state = ks.evaluate(
        health=_health(), account=account(equity=1_000.0, margin_free=250.0), freshness={}
    )
    assert state.latched
    assert state.reason.name == "MARGIN_CRITICAL"
    assert "25.0%" in state.message


def test_kill_switch_el_margen_no_dispara_si_esta_desactivado():
    ks = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0, min_free_margin_pct=0.0))
    state = ks.evaluate(
        health=_health(), account=account(equity=1_000.0, margin_free=0.0), freshness={}
    )
    assert not state.latched


def test_kill_switch_el_suelo_de_equity_manda_sobre_el_margen():
    """Prioridad 4 (EQUITY_FLOOR) antes que 5 (MARGIN_CRITICAL): el motivo debe ser el suelo."""
    ks = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0, min_free_margin_pct=30.0))
    state = ks.evaluate(
        health=_health(), account=account(equity=790.0, margin_free=100.0), freshness={}
    )
    assert state.reason.name == "EQUITY_FLOOR"


def test_kill_switch_el_margen_con_equity_cero_no_divide():
    ks = LatchedKillSwitch(KillSwitchSettings(equity_floor_usd=800.0, min_free_margin_pct=30.0))
    state = ks.evaluate(
        health=_health(), account=account(equity=0.0, margin_free=0.0), freshness={}
    )
    assert state.latched
    assert state.reason.name == "EQUITY_FLOOR"


# ---------------------------------------------------------------------------
# ExecutionEngine sobre el simulador
# ---------------------------------------------------------------------------


def _sim():
    from tests.unit._synthetic import make_candles

    from goldbot.adapters.simulator import SimulatorBrokerAdapter
    from goldbot.domain.types import Timeframe

    candles = {
        Timeframe.M5: make_candles(Timeframe.M5, 200, end_utc=NOW, start=2_400.0, drift=0.1),
    }
    sim = SimulatorBrokerAdapter(
        candles,
        spec(),
        initial_balance=1_000.0,
        slippage_points=2,
        commission_per_lot_side_usd=3.5,
    )
    sim.connect()
    sim.advance(candles[Timeframe.M5][-1])
    return sim


def _decision(side=Side.BUY, *, entry, stop_loss, take_profit):
    from goldbot.domain.types import PolicyDecision, Selection

    sel = Selection(playbook_id="trend_pullback", scores={}, rationale="test", fallback_no_trade=False)
    s = signal(side=side, entry=entry, stop_loss=stop_loss, take_profit=take_profit)
    return PolicyDecision(selection=sel, signal=s, filters=None, action="ENTER", rationale="test")


def test_execution_engine_placea_orden():
    sim = _sim()
    tick = sim.get_last_tick("XAUUSD")
    engine = ExecutionEngine(
        sim,
        AtrRiskManager(RiskSettings(risk_pct=1.0)),
        clock(),
    )
    # La entrada se evalúa al precio de mercado del simulador (~2409): SL y TP deben
    # estar correctamente colocados respecto al ask real para que la validación pase.
    ask = tick.ask
    result = engine.execute_signal(
        _decision(Side.BUY, entry=ask, stop_loss=ask - 10.0, take_profit=ask + 10.0),
        snapshot(),
        account(equity=1_000.0),
        spec(),
        tick,
        magic=123,
        decision_id="dec1",
    )
    assert result.placed
    assert result.result is not None
    assert result.result.retcode == 10009
    # El SL quedó en la posición del simulador.
    positions = sim.positions(123)
    assert len(positions) == 1
    assert positions[0].sl == pytest.approx(ask - 10.0)


def test_execution_engine_bloquea_por_validacion():
    from goldbot.domain.types import PolicyDecision, Selection

    sim = _sim()
    tick = sim.get_last_tick("XAUUSD")
    engine = ExecutionEngine(
        sim,
        AtrRiskManager(RiskSettings(risk_pct=1.0)),
        clock(),
    )
    # Un SL muy por encima del precio actual de una compra -> validate_order bloquea
    # (el SL debe quedar por debajo del ask). El riesgo se mantiene pequeño para que
    # el RiskManager apruebe y la orden llegue a la validación.
    ask = tick.ask
    sel = Selection(playbook_id="trend_pullback", scores={}, rationale="test", fallback_no_trade=False)
    s = signal(side=Side.BUY, entry=ask + 0.01, stop_loss=ask + 1.0, take_profit=ask + 2.0)
    decision = PolicyDecision(selection=sel, signal=s, filters=None, action="ENTER", rationale="test")
    result = engine.execute_signal(
        decision,
        snapshot(),
        account(equity=1_000.0),
        spec(),
        tick,
        magic=123,
        decision_id="dec2",
    )
    assert not result.placed
    assert any(i.code == "SL_WRONG_SIDE" for i in result.issues)


def test_execution_engine_rechaza_si_riesgo_no_aprueba():
    sim = _sim()
    tick = sim.get_last_tick("XAUUSD")
    engine = ExecutionEngine(
        sim,
        AtrRiskManager(RiskSettings(risk_pct=1.0)),
        clock(),
    )
    # Equity muy bajo -> el lote mínimo excede el riesgo -> no aprobado.
    ask = tick.ask
    result = engine.execute_signal(
        _decision(Side.BUY, entry=ask, stop_loss=ask - 10.0, take_profit=ask + 10.0),
        snapshot(),
        account(equity=50.0),
        spec(),
        tick,
        magic=123,
        decision_id="dec3",
    )
    assert not result.placed
    assert result.risk is not None
    assert not result.risk.approved