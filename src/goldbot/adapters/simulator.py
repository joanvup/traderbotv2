"""Broker simulado para la Fase 4: mismas interfaces que `BrokerAdapter`, sin terminal.

Este simulador deja probar la ejecución completa (entradas, SL/TP, cierre parcial,
reconciliación de deals) sin tocar MT5, con un reloj inyectable y determinista. Es
intercambiable con `Mt5BrokerAdapter`: implementa exactamente el Protocolo
`BrokerAdapter` de `domain/interfaces.py`.

Criterio de simulación (conservador, igual que el backtest de la Fase 3):
- Se decide al cierre de la vela; la entrada se ejecuta al precio del último cierre
  más el spread + slippage en la dirección correcta.
- Si en la misma vela se tocan SL y TP, se asume que gana el SL (nunca el TP).
- Una posición a la vez por defecto; sin promediar, sin grid, sin aumentar lote.
- Comisión por lote y lado, swap = 0 (el backtest tampoco lo modela).
- `account()` siempre reporta `is_demo=True`.

El reloj es inyectable y se usa para todas las marcas de tiempo; si no se pasa ninguno
se usa `datetime.now(UTC)` (para tests conviene inyectar un reloj fijo). Nunca se
importa `MetaTrader5`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from goldbot.domain.types import (
    AccountState,
    BrokerHealth,
    Candle,
    DealRecord,
    OrderErrorCategory,
    OrderRequest,
    OrderResult,
    PositionState,
    Regime,
    Side,
    SymbolSpec,
    Tick,
    Timeframe,
)

NowFn = Callable[[], datetime]


@dataclass
class _SimDeal:
    """Registro interno de un deal del simulador (para `closed_deals` y reconciliación)."""

    deal: DealRecord


@dataclass
class _SimPosition:
    """Posición abierta del simulador, con los campos que el dominio necesita."""

    ticket: int
    position_id: int
    side: Side
    volume: float
    entry_price: float
    sl: float | None
    tp: float | None
    playbook_id: str = ""
    regime_at_entry: Regime | None = None
    decision_id_entry: str | None = None
    breakeven_done: bool = False
    partials_done: int = 0
    last_action: str | None = None
    opened_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    initial_risk_price: float = 0.0
    # Precio actual del último cierre procesado (para PnL flotante).
    current_price: float = 0.0


class SimulatorBrokerAdapter:
    """Reutiliza el mismo núcleo de decisión con spread, comisión y slippage configurados.

    Constructor:
        candles: velas cerradas por timeframe (para `get_candles` causal).
        spec: especificación del símbolo (XAUUSD real de Pepperstone en tests).
        initial_balance: balance de arranque de la cuenta simulada.
        clock: reloj inyectable; si es None se usa `datetime.now(UTC)`.
        slippage_points / commission_per_lot_side_usd: costes configurados.
    """

    def __init__(
        self,
        candles: Mapping[Timeframe, Sequence[Candle]],
        spec: SymbolSpec,
        *,
        initial_balance: float,
        clock: NowFn | None = None,
        slippage_points: float = 2.0,
        commission_per_lot_side_usd: float = 3.5,
        account_login: int = 0,
        server: str = "simulator",
    ) -> None:
        self._candles = candles
        self._spec = spec
        self._initial_balance = initial_balance
        self._now = clock or (lambda: datetime.now(UTC))
        self._slippage_points = slippage_points
        self._commission_per_lot_side_usd = commission_per_lot_side_usd
        self._account_login = account_login
        self._server = server
        self._balance = initial_balance
        self._positions: dict[int, _SimPosition] = {}
        self._deals: list[_SimDeal] = []
        self._next_ticket = 1
        self._connected = False
        self._algo_enabled = True
        # Última vela procesada por `advance` (para derivar el tick y el precio actual).
        self._last_bar: Candle | None = None
        # Registro de órdenes enviadas (para `closed_deals` de reconciliación).
        self._order_deals: dict[int, DealRecord] = {}

    # -- vida ----------------------------------------------------------------

    def connect(self) -> BrokerHealth:
        self._connected = True
        return self.health()

    def shutdown(self) -> None:
        self._connected = False

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=self._connected,
            terminal_connected=self._connected,
            algo_trading_enabled=self._algo_enabled,
            account_trade_allowed=True,
            latency_ms=0.0,
            last_error=None if self._connected else "simulador desconectado",
            checked_at_utc=self._now(),
        )

    def account(self) -> AccountState:
        equity = self._balance + self._floating_pnl()
        return AccountState(
            login=self._account_login,
            server=self._server,
            currency=self._spec.currency_profit or "USD",
            balance=self._balance,
            equity=equity,
            margin_free=equity,
            leverage=100,
            is_demo=True,
            trade_allowed=True,
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol not in (self._spec.resolved_name, self._spec.requested_name):
            raise LookupError(f"símbolo {symbol!r} no es el del simulador ({self._spec.resolved_name!r})")
        return self._spec

    def server_time(self) -> datetime:
        return self._now()

    # -- datos ---------------------------------------------------------------

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        *,
        closed_only: bool = True,
        as_of_utc: datetime | None = None,
    ) -> Sequence[Candle]:
        """Devuelve velas cerradas del timeframe, cortadas por `as_of_utc`.

        Requisito causal duro: con `closed_only=True` (siempre en el bot) solo se
        devuelven velas cuyo `close_time_utc <= as_of_utc`, y nunca la vela en formación.
        """
        series = self._candles.get(timeframe, ())
        if closed_only:
            series = [c for c in series if c.is_closed]
        if as_of_utc is not None:
            series = [c for c in series if c.close_time_utc <= as_of_utc]
        return tuple(series[-count:] if count > 0 else series)

    def get_last_tick(self, symbol: str) -> Tick:
        """Deriva bid/ask de la última vela cerrada conocida con el spread de esa vela.

        La hora del tick es el reloj inyectado, no el cierre de la vela: un tick es la
        cotización que llega *ahora*. Usar el cierre habría hecho que el simulador se
        viera siempre con un feed atrasado ante el guard de frescura de la entrada.
        """
        bar = self._last_bar or self._last_closed()
        if bar is None:
            raise RuntimeError("simulador sin velas para derivar tick")
        point = self._spec.point or 1.0
        mid = (bar.open + bar.close) / 2.0
        spread = max(bar.spread_points * point, self._spec.point)
        bid = mid - spread / 2.0
        ask = mid + spread / 2.0
        return Tick(
            time_utc=self._now(),
            bid=round(bid, self._spec.digits),
            ask=round(ask, self._spec.digits),
            last=bar.close,
            spread_points=bar.spread_points,
        )

    def _last_closed(self) -> Candle | None:
        # El marco de decisión del bot es M5 por defecto; usamos cualquier timeframe
        # con velas cerradas y elegimos la más reciente.
        best: Candle | None = None
        for candles in self._candles.values():
            for candle in candles:
                if not candle.is_closed:
                    continue
                if best is None or candle.close_time_utc > best.close_time_utc:
                    best = candle
        return best

    # -- posiciones ----------------------------------------------------------

    def positions(self, magic: int) -> Sequence[PositionState]:
        """Posiciones abiertas propias con precio actual y PnL en USD y en R."""
        result: list[PositionState] = []
        for sim in self._positions.values():
            result.append(
                PositionState(
                    ticket=sim.ticket,
                    position_id=sim.position_id,
                    side=sim.side,
                    volume=sim.volume,
                    entry_price=sim.entry_price,
                    current_price=sim.current_price,
                    sl=sim.sl,
                    tp=sim.tp,
                    pnl_money=self._position_pnl(sim),
                    initial_risk_price=sim.initial_risk_price,
                    r_multiple=self._position_r(sim),
                    playbook_id=sim.playbook_id,
                    regime_at_entry=sim.regime_at_entry,
                    decision_id_entry=sim.decision_id_entry,
                    opened_at_utc=sim.opened_at_utc,
                    breakeven_done=sim.breakeven_done,
                    partials_done=sim.partials_done,
                    last_action=sim.last_action,
                )
            )
        return result

    def _position_pnl(self, pos: _SimPosition) -> float:
        direction = 1.0 if pos.side is Side.BUY else -1.0
        return (pos.current_price - pos.entry_price) * direction * self._spec.contract_size * pos.volume

    def _position_r(self, pos: _SimPosition) -> float:
        if pos.initial_risk_price <= 0.0:
            return 0.0
        direction = 1.0 if pos.side is Side.BUY else -1.0
        return (pos.current_price - pos.entry_price) * direction / pos.initial_risk_price

    def _floating_pnl(self) -> float:
        return sum(self._position_pnl(pos) for pos in self._positions.values())

    # -- ejecución -----------------------------------------------------------

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Abre una posición a mercado contra el último precio conocido.

        Ejecuta al precio de la última vela cerrada más el spread + slippage en la
        dirección correcta (BUY paga ask, SELL cobra bid). Rechaza (retcode de error,
        sin excepción) si la cuenta está desconectada, el volumen no respeta el
        `volume_step`, o el símbolo no coincide.
        """
        if not self._connected:
            return self._error(10013, "NO_RESULT_FROM_TERMINAL", "simulador desconectado", request)
        if request.symbol not in (self._spec.resolved_name, self._spec.requested_name):
            return self._error(10013, "INVALID_REQUEST", f"símbolo {request.symbol!r} inválido", request)

        # Volumen alineado al step y dentro de límites.
        step = self._spec.volume_step
        aligned = round(request.volume / step) * step
        if abs(aligned - request.volume) > 1e-9:
            return self._error(10013, "INVALID_VOLUME", "volumen no múltiplo del step", request)
        if request.volume < self._spec.volume_min or request.volume > self._spec.volume_max:
            return self._error(10013, "INVALID_VOLUME", "volumen fuera de límites", request)
        if request.sl is None:
            return self._error(10013, "INVALID_STOPS", "SL obligatorio en el simulador", request)

        bar = self._last_bar or self._last_closed()
        if bar is None:
            return self._error(10013, "NO_PRICE", "sin velas para ejecutar", request)

        point = self._spec.point
        slippage = self._slippage_points * point
        spread = max(bar.spread_points * point, point)
        entry = bar.close
        if request.side is Side.BUY:
            fill = entry + spread + slippage
        else:
            fill = entry - slippage

        ticket = self._next_ticket
        self._next_ticket += 1
        now = self._now()
        sim = _SimPosition(
            ticket=ticket,
            position_id=ticket,
            side=request.side,
            volume=request.volume,
            entry_price=fill,
            sl=request.sl,
            tp=request.tp,
            playbook_id=request.comment.split("|")[0] if request.comment else "",
            decision_id_entry=request.decision_id,
            opened_at_utc=now,
            initial_risk_price=abs(fill - request.sl),
            current_price=fill,
        )
        self._positions[ticket] = sim
        # La comisión se registra NEGATIVA, igual que `deal.commission` en MT5: es un
        # cargo. La reconciliación del bot suma `profit + commission`, así que darla
        # positiva convertiría un coste en ganancia e inflaría el PnL del día.
        commission = -self._commission_per_lot_side_usd * request.volume
        self._record_deal(
            DealRecord(
                deal_ticket=ticket * 2,
                position_id=ticket,
                order_ticket=ticket,
                entry="in",
                side=request.side,
                volume=request.volume,
                price=fill,
                profit=0.0,
                commission=commission,
                swap=0.0,
                fee=0.0,
                time_utc=now,
                comment=request.comment,
            )
        )
        self._balance += commission
        return OrderResult(
            retcode=10009,
            retcode_name="TRADE_RETCODE_DONE",
            order_ticket=ticket,
            deal_ticket=ticket * 2,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.NONE,
            price=fill,
            raw={"request_id": ticket, "side": request.side.value, "volume": request.volume},
        )

    def modify_position(
        self,
        position_id: int,
        *,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        """Mueve SL/TP. Regla dura: un SL nunca puede ir en contra de la posición.

        BUY: el SL solo puede subir. SELL: el SL solo puede bajar. Un intento que
        empeora la protección se rechaza con retcode de error (sin excepción).
        """
        sim = self._find(position_id)
        if sim is None:
            return self._error_pos(10013, "INVALID_REQUEST", f"posición {position_id} no encontrada")
        if sl is not None:
            if sim.side is Side.BUY and sim.sl is not None and sl < sim.sl:
                return self._error_pos(10013, "INVALID_STOPS", "el SL de una compra solo puede subir")
            if sim.side is Side.SELL and sim.sl is not None and sl > sim.sl:
                return self._error_pos(10013, "INVALID_STOPS", "el SL de una venta solo puede bajar")
            sim.sl = sl
        if tp is not None:
            sim.tp = tp
        return OrderResult(
            retcode=10009,
            retcode_name="TRADE_RETCODE_DONE",
            order_ticket=None,
            deal_ticket=None,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.NONE,
            price=None,
            raw={"position_id": position_id, "sl": sim.sl, "tp": sim.tp},
        )

    def close_position(self, position_id: int, *, volume: float | None = None) -> OrderResult:
        """Cierra total o parcialmente. Un parcial genera un deal OUT y reduce el volumen."""
        sim = self._find(position_id)
        if sim is None:
            return self._error_pos(10013, "INVALID_REQUEST", f"posición {position_id} no encontrada")
        close_volume = volume if volume is not None else sim.volume
        if close_volume <= 0.0 or close_volume > sim.volume:
            return self._error_pos(10013, "INVALID_VOLUME", "volumen de cierre inválido")
        point = self._spec.point
        slippage = self._slippage_points * point
        bar = self._last_bar or self._last_closed()
        base = bar.close if bar is not None else sim.current_price
        if sim.side is Side.BUY:
            exit_price = base - slippage
        else:
            exit_price = base + slippage

        direction = 1.0 if sim.side is Side.BUY else -1.0
        gross = (exit_price - sim.entry_price) * direction * self._spec.contract_size * close_volume
        commission = -self._commission_per_lot_side_usd * close_volume
        net = gross + commission
        self._balance += net
        now = self._now()
        order_ticket = self._next_ticket
        self._next_ticket += 1
        self._record_deal(
            DealRecord(
                deal_ticket=order_ticket * 2,
                position_id=sim.ticket,
                order_ticket=order_ticket,
                entry="out",
                side=sim.side,
                volume=close_volume,
                price=exit_price,
                profit=gross,
                commission=commission,
                swap=0.0,
                fee=0.0,
                time_utc=now,
                comment="close",
            )
        )
        sim.volume = round(sim.volume - close_volume, 8)
        sim.current_price = exit_price
        if sim.volume <= 1e-9:
            self._positions.pop(sim.ticket, None)
        return OrderResult(
            retcode=10009,
            retcode_name="TRADE_RETCODE_DONE",
            order_ticket=order_ticket,
            deal_ticket=order_ticket * 2,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.NONE,
            price=exit_price,
            raw={"position_id": position_id, "closed_volume": close_volume},
        )

    def close_all(self, magic: int) -> Sequence[OrderResult]:
        """Cierra todas las posiciones propias; devuelve los resultados en orden."""
        results: list[OrderResult] = []
        for ticket in list(self._positions.keys()):
            results.append(self.close_position(ticket))
        return results

    # -- procesamiento de velas ----------------------------------------------

    def advance(self, bar: Candle) -> None:
        """Procesa una vela cerrada: actualiza precios, SL/TP y cierres intra-vela.

        Regla conservadora: si en esta vela se tocan SL y TP, se asume que gana el SL.
        Las posiciones que tocan su SL se cierran al precio del SL (menos slippage);
        las que tocan su TP se cierran al TP. Un cierre parcial interno NO ocurre aquí:
        el parcial lo decide el TradeManager en la siguiente vela de gestión.
        """
        self._last_bar = bar
        for sim in list(self._positions.values()):
            sim.current_price = bar.close
            if sim.side is Side.BUY:
                if sim.sl is not None and bar.low <= sim.sl:
                    self._force_close(sim, sim.sl - self._slippage_points * self._spec.point, "SL")
                elif sim.tp is not None and bar.high >= sim.tp:
                    self._force_close(sim, sim.tp, "TP")
            else:
                if sim.sl is not None and bar.high >= sim.sl:
                    self._force_close(sim, sim.sl + self._slippage_points * self._spec.point, "SL")
                elif sim.tp is not None and bar.low <= sim.tp:
                    self._force_close(sim, sim.tp, "TP")

    def _force_close(self, sim: _SimPosition, exit_price: float, reason: str) -> None:
        """Cierra por SL/TP dentro de la vela, con la comisión del lado de salida."""
        direction = 1.0 if sim.side is Side.BUY else -1.0
        gross = (exit_price - sim.entry_price) * direction * self._spec.contract_size * sim.volume
        commission = -self._commission_per_lot_side_usd * sim.volume
        self._balance += gross + commission
        now = self._now()
        self._record_deal(
            DealRecord(
                deal_ticket=self._next_ticket * 2,
                position_id=sim.ticket,
                order_ticket=self._next_ticket,
                entry="out",
                side=sim.side,
                volume=sim.volume,
                price=exit_price,
                profit=gross,
                commission=commission,
                swap=0.0,
                fee=0.0,
                time_utc=now,
                comment=reason,
            )
        )
        self._next_ticket += 1
        self._positions.pop(sim.ticket, None)

    # -- historial -----------------------------------------------------------

    def closed_deals(
        self,
        *,
        since_utc: datetime,
        until_utc: datetime,
        position_id: int | None = None,
    ) -> Sequence[DealRecord]:
        """Deals del rango solicitado (el rango es inclusivo en ambos extremos)."""
        filtered: list[DealRecord] = []
        for entry in self._deals:
            deal = entry.deal
            if not (since_utc <= deal.time_utc <= until_utc):
                continue
            if position_id is not None and deal.position_id != position_id:
                continue
            filtered.append(deal)
        return filtered

    # -- internos ------------------------------------------------------------

    def _record_deal(self, deal: DealRecord) -> None:
        self._deals.append(_SimDeal(deal=deal))

    def _find(self, position_id: int) -> _SimPosition | None:
        for sim in self._positions.values():
            if sim.ticket == position_id or sim.position_id == position_id:
                return sim
        return None

    def _error(
        self, retcode: int, name: str, message: str, request: OrderRequest
    ) -> OrderResult:
        return OrderResult(
            retcode=retcode,
            retcode_name=name,
            order_ticket=None,
            deal_ticket=None,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.REJECTED,
            price=None,
            raw={"error": message, "symbol": request.symbol, "side": request.side.value},
        )

    def _error_pos(self, retcode: int, name: str, message: str) -> OrderResult:
        return OrderResult(
            retcode=retcode,
            retcode_name=name,
            order_ticket=None,
            deal_ticket=None,
            request_id=None,
            retryable=False,
            category=OrderErrorCategory.REJECTED,
            price=None,
            raw={"error": message},
        )

    # -- utilidades para tests -----------------------------------------------

    @property
    def balance(self) -> float:
        return self._balance

    def simulate_disconnect(self, disconnected: bool) -> None:
        """Simula una caída del terminal para los tests de reconexión del Fase 4."""
        self._connected = not disconnected

    def simulate_algo_off(self, algo_off: bool) -> None:
        """Simula el trading algorítmico deshabilitado (retcode 10027) en los tests."""
        self._algo_enabled = not algo_off