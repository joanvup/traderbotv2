"""El comment de la orden debe caber en los 29 caracteres que acepta el paquete MT5.

Motivo real: en DEMO, `mt5.order_send` devolvía `None` (retcode sintetizado 10012
`NO_RESULT_FROM_TERMINAL`, latencia 0 ms) porque el adapter concatenaba
`playbook|señal|decision_id` y lo recortaba a 31. El wrapper de Python rechaza la
petición localmente con `(-2, 'Invalid "comment" argument')` a partir de 30 caracteres:
cualquier entrada del bot habría fallado igual.
"""

from __future__ import annotations

from datetime import UTC, datetime

from goldbot.adapters.mt5 import orders
from goldbot.adapters.mt5_adapter import _order_comment
from goldbot.domain.types import OrderRequest, Side, SymbolSpec


def _request(comment: str, decision_id: str) -> OrderRequest:
    return OrderRequest(
        client_order_id="cid-1",
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.01,
        sl=3894.67,
        tp=3909.67,
        magic=20261030,
        deviation_points=20,
        filling=None,
        comment=comment,
        decision_id=decision_id,
    )


def test_el_comment_de_produccion_cabe_y_conserva_el_decision_id() -> None:
    # Forma real: playbook "trend_pullback" + 10 caracteres de señal + id de 16 hex.
    request = _request("trend_pullback|4efe46880b", "4efe46880bf8534b")
    comment = _order_comment(request)

    assert len(comment) <= orders.MAX_COMMENT_CHARS
    assert comment.endswith("4efe46880bf8534b")
    assert comment.startswith("trend_")


def test_sin_comment_de_playbook_se_envia_solo_el_decision_id() -> None:
    assert _order_comment(_request("", "4efe46880bf8534b")) == "4efe46880bf8534b"


def test_un_decision_id_largo_no_desborda_el_comment() -> None:
    comment = _order_comment(_request("trend_pullback|4efe46880b", "d" * 40))

    assert len(comment) <= orders.MAX_COMMENT_CHARS
    assert comment == "d" * orders.MAX_COMMENT_CHARS


class _RecordingApi:
    """`order_send` que guarda el dict y devuelve un DONE con comment espejado."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def order_send(self, request_dict: dict[str, object]) -> object:
        self.sent.append(request_dict)
        return type(
            "Result",
            (),
            {
                "retcode": 10009,
                "order": 555,
                "deal": 556,
                "price": 3899.79,
                "comment": str(request_dict["comment"]),
                "request_id": 1,
            },
        )()


def test_send_order_recorta_al_tope_real_aunque_el_llamador_aborrache() -> None:
    api = _RecordingApi()
    request = _request("trend_pullback|4efe46880b", "4efe46880bf8534b")

    result = orders.send_order(
        api,
        request,
        _spec(),
        filling=orders.ORDER_FILLING_IOC,
        deviation=20,
        comment="x" * 60,
    )

    assert result.filled
    sent_comment = str(api.sent[0]["comment"])
    assert len(sent_comment) == orders.MAX_COMMENT_CHARS


def _spec() -> SymbolSpec:
    return SymbolSpec(
        resolved_name="XAUUSD",
        requested_name="XAUUSD",
        digits=2,
        point=0.01,
        tick_size=0.01,
        trade_tick_value_loss=1.0,
        trade_tick_value_profit=1.0,
        contract_size=100.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        stops_level_points=0,
        freeze_level_points=0,
        allowed_fillings=2,
        filling_mode=2,
        trade_mode=4,
        currency_profit="USD",
        spec_hash="spec-hash",
        fetched_at_utc=datetime(2026, 11, 2, tzinfo=UTC),
    )
