-- Esquema inicial del StateStore (SQLite WAL).
-- Convención: todas las marcas de tiempo son TEXT ISO-8601 en UTC (sufijo Z).
-- Migraciones gestionadas por infra/migrations.py con PRAGMA user_version.

PRAGMA foreign_keys = ON;

-- Estado agregado del bot: una única fila. El dashboard la lee para la cabecera,
-- KPIs, chips y panel "Cerebro".
CREATE TABLE IF NOT EXISTS bot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    mode TEXT NOT NULL CHECK (mode IN ('DEMO', 'LIVE')),
    requested_symbol TEXT NOT NULL,
    resolved_symbol TEXT,
    magic INTEGER NOT NULL,
    account_json TEXT,
    context_json TEXT,
    regime_json TEXT,
    playbook_json TEXT,
    filters_json TEXT,
    feed_status TEXT,
    last_cycle_at_utc TEXT,
    cycle_seq INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_utc TEXT,
    kill_switch_latched INTEGER NOT NULL DEFAULT 0 CHECK (kill_switch_latched IN (0, 1)),
    kill_reason TEXT,
    config_hash TEXT,
    app_version TEXT,
    server_offset_min INTEGER,
    updated_at_utc TEXT NOT NULL
);

-- Equity por minuto. Los huecos se detectan por delta de ts_utc y NUNCA se interpolan.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_login INTEGER NOT NULL,
    ts_utc TEXT NOT NULL,
    ts_utc_minute TEXT NOT NULL,
    balance REAL NOT NULL,
    equity REAL NOT NULL,
    margin_free REAL,
    floating_pnl REAL,
    positions_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL CHECK (source IN ('PERIODIC', 'TRADE', 'STARTUP')),
    UNIQUE (account_login, ts_utc_minute)
);

CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots (ts_utc DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('BOT', 'MT5', 'DASHBOARD')),
    type TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    correlation_id TEXT,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts_utc DESC);

-- Una fila por ciclo de decisión, con snapshot completo para reproducir el razonamiento.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    bar_time_utc TEXT,
    action TEXT NOT NULL,
    snapshot_hash TEXT,
    config_hash TEXT,
    model_versions_json TEXT,
    regime_json TEXT,
    probs_json TEXT,
    playbook_id TEXT,
    signal_json TEXT,
    filters_json TEXT,
    risk_json TEXT,
    rationale TEXT,
    latency_ms INTEGER,
    snapshot_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts_utc DESC);

-- Foto de posiciones abiertas gestionadas por el bot (no es el historial de MT5).
CREATE TABLE IF NOT EXISTS positions (
    ticket INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    volume REAL NOT NULL,
    entry_price REAL NOT NULL,
    sl REAL,
    tp REAL,
    opened_at_utc TEXT,
    playbook_id TEXT,
    regime_at_entry TEXT,
    decision_id_entry TEXT,
    initial_risk_price REAL,
    r_multiple REAL,
    last_action TEXT,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_position_id ON positions (position_id);

-- Historial conciliado de operaciones cerradas. El motivo de salida es propio del bot
-- y se une por position_id; los cierres externos se marcan como EXTERNAL.
CREATE TABLE IF NOT EXISTS trades (
    position_id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    volume REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    sl REAL,
    tp REAL,
    open_time_utc TEXT NOT NULL,
    close_time_utc TEXT NOT NULL,
    entry_deal_id INTEGER,
    close_deal_id INTEGER,
    commission REAL NOT NULL DEFAULT 0,
    swap REAL NOT NULL DEFAULT 0,
    net_profit REAL,
    r_multiple REAL,
    duration_s INTEGER,
    exit_reason TEXT,
    exit_reason_source TEXT CHECK (exit_reason_source IN ('BOT', 'SL', 'TP', 'EXTERNAL', 'RECON')),
    playbook_id TEXT,
    regime_at_entry TEXT,
    decision_id_entry TEXT,
    reconciled_at_utc TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_close_time ON trades (close_time_utc DESC);
CREATE INDEX IF NOT EXISTS idx_trades_open_time ON trades (open_time_utc DESC);

-- Comandos del dashboard con ciclo de vida completo e idempotencia.
CREATE TABLE IF NOT EXISTS commands (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    command TEXT NOT NULL,
    args_json TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'CLAIMED', 'DONE', 'FAILED', 'EXPIRED')),
    created_at_utc TEXT NOT NULL,
    claimed_at_utc TEXT,
    completed_at_utc TEXT,
    expires_at_utc TEXT,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_commands_status_seq ON commands (status, seq);

-- Estadísticas por día operativo en hora del servidor; se crea fila en cada rollover
-- para que el gráfico de 14 días incluya días sin operar.
CREATE TABLE IF NOT EXISTS daily_stats (
    trading_day TEXT PRIMARY KEY,
    tz TEXT NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    equity_start REAL,
    equity_end REAL,
    entries_used INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    gross_profit REAL NOT NULL DEFAULT 0,
    gross_loss REAL NOT NULL DEFAULT 0,
    daily_lock_latched INTEGER NOT NULL DEFAULT 0 CHECK (daily_lock_latched IN (0, 1)),
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT,
    cycle_id TEXT,
    message TEXT NOT NULL,
    extra_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_lines_ts ON log_lines (ts_utc DESC);

-- Pie del dashboard: última sincronización del historial desde MT5.
CREATE TABLE IF NOT EXISTS history_sync (
    key TEXT PRIMARY KEY,
    last_sync_utc TEXT,
    last_deal_ts_utc TEXT
);

-- Caché reproducible de eventos del calendario (CSV del puente MQL5; también backtest).
CREATE TABLE IF NOT EXISTS calendar_events (
    event_id TEXT PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    currency TEXT,
    impact TEXT,
    title TEXT,
    cached_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calendar_ts ON calendar_events (ts_utc);

-- Auditoría de cada intento de ejecución y su retcode.
CREATE TABLE IF NOT EXISTS order_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    decision_id TEXT,
    request_json TEXT NOT NULL,
    retcode INTEGER,
    retcode_name TEXT,
    retryable INTEGER,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_order_attempts_ts ON order_attempts (ts_utc DESC);
