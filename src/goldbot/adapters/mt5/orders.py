"""Envío, modificación y cierre de órdenes, con resolución de filling mode y retcodes.

Nada de esta función se llama sin haber pasado antes por `validate_order` en
`domain/risk.py`, pero revalida de forma conservadora (es la última barrera antes
de enviar al broker).

Consejos de seguridad aplicados aquí:
- `MetaTrader5` NO es thread-safe. Este adaptador asume llamadas secuenciales;
  el `Bot` de la Fase 4 garantiza exclusión enviando y verificando en un único hilo.
- Retcodes 10026/10027 (`SERVER_DISABLES_AT` / `CLIENT_DISABLES_AT`) significan que el
  trading algorítmico está apagado (en el servidor o en el terminal): se clasifican
  como `ALGO_DISABLED` y el bot debe levantar kill switch.
- Un retcode ambiguo (TIMEOUT, ERROR, CONNECTION) NO se reintenta a ciegas: puede que
  la orden sí haya llegado al mercado. `is_ambiguous()` marca esos casos y la capa de
  ejecución reconcilia por deals antes de decidir qué hacer.

Constantes: los valores están verificados contra la API instalada (`MetaTrader5`
5.0.6231) con `getattr(mt5, ...)`. Son dos espacios de valores distintos y confundirlos
rompe el envío: `symbol_info().filling_mode` es un BITMASK (1 FOK / 2 IOC / 4 RETURN)
mientras que `MqlTradeRequest.type_filling` usa ORDER_FILLING_* (0 FOK / 1 IOC / 2
RETURN / 3 BOC).
"""

from __future__ import annotations

import time
from typing import Any

from goldbot.domain.types import (
    OrderErrorCategory,
    OrderRequest,
    OrderResult,
    Side,
    SymbolSpec,
)

# ---------------------------------------------------------------------------
# Constantes oficiales de la API de MT5 (copia local, no se importan del broker
# aquí porque la API de MT5 puede cambiar entre builds; se usan para
# construir el dict de la orden y para interpretar el retcode devuelto).
# ---------------------------------------------------------------------------

TRADE_ACTION_DEAL = 1        # orden a mercado (abrir o cerrar)
TRADE_ACTION_PENDING = 5     # orden pendiente (no se usa)
TRADE_ACTION_SLTP = 6        # modificar SL/TP de una posición
TRADE_ACTION_MODIFY = 7      # modificar orden (no se usa)
TRADE_ACTION_REMOVE = 8      # eliminar orden (no se usa)
TRADE_ACTION_CLOSE_BY = 10   # cierre por contra (no se usa; cerramos con un DEAL)
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

# Bitmask que reporta `symbol_info().filling_mode` (más de un bit puede estar activo).
FILLING_MASK_FOK = 1
FILLING_MASK_IOC = 2
FILLING_MASK_RETURN = 4

# Valores que acepta `MqlTradeRequest.type_filling` (ORDER_FILLING_*).
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
ORDER_FILLING_BOC = 3

# Tope real de `comment` que acepta el paquete Python de MetaTrader5. La documentación
# dice 31, pero el buffer del `MqlTradeRequest` es de 31 BYTES con terminador: el wrapper
# rechaza la petición ANTES de enviarla al terminal con
# `mt5.last_error() == (-2, 'Invalid "comment" argument')`. Medido en Pepperstone-Demo
# (build 5.0.6231) con `order_send` de volumen 0.0 — imposible de llenar: 29 caracteres
# devuelve un retcode del broker (10014) y 30 o más devuelve `None`. Ese `None` se
# clasificaba como 10012 `NO_RESULT_FROM_TERMINAL`, un fallo de enlace que en realidad
# era un fallo de formato local.
MAX_COMMENT_CHARS = 29

_REQUEST_VALUE_FOR_MASK: dict[int, int] = {
    FILLING_MASK_FOK: ORDER_FILLING_FOK,
    FILLING_MASK_IOC: ORDER_FILLING_IOC,
    FILLING_MASK_RETURN: ORDER_FILLING_RETURN,
}

