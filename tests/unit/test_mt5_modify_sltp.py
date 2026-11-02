"""`TRADE_ACTION_SLTP` aplica el PAR sl/tp: el lado que falta viaja como 0 y el broker lo borra.

Motivo real: en DEMO, el breakeven de la posición 414168566 movió el SL a 3906,37 y la
posición se quedó sin take profit (lo tenía en 3896,34). El bot llama
`modify_position(ticket, sl=...)` y el helper sólo mete en el dict los campos no nulos,
pero el paquete `MetaTrader5` rellena el ausente con 0 y el servidor lo lee como "sin TP".
Mismo riesgo en espejo para el SL. Ningún test lo veía porque el simulador trata `None`
como "no tocar", que es la semántica que el llamador espera, no la del terminal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from goldbot.adapters.mt5_adapter import Mt5BrokerAdapter
from goldbot.infra.time import UtcClock

MAGIC = 20261030
# Posición real de la prueba de gestión: SELL 0,05 @ 3906,58 con SL 3908,29 y TP 3896,34.
_TICKET = 414168566
_ENTRY = 3906.58
_SL = 3908.29
_TP = 3896.34


class _Pos:
    """Copia del `TradePosition` que expone el build 5.0.6231 (sin `commission`)."""

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _SymbolInfo:
    digits = 2
    point = 0.01
    trade_tick_size = 0.01
    trade_tick_value_loss = 1.0
    trade_tick_value_profit = 1.0
    trade_contract_size = 100.0
    volume_min = 0.01
    volume_max = 50.0
    volume_step = 0.01
    trade_stops_level = 0
    trade_freeze_level = 0
    filling_mode = 1
    trade_mode = 4
    currency_profit = "USD"
    visible = True


class _Api:
    def __init__(self, positions: list[object]) -> None:
        self._positions = positions
        self.sent: list[dict[str, object]] = []

    def positions_get(self) -> list[object]:
        return self._positions

    def symbol_info(self, symbol: str) -> object:
        return _SymbolInfo()

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def order_send(self, request_dict: dict[str, object]) -> object:
        self.sent.append(request_dict)
        return type("Result", (), {"retcode": 10009, "order": 0, "request_id": 1})()


class _Connection:
    def __init__(self, api: _Api) -> None:
        self.api = api


def _adapter_from(api: _Api) -> Mt5BrokerAdapter:
    return Mt5BrokerAdapter(
        _Connection(api),  # type: ignore[arg-type]
        UtcClock("Etc/GMT-2", server_offset=timedelta(hours=2)),
        magic=MAGIC,
        symbol="XAUUSD",
    )


def _position(**overrides: object) -> _Pos:
    values: dict[str, object] = {
        "ticket": _TICKET,
        "identifier": _TICKET,
        "magic": MAGIC,
        "symbol": "XAUUSD",
        "type": 1,  # SELL
        "volume": 0.05,
        "price_open": _ENTRY,
        "price_current": 3903.92,
        "sl": _SL,
        "tp": _TP,
        "profit": 13.30,
        "swap": 0.0,
        "time": datetime(2026, 11, 2, 7, 20, tzinfo=UTC).timestamp(),
        "comment": "goldbot",
    }
    values.update(overrides)
    return _Pos(**values)


def test_mover_el_SL_solo_lleva_consigo_el_TP_vivo() -> None:
    api = _Api([_position()])

    result = _adapter_from(api).modify_position(_TICKET, sl=3906.37)

    assert result.filled
    assert api.sent, "el adaptador no envió ninguna petición"
    enviados = api.sent[0]
    assert enviados["sl"] == 3906.37
    assert enviados["tp"] == _TP, "sin TP explícito el terminal borra el objetivo de beneficio"


def test_mover_el_TP_solo_lleva_consigo_el_SL_vivo() -> None:
    api = _Api([_position()])

    _adapter_from(api).modify_position(_TICKET, tp=3900.00)

    assert api.sent, "el adaptador no envió ninguna petición"
    enviados = api.sent[0]
    assert enviados["tp"] == 3900.00
    assert enviados["sl"] == _SL, "omitir el SL dejaría la posición sin stop"


def test_sin_posicion_legible_no_se_envia_una_peticion_que_puede_borrar_stops() -> None:
    api = _Api([])

    result = _adapter_from(api).modify_position(_TICKET, sl=3906.37)

    assert not result.filled
    assert api.sent == [], "enviar sin conocer el TP vivo es lo que borró el TP en demo"
