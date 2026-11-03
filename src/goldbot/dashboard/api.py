"""Routers del dashboard: lectura del estado del bot y comandos protegidos por token.

Principios que se aplican aquí:

- Todo sale de la base SQLite que escribe el bot (`readmodel`). El dashboard no abre MT5
  ni decide nada: si un dato no existe, se devuelve `None` con su antigüedad, nunca un
  valor supuesto.
- Cada payload lleva `*_staleness` por fuente para que el frontend pinte "dato atrasado"
  en el panel concreto, no en toda la página.
- Los únicos comandos admisibles son `pause`, `resume`, `close_all` y `reset_kill_switch`.
  No existe ninguna ruta para abrir una operación ni para cambiar parámetros de riesgo:
  el modelo de entrada (`CommandRequest`) no tiene campos para ello.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from goldbot.dashboard.auth import token_dependency
from goldbot.dashboard.readmodel import DashboardReadModel
from goldbot.dashboard.schemas import CommandRequest, CommandResponse, HealthResponse
from goldbot.domain.interfaces import Clock, StateStore
from goldbot.domain.types import EventRecord, EventSource, EventType

# `PAUSE`/`RESUME`/`RESET_KILL_SWITCH` no caducan: si el bot está caído, la orden de pausar
# debe cumplirse al recuperar, no perderse. `CLOSE_ALL` sí caduca: cerrar posiciones horas
# después de que el operador lo pidiera no es lo que él pidió.
_NO_EXPIRY: frozenset[str] = frozenset({"pause", "resume", "reset_kill_switch"})


def _command_type(name: str) -> Any:
    """Traduce el nombre del panel al `CommandType` del dominio."""
    from goldbot.domain.types import CommandType  # noqa: PLC0415 - mapeo 1:1 con el Literal

    return {
        "pause": CommandType.PAUSE,
        "resume": CommandType.RESUME,
        "close_all": CommandType.CLOSE_ALL,
        "reset_kill_switch": CommandType.RESET_KILL_SWITCH,
    }[name]


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _account_blob(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    raw = state.get("account_json")
    return raw if isinstance(raw, dict) else None


def _context_blob(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    raw = state.get("context_json")
    return raw if isinstance(raw, dict) else None


def _health_blob(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    raw = state.get("health_json")
    return raw if isinstance(raw, dict) else None


def _chip(value: Any, *, ok: bool | None = None, label: str | None = None) -> dict[str, Any]:
    return {"label": label, "value": value, "ok": ok}


def _switch_chip(value: Any, label: str) -> dict[str, Any]:
    """ON/OFF para un interruptor del terminal; `None` es 'no lo sabemos', no 'OFF'.

    Pintar OFF cuando el bot nunca publicó salud del broker afirmaría algo falso: con
    `algo trading` en OFF el operador cree que el terminal está bloqueado cuando en
    realidad simplemente no hay lectura.
    """
    if value is None:
        return _chip("sin lectura", label=label, ok=None)
    return _chip("ON" if value else "OFF", label=label, ok=bool(value))


def _number(value: Any, digits: int = 2) -> float | None:
    """Redondea para pantalla sin convertir un `None` en cero: ausencia no es cero."""
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _trades_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Resumen honesto del periodo: sin operaciones dice 0, no inventa métricas."""
    closed = [row for row in rows if row.get("net_profit") is not None]
    if not closed:
        return {
            "trades": 0,
            "win_rate_pct": None,
            "profit_factor": None,
            "expectancy_r": None,
            "net_usd": 0.0,
            "gross_profit_usd": 0.0,
            "gross_loss_usd": 0.0,
        }
    wins = [row for row in closed if float(row["net_profit"]) > 0.0]
    losses = [row for row in closed if float(row["net_profit"]) < 0.0]
    gross_profit = sum(float(row["net_profit"]) for row in wins)
    gross_loss = sum(float(row["net_profit"]) for row in losses)
    r_multiples = [float(row["r_multiple"]) for row in closed if row.get("r_multiple") is not None]
    profit_factor: float | None
    if gross_loss == 0.0:
        profit_factor = None if gross_profit == 0.0 else float("inf")
    else:
        profit_factor = round(gross_profit / abs(gross_loss), 3)
    return {
        "trades": len(closed),
        "win_rate_pct": round(100.0 * len(wins) / len(closed), 1),
        "profit_factor": profit_factor,
        "expectancy_r": (
            round(sum(r_multiples) / len(r_multiples), 3) if r_multiples else None
        ),
        "net_usd": round(gross_profit + gross_loss, 2),
        "gross_profit_usd": round(gross_profit, 2),
        "gross_loss_usd": round(gross_loss, 2),
    }