_MASK_BIT_FOR_POLICY: dict[str, int] = {
    "fok": FILLING_MASK_FOK,
    "ioc": FILLING_MASK_IOC,
    "return": FILLING_MASK_RETURN,
}

# ---------------------------------------------------------------------------
# Retcodes oficiales (`TRADE_RETCODE_*`) con su nombre, categoría y si es seguro
# reintentar el envío sin reconciliar antes. Solo los códigos que garantizan que
# la orden NO llegó al mercado son reintentables.
# ---------------------------------------------------------------------------

_RETCODES: dict[
    int,
    tuple[str, OrderErrorCategory, bool],
] = {
    10004: ("REQUOTE", OrderErrorCategory.RETRYABLE, True),
    10006: ("REJECT", OrderErrorCategory.REJECTED, False),
    10007: ("CANCEL", OrderErrorCategory.REJECTED, False),
    10008: ("PLACED", OrderErrorCategory.NONE, False),
    10009: ("DONE", OrderErrorCategory.NONE, False),
    10010: ("DONE_PARTIAL", OrderErrorCategory.NONE, False),
    10011: ("ERROR", OrderErrorCategory.FATAL, False),
    10012: ("TIMEOUT", OrderErrorCategory.FATAL, False),
    10013: ("INVALID", OrderErrorCategory.INVALID_REQUEST, False),
    10014: ("INVALID_VOLUME", OrderErrorCategory.REJECTED, False),
    10015: ("INVALID_PRICE", OrderErrorCategory.REJECTED, False),
    10016: ("INVALID_STOPS", OrderErrorCategory.REJECTED, False),
    10017: ("TRADE_DISABLED", OrderErrorCategory.ALGO_DISABLED, False),
    10018: ("MARKET_CLOSED", OrderErrorCategory.REJECTED, False),
    10019: ("NO_MONEY", OrderErrorCategory.REJECTED, False),
    10020: ("PRICE_CHANGED", OrderErrorCategory.RETRYABLE, True),
    10021: ("PRICE_OFF", OrderErrorCategory.REJECTED, False),
    10022: ("INVALID_EXPIRATION", OrderErrorCategory.INVALID_REQUEST, False),
    10023: ("ORDER_CHANGED", OrderErrorCategory.NONE, False),
    10024: ("TOO_MANY_REQUESTS", OrderErrorCategory.RETRYABLE, True),
    10025: ("NO_CHANGES", OrderErrorCategory.NONE, False),
    10026: ("SERVER_DISABLES_AT", OrderErrorCategory.ALGO_DISABLED, False),
    10027: ("CLIENT_DISABLES_AT", OrderErrorCategory.ALGO_DISABLED, False),
    10028: ("LOCKED", OrderErrorCategory.REJECTED, False),
    10029: ("FROZEN", OrderErrorCategory.REJECTED, False),
    10030: ("INVALID_FILL", OrderErrorCategory.INVALID_REQUEST, False),
    10031: ("CONNECTION", OrderErrorCategory.FATAL, False),
    10032: ("ONLY_REAL", OrderErrorCategory.REJECTED, False),
    10033: ("LIMIT_ORDERS", OrderErrorCategory.REJECTED, False),
    10034: ("LIMIT_VOLUME", OrderErrorCategory.REJECTED, False),
    10035: ("INVALID_ORDER", OrderErrorCategory.INVALID_REQUEST, False),
    10036: ("POSITION_CLOSED", OrderErrorCategory.REJECTED, False),
    10038: ("INVALID_CLOSE_VOLUME", OrderErrorCategory.REJECTED, False),
    10039: ("CLOSE_ORDER_EXIST", OrderErrorCategory.REJECTED, False),
    10040: ("LIMIT_POSITIONS", OrderErrorCategory.REJECTED, False),
    10041: ("REJECT_CANCEL", OrderErrorCategory.REJECTED, False),
    10042: ("LONG_ONLY", OrderErrorCategory.REJECTED, False),
    10043: ("SHORT_ONLY", OrderErrorCategory.REJECTED, False),
    10044: ("CLOSE_ONLY", OrderErrorCategory.REJECTED, False),
    10045: ("FIFO_CLOSE", OrderErrorCategory.REJECTED, False),
}

