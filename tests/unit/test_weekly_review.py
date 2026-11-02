"""Pruebas de la revisión semanal (Fase 7): agregados, auditorías de candados e informe.

Ninguna prueba abre MT5: se rellena la misma base que llenaría el bot y se mira qué responde la
revisión. Lo que se comprueba es doble —que los agregados cuadran con lo escrito y que **ningún
candado duro de la especificación puede pasar por alto** (2 operaciones/día, SL obligatorio, 1 %
de riesgo, demo, kill switch, pérdida diaria, suelo de equity).

La honestidad del informe también es un contrato: si no hubo decisiones debe decirlo, y si la
base está en modo lectura la revisión no puede escribirla.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.unit._snapshots import NOW, account, regime, signal, snapshot

from goldbot.config import AppConfig
from goldbot.domain.types import (
    DecisionRecord,
    EventRecord,
    EventSource,
    EventType,
    ExitReason,
    ExitReasonSource,
    FilterCode,
    FilterOutcome,
    FilterResult,
    LogLevel,
    PositionState,
    Regime,
    RiskDecision,
    Side,
    TradeRecord,
)
from goldbot.infra.state_store import SqliteDailyLedger, SqliteStateStore
from goldbot.infra.time import UtcClock
from goldbot.review.report import WeeklyReportBuilder, weekly_review_to_jsonable
from goldbot.review.weekly import (
    WeeklyReview,
    WeeklyReviewCollector,
)

MAGIC = 20261030
LOGIN = 61623438
DAY = NOW.date()


def _clock() -> UtcClock:
    from tests.unit._snapshots import clock

    return clock()


# ---------------------------------------------------------------------------
# Relleno de la base
# ---------------------------------------------------------------------------


def _store(db_path: Path) -> SqliteStateStore:
    store = SqliteStateStore(db_path, _clock())
    store.initialize()
    return store


def _write_state(
    store: SqliteStateStore,
    *,
    mode: str = "DEMO",
    is_demo: bool = True,
    equity: float = 1_000.0,
    kill_latched: bool = False,
    kill_reason: str | None = None,
) -> None:
    store.write_bot_state(
        {
            "state": "OPERATIVO",
            "paused": False,
            "mode": mode,
            "resolved_symbol": "XAUUSD",
            "magic": MAGIC,
            "account": asdict(account(equity=equity, is_demo=is_demo)),
            "feed_status": "OK",
            "last_cycle_at_utc": NOW,
            "cycle_seq": 42,
            "last_heartbeat_utc": NOW,
            "kill_switch_latched": kill_latched,
            "kill_reason": kill_reason,
            "config_hash": "cfg-test",
            "app_version": "0.1.0",
            "server_offset_min": 180,
        }
    )


def _risk(pct: float | None, *, approved: bool = True) -> RiskDecision:
    return RiskDecision(
        approved=approved,
        lots=0.02 if approved else None,
        risk_money=1_000.0 * pct / 100 if pct is not None else None,
        risk_pct_actual=pct,
        sl_distance_price=4.12,
        sl_distance_points=412.0,
        reason="dimensionado al riesgo configurado",
        spec_hash="spec-test",
        equity_at_decision=1_000.0,
    )


def _filters(*blocked: FilterCode) -> FilterOutcome:
    results = tuple(
        FilterResult(code, True, 40.0, 30.0, f"bloqueo por {code.value}") for code in blocked
    )
    return FilterOutcome(
        results=results,
        blocked=bool(results),
        primary_reason=results[0].message if results else None,
    )


def _decision(
    decision_id: str,
    *,
    action: str,
    playbook: str = "trend_pullback",
    ts: datetime = NOW,
    bar: datetime | None = None,
    confidence: float = 0.82,
    filters: FilterOutcome | None = None,
    risk: RiskDecision | None = None,
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        cycle_id=decision_id,
        ts_utc=ts,
        bar_time_utc=bar if bar is not None else ts - timedelta(minutes=5),
        action=action,
        snapshot=snapshot(now=ts),
        regime=regime(Regime.TREND_UP, confidence=confidence, now=ts),
        playbook_id=playbook,
        signal=signal(now=ts) if action == "ENTER" else None,
        filters=filters,
        risk=risk,
        rationale=f"{action} de prueba",
        latency_ms=90,
        config_hash="cfg-test",
        model_versions={"rules": "rules-1"},
    )


def _trade(
    position_id: int,
    *,
    net: float,
    reason: ExitReason = ExitReason.SL,
    closed_at: datetime = NOW - timedelta(hours=3),
    sl: float | None = 2_396.0,
    regime_at_entry: Regime | None = Regime.TREND_UP,
) -> TradeRecord:
    return TradeRecord(
        position_id=position_id,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=0.02,
        entry_price=2_400.0,
        exit_price=2_400.0 + net / 25.0,
        sl=sl,
        tp=2_408.0,
        open_time_utc=closed_at - timedelta(hours=2),
        close_time_utc=closed_at,
        entry_deal_id=position_id * 10,
        close_deal_id=position_id * 10 + 1,
        commission=-0.14,
        swap=0.0,
        net_profit=net,
        r_multiple=round(net / 8.0, 3),
        duration_s=2 * 3600,
        exit_reason=reason,
        exit_reason_source=ExitReasonSource.SL if reason is ExitReason.SL else ExitReasonSource.BOT,
        playbook_id="trend_pullback",
        regime_at_entry=regime_at_entry,
        decision_id_entry=f"dec-{position_id}",
        reconciled_at_utc=closed_at,
    )


def _event(
    store: SqliteStateStore,
    type_: EventType,
    *,
    ts: datetime = NOW,
    message: str = "evento de prueba",
    level: LogLevel = LogLevel.INFO,
) -> None:
    store.write_event(
        EventRecord(
            ts_utc=ts,
            source=EventSource.BOT,
            type=type_,
            level=level,
            message=message,
            payload={"magic": MAGIC},
        )
    )


def _equity(
    store: SqliteStateStore,
    stamps: list[datetime],
    *,
    equity: float = 1_000.0,
    margin_free: float | None = None,
) -> None:
    for stamp in stamps:
        store.write_equity_snapshot(
            account_login=LOGIN,
            ts_utc=stamp,
            balance=equity,
            equity=equity,
            margin_free=equity if margin_free is None else margin_free,
            floating_pnl=0.0,
            positions_count=0,
            source="PERIODIC",
        )


def _collect(db_path: Path, **kwargs: Any) -> WeeklyReview:
    return WeeklyReviewCollector(AppConfig(), db_path=db_path, now_utc=NOW, **kwargs).collect()


def _finding(review: WeeklyReview, code: str):
    matches = [finding for finding in review.audits if finding.code == code]
    assert matches, f"falta la auditoría {code}"
    return matches[0]


def _healthy_db(tmp_path: Path) -> Path:
    """Base mínima que pasa todos los candados: una entrada con riesgo correcto y SL puesto."""
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1), NOW])
    store.write_decision(
        _decision(
            "dec-1",
            action="ENTER",
            ts=NOW - timedelta(hours=1),
            bar=NOW - timedelta(hours=1, minutes=5),
            filters=_filters(),
            risk=_risk(0.98),
        )
    )
    store.write_trade(_trade(900_001, net=7.86, reason=ExitReason.TP))
    store.upsert_position(
        PositionState(
            ticket=900_002,
            position_id=900_002,
            side=Side.BUY,
            volume=0.02,
            entry_price=2_400.0,
            current_price=2_402.0,
            sl=2_396.0,
            tp=2_408.0,
            pnl_money=4.0,
            initial_risk_price=4.0,
            r_multiple=0.5,
            playbook_id="trend_pullback",
            regime_at_entry=Regime.TREND_UP,
            decision_id_entry="dec-1",
            opened_at_utc=NOW - timedelta(minutes=30),
            breakeven_done=False,
            partials_done=0,
            last_action="HOLD",
        )
    )
    return db_path


# ---------------------------------------------------------------------------
# Agregados
# ---------------------------------------------------------------------------


def test_agrega_dia_decisiones_y_bloqueos_por_filtro(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    day1 = NOW - timedelta(hours=20)
    day2 = NOW - timedelta(hours=2)
    store.write_decision(
        _decision(
            "a1",
            action="BLOCKED",
            playbook="breakout",
            ts=day1,
            filters=_filters(FilterCode.NEWS),
        )
    )
    store.write_decision(
        _decision("a2", action="ENTER", ts=day2, risk=_risk(0.97), filters=_filters())
    )
    store.write_decision(
        _decision("a3", action="NO_SETUP", playbook="range_fade", ts=day2, confidence=0.44)
    )

    review = _collect(db_path)

    assert review.decisions_total == 3
    assert review.actions == {"BLOCKED": 1, "ENTER": 1, "NO_SETUP": 1}
    assert review.blocked_by_filter == {"NEWS": 1}
    assert review.playbooks_selected == {"breakout": 1, "trend_pullback": 1, "range_fade": 1}
    assert review.decision_days == 2
    assert review.entry_days == 1
    assert review.no_trade_days_pct == pytest.approx(50.0)
    assert review.low_confidence_decisions == 1
    assert review.risk_pct_actual_max == pytest.approx(0.97)
    assert {day.trading_day for day in review.days} == {
        _clock().trading_day(day1),
        _clock().trading_day(day2),
    }
    later = [day for day in review.days if day.trading_day == _clock().trading_day(day2)]
    assert later[0].decisions == 2 and later[0].entries == 1


def test_la_revision_no_escribe_en_la_base_del_bot(tmp_path: Path) -> None:
    db_path = _healthy_db(tmp_path)
    before = db_path.read_bytes()

    _collect(db_path)

    assert db_path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            connection.execute("insert into events (ts_utc) values ('x')")
        finally:
            connection.close()


def test_reutiliza_las_metricas_del_backtest_sobre_las_operaciones_cerradas(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    store.write_decision(_decision("d1", action="ENTER", risk=_risk(1.0)))
    store.write_trade(_trade(1, net=16.0, reason=ExitReason.TP))
    store.write_trade(_trade(2, net=-8.0, reason=ExitReason.SL))
    store.write_trade(_trade(3, net=-8.0, reason=ExitReason.SL))

    review = _collect(db_path)

    assert len(review.trades) == 3
    assert review.group.trades == 3
    assert review.group.win_rate_pct == pytest.approx(100.0 / 3.0)
    assert review.group.net_profit_usd == pytest.approx(0.0)
    assert review.exit_reasons == {"TP": 1, "SL": 2}
    assert review.exit_sources == {"SL": 2, "BOT": 1}
    assert review.regimes_at_entry == {"TREND_UP": 3}
    assert review.r_distribution["<=-1"] == 2
    assert review.r_distribution[">=2"] == 1


def test_sin_decisiones_la_revision_lo_dice_claro(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])

    review = _collect(db_path)

    assert review.decisions_total == 0
    assert review.decision_days == 0
    assert review.no_trade_days_pct == 0.0
    assert _finding(review, "REGISTRO").status == "WARN"
    assert any("Sin decisiones en la ventana" in note for note in review.notes)


# ---------------------------------------------------------------------------
# Auditorías de candados
# ---------------------------------------------------------------------------


def test_mas_de_dos_entradas_en_un_dia_es_fallo(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    for index in range(3):
        store.write_decision(
            _decision(
                f"e{index}",
                action="ENTER",
                ts=NOW - timedelta(minutes=index + 1),
                bar=NOW - timedelta(minutes=index + 1),
                risk=_risk(0.9),
            )
        )

    review = _collect(db_path)

    assert _finding(review, "MAX_ENTRADAS_DIA").status == "FALLO"
    assert "3 entrada(s)" in _finding(review, "MAX_ENTRADAS_DIA").observed


def test_una_decisión_ENTER_rechazada_por_riesgo_no_cuenta_como_entrada(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    for index in range(3):
        store.write_decision(
            _decision(
                f"r{index}",
                action="ENTER",
                risk=_risk(None, approved=False),
            )
        )

    review = _collect(db_path)

    assert review.decisions_total == 3
    assert sum(day.entries for day in review.days) == 0
    assert _finding(review, "MAX_ENTRADAS_DIA").status == "OK"
    assert _finding(review, "RIESGO_POR_OPERACION").status == "WARN"


def test_riesgo_por_encima_del_configurado_es_fallo(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    store.write_decision(_decision("big", action="ENTER", risk=_risk(2.4)))

    review = _collect(db_path)

    finding = _finding(review, "RIESGO_POR_OPERACION")
    assert finding.status == "FALLO"
    assert "2.4000 %" in finding.observed


def test_posicion_sin_stop_es_fallo(tmp_path: Path) -> None:
    db_path = _healthy_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("update positions set sl = null")
        connection.execute("update trades set sl = null")

    review = _collect(db_path)

    finding = _finding(review, "SL_OBLIGATORIO")
    assert finding.status == "FALLO"
    assert "900001" in finding.detail and "900002" in finding.detail


def test_cuenta_que_no_es_demo_es_fallo(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store, mode="LIVE", is_demo=False)
    _equity(store, [NOW - timedelta(minutes=1)])

    review = _collect(db_path)

    assert _finding(review, "DEMO").status == "FALLO"
    assert review.failed_audits


def test_kill_switch_con_entrada_posterior_es_fallo(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store, kill_latched=True, kill_reason="EQUITY_FLOOR")
    _equity(store, [NOW - timedelta(minutes=1)])
    _event(store, EventType.KILL_SWITCH, ts=NOW - timedelta(hours=2), level=LogLevel.ERROR)
    store.write_decision(
        _decision(
            "after",
            action="ENTER",
            ts=NOW - timedelta(hours=1),
            bar=NOW - timedelta(hours=1),
            risk=_risk(0.5),
        )
    )

    review = _collect(db_path)

    assert "entradas después del kill=1" in _finding(review, "KILL_SWITCH").observed
    assert _finding(review, "KILL_SWITCH").status == "FALLO"
    assert _finding(review, "EQUITY_FLOOR").status == "OK"


def test_el_margen_libre_sano_pasa_el_candado(tmp_path: Path) -> None:
    review = _collect(_healthy_db(tmp_path))

    assert _finding(review, "MARGEN_LIBRE").status == "OK"


def test_entrar_con_el_margen_por_debajo_del_umbral_es_fallo(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    # 10 % de margen libre sobre 1 000 USD de equity: umbral configurado 30 %.
    _equity(store, [NOW - timedelta(hours=2)], margin_free=100.0)
    _equity(store, [NOW - timedelta(minutes=1)], margin_free=950.0)
    store.write_decision(
        _decision(
            "con_poco_margen",
            action="ENTER",
            ts=NOW - timedelta(hours=1),
            bar=NOW - timedelta(hours=1, minutes=5),
            filters=_filters(),
            risk=_risk(0.6),
        )
    )

    review = _collect(db_path)
    finding = _finding(review, "MARGEN_LIBRE")

    assert finding.status == "FALLO"
    assert "1 entrada(s) con el margen ya por debajo del umbral" in finding.detail


def test_el_margen_bajo_sin_entradas_es_aviso_y_sin_dato_tambien(tmp_path: Path) -> None:
    bajo = tmp_path / "bajo.db"
    store = _store(bajo)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)], margin_free=100.0)
    assert _finding(_collect(bajo), "MARGEN_LIBRE").status == "WARN"

    sin_dato = tmp_path / "sin_dato.db"
    store = _store(sin_dato)
    _write_state(store)
    # Snapshot sin columna de margen: no se puede afirmar que el candado se cumplió.
    store.write_equity_snapshot(
        account_login=LOGIN,
        ts_utc=NOW,
        balance=1_000.0,
        equity=1_000.0,
        margin_free=None,
        floating_pnl=0.0,
        positions_count=0,
        source="PERIODIC",
    )
    sin_dato_finding = _finding(_collect(sin_dato), "MARGEN_LIBRE")
    assert sin_dato_finding.status == "WARN"
    assert "no se puede auditar el margen" in sin_dato_finding.detail


def test_kill_switch_sin_entradas_solo_avisa(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store, kill_latched=True, kill_reason="TERMINAL_DISCONNECTED")
    _equity(store, [NOW - timedelta(minutes=1)])
    _event(store, EventType.KILL_SWITCH, ts=NOW - timedelta(hours=2), level=LogLevel.ERROR)

    review = _collect(db_path)

    assert _finding(review, "KILL_SWITCH").status == "WARN"
    assert not review.failed_audits


def test_candado_diario_latcheado_se_informa_como_aviso(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    daily = SqliteDailyLedger(db_path, clock=_clock())
    daily.ensure_day(DAY, 1_000.0)
    daily.record_pnl(DAY, -25.0)
    daily.mark_daily_lock(DAY)

    review = _collect(db_path)

    assert _finding(review, "CANDADO_DIARIO").status == "WARN"
    assert [day.daily_lock_latched for day in review.days if day.trading_day == DAY] == [True]


def test_un_hueco_largo_entre_dias_de_mercado_es_fallo(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(hours=6), NOW - timedelta(hours=3)])
    store.write_decision(_decision("d", action="NO_TRADE", ts=NOW - timedelta(hours=1)))

    review = _collect(db_path)

    fatal = [gap for gap in review.gaps if not gap.tolerated]
    assert [gap.duration_s for gap in fatal] == [3 * 3600.0]
    assert _finding(review, "HUECOS_FEED").status == "FALLO"


def test_el_hueco_de_fin_de_semana_se_tolera(tmp_path: Path) -> None:
    # 2026-07-10 es viernes: el mercado cierra el sábado y reabre el lunes, así que las 54 h
    # sin snapshot no son una caída del enlace y la revisión no debe marcarla.
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(
        store,
        [
            datetime(2026, 7, 10, 21, 0, tzinfo=NOW.tzinfo),
            datetime(2026, 7, 13, 3, 0, tzinfo=NOW.tzinfo),
        ],
    )

    review = _collect(db_path)

    assert [gap.duration_s for gap in review.gaps] == [54 * 3600.0]
    assert all(gap.tolerated for gap in review.gaps)
    assert _finding(review, "HUECOS_FEED").status == "OK"


def test_sin_snapshots_de_equity_el_hueco_no_puede_comprobarse(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    store.write_decision(_decision("d", action="NO_TRADE"))

    review = _collect(db_path)

    assert review.equity_points == 0
    assert _finding(review, "HUECOS_FEED").status == "FALLO"


def test_retcode_erroneo_y_reconexiones_dejan_rastro(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    store.record_order_attempt(
        ts_utc=NOW - timedelta(minutes=5),
        decision_id="d1",
        request={"action": "TRADE_ACTION_DEAL", "volume": 0.02},
        retcode=10016,
        retcode_name="TRADE_RETCODE_INVALID_VOLUME",
        retryable=False,
        result=None,
    )
    for index in range(3):
        _event(store, EventType.RECONNECT, ts=NOW - timedelta(minutes=index))
    _event(store, EventType.ERROR, ts=NOW - timedelta(minutes=2), level=LogLevel.ERROR)
    store.write_log_line(
        NOW - timedelta(minutes=2), LogLevel.ERROR, "goldbot.engine.bot", "order_send falló"
    )

    review = _collect(db_path)

    assert review.order_attempts == 1
    assert review.order_failures == 1
    assert review.retcodes == {"TRADE_RETCODE_INVALID_VOLUME": 1}
    assert _finding(review, "RETCODES").status == "WARN"
    assert _finding(review, "RECONEXIONES").status == "WARN"
    assert _finding(review, "ERRORES").status == "WARN"
    assert review.log_levels == {"ERROR": 1}


def test_dos_filas_sobre_la_misma_vela_de_decision_avisan_sin_romper_el_informe(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    bar = NOW - timedelta(minutes=5)
    store.write_decision(_decision("p1", action="NO_TRADE", ts=NOW, bar=bar))
    store.write_decision(_decision("p2", action="NO_TRADE", ts=NOW, bar=bar))

    review = _collect(db_path)

    assert review.duplicated_bars == 1
    assert _finding(review, "DECISIONES_DUPLICADAS").status == "WARN"
    # Un `--once` repetido no duplica el resultado económico ni la cuenta de días.
    assert review.decision_days == 1


def test_reinicios_y_eventos_notables_se_cuentan(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    for index in range(2):
        _event(store, EventType.STARTUP, ts=NOW - timedelta(hours=index))
    _event(store, EventType.PAUSE, ts=NOW - timedelta(minutes=10), message="pausa desde el panel")

    review = _collect(db_path)

    assert review.restarts == 2
    assert review.events_by_type == {"STARTUP": 2, "PAUSE": 1}
    assert [event["type"] for event in review.notable_events] == ["PAUSE"]


def test_base_inexistente_no_se_crea_ni_se_intenta_revisar(tmp_path: Path) -> None:
    missing = tmp_path / "nunca" / "state.db"

    with pytest.raises(FileNotFoundError):
        WeeklyReviewCollector(AppConfig(), db_path=missing, now_utc=NOW).collect()


def test_ventana_invalida_se_rechaza_antes_de_abrir_la_base(tmp_path: Path) -> None:
    db_path = _healthy_db(tmp_path)
    with pytest.raises(ValueError):
        WeeklyReviewCollector(AppConfig(), db_path=db_path, window_days=0, now_utc=NOW)


def test_la_ventana_excluye_lo_anterior(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store)
    _equity(store, [NOW - timedelta(minutes=1)])
    store.write_decision(_decision("in", action="ENTER", risk=_risk(1.0)))
    store.write_trade(_trade(10, net=6.0, closed_at=NOW - timedelta(hours=1)))
    store.write_trade(_trade(11, net=-6.0, closed_at=NOW - timedelta(days=9)))

    review = _collect(db_path)

    assert len(review.trades) == 1
    assert review.trades[0].position_id == 10


def test_una_semana_sin_violar_nada_pasa_todos_los_candados(tmp_path: Path) -> None:
    review = _collect(_healthy_db(tmp_path))

    assert not review.failed_audits
    assert {finding.code for finding in review.audits} == {
        "DEMO",
        "MAX_ENTRADAS_DIA",
        "SL_OBLIGATORIO",
        "RIESGO_POR_OPERACION",
        "CANDADO_DIARIO",
        "EQUITY_FLOOR",
        "MARGEN_LIBRE",
        "HUECOS_FEED",
        "RECONEXIONES",
        "ERRORES",
        "RETCODES",
        "KILL_SWITCH",
        "DECISIONES_DUPLICADAS",
        "REGISTRO",
    }


# ---------------------------------------------------------------------------
# Informe de texto y JSON
# ---------------------------------------------------------------------------


def test_el_informe_de_texto_muestra_lo_que_la_base_dice(tmp_path: Path) -> None:
    review = _collect(_healthy_db(tmp_path))

    text = WeeklyReportBuilder().build(review)

    assert "REVISIÓN SEMANAL — operación en DEMO" in text
    assert "Auditoría de candados" in text
    assert "Pepperstone-Demo" in text
    assert "RIESGO_POR_OPERACION" in text
    assert "Lectura de la semana" in text
    assert "[FALLO" not in text


def test_el_json_refleja_las_auditorias_y_las_operaciones(tmp_path: Path) -> None:
    review = _collect(_healthy_db(tmp_path))

    payload = weekly_review_to_jsonable(review)

    assert payload["window_days"] == 7
    assert payload["decisions_total"] == 1
    assert payload["account"]["server"] == "Pepperstone-Demo"
    assert [item["code"] for item in payload["audits"]] == [f.code for f in review.audits]
    assert len(payload["trades"]) == 1
    assert json.dumps(payload, ensure_ascii=False).count("FALLO") == sum(
        1 for finding in review.audits if finding.status == "FALLO"
    )
    assert payload["risk_pct_actual_max"] == pytest.approx(0.98)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_module():
    root = Path(__file__).resolve().parents[2]
    location = root / "scripts" / "weekly_review.py"
    spec = importlib.util.spec_from_file_location("weekly_review_cli", location)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_la_cli_imprime_sale_cero_cuando_todo_pasa_y_escribe_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = _healthy_db(tmp_path)
    out = tmp_path / "informes" / "revision.json"
    cli = _cli_module()

    code = cli.main(
        [
            "--config",
            str(_write_config(tmp_path)),
            "--db",
            str(db_path),
            "--until",
            NOW.isoformat(),
            "--json",
            str(out),
        ]
    )
    printed = capsys.readouterr().out

    assert code == 0
    assert "Auditoría de candados" in printed
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["decisions_total"] == 1


def test_la_cli_sale_con_codigo_dos_si_un_candado_falla(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "state.db"
    store = _store(db_path)
    _write_state(store, mode="LIVE", is_demo=False)
    _equity(store, [NOW - timedelta(minutes=1)])
    cli = _cli_module()

    code = cli.main(
        [
            "--config",
            str(_write_config(tmp_path)),
            "--db",
            str(db_path),
            "--until",
            NOW.isoformat(),
        ]
    )

    assert code == 2
    assert "DEMO" in capsys.readouterr().err


def test_la_cli_avisa_si_no_hay_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = _cli_module()

    code = cli.main(["--config", str(_write_config(tmp_path)), "--db", str(tmp_path / "x.db")])

    assert code == 3
    assert "no existe el StateStore" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["ayer", "2026-13-45T99:00"])
def test_la_cli_rechaza_unos_argumentos_ilegibles(
    tmp_path: Path, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = _healthy_db(tmp_path)
    cli = _cli_module()

    assert (
        cli.main(["--config", str(_write_config(tmp_path)), "--db", str(db_path), "--until", value])
        == 4
    )
    assert cli.main(["--config", str(_write_config(tmp_path)), "--db", str(db_path), "--days", "0"]) == 4


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("app:\n  name: goldbot\n", encoding="utf-8")
    return path
