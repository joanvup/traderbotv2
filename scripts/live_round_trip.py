"""Prueba de la ruta de orden contra el terminal real (cuenta DEMO).

Sirve para el paso que la Fase 4 no pudo cerrar un sábado: el llenado IOC real, la
verificación posterior del SL/TP en la posición y, más tarde, su reconciliación cuando
el bot en vivo vea el deal. No sustituye al núcleo de decisión: la señal se construye a
mano sobre precios reales (ATR de la última vela cerrada) y se ejecuta por el MISMO
`ExecutionEngine`, con el MISMO magic y los mismos candados.

Por defecto NO envía nada: imprime el snapshot, el lote, la orden y los avisos de
`validate_order`. Hay que añadir `--send` para tocar el mercado.

    .venv/Scripts/python.exe -X utf8 scripts/live_round_trip.py --config config/config.example.yaml
    .venv/Scripts/python.exe -X utf8 scripts/live_round_trip.py --config config/config.example.yaml --send

`--allow-stack` apila sobre una posición propia; existe para la prueba de gestión (§12.11),
que necesita dos posiciones vivas a la vez. El lote NO lo fija la prueba: lo recalcula el
`ExecutionEngine` con el RiskManager, y `--max-lots` es sólo un techo que aborta antes de
enviar si ese cálculo se pasa de lo que la prueba quiere asumir.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC

from goldbot.config import compute_config_hash, load_config, load_environment
from goldbot.domain.context import ContextBuilder
from goldbot.domain.risk import AtrRiskManager, validate_order
from goldbot.domain.types import (
    OrderRequest,
    PolicyDecision,
    Regime,
    RiskSettings,
    Selection,
    Side,
    Signal,
    Timeframe,
)
from goldbot.engine.analysis import build_context_settings
from goldbot.engine.data_service import build_broker, build_clock
from goldbot.engine.execution import ExecutionEngine
from goldbot.engine.guards import assert_algo_trading, assert_demo_account, assert_feed_fresh
from goldbot.infra.logging_setup import configure_logging
from goldbot.infra.state_store import SqliteStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live_round_trip", description="Prueba de la ruta de orden en cuenta DEMO"
    )
    parser.add_argument("--config", required=True, help="ruta al YAML de configuración")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy", help="lado de la señal")
    parser.add_argument(
        "--atr-mult", type=float, default=1.0, help="distancia del stop en múltiplos del ATR"
    )
    parser.add_argument("--rr", type=float, default=2.0, help="múltiplo del stop para el TP")
    parser.add_argument(
        "--max-lots", type=float, default=0.01, help="techo de lote para la prueba de humo"
    )
    parser.add_argument(
        "--allow-stack",
        action="store_true",
        help="permite abrir aunque ya haya una posición propia (prueba de gestión con dos vivas)",
    )
    parser.add_argument(
        "--send", action="store_true", help="ENVÍA la orden al broker (por defecto no)"
    )
    parser.add_argument("--level", default="WARNING", help="nivel de logging de consola")
    return parser


def _candles_for(broker, config, clock, now):
    """Velas cerradas de todos los marcos configurados, con el warmup del YAML."""
    decision = Timeframe(config.data.decision_timeframe)
    timeframes = tuple(
        dict.fromkeys(
            Timeframe(name)
            for name in (decision.value, *config.data.entry_timeframes, *config.data.context_timeframes)
        )
    )
    warmup = {Timeframe(name): count for name, count in config.data.warmup_bars.items()}
    return {
        timeframe: broker.get_candles(
            config.mt5.symbol, timeframe, warmup[timeframe], closed_only=True, as_of_utc=now
        )
        for timeframe in timeframes
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment()
    config = load_config(args.config)
    configure_logging(config.app.log_dir, level=args.level, console=True)

    clock = build_clock(config)
    broker = build_broker(config, clock)
    now = clock.now_utc()

    health = broker.connect()
    account = broker.account()
    print(f"cuenta {account.login} · {account.server} · {'DEMO' if account.is_demo else 'REAL'}")
    print(f"equity {account.equity:.2f} {account.currency} · margen libre {account.margin_free:.2f}")
    print(
        f"terminal conectado={health.connected} enlace={health.terminal_connected} "
        f"algo={health.algo_trading_enabled} latencia={health.latency_ms:.1f} ms"
    )

    # Los dos candados duros del bot, antes de calcular nada.
    assert_demo_account(account, allow_live=config.safety.allow_live)
    assert_algo_trading(health)

    spec = broker.symbol_spec(config.mt5.symbol)
    print(
        f"spec {spec.resolved_name}: digits {spec.digits} point {spec.point} "
        f"contract {spec.contract_size} lote mín {spec.volume_min} step {spec.volume_step} "
        f"tick_value {spec.trade_tick_value_profit}"
    )

    decision_tf = Timeframe(config.data.decision_timeframe)
    snapshot = ContextBuilder(
        build_context_settings(config),
        stale_after_s={
            Timeframe(name): float(seconds) for name, seconds in config.data.stale_after_s.items()
        },
    ).build(
        _candles_for(broker, config, clock, now),
        now_utc=now,
        decision_timeframe=decision_tf,
        session=clock.session_at(now),
        config_hash=compute_config_hash(config),
        model_versions={"live_round_trip": "smoke-1"},
    )
    tick = broker.get_last_tick(config.mt5.symbol)
    print(
        f"vela de decisión {snapshot.bar_time_utc:%Y-%m-%dT%H:%M}Z · sesión "
        f"{snapshot.session.value} · ATR {snapshot.timing.atr:.2f} · "
        f"tick {tick.time_utc:%H:%M:%S}Z bid {tick.bid} ask {tick.ask} "
        f"spread {tick.spread_points:.0f} pts"
    )

    # La misma puerta de frescura que usa el bot: si falla, no hay que enviar nada.
    assert_feed_fresh(
        snapshot.freshness,
        decision_timeframe=snapshot.decision_timeframe,
        tick_age_s=(clock.now_utc() - tick.time_utc).total_seconds(),
        max_tick_age_s=config.data.max_feed_age_s,
    )
    propios = broker.positions(config.app.magic)
    print(f"posiciones propias abiertas con magic {config.app.magic}: {len(propios)}")
    if propios and args.send and not args.allow_stack:
        print("RECHAZADO: hay posición propia abierta; la prueba de humo no apila operaciones.")
        print("       (usa --allow-stack para apilar a propósito, p. ej. la prueba de gestión)")
        return 3
    if propios and args.send:
        print(
            f"APILANDO sobre {len(propios)} posición(es) propia(s): "
            f"{sorted(p.ticket for p in propios)}"
        )

    side = Side.BUY if args.side == "buy" else Side.SELL
    entry = (tick.ask if side is Side.BUY else tick.bid)
    stop_distance = max(snapshot.timing.atr * args.atr_mult, spec.point * 10)
    sign = -1.0 if side is Side.BUY else 1.0
    stop_loss = round(entry + sign * stop_distance, spec.digits)
    take_profit = round(entry - sign * stop_distance * args.rr, spec.digits)
    signal = Signal(
        signal_id=f"smoke-{now.strftime('%Y%m%d%H%M%S')}",
        playbook_id="live_round_trip",
        side=side,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_distance=stop_distance,
        confidence=1.0,
        regime_at_signal=Regime.UNKNOWN,
        created_at_utc=now,
        valid_until_bar_utc=None,
        invalidation_price=None,
        rationale="prueba de la ruta de orden (no es una señal de la estrategia)",
        inputs_hash=None,
    )
    risk_manager = AtrRiskManager(
        RiskSettings(
            risk_pct=config.risk.risk_pct,
            daily_loss_pct=config.risk.daily_loss_pct,
            max_trades_day=config.risk.max_trades_day,
            equity_floor_usd=config.risk.equity_floor_usd,
        )
    )
    risk = risk_manager.size(signal, snapshot, account, spec)
    if not risk.approved or risk.lots is None:
        print(f"RECHAZADO por el RiskManager: {risk.reason}")
        return 3
    lots = risk.lots
    if lots > args.max_lots:
        print(
            f"RECHAZADO: el lote calculado ({lots}) supera el techo {args.max_lots} de la prueba."
        )
        print(
            "       El tamaño lo decide el RiskManager (1 % del equity / distancia de stop) y "
            "la prueba no lo baja a la fuerza: se baja ensanchando el stop (--atr-mult)."
        )
        return 3

    decision_id = f"smoke-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    pct_actual = 0.0 if risk.risk_pct_actual is None else risk.risk_pct_actual
    presupuesto = 0.0 if risk.risk_money is None else risk.risk_money
    dinero_riesgo = lots * spec.contract_size * stop_distance
    request = OrderRequest(
        client_order_id=f"{decision_id}-{signal.signal_id[:8]}",
        symbol=spec.resolved_name,
        side=side,
        volume=lots,
        sl=signal.stop_loss,
        tp=signal.take_profit,
        magic=config.app.magic,
        deviation_points=config.execution.deviation_points,
        filling=None,
        comment=f"{signal.playbook_id}|{signal.signal_id[:10]}",
        decision_id=decision_id,
    )
    issues = validate_order(
        request, spec, tick, require_sl=True, require_tp=signal.take_profit is not None
    )
    print(
        f"orden: {side.value} {lots} {request.symbol} @ {entry:.2f} "
        f"SL {stop_loss:.2f} TP {take_profit:.2f} magic {request.magic} · "
        f"riesgo real {dinero_riesgo:.2f} USD "
        f"({pct_actual:.2f} % del equity, presupuesto {presupuesto:.2f} USD)"
    )
    print(
        "       (el lote es el que recalcula el RiskManager dentro de ExecutionEngine; "
        "la prueba no lo puede cambiar)"
    )
    for issue in issues:
        print(f"  validate_order [{issue.severity}] {issue.message}")

    if not args.send:
        print("DRY RUN: no se envió ninguna orden. Añade --send para tocar el mercado.")
        return 0

    execution = ExecutionEngine(
        broker,
        risk_manager,
        clock,
        max_retries=config.execution.max_retries,
        retry_backoff_s=config.execution.retry_backoff_s,
        deviation_points=config.execution.deviation_points,
    )
    decision = PolicyDecision(
        selection=Selection(
            playbook_id="live_round_trip",
            scores={"live_round_trip": 1.0},
            rationale="prueba de la ruta de orden",
            fallback_no_trade=False,
        ),
        signal=signal,
        filters=None,
        action="ENTER",
        rationale=request.comment,
    )
    result = execution.execute_signal(
        decision, snapshot, account, spec, tick, magic=config.app.magic, decision_id=decision_id
    )
    retcode = None if result.result is None else result.result.retcode
    retcode_name = None if result.result is None else result.result.retcode_name
    print(f"placed={result.placed} retcode={retcode} ({retcode_name}) · {result.message}")
    previas = {position.ticket for position in propios}
    abiertas = [p for p in broker.positions(config.app.magic) if p.ticket not in previas]
    for position in abiertas:
        print(
            f"  posición {position.position_id} {position.side.value} {position.volume} @"
            f"{position.entry_price:.2f} SL {position.sl} TP {position.tp} "
            f"pnl {position.pnl_money:.2f} R {position.r_multiple:.2f}"
        )
    if not result.placed:
        raw = {} if result.result is None else dict(result.result.raw or {})
        print(
            f"  raw del terminal: last_error={raw.get('error')!r} "
            f"latencia={raw.get('latency_ms')} ms request={raw.get('request')}"
        )
        print("La orden NO quedó confirmada con SL/TP: revisar antes de dejar el bot solo.")
        return 4
    # Foto en el StateStore, igual que hace `Bot._register_opened_position`. Sin esta fila
    # el bot gestiona la posición (las lee del broker) pero `_reconcile_closed` sólo cierra
    # el ledger sobre las que conoce, y el deal de `Cerrar todo` no aparecería en `trades`.
    # No se pisa `initial_risk_price`: el que trae la lectura del broker es `abs(sl - fill)`,
    # el riesgo REAL desde el precio de llenado; la distancia pedida (`stop_distance`) puede
    # diferir por el deslizamiento y sesgaría el R de la gestión.
    store = SqliteStateStore(config.app.db_path, clock)
    for position in abiertas:
        store.upsert_position(replace(position, decision_id_entry=decision_id))
    print(
        f"registrada en {config.app.db_path} (tickets {sorted(p.ticket for p in abiertas)}). "
        "Siguiente paso: `Cerrar todo` desde el panel debe conciliar el deal en "
        "`trades`/`daily_stats`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