# El envío pudo haber llegado al mercado: hay que reconciliar antes de reintentar.
_AMBIGUOUS_RETCODES = frozenset({10011, 10012, 10031})

# Éxito: orden ejecutada (total o parcialmente).
_RETCODE_DONE = 10009
_RETCODE_DONE_PARTIAL = 10010


class RetcodeNameNotFound(Exception):
    """El retcode devuelto no está en el diccionario oficial (caso raro en MT5 5.x)."""


class FillingNotAvailable(Exception):
    """El símbolo no permite ningún filling utilizable por el bot."""


def is_ambiguous(retcode: int) -> bool:
    """True si el retcode deja la ejecución en estado desconocido (hay que reconciliar)."""
    return int(retcode) in _AMBIGUOUS_RETCODES


def is_success(retcode: int) -> bool:
    """True si el broker confirmó el llenado (total o parcial)."""
    return int(retcode) in (_RETCODE_DONE, _RETCODE_DONE_PARTIAL)


def resolve_filling(spec: SymbolSpec, *, policy: str = "auto") -> int:
    """Devuelve el VALOR de `type_filling` que acepta el símbolo; nunca asume FOK.

    `spec.allowed_fillings` es el bitmask de `symbol_info().filling_mode` (1 FOK /
    2 IOC / 4 RETURN). Este mapa de bits se usa solo para ELEGIR; lo que se envía al
    terminal es el ORDER_FILLING_* correspondiente (0 / 1 / 2). Mandar el bit en lugar
    del valor produce `TRADE_RETCODE_INVALID_FILL` (10030) en cada orden.

    Preferencia con `policy="auto"`: RETURN > IOC > FOK. RETURN es el más seguro para
    una entrada a mercado (si no se llena, devuelve error en lugar de dejar un parcial
    colgando); en Pepperstone el símbolo solo reporta IOC, así que se cae en IOC.
    Una política explícita ("ioc"/"fok"/"return") se respeta solo si el símbolo la
    permite; si no, se degrada al siguiente filling permitido.
    """
    allowed = int(spec.allowed_fillings)
    preferred = [_MASK_BIT_FOR_POLICY[policy]] if policy in _MASK_BIT_FOR_POLICY else []
    fallback = [FILLING_MASK_RETURN, FILLING_MASK_IOC, FILLING_MASK_FOK]
    for bit in (*preferred, *fallback):
        if allowed & bit:
            return _REQUEST_VALUE_FOR_MASK[bit]
    raise FillingNotAvailable(
        f"el símbolo {spec.resolved_name!r} no reporta ningún filling permitido "
        f"(bitmask {allowed}); no se puede enviar una orden a mercado"
    )


def _classify(retcode: int) -> tuple[str, OrderErrorCategory, bool]:
    """Clasifica un retcode: (nombre, categoría, es reintentable sin reconciliar).

    Un código fuera de la tabla se trata como `FATAL` (no reintentar): puede ser un
    retcode de un build más nuevo cuyo significado real desconocemos, y reintentar a
    ciegas una orden que sí llegó al mercado duplicaría la posición.
    """
    return _RETCODES.get(
        retcode, (f"UNKNOWN_{retcode}", OrderErrorCategory.FATAL, False)
    )


