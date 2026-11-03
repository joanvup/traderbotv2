"""Pruebas del dashboard (Fase 5): API de lectura, comandos con token y Pausa real.

Ninguna prueba abre MT5: se rellena la misma base SQLite que llenaría el bot y se comprueba
lo que ve el navegador. La pregunta de fondo es de honestidad — si falta un dato el panel
tiene que decir "no hay dato", nunca inventarlo.

Se cubren los cinco casos que exige el diseño: API completa, base vacía, base inexistente
(MT5/bot caído antes de escribir nunca), datos atrasados, rechazo de token, idempotencia y
que `Pausa` bloquee entradas nuevas sin cerrar lo abierto.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.unit._snapshots import NOW, account, regime, signal, snapshot

from goldbot.config import AppConfig
from goldbot.dashboard.app import create_app
from goldbot.domain.types import (
    BrokerHealth,
    CommandType,
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
from goldbot.infra.time import UtcClock, parse_session_windows

MAGIC = 20261030
TOKEN = "token-de-prueba-0123456789"
TOKEN_ENV = "GOLDBOT_DASHBOARD_TEST_TOKEN"
DAY = NOW.date()

_SESSIONS = parse_session_windows(
    {"ASIA": ["03:00-11:00"], "LONDON": ["11:00-19:00"], "NEW_YORK": ["16:00-00:00"]}
)


class _FrozenClock(UtcClock):
    """Reloj anclado: la antigüedad de cada panel se comprueba sin depender del tiempo real."""

    def __init__(self, moment: datetime = NOW) -> None:
        super().__init__(
            "America/New_York", server_shift=timedelta(hours=7), session_windows=_SESSIONS
        )
        self.moment = moment

    def now_utc(self) -> datetime:
        return self.moment

    def sleep_until(self, ts_utc: datetime) -> None:
        return None


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)


def _config(**dashboard: Any) -> AppConfig:
    return AppConfig(dashboard={"token_env": TOKEN_ENV, "stale_after_s": 15, **dashboard})


def _client(db_path: Path, clock: _FrozenClock, config: AppConfig | None = None) -> TestClient:
    app = create_app(db_path=db_path, config=config or _config(), clock=clock)
    return TestClient(app, raise_server_exceptions=False)


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"X-Dashboard-Token": token}


def _command(client: TestClient, name: str, key: str, token: str = TOKEN) -> Any:
    return client.post(
        "/api/command",
        json={"command": name, "idempotency_key": key},
        headers=_auth(token),
    )


def _rows(db_path: Path, query: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(query))


def _congelar_foto_de_posicion(db_path: Path, ticket: int, *, hace_s: int) -> None:
    """Deja `positions.updated_at_utc` donde la deja un bot que no actúa sobre esa operación.

    `upsert_position` estampa siempre el reloj de la máquina, así que por API no hay forma de
    escribir una foto vieja; reproducir el caso real pasa por mover la columna.
    """
    marca = (NOW - timedelta(seconds=hace_s)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE positions SET updated_at_utc = ? WHERE ticket = ?", (marca, ticket))
        conn.commit()


# ---------------------------------------------------------------------------
# Relleno de la base como lo haría el bot
# ---------------------------------------------------------------------------


def _filters(*, blocked: bool = False) -> FilterOutcome:
    results = (
        FilterResult(FilterCode.SPREAD, False, 20.0, 35.0, "spread dentro del límite"),
        FilterResult(FilterCode.NEWS, blocked, 40.0, 30.0, "minutos al evento rojo"),
        FilterResult(FilterCode.MAX_TRADES, False, 1, 2, "entradas del día disponibles"),
    )
    return FilterOutcome(
        results=results,
        blocked=blocked,
        primary_reason="NEWS: faltan 40 min para un evento de impacto alto" if blocked else None,
    )


def _risk() -> RiskDecision:
    return RiskDecision(
        approved=True,
        lots=0.02,
        risk_money=9.96,
        risk_pct_actual=0.996,
        sl_distance_price=4.12,
        sl_distance_points=412.0,
        reason="dimensionado al 1% del equity",
        spec_hash="spec-test",
        equity_at_decision=1_000.0,
    )


def _decision(
    *,
    decision_id: str,
    action: str,
    playbook: str,
    rationale: str,
    with_filters: bool,
    with_risk: bool,
) -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        cycle_id=f"c{decision_id[-1]}",
        ts_utc=NOW,
        bar_time_utc=NOW - timedelta(minutes=5),
        action=action,
        snapshot=snapshot(),
        regime=regime(Regime.TREND_UP, confidence=0.82),
        playbook_id=playbook,
        signal=signal() if with_filters else None,
        filters=_filters() if with_filters else None,
        risk=_risk() if with_risk else None,
        rationale=rationale,
        latency_ms=120,
        config_hash="cfg-test",
        model_versions={"rules": "rules-1"},
    )


def _trade(
    position_id: int,
    *,
    side: Side,
    entry: float,
    exit_price: float,
    net: float,
    reason: ExitReason,
    closed_at: datetime,
) -> TradeRecord:
    return TradeRecord(
        position_id=position_id,
        symbol="XAUUSD",
        side=side,
        volume=0.02,
        entry_price=entry,
        exit_price=exit_price,
        sl=entry - 4.0 if side is Side.BUY else entry + 4.0,
        tp=entry + 8.0 if side is Side.BUY else entry - 8.0,
        open_time_utc=closed_at - timedelta(hours=3),
        close_time_utc=closed_at,
        entry_deal_id=position_id * 10,
        close_deal_id=position_id * 10 + 1,
        commission=-0.14,
        swap=0.0,
        net_profit=net,
        r_multiple=round(net / 8.0, 3),
        duration_s=3 * 3600,
        exit_reason=reason,
        exit_reason_source=ExitReasonSource.SL
        if reason is ExitReason.SL
        else ExitReasonSource.BOT,
        playbook_id="trend_pullback",
        regime_at_entry=Regime.TREND_UP,
        decision_id_entry=f"dec-{position_id}",
        reconciled_at_utc=closed_at,
    )


def _seed(db_path: Path, clock: _FrozenClock) -> SqliteStateStore:
    """Escribe en la base exactamente lo que escribiría un ciclo real del bot."""
    store = SqliteStateStore(db_path, clock)
    store.initialize()
    daily = SqliteDailyLedger(db_path, clock=clock)

    ctx = snapshot()
    store.write_bot_state(
        {
            "state": "OPERATIVO",
            "paused": False,
            "mode": "DEMO",
            "requested_symbol": "XAUUSD",
            "resolved_symbol": "XAUUSD",
            "magic": MAGIC,
            "account": asdict(account(equity=1_000.0, balance=995.0)),
            "health": asdict(
                BrokerHealth(
                    connected=True,
                    terminal_connected=True,
                    algo_trading_enabled=True,
                    account_trade_allowed=True,
                    latency_ms=4.2,
                    last_error=None,
                    checked_at_utc=clock.now_utc(),
                )
            ),
            "context": ctx.to_jsonable(),
            "regime": {
                "label": Regime.TREND_UP.value,
                "confidence": 0.82,
                "method": "both",
                "confirmed_bars": 3,
                "probs": {r.value: p for r, p in ctx_bias_probs().items()},
            },
            "playbook": None,
            "filters": None,
            "feed_status": "OK",
            "last_cycle_at_utc": clock.now_utc(),
            "cycle_seq": 7,
            "last_heartbeat_utc": clock.now_utc(),
            "kill_switch_latched": False,
            "kill_reason": None,
            "config_hash": "cfg-test",
            "app_version": "0.1.0",
            "server_offset_min": 180,
        }
    )

    for minutes_ago, equity in ((90, 991.0), (3, 995.0), (2, 997.0), (1, 1_000.0)):
        store.write_equity_snapshot(
            account_login=61623438,
            ts_utc=NOW - timedelta(minutes=minutes_ago),
            balance=995.0,
            equity=equity,
            margin_free=equity,
            floating_pnl=equity - 995.0,
            positions_count=1 if minutes_ago < 90 else 0,
            source="PERIODIC",
        )

    store.write_event(
        EventRecord(
            ts_utc=NOW - timedelta(minutes=40),
            source=EventSource.BOT,
            type=EventType.STARTUP,
            level=LogLevel.INFO,
            message="goldbot iniciado en cuenta DEMO",
            payload={"magic": MAGIC},
        )
    )
    for index in range(3):
        store.write_event(
            EventRecord(
                ts_utc=NOW - timedelta(minutes=index),
                source=EventSource.BOT,
                type=EventType.REVIEW,
                level=LogLevel.INFO,
                message=f"ciclo {index + 1}: revisión del contexto",
                payload={"cycle": index + 1},
            )
        )
    for level, message in (
        (LogLevel.INFO, "conectado al terminal"),
        (LogLevel.WARNING, "spread elevado durante el rollover"),
        (LogLevel.ERROR, "order_send devolvió TRADE_RETCODE_REQUOTE"),
    ):
        store.write_log_line(
            NOW - timedelta(minutes=2), level, "goldbot.engine.bot", message, cycle_id="c7"
        )

    store.write_decision(
        _decision(
            decision_id="dec-enter-1",
            action="ENTER",
            playbook="trend_pullback",
            rationale="reteste de la EMA20 en H1 con estructura alcista",
            with_filters=True,
            with_risk=True,
        )
    )
    store.write_decision(
        _decision(
            decision_id="dec-nosetup-2",
            action="NO_SETUP",
            playbook="range_fade",
            rationale="range_fade seleccionado pero sin setup válido",
            with_filters=False,
            with_risk=False,
        )
    )

    store.upsert_position(
        PositionState(
            ticket=5_510_001,
            position_id=5_510_001,
            side=Side.BUY,
            volume=0.02,
            entry_price=2_400.22,
            current_price=2_404.10,
            sl=2_400.22,
            tp=2_408.10,
            pnl_money=7.76,
            initial_risk_price=4.12,
            r_multiple=0.94,
            playbook_id="trend_pullback",
            regime_at_entry=Regime.TREND_UP,
            decision_id_entry="dec-enter-1",
            opened_at_utc=NOW - timedelta(minutes=35),
            breakeven_done=True,
            partials_done=1,
            last_action="TRAIL_SL",
        )
    )

    store.write_trade(
        _trade(
            5_500_001,
            side=Side.BUY,
            entry=2_380.0,
            exit_price=2_388.0,
            net=15.86,
            reason=ExitReason.TP,
            closed_at=NOW - timedelta(days=2),
        )
    )
    store.write_trade(
        _trade(
            5_500_002,
            side=Side.SELL,
            entry=2_392.0,
            exit_price=2_396.5,
            net=-9.0,
            reason=ExitReason.SL,
            # 26 h: a propósito fuera de la ventana de 1 día para poder probar el filtro de periodo.
            closed_at=NOW - timedelta(hours=26),
        )
    )
    store.write_trade(
        _trade(
            5_500_003,
            side=Side.BUY,
            entry=2_399.0,
            exit_price=2_403.0,
            net=7.86,
            reason=ExitReason.BREAKEVEN,
            closed_at=NOW - timedelta(hours=6),
        )
    )

    daily.ensure_day(DAY, 1_000.0)
    daily.record_entry(DAY)
    daily.record_pnl(DAY, 7.86)
    previous_day = DAY - timedelta(days=1)
    daily.ensure_day(previous_day, 1_010.0)
    daily.record_entry(previous_day)
    daily.record_pnl(previous_day, -9.0)

    store.record_history_sync(
        "candles:XAUUSD:M5",
        last_sync_utc=NOW - timedelta(minutes=5),
        last_deal_ts_utc=NOW - timedelta(days=1),
    )
    store.record_order_attempt(
        ts_utc=NOW - timedelta(minutes=30),
        decision_id="dec-enter-1",
        request={"action": "TRADE_ACTION_DEAL", "volume": 0.02, "type_filling": 1},
        retcode=10009,
        retcode_name="TRADE_RETCODE_DONE",
        retryable=False,
        result={"order_ticket": 771_001, "deal_ticket": 771_002},
    )
    return store


def ctx_bias_probs() -> dict[Regime, float]:
    return {Regime.TREND_UP: 0.82, Regime.RANGE: 0.11, Regime.TREND_DOWN: 0.07}


# ---------------------------------------------------------------------------
# Base vacía y base inexistente
# ---------------------------------------------------------------------------


def test_base_inexistente_no_la_crea_el_dashboard_y_pinta_vacio(tmp_path: Path) -> None:
    clock = _FrozenClock()
    missing = tmp_path / "nunca_existo" / "state.db"
    client = _client(missing, clock)

    overview = client.get("/api/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["header"]["state"] is None
    assert body["kpis"]["equity_usd"] is None
    assert body["positions"] == []
    assert body["chips"]
    # Sin base no hay frescura que fingir: toda fuente se marca como "sin dato".
    assert all(flag["age_s"] is None for flag in body["staleness"].values())

    assert client.get("/api/brain").json()["latest"] is None
    assert client.get("/api/trades").json()["summary"]["trades"] == 0
    assert client.get("/api/events").json()["rows"] == []
    assert client.get("/api/logs").json()["rows"] == []
    health = client.get("/api/healthz").json()
    assert health["status"] == "degraded" and health["db_reachable"] is False

    # El lector nunca escribe: ni el fichero ni el `-wal` deben aparecer.
    assert not missing.exists()
    assert not list(missing.parent.glob("state.db*"))


def test_base_vacia_endpoints_operativos_sin_filas(tmp_path: Path) -> None:
    clock = _FrozenClock()
    db_path = tmp_path / "state.db"
    SqliteStateStore(db_path, clock).initialize()
    client = _client(db_path, clock)

    body = client.get("/api/overview").json()
    assert body["header"]["symbol"] is None
    assert body["kpis"]["balance_usd"] is None
    assert body["footer"]["counts"]["events"] == 0
    equity = client.get("/api/charts/equity").json()
    assert equity["points"] == 0
    assert equity["summary"]["coverage_pct"] == 0.0
    # La rejilla sigue existiendo: sin datos la línea está entera en huecos, no en ceros.
    assert set(equity["equity"]) == {None}
    assert client.get("/api/charts/daily-pnl").json()["rows"] == []
    assert client.get("/api/healthz").json()["status"] == "degraded"


# ---------------------------------------------------------------------------
# Paneles con datos
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[Path, _FrozenClock, TestClient]:
    clock = _FrozenClock()
    db_path = tmp_path / "state.db"
    _seed(db_path, clock)
    return db_path, clock, _client(db_path, clock)


def test_cabecera_kpis_y_chips(seeded: tuple[Path, _FrozenClock, TestClient]) -> None:
    _, _, client = seeded
    body = client.get("/api/overview").json()

    header = body["header"]
    assert header["mode"] == "DEMO"
    assert header["is_demo"] is True
    assert header["state"] == "OPERATIVO"
    assert header["symbol"] == "XAUUSD"
    assert header["account"]["server"] == "Pepperstone-Demo"
    assert header["uptime_s"] == pytest.approx(40 * 60, abs=2)
    # La hora que se pinta es la del servidor, con su zona indicada.
    assert header["server_time"]["server_zone"] == "America/New_York"
    assert header["server_time"]["offset_min"] == 180
    assert header["server_time"]["server"].startswith(str(DAY))

    kpis = body["kpis"]
    assert kpis["balance_usd"] == 995.0
    assert kpis["equity_usd"] == 1_000.0
    assert kpis["floating_usd"] == 5.0
    assert kpis["day_realized_usd"] == 7.86
    assert kpis["day_total_usd"] == pytest.approx(12.86)
    assert kpis["daily_limit_usd"] == pytest.approx(20.0)  # 2 % de 1000
    # Un día en positivo consume 0 % del límite, nunca un porcentaje negativo.
    assert kpis["daily_limit_used_pct"] == 0.0
    assert "equity del inicio del día" in kpis["base_calculo"]

    chips = {c["label"]: c for c in body["chips"]}
    assert chips["spread actual (puntos)"]["value"] == 20.0
    assert chips["algo trading (terminal)"]["value"] == "ON"
    assert chips["trading permitido (cuenta)"]["value"] == "ON"
    assert chips["feed MT5"]["ok"] is True
    assert chips["entradas"]["ok"] is True
    assert "1/2" in chips["entradas usadas hoy"]["value"]

    # Con datos frescos ningún panel se marca como atrasado.
    assert body["staleness"]["bot_state"]["stale"] is False
    assert body["staleness"]["health"]["stale"] is False
    assert body["feed_freshness"]["M5"]["status"] == "OK"


def test_banderas_de_pausa_y_kill_switch(seeded: tuple[Path, _FrozenClock, TestClient]) -> None:
    db_path, clock, client = seeded
    store = SqliteStateStore(db_path, clock)
    # Nueva foto sin salud ni contexto: el panel debe seguir pintando el estado y sus
    # banderas, y marcar como "sin lectura" lo que el bot ya no publica.
    store.write_bot_state(
        {
            "state": "PAUSADO",
            "paused": True,
            "mode": "DEMO",
            "requested_symbol": "XAUUSD",
            "magic": MAGIC,
            "account": asdict(account()),
            "feed_status": "OK",
            "last_cycle_at_utc": clock.now_utc(),
            "cycle_seq": 8,
        }
    )
    body = client.get("/api/overview").json()
    assert body["header"]["state"] == "PAUSADO"
    assert body["header"]["paused"] is True
    assert body["chips"][4]["label"] == "entradas"
    assert body["chips"][4]["ok"] is False
    assert "pausado desde el dashboard" in body["chips"][4]["value"]

    store.write_bot_state(
        {
            "state": "KILLED",
            "paused": False,
            "mode": "DEMO",
            "requested_symbol": "XAUUSD",
            "magic": MAGIC,
            "account": asdict(account()),
            "feed_status": "OK",
            "last_cycle_at_utc": clock.now_utc(),
            "cycle_seq": 9,
            "kill_switch_latched": True,
            "kill_reason": "pérdida diaria máxima",
        }
    )
    killed = client.get("/api/overview").json()
    assert killed["header"]["kill_switch_latched"] is True
    assert "kill switch" in killed["chips"][4]["value"]
    # El kill switch manda sobre la pausa: el motivo visible es el candado.
    assert killed["chips"][4]["ok"] is False


def test_cerebro_regimen_filtros_sesgo_y_historial(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    _, _, client = seeded
    brain = client.get("/api/brain?limit=5").json()

    assert brain["regime"]["label"] == "TREND_UP"
    assert brain["probs"]["TREND_UP"] == pytest.approx(0.82)
    latest = brain["latest"]
    assert latest["action"] == "NO_SETUP"  # la última decisión publicada es la abstención
    assert latest["filters"] is None
    assert latest["risk"] is None
    # La abstención no finge filtros: se ve en el historial que la ENTER sí los corrió.
    acciones = {row["decision_id"]: row["action"] for row in brain["history"]}
    assert acciones == {"dec-nosetup-2": "NO_SETUP", "dec-enter-1": "ENTER"}
    enter = next(row for row in brain["history"] if row["decision_id"] == "dec-enter-1")
    assert enter["filters"]["blocked"] is False
    assert [r["code"] for r in enter["filters"]["results"]] == ["SPREAD", "NEWS", "MAX_TRADES"]
    assert enter["risk"]["lots"] == 0.02
    assert enter["risk"]["sl_distance_points"] == 412.0
    assert brain["bias"] and set(brain["bias"]) >= {"M15", "H1", "H4", "D1"}
    assert brain["structure"]["prev_day_high"] == 2_420.0
    assert brain["timing"]["spread_points"] == 20.0


def test_filtro_bloqueante_se_reporta_como_bloqueo_de_entradas(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    db_path, clock, client = seeded
    store = SqliteStateStore(db_path, clock)
    store.write_decision(
        DecisionRecord(
            decision_id="dec-blocked-9",
            cycle_id="c9",
            ts_utc=clock.now_utc(),
            bar_time_utc=clock.now_utc() - timedelta(minutes=5),
            action="BLOCKED",
            snapshot=snapshot(),
            regime=regime(Regime.TREND_UP),
            playbook_id="trend_pullback",
            signal=signal(),
            filters=_filters(blocked=True),
            risk=None,
            rationale="NEWS: faltan 40 min para un evento de impacto alto",
            latency_ms=90,
            config_hash="cfg-test",
            model_versions={"rules": "rules-1"},
        )
    )
    brain = client.get("/api/brain").json()
    assert brain["latest"]["filters"]["blocked"] is True
    assert "evento de impacto alto" in brain["latest"]["filters"]["primary_reason"]
    chips = {c["label"]: c for c in client.get("/api/overview").json()["chips"]}
    assert chips["entradas"]["ok"] is False
    assert "evento de impacto alto" in chips["entradas"]["value"]


def test_posicion_abierta_con_su_gestion(seeded: tuple[Path, _FrozenClock, TestClient]) -> None:
    _, _, client = seeded
    positions = client.get("/api/overview").json()["positions"]
    assert len(positions) == 1
    opened = positions[0]
    assert opened["ticket"] == 5_510_001
    assert opened["side"] == "BUY"
    assert opened["entry_price"] == 2_400.22
    assert opened["current_price"] == 2_404.10
    # El PnL flotante lo publica el bot; el panel no lo recalcula con un contrato supuesto.
    assert opened["pnl_money"] == 7.76
    assert opened["playbook_id"] == "trend_pullback"
    assert opened["last_action"] == "TRAIL_SL"
    assert opened["breakeven_done"] == 1
    assert opened["partials_done"] == 1
    assert opened["photo_age_s"] == pytest.approx(0.0, abs=1)


def test_grafico_de_equity_conserva_el_hueco_sin_interpolar(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    _, _, client = seeded
    chart = client.get("/api/charts/equity?hours=24").json()
    assert chart["summary"]["filled_min"] == 4
    # Entre la vela de hace 90 min y la de hace 3 min hay 86 minutos sin ninguna foto.
    assert chart["summary"]["gap_min"] == 24 * 60 + 1 - 4
    assert chart["summary"]["coverage_pct"] == pytest.approx(0.3, abs=0.1)
    assert chart["summary"]["min_equity"] == 991.0
    assert chart["summary"]["max_equity"] == 1_000.0
    assert chart["summary"]["last_ts_utc"].endswith("00Z")
    # La posición abierta marca una entrada; el cierre de hace 6 horas marca una salida.
    kinds = [m["kind"] for m in chart["markers"]]
    assert "entry" in kinds and "exit" in kinds
    valores = [v for v in chart["equity"] if v is not None]
    assert len(valores) == 4
    assert valores.count(991.0) == 1  # el punto aislado sigue ahí, no se rellena


def test_pnl_diario_y_historial_de_operaciones(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    _, _, client = seeded
    daily = client.get("/api/charts/daily-pnl?days=14").json()
    assert daily["days"] == [str(DAY - timedelta(days=1)), str(DAY)]
    assert daily["realized_usd"] == [-9.0, 7.86]
    assert daily["entries"] == [1, 1]
    assert daily["locked"] == [False, False]
    assert daily["rows"][1]["equity_start"] == 1_000.0

    trades = client.get("/api/trades?days=30").json()
    summary = trades["summary"]
    assert summary["trades"] == 3
    assert summary["win_rate_pct"] == pytest.approx(66.7)
    # PF = 23,72 / 9,00: se informa del bruto, no de un neto maquillado.
    assert summary["profit_factor"] == pytest.approx(2.636, abs=0.01)
    assert summary["net_usd"] == pytest.approx(14.72)
    assert [row["exit_reason"] for row in trades["rows"]] == ["BREAKEVEN", "SL", "TP"]
    assert trades["rows"][0]["position_id"] == 5_500_003

    # El filtro de periodo sí filtra: 1 día deja fuera los cierres más antiguos.
    assert client.get("/api/trades?days=1").json()["summary"]["trades"] == 1


def test_eventos_y_log_en_orden_cronologico(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    _, _, client = seeded
    events = client.get("/api/events?limit=60").json()["rows"]
    assert len(events) == 4
    assert events[0]["type"] == "REVIEW"  # el más reciente primero
    assert events[-1]["type"] == "STARTUP"
    assert events[-1]["payload_json"]["magic"] == MAGIC

    logs = client.get("/api/logs?limit=160").json()["rows"]
    assert len(logs) == 3
    # El log se pinta de arriba abajo en orden creciente: la última línea al pie.
    assert [row["level"] for row in logs] == ["INFO", "WARNING", "ERROR"]
    assert logs[-1]["cycle_id"] == "c7"

    footer = client.get("/api/overview").json()["footer"]
    assert footer["magic"] == MAGIC
    assert footer["closed_trades_30d"] == 3
    assert footer["counts"]["order_attempts"] == 1
    assert footer["history_sync"]["key"] == "candles:XAUUSD:M5"


def test_panels_atrasados_cuando_el_bot_deja_de_escribir(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    db_path, clock, _ = seeded
    # El mismo proceso del dashboard, pero 10 minutos después de la última escritura.
    tarde = _FrozenClock(NOW + timedelta(minutes=10))
    client = _client(db_path, tarde, _config())

    body = client.get("/api/overview").json()
    assert body["staleness"]["bot_state"]["stale"] is True
    assert body["staleness"]["bot_state"]["age_s"] == pytest.approx(600, abs=1)
    assert body["staleness"]["decision"]["stale"] is True
    # El feed NO se marca OK: el panel debe avisar de que el enlace dejó de publicar.
    assert body["chips"][9]["ok"] is False
    assert body["staleness"]["positions"]["stale"] is True
    assert client.get("/api/healthz").json()["status"] == "degraded"
    # Sigue pintando el último dato conocido, con su antigüedad: no lo borra ni lo inventa.
    assert body["kpis"]["equity_usd"] == 1_000.0
    assert body["positions"][0]["current_price"] == 2_404.10


def test_la_decision_se_mide_con_el_paso_de_vela_no_con_el_poll(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    """Dos cadencias distintas, dos umbrales: 15 s para el poll, una vela para la decisión.

    La decisión solo se escribe al cerrar la vela de decisión (M5 = 300 s). Con el umbral del
    dashboard, `stale_after_s: 15`, el chip del Cerebro estaba ambarado **siempre** —incluso con
    el bot perfecto decidiendo vela a vela—, porque medía una cadencia de 300 s con un reloj de
    15 s. Aquí la última escritura tiene 2 minutos: tarde para un poll, fresco para una vela.
    """
    db_path, clock, _ = seeded
    a_los_dos_minutos = _FrozenClock(NOW + timedelta(seconds=120))
    client = _client(db_path, a_los_dos_minutos, _config())

    overview = client.get("/api/overview").json()
    # Lo que escribe cada poll sigue juzgándose con los 15 s del dashboard: 120 s sin escribir
    # bot_state es un bot que dejó de publicar, y eso sí hay que pintarlo.
    assert overview["staleness"]["bot_state"]["stale"] is True
    assert overview["staleness"]["decision"]["stale"] is False
    assert overview["staleness"]["decision"]["age_s"] == pytest.approx(120, abs=1)
    assert overview["staleness"]["context"]["stale"] is False

    brain = client.get("/api/brain").json()
    assert brain["staleness"]["decision"]["stale"] is False
    assert brain["staleness"]["snapshot"]["stale"] is False

    # Y el umbral nuevo no es un "nunca atrasado": pasadas dos velas sin decisión, ambar.
    # (El caso de 10 minutos ya lo cubre la prueba anterior con la misma base sembrada.)
    client_tarde = _client(db_path, _FrozenClock(NOW + timedelta(seconds=400)), _config())
    assert client_tarde.get("/api/overview").json()["staleness"]["decision"]["stale"] is True


def test_la_curva_de_equity_se_mide_con_su_minuto_no_con_el_poll(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    """Una foto por minuto no es un poll cada 5 s: el gráfico de equity tiene su propio umbral.

    `equity_snapshots` se guarda con la marca del minuto en curso, así que la edad del último
    punto real sube de 0 a ~60 s y vuelve a caer (medido en demo con poll de 5 s). Con
    `stale_after_s: 15` el panel viviría ambarado enseñando una curva perfecta.
    """
    db_path, _, _ = seeded
    client = _client(db_path, _FrozenClock(NOW), _config())

    # La base sembrada tiene su última foto justo un minuto antes de NOW.
    overview = client.get("/api/overview").json()
    assert overview["staleness"]["equity_series"]["age_s"] == pytest.approx(60, abs=1)
    assert overview["staleness"]["equity_series"]["stale"] is False

    charts = client.get("/api/charts/equity").json()
    assert charts["staleness"]["stale"] is False

    # Un minuto entero sin foto nueva todavía es la cadencia normal; dos sí es un sondeo muerto.
    # Y el umbral es configuración: bajándolo a 5 s vuelve el ámbar con la misma base.
    tarde = _client(db_path, _FrozenClock(NOW + timedelta(seconds=130)), _config())
    assert tarde.get("/api/overview").json()["staleness"]["equity_series"]["stale"] is True
    assert tarde.get("/api/charts/equity").json()["staleness"]["stale"] is True
    apretado = _client(db_path, _FrozenClock(NOW), _config(equity_stale_after_s=5))
    assert apretado.get("/api/overview").json()["staleness"]["equity_series"]["stale"] is True


def test_el_pnl_diario_se_mide_con_la_vela_de_decision(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    """`daily_stats` solo se reescribe dentro de `run_cycle`, o sea con vela nueva del marco.

    Medirlo con el reloj del poll ambraba el panel de PnL diario en cuanto el bot dejaba de
    publicar la foto de estado, aunque siguiera cerrando velas y registrando el día.
    """
    db_path, _, _ = seeded
    client = _client(db_path, _FrozenClock(NOW + timedelta(seconds=120)), _config())
    daily = client.get("/api/charts/daily-pnl").json()
    assert daily["staleness"]["daily"]["stale"] is False
    assert daily["staleness"]["daily"]["age_s"] == pytest.approx(120, abs=1)

    # Pasadas dos velas sin ciclo, el PnL diario sí está atrasado.
    tarde = _client(db_path, _FrozenClock(NOW + timedelta(seconds=400)), _config())
    assert tarde.get("/api/charts/daily-pnl").json()["staleness"]["daily"]["stale"] is True


def test_sin_posiciones_abiertas_no_es_dato_atrasado(tmp_path: Path) -> None:
    """La lista vacía la garantiza el ciclo que reconcilia: sin posiciones su frescura es la del ciclo.

    Con `positions` vacía no había `updated_at_utc` que mirar y el chip del panel de posiciones
    se quedaba en ámbar permanente — justo en el estado deseado del bot, que operar 0 veces es
    válido. Aquí verde mientras el ciclo que reconcilia esté vivo, y ámbar si ese ciclo muere.
    """
    clock = _FrozenClock()
    db_path = tmp_path / "state.db"
    store = _seed(db_path, clock)
    store.delete_position(5_510_001)
    client = _client(db_path, clock, _config())

    overview = client.get("/api/overview").json()
    assert overview["positions"] == []
    assert overview["staleness"]["positions"]["stale"] is False
    assert overview["staleness"]["positions"]["age_s"] == pytest.approx(0, abs=1)

    # Sin posiciones *y* sin ciclo también es un enlace caído: el verde no tapa el fallo.
    tarde = _client(db_path, _FrozenClock(NOW + timedelta(minutes=10)), _config())
    assert tarde.get("/api/overview").json()["staleness"]["positions"]["stale"] is True

    # Y el frontend lo dice con letras, no sólo con color: existe la variante `flat` del chip y
    # el panel de posiciones la pide precisamente cuando la lista llega vacía.
    js = (
        Path(__file__).resolve().parents[2] / "src" / "goldbot" / "dashboard" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "sin posiciones · " in js
    assert 'flat ? "flat" : "ciclo"' in js


def test_una_posicion_viva_sin_accion_no_es_dato_atrasado(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    """La foto de la posición no avanza si la gestión no actúa; el bloque sí avanza con el ciclo.

    Reproducido en demo el 2026-11-03: con una SELL viva cuyo stop sólo se movía en alguna vela,
    el chip del panel de posiciones se pintaba en ámbar a los 60 y a los 82 s de la última acción,
    porque se medía contra `positions.updated_at_utc` con el umbral del poll (15 s).
    """
    db_path, _, _ = seeded
    _congelar_foto_de_posicion(db_path, 5_510_001, hace_s=35 * 60)

    client = _client(db_path, _FrozenClock(NOW), _config())
    body = client.get("/api/overview").json()

    # La posición sigue ahí y su foto conserva la antigüedad real, por fila.
    assert len(body["positions"]) == 1
    assert body["positions"][0]["photo_age_s"] == pytest.approx(35 * 60, abs=1)

    # Pero el bloque está al día: `_seed` estampa `daily_stats` con el reloj del último ciclo, y
    # una vela de diferencia reconciliando contra el broker no es un dato atrasado.
    assert body["staleness"]["positions"]["stale"] is False
    assert body["staleness"]["positions"]["age_s"] == pytest.approx(0, abs=1)

    # Con el ciclo muerto varios pasos de vela, ámbar otra vez: el verde no tapa un enlace caído.
    tarde = _client(db_path, _FrozenClock(NOW + timedelta(minutes=20)), _config())
    assert tarde.get("/api/overview").json()["staleness"]["positions"]["stale"] is True


# ---------------------------------------------------------------------------
# Comandos: token, idempotencia, caducidad y límites
# ---------------------------------------------------------------------------


def test_comandos_sin_token_o_con_token_incorrecto_401(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    _, _, client = seeded
    sin_header = client.post(
        "/api/command", json={"command": "pause", "idempotency_key": "pause-000001"}
    )
    assert sin_header.status_code == 401
    assert client.post(
        "/api/command",
        json={"command": "pause", "idempotency_key": "pause-000002"},
        headers=_auth("token-equivocado"),
    ).status_code == 401
    # Nadie escribió en la cola: el rechazo es previo a cualquier efecto.
    assert client.get("/api/commands").json()["rows"] == []


def test_sin_token_en_el_entorno_todos_los_comandos_401(
    seeded: tuple[Path, _FrozenClock, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, clock, _ = seeded
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    client = _client(db_path, clock, _config())
    for token in (TOKEN, "", "cualquiera"):
        headers = _auth(token) if token else {}
        res = client.post(
            "/api/command", json={"command": "pause", "idempotency_key": "pause-000003"}, headers=headers
        )
        assert res.status_code == 401
    assert client.get("/api/commands").json()["rows"] == []


def test_pausa_se_encola_sin_caduca_y_se_audita(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    db_path, _, client = seeded
    res = _command(client, "pause", "pause-000010")
    assert res.status_code == 200
    assert res.json()["status"] == "queued"

    rows = _rows(db_path, "SELECT command, status, origin, expires_at_utc FROM commands")
    assert len(rows) == 1
    assert rows[0]["command"] == CommandType.PAUSE.value
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["origin"] == "DASHBOARD"
    # La pausa no caduca: si el bot estaba caído, debe cumplirse al recuperar.
    assert rows[0]["expires_at_utc"] is None

    eventos = [e for e in client.get("/api/events?limit=60").json()["rows"] if e["type"] == "COMMAND"]
    assert len(eventos) == 1
    assert eventos[0]["source"] == "DASHBOARD"
    assert eventos[0]["payload_json"]["idempotency_key"] == "pause-000010"


def test_rearme_del_kill_switch_se_encola_sin_caduca_y_se_audita(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    db_path, _, client = seeded
    res = _command(client, "reset_kill_switch", "rearm-000040")
    assert res.status_code == 200
    assert res.json()["status"] == "queued"

    rows = _rows(db_path, "SELECT command, status, expires_at_utc FROM commands")
    assert rows[0]["command"] == CommandType.RESET_KILL_SWITCH.value
    assert rows[0]["status"] == "PENDING"
    # Rearmar no caduca: si el bot estaba caído cuando el operador lo pulsó, la orden sigue
    # siendo la del operador al recuperar.
    assert rows[0]["expires_at_utc"] is None

    eventos = [
        e for e in client.get("/api/events?limit=60").json()["rows"] if e["type"] == "COMMAND"
    ]
    assert len(eventos) == 1
    # Es el comando que devuelve el bot a operar: se audita con nivel de aviso, no informativo.
    assert eventos[0]["level"] == "WARNING"


def test_el_panel_tiene_boton_de_rearme_y_lo_oculta_sin_candado() -> None:
    """El botón existía sólo por API (§9 del runbook); aquí se ata al frontend."""
    here = Path(__file__).resolve()
    static = here.parents[2] / "src" / "goldbot" / "dashboard" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")

    assert 'id="btn-reset-kill"' in html
    assert 'addEventListener("click", armResetKill)' in js
    # Sólo se ofrece con el candado latcheado: un botón que no hace nada es ruido en la cabecera.
    assert "resetBtn.hidden = !latched" in js
    # Y exige segundo clic, como cerrar posiciones.
    assert js.index('ui.armedResetKill + 5000 < Date.now()') < js.index('sendCommand("reset_kill_switch")')


def test_el_panel_avis_cuando_el_guion_no_arranca_o_falta_uPlot() -> None:
    """El panel de una máquina con el registro roto se quedó vacío y sin decir nada.

    Se ata sobre el fuente, sin navegador: en esta suite no hay runner de JS. Lo que se fija es el
    contrato de los dos silencios que hubo que romper — que exista un aviso sin ejecutar nada y que
    la falta de uPlot no arrastre al resto de la página.
    """
    here = Path(__file__).resolve()
    static = here.parents[2] / "src" / "goldbot" / "dashboard" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")

    # 1 · El centinela se pinta desde el HTML — sin `hidden`, a diferencia del banner condicional —
    #     y lo quita la primera sentencia que ejecuta el guion. Si el guion no corre, no hay nada
    #     que lo oculte: es el único aviso posible cuando el navegador rechaza `app.js`.
    assert 'id="banner-no-js" class="banner banner-danger">' in html
    assert 'id="banner-live" class="banner banner-danger hidden"' in html
    arranque_guion = js[js.index('"use strict";') : js.index("const ui = {")]
    assert 'getElementById("banner-no-js")' in arranque_guion

    # 2 · Sin uPlot el bloque de gráficos se degrada con aviso y `boot()` llega a pedir datos.
    assert "function hayUPlot(notaId)" in js
    for funcion, nota in (("buildEquityChart", "equity-note"), ("buildDailyChart", "daily-note")):
        cuerpo = js[js.index(f"function {funcion}()") :]
        cuerpo = cuerpo[: cuerpo.index("\n}")]
        assert f'hayUPlot("{nota}")' in cuerpo, funcion
        assert cuerpo.index("hayUPlot(") < cuerpo.index("new uPlot("), funcion
        assert f'id="{nota}"' in html, nota
    boot = js[js.index("function boot()") :]
    assert boot.index("buildDailyChart()") < boot.index("startLoop()")


def test_close_all_caduca_y_los_comandos_prohibidos_no_existen(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    db_path, _, client = seeded
    assert _command(client, "close_all", "close-000020").status_code == 200
    expires = _rows(db_path, "SELECT expires_at_utc FROM commands WHERE command = 'CLOSE_ALL'")
    assert expires[0]["expires_at_utc"] is not None

    # No hay ninguna ruta para abrir una operación ni para tocar el riesgo.
    for nombre in ("open_position", "set_risk_pct", "modify_sl", "dele"):
        res = client.post(
            "/api/command",
            json={"command": nombre, "idempotency_key": f"prohibido-{nombre}-001"},
            headers=_auth(),
        )
        assert res.status_code == 422, nombre
    assert len(_rows(db_path, "SELECT 1 FROM commands")) == 1


def test_reintentos_idempotentes_no_duplican_el_comando(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    db_path, _, client = seeded
    primero = _command(client, "close_all", "close-000030")
    repetido = _command(client, "close_all", "close-000030")
    assert primero.json()["status"] == "queued"
    assert repetido.json()["status"] == "duplicate"
    assert repetido.json()["command_id"] == primero.json()["command_id"]
    assert len(_rows(db_path, "SELECT 1 FROM commands")) == 1
    # Y la auditoría no se repite: un doble clic deja un solo evento de comando.
    assert len(
        [e for e in client.get("/api/events?limit=60").json()["rows"] if e["type"] == "COMMAND"]
    ) == 1


def test_close_all_deshabilitado_devuelve_403(
    seeded: tuple[Path, _FrozenClock, TestClient]
) -> None:
    db_path, clock, _ = seeded
    client = _client(db_path, clock, _config(close_all_enabled=False))
    res = _command(client, "close_all", "close-000040")
    assert res.status_code == 403
    assert "close_all_enabled" in res.json()["detail"]
    # La pausa sigue disponible: deshabilitar el cierre total no desconecta el panel.
    assert _command(client, "pause", "pause-000041").status_code == 200


def test_comando_sin_base_del_bot_devuelve_503_sin_crear_la_base(tmp_path: Path) -> None:
    clock = _FrozenClock()
    missing = tmp_path / "state.db"
    client = _client(missing, clock)
    res = _command(client, "pause", "pause-000050")
    assert res.status_code == 503
    assert not missing.exists()


def test_api_de_lectura_no_exige_token(seeded: tuple[Path, _FrozenClock, TestClient]) -> None:
    _, _, client = seeded
    # La lectura es local y sin secreto: el token sólo protege lo que escribe.
    assert client.get("/api/overview").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/").headers["x-frame-options"] == "DENY"
    # La página no depende de ningún CDN: sólo recursos locales.
    html = client.get("/").text
    assert "http://" not in html and "https://" not in html
    for recurso in ("styles.css", "app.js", "vendor/uPlot.iife.min.js", "vendor/uPlot.min.css"):
        assert f'"{recurso}"' in html, recurso


def test_el_content_type_de_los_estaticos_no_depende_del_registro(tmp_path: Path) -> None:
    """Un `HKCR\\.js` con `Content Type = text/plain` dejaba el panel vacío y sin avisar.

    Ocurrido el 2026-11-03 en una segunda máquina: `mimetypes` sobrepone el valor del registro a
    su tabla base, Starlette sirve `app.js` como `text/plain`, y como el panel manda
    `X-Content-Type-Options: nosniff` el navegador se niega a ejecutarlo. El resultado es la
    página con los estilos aplicados y todos los bloques vacíos — `boot()` nunca corre y no se
    pide ningún dato — sin ningún error visible.
    """
    import mimetypes

    extensiones = (".js", ".css")
    originales = {ext: mimetypes.guess_type(f"recurso{ext}")[0] for ext in extensiones}
    # Envenar como lo haría el registro: `read_windows_registry` llama a `add_type(ctype, ext)`.
    for ext in extensiones:
        mimetypes.add_type("text/plain", ext)
    try:
        assert mimetypes.guess_type("app.js")[0] == "text/plain"
        client = _client(tmp_path / "state.db", _FrozenClock())
        for recurso in ("app.js", "vendor/uPlot.iife.min.js"):
            res = client.get(f"/{recurso}")
            assert res.status_code == 200, recurso
            assert res.headers["content-type"] == "text/javascript; charset=utf-8", recurso
        for recurso in ("styles.css", "vendor/uPlot.min.css"):
            res = client.get(f"/{recurso}")
            assert res.status_code == 200, recurso
            assert res.headers["content-type"] == "text/css; charset=utf-8", recurso
        # La cabecera que provoca el bloqueo sigue puesta: lo que se corrige es el tipo, no la
        # política de seguridad.
        assert client.get("/app.js").headers["x-content-type-options"] == "nosniff"
    finally:
        for ext, tipo in originales.items():
            if tipo is not None:
                mimetypes.add_type(tipo, ext)


# ---------------------------------------------------------------------------
# Que la Pausa haga de verdad lo que promete el botón
# ---------------------------------------------------------------------------


def _apply(rig: Any, command: CommandType, key: str) -> str:
    """Encola el comando como haría el panel, deja que el bot lo reclame y lo ejecuta."""
    from goldbot.domain.events import CommandEvent

    rig.store.enqueue_command(
        command, {}, idempotency_key=key, origin="DASHBOARD", ttl_s=0
    )
    claimed = rig.store.claim_next_command(now_utc=rig.clock.now_utc())
    assert claimed is not None and claimed.command is command
    resultado = rig.bot.step(CommandEvent(command=claimed, received_at_utc=rig.clock.now_utc()))
    assert resultado.action == "COMMAND"
    return claimed.command_id


def test_pausar_antes_de_la_primera_vela_evita_la_entrada(tmp_path: Path) -> None:
    """Sin posición abierta y con la señal del núcleo lista, la pausa produce 0 operaciones."""
    from tests.integration.test_fase4_bot import _rig

    rig = _rig(tmp_path)
    _apply(rig, CommandType.PAUSE, "pause-ui-000001")
    assert rig.bot.paused is True

    resultado = rig.bot.run_cycle(rig.analysis)
    assert resultado.action == "PAUSED"
    assert rig.sim.positions(MAGIC) == []
    assert list(rig.store.read_open_positions()) == []
    assert _rows(rig.db_path, "SELECT 1 FROM order_attempts") == []
    assert rig.daily.counters(DAY).entries_used == 0
    # El estado publicado es el que lee el panel: no hay un "pausado" paralelo en memoria.
    assert rig.store.read_bot_state()["state"] == "PAUSADO"

    # Reanudar devuelve el ciclo a la decisión normal.
    _apply(rig, CommandType.RESUME, "resume-ui-000002")
    assert rig.bot.run_cycle(rig.analysis).action == "ENTER"
    assert rig.store.read_bot_state()["state"] == "OPERATIVO"


def test_pausa_con_posicion_abierta_no_cierra_ni_repite_entrada(tmp_path: Path) -> None:
    from tests.integration.test_fase4_bot import _rig

    rig = _rig(tmp_path)
    # 1) Se abre una posición con el bot en régimen normal.
    primera = rig.bot.run_cycle(rig.analysis)
    assert primera.action == "ENTER"
    ticket = rig.sim.positions(MAGIC)[0].ticket

    # 2) Pausar NO cierra: la posición sigue viva en el broker y en la base.
    _apply(rig, CommandType.PAUSE, "pause-ui-000003")
    assert [p.ticket for p in rig.sim.positions(MAGIC)] == [ticket]
    assert list(rig.store.read_open_positions()) == [ticket]
    intentos_antes = len(_rows(rig.db_path, "SELECT 1 FROM order_attempts"))

    # 3) Con la pausa activa el ciclo es de gestión: no se envía ninguna orden nueva.
    siguiente = rig.bot.run_cycle(rig.analysis)
    assert siguiente.action == "MANAGE"
    assert [p.ticket for p in rig.sim.positions(MAGIC)] == [ticket]
    assert len(_rows(rig.db_path, "SELECT 1 FROM order_attempts")) == intentos_antes
    assert rig.daily.counters(DAY).entries_used == 1
    assert rig.store.read_bot_state()["state"] == "PAUSADO"

    # 4) Reanudar no reabre (ya hay posición) pero devuelve el estado a OPERATIVO.
    _apply(rig, CommandType.RESUME, "resume-ui-000004")
    assert rig.bot.paused is False
    assert rig.bot.run_cycle(rig.analysis).action == "MANAGE"
    assert rig.store.read_bot_state()["state"] == "OPERATIVO"
    assert len(_rows(rig.db_path, "SELECT 1 FROM order_attempts")) == intentos_antes


def test_la_pausa_sigue_gestionando_la_posicion_abierta(tmp_path: Path) -> None:
    """Pausa no es "congelar": mover el stop a favor sigue siendo defensivo."""
    from tests.integration.test_fase4_bot import _bar, _rig

    rig = _rig(tmp_path)
    rig.bot.run_cycle(rig.analysis)
    _apply(rig, CommandType.PAUSE, "pause-ui-000005")

    sl_antes = rig.sim.positions(MAGIC)[0].sl
    # Vela a favor (r = 1.40, sin tocar el TP de 2408.10): el breakeven se mueve pese a la pausa.
    rig.sim.advance(_bar(2_406.0, 2_405.0, 2_406.5))
    resultado = rig.bot.run_cycle(rig.analysis)

    assert resultado.action == "MANAGE"
    assert rig.sim.positions(MAGIC)[0].sl == pytest.approx(2_400.42)
    assert rig.sim.positions(MAGIC)[0].sl > sl_antes
    assert rig.store.read_open_positions()[rig.sim.positions(MAGIC)[0].ticket].breakeven_done is True


def test_el_dashboard_ve_la_pausa_que_escribio_el_bot(tmp_path: Path) -> None:
    """El botón y el estado leído son el mismo hecho: el panel no inventa la pausa."""
    from tests.integration.test_fase4_bot import _rig

    rig = _rig(tmp_path)
    clock = _FrozenClock(rig.clock.now_utc())
    client = _client(rig.db_path, clock, _config())

    # Primer ciclo: el bot publica su estado antes de que exista comando alguno.
    assert rig.bot.run_cycle(rig.analysis).action == "ENTER"
    antes = client.get("/api/overview").json()
    assert antes["header"]["state"] == "OPERATIVO"
    assert antes["header"]["paused"] is False
    assert antes["chips"][4]["ok"] is True

    _apply(rig, CommandType.PAUSE, "pause-ui-000006")
    # Todavía no hay foto nueva: el panel sigue mostrando lo último publicado, no "PAUSADO".
    assert client.get("/api/overview").json()["header"]["paused"] is False

    rig.bot.run_cycle(rig.analysis)
    despues = client.get("/api/overview").json()
    assert despues["header"]["state"] == "PAUSADO"
    assert despues["header"]["paused"] is True
    assert despues["chips"][4]["ok"] is False
    assert "pausado" in despues["chips"][4]["value"]
    assert despues["footer"]["counts"]["commands"] >= 1
    eventos = client.get("/api/events?limit=60").json()["rows"]
    assert [e["type"] for e in eventos if e["type"] == "PAUSE"] == ["PAUSE"]


def test_close_all_del_panel_cierra_lo_propio_y_deja_rastro(tmp_path: Path) -> None:
    """`Cerrar todo` es el botón más peligroso: se prueba de punta a punta, no sólo la cola."""
    from tests.integration.test_fase4_bot import _rig

    from goldbot.domain.events import CommandEvent

    rig = _rig(tmp_path)
    clock = _FrozenClock(rig.clock.now_utc())
    client = _client(rig.db_path, clock, _config())

    assert rig.bot.run_cycle(rig.analysis).action == "ENTER"
    ticket = rig.sim.positions(MAGIC)[0].ticket
    assert list(rig.store.read_open_positions()) == [ticket]

    # El comando entra por la ruta real: HTTP con token -> cola -> bot lo reclama y lo ejecuta.
    aceptado = client.post(
        "/api/command",
        json={"command": "close_all", "idempotency_key": "close-all-ui-000007"},
        headers=_auth(),
    )
    assert aceptado.status_code == 200 and aceptado.json()["status"] == "queued"
    claimed = rig.store.claim_next_command(now_utc=rig.clock.now_utc())
    assert claimed is not None and claimed.command is CommandType.CLOSE_ALL
    ejecutado = rig.bot.step(
        CommandEvent(command=claimed, received_at_utc=rig.clock.now_utc())
    )
    assert ejecutado.action == "COMMAND"

    # 1) La posición ya no existe en el broker y el comando quedó cerrado en DONE.
    assert rig.sim.positions(MAGIC) == []
    comando = _rows(rig.db_path, "SELECT status, completed_at_utc FROM commands WHERE command = 'CLOSE_ALL'")
    assert len(comando) == 1 and comando[0]["status"] == "DONE"
    assert comando[0]["completed_at_utc"] is not None

    # 2) El ciclo siguiente concilia el deal: la operación aparece en `trades` y deja de
    #    estar en `positions`, que es lo que pinta el panel de historial.
    rig.bot.run_cycle(rig.analysis)
    assert list(rig.store.read_open_positions()) == []
    cerradas = _rows(rig.db_path, "SELECT position_id, exit_reason, net_profit FROM trades")
    assert [int(row["position_id"]) for row in cerradas] == [ticket]
    assert cerradas[0]["net_profit"] is not None

    # 3) Queda el rastro auditable: evento del cierre y comando visible en el panel.
    eventos = client.get("/api/events?limit=60").json()["rows"]
    tipos = [e["type"] for e in eventos]
    assert "COMMAND" in tipos and "CLOSED" in tipos
    # El orden de escritura es el orden de los hechos: primero el acta del panel, después
    # el cierre del bot. Con reloj congelado la lista de la API no ordena por instante.
    por_id = [row["type"] for row in _rows(rig.db_path, "SELECT type FROM events ORDER BY id")]
    assert por_id.index("COMMAND") < por_id.index("CLOSED")
    audit = client.get("/api/commands?limit=20").json()["rows"]
    assert [row["command"] for row in audit] == ["CLOSE_ALL"]
    assert audit[0]["status"] == "DONE"

    # 4) Cerrar todo no bloquea el día: al ciclo siguiente el bot vuelve a decidir.
    assert rig.bot.run_cycle(rig.analysis).action in {"ENTER", "MANAGE", "NO_SETUP", "PAUSED"}
    assert rig.store.read_bot_state()["state"] == "OPERATIVO"

