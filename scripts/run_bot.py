"""Punto de entrada del bot. Fase 1-3: solo lectura y backtest. Fase 4: ejecución DEMO.

Modo por defecto (sin `--execute`): igual que antes, solo datos, contexto y régimen,
sin enviar órdenes. Con `--execute`: además decide y ejecuta en cuenta DEMO (entradas
con SL/TP, gestión, kill switch), exigiendo todos los candados de seguridad.
"""

from __future__ import annotations

import argparse
import logging

from goldbot.config import load_config, load_environment
from goldbot.engine.data_service import build_clock, build_data_service
from goldbot.engine.guards import GuardViolation, assert_algo_trading, assert_demo_account
from goldbot.infra.logging_setup import attach_state_store, configure_logging
from goldbot.infra.state_store import SqliteDailyLedger, SqliteStateStore

LOGGER = logging.getLogger("goldbot.run_bot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_bot", description="Bot XAUUSD para MT5 demo")
    parser.add_argument("--config", required=True, help="ruta al YAML de configuración")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="segundos entre sondeos")
    parser.add_argument("--cycles", type=int, default=None, help="número de ciclos (por defecto, infinito)")
    parser.add_argument("--once", action="store_true", help="arranca, sondea una vez y sale")
    parser.add_argument("--skip-history", action="store_true", help="no descarga histórico al arrancar")
    parser.add_argument(
        "--no-analysis", action="store_true", help="desactiva contexto y régimen (solo datos)"
    )
    parser.add_argument("--execute", action="store_true", help="HABILITA ejecución DEMO (Fase 4)")
    parser.add_argument("--level", default="INFO", help="nivel de logging (DEBUG, INFO, ...)")
    parser.add_argument("--no-console", action="store_true", help="solo escribe el JSONL")
    return parser


def _print_regime(service) -> None:
    analysis = getattr(service, "analysis", None)
    if analysis is None:
        return
    regime = analysis.regime
    snapshot = analysis.snapshot
    print(
        f"Régimen: {regime.label.value} (confianza {regime.confidence:.2f}, "
        f"{regime.confirmed_bars} velas) · sesión {snapshot.session.value} · "
        f"ATR {snapshot.timing.atr:.2f} · hash {snapshot.snapshot_hash[:12]}"
    )