def send_order(
    mt5: Any,
    request: OrderRequest,
    spec: SymbolSpec,
    *,
    filling: int,
    deviation: int,
    comment: str,
) -> OrderResult:
    """Envía la orden a mercado (TRADE_ACTION_DEAL) y clasifica el retcode.

    El módulo `mt5` se recibe por parámetro (lo aporta `Mt5Connection`, que ya hace el
    import perezoso): así este archivo nunca importa `MetaTrader5` al importarse.

    Campos de la orden según la API de MT5:
      - action: TRADE_ACTION_DEAL (a mercado, abrir o cerrar)
      - type: ORDER_TYPE_BUY / ORDER_TYPE_SELL
      - sl/tp: redondeados a `digits`; `tp` se OMITE si no hay objetivo (la API rechaza
        un `tp` nulo explícito)
      - deviation: desviación máxima en puntos respecto al precio solicitado
      - magic/comment: trazabilidad del bot (ver `MAX_COMMENT_CHARS`: el tope útil es 29)
    """
    if request.symbol != spec.resolved_name and request.symbol != spec.requested_name:
        return OrderResult(
            retcode=10013,
            retcode_name="INVALID_REQUEST",
            order_ticket=None,
            deal_ticket=None,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.INVALID_REQUEST,
            price=None,
            raw={"error": f"símbolo {request.symbol!r} no coincide con {spec.resolved_name!r}"},
        )

    request_dict: dict[str, Any] = {
        "action": TRADE_ACTION_DEAL,
        "symbol": request.symbol,
        "type": ORDER_TYPE_BUY if request.side is Side.BUY else ORDER_TYPE_SELL,
        "volume": round(float(request.volume), 2),
        "price": 0.0,  # 0 = al precio de mercado vigente (bid/ask según el lado)
        "deviation": int(deviation),
        "magic": int(request.magic),
        "comment": comment[:MAX_COMMENT_CHARS],
        "type_filling": int(filling),
    }
    # SL obligatorio en el dominio; TP opcional y se omite si no existe.
    if request.sl is not None:
        request_dict["sl"] = round(float(request.sl), spec.digits)
    if request.tp is not None:
        request_dict["tp"] = round(float(request.tp), spec.digits)

    started = time.perf_counter()
    result = mt5.order_send(request_dict)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if result is None:
        # Sin respuesta del terminal: la orden PUDO llegar al mercado. Se reporta como
        # ambiguo (TIMEOUT) para que la capa de ejecución reconcilie por deals antes de
        # hacer cualquier cosa, en lugar de reintentar y duplicar la posición.
        return OrderResult(
            retcode=10012,
            retcode_name="NO_RESULT_FROM_TERMINAL",
            order_ticket=None,
            deal_ticket=None,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.FATAL,
            price=None,
            raw={
                "request": request_dict,
                "error": str(mt5.last_error()),
                "latency_ms": round(elapsed_ms, 1),
                "ambiguous": True,
            },
        )

    retcode = int(result.retcode)
    name, category, retryable = _classify(retcode)
    order_ticket = int(result.order) if getattr(result, "order", 0) else None
    deal_ticket = int(result.deal) if getattr(result, "deal", 0) else None
    price_value = float(result.price) if getattr(result, "price", 0.0) else None

    return OrderResult(
        retcode=retcode,
        retcode_name=name,
        order_ticket=order_ticket,
        deal_ticket=deal_ticket,
        request_id=int(getattr(result, "request_id", 0)) or None,
        retryable=retryable,
        category=category,
        price=price_value,
        raw={
            "request": request_dict,
            "retcode": retcode,
            "retcode_name": name,
            "order": order_ticket,
            "deal": deal_ticket,
            "price": price_value,
            "comment": str(getattr(result, "comment", comment)),
            "latency_ms": round(elapsed_ms, 1),
        },
    )


