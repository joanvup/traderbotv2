"""Fachada que implementa BrokerAdapter sobre el terminal MT5."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from goldbot.adapters.mt5 import history, symbols
from goldbot.adapters.mt5 import orders as mt5_orders
from goldbot.adapters.mt5.connection import Mt5Connection
from goldbot.domain.interfaces import Clock
from goldbot.domain.types import (
    AccountState,
    BrokerHealth,
    Candle,
    DealRecord,
    OrderErrorCategory,
    OrderRequest,
    OrderResult,
    PositionState,
    Side,
    SymbolSpec,
    Tick,
    Timeframe,
)


class Mt5BrokerAdapter:
    """Traduce el dominio a la API de MT5; nunca expone `MetaTrader5` fuera del adaptador."""

    def __init__(
        self,
        connection: Mt5Connection,
        clock: Clock,
        *,
        deviation_points: int = 20,
        symbol_override: str | None = None,
        magic: int = 0,
        symbol: str = "XAUUSD",
        filling_policy: str = "auto",
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._deviation_points = deviation_points
        self._symbol_override = symbol_override
        self._magic = magic
        self._requested_symbol = symbol
        self._filling_policy = filling_policy
        self._specs: dict[str, SymbolSpec] = {}
        self._resolved_symbol: str | None = None

    def connect(self) -> BrokerHealth:
        return self._connection.connect()

    def shutdown(self) -> None:
        self._connection.shutdown()

    def health(self) -> BrokerHealth:
        return self._connection.health()

    def server_time(self) -> datetime:
        return self._connection.server_time()

    def account(self) -> AccountState:
        info = self._connection.api.account_info()
        if info is None:
            raise RuntimeError("account_info devolvió None")
        return AccountState(
            login=int(info.login),
            server=str(info.server),
            currency=str(info.currency),
            balance=float(info.balance),
            equity=float(info.equity),
            margin_free=float(info.margin_free),
            leverage=int(info.leverage),
            is_demo=int(info.trade_mode) == 0,
            trade_allowed=bool(info.trade_allowed and info.trade_expert),
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        api = self._connection.api
        resolved = symbols.resolve_symbol(api, symbol, self._symbol_override)
        cached = self._specs.get(resolved)
        if cached is not None:
            return cached
        spec = symbols.read_symbol_spec(
            api, resolved, requested=symbol, now_utc=self._clock.now_utc()
        )
        self._specs[resolved] = spec
        self._resolved_symbol = resolved
        return spec

    @property
    def resolved_symbol(self) -> str | None:
        return self._resolved_symbol

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        *,
        closed_only: bool = True,
        as_of_utc: datetime | None = None,
    ) -> Sequence[Candle]:
        resolved = symbols.resolve_symbol(self._connection.api, symbol, self._symbol_override)
        return history.fetch_candles(
            self._connection.api,
            resolved,
            timeframe,
            count,
            self._clock,
            closed_only=closed_only,
            as_of_utc=as_of_utc,
        )

    def get_last_tick(self, symbol: str) -> Tick:
        resolved = symbols.resolve_symbol(self._connection.api, symbol, self._symbol_override)
        raw = self._connection.api.symbol_info_tick(resolved)
        if raw is None or not raw.time:
            raise RuntimeError(f"symbol_info_tick sin datos para {resolved}")
        point = self.symbol_spec(symbol).point
        spread_points = (float(raw.ask) - float(raw.bid)) / point if point else 0.0
        wall = datetime.fromtimestamp(int(raw.time), UTC).replace(tzinfo=None)
        return Tick(
            time_utc=self._clock.to_utc(wall),
            bid=float(raw.bid),
            ask=float(raw.ask),
            last=float(raw.last),
            spread_points=spread_points,
        )

    def positions_count(self) -> int:
        return int(self._connection.api.positions_total())

    def positions(self, magic: int) -> Sequence[PositionState]:
        """Devuelve las posiciones abiertas propias (por `magic`) con precio y PnL actualizados.

        El filtro por magic se hace en Python porque `positions_get` de este build solo
        admite symbol/ticket/group: es la garantía de no gestionar una posición abierta
        a mano o por otro EA en la misma cuenta.
        """
        api = self._connection.api
        positions = api.positions_get()
        result: list[PositionState] = []
        if positions is None:
            return result
        now = self._clock.now_utc()
        for pos in positions:
            if int(pos.magic) != magic:
                continue
            side = Side.BUY if pos.type == 0 else Side.SELL
            entry = float(pos.price_open)
            sl = float(pos.sl) if pos.sl > 0 else None
            tp = float(pos.tp) if pos.tp > 0 else None
            # Este build del paquete `MetaTrader5` (5.0.6231) no expone `commission` en
            # `TradePosition`: la comisión sólo aparece en el deal, así que el PnL de la
            # posición abierta es profit + swap y el neto real llega en la reconciliación.
            pnl = float(pos.profit) + float(pos.swap)
            direction = 1.0 if side is Side.BUY else -1.0
            initial_risk = abs(sl - entry) if sl is not None else 0.0
            r_multiple = (
                (float(pos.price_current) - entry) * direction / initial_risk
                if initial_risk > 0.0
                else 0.0
            )
            # MT5 entrega `time` como epoch de la HORA DE SERVIDOR (no UTC): se convierte
            # con la misma regla que las velas (historia.py), o el time-stop iría 7 h
            # desfasado en Pepperstone.
            opened_wall = datetime.fromtimestamp(int(pos.time), UTC).replace(tzinfo=None)
            result.append(
                PositionState(
                    ticket=int(pos.ticket),
                    # `position` sólo existe en builds nuevas del paquete; en 5.0.6231 el id
                    # de posición es `identifier` (coincide con `ticket` en cuenta hedging).
                    position_id=int(getattr(pos, "position", 0) or pos.identifier),
                    side=side,
                    volume=float(pos.volume),
                    entry_price=entry,
                    current_price=float(pos.price_current),
                    sl=sl,
                    tp=tp,
                    pnl_money=pnl,
                    initial_risk_price=initial_risk,
                    r_multiple=r_multiple,
                    playbook_id="",
                    regime_at_entry=None,
                    decision_id_entry=None,
                    opened_at_utc=self._clock.to_utc(opened_wall, at_utc=now),
                    breakeven_done=False,
                    partials_done=0,
                    last_action=None,
                )
            )
        return result

    def _resolve_filling(self, spec: SymbolSpec) -> int | OrderResult:
        """Elige el `type_filling` permitido por el símbolo o un resultado de rechazo.

        Devuelve el valor para `MqlTradeRequest.type_filling` (ORDER_FILLING_*), no el
        bit del bitmask. Si el símbolo no reporta ninguno, devuelve un OrderResult con
        el retcode 10030 (INVALID_FILL) en lugar de lanzar: la capa de ejecución trata
        los rechazos como datos, no como excepciones.
        """
        try:
            return mt5_orders.resolve_filling(spec, policy=self._filling_policy)
        except mt5_orders.FillingNotAvailable as exc:
            return OrderResult(
                retcode=10030,
                retcode_name="INVALID_FILL",
                order_ticket=None,
                deal_ticket=None,
                request_id=None,
                retryable=False,
                category=OrderErrorCategory.INVALID_REQUEST,
                price=None,
                raw={"error": str(exc), "allowed_fillings": spec.allowed_fillings},
            )

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Envía una orden a mercado a través del adaptador MT5."""
        spec = self.symbol_spec(request.symbol)
        resolved = self._resolve_filling(spec)
        if isinstance(resolved, OrderResult):
            return resolved
        comment = _order_comment(request)
        return mt5_orders.send_order(
            self._connection.api,
            request,
            spec,
            filling=resolved,
            deviation=self._deviation_points,
            comment=comment,
        )

    def modify_position(
        self,
        position_id: int,
        *,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        """Modifica SL/TP de una posición MT5 (TRADE_ACTION_SLTP).

        El terminal aplica el PAR completo: el lado que la petición no lleva viaja como 0 y
        el broker lo lee como "borrar ese stop". En demo el breakeven de la 414168566 pidió
        sólo `sl` y la posición perdió su TP 3896,34. Por eso cada lado ausente se rellena
        con el valor vivo antes de enviar, y si la posición no se puede leer se aborta la
        modificación: quedarse sin stop contradice la regla de operar siempre con SL.
        """
        if sl is None or tp is None:
            viva = self._find_position(position_id)
            if viva is None:
                return OrderResult(
                    retcode=10013,
                    retcode_name="POSITION_NOT_READABLE",
                    order_ticket=None,
                    deal_ticket=None,
                    request_id=None,
                    retryable=True,
                    category=OrderErrorCategory.RETRYABLE,
                    price=None,
                    raw={"position": position_id, "sl": sl, "tp": tp},
                )
            sl = viva.sl if sl is None else sl
            tp = viva.tp if tp is None else tp
        spec = self.symbol_spec(self._requested_symbol)
        return mt5_orders.modify_position(
            self._connection.api,
            position_id,
            sl=sl,
            tp=tp,
            digits=spec.digits,
        )

    def close_position(self, position_id: int, *, volume: float | None = None) -> OrderResult:
        """Cierra (total o parcialmente) una posición MT5 (TRADE_ACTION_DEAL).

        `position_id` es el ticket de la posición (el identificador que acepta
        `order_send.position`). El cierre requiere conocer el lado y el símbolo de la
        posición; lo leemos de `positions_get` para enviar el DEAL en el lado opuesto.
        """
        spec = self.symbol_spec(self._requested_symbol)
        resolved = self._resolve_filling(spec)
        if isinstance(resolved, OrderResult):
            return resolved
        pos = self._find_position(position_id)
        if pos is None:
            return OrderResult(
                retcode=10035,
                retcode_name="INVALID_ORDER",
                order_ticket=None,
                deal_ticket=None,
                request_id=None,
                retryable=False,
                category=OrderErrorCategory.INVALID_REQUEST,
                price=None,
                raw={"error": f"posición {position_id} no encontrada para cerrar"},
            )
        return mt5_orders.close_position(
            self._connection.api,
            ticket=pos.ticket,
            symbol=spec.resolved_name,
            side=pos.side,
            volume=volume if volume is not None else pos.volume,
            filling=resolved,
            deviation=self._deviation_points,
            magic=self._magic,
            digits=spec.digits,
            comment="goldbot close",
        )

    def close_all(self, magic: int) -> Sequence[OrderResult]:
        """Cierra todas las posiciones propias (por `magic`), de mayor a menor volumen.

        La Fase 5 la llama desde el dashboard; la Fase 4 puede usarla para el cierre
        de emergencia. Si el terminal está desconectado, devuelve errores sin lanzar.
        """
        try:
            positions = self.positions(magic)
        except Exception:
            return []
        # Ordenar por volumen descendente para cerrar primero las más grandes y
        # liberar margen; el orden en MT5 no afecta la concurrencia porque cada
        # cierre se hace en el único hilo del bot.
        ordered = sorted(positions, key=lambda p: p.volume, reverse=True)
        results: list[OrderResult] = []
        for pos in ordered:
            results.append(self.close_position(pos.ticket, volume=None))
        return results

    def _deal_time_utc(self, raw_epoch: int) -> datetime:
        """Convierte el epoch de un deal de MT5 (reloj de servidor) a UTC real.

        La API entrega los `time` como segundos desde 1970 interpretando la hora LOCAL
        DEL SERVIDOR como si fuera UTC; `fromtimestamp(..., UTC)` devuelve ese
        reloj-naïve y `to_utc` le aplica el offset (+7 h en Pepperstone). Sin esta
        corrección el PnL realizado caería en el trading day equivocado y la
        reconciliación buscaría en la ventana incorrecta.
        """
        wall = datetime.fromtimestamp(int(raw_epoch), UTC).replace(tzinfo=None)
        return self._clock.to_utc(wall, at_utc=self._clock.now_utc())

    def closed_deals(
        self,
        *,
        since_utc: datetime,
        until_utc: datetime,
        position_id: int | None = None,
    ) -> Sequence[DealRecord]:
        """Deals cerrados en el rango UTC solicitado.

        MT5 `history_deals_get` pide fechas en hora de servidor (naïve); las convertimos
        a UTC y recuperamos un margen extra de una vela para compensar el desfase de
        procesamiento. La conversión usa la regla `to_utc` del reloj del bot.

        En la build 5.0.6231 la variante por ventana de fecha devuelve vacío si el
        terminal no tiene ese histórico cacheado; la reconciliación siempre pasa
        `position_id`, que es la forma que funciona y la que se usa en vivo.
        """
        api = self._connection.api
        now = self._clock.now_utc()
        # Pedimos un rango un poco más amplio por si hay desfase de relleno de velas.
        window_before = self._clock.to_utc(
            since_utc - timedelta(minutes=5), at_utc=now
        )
        window_after = self._clock.to_utc(
            until_utc + timedelta(minutes=5), at_utc=now
        )
        from_dt = self._clock.to_server(window_before)
        to_dt = self._clock.to_server(window_after)
        if position_id is not None:
            # `history_deals_get(date_from, date_to)` no devuelve nada en las builds
            # recientes si la ventana no coincide al minuto con el histórico cacheado del
            # terminal; la búsqueda por posición es exacta y es la que usa la
            # reconciliación. Devuelve None si el terminal aún no tiene el deal cargado.
            deals = api.history_deals_get(position=int(position_id))
        else:
            deals = api.history_deals_get(from_dt, to_dt)
        if deals is None:
            return ()
        result: list[DealRecord] = []
        for deal in deals:
            if not deal.time:
                continue
            if int(getattr(deal, "magic", self._magic) or self._magic) != self._magic:
                # Deal de otro EA o abierto a mano en la misma cuenta: no es nuestro.
                continue
            # `TradeDeal` expone `ticket` y `position_id`; `id`/`position` sólo existen en
            # builds antiguas. Leerlos a ciegas lanzaba AttributeError y la reconciliación
            # se tragaba el error sin escribir nunca el trade.
            deal_ticket = int(getattr(deal, "ticket", 0) or getattr(deal, "id", 0))
            deal_position = int(
                getattr(deal, "position_id", 0) or getattr(deal, "position", 0) or deal_ticket
            )
            ts = self._deal_time_utc(int(deal.time))
            if not (window_before <= ts <= window_after):
                continue
            if position_id is not None and deal_position != int(position_id):
                continue
            result.append(
                DealRecord(
                    deal_ticket=deal_ticket,
                    position_id=deal_position,
                    order_ticket=int(deal.order) if deal.order else None,
                    entry=_parse_entry(int(deal.entry)),
                    side=Side.BUY if deal.type == 0 else Side.SELL,
                    volume=float(deal.volume),
                    price=float(deal.price),
                    profit=float(deal.profit),
                    commission=float(deal.commission),
                    swap=float(deal.swap),
                    fee=float(deal.fee),
                    time_utc=ts,
                    comment=str(deal.comment),
                )
            )
        return result

    def _find_position(self, position_id: int) -> PositionState | None:
        """Devuelve la posición propia (por ticket) o None; el cierre la necesita para
        conocer el lado y el volumen residual."""
        try:
            for pos in self.positions(self._magic):
                if pos.ticket == position_id or pos.position_id == position_id:
                    return pos
        except Exception:
            return None
        return None


def _parse_entry(entry_code: int) -> str:
    """Traduce el código numérico de entrada de un deal MT5 a una etiqueta legible.

    La API devuelve 0=DEAL_ENTRY_IN (apertura), 1=DEAL_ENTRY_OUT (cierre) y
    2=DEAL_ENTRY_INOUT (cerrado contra apertura). Cualquier otro valor se etiqueta
    como desconocido para auditar sin lanzar.
    """
    return {0: "in", 1: "out", 2: "inout"}.get(entry_code, f"unknown-{entry_code}")


def _order_comment(request: OrderRequest) -> str:
    """Comment de la entrada, dentro de los 29 caracteres que acepta el terminal.

    Cabe poco, así que se conserva el `decision_id` entero (es la clave con la que el
    deal se cruza con la decisión al conciliar) y se recorta el playbook, que ya queda
    registrado en `decisions`. El comment del broker es trazabilidad humana, no el
    identificador de referencia.
    """
    limit = mt5_orders.MAX_COMMENT_CHARS
    decision_id = request.decision_id
    if not request.comment:
        return decision_id[:limit]
    budget = limit - len(decision_id) - 1
    if budget <= 0:
        return decision_id[:limit]
    return f"{request.comment[:budget]}|{decision_id}"