def _entries_gate(
    state: dict[str, Any] | None,
    day: dict[str, Any] | None,
    latest_decision: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Decide si el bot puede abrir operaciones, solo con lo que el bot publicó.

    No se recalcula la ventana horaria ni el filtro de noticias: se reporta el veredicto
    que el bot ya emitió en su última decisión, para que el panel no mienta cuando el
    bot lleva rato sin ciclo.
    """
    if state is None:
        return False, "sin estado publicado del bot"
    if bool(state.get("kill_switch_latched")):
        reason = str(state.get("kill_reason") or "sin motivo registrado")
        return False, f"kill switch activado ({reason})"
    if bool(state.get("paused")):
        return False, "pausado desde el dashboard"
    if day is not None and bool(day.get("daily_lock_latched")):
        return False, "pérdida diaria máxima alcanzada"
    feed = str(state.get("feed_status") or "")
    if feed and feed.upper() != "OK":
        return False, f"feed de datos {feed.lower()}"
    filters = (latest_decision or {}).get("filters_json")
    if isinstance(filters, dict) and filters.get("blocked"):
        return False, str(filters.get("primary_reason") or "filtro previo bloquea")
    if latest_decision is None:
        return False, "el bot aún no evaluó ninguna vela cerrada"
    return True, "ventana de decisión sin bloqueos"


def build_read_router(
    *, rm: DashboardReadModel, clock: Clock, config: Any, stale_after_s: int
) -> APIRouter:
    """Expone estado, KPIs, cerebro, posiciones, gráficos, historial, eventos y log."""
    router = APIRouter()

    # `stale_after_s` del dashboard mide escrituras por poll (5 s). La decisión y el contexto que
    # la acompaña solo se escriben al cerrar la vela de decisión, así que con 15 s el panel estaría
    # ambarado también con un bot perfecto: una vela M5 tarda 300 s. Se usa la cadencia ya
    # declarada por timeframe en `data.stale_after_s` (M5 -> 360 s = una vela + margen) y se
    # respeta el umbral del dashboard si alguien lo configuró más alto.
    _cadencia = dict(getattr(config.data, "stale_after_s", {}) or {})
    decision_after_s = max(
        stale_after_s, int(_cadencia.get(str(config.data.decision_timeframe), 360))
    )
    # La curva de equity tampoco sigue la cadencia del poll: la fila se guarda con la marca del
    # minuto en curso, así que la edad del último punto real sube de 0 a ~60 s y vuelve a caer.
    # Con los 15 s del poll el gráfico viviría ambarado la mayor parte del minuto.
    equity_after_s = max(stale_after_s, int(config.dashboard.equity_stale_after_s))

    def server_time_block() -> dict[str, Any]:
        now = clock.now_utc()
        server_naive = clock.to_server(now)
        return {
            "utc": _iso(now),
            "server": server_naive.isoformat(timespec="seconds"),
            "server_zone": config.app.server_timezone,
            "offset_min": round(clock.server_offset(now).total_seconds() / 60),
        }

    @router.get("/api/overview")
    def overview() -> dict[str, Any]:
        """Cabecera, KPIs, chips, posiciones y pie: los paneles que van arriba."""
        state = rm.bot_state()
        account = _account_blob(state)
        context = _context_blob(state)
        health = _health_blob(state)
        day: dict[str, Any] | None = None
        if state is not None:
            day_iso = str(state.get("last_cycle_at_utc") or clock.now_utc().date().isoformat())
            day = rm.day_row(day_iso[:10])
        latest = rm.latest_decision()
        positions = rm.open_positions()

        equity = _number((account or {}).get("equity"))
        balance = _number((account or {}).get("balance"))
        floating = None if equity is None or balance is None else round(equity - balance, 2)
        realized = _number((day or {}).get("realized_pnl"))
        day_pnl = (realized or 0.0) + (floating or 0.0) if day or floating else None
        equity_start = _number((day or {}).get("equity_start"))
        limit_usd = (
            None
            if equity_start is None
            else round(equity_start * config.risk.daily_loss_pct / 100.0, 2)
        )
        used = 0.0 if day_pnl is None else max(0.0, -day_pnl)
        progress = None if not limit_usd else round(min(1.0, used / limit_usd), 4)

        entries_on, entries_reason = _entries_gate(state, day, latest)
        timing = (context or {}).get("timing") or {}
        freshness = (context or {}).get("freshness") or {}
        risk_json = (latest or {}).get("risk_json") or {}
        last_equity_row = rm.equity_grid(24)
        # La marca del panel de posiciones es la del ciclo que lo produce, no la de sus filas.
        # `_reconcile_closed` borra la fila cuando el broker ya no la tiene, pero
        # `positions.updated_at_utc` sólo se reescribe cuando la gestión actúa: una posición viva
        # que no necesita acción la deja congelada horas. Medido en demo, con el enlace perfecto y
        # el ciclo al día, el chip se pintaba en ámbar a los 60 y a los 82 s. La única escritura
        # que ocurre en CADA `run_cycle` — actúe la gestión o no — es el estampo de `daily_stats`
        # (`ensure_day` y `record_equity_end`, antes de la rama de posiciones), así que ésa es la
        # edad real de la última reconciliación y se juzga con el paso de la vela de decisión.
        ciclo_reconciliacion = max(
            (str(row["updated_at_utc"]) for row in rm.daily_stats(2)), default=None
        )

        # El feed sólo es "OK" si el bot sigue publicando: `feed_status` es lo último que
        # escribió, y un bot muerto hace 10 minutos dejó de escribirlo con el enlace vivo.
        feed_status = str((state or {}).get("feed_status") or "sin dato")
        state_age = rm.age_s((state or {}).get("updated_at_utc"))
        feed_live = state_age is not None and state_age <= stale_after_s
        feed_ok = bool(state) and feed_status.upper() == "OK" and feed_live
        feed_value = (
            feed_status
            if feed_live or state is None
            else f"{feed_status} · sin ciclos hace {int(state_age or 0)} s"
        )

        chips = [
            _chip(
                _number(timing.get("spread_points"), 1),
                label="spread actual (puntos)",
                ok=None if timing.get("spread_points") is None else True,
            ),
            _chip(
                _number(timing.get("atr"), 2),
                label=f"ATR {str((context or {}).get('decision_timeframe') or 'TF de decisión')}",
            ),
            _chip(
                _number(risk_json.get("sl_distance_price"), 2),
                label="distancia de stop (precio)",
            ),
            _chip(
                str((context or {}).get("session") or "desconocida"),
                label="sesión",
            ),
            _chip(entries_reason, label="entradas", ok=entries_on),
            _chip(
                f"{len(positions)}/{config.risk.max_trades_day}",
                label="posiciones (una a la vez por diseño)",
                ok=len(positions) == 0,
            ),
            _chip(
                f"{int((day or {}).get('entries_used') or 0)}/{config.risk.max_trades_day}",
                label="entradas usadas hoy",
            ),
            _switch_chip(
                (health or {}).get("algo_trading_enabled") if health else None,
                "algo trading (terminal)",
            ),
            _switch_chip(
                (health or {}).get("account_trade_allowed") if health else None,
                "trading permitido (cuenta)",
            ),
            _chip(
                feed_value,
                label="feed MT5",
                ok=feed_ok,
            ),
        ]

        return {
            "header": {
                "name": "goldbot",
                "app_version": (state or {}).get("app_version"),
                "symbol": (state or {}).get("resolved_symbol") or (state or {}).get(
                    "requested_symbol"
                ),
                "requested_symbol": (state or {}).get("requested_symbol"),
                "mode": (state or {}).get("mode"),
                "is_demo": bool((account or {}).get("is_demo")),
                "account": {
                    "login": (account or {}).get("login"),
                    "server": (account or {}).get("server"),
                    "currency": (account or {}).get("currency"),
                },
                "state": (state or {}).get("state"),
                "paused": bool((state or {}).get("paused")),
                "kill_switch_latched": bool((state or {}).get("kill_switch_latched")),
                "kill_reason": (state or {}).get("kill_reason"),
                "entry_windows": list(config.sessions.entries),
                "avoid_rollover": bool(config.sessions.avoid_rollover),
                "cycle_seq": (state or {}).get("cycle_seq"),
                "config_hash": (state or {}).get("config_hash"),
                "server_time": server_time_block(),
                "uptime_s": rm.age_s(rm.last_event_ts("STARTUP")),
                "last_startup_utc": rm.last_event_ts("STARTUP"),
            },
            "kpis": {
                "balance_usd": balance,
                "equity_usd": equity,
                "floating_usd": floating,
                "day_realized_usd": realized,
                "day_total_usd": _number(day_pnl),
                "daily_limit_usd": limit_usd,
                "daily_limit_used_pct": progress,
                "daily_loss_pct": config.risk.daily_loss_pct,
                "risk_pct": config.risk.risk_pct,
                "base_calculo": "equity del inicio del día operativo",
                "daily_lock_latched": bool((day or {}).get("daily_lock_latched")),
            },
            "chips": chips,
            "positions": [
                {
                    **position,
                    "entry_age_s": rm.age_s(position.get("opened_at_utc")),
                    "photo_age_s": rm.age_s(position.get("updated_at_utc")),
                }
                for position in positions
            ],
            "footer": {
                "magic": (state or {}).get("magic"),
                "mode": (state or {}).get("mode"),
                "account_login": (account or {}).get("login"),
                "closed_trades_30d": len(rm.trades(30)),
                "counts": rm.counts(),
                "history_sync": rm.last_history_sync(),
                "equity_points_24h": last_equity_row["points"],
            },
            "staleness": {
                "bot_state": rm.stale_flag((state or {}).get("updated_at_utc")),
                "account": rm.stale_flag((state or {}).get("updated_at_utc")),
                "context": rm.stale_flag(
                    (context or {}).get("data_cutoff_utc"), after_s=decision_after_s
                ),
                "health": rm.stale_flag((health or {}).get("checked_at_utc")),
                "decision": rm.stale_flag((latest or {}).get("ts_utc"), after_s=decision_after_s),
                "equity_series": rm.stale_flag(
                    last_equity_row["last_ts_utc"], after_s=equity_after_s
                ),
                # Con la lista vacía no hay fila que pueda estar atrasada y con una viva la fila no
                # avanza si la gestión no actúa: en los dos casos el bloque vale lo que vale el
                # ciclo que lo reconcilia. Sin `daily_stats` (nadie ha corrido un `run_cycle`) no
                # hay ciclo que medir, y el panel lo dice como dice un enlace caído.
                # Con la lista vacía no hay fila que pueda estar atrasada y con una viva la fila no
                # avanza si la gestión no actúa: en los dos casos el bloque vale lo que vale el
                # ciclo que lo reconcilia. Sin `daily_stats` (nadie ha corrido un `run_cycle`) no
                # hay ciclo que medir, y el panel lo dice como dice un enlace caído.
                "positions": rm.stale_flag(ciclo_reconciliacion, after_s=decision_after_s),
            },
            "feed_freshness": freshness,
            # Parámetros que el navegador necesita para no adiviarlos: cadencia de refresco,
            # umbral de "dato atrasado" y si el botón "Cerrar todo" existe o no.
            "view": {
                "refresh_s": config.dashboard.refresh_s,
                "stale_after_s": stale_after_s,
                "close_all_enabled": bool(config.dashboard.close_all_enabled),
                "close_all_ttl_s": int(config.dashboard.close_all_ttl_s),
                "token_env": config.dashboard.token_env,
                "token_present": bool(os.getenv(config.dashboard.token_env, "")),
            },
        }

    @router.get("/api/brain")
    def brain(
        limit: int = Query(default=12, ge=1, le=50),
    ) -> dict[str, Any]:
        """Panel "cerebro": régimen, playbook, filtros, sesgos, niveles e historial."""
        latest = rm.latest_decision()
        history = rm.decisions(limit)
        state = rm.bot_state()
        context = latest.get("snapshot_json") if latest else _context_blob(state)

        def slim_decision(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "decision_id": row["decision_id"],
                "cycle_id": row["cycle_id"],
                "ts_utc": row["ts_utc"],
                "bar_time_utc": row["bar_time_utc"],
                "action": row["action"],
                "playbook_id": row["playbook_id"],
                "rationale": row["rationale"],
                "regime": row.get("regime_json"),
                "probs": row.get("probs_json"),
                "filters": row.get("filters_json"),
                "signal": row.get("signal_json"),
                "risk": row.get("risk_json"),
                "snapshot_hash": row["snapshot_hash"],
                "latency_ms": row["latency_ms"],
            }

        regime_json = (latest or {}).get("regime_json")
        return {
            "latest": slim_decision(latest) if latest else None,
            "regime": regime_json,
            "probs": (latest or {}).get("probs_json"),
            "playbook": (latest or {}).get("playbook_id"),
            "filters": (latest or {}).get("filters_json"),
            "rationale": (latest or {}).get("rationale"),
            "bias": (context or {}).get("bias") if isinstance(context, dict) else None,
            "structure": (context or {}).get("structure") if isinstance(context, dict) else None,
            "timing": (context or {}).get("timing") if isinstance(context, dict) else None,
            "session": (context or {}).get("session") if isinstance(context, dict) else None,
            "bar_time_utc": (context or {}).get("bar_time_utc") if isinstance(context, dict) else None,
            "model_versions": (latest or {}).get("model_versions_json"),
            "history": [slim_decision(row) for row in history],
            "staleness": {
                "decision": rm.stale_flag((latest or {}).get("ts_utc"), after_s=decision_after_s),
                "snapshot": rm.stale_flag(
                    (context or {}).get("data_cutoff_utc") if isinstance(context, dict) else None,
                    after_s=decision_after_s,
                ),
            },
        }

    @router.get("/api/charts/equity")
    def equity_chart(hours: int = Query(default=24, ge=1, le=168)) -> dict[str, Any]:
        grid = rm.equity_grid(hours)
        markers = rm.trade_markers(hours)
        slots = len(grid["x"])
        return {
            "hours": hours,
            "x": grid["x"],
            "equity": grid["equity"],
            "markers": markers,
            "points": grid["points"],
            # Resumen por texto: la línea del gráfico no comunica solas la cobertura ni el
            # tamaño del corte, y el requisito pide estado y resumen del gráfico.
            "summary": {
                "window_min": slots,
                "filled_min": grid["points"],
                "gap_min": max(0, slots - grid["points"]),
                "coverage_pct": round(100.0 * grid["points"] / slots, 1) if slots else 0.0,
                "first_ts_utc": grid["first_ts_utc"],
                "last_ts_utc": grid["last_ts_utc"],
                "current_gap_s": rm.age_s(grid["last_ts_utc"]),
                "min_equity": _number(grid["min_equity"]),
                "max_equity": _number(grid["max_equity"]),
                "last_equity": _number(grid["last_equity"]),
            },
            "staleness": rm.stale_flag(grid["last_ts_utc"], after_s=equity_after_s),
        }

    @router.get("/api/charts/daily-pnl")
    def daily_pnl(days: int = Query(default=14, ge=1, le=90)) -> dict[str, Any]:
        rows = rm.daily_stats(days)
        rows.reverse()  # eje X cronológico
        return {
            "days": [row["trading_day"] for row in rows],
            "realized_usd": [_number(row["realized_pnl"]) for row in rows],
            "entries": [int(row["entries_used"] or 0) for row in rows],
            "locked": [bool(row["daily_lock_latched"]) for row in rows],
            # Series paralelas para uPlot + filas por día para el resumen en texto.
            # Se omite la columna `tz`: `trading_day` se calcula con la hora del servidor y la
            # columna está fijada a 'UTC' desde Fase 4, así que mostrarla mentiría.
            "rows": [
                {
                    "trading_day": row["trading_day"],
                    "realized_usd": _number(row["realized_pnl"]),
                    "equity_start": _number(row["equity_start"]),
                    "equity_end": _number(row["equity_end"]),
                    "entries": int(row["entries_used"] or 0),
                    "wins": int(row["wins"] or 0),
                    "losses": int(row["losses"] or 0),
                    "locked": bool(row["daily_lock_latched"]),
                }
                for row in rows
            ],
            "staleness": {
                "daily": rm.stale_flag(
                    max((str(r["updated_at_utc"]) for r in rows), default=None),
                    after_s=decision_after_s,
                )
            },
        }

    @router.get("/api/trades")
    def trades(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
        rows = rm.trades(days)
        return {
            "days": days,
            "summary": _trades_summary(rows),
            "rows": rows,
            "staleness": rm.stale_flag(
                max((str(row["close_time_utc"]) for row in rows), default=None)
            ),
        }

    @router.get("/api/events")
    def events(limit: int = Query(default=60, ge=1, le=500)) -> dict[str, Any]:
        rows = rm.events(limit)
        return {
            "rows": rows,
            "staleness": rm.stale_flag(rows[0]["ts_utc"] if rows else None),
        }

    @router.get("/api/logs")
    def logs(limit: int = Query(default=160, ge=1, le=1_000)) -> dict[str, Any]:
        rows = rm.log_lines(limit)
        return {
            "rows": rows,
            "staleness": rm.stale_flag(rows[-1]["ts_utc"] if rows else None),
        }

    @router.get("/api/commands")
    def commands(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"rows": rm.commands(limit)}

    @router.get("/api/order-attempts")
    def order_attempts(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"rows": rm.order_attempts(limit)}

    @router.get("/api/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        reachable = rm.db_reachable()
        state = rm.bot_state() if reachable else None
        age = rm.age_s((state or {}).get("updated_at_utc"))
        degraded = (not reachable) or age is None or age > stale_after_s
        return HealthResponse(
            status="degraded" if degraded else "ok",
            db_reachable=reachable,
            feed_age_s=None if age is None else round(age, 1),
        )

    return router


def build_command_router(
    *, store: StateStore, config: Any, rm: DashboardReadModel, clock: Clock
) -> APIRouter:
    """Expone Pausa/Reanudar/Cerrar todo/Reset con token obligatorio e idempotencia."""
    router = APIRouter()
    token = os.getenv(config.dashboard.token_env, "")

    @router.post("/api/command", response_model=CommandResponse)
    def enqueue(request: CommandRequest, _: None = Depends(token_dependency(token))) -> CommandResponse:
        if not rm.db_reachable():
            # Sin base del bot no hay canal: el dashboard NO la crea (sus tablas y su WAL
            # pertenecen al proceso que opera), simplemente rechaza la orden.
            raise HTTPException(
                status_code=503,
                detail="la base del bot no existe todavía; no hay canal de comandos",
            )
        if request.command == "close_all" and not config.dashboard.close_all_enabled:
            raise HTTPException(
                status_code=403,
                detail="cerrar todo está deshabilitado en la configuración (dashboard.close_all_enabled)",
            )

        command_type = _command_type(request.command)
        existing = {str(row["idempotency_key"]): row for row in rm.commands(200)}
        duplicate = existing.get(request.idempotency_key)
        ttl_s = 0 if request.command in _NO_EXPIRY else int(config.dashboard.close_all_ttl_s)

        command_id = store.enqueue_command(
            command_type,
            {},
            idempotency_key=request.idempotency_key,
            origin="DASHBOARD",
            ttl_s=ttl_s,
        )
        if duplicate is None:
            store.write_event(
                EventRecord(
                    ts_utc=clock.now_utc(),
                    source=EventSource.DASHBOARD,
                    type=EventType.COMMAND,
                    level=_command_level(request.command),
                    message=f"comando desde el dashboard: {request.command}",
                    payload={
                        "command_id": command_id,
                        "command": request.command,
                        "idempotency_key": request.idempotency_key,
                        "ttl_s": ttl_s,
                    },
                )
            )
            return CommandResponse(command_id=command_id, status="queued")
        return CommandResponse(command_id=command_id, status="duplicate")

    return router


def _command_level(name: str) -> Any:
    """El cierre total y el reset del kill switch se registran como WARNING: son reversibles
    pero visiblemente peligrosos; la pausa y la reanudación son informativas."""
    from goldbot.domain.types import LogLevel  # noqa: PLC0415

    return LogLevel.WARNING if name in {"close_all", "reset_kill_switch"} else LogLevel.INFO