def modify_position(
    mt5: Any,
    position_id: int,
    *,
    sl: float | None,
    tp: float | None,
    digits: int,
) -> OrderResult:
    """Modifica SL/TP de una posición (TRADE_ACTION_SLTP).

    `TRADE_ACTION_SLTP` identifica la posición por su ticket en `position` y NO admite
    filling ni precio: solo `sl`/`tp`. El terminal aplica el PAR completo, así que un
    campo que aquí falte viaja como 0 y el broker lo lee como "borrar ese stop"; por eso
    el adaptador rellena el lado ausente con el valor vivo antes de llamar. Enviar `sl`
    solo, como se hacía, dejó en demo una posición sin take profit.
    """
    request_dict: dict[str, Any] = {
        "action": TRADE_ACTION_SLTP,
        "position": int(position_id),
        "deviation": 0,
        "comment": "goldbot",
    }
    if sl is not None:
        request_dict["sl"] = round(float(sl), digits)
    if tp is not None:
        request_dict["tp"] = round(float(tp), digits)

    started = time.perf_counter()
    result = mt5.order_send(request_dict)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if result is None:
        # Modificar SL/TP es idempotente: si no hay respuesta, la posición conserva los
        # stops anteriores y la siguiente vela reintenta la gestión. No es ambiguo.
        return OrderResult(
            retcode=10013,
            retcode_name="NO_RESULT_FROM_TERMINAL",
            order_ticket=None,
            deal_ticket=None,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.INVALID_REQUEST,
            price=None,
            raw={"request": request_dict, "error": str(mt5.last_error())},
        )

    retcode = int(result.retcode)
    name, category, retryable = _classify(retcode)

    return OrderResult(
        retcode=retcode,
        retcode_name=name,
        order_ticket=int(getattr(result, "order", 0)) or None,
        deal_ticket=None,
        request_id=int(getattr(result, "request_id", 0)) or None,
        retryable=retryable,
        category=category,
        price=None,
        raw={
            "request": request_dict,
            "retcode": retcode,
            "retcode_name": name,
            "latency_ms": round(elapsed_ms, 1),
        },
    )


def close_position(
    mt5: Any,
    *,
    ticket: int,
    symbol: str,
    side: Side,
    volume: float,
    filling: int,
    deviation: int,
    magic: int,
    digits: int,
    comment: str,
) -> OrderResult:
    """Cierra (total o parcialmente) una posición con un DEAL en el lado opuesto.

    Cerrar en MT5 es enviar `TRADE_ACTION_DEAL` con `position=<ticket>` y el `type`
    CONTRARIO al de la posición abierta; el broker empareja y liquida. Se envía
    explícitamente `sl=0`/`tp=0` para que el cierre no arrastre los niveles viejos.

    Recibe `ticket` (no `position_id`): `positions_get().ticket` es el identificador que
    acepta `order_send.position`, y en MT5 de mercado ambos coinciden para la posición.
    """
    request_dict: dict[str, Any] = {
        "action": TRADE_ACTION_DEAL,
        "position": int(ticket),
        "symbol": symbol,
        "type": ORDER_TYPE_SELL if side is Side.BUY else ORDER_TYPE_BUY,
        "volume": round(float(volume), 2),
        "price": 0.0,
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment[:MAX_COMMENT_CHARS],
        "type_filling": int(filling),
        "sl": 0.0,
        "tp": 0.0,
    }
    _ = digits  # el cierre no fija SL/TP nuevos; se mantiene por claridad de contrato

    started = time.perf_counter()
    result = mt5.order_send(request_dict)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if result is None:
        # Igual que en la entrada: sin respuesta no sabemos si el cierre se ejecutó.
        # La posición sigue abierta (o ya se cerró); reconciliar antes de reintentar.
        return OrderResult(
            retcode=10012,
            retcode_name="NO_RESULT_FROM_TERMINAL",
            order_ticket=None,
            deal_ticket=None,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.FATAL,
            price=None,
            raw={
                "request": request_dict,
                "error": str(mt5.last_error()),
                "latency_ms": round(elapsed_ms, 1),
                "ambiguous": True,
            },
        )

    retcode = int(result.retcode)
    name, category, retryable = _classify(retcode)

    return OrderResult(
        retcode=retcode,
        retcode_name=name,
        order_ticket=int(getattr(result, "order", 0)) or None,
        deal_ticket=int(getattr(result, "deal", 0)) or None,
        request_id=int(getattr(result, "request_id", 0)) or None,
        retryable=retryable,
        category=category,
        price=float(result.price) if getattr(result, "price", 0.0) else None,
        raw={
            "request": request_dict,
            "retcode": retcode,
            "retcode_name": name,
            "latency_ms": round(elapsed_ms, 1),
        },
    )
