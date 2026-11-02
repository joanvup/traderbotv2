"""Tests del `SimulatorBrokerAdapter` (Fase 4): ejecución sin terminal.

Cubren la semántica conservadora del simulador: SL antes que TP si ambos se tocan en
la misma vela, cierre parcial con deals correctos, rechazo de modificación en contra,
causalidad de `get_candles` y desconexión simulada.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.unit._snapshots import NOW, spec
from tests.unit._synthetic import make_candles

from goldbot.adapters.simulator import SimulatorBrokerAdapter
from goldbot.domain.types import OrderRequest, Side, Timeframe


def _sim(candles=None):
    if candles is None:
        candles = {
            Timeframe.M5: make_candles(Timeframe.M5, 200, end_utc=NOW, start=2_400.0, drift=0.1),
        }
    # Reloj fijo para que el simulador sea determinista (deals con timestamps en rango).
    sim = SimulatorBrokerAdapter(
        candles, spec(), initial_balance=1_000.0, clock=lambda: NOW
    )
    sim.connect()
    sim.advance(candles[Timeframe.M5][-1])
    return sim, candles


def _request(side=Side.BUY, *, sl, tp=None, volume=0.01):
    return OrderRequest(
        client_order_id="c1",
        symbol="XAUUSD",
        side=side,
        volume=volume,
        sl=sl,
        tp=tp,
        magic=7,
        deviation_points=0,
        filling=None,
        comment="trend_pullback|test",
        decision_id="d1",
    )


def test_place_y_cierre_por_sl_intra_vela():
    sim, candles = _sim()
    # La última vela (procesada) cierra ~2409; abrimos con SL debajo y el siguiente
    # advance con una vela que baja a tocar el SL.
    tick = sim.get_last_tick("XAUUSD")
    result = sim.place_order(_request(Side.BUY, sl=tick.bid - 5.0, tp=tick.bid + 10.0))
    assert result.retcode == 10009
    positions = sim.positions(7)
    assert len(positions) == 1
    # Avanzar con una vela que toca el SL.
    last = candles[Timeframe.M5][-1]
    down = make_candles(
        Timeframe.M5, 1, end_utc=last.close_time_utc + timedelta(minutes=5), start=last.close, drift=-20.0, noise=0.0
    )
    sim.advance(down[0])
    assert len(sim.positions(7)) == 0
    deals = sim.closed_deals(since_utc=NOW - timedelta(hours=1), until_utc=NOW + timedelta(hours=1))
    assert any(d.entry == "out" and d.comment == "SL" for d in deals)


def test_sl_gana_a_tp_en_misma_vela():
    """Si SL y TP se tocan en la misma vela, se asume que gana el SL (conservador)."""
    sim, candles = _sim()
    tick = sim.get_last_tick("XAUUSD")
    result = sim.place_order(_request(Side.BUY, sl=tick.bid - 1.0, tp=tick.bid + 1.0))
    assert result.retcode == 10009
    # Una vela construida a mano que toca AMBOS niveles (low < SL y high > TP),
    # con cierre arriba: debe ganar el SL (regla conservadora: stop primero).
    last = candles[Timeframe.M5][-1]
    from goldbot.domain.types import Candle

    wide_bar = Candle(
        open_time_utc=last.close_time_utc,
        close_time_utc=last.close_time_utc + timedelta(minutes=5),
        open=last.close,
        high=last.close + 3.0,  # por encima del TP (tick.bid + 1)
        low=last.close - 3.0,   # por debajo del SL (tick.bid - 1)
        close=last.close + 1.0,
        tick_volume=100,
        spread_points=15.0,
        is_closed=True,
        received_at_utc=last.close_time_utc + timedelta(minutes=5),
        source="TEST",
    )
    sim.advance(wide_bar)
    assert len(sim.positions(7)) == 0
    deals = sim.closed_deals(since_utc=NOW - timedelta(hours=1), until_utc=NOW + timedelta(hours=1))
    sl_deal = next((d for d in deals if d.comment == "SL"), None)
    assert sl_deal is not None


def test_cierre_parcial_genera_deals_y_volumen_residual():
    sim, candles = _sim()
    tick = sim.get_last_tick("XAUUSD")
    result = sim.place_order(_request(Side.BUY, sl=tick.bid - 5.0, volume=0.02))
    assert result.retcode == 10009
    # Cierre parcial de 0.01.
    closed = sim.close_position(result.order_ticket, volume=0.01)
    assert closed.retcode == 10009
    positions = sim.positions(7)
    assert len(positions) == 1
    assert positions[0].volume == pytest.approx(0.01)


def test_modify_position_en_contra_rechazado():
    sim, candles = _sim()
    tick = sim.get_last_tick("XAUUSD")
    result = sim.place_order(_request(Side.BUY, sl=tick.bid - 5.0))
    # Para BUY, bajar el SL está en contra de la protección -> rechazado.
    mod = sim.modify_position(result.order_ticket, sl=tick.bid - 20.0)
    assert mod.retcode != 10009
    positions = sim.positions(7)
    assert positions[0].sl == pytest.approx(tick.bid - 5.0)


def test_get_candles_es_causal():
    sim, candles = _sim()
    cut = candles[Timeframe.M5][100].close_time_utc
    seen = sim.get_candles("XAUUSD", Timeframe.M5, 500, closed_only=True, as_of_utc=cut)
    assert all(c.close_time_utc <= cut for c in seen)


def test_desconexion_simulada_rechaza_ordenes():
    sim, candles = _sim()
    sim.simulate_disconnect(True)
    health = sim.health()
    assert not health.connected
    tick = sim.get_last_tick("XAUUSD")
    result = sim.place_order(_request(Side.BUY, sl=tick.bid - 5.0))
    assert result.retcode != 10009


def test_account_refleja_pnl_flotante():
    sim, candles = _sim()
    tick = sim.get_last_tick("XAUUSD")
    result = sim.place_order(_request(Side.BUY, sl=tick.bid - 50.0, tp=tick.bid + 50.0, volume=0.01))
    assert result.retcode == 10009
    before = sim.account()
    # Avanzar con una vela al alza para tener PnL flotante positivo.
    last = candles[Timeframe.M5][-1]
    up = make_candles(
        Timeframe.M5, 1, end_utc=last.close_time_utc + timedelta(minutes=5), start=last.close, drift=5.0, noise=0.0
    )
    sim.advance(up[0])
    after = sim.account()
    assert after.equity != before.equity
    assert after.is_demo