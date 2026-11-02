"""Lectura de posiciones contra el `TradePosition` que expone el paquete MT5 real.

El build 5.0.6231 no tiene `commission` ni `position` en la posición abierta: tocarlos
lanzaba `AttributeError` dentro de `run_cycle`, que el bot traga como fallo de datos, así
que con una operación viva el bot dejaba de decidir en cada vela. El fake de este archivo
declara SOLO los atributos que existen en ese build, para que la regresión sea roja.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from goldbot.adapters.mt5_adapter import Mt5BrokerAdapter
from goldbot.domain.types import Side
from goldbot.infra.time import UtcClock

_SERVER_OFFSET_H = 2  # Pepperstone: servidor = NY+7, que tras el fin del DST es GMT+2.


class TradePositionThisBuild:
    """Copia del `namedtuple` que devuelve `positions_get` en esta build."""

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _Api:
    def __init__(self, positions: list[object]) -> None:
        self._positions = positions

    def positions_get(self) -> list[object]:
        return self._positions


class _Connection:
    def __init__(self, positions: list[object]) -> None:
        self.api = _Api(positions)


def _adapter(positions: list[object]) -> Mt5BrokerAdapter:
    """Reloj con el desfase fijo de Pepperstone tras el cambio de DST (2026-11-01): GMT+2."""
    return Mt5BrokerAdapter(
        _Connection(positions),
        UtcClock("Etc/GMT-2", server_offset=timedelta(hours=_SERVER_OFFSET_H)),
        magic=20261030,
        symbol="XAUUSD",
    )


def _position(**overrides: object) -> TradePositionThisBuild:
    # `time` llega como epoch de la HORA DE SERVIDOR: 02:17 en Pepperstone = 00:17 UTC.
    server_wall_epoch = datetime(2026, 11, 2, 2, 17, tzinfo=UTC).timestamp()
    values: dict[str, object] = {
        "ticket": 413997353,
        "identifier": 413997353,
        "magic": 20261030,
        "symbol": "XAUUSD",
        "type": 0,
        "volume": 0.01,
        "price_open": 3899.79,
        "price_current": 3897.41,
        "sl": 3894.67,
        "tp": 3909.67,
        "profit": -2.38,
        "swap": 0.0,
        "time": server_wall_epoch,
        "comment": "diag-roundtrip",
    }
    values.update(overrides)
    return TradePositionThisBuild(**values)


def test_una_posicion_propia_se_lee_sin_attributos_que_no_existen() -> None:
    state = _adapter([_position()]).positions(20261030)[0]

    assert state.ticket == 413997353
    assert state.position_id == 413997353
    assert state.side is Side.BUY
    assert state.entry_price == 3899.79
    assert (state.sl, state.tp) == (3894.67, 3909.67)
    assert state.pnl_money == -2.38
    assert state.r_multiple == pytest.approx(-2.38 / 5.12)
    assert state.opened_at_utc == datetime(2026, 11, 2, 0, 17, tzinfo=UTC)


def test_una_posicion_abierta_no_es_del_bot_no_se_gestiona() -> None:
    other = _position(magic=7, ticket=99, identifier=99)

    assert [p.ticket for p in _adapter([other, _position()]).positions(20261030)] == [413997353]


class TradeDealThisBuild:
    """Copia de `TradeDeal`: `ticket`/`position_id`, sin `id` ni `position`."""

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _DealsApi:
    def __init__(self, deals: list[object]) -> None:
        self._deals = deals
        self.calls: list[dict[str, object]] = []

    def history_deals_get(self, **kwargs: object) -> list[object]:
        self.calls.append(kwargs)
        if "position" in kwargs:
            return [d for d in self._deals if d.position_id == kwargs["position"]]
        return list(self._deals)


class _AnyApi:
    """Envoltorio: `positions_get` vacío y los deals que se le den."""

    def __init__(self, deals: list[object]) -> None:
        self._deals_api = _DealsApi(deals)

    @property
    def calls(self) -> list[dict[str, object]]:
        return self._deals_api.calls

    def positions_get(self) -> list[object]:
        return []

    def history_deals_get(self, **kwargs: object) -> list[object]:
        return self._deals_api.history_deals_get(**kwargs)


def _deals_adapter(deals: list[object]) -> tuple[Mt5BrokerAdapter, _AnyApi]:
    api = _AnyApi(deals)

    class _Conn:
        def __init__(self) -> None:
            self.api = api

    clock = UtcClock("Etc/GMT-2", server_offset=timedelta(hours=_SERVER_OFFSET_H))
    return Mt5BrokerAdapter(_Conn(), clock, magic=20261030, symbol="XAUUSD"), api


def _deal(**overrides: object) -> TradeDealThisBuild:
    values: dict[str, object] = {
        "ticket": 322860547,
        "order": 413997353,
        "position_id": 413997353,
        "magic": 20261030,
        "entry": 0,
        "type": 0,
        "volume": 0.01,
        "price": 3899.79,
        "profit": 0.0,
        "commission": -0.04,
        "swap": 0.0,
        "fee": 0.0,
        "time": datetime(2026, 11, 2, 2, 18, tzinfo=UTC).timestamp(),
        "comment": "goldbot",
    }
    values.update(overrides)
    return TradeDealThisBuild(**values)


def test_los_deals_se_leen_por_posicion_y_con_los_nombres_reales() -> None:
    """La build 6231 no tiene `deal.id`/`deal.position`: la reconciliación fallaba en silencio."""
    out = _deal(ticket=322862907, entry=1, type=1, price=3900.10, profit=0.31)
    broker, api = _deals_adapter([_deal(), out])
    now = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)

    deals = list(broker.closed_deals(since_utc=now - timedelta(days=3), until_utc=now, position_id=413997353))

    assert api.calls == [{"position": 413997353}]
    assert [d.deal_ticket for d in deals] == [322860547, 322862907]
    assert [d.entry for d in deals] == ["in", "out"]
    assert deals[1].profit + deals[1].commission + deals[1].swap + deals[1].fee == pytest.approx(0.27)
    assert deals[0].time_utc == datetime(2026, 11, 2, 0, 18, tzinfo=UTC)