def _build_bot(config, clock, store, broker, service):
    """Construye el `Bot` completo de la Fase 4 si se pide ejecución."""
    from pathlib import Path

    from goldbot.domain.decision_log import JsonlDecisionLogger
    from goldbot.domain.risk import AtrRiskManager
    from goldbot.domain.trade_manager import AtrStructureTradeManager
    from goldbot.domain.types import ManagementSettings, RiskSettings
    from goldbot.engine.bot import Bot
    from goldbot.engine.decisions import build_decision_core
    from goldbot.engine.execution import ExecutionEngine
    from goldbot.engine.kill_switch import KillSwitchSettings, LatchedKillSwitch
    from goldbot.infra.calendar import CachedEconomicCalendar, CsvBridgeEconomicCalendar

    # Candado duro de la especificación: sin estas dos comprobaciones el bot NO se
    # construye ni una sola vez, aunque el YAML traiga `--execute`.
    assert_demo_account(broker.account(), allow_live=config.safety.allow_live)
    assert_algo_trading(broker.health())

    daily = SqliteDailyLedger(config.app.db_path, clock=clock)

    calendar = CachedEconomicCalendar(
        CsvBridgeEconomicCalendar(
            config.filters.calendar_bridge_path,
            max_age_s=config.filters.calendar_max_age_s,
        ),
        Path(config.app.data_dir) / "cache" / "calendar.json",
    )
    core = build_decision_core(config, calendar=calendar, clock=clock, daily=daily)

    risk_manager = AtrRiskManager(
        RiskSettings(
            risk_pct=config.risk.risk_pct,
            daily_loss_pct=config.risk.daily_loss_pct,
            max_trades_day=config.risk.max_trades_day,
            equity_floor_usd=config.risk.equity_floor_usd,
        )
    )
    trade_manager = AtrStructureTradeManager(
        ManagementSettings(
            breakeven_at_r=config.management.breakeven_at_r,
            partial_at_r=config.management.partial_at_r,
            partial_close_pct=config.management.partial_close_pct,
            trailing_atr_mult=config.management.trailing_atr_mult,
            time_stop_bars=config.management.time_stop_bars,
            regime_exit_confidence=config.management.regime_exit_confidence,
            atr_sl_mult=config.management.atr_sl_mult,
        )
    )
    spec = broker.symbol_spec(config.mt5.symbol)
    trade_manager.set_symbol_limits(
        spec_point=spec.point,
        volume_step=spec.volume_step,
        volume_min=spec.volume_min,
    )

    kill_switch = LatchedKillSwitch(
        KillSwitchSettings(
            equity_floor_usd=config.risk.equity_floor_usd,
            max_consecutive_execution_errors=config.safety.max_consecutive_execution_errors,
            missing_grace_s=config.safety.missing_grace_s,
            min_free_margin_pct=config.safety.min_free_margin_pct,
            disconnect_tolerance_polls=config.safety.disconnect_tolerance_polls,
        )
    )

    execution = ExecutionEngine(
        broker,
        risk_manager,
        clock,
        max_retries=config.execution.max_retries,
        retry_backoff_s=config.execution.retry_backoff_s,
        deviation_points=config.execution.deviation_points,
    )

    decision_logger = JsonlDecisionLogger(config.app.log_dir / "decisions.jsonl")

    return Bot(
        config=config,
        broker=broker,
        store=store,
        clock=clock,
        trade_manager=trade_manager,
        decision_logger=decision_logger,
        kill_switch=kill_switch,
        core=core,
        execution=execution,
        analysis_provider=lambda: service.analysis,
        data_poll=service.refresh_bars,
        daily=daily,
        magic=config.app.magic,
        symbol=config.mt5.symbol,
        spec=spec,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_environment()
    config = load_config(args.config)
    configure_logging(config.app.log_dir, level=args.level, console=not args.no_console)

    clock = build_clock(config)
    store = SqliteStateStore(config.app.db_path, clock)
    attach_state_store(store)
    service = build_data_service(
        config, clock=clock, store=store, enable_analysis=not args.no_analysis
    )

    bot = None
    try:
        report = service.startup(download_history=not args.skip_history)

        if args.execute:
            bot = _build_bot(config, clock, store, service.broker, service)
            bot.restore_kill_latch()
            print(
                "EJECUCIÓN DEMO HABILITADA (Fase 4). Cuenta DEMO verificada; "
                "el bot puede abrir y gestionar posiciones."
            )
            LOGGER.warning("EJECUCIÓN DEMO HABILITADA: el bot puede operar en cuenta DEMO.")

        print(
            f"Conectado: cuenta {report.account_login} "
            f"({'DEMO' if report.is_demo else 'NO DEMO'}) · símbolo {report.resolved_symbol} "
            f"· offset servidor {report.server_offset_min:+d} min"
        )
        if report.bars_cached:
            bars = ", ".join(f"{name}={count}" for name, count in report.bars_cached.items())
            print(f"Velas cerradas en caché: {bars}")
        _print_regime(service)

        if args.once:
            result = service.poll_once()
            feed = ", ".join(f"{item.timeframe.value}={item.status.value}" for item in result.feed)
            tick = "n/d" if result.tick_age_s is None else f"{result.tick_age_s:.0f} s"
            print(
                f"Ciclo {result.sequence}: feed {result.overall_status.value} "
                f"(tick {tick}) · {feed}"
            )
            _print_regime(service)
            if bot is not None and service.analysis is not None:
                cycle = bot.run_cycle(service.analysis)
                print(
                    f"Ciclo de decisión/ejecución Fase 4: {cycle.action} "
                    f"({', '.join(cycle.actions_taken)})"
                )
        else:
            LOGGER.info("bucle de datos iniciado (poll %.1f s)", args.poll_interval)
            if bot is not None:
                LOGGER.info("bucle Fase 4 con ejecución iniciado (poll %.1f s)", args.poll_interval)
                bot.run_forever(poll_interval_s=args.poll_interval, max_cycles=args.cycles)
            else:
                service.run(poll_interval_s=args.poll_interval, max_cycles=args.cycles)
    except GuardViolation as exc:
        LOGGER.error("candado de seguridad: %s", exc)
        print(f"EJECUCIÓN RECHAZADA: {exc}")
        return 3
    except KeyboardInterrupt:
        LOGGER.info("interrumpido por el operador")
    except Exception:
        LOGGER.exception("el servicio de datos se detuvo por un error")
        return 2
    finally:
        service.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())