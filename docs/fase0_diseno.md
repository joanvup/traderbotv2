# Fase 0 — Diseño y esqueleto del bot XAUUSD (`goldbot`)

## 1. Contexto y alcance

El objetivo es un bot discrecional adaptativo para XAUUSD en cuenta DEMO de Pepperstone
(MT5, Windows, 1000 USD), con dashboard local, backtest walk-forward y entrega por fases.

Esta Fase 0 entrega **diseño + esqueleto sin lógica de trading**:

- Estructura de carpetas y reglas de dependencia.
- Dataclasses, Enums y Protocols con firmas y docstrings en español.
- Esquema del StateStore SQLite (WAL, migraciones, comandos idempotentes).
- Configuración YAML validada con pydantic v2.
- Tests de andamiaje y lista de dudas.

**No se conecta a MT5 ni se envía ninguna orden.** Los cuerpos de la lógica lanzan
`NotImplementedError` con la fase en la que se implementarán.

## 2. Decisiones acordadas

| Tema | Decisión |
|---|---|
| Entrega | Documento + stubs Python |
| Entorno MT5 | Terminal de Pepperstone instalado y logueado (ruta a localizar en Fase 1) |
| Credenciales | Sesión del terminal; login opcional solo desde `.env` |
| Calendario | Puente MQL5: EA exporta el calendario nativo a CSV; el bot lo lee detrás de `EconomicCalendar` |
| Zona horaria | `America/New_York + 7 h` (GMT+3/GMT+2); todo persistido en UTC |
| Dashboard | FastAPI + estáticos locales, polling 3 s, sin CDN |
| Gráficos | uPlot vendorizado (Fase 5) |
| HMM | `hmmlearn`, con import perezoso |
| Git | No se inicializa sin pedirlo |

## 3. Estructura

```
traderbot/
├─ pyproject.toml  README.md  .gitignore  .env.example
├─ config/config.example.yaml
├─ docs/fase0_diseno.md
├─ mt5/GoldbotCalendarExport.mq5  README.md
├─ scripts/{run_bot,run_dashboard,sync_history,build_calendar_cache}.py
├─ src/goldbot/
│  ├─ config.py
│  ├─ domain/
│  │  ├─ types.py  events.py  interfaces.py  context.py
│  │  ├─ regime/{base,rules,hmm}.py
│  │  ├─ playbooks/{base,trend_pullback,breakout,range_fade,no_trade}.py
│  │  ├─ selector.py  filters.py  risk.py  trade_manager.py  decision_log.py
│  ├─ adapters/
│  │  ├─ mt5/{connection,symbols,orders,history}.py
│  │  ├─ mt5_adapter.py  simulator.py
│  ├─ infra/
│  │  ├─ db/{connection,repositories}.py
│  │  ├─ sql/001_init.sql  migrations.py  state_store.py
│  │  ├─ time.py  calendar.py  logging_setup.py
│  ├─ engine/{bot,guards,kill_switch}.py
│  ├─ backtest/{engine,feed,broker_sim,clock,metrics,walk_forward,report}.py
│  └─ dashboard/{app,api,auth,schemas}.py + static/
└─ tests/{unit,integration}/
```

Regla de dependencias: `domain/` no importa `adapters/`, `infra/` ni `dashboard/`.
`MetaTrader5` se importa **solo** dentro de funciones del adaptador (`load_mt5()`),
de modo que tests y backtest funcionan sin terminal.

## 4. Contratos de dominio

### 4.1 Regla temporal (anti look-ahead)

- Toda vela lleva `open_time_utc`, `close_time_utc`, `is_closed` y `received_at_utc`.
- `ContextSnapshot` congela `decision_time_utc`, `data_cutoff_utc`, `bar_time_utc`,
  `snapshot_hash` y `config_hash`.
- `get_candles(..., closed_only=True, as_of_utc=...)` descarta la vela en formación y
  cualquier dato posterior al corte.
- HMM: `fit(features, train_end_utc)` y `clone()` por ventana walk-forward; normalización
  causal (rolling/expanding), nunca con estadísticas globales.
- El mismo `Bot.step(event)` procesa eventos en vivo y en backtest.

### 4.2 Tipos principales (`domain/types.py`)

`Candle`, `Tick`, `SymbolSpec`, `AccountState`, `BrokerHealth`, `DealRecord`,
`Bias`, `Swing`, `Level`, `StructureState`, `TimingState`, `DataFreshness`,
`ContextSnapshot`, `RegimeProbabilities`, `RegimeState`, `HysteresisState`, `ModelInfo`,
`Signal`, `Selection`, `ManageAction`, `PositionState`,
`OrderRequest`, `OrderResult`, `ValidationIssue`,
`RiskDecision`, `FilterResult`, `FilterOutcome`,
`TradeRecord`, `DailyStats`, `KillSwitchState`, `CycleResult`, `EventRecord`,
`CommandRecord`, `DecisionRecord`, `CalendarEvent`, `CalendarWindow`, `CalendarBlock`.

Enums: `Timeframe`, `Side`, `Session`, `Regime`, `BotState`, `DataStatus`,
`CalendarHealth`, `EventSource`, `EventType`, `LogLevel`, `CommandType`, `CommandStatus`,
`ExitReason`, `ExitReasonSource`, `OrderErrorCategory`, `ManageKind`, `Urgency`,
`FilterCode`, `KillSwitchReason`.

### 4.3 Protocols (`domain/interfaces.py`)

`BrokerAdapter`, `RegimeDetector`, `Playbook`, `PlaybookSelector`, `PreTradeFilters`,
`RiskManager`, `TradeManager`, `EconomicCalendar`, `Clock`, `StateStore`,
`DecisionLogger`, `KillSwitch`, `BotRuntime`.

Además, función pura obligatoria `validate_order(request, spec, tick)` en `domain/risk.py`:
valida SL/TP obligatorios, `stops_level`, `freeze_level`, volumen y filling **antes** de
enviar a MT5.

## 5. StateStore (SQLite WAL)

PRAGMAs: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.
Migraciones con `PRAGMA user_version` y archivos `infra/sql/NNN_*.sql`.

Hay dos escritores (bot y dashboard) en tablas casi disjuntas: transacciones cortas,
`BEGIN IMMEDIATE` y reintentos; nunca una transacción abierta entre llamadas a MT5.

| Tabla | Uso |
|---|---|
| `bot_state` | Fila única con estado, pausa, régimen, filtros, feed, kill switch y offsets |
| `equity_snapshots` | Equity por minuto; huecos detectados por delta, nunca interpolados |
| `events` | Feed de eventos (INICIO, PAUSA, ENTRADA, CLOSED, RECONNECT, KILL_SWITCH…) |
| `decisions` | Una fila por ciclo con snapshot completo y rationale |
| `positions` | Foto de posiciones abiertas con R, playbook y última acción |
| `trades` | Historial conciliado con comisión, swap, motivo y origen del motivo |
| `commands` | Comandos del dashboard con idempotencia, TTL y estados |
| `daily_stats` | Día operativo en hora del servidor, límites y PnL |
| `log_lines` | Últimas 160 líneas para el panel de log (retención 2.000) |
| `history_sync` | Última sincronización del historial (pie del dashboard) |
| `order_attempts` | Auditoría de retcodes y reintentos |

**`calendar_events` no es del bot.** La tabla se crea con el esquema pero ningún código la
escribe: la persistencia real del calendario son los CSV del puente MQL5 y el snapshot congelado
(§17). Dejarla en la lista como "caché reproducible del calendario" era una afirmación
documentada-y-falsa del tipo que detectó la auditoría §16.8.

Ciclo de un comando: dashboard inserta `PENDING` con `idempotency_key`; el bot reclama
(`CLAIMED`), ejecuta y confirma (`DONE/FAILED`), con expiración. `Pausar` se persiste en
`bot_state.paused` para sobrevivir reinicios.

## 6. Configuración

`config/config.example.yaml` cubre: `app` (timezone, magic, rutas), `mt5`, `risk`,
`sessions`, `data` (timeframes, warmup, frescura), `regime`, `playbooks`, `filters`,
`execution`, `management`, `dashboard`, `backtest`, `storage`, `safety`.

Candados implementados en pydantic:

- `risk.max_trades_day <= 2`.
- `dashboard.host` solo loopback.
- `safety.allow_live` exige `require_demo=false` (doble candado).
- `no_trade` siempre presente.
- Timeframes con warmup y umbral de frescura completos.

Credenciales: `.env` (`MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`) y
`DASHBOARD_TOKEN`; nunca en YAML ni en SQLite.

## 7. Blindajes de diseño

- **Filling mode** es bitmask: `allowed_fillings` + `resolve_filling`, sin asumir FOK.
- **`stops_level`/`freeze_level`** en points, validados en dominio antes de enviar.
- **`tick_value`**: usar `trade_tick_value_loss/profit` y `contract_size`; `spec_hash` auditado.
- **Zonas**: MT5 devuelve hora naïve de servidor; `UtcClock` convierte con zona IANA (DST).
- **Historial**: deals ≠ órdenes; cierres parciales, comisión y swap separados;
  reconciliación incremental; motivo propio unido por `position_id`; externos → `EXTERNAL`.
- **Calendario nativo**: la API Python no lo expone; un EA MQL5 lo exporta a
  `Common\Files\goldbot\calendar_events.csv` (`mt5/README.md`), validando frescura,
  esquema y offset; `CalendarHealth` + política `block`/`allow`. Desde v1.10 el puente publica
  además `calendar_history.csv` (histórico profundo) con troceo de peticiones y presupuesto de
  tiempo, y la validación consume un snapshot congelado; ver §17.
- **Reconexión**: MT5 no es thread-safe; un solo hilo, timeouts, backoff, `BrokerHealth`;
  no reintentar `order_send` sin comprobar posiciones/deals por `client_order_id`/magic.
- **Símbolo con sufijo**: resolver con `symbols_get` + override; persistir ambos nombres.
- **Retcodes** 10004/10006/10018/10027/10030/10016: `retryable` y categoría; 10027 → kill switch.
- **Demo guard** implementado en `engine/guards.py`: falla cerrado si la cuenta no es DEMO.
- **Kill switch latcheado** con reset manual auditado.
- **Dashboard aislado**: solo SQLite + comandos, token obligatorio, assets locales,
  datos atrasados etiquetados y nunca inventados.

## 8. Verificación

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e ".[dev]"
python -c "import goldbot, goldbot.domain.types, goldbot.domain.interfaces"
python -c "from goldbot.config import load_config; load_config('config/config.example.yaml')"
pytest -q
```

Tests: import completo sin cargar `MetaTrader5`, validación del YAML y candados,
creación idempotente del esquema con WAL y versión 1, presencia de tablas e índices.

## 9. Sondeo MT5 (2026-10-30) y dudas resueltas

Sondeo de solo lectura sobre el terminal abierto: sin órdenes ni cambios de cuenta.

| Duda | Resultado |
|---|---|
| Cuenta demo | login `61623438`, servidor `Pepperstone-Demo`, `trade_mode=0` (DEMO), USD, 1:200, sin posiciones abiertas, `trade_allowed`/`trade_expert` = True |
| Símbolo | **`XAUUSD`** exacto, sin sufijo (existen `XAUAUD`, `XAUUSD-F`, `GOLD-PERP`; descartados) |
| Especificaciones | digits 2, point 0,01, contrato 100 oz, tick value 1 USD, volumen 0,01/0,01/50, `stops_level=0`, `freeze_level=0`, `filling_mode=2` (IOC), market execution, spread flotante (17 points en la medición) |
| Zona horaria | servidor en **GMT+3** con el horario de verano de EE. UU. y GMT+2 el resto del año = `America/New_York + 7 h`; `Europe/Athens` sería incorrecto en las semanas de cambio desacompasadas. Validado con NFP 12:30 UTC, ISM 14:00 UTC y CFTC 19:30 UTC |
| Comisión | **3,50 USD/lote/lado** (redondeo a centavos), verificado en 98 deals |
| Margen | 19,38 USD por 0,01 lote al precio medido |
| Swap | long −78,93 / short +26,29 points por lote/noche, triple el miércoles |
| Histórico | D1/H4 desde 2000; H1 desde 2022; M15 desde 2024; M5 desde ~2025-10; M1 desde ~2026-08 |
| Calendario | `MetaTrader5` 5.0.6231 (última en PyPI) no expone `calendar_*`; resuelto con el puente MQL5. CSV activo con 1.575 eventos y `server_offset_s=10800`; el impacto máximo de Pepperstone es **moderate** (no hay `high`). Con el puente v1.10 el broker sirve **744 días** de calendario, no 44 (§17) |

Consecuencias aplicadas al diseño:

- Enviar **IOC**; no asumir FOK.
- `stops_level=0`: la distancia al stop la impone el bot (ATR/estructura), no el broker.
- Backtest: M5 solo cubre ~12 meses; para walk-forward largo usar M15 (3 años) como
  timeframe de decisión o aceptar una ventana corta en M5.
- La comisión configurada (3,5 USD/lote/lado) coincide con la real del bróker.
- Ruta del terminal confirmada: `C:\Program Files\Pepperstone MetaTrader 5\terminal64.exe`;
  `mt5.initialize()` lo abre sin indicar ruta; `mt5.terminal_path` queda como override.
- Umbral de noticias por defecto **moderate** (antes `high`), con
  `news_critical_event_codes` para bloquear eventos concretos de impacto menor.

El puente de calendario ya está validado: `GoldbotCalendarExport.ex5` adjunto en un gráfico
produce `Common\Files\goldbot\calendar_events.csv` con eventos USD `moderate` (los `high` no
existen en este build). Instrucciones en `mt5/README.md`.

## 10. Fase 2 — ContextBuilder y RegimeDetector

Implementado y verificado en solo lectura (sin órdenes). El análisis corre dentro del
`DataService` en cada nueva vela de decisión.

### 10.1 Flujo

1. `MarketAnalyzer.warm_up()` descarga histórico de todos los timeframes configurados
   (decisión, entrada y contexto), calcula features causales y entrena el detector con
   `train_end_utc` = cierre de la última vela de decisión.
2. En cada vela cerrada nueva: refresca las últimas velas, recalcula features, reajusta el
   HMM cada `retrain_every_bars` y predice el lote completo con histéresis determinista
   (misma secuencia desde el inicio de la ventana).
3. `ContextBuilder.build()` genera el `ContextSnapshot` reproducible sobre velas cerradas y
   el `DataService` persiste `context_json`, `regime_json` y emite `REGIME_CHANGE`.

### 10.2 Features (todas causales)

`ret_1`, `log_range`, `atr` (Wilder), `atr_pct` (rango percentil rolling), `adx`, `slope_norm`
(pendiente EMA normalizada por ATR), `efficiency` (Kaufman) y `vol_ratio`. Se construyen con
`ewm`/`rolling`/`shift`; nunca estadísticas globales. Tests comparan prefijos cortos y largos
con `assert_frame_equal`.

### 10.3 Reglas

- Tendencia = intensidad ADX (`(adx-20)/25`) × eficiencia; dirección = `sigmoid(pendiente)`.
- Volatilidad alta = percentil ATR por encima del umbral configurado.
- Rango = `(1-tendencia) × (1-0.5·volatilidad)`.
- Filas sin datos suficientes devuelven distribución uniforme (no NaN).

### 10.4 HMM

`GaussianHMM` (hmmlearn, diagonal, `random_state=42`, 200 iteraciones) sobre features
normalizadas con media/desviación **congeladas en la ventana de entrenamiento** y recorte
±10. El mapeo estado→régimen es explícito y auditable:

- `TREND_UP`/`TREND_DOWN`: mayor/menor `ret_1 + slope_norm`, exigiendo `|dirección| ≥ 1 ATR`
  y eficiencia media ≥ la media de los estados (evita etiquetar micro-derivas como tendencia).
- `HIGH_VOLATILITY`: mayor `atr_pct` restante si supera el percentil configurado.
- Resto: `RANGE`.

El `params_hash` combina settings, medias y matriz de transición.

### 10.5 Blend y UNKNOWN

`method: both` promedia reglas y HMM 50/50. Como ambas salidas son casi categóricas, un
desacuerdo produce un empate exacto 0,5 y, con `confidence_min: 0.6`, la etiqueta publicada
es `UNKNOWN`. Es una **puerta de acuerdo deliberadamente conservadora**: en la ventana real
de 600 velas M5, el HMM solo detectó `TREND_DOWN` 90 velas y las reglas 37, pero casi nunca
en los mismos tramos, por lo que el blend no confirmó tendencia. El informe
`reports/regimes.html` añade pistas separadas (reglas y HMM) para auditar esos desacuerdos.

Si el HMM falla (dependencia ausente o ajuste degenerado), el blend degrada a reglas y lo
registra (`hmm_active=False`, `ModelInfo.name="rules"`). El modelo combinado se publica como
`blend rules-1+hmm-1-4` con hash conjunto.

### 10.6 Contexto y sesiones

- Sesgo por timeframe: EMA rápida/lenta + pendiente normalizada por ATR; umbral de
  `strength` 0,15 para BUY/SELL (si no, sin dirección).
- Estructura: swings fractales, niveles agrupados por tolerancia ATR, máximo/mínimo/rango
  del día anterior, apertura del día y niveles redondos.
- Timing: ATR, percentil de volatilidad, spread, momento normalizado y ratio de volumen.
- Frescura por timeframe: `OK`/`STALE`/`MISSING` según antigüedad del cierre; un feed
  atrasado bloquea cualquier operación futura.
- Sesiones en **hora de servidor**: Asia 03:00–11:00, Londres 11:00–19:00, NY 16:00–00:00,
  con prioridad NY > Londres > Asia; fuera de ventana, `CLOSED`.

### 10.7 Verificación

- `pytest -q`: 54 tests verdes (features causales, reglas, HMM, blend y fallback, contexto
  reproducible, sesiones, integración con `DataService`, esquema SQLite).
- `run_bot.py --once` real: cuenta 61623438 DEMO, `blend rules-1+hmm-1-4` entrenado sobre
  5.400 velas M5 en ~1,8 s; régimen `RANGE` 0,98; sesión `CLOSED`; feed `STALE` por cierre
  de mercado (comportamiento correcto, no se inventan datos).
- `visualize_regimes.py` genera `reports/regimes.html` sin CDN y sin tocar el StateStore.

### 10.8 Decisiones abiertas

- El histórico M5 solo cubre ~12 meses; para walk-forward largo habrá que decidir M15 como
  timeframe de decisión o aceptar ventanas cortas.
- El HMM puede ser inestable entre reentrenamientos; el blend conservador lo contiene, pero
  conviene medir la rotación de etiquetas en la Fase 6 antes de relajar `confidence_min`.

## 11. Fase 3 — Playbooks, selector, filtros y backtest

Implementado sin enviar órdenes: la cadena de decisión es la misma que usará el bot en vivo,
y el backtest la ejecuta sobre la caché local.

### 11.1 Cadena de decisión

`DecisionCore.decide(ctx, regime, account, spec)` es pura y compartida por vivo y backtest:

1. `ProbabilityPlaybookSelector` elige el playbook del régimen con mayor probabilidad por
   encima de su `min_confidence`; si el régimen es `UNKNOWN`, la confianza no alcanza o el
   régimen no tiene playbook, devuelve `no_trade`.
2. El playbook evalúa el snapshot y devuelve `Signal | None` (`NO_SETUP` es una abstención
   explícita, no un error).
3. `PreTradeFilterChain` aplica, en orden: guardia demo, frescura del timeframe de decisión,
   sesión/ventana de entradas, rollover, spread, noticias, máximo de operaciones diarias y
   pérdida diaria. Cualquier bloqueo produce `BLOCKED` con el primer motivo como principal.

Playbooks: `trend_pullback` (retroceso a EMA20 M15 a favor de H1/D1/H4), `breakout` (ruptura
de máximo/mínimo del día previo o nivel de estructura con expansión), `range_fade` (reversión
en extremos del rango con veto de H1) y `no_trade`. Todos los stops son ATR/estructura, con
objetivo en R y brackets validados antes de emitir la señal.

### 11.2 Simulación del backtest

- Se decide al cierre de cada vela M5 y se entra en la apertura siguiente, pagando el spread
  de la vela de señal más slippage (2 puntos por defecto).
- Stop antes que objetivo si ambos se tocan en la misma vela; una posición a la vez; lote fijo
  0,01 para medir en R; stop temporal de `management.time_stop_bars`.
- Comisión 3,50 USD por lote y lado; sin swap. La gestión fina (break-even, parciales,
  trailing) no entra hasta la Fase 4, así que el resultado mide la señal aislada.
- El calendario se lee del CSV del puente. Fuera de su cobertura la salud es `DEGRADED` y el
  filtro de noticias bloquea por política, nunca asume "sin noticias". Desde v1.10 hay dos CSV
  del puente y una foto congelada para backtest (§17); conviene leer §14.7 antes de fiarse de
  cualquier informe anterior a esa fecha.

### 11.3 Cómo ejecutarlo

```bash
python scripts/backtest.py --config config/config.example.yaml
python scripts/backtest.py --config config/config.example.yaml --method rules
python scripts/backtest.py --config config/config.example.yaml --method hmm --jsonl logs/bt.jsonl
python scripts/backtest.py --config config/config.example.yaml --start 2026-09-30 --end 2026-10-30
```

Escribe un informe de texto y `reports/backtest_phase3.json` (métricas, operaciones, curva de
equity, contadores de política, régimen y playbooks). Ojo con `--calendar none`: no desactiva el
filtro de noticias, lo deja sin fuente, y con `calendar_unavailable_policy: block` (el valor del
YAML) eso significa bloquear toda candidata —cero operaciones—. Para diagnosticar "¿y si no
hubo filtro?" hay que poner la política en `allow` en el YAML; ver §14.7.

### 11.4 Resultado real (2026-09-30 → 2026-10-30)

| Método | Operaciones | Acierto | Expectancy | Profit factor | PnL | DD máx. |
|---|---:|---:|---:|---:|---:|---:|
| `both` (config) | 25 | 20,0 % | −0,31 R | 0,52 | −54,93 USD | 5,49 % |
| `rules` | 24 | 16,7 % | −0,49 R | 0,39 | −74,02 USD | 7,40 % |
| `hmm` | 27 | 14,8 % | −0,48 R | 0,43 | −67,41 USD | 6,74 % |

Con `both`, 4.569 velas se clasificaron `RANGE`, 460 `HIGH_VOLATILITY` y 663 `UNKNOWN`;
ninguna `TREND_*`. Por eso solo compitieron `range_fade` y `breakout`. Con `rules` sí hubo
423 selecciones de `trend_pullback`, pero ninguna cumplió a la vez alineación H1 y retroceso
no extendido (351 sin sesgo H1 a favor, 72 ya extendidas). No es un fallo de código: es una
condición estricta sobre un mes con pocas tendencias.

**Lectura honesta:** no hay ventaja estadística demostrada. 25 operaciones en un mes son una
muestra demasiado pequeña, el 80 % de los stops son normales para un objetivo de 2 R y la
gestión de salida aún no existe. No se han ajustado umbrales para mejorar el número; el
walk-forward completo con reentrenamiento por ventana es la Fase 6.

### 11.5 Verificación

- `pytest -q`: 101 tests verdes (playbooks, selector, filtros, calendario, feed causal,
  ejecución conservadora, métricas e informe).
- `ruff check .` limpio; `mypy src/goldbot scripts` sin errores.
- Backtest real ejecutado contra 15.000 velas M5 y el calendario exportado (1.574 eventos).

## 12. Fase 4 — Riesgo, gestión y ejecución DEMO con kill switch

Aquí es donde el bot puede tocar el terminal. Todo se ha construido y verificado **sin
enviar una sola orden real**: la prueba de ejecución corre contra `SimulatorBrokerAdapter`.

### 12.1 Orden del ciclo (`engine/bot.py`)

`run_cycle(analysis)` ejecuta siempre la misma secuencia, y el orden es parte de la seguridad:

1. Lecturas del broker (`account`, posiciones, `health`). Un fallo **no** lanza: registra,
   aplica backoff y bloquea nuevas entradas (§12.7).
2. `ensure_day(equity)` para que el candado de pérdida diaria tenga la referencia de arranque y
   `record_equity_end(equity)` para cerrar el día. El cierre se reescribe en cada ciclo: cuando el
   día deja de ser hoy la última escritura queda congelada, y ese es su equity de cierre. Hasta el
   2026-11-02 este paso no existía, así que `daily_stats.equity_end` era NULL en todas las filas y
   el auditor `EQUITY_FLOOR` de la revisión semanal descartaba el día por pedir `equity_end` no
   nulo (§16.3).
3. Evaluación del kill switch.
4. **Reconciliación de deals** antes de decidir nada: devuelve los tickets que ya no están en
   el broker y se descartan de la lista local. Sin este descarte la gestión intentaría
   `modify_position` sobre un ticket fantasma, cada fallo contaría como error de ejecución y
   tres fallos latchearían el kill switch.
5. Con posición propia: **gestión**, antes de los candados de entrada. Bloquear el día o
   matar las entradas no puede dejar una operación viva sin arrastrar su stop a favor.
6. Candado diario persistido (`daily_lock_latched`) → `DAILY_LOCK`.
7. Kill latcheado → `KILLED`; pausa del dashboard → `PAUSED`.
8. Decisión y ejecución. Si el `decision_id` (hash del snapshot + acción + señal) ya se
   ejecutó, no se re-entra: una pérdida no se "recupera" sobre el mismo snapshot. Antes de
   enviar la orden, `guards.assert_feed_fresh` exige **marco de decisión en `OK` y tick
   reciente** (`data.max_feed_age_s`); si falla, el ciclo termina en `NO_TRADE` con la etapa
   `feed_stale`, se registra el descarte como evento `FEED_STALE` y **no** cuenta como error de
   ejecución (no se envió ninguna orden, no acerca el latch).

La puerta de frescura de las entradas mide solo el marco sobre el que se decide y la edad del
último tick. Los marcos de contexto (H1/H4/D1) se siguen midiendo y publicando en el bloque
`feed_freshness` del panel, pero no bloquean: después de reabrir el mercado del domingo su última
vela *cerrada* queda a decenas de horas -el daily no cierra hasta la medianoche del servidor,
lunes ~21:00-22:00 UTC- y gatear por ellos habría dejado al bot sin entradas válidas durante todo
el lunes sin que hubiera ningún dato malo. El mismo criterio rige el estado global del feed que
escribe el `DataService` (`_overall`) y, por tanto, el chip `entradas` del panel: panel y motor
ven exactamente la misma puerta.

### 12.2 Lote y validación (`domain/risk.py`)

- Riesgo en dinero = `risk_pct` % del **equity** (no del balance). Lote =
  `riesgo_dinero / (distancia_stop · tick_value/tick_size)`, redondeado **siempre hacia
  abajo** al `volume_step`: el riesgo real nunca supera el configurado.
- Si el lote mínimo del broker implica más riesgo del permitido → **NO se opera** (el
  rechazo más importante de esta fase).
- `validate_order` es la puerta entre decisión y terminal: símbolo, magic, volumen (mínimo,
  máximo, múltiplo exacto del step), SL obligatorio y del lado correcto respecto al bid/ask
  **de ejecución**, TP coherente, `stops_level`/`freeze_level`, filling contra el bitmask del
  símbolo y alineación a `tick_size`. Solo los `ERROR` bloquean.
- La alineación a la rejilla se mide por distancia al paso (`|round(p/tick)·tick − p|`), no
  con `p % tick`: con precios reales de XAUUSD el resto en coma flotante binaria da restos
  tipo 2,9e-14 y marcaba como desalineado un SL perfecto (2 decimales). Fijo en la primera
  corrida contra el terminal; `test_validate_order_sl_alineado_no_avisa_a_pesar_del_ruido_flotante`
  lo cubre.

### 12.3 Ejecución y verificación post-orden (`engine/execution.py`)

`riesgo → validación → envío → verificación SL/TP`. Antes de enviar se fotografían los
tickets propios: en una cuenta netting el `order_ticket` devuelto **no** identifica la
posición, así que la posición nueva se reconoce por la diferencia antes/después.

Los retcodes se tratan por familia (constantes medidas en el terminal, no supuestas): 10009 y
10010 son "llenado", 10011/10012/10031/respuesta nula son **resultado ambiguo** y se
reconcilian, nunca se reintegan a ciegas; solo se reintenta un reintentable explícito, con
`retry_backoff_s` del YAML. Si el broker no dejó SL en la posición recién abierta, se
corrige con `TRADE_ACTION_SLTP` y, si la corrección falla, **se cierra**: una posición
nuestra jamás queda desprotegida. El `deviation_points` y el `filling_policy` vienen del
YAML (Pepperstone: bitmask 2 = solo IOC).

Cada intento queda auditado en `order_attempts` con su request, retcode y resultado.

### 12.4 Gestión (`domain/trade_manager.py`)

Prioridad fija por vela: régimen en contra (solo con
`regime.confidence >= regime_exit_confidence`) → time stop (solo si la operación **no** ha
recorrido `breakeven_at_r`: el reloj no corta una operación ganadora) → break-even (entrada +
0,05·ATR, alineado al point) → trailing por ATR → parcial → HOLD.

Reglas duras añadidas en la auditoría: **un solo movimiento de SL por vela** (dos órdenes
SLTP seguidas sobre la misma posición pueden fallar por `freeze_level`), el parcial se
redondea hacia abajo al step y se rechaza si el volumen cerrado o el residual queda por
debajo de `volume_min`. Las banderas `breakeven_done`/`partials_done` se persisten
(migración `002_position_management.sql`) y se hidratan al leer posiciones: el broker no
sabe qué hizo ESTE bot, y sin eso un reinicio repetiría la acción.

### 12.5 Kill switch (`engine/kill_switch.py`)

Latch persistente con prioridad fija: cuenta no demo → terminal desconectado (tras
`safety.disconnect_tolerance_polls` lecturas seguidas sin enlace) → algo trading deshabilitado →
equity floor → margen libre bajo `safety.min_free_margin_pct` % del equity → marco de decisión
`MISSING` tras `safety.missing_grace_s` → errores consecutivos de ejecución
(`safety.max_consecutive_execution_errors`). Los umbrales vienen del YAML; ninguno está
hardcodeado en el dataclass de ajustes, que solo aporta los valores por defecto. El candado de
margen (`MARGIN_CRITICAL`) existe para que no sea el broker quien cierre las posiciones por stop
out: con XAUUSD a 1:200 el margen por 0,01 lote es ~1,2 % del equity, así que solo se dispara si
el tamaño de posición se desmadra. `STALE` **no** latchea a propósito: un fin de semana bloqueado
obligaría a resets manuales constantes mientras el SL/TP ya protegido sigue vivo en el
broker. Lo que sí hace `STALE` en el marco de decisión (o un tick viejo) es cerrar la puerta de
entradas (§12.1). El reset solo lo produce un comando del operador con token y queda registrado
como `KILL_RESET` con su origen. Un proceso nuevo que arranca con el latch en la BD lo recupera
(`restore_kill_latch`) y no puede operar hasta ese reset.

Dos decisiones de 2026-11-02, después de ver el bot en vivo:

- **El latcheo se registra como evento `KILL_SWITCH` (ERROR) en el flanco de subida**, con el
  motivo y el momento (`_sync_kill_state`, en `engine/bot.py`). Antes el candado solo se veía en
  el chip del panel: al reiniciar el proceso se perdía el "por qué" y la revisión semanal no
  tenía dónde buscarlo. Se emite una sola vez, no en cada ciclo, porque el ciclo de salud corre
  cada poll y habría inundado el registro.
- **Se toleran varias lecturas sin enlace antes de latchear `TERMINAL_DISCONNECTED`.** Con
  tolerancia 1, un `terminal_info()` que devuelve `None` un instante —el terminal reiniciándose,
  ocupado mientras se envía un `order_send`— bloqueaba el día entero y exigía reset manual. No
  abre ningún hueco: durante la caída las lecturas del ciclo lanzan, `_note_data_failure` aplica
  backoff y no se abren operaciones; lo que cambia es que una racha corta ya no necesita
  operador. Con el poll de 5 s, la tolerancia 3 son ~15 s.

### 12.6 Reconciliación y candado del día

Cuando una posición propia desaparece del broker, el bot lee sus deals (`closed_deals` por
rango y `position_id`; en MT5 hay que filtrar el magic en cliente), suma
`profit + commission + swap + fee` de **todos** los deals (IN y cada OUT, así un parcial no
se cuenta dos veces) y escribe la fila en `trades` con motivo de salida clasificado desde el
comentario del deal, R múltiplo final (`net / riesgo_inicial`) y `decision_id_entry`. Ese PnL
realimentó `daily_stats`, y `_apply_daily_loss_lock` compara el equity con el de arranque del
día: si la pérdida alcanza `daily_loss_pct`, engancha `daily_lock_latched` hasta el día
siguiente. Los flags de `Mt5BrokerAdapter` se construyen ahora con el magic de la app: con el
0 por defecto la reconciliación no habría visto **ningún** deal propio.

Convención de signos corregida en el simulador: la comisión se registra **negativa**, como
`deal.commission` en MT5, y se cobra en los dos lados. Con el signo anterior la reconciliación
convertía un coste en ganancia y el candado de pérdida diaria se disparaba tarde.

### 12.7 Enlace caído

`_note_data_failure` aplica `1 → 2 → 5 → 10 → 20 → 60 s` y publica `SIN_ENLACE`; mientras
haya backoff el bucle espera, reintenta el `connect()` del adaptador y no abre operaciones.
La recuperación se registra como evento `RECONNECT`. Un fallo leyendo nunca tira el proceso.

### 12.8 Verificación

- `pytest -q`: **165 tests verdes**, incluidos `tests/integration/test_fase4_bot.py`, la
  prueba de ejecución del `Bot` completo sobre el simulador, vela a vela: entrada con SL en
  el broker, ciclo siguiente que no repite entrada, break-even idempotente y persistido,
  trailing + parcial con sus banderas, SL que salta en el servidor y se reconcilia en
  `trades`/`daily_stats` (−8,42 USD, −1,02 R), candado diario que sobrevive al reinicio,
  backoff creciente sin órdenes nuevas durante la caída y latch del kill switch restaurado
  tras arrancar de nuevo, que solo suelta un comando `RESET_KILL_SWITCH` de origen
  `DASHBOARD`.
- `ruff check .` limpio; `mypy src/goldbot scripts` sin errores en 70 ficheros.

### 12.9 Corrida contra el terminal real (2026-10-31, DEMO)

`python scripts/run_bot.py --config config/config.example.yaml --execute --once` contra la
Pepperstone MetaTrader 5 build 6295 conectada. Lo que quedó probado con datos reales:

- **Cuenta y guards**: `login 61623438`, `server Pepperstone-Demo`, `trade_mode 0` (DEMO),
  equity 989,82 USD, `trade_allowed` y `trade_expert` activos, `terminal_info().trade_allowed`
  (Algo trading) activo. `assert_demo_account` y `assert_algo_trading` pasan sin tocar
  `safety.allow_live`.
- **Specs leídas, no asumidas** (`symbol_info()`): `XAUUSD`, digits 2, point y
  `trade_tick_size` 0,01, `trade_tick_value_profit` = `trade_tick_value_loss` = 1,0,
  `trade_contract_size` 100, `volume_min` 0,01, `volume_max` 50, `volume_step` 0,01,
  `volume_limit` 150, `trade_stops_level` 0, `trade_freeze_level` 0, `currency_profit` USD
  (igual que la moneda de la cuenta, así que el riesgo en dinero es directo).
- **Filling**: el símbolo reporta bitmask `2` (solo IOC) y `resolve_filling` devuelve
  `type_filling = 1` (IOC). Un FOK hardcoded habría rechazado aquí.
- **Aislamiento por magic**: la cuenta tiene 105 deals en los últimos 45 días de
  `magic` 0 (manual), 20261026, 20261028 y 20261029. `positions(20261030)` devolvió 0 y
  `closed_deals()` 0: la conciliación y `close_all` no ven ni tocan operaciones ajenas.
- **Riesgo sobre specs reales**: con stop a 1×ATR (4,40) y 1 % del equity (9,90 USD), el
  lote sale 0,02 con riesgo real 0,89 % (8,80 USD) y `validate_order` sin bloqueantes.
- **Decisión**: `NO_SETUP` — el `range_fade` no encontró extremo de rango en la última vela
  cerrada (la última vela M5 es de 2026-10-30 20:55 UTC).

**Lo que NO quedó probado ese día**: no se envió ninguna orden, y no se pudo probar el llenado real
ni la conciliación de un deal propio, porque era sábado: el feed informa `STALE` con tick de
76 413 s (M5/M15/H1/H4 `STALE`, D1 `OK`) y el mercado está cerrado. Ese paso hay que hacerlo
con el mercado abierto; hasta entonces la evidencia de la ruta de orden es la del simulador.
**Ese paso se dio el 2026-11-02** con el mercado abierto: ver §12.10.

### 12.10 La ruta de orden probada en vivo (2026-11-02, orden enviada en DEMO)

Con el terminal conectado (Pepperstone MetaTrader 5 build 6295, paquete `MetaTrader5`
5.0.6231) y el bot corriendo `--execute`, se enviaron **dos operaciones reales de 0,01 lote**
por la ruta de producción (`ExecutionEngine` → `Mt5BrokerAdapter.place_order` →
`orders.send_order`) con `scripts/live_round_trip.py`. No son señales de la estrategia: la
señal la fabrica el script para tocar el mismo código que tocaría una entrada real.

| posición | entrada | salida | quién cerró |
|---|---|---|---|
| 413997353 | deal 322860547 @ 3899,79 · SL 3894,67 · TP 3909,67 · 00:18:04Z | deal 322862907 @ 3900,10 · 00:25:04Z | el bot, por time stop (**a los 7 min, por el bug 4 de abajo**) |
| 414011783 | deal 322868717 @ 3898,24 · SL 3892,45 · TP 3909,83 · 00:48:02Z | deal 322868809 @ 3899,19 · 00:48:11Z | `Cerrar todo` del panel |

- **Llenado IOC real**: `retcode 10009 (DONE)` con `type_filling = 1`; el broker aceptó la
  orden con SL y TP en el mismo envío y `positions_get` los devolvió idénticos (3892,45 /
  3909,83). La verificación post-ejecución de `ExecutionEngine` confirmó el SL antes de dar
  la operación por abierta.
- **`Cerrar todo` desde el panel** (`POST /api/command` con `X-Dashboard-Token`, comando
  `CLOSE_ALL`, `idempotency_key` nuevo): en cola 00:48:08.337Z → reclamado por el bot
  00:48:11.017Z → ejecutado 00:48:11.119Z (**2,78 s**), estado `DONE`, evento `CLOSED`
  "cierre de todas las posiciones por comando". El deal de salida lleva el comment
  `goldbot close`.
- **Conciliación del deal propio**: en el cierre de la vela siguiente (00:50:01) el bot
  detectó que la posición ya no estaba en el broker, leyó sus dos deals por `position_id` y
  escribió la fila en `trades` (+0,87 USD netos: +0,95 de bruto menos −0,08 de comisión,
  duración 9 s, `exit_reason BOT`, `exit_reason_source BOT`, `decision_id_entry` conservado)
  y acumuló en `daily_stats` del día 2026-11-02 (realized 1,10 USD, 2 victorias, 0 derrotas).
  Después: 0 posiciones, 0 órdenes pendientes, equity 990,92 USD.
- El comment del deal de entrada en el broker es `live_r|smoke-20261102T004801Z`: 29
  caracteres justos, con el `decision_id` intacto (ver el bug 5 abajo).

**Cinco fallos de producción que sólo salían con orden real** (ninguno era visible en el
simulador, que no marshallea objetos MT5):

1. **`TradePosition.commission` no existe** en esta build. El `AttributeError` saltaba dentro
   de `_read_positions`, que el `run_cycle` atrapa como "fallo de datos": con una operación
   viva el bot dejaba de decidir en cada vela y lo reportaba como caída del enlace
   (`00:20:03 ERROR RECONNECT … 'commission'`). El PnL de la posición abierta es ahora
   `profit + swap`; la comisión real llega al conciliar el deal. Test:
   `tests/unit/test_mt5_positions.py`, cuyo fake declara **solo** los atributos que existen.
2. **`TradePosition.position` no existe**: el `position_id` se toma de `identifier`
   (`getattr(pos, "position", 0) or pos.identifier`), que es lo que acepta `order_send`.
3. **`history_deals_get(from, to)` devuelve vacío** en esta build si la ventana no coincide
   con el histórico cacheado, y `TradeDeal` expone `ticket`/`position_id`, no `id`/`position`.
   La conciliación habría sido **silenciosamente incapaz de escribir nunca** en `trades`. Se
   consulta por posición (`history_deals_get(position=...)`), que es exacta.
4. **`_bars_since_open` contaba minutos como segundos** (`_TIMEFRAME_DELTA` en minutos sobre
   un numerador en segundos): una posición de 7 minutos se leyó como "84 barras" y el time
   stop la cerró de inmediato. Ahora divide por `minutos * 60`. El `time_stop_bars` de H1
   coincidía por accidente con la matemática vieja, así que ningún test lo detectaba; se añadió
   la regresión `test_el_time_stop_no_cuenta_minutos_como_segundos`.
5. **El tope de `comment` es 29 caracteres, no 31.** El adapter concatenaba
   `playbook|señal|decision_id` y lo recortaba a 31; el wrapper de Python rechaza la petición
   **localmente** (`mt5.last_error() == (-2, 'Invalid "comment" argument')`) y `order_send`
   devuelve `None`, que se clasificaba como 10012 `NO_RESULT_FROM_TERMINAL` — un falso "fallo
   de enlace". Medido con `order_send` de volumen 0,0 (imposible de llenar): 29 caracteres →
   retcode del broker, 30 o más → `None`. Sin este arreglo **ninguna entrada del bot habría
   llegado al mercado**. Corrección: `orders.MAX_COMMENT_CHARS = 29` y `_order_comment`, que
   recorta el playbook y conserva el `decision_id` entero; los dos recortes de `send_order` y
   `close_position` usan esa constante. Tests: `tests/unit/test_mt5_order_comment.py`.

**Lo que sigue sin probarse**, dicho con honestidad: ninguna de las dos órdenes la disparó un
playbook (la estrategia no ha dado una señal en vivo); no se ha visto un SL saltando en el
servidor mientras el bot estaba caído; ~~ni break-even, ni parcial, ni trailing se han aplicado
sobre una posición viva; y `Cerrar todo` con más de una posición abierta tampoco~~ → esas cuatro
se cerraron más tarde el mismo día, en §12.11, y dos defectos de producción salieron ahí. Dos
viajes de +0,23 y +0,87 USD **no dicen absolutamente nada de la ventaja estadística** — el
walk-forward de §14 sigue siendo la única evidencia, y su lectura es negativa. La suite tras estos
arreglos: **282 tests verdes**, `ruff check .` limpio, `mypy src/goldbot scripts` sin errores en
76 ficheros.

**Un matiz del candado de 2/día que esta prueba deja ver**: `daily_stats.entries_used` quedó en
0 pese a dos operaciones abiertas, porque lo incrementa `Bot._execute` al tomar la decisión, no
el hecho de que exista posición en el broker. El PnL **sí** se acumuló (1,10 USD computan contra
el techo de −2 %), y el bot tampoco abre una segunda entrada con una posición viva. O sea: una
orden metida a mano fuera del bot no gasta cupo diario; no es un agujero del diseño (el bot no
opera por fuera), pero conviene saberlo al leer el panel tras una prueba como esta.

### 12.11 La gestión probada en vivo (2026-11-02, dos posiciones simultáneas en DEMO)

Cuatro cosas seguían sin verse sobre una posición viva: break-even, parcial, trailing y un
`Cerrar todo` con más de una operación abierta. Se probaron las cuatro en la misma cuenta demo,
y salieron **dos defectos de producción** que el simulador no puede dar.

**Geometría de la prueba.** Las señales las sigue fabricando `scripts/live_round_trip.py` — ningún
playbook ha disparado una entrada en vivo —, pero elegidas para que mande la gestión y no el
objetivo: `--atr-mult` fija el ancho del stop, `--rr 6`/`--rr 8` deja el TP muy lejos y `--max-lots`
funciona como puerta de aborto antes de enviar. El lote **no** lo decide la prueba: lo recalcula
`ExecutionEngine` con el `RiskManager` (1 % del equity dividido por la distancia de stop), así que
un stop de 1,67 USD dio 0,05 lotes (8,35 USD de riesgo) y uno de 4,61 dio 0,02 (9,22 USD). Se
quitó el
`--force-lots` que había: imprimía 0,01 mientras el broker se quedaba con 0,02, porque el valor
pedido nunca llegaba al `OrderRequest`. **La knob mentía, y una knob que miente en una prueba de
dinero es peor que no tenerla.** `--allow-stack` es la única forma de apilar la segunda pata: sin
él la prueba se niega, porque la ruta normal del bot no abre con una posición viva. Con dos patas
a ~1 % cada una hay ~2 % de riesgo simultáneo **abierto** (no realizado), yendo el par en lados
opuestos; el tope del 2 % del spec es de pérdida realizada diaria y no se tocó.

**Primera corrida (02:30–03:06) — defecto 1: el divisor de R se re-derivaba del SL vivo.**

| hora | evento | R declarado |
|---|---|---|
| 02:32 | 414063962 (BUY 0,02 @ 3912,27, riesgo 3,66) muere por SL del broker | −7,46 USD → **−1,02 R** ✔ |
| 02:35 | 414064025 (SELL 0,02 @ 3912,86): `MOVE_SL_BREAKEVEN` a 3912,60 | **1,47 R** ✔ (aún no había SL movido) |
| 02:40 | parcial: cierra 0,01 de 0,02 | **20,38 R** ✗ |
| 02:45 | `TRAIL_SL` a 3912,13 | **32,85 R** ✗ |
| 02:55 | `TRAIL_SL` a 3903,55 | **24,15 R** ✗ |
| 03:06 | muere por el SL arrastrado | +14,40 USD, `trades.r_multiple` **19,73** ✗ |

La causa está en el adaptador: `TradePosition` no guarda el riesgo de la entrada, así que
`mt5_adapter.py` lo re-deriva como `abs(sl - entry)` en cada lectura. Nada más abrir es exacto,
pero en cuanto el bot lleva el SL a breakeven el derivado se hunde —en 414064025 pasó a 0,26 USD,
que es `|3912,60 − 3912,86|`— y con él el divisor: una operación que iba en 1,4 R se lee en 20 R
y el `TradeManager` cierra media posición **creyendo** que corrió 1,5 R. El parcial de las 02:40
no era una gestión, era el divisor
roto. Arreglo en `bot.py`: `_riesgo_inicial()` toma el valor **congelado en la fila `positions`**
escrita al abrir y la lectura del broker sólo vale para una posición adoptada (sin fila propia), y
`_r_desde()` recalcula el R con ese mismo divisor para que el panel y la BD no pinten repartos
distintos. `_reload()` persiste el congelado en vez del re-derivado. Regresión:
`tests/integration/test_fase4_bot.py::test_un_breakeven_en_el_broker_no_reescribe_el_riesgo_inicial_congelado`.

Que el `net_profit` de la fila de arriba sea correcto (+14,40, sale de sumar los deals) y su
`r_multiple` no es el punto de todo esto: **el R de una operación cerrada se calcula sobre el
riesgo que está en la BD**, y el 19,73 es literalmente 14,40 / (0,73 × 100 × 0,01), con 0,73 =
`abs(3912,13 − 3912,86)`, o sea el SL del primer trailing. No hay forma de recuperar el R honesto
de esa fila desde la fila misma: el divisor original ya estaba sobescrito cuando se concilió. Se
deja como evidencia y **no se retoca**; reescribir un derivado de un registro contable para que
"se vea bien" es justo lo que el spec prohíbe. Si algún día se toca, es como anotación nueva, no
como enmienda del número.

**Segunda corrida (03:42–04:30) — `Cerrar todo` con dos operaciones vivas.** Par apilado con el
código ya arreglado (build r5): 414148301 BUY 0,02 @ 3905,18 (SL 3900,57, TP 3932,85) y 414148356
SELL 0,02 @ 3905,13 (SL 3909,44, TP 3877,16). `POST /api/command` con `close_all` a las
04:25:35.7 → reclamado 04:25:38.9 → `DONE` 04:25:39.1 (**3,4 s** de punta a punta, 0,2 s de
ejecución); `positions_get` volvió a 0 y las dos patas siguieron en `positions` hasta el cierre de
la vela M5 de las 04:30:00, que es cuando corre `_reconcile_closed`:

| posición | salida | neto | R | `exit_reason` |
|---|---|---|---|---|
| 414148301 | 3905,95 | +1,40 | **+0,15** | `BOT` |
| 414148356 | 3906,06 | −2,00 | **−0,23** | `BOT` |

Ambos riesgos iniciales se reescribieron a mano en la BD (`abs(sl − entry)`, 4,61 y 4,31) porque
el script había guardado la distancia **pedida** y el llenado deslizó; es la diferencia entre el
riesgo solicitado y el realizado, y la prueba no la contemplaba. Con un divisor de 0,20 en vez de
4,61 esas dos filas habrían salido con R de ~7 y ~10.

**Tercera corrida (04:26–05:00) — breakeven, parcial y trailing con el divisor correcto.** Par
ceñido a lados opuestos, 1 R ≈ 1,7 USD para que el mercado no tuviera que moverse un dólar entero:

| hora | pata | acción | R declarado |
|---|---|---|---|
| 04:27:45 | 414168548 BUY 0,05 @ 3906,86 (SL 3905,19) | SL del broker a los 73 s: −8,71 USD | **−1,04 R** ✔ |
| 04:30:00 | 414168566 SELL 0,05 @ 3906,58 (SL 3908,29, TP 3896,34) | `MOVE_SL_BREAKEVEN` a 3906,37 | **1,37 R** ✔ |
| 04:35:01 | idem | parcial: 0,02 de 0,05 → quedan 0,03 | **2,19 R** ✔ |
| 04:50:01 | idem | `TRAIL_SL` a 3904,78 | **4,37 R** ✔ |
| 04:55:02 | idem | `TRAIL_SL` a 3904,35 | **4,49 R** ✔ |

Los R cuadran con `(precio − entrada) / 1,71` calculados a mano; la lectura cruda del broker de esa
misma posición seguía diciendo `abs(3906,37 − 3906,58) = 0,21 → 12,67 R`, que es exactamente lo
que el arreglo ya no deja pasar a la gestión. El parcial redondeó hacia abajo como toca
(50 % de 0,05 = 0,025 → 0,02 por `volume_step`) y dejó 0,03 con SL y TP intactos.

**Defecto 2: mover el SL borraba el TP en el broker.** `Bot._manage_positions` llamaba
`modify_position(ticket, sl=...)` **sin `tp`**, y `TRADE_ACTION_SLTP` aplica el par completo: el
lado que falta viaja como 0 y el servidor lo lee como "sin take profit". Prueba en vivo, dos veces
sobre la misma posición con un experimento controlado (se repuso el TP a mano y se dejó correr):

- 04:30:00 breakeven sobre 414168566 → el terminal perdió el TP 3896,34.
- 04:32 TP repuesto a mano (`10009 DONE`).
- 04:50:01 `TRAIL_SL` (sólo SL) → **TP vuelto a NULL**, y así quedó persistido en la foto de BD.
- Testigo antiguo: la fila `trades` de 414064025 (la corrida de las 02:35) tiene `tp` **NULL**:
  perdió su objetivo en aquel breakeven. Es el mismo defecto, dos horas antes.

Ningún test lo veía porque el simulador trata `None` como "no tocar", que es la semántica que el
llamador espera, no la del terminal; y el docstring de `orders.modify_position` afirmaba que omitir
el campo nulo *protegía* el stop. Era al revés. Arreglo en `Mt5BrokerAdapter.modify_position` (la
capa que conoce el protocolo, no el llamador): si falta `sl` o `tp`, se rellena con el valor vivo
de la posición antes de enviar, y **si la posición no se puede leer se aborta la modificación** —
quedarse sin stop rompe la regla de nunca operar sin SL. Tests nuevos:
`tests/unit/test_mt5_modify_sltp.py` (tres casos, rojo antes del arreglo con `KeyError: 'tp'` en el
dict que se envía; verde después). Verificación en vivo con build r6, sobre la posición real, con
la llamada exacta de producción (`sl` sólo, cambio real): `10009 DONE`, SL 3904,35 → 3904,34 y
**TP 3896,34 intacto**.

De paso, otro detalle del terminal: un `TRADE_ACTION_SLTP` con ambos valores iguales a los vigentes
se rechaza con **`10025 NO_CHANGES`** (`filled = False`). Ya estaba clasificado como benigno
(`OrderErrorCategory.NONE`) y no altera el estado, pero la primera vez que intenté verificar el
arreglo pasé el SL sin cambiarlo: la petición ni llegó al mercado y el "TP conservado" no probaba
nada. Hubo que pedir un cambio real.

**Cierre de la pata y un tercer hallazgo (documentado, NO arreglado).** La 414168566 murió en el
broker a las 05:06:00 por su SL arrastrado, en **3904,34** — el nivel que dejé puesto al verificar
el arreglo (bajé 0,01 el SL y no lo devolví); el TP 3896,34 nunca se llenó. Se concilió en el cierre
M5 de las 05:10: +13,78 USD, `exit_reason SL`, `r_multiple` **2,69**. El neto cuadra (los 0,02 del
parcial + los 0,03 finales menos comisión), pero **el R no es el de la operación**:
`_r_from_net` divide por `initial_risk_price × contract × stored.volume`, y `stored.volume` vale
0,03 porque el parcial lo bajó. El riesgo que se tomó al abrir fueron 0,05 lotes = 8,55 USD, así
que el R honesto de esa posición es **13,78 / 8,55 = 1,61**, no 2,69. Es la misma familia de error
que el defecto 1 (un R inflado que se va al ledger y a la revisión semanal), pero con un origen
distinto: **el denominador del R de una operación cerrada debe ser el riesgo inicial en dólares,
no el que queda después de los parciales.**

No lo arreglo en esta entrega porque el arreglo correcto es guardar el volumen (o el `risk_money`
en USD) de la entrada en `positions` al abrir, y eso es una **migración de esquema** con una base
demo ya escrita encima — no es un cambio de una línea, y merecía decirlo antes de tocarlo. El
efecto en los datos que hay ahora: solo las operaciones con parcial cerrado salen infladas, y en
esta base es un caso (414064025: 19,73 con divisor roto; 414168566: 2,69 en lugar de 1,61).

**Lo que sigue sin probarse**, dicho con honestidad: sigue sin haber una `ENTRY` disparada por un
playbook (todas las operaciones de este § las fabricó el script); no se ha visto un **TP llenándose
en el broker** — la pata final salió por el SL arrastrado, que es lo que hace el trailing, y el TP
solo estuvo puesto ~10 minutos tras reponerlo a mano; el cierre por régimen en contra y el candado
del kill switch tampoco se han visto en vivo; y el `Cerrar todo` se probó con dos posiciones, no
con tres. Cuatro patas cerradas y conciliadas, con las tres primeras salidas (SL del broker, `BOT`
por comando, SL arrastrado) y dos defectos de producción corregidos: eso **no es evidencia de
ventaja estadística**. El balance del día quedó en **+12,51 USD realizados** (5 victorias,
3 derrotas) sobre 989,82 iniciales y `positions` a 0 — y aun así el único dato de ventaja que
existe sigue siendo el walk-forward de §14, cuya lectura es negativa. Suite tras estos arreglos:
**289 tests verdes**, `ruff check .` limpio, `mypy src/goldbot scripts` sin errores en 76 ficheros.
Procesos vivos: bot r6 (PIDs 21820/21108) y panel (9716/19132).


## 13. Fase 5 — Dashboard local

### 13.1 Procesos y canal

El panel es un **proceso separado** del bot (`scripts/run_dashboard.py`); nunca corre dentro
del bucle de trading, nunca importa `MetaTrader5` y nunca decide nada. Los dos procesos se
comunican sólo por la base SQLite en modo WAL:

- El bot escribe; el dashboard lee con `mode=ro` (`readmodel._connect`). Si el `mode=ro`
  falla —el bot murió dejando el `-wal` sin `-shm`, o el directorio no es escribible—, abre
  una conexión ordinaria **de la que sólo hace `SELECT`**, porque el requisito es seguir
  pintando el último dato conocido, no negarse a arrancar.
- `_rows()` devuelve `[]` si el fichero no existe: es "sin datos", no un error, y el lector
  nunca crea la base ni sus tablas (eso pertenece al proceso que opera).
- La única escritura admitida es encolar una fila en `commands` a través del `StateStore`.

Escucha **sólo en loopback**: `run_dashboard` comprueba `dashboard.host` contra
`{127.0.0.1, localhost, ::1}` y devuelve 2 si el YAML pide otra cosa. El middleware
`_LocalHostHeaders` no filtra el origen —el único cortafuegos es el bind— y se limita a las
cabeceras duras de un servidor que no sale de la máquina (`Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`). Los comandos de
escritura exigen la cabecera `X-Dashboard-Token` con el valor de `dashboard.token_env`
(`DASHBOARD_TOKEN`), comparado con `secrets.compare_digest`. **Fail-closed**: si la variable
no está en el entorno, `create_app` recibe token `""` y `verify_token` devuelve `False` para
cualquier cabecera, así que los botones quedan bloqueados con 401 en vez de abiertos; el
arranque lo avisa por stderr. Los estáticos (uPlot, CSS, JS) se sirven en local —no hay ni
una referencia a un CDN— y se desactivó `docs_url`/`redoc_url` porque esas UIs cargan assets
remotos; el esquema sigue en `/openapi.json`.

### 13.2 Mapa de paneles

`GET` de lectura: `/api/overview`, `/api/brain`, `/api/charts/equity`,
`/api/charts/daily-pnl`, `/api/trades`, `/api/events`, `/api/logs`, `/api/commands`,
`/api/order-attempts`, `/api/healthz`. Una sola `POST`: `/api/command`. El navegador refresca
en paralelo nueve de ellas —todas salvo `/api/healthz`— cada `dashboard.refresh_s` (3 s, suelo
1 s) con `setTimeout` encadenado, así que una pestaña oculta o una petición lenta no apilan
trabajo.

En el orden del diseño: **1** cabecera (estado del bot, hora del servidor con zona y offset,
uptime, magic, botones Pausar/Reanudar/Cerrar todo, indicación del token), **2** KPIs de
cuenta y riesgo del día (balance, equity, flotante, realizado+flotante, riesgo %, límite de
pérdida diaria y % usado), **3** chips de enlace y mercado (spread, ATR M5, distancia de stop,
sesión, entradas, posiciones, entradas usadas hoy, algo trading, trading permitido, feed MT5)
más la última vela **cerrada** por temporalidad, **4** "cerebro" (régimen con confianza y
método, playbook y veredicto, filtros pre-trade, sesgo multi-temporal, niveles clave, últimas
decisiones incluidas las abstenciones), **5** posiciones abiertas, **6** gráficos (equity 24 h
con marcadores de entrada/salida y PnL realizado por día), **7** historial cerrado con filtro
7/30/90 d y resumen, **8** eventos (60), **9** log del bot (160, con auto-desplazamiento
activable), **10**
auditoría de comandos e intentos de orden, más el pie con magic, modo, cuenta, feed, caché de
velas y recuentos de filas.

### 13.3 Honestidad de los datos

Cada payload lleva `staleness` **por fuente** (`{as_of_utc, age_s, stale}`), no por página, y
cada panel pinta su propia etiqueta. Hay dos clases, y la distinción es lo que evita que la
página amanezca en ámbar permanente:

- **Fuente de ciclo** (`bot_state`, `equity_snapshots`): la escribe el bucle del bot, así que
  `stale_after_s` (15 s, frente a un sondeo real de ~5 s) marca el corte. "dato atrasado ·
  hace 2 h". La foto de equity la mete el sondeo de datos de cada modo —`poll_once` sin
  `--execute`, `refresh_bars` con él— y una fila por minuto, que es la cadencia real del upsert.
  Hasta el 2026-11-02 `refresh_bars` no la escribía: con `--execute` el gráfico quedaba clavado en
  el valor del arranque y el panel lo pintaba como si el enlace se hubiera caído (§12.1 paso 2).
- **Fuente de registro** (`trades`, `events`, `log_lines`): tablas append-only que sólo crecen
  cuando pasa algo. Aquí la antigüedad es informativa y se pinta en neutro: "último registro
  hace 6 h". Un panel de historial no está "atrasado" porque nadie haya operado.

Si `bot_state` falta o está viejo, la cabecera muestra el banner `⚠ … Se muestran los últimos
datos conocidos; no se rellena ni se interpola`, y el kill switch, el candado diario y un
último ciclo en `ERROR` suman su motivo al mismo banner. El gráfico de equity se construye
sobre una **rejilla de minutos** (`equity_grid`) donde lo que no hay dato es `None`: uPlot lo
dibuja como corte de línea, así que una caída del enlace se ve como hueco en la curva en lugar
de una recta inventada; la nota del panel cuantifica cobertura, minutos perdidos y hueco
actual. La serie de PnL por día y los marcadores salen de las mismas filas, sin suavizado.

### 13.4 Comandos: qué pueden y qué no

`CommandRequest` admite exactamente cuatro nombres —`pause`, `resume`, `close_all`,
`reset_kill_switch`— y **no tiene campos** para lote, riesgo, precio ni símbolo: no existe
ruta posible para abrir una operación ni para tocar parámetros de riesgo. La validación del
cuerpo corre antes que la dependencia del token, así que un comando inexistente (o una
`idempotency_key` de menos de 8 caracteres) es 422 y nunca llega a la puerta de autenticación.
Reglas del endpoint:

| Condición | Respuesta |
| --- | --- |
| token ausente o inválido | 401 |
| la base del bot no existe todavía | 503 (el dashboard no la crea) |
| `close_all` con `dashboard.close_all_enabled: false` | 403 |
| `idempotency_key` ya vista en los últimos 200 comandos | 200 con `status: "duplicate"` |
| resto | 200 con `status: "queued"` y `command_id` |

La asimetría de caducidad es deliberada: `pause`, `resume` y `reset_kill_switch` **no
caducan** (`ttl_s = 0`) porque si el bot estaba caído la orden de pausar debe cumplirse al
recuperar; `close_all` sí caduca a los `close_all_ttl_s` (120 s) porque cerrar posiciones
horas después de pedirlo no es lo que el operador pidió. La deduplicación se resuelve dentro
de la transacción `BEGIN IMMEDIATE` del `StateStore`, no en la capa HTTP, y cada comando
nuevo se registra como evento `COMMAND` de origen `DASHBOARD` (WARNING para `close_all` y
`reset_kill_switch`, INFO para pausa y reanudación). En el panel, "Cerrar todo" y "Rearmar kill
switch" exigen dos clics: el primero arma el botón y avisa por toast, el segundo envía. El rearme
sólo se ofrece con `header.kill_switch_latched` activo (el motivo va en el tooltip) y se añadió en
la Fase 7 para cerrar el hueco que la Fase 5 documentó; verificado en el DOM real contra una copia
de la base con el candado latcheado: oculto con `OPERATIVO`, armado al primer clic, `RESET_KILL_SWITCH`
en `commands` con `status: PENDING` tras el segundo.

**Pausa no cierra nada**: `run_cycle` aplica la pausa *después* de gestionar las posiciones
abiertas (§12.1), y tres pruebas lo fijan —pausar antes de la primera vela evita la entrada,
pausar con posición abierta la sigue gestionando (el SL sube a break-even) sin repetir
entrada, y el estado `PAUSADO` sólo aparece en el panel cuando el bot lo escribió, nunca
inventado por la interfaz. `Cerrar todo` se prueba de punta a punta por la ruta real (POST con
token → `commands` → el `Bot` lo reclama → broker → conciliación): la posición desaparece del
simulador y de `positions`, aterriza en `trades` con su PnL, el comando cierra en `DONE` y
quedan los dos eventos (`COMMAND` de origen `DASHBOARD` antes que `CLOSED` de origen `BOT`);
después del cierre el bot vuelve a `OPERATIVO`, porque cerrar no es un candado del día.

### 13.5 Frontend

Una página, sin framework ni build: `index.html` + `styles.css` + `app.js` (tema oscuro con
acento dorado, números en monoespaciada, importes con `Intl` en es-ES) y uPlot 1.6.32
vendorizado en `static/vendor/`. Decisiones que costaron un rato:

- **Ejes en hora del servidor**: la rejilla del gráfico va en epoch y el eje pinta la etiqueta
  convertida con el offset del bot (`server_offset_min`), porque el operador razona en la hora
  de su broker. El callback `values` de un eje en uPlot **debe devolver un array de strings**;
  devolver la cadena de un `tick` pintaba "N a N : N a N" en todo el eje.
- **Barras con signo**: el PnL por día son dos series de barras (positivas y negativas) sobre
  una escala `x.time = false`, porque mezclar ambos signos en una sola serie deja las barras
  negativas dibujadas hacia arriba.
- **Tablas anchas**: `#positions`, `#trades`, `#commands` y `#attempts` van dentro de
  `.scroll-x` (`overflow-x: auto`) en lugar de comprimir columnas; con 12 columnas de números
  monoespaciados, ajustar es ilegible.
- **Fallos parciales**: cada ruta se lee con `safeGet`; si una falla el resto de la página se
  pinta igual y el error se resume en un toast, salvo `/api/overview`, que es la cabecera y
  activa el banner de "el navegador no puede hablar con el proceso del dashboard".

### 13.6 Dos defectos reales que sacaron las pruebas

- `Bot._log_decision` hacía `if logger is None: return` **antes** de escribir en SQLite, así
  que una ejecución sin JSONL dejaba el panel "cerebro" vacío aunque el bot sí hubiera
  decidido. Ahora la tabla `decisions` se escribe siempre y el JSONL es un espejo opcional.
- El chip `feed MT5` mostraba el último `feed_status` publicado, de modo que un bot muerto
  desde hacía 10 minutos seguía presumiendo de `OK`. El chip ahora exige que el estado sea
  reciente (`feed_live`) y, si no lo es, pinta `OK · sin ciclos hace N s` en rojo.

### 13.7 Verificación

- `pytest -q`: **189 tests verdes** al cerrar la fase, 24 de ellos en
  `tests/integration/test_fase5_dashboard.py` sobre una base sembrada con reloj congelado
  (ninguna prueba abre MT5). Cubren: API completa
  con datos reales del dominio, base vacía, base inexistente, datos atrasados, huecos de la
  rejilla, rechazo de token ausente/malo, 403 de `close_all` deshabilitado, 503 sin base,
  422 de comandos imposibles, idempotencia y no-duplicación del evento, TTL de cada comando,
  y las cinco pruebas conducidas por el `Bot` real de §13.4.
- Añadido después, con el botón de rearme: 2 tests más en el mismo fichero (el comando
  `reset_kill_switch` se encola sin caducar y se audita en WARNING, y el estático del panel
  expone `id="btn-reset-kill"`, su listener y `resetBtn.hidden = !latched` con dos clics
  antes de enviar). Verificado además en el navegador contra una copia aislada de la base.
- `ruff check .` limpio; `mypy src/goldbot scripts` sin errores en 71 ficheros.
- `python scripts/run_dashboard.py --config config/config.example.yaml --check` construye la
  app, informa de base inalcanzable y de token ausente y enumera los 11 endpoints.
- **Capturas** en `docs/capturas_fase5/` (Chrome headless, `--screenshot`): `01_base_real_bot_parado`
  es la base **real** de la DEMO con el bot detenido —banner de degradado, "dato atrasado ·
  hace 2 h", "sin lectura" y el chip del feed confesando `sin ciclos hace 7973 s`—;
  `02_demo_pagina_completa`, `03_demo_cabecera_kpis_chips`, `04_demo_grafico_equity_hueco`
  (la curva con el corte de 45 min dibujado como hueco) y `05_demo_historial_eventos_log` son
  la base de demostración sembrada por los tests, con importes ficticios.

### 13.8 Limitaciones conocidas

- `daily_stats.tz` se escribe con la literal `'UTC'` mientras que la clave `trading_day` es la
  **fecha del servidor** (`UtcClock.trading_day` → `to_server().date()`). La columna miente;
  hoy nada la lee para decidir, pero hay que corregirla o eliminarla antes de confiar en ella.
- La tabla de posiciones abiertas se corta horizontalmente a 1500 px de ancho: es un scroll
  deliberado (§13.5), no un bug, pero en una ventana de 1280 px deja el PnL fuera de vista.
- El panel de comandos se alimenta de `commands`: si el bot está detenido nadie reclama las
  filas, así que un "Pausar" aparece como encolado pero el estado del bot no cambia hasta que
  arranca. Es el comportamiento correcto, pero conviene leerlo con esa clave.
- No hay autenticación de origen (CSRF/CORS): el único cortafuegos es el loopback más el
  token. Mientras el panel escuche en `127.0.0.1` es asumible; publicar el puerto exigiría
  revisar esto primero.
- ~~`Cerrar todo` está probado contra el simulador (§13.4), pero nunca contra el broker real~~
  → ejercitado el 2026-11-02 sobre una posición viva: en cola con token, ejecutado en 2,78 s y
  deal conciliado (§12.10).

## 14. Fase 6 — Backtest walk-forward completo e informe por régimen y playbook

La especificación lo pide literalmente (§ "Validación"): *entrenar en ventana A, probar en B,
desplazar* y **prohibido optimizar sobre todo el histórico y reportar eso como resultado**.
Fases anteriores corrían un único corte (`--start`/`--end` con un `train_end`); esta fase añade
el bucle de pliegues, las métricas agrupadas entre pliegues y el informe por régimen y playbook.

Código: `src/goldbot/backtest/walk_forward.py` (runner y geometría), `metrics.py` (`GroupStats`
y los desgloses), `report.py` (`WalkForwardReportBuilder` y `walk_forward_to_jsonable`) y
`scripts/backtest.py --walk-forward`.

### 14.1 Geometría de los pliegues

`WalkForwardRunner.plan(bars)` corta el histórico del timeframe de decisión anclándose en la
apertura de la **primera** vela y avanzando de `step_days` en `step_days`:

- pliegue *k*: entrena en `[start + k·step, start + k·step + train)` y prueba en
  `[train_end_k, train_end_k + test)`, de modo que `test_start == train_end` —no hay solape
  entre entrenamiento y prueba dentro de un pliegue, que es justo lo que mata el look-ahead.
- Solo se conserva un pliegue cuyo tramo de test está **completo** dentro del histórico
  (`test_end <= última apertura`). La cola que no cabe se descarta y se informa como
  `discarded_tail_days`: un tramo recortado tiene menos días, por tanto menos operaciones, y
  no es comparable con los demás.
- Si el histórico no alcanza `train + test`, `plan` devuelve `([], margen_negativo)` y `run`
  lanza `ValueError` con el número real de velas. No hay pliegues "de relleno".
- Las ventanas vienen del YAML (`backtest.train_window_days` / `test_window_days` /
  `step_days`, 180/30/30 en el ejemplo) y `--train-days/--test-days/--step-days` las
  sobreescriben en la CLI. `BacktestSection` rechaza en carga
  `test_window_days > train_window_days`, y el runner repite la comprobación porque también
  puede construirse en código.

Corrida real sobre la caché M5 (60 000 velas, 2025-12-26 → 2026-10-30):

```
Pliegues planificados: 4 · cola sin evaluar 8 días
  pliegue 1: entrena hasta 2026-06-24, prueba 2026-06-24 -> 2026-07-24 (6022 velas)
  pliegue 2: entrena hasta 2026-07-24, prueba 2026-07-24 -> 2026-08-23 (5614 velas)
  pliegue 3: entrena hasta 2026-08-23, prueba 2026-08-23 -> 2026-09-22 (5940 velas)
  pliegue 4: entrena hasta 2026-09-22, prueba 2026-09-22 -> 2026-10-22 (6068 velas)
```

### 14.2 Qué recibe el motor en cada pliegue

Cada pliegue construye **un `BacktestEngine` nuevo** con el histórico completo y su propio
`train_end_utc`. Dos detalles que son deliberados:

- El feed no se recorta: el detector de régimen y el `ContextBuilder` necesitan sus velas de
  calentamiento, y las etiquetas las pone un rastreador de histéresis sembrado en la primera
  vela. Lo que avanza es el corte de entrenamiento, que `BacktestEngine._prepare_model` respeta
  (`features[index <= train_end].tail(...)` y el HMM encima congela media y desviación solo de
  ese tramo, §10.4) y el tramo de prueba, que es lo único que se mide
  (`start_utc`/`end_utc` del pliegue).
- El libro diario, el reloj y el candado de 2 operaciones/día no se heredan entre pliegues:
  un pliegue no puede "arrastrar" el equity ni el contador del anterior.

Hay dos conceptos de "ventana de entrenamiento" y conviene no confundirlos: `train_window_days`
es geometría de validación (cuánto histórico queda disponible antes del tramo de prueba), mientras
que el modelo usa su propio `regime.train_window_bars` (5 000 velas; el motor pide
`train_window_bars + atr_percentile_window + 200` = 5 400) tomado como *cola* que termina en
`train_end`. La ventana con la que se ajusta el detector también se desplaza pliegue a pliegue,
que es justo lo que separa esto de "optimizar sobre todo el histórico".

### 14.3 Métricas por grupo (`metrics.py`)

`GroupStats` es la misma cifra para cualquier recorte: n, ganadoras/perdedoras, % de acierto,
expectancy en R, σ de las R, t, profit factor, neto en USD, media de ganancia y de pérdida,
racha de pérdidas, drawdown del grupo y cuántas operaciones tienen R definida (`with_r`).
`summarize()` los calcula y `breakdown()` los agrupa por una clave:

- `by_regime(trades)` — etiqueta de régimen **en la entrada** (`regime_at_entry`), no la actual.
- `by_playbook(trades)` — `playbook_id` de la entrada.
- `regime_playbook_grid(trades)` — la cuadrícula régimen × playbook, que es el informe que pide
  la especificación; cada celda lleva su n y su veredicto.

`backtest.min_cell_trades` (YAML, por defecto 10) marca las celdas con menos operaciones como
`no concluyente`. El umbral puede sobreescribirse en el constructor del runner, pero si no se
pasa nada manda el YAML: un número mágico en el código sería justo la clase de ajuste que la
fase quiere evitar.

**Guarda de dispersión.** Nueve pérdidas que salen todas por el mismo stop dan R ≈ −1 en las
nueve: la σ de la muestra es ~0,002 y el estadístico t = e/(σ/√n) se dispara a valores absurdos.
No es hipotético: en `rules`, el grupo `RANGE` del agregado tiene 9 operaciones, expectancy
−1,0217 R y σ 0,00245 R, lo que daría t = **−1251** por la fórmula de siempre. `has_dispersion()`
exige `with_r ≥ 2` y `σ ≥ 0,05`; sin dispersión, `t_statistic()` devuelve 0,0 y el informe pinta
`n/a` (`has_dispersion: false` en el JSON). Es preferible confesar que el estadístico no dice nada
que lucir un número gigante y falso.

### 14.4 El informe

`WalkForwardReportBuilder.build(report)` imprime, en este orden: cabecera (histórico, ventanas,
pliegues evaluados/rechazados, días de prueba, modelo de régimen y estado del filtro de noticias
con su cobertura), tabla *Por pliegue* (velas, ops, expectancy, PF, DD USD, DD %, cobertura del
calendario), *Agrupado*, *Estabilidad entre pliegues*, *Desglose por régimen*, *Desglose por
playbook*, *Cuadrícula régimen × playbook* y *Lectura honesta*. Todo en columnas monoespaciadas
con `_fmt`, sin decimales fingidos.

*Agrupado* lleva explícitamente lo que pide la especificación de validación: número de
operaciones, ganadoras/perdedoras, expectancy con su t y su σ, profit factor, media de ganancia
y de pérdida, PnL neto, drawdown, racha de pérdidas, **% de días sin operar** y **distribución de
R** en los mismos tramos (`<=-1`, `-1..0`, `0..1`, `1..2`, `>=2`) que usa el corte único. Los dos
últimos vienen de `WalkForwardReport.no_trade_days_pct` y de su `r_distribution`: el primero
promedia los días de decisión de cada pliegue —como los tramos de test no se solapan, la media
ponderada por `decision_days` es exactamente el porcentaje agrupado—, así que un 100 % significa
"el bot no abrió nada ningún día", que es un resultado y no un fallo. Para ello el motor informa
de cuántos días de decisión tuvo (`BacktestResult.decision_days`); antes no lo exponía y el
porcentaje solo existía dentro de cada pliegue.

`walk_forward_to_jsonable(report)` es el mismo contenido estructurado: `windows`,
`history_*_utc`, `tested_days`, `uncovered_days`, `no_trade_days_pct`, `r_distribution`,
`discarded_tail_days`, `model_versions`,
`news_filter_active`, `calendar_coverage_pct`, `min_cell_trades`, `aggregate`, `by_regime`,
`by_playbook`, `grid`, `fold_expectancy_r`, `fold_trades`, `expectancy_median_r`,
`profitable_folds` y `folds[]` con las fechas de cada ventana, sus velas, su cobertura, el
`error` si el motor lo rechazó y el resultado completo del pliegue (sin la curva de equity,
para que el JSON siga siendo legible).

`_verdict()` decide qué se puede afirmar con esos números y solo hay tres ramas posibles:
cero operaciones ("este informe no demuestra NINGUNA ventaja"), |t| < 2 ("no hay ventaja
demostrada, **ni buena ni mala**"), |t| ≥ 2 (la expectancy se sostiene sobre cero, con la
advertencia de que sigue siendo una sola estrategia sobre un único símbolo). Encima añade,
según el caso, los pliegues rechazados, la estabilidad falsa de "todos los pliegues dan el
mismo número de operaciones" y la cobertura del calendario.

### 14.5 CLI

```bash
python scripts/backtest.py --config config/config.example.yaml --walk-forward --method both \
  --report reports/walk_forward_both.json
```

`--walk-forward` sustituye el corte único: imprime el tramo real de la caché, los pliegues
planificados, y va informando por pliegue (tiempo y operaciones) mientras corre. El JSON va a
`reports/walk_forward.json` si no se pasa `--report`. A diferencia del corte único, aquí **no** se
mueve el tramo hacia la cobertura del calendario: los pliegues los fija la geometría, así que un
calendario corto se informa (cobertura por pliegue y en la lectura honesta) en vez de recortar el
estudio a escondidas. Con menos de 20 operaciones agrupadas avisa por stderr de que no hay base
estadística, y con `--calendar none` y `calendar_unavailable_policy: block` avisa además de que
ese estudio no mide la estrategia sino la ausencia de datos.

### 14.6 Corrida real (2026-10-31) y lectura honesta

Cuatro pliegues de 30 días sobre la caché M5 completa, calendario real y las tres variantes del
detector, sin tocar ningún umbral:

| método | ops agrupadas | acierto | expectancy | t | PF | neto USD | DD USD | racha | pliegues con ops |
|---|---|---|---|---|---|---|---|---|---|
| `both` | 14 | 28,6 % | −0,31 R | −0,92 | 0,66 | −18,51 | 35,49 | 5 | 1 de 4 |
| `rules` | 18 | 22,2 % | −0,32 R | −1,06 | 0,63 | −27,58 | 44,42 | 7 | 1 de 4 |
| `hmm` | 17 | 17,6 % | −0,66 R | −3,13 | 0,34 | −49,16 | 52,34 | 8 | 1 de 4 |

Qué dice esto, sin adornos:

- **No hay ventaja demostrada.** Con `both` y `rules`, |t| < 2: la muestra no permite afirmar ni
  que la estrategia sea mala, aunque todo apunta a que no gana. Con `hmm` la expectancy
  (−0,66 R, |t| = 3,13) **sí** se sostiene sobre cero, y en el lado malo: la lectura firme de
  este estudio es que pierde dinero.
- **Los 120 días de prueba solo dan 14–18 operaciones** en la variante que sea. Es poco, y es
  lo que hay: el playbook dispara en un puñado de velas sobre ~24 000 decisiones por pliegue.
- **La cuadrícula sale vacía de casi todo.** Solo dos celdas tienen operaciones
  (`HIGH_VOLATILITY × breakout` y `RANGE × range_fade`), las dos por debajo de
  `min_cell_trades = 10`, así que las dos se marcan `no concluyentes`. El resto de playbooks y
  de regímenes no generaron ni una entrada en 120 días.
- **Las dos celdas que existen van en direcciones opuestas**, y eso es lo único interesante del
  cuadro: con `rules`, `HIGH_VOLATILITY × breakout` da +0,39 R con profit factor 1,30 (+10,91 USD
  en 9 operaciones) mientras que `RANGE × range_fade` es −1,02 R en 9 operaciones que acabaron
  todas en el stop. Con `both` el breakout se queda en +0,01 R. Nueve operaciones no prueban
  nada —de ahí el `no concluyente`—, pero sí dicen que la agregación negativa viene del rango, no
  del breakout, y esa es la hipótesis que hay que contrastar con más histórico, no con otro
  umbral.
- **Ningún pliegue fue rentable** (0 de 4) y la mediana de la expectancy por pliegue es 0,00 R
  porque tres de los cuatro no tuvieron operaciones.
- **El 91 % de los días con vela de decisión acabaron sin operar** en el agregado `both`; en
  `rules` y `hmm`, 88,8 %. Es la media ponderada por días de decisión de cada pliegue y sale de
  los 100 %, 100 %, 100 % y 65,2 % por pliegue: en el único pliegue con operaciones se abrió en 8
  de sus 23 días (65,2 % = 15/23), y en `rules`/`hmm` en 10 de 23 (56,5 % = 13/23). No es un
  fallo del backtest: el candado de dos operaciones diarias y el filtro de noticias existen para
  no operar, y la especificación pide expresamente que se informe.
- **La distribución de R explica la pérdida mejor que la media.** En `both`, de las 14
  operaciones, **las 10 perdedoras están todas en el tramo `<=-1`** (`{'<=-1': 10, '0..1': 2,
  '1..2': 1, '>=2': 1}`): no se perdió ni una operación a media camino —cada perdedora salió por
  su stop, y cae ligeramente por debajo de −1 R por la comisión (p. ej. −1,0239 R)—. Igual en
  `hmm` (`<=-1:14, 0..1:2, 1..2:1`). En `rules` hay **una sola excepción**
  (`<=-1:13, -1..0:1, 1..2:3, >=2:1`): un `breakout` en corto del 2026-10-05 que cerró por tiempo
  (`TIME`) en −0,26 R, −2,82 USD. El corte único de la Fase 3 no gestiona la posición —breakeven,
  parciales y trailing llegan con la gestión de la Fase 4, que el backtest de señal aislada no
  simula—, así que lo que se está midiendo aquí es "entradas con stop fijo hasta el objetivo o el
  stop", no la estrategia completa con su gestión.
- No se ajustó ningún parámetro para que el resultado "se viera mejor".

### 14.7 El problema que domina el resultado: el calendario no llega

> **Este diagnóstico quedó superado el 2026-11-01** con el puente v1.10 (§17) y la re-corrida con
> cobertura completa (§14.10). Se mantiene tal cual porque explica por qué las cifras de §14.6
> son lo que son: tres pliegues no medían la estrategia, medían la ausencia de datos.

Tres de los cuatro pliegues tienen **0 % de cobertura del calendario** y terminan con **0
operaciones**. La causa no es que el mercado no se moviera: el filtro de noticias consulta el
`EconomicCalendar` y, cuando el tramo pedido cae fuera de la cobertura exportada, el calendario
responde `health = DEGRADED`; con `filters.calendar_unavailable_policy: block` (el valor del
YAML) cualquier candidato se bloquea. La huella en los conteos es exacta: en los pliegues 1–3 el
número de decisiones `BLOCKED` es **igual** al de rechazos por `NEWS`
(`both`: 646/458/622 · `rules`: 418/250/427 · `hmm`: 819/610/850), mientras que en el pliegue 4,
el único cubierto (68,9 %), aparecen los otros filtros (`SESSION 419`, `NEWS 380`,
`MAX_TRADES 279`) y las 14–18 operaciones.

Es decir: esos pliegues no miden la estrategia, miden la falta de datos. El CSV exportado
hoy cubre 2026-10-01 → 2026-11-14 (~44 días) y la caché llega hasta 2025-12-26. **Antes de
fiarse de un walk-forward largo hay que reexportar el calendario con `InpDaysBack` grande**
(600+ días) desde `mt5/GoldbotCalendarExport.mq5`, que es acción de terminal, no de este
código: MT5 no expone el calendario económico por API Python. El informe lo recuerda en la
cabecera (`Filtro noticias: ACTIVO · cobertura media del calendario 17 %`) y en la lectura
honesta, porque un 0 % silencioso es la trampa más fácil de esta fase.

Como comparación diagnóstica, `--calendar none` **no** sirve para ver qué habría pasado sin el
filtro de noticias: sin calendario la propia política `block` bloquea toda candidata
(`domain/filters.py:_news` devuelve `blocked=True` con `observed="sin calendario"`). Corrido, el
resultado es **0 operaciones en los cuatro pliegues** —también el 4, que con calendario dio 14— y
`Días sin operar 100,0 %` (`reports/walk_forward_both_sin_calendario.json`). En los pliegues 1–3
los conteos son **idénticos** a la corrida con calendario (`BLOCKED = NEWS = 646 / 458 / 622`):
fuera de cobertura, quitar el CSV o dejarlo da exactamente el mismo backtest, que es la prueba
directa de que esos pliegues no medían nada. El informe sale vacío por otra ruta, no más
informativo. Y engaña en la cabecera: sin calendario imprime `Filtro
noticias: desactivado`, que describe la fuente y no el efecto; por eso la CLI avisa además por
stderr de que ese estudio no mide la estrategia. La única forma de medir el filtro es cambiar
`filters.calendar_unavailable_policy` a `allow` en el YAML, y eso es una decisión de riesgo, no un
ajuste de backtest.

### 14.8 Verificación

- `pytest -q`: **213 tests verdes**, 24 nuevos en `tests/unit/test_walk_forward.py`. Cubren la
  geometría (anclaje en la primera vela, `test_start == train_end`, sin solapes, avance de
  `step_days`, cola parcial descartada, histórico insuficiente, caché vacía, ventanas
  imposibles), el bucle (agregación entre pliegues, pliegue rechazado que se informa con su
  motivo y no se borra, cobertura del calendario por pliegue, ponderación de los días sin
  operar), las métricas por grupo (`summarize`, t con y sin dispersión, cuadrícula, tramos de R)
  y el informe (las secciones que pide la especificación, "no hay ventaja", "ninguna operación",
  aviso de calendario, `RECHAZADO` y el JSON con el detalle). Ninguna prueba abre MT5 ni lee la
  caché real: usan velas diarias sintéticas y un motor de prueba.
- `ruff check .` limpio; `mypy src/goldbot scripts` sin errores en 71 ficheros.
- Artefactos de la corrida: `reports/walk_forward_both.json`, `reports/walk_forward_rules.json`,
  `reports/walk_forward_hmm.json` y la comparación sin calendario
  `reports/walk_forward_both_sin_calendario.json`. Las tres corridas con calendario son
  deterministas: repetidas tras añadir los días sin operar y la distribución de R, dieron las
  mismas 14/18/17 operaciones y los mismos expectancy, t y drawdown.
- Re-corrida con el calendario de 744 días (§14.10 y §14.10.1): `reports/walk_forward_cal744.json`,
  `reports/walk_forward_rules_cal744.json` y `reports/walk_forward_hmm_cal744.json`, todas con la
  misma fuente congelada `data/cache/calendar_snapshot.csv`.

### 14.9 Limitaciones conocidas

- **Los pliegues solapan sus entrenamientos** (`step_days` 30 < `train_window_days` 180): es
  correcto —ningún pliegue ve su propia prueba—, pero los cuatro pliegues no son cuatro muestras
  independientes y el agregado no debe leerse como 120 días de datos sin relación.
- El plan de pliegues se ancla en días naturales sobre velas de 5 minutos; los tramos de test
  tienen días de bolsa distintos (fin de semana, festivos), por eso las "velas" por pliegue
  varían (5614–6068) aunque la ventana dure lo mismo.
- 120 días de prueba con un único símbolo y una posición a la vez: el drawdown del agregado
  (35–52 USD sobre 1000) es el de esta muestra, no un riesgo esperable a un año. Con el calendario
  completo de §14.10 la misma geometría da **266 USD** y una racha de 11: el rango 35–52 estaba
  medido sobre 14–18 operaciones, y esa cifra era baja porque casi no había trades, no porque el
  riesgo estuviera controlado.
- La salida del `hmm` con t = −3,13 se apoya en 17 operaciones de un solo pliegue; es
  estadísticamente "firme" dentro de la muestra y aun así frágil como predicción.
- El filtro `MAX_TRADES` del candado de 2 operaciones/día aparece en el pliegue cubierto (279
  decisiones bloqueadas por ese candado): el tope diario forma parte de la estrategia medida, no
  es un estorbo del backtest, pero también significa que el resultado depende del reparto
  intradía de las señales.
- Sigue en pie lo de Fases 3 y 4: el motor simula llenado en la apertura siguiente con
  spread/slippage/comisión y asume stop antes que objetivo en la misma vela; nada de eso se ha
  confrontado con un llenado real del broker.

### 14.10 Re-corrida con calendario completo (2026-11-01, puente v1.10)

El puente v1.10 (§17) dejó 744 días de calendario exportados y `data/cache/calendar_snapshot.csv`
los congela. Misma caché M5 (60 000 velas), mismos 4 pliegues de §14.1, misma geometría
180/30/30, **ningún umbral tocado**, `--method both`:

```
python scripts/backtest.py --config config/config.example.yaml --walk-forward --method both \
  --calendar data/cache/calendar_snapshot.csv --report reports/walk_forward_cal744.json
```

La diferencia de fondo es la cobertura: **100 % en los cuatro pliegues** (antes 0/0/0/68,9 %,
media 17 %), y con ella cambia todo el cuadro.

| métrica | §14.6 (calendario de 44 días) | §14.10 (snapshot de 744 días) |
|---|---|---|
| operaciones agrupadas | 14 | **117** |
| acierto | 28,6 % | 26,5 % (31 ganadoras / 86 perdedoras) |
| expectancy | −0,31 R | **−0,199 R** (σ 1,53 R) |
| t | −0,92 | **−1,40** |
| profit factor | 0,66 | 0,62 |
| neto / drawdown | −18,51 / 35,49 USD | **−261,72 / 266,41 USD** |
| racha de pérdidas | 5 | 11 |
| días sin operar | 90,9 % | **25,8 %** |
| pliegues con operaciones | 1 de 4 | **4 de 4** (32 / 28 / 34 / 23) |
| pliegues rentables | 0 de 4 | 1 de 4 (exp. por pliegue −0,44 / +0,16 / −0,01 / −0,59; mediana −0,22 R) |
| distribución de R | `<=-1:10, 0..1:2, 1..2:1, >=2:1` | `<=-1:85, -1..0:1, 0..1:5, 1..2:22, >=2:4` |

Qué dice, sin adornos:

- **Sigue sin haber ventaja demostrada.** Con 117 operaciones el t sube en valor absoluto de 0,92 a
  1,40, pero sigue por debajo de 2: la muestra no autoriza a afirmar que la estrategia sea mala,
  aunque el agregado sea negativo (−0,199 R y PF 0,62). La lectura honesta del informe lo dice con
  esas palabras.
- **El estudio por fin mide la estrategia.** El 91 % de días sin operar de §14.6 era, en su
  mayoría, el filtro de noticias bloqueando por falta de datos. Con cobertura real baja al 25,8 %
  y los cuatro pliegues producen operaciones: esa es la señal de que el número anterior era
  artefacto del calendario, no comportamiento del mercado.
- **La hipótesis de §14.6 sobre el rango se invierte.** Allí `RANGE × range_fade` era −1,02 R en 9
  operaciones y parecía el origen de la pérdida, mientras `HIGH_VOLATILITY × breakout` daba +0,39 R.
  Con las 117 operaciones, `RANGE` se acerca a cero (−0,08 R, PF 0,96, 33 ops) y **el agujero está
  en `HIGH_VOLATILITY × breakout`: 84 operaciones, −0,24 R, PF 0,56, −256,84 USD** —casi todo el
  agregado. Con histórica suficiente, la celda que parecía buena es la que pierde.
- **Las dos celdas ya superan `min_cell_trades`** (84 y 33 operaciones frente a las 9 de §14.6), así
  que el informe las marca `ok` y no `no concluyente`. Eso no las hace significativas: el t de la
  celda de volatilidad alta es −1,79 (se queda corto del umbral 2) y el de `RANGE` es −0,22 con
  PF 0,96, es decir, "casi no pierde" pero tampoco gana.
- **La firma de la pérdida es la misma**: media de ganancia +2,05 R frente a media de pérdida
  −1,01 R y 85 de las 86 perdedoras en el tramo `<=-1`. El juego de pagos no es el problema; el
  problema es que se acierta 26,5 % de las veces y con ese ratio hace falta ~33 % para el
  punto de equilibrio con estos tamaños.
- **El drawdown pasa a ser la cifra seria**: 266 USD sobre una base de ~1 000 USD en 120 días de
  prueba (racha de 11 pérdidas). Es el de esta muestra y el motor de backtest solo corta por
  stop/objetivo/`time_stop_bars` —no aplica la gestión de la Fase 4 (breakeven, parciales,
  trailing) ni el candado de pérdida diaria del 2 %, que en el bot real habrían detenido la
  sangría antes (§12.5)—, pero es la primera vez que el número es lo bastante grande como para que
  ese candado deje de ser teórico.
- Las tres variantes, mismo snapshot, misma caché, mismas ventanas (§14.10.1): todas negativas y
  todas con cobertura del 100 %, pero no todas cuentan la misma historia.
- Tampoco se ajustó ningún parámetro para que saliera mejor. El único cambio entre §14.6 y esta
  corrida es la fuente de calendario.

#### 14.10.1 `rules` y `hmm` con el calendario completo (mismo día)

| métrica | `both` | `rules` | `hmm` |
|---|---|---|---|
| cobertura del calendario | 100 % | 100 % | 100 % |
| operaciones | 117 | **80** | **129** |
| acierto | 26,5 % | 25,0 % | 24,0 % |
| expectancy | −0,199 R | −0,143 R | **−0,274 R** |
| t | −1,40 | −0,79 | **−2,12** |
| profit factor | 0,62 | 0,85 | 0,58 |
| neto USD | −261,72 | −59,80 | **−310,12** |
| drawdown USD / racha | 266,41 / 11 | 180,45 / **16** | 313,30 / 13 |
| días sin operar | 25,8 % | 48,3 % | 20,2 % |
| pliegues con ops / rentables | 4 de 4 / 1 de 4 | 4 de 4 / **2 de 4** | 4 de 4 / **0 de 4** |
| mediana de expectancy por pliegue | −0,22 R | −0,23 R | −0,31 R |

Qué se puede afirmar con las tres juntas:

- **La conclusión estable es que no hay ventaja.** Tres variantes, tres expectancy negativas sobre
  80–129 operaciones. Solo `hmm` pasa el umbral (t = −2,12), así que lo firme sigue siendo "este
  conjunto de parámetros pierde dinero en esta muestra", y lo honesto es que `both` y `rules` no lo
  alcanzan: |t| = 1,40 y 0,79 no permiten afirmar ni buena ni mala suerte.
- **El agujero en USD está en `HIGH_VOLATILITY × breakout` en las tres**: −256,84 USD (`both`,
  84 ops), −283,20 USD (`hmm`, 95 ops) y −43,00 USD (`rules`, 37 ops —el mayor perdido de esa
  variante aunque su expectancy sea casi nula, −0,02 R, porque el neto paga comisión y los stops de
  esa celda son más anchos). Lo que cambia entre variantes es `RANGE`: en `rules` es el que más R
  pierde (−0,25 R en 43 ops), en `hmm` se queda en −0,22 R con PF 0,78 y en `both` casi empata
  (−0,08 R, PF 0,96). **La hipótesis de §14.6 —«la pérdida viene del rango, no del breakout»— no
  sobrevive a las tres corridas: por dinero perdido, es del breakout.**
- Las seis celdas superan `min_cell_trades = 10`, así que ninguna se marca `no concluyente`; eso no
  las vuelve significativas: `RANGE` de `rules` tiene t = −0,89 y `RANGE` de `hmm`, −0,63.
- **La dispersión entre variantes dice algo del problema**: con el mismo histórico y los mismos
  umbrales, el modo de detección cambia el número de operaciones (80 frente a 129) y el signo por
  celda. Un resultado que depende tanto de cómo se etiqueta el régimen no es una ventaja medible;
  es un ajuste.
- En `rules` los dos primeros pliegues son rentables (+0,04 y +0,81 R) y los dos últimos no
  (−0,50 y −0,53 R), con mediana −0,23 R. Es deriva de la segunda mitad del histórico, no
  estabilidad entre pliegues.
- Siguen aplicando los artificios de la muestra de §14.9: los pliegues solapan entrenamientos, el
  motor no gestiona la posición (breakeven, parciales, trailing) ni corta por pérdida diaria, y la
  racha de **16** de `rules` sobre una base de ~1 000 USD recuerda que el candado del 2 % diario del
  bot real habría bloqueado días enteros que aquí se operaron.
## 15. Próximos pasos

- **Fase 1**: conexión MT5, `symbol_info`, descarga de velas, logging JSONL y StateStore
  en modo lectura (sin órdenes). ✅
- **Fase 2**: ContextBuilder + RegimeDetector (reglas y HMM) con visualización y tests. ✅
- **Fase 3**: playbooks, selector, filtros y backtest de señal aislada. ✅
- **Fase 4**: riesgo, gestión y ejecución DEMO con kill switch y reconexión. ✅ (probada
  vela a vela sobre simulador, validada contra el terminal real en solo lectura y con **orden
  IOC llenada, SL/TP verificados y deal conciliado** el 2026-11-02, §12.10)
- **Fase 5**: dashboard completo. ✅ (proceso separado, 10 paneles, comandos con token y
  Pausa probada contra el `Bot` real; `Cerrar todo` ejecutado contra el broker el 2026-11-02)
- **Fase 6**: walk-forward e informe por régimen/playbook. ✅ (4 pliegues de 30 días sobre la
  caché real y las tres variantes del detector; sin ventaja demostrada. El matiz de la primera
  corrida —3 de los 4 pliegues fuera de la cobertura del calendario, así que solo un cuarto del
  estudio era interpretable— está resuelto con el puente v1.10 (§17) y re-corrido con cobertura
  del 100 % en las tres variantes (§14.10.1): 80–129 operaciones, expectancy entre −0,14 y −0,27 R,
  significativa solo en `hmm`, y la pérdida en USD concentrada en `HIGH_VOLATILITY × breakout`.)
- **Fase 7**: operación demo con revisión semanal. 🟡 escrita y probada la capa de revisión (§16);
  de los dos prerequisitos no-código que quedaban, **el del calendario está cumplido** (§17). Falta
  el otro: confirmar un llenado real IOC, su verificación SL/TP y la reconciliación de un deal
  propio en DEMO con el mercado abierto —con el terminal apagado no se puede, y esa es la única
  razón por la que la fase sigue en amarillo.

## 16. Fase 7 — Operación en DEMO y revisión semanal

La especificación cierra con *operación en demo en vivo con revisión semanal del DecisionLog y del
dashboard* (§ Fases, 7). "Revisión semanal" suena a tarea de mirar pantallas; aquí se trata de que
**esa mirada sea reproducible y obligatoria**, así que se escribió como código: un módulo que lee
el StateStore, agrega la ventana, audita los candados y devuelve un informe de texto y otro JSON.

Código: `src/goldbot/review/weekly.py` (agregados y auditorías), `src/goldbot/review/report.py`
(informe de texto y `weekly_review_to_jsonable`) y `scripts/weekly_review.py` (CLI). Runbook de
operación para un día cualquiera: `docs/operacion_demo.md`.

### 16.1 Cuatro decisiones de diseño que importan

- **Solo lectura, por construcción.** La revisión abre la base con
  `sqlite3.connect("file:...?mode=ro", uri=True)`, así que la revisión no *puede* escribir en el
  estado que alimenta el trading y corre con el bot en marcha. No es una convención de código: es
  el modo del driver.
- **Los umbrales se leen del YAML vigente, nunca de un valor recordado.** La auditoría compara
  `risk.max_trades_day`, `risk.risk_pct`, `risk.daily_loss_pct` y `risk.equity_floor_usd` con lo
  que hay en la base, y el recuento de decisiones por debajo del umbral usa
  `regime.confidence_min`. Si mañana se cambia el riesgo al 0,5 %, la revisión de la semana pasada
  se juzga con el número de ahora y se dice en el propio informe —no se guarda una copia de los
  umbrales para que "siempre cuadre".
- **Se reutilizan las métricas del backtest.** Las filas de `trades` se reconstruyen como
  `TradeRecord` del dominio y pasan por `summarize`, `r_histogram` y `has_dispersion`, las mismas
  funciones de §14. Dos informes con fórmulas distintas no son comparables; la revisión semanal y
  el walk-forward tienen que contar la expectancy y los tramos de R exactamente igual.
- **No operar es un resultado.** `sin operar 100 %`, `Cero operaciones cerradas` y `Sin decisiones
  en la ventana` se imprimen tal cual, con su motivo posible, en vez de pintar un informe vacío que
  parece un fallo del script.

### 16.2 Qué agrega

Por **día operativo del servidor** (`clock.trading_day`, clave real de `daily_stats`, no día UTC):
decisiones, entradas, acciones, bloqueos por filtro (una decisión puede acumular varios códigos),
operaciones cerradas, neto USD, PnL realizado, equity de cierre y si el candado diario se activó.

Encima, el total de la ventana: días con decisión y con entrada y su porcentaje sin operar,
playbooks seleccionados, régimen de cada entrada, confianza media/mínima y decisiones por debajo de
`regime.confidence_min`, velas de decisión repetidas (reinicios y `--once`), operaciones cerradas
con sus métricas completas, intentos de orden con su retcode, huecos del feed de equity y eventos
notables. El JSON (`weekly_review_to_jsonable`) lleva las 37 claves de la misma revisión, incluidas
todas las auditorías y cada operación, para comparar semana contra semana.

### 16.3 Los 14 candados auditados

| Código | Qué comprueba | Veredicto |
|---|---|---|
| `DEMO` | `bot_state.mode == DEMO` o `account.is_demo` | `FALLO` si nada indica una cuenta demo |
| `MAX_ENTRADAS_DIA` | entradas del día con más (DecisionLog) y `entries_used` de `daily_stats` | `FALLO` si supera `risk.max_trades_day` |
| `SL_OBLIGATORIO` | toda operación cerrada y toda posición abierta con `sl` | `FALLO` con la lista de tickets |
| `RIESGO_POR_OPERACION` | máximo de `risk_json.risk_pct_actual` frente a `risk.risk_pct` | `FALLO` si se pasa, `WARN` si no hubo entradas |
| `CANDADO_DIARIO` | `daily_lock_latched` en `daily_stats` | `WARN` con los días bloqueados |
| `EQUITY_FLOOR` | equity actual y cierres de día bajo `risk.equity_floor_usd` | `FALLO` si estuvo bajo sin candado |
| `MARGEN_LIBRE` | `margin_free/equity` de cada snapshot frente a `safety.min_free_margin_pct` | `FALLO` si se entrou con el margen bajo; `WARN` si hubo un margen bajo sin entradas o si ningún snapshot trae margen |
| `HUECOS_FEED` | huecos ≥ `GAP_MIN_S` entre snapshots; ≥ 30 min no tolerado es grave | `FALLO`, `WARN` o `OK` |
| `RECONEXIONES` | nº de eventos `RECONNECT` | `WARN` a partir de 3 |
| `ERRORES` | eventos `ERROR` y líneas de log en `ERROR` | `WARN` |
| `RETCODES` | intentos de orden sin `10004` | `WARN` con el desglose de retcodes |
| `KILL_SWITCH` | entradas posteriores al último `KILL_SWITCH` | `FALLO` si el latch no cortó |
| `DECISIONES_DUPLICADAS` | velas de decisión con más de una fila | `WARN` |
| `REGISTRO` | que la ventana tenga decisiones | `WARN` si está vacía |

Dos detalles que evitan falsos positivos:

- **Un `ENTER` rechazado por el RiskManager no cuenta como entrada** (`_DecisionView.is_entry`):
  la especificación exige que el candado de 2/día cuente las operaciones que *se abrieron*, no las
  que se intentaron.
- **El fin de semana no es un hueco.** Un tramo sin snapshots que abarca un sábado o un domingo en
  hora del servidor se marca `tolerated` y se informa aparte; con mercado cerrado, pintar un `FALLO`
  de enlace sería mentira.

### 16.4 CLI

`scripts/weekly_review.py` sigue las convenciones del resto de scripts (`--config` obligatorio,
`load_environment` + `load_config`, `build_parser()`/`main(argv) -> int`). Añade `--days`, `--db`,
`--json` y `--until`:

```bash
python scripts/weekly_review.py --config config/config.example.yaml --days 14 \
  --json reports/revision_14d.json
python scripts/weekly_review.py --config config/config.example.yaml --until 2026-11-01T21:00:00Z
```

`--until` corta la ventana en esa marca UTC (por defecto, ahora) y sirve para repasar una semana ya
cerrada sin reejecutar nada —y para que los tests sean deterministas, que también cuenta. Los
códigos de salida están pensados para automatizar la revisión: **0** sin fallos duros, **2** con algún
`FALLO` de auditoría, **3** si no existe la base, **4** si `--days` o `--until` no valen. Así basta
con mirar los días que devuelven distinto de cero.

### 16.5 Corrida real sobre la base de la DEMO

Ventana 2026-10-25 → 2026-11-01 (7 días) sobre `data/state.db`, generada un domingo con el mercado
cerrado:

- 3 decisiones en 1 día operativo, todas `NO_SETUP` con `range_fade` seleccionado y confianza 0,984;
  **0 entradas, 0 operaciones** y `sin operar 100 %`.
- 13 snapshots de equity con 7 huecos, **los 7 de fin de semana** (tolerados); 0 intentos de orden;
  138 líneas `INFO` y 16 `WARNING`, 0 `ERROR`; 22 `STARTUP` y 22 `SHUTDOWN` (muchos `--once` de
  prueba), 13 avisos `FEED_STALE` y `feed_status: STALE` en el estado.
- Cuenta `61623438 · Pepperstone-Demo · DEMO`, equity 989,82 USD sobre el suelo de 800.
- `FALLO`: ninguno. `WARN`: `DECISIONES_DUPLICADAS` (2 velas repetidas), `HUECOS_FEED` (por los
  `FEED_STALE`) y `RIESGO_POR_OPERACION` (sin entradas que auditar).

El cambio de hora de EE. UU. del 1 de noviembre se ve en el propio informe: `server_offset_min`
pasa de 180 a **120** (el servidor Pepperstone deja de ir en GMT+3), y los días operativos se
agrupan con la nueva zona. Es un buen test de que la revisión usa la hora del servidor vigente y no
un desplazamiento memorizado.

Es el resultado esperado un domingo: el informe lo dice en la *Lectura de la semana* en lugar de
inventarse contexto. Artefacto: `reports/revision_7d.json`, regenerado tras el último arranque en
frío de solo lectura (`run_bot.py --once`, sin `--execute`) que añade un `STARTUP`/`SHUTDOWN` y un
`FEED_STALE` más a la ventana.

### 16.6 Verificación

- `pytest -o addopts=""`: **253 tests verdes** al cerrar la fase (259 tras los tests de calendario
  de §17.5). 32 en `tests/unit/test_weekly_review.py`: agregación por día
  operativo, recuento de bloqueos por filtro, conversión de filas de `trades` a `TradeRecord` y
  métricas reutilizadas, que un `ENTER` rechazado por riesgo no cuente, que la revisión **no
  escribe** en la base (bytes idénticos + `INSERT` rechazado), la ventana que excluye lo anterior,
  base inexistente (no la crea), ventana inválida, y cada resultado de auditoría: más de 2 entradas,
  riesgo por encima del configurado, posición sin SL, cuenta no demo, kill con entrada posterior y
  kill sin ella, candado diario latcheado, margen libre sano / bajo con entrada posterior (`FALLO`) /
  bajo sin entradas y sin columna de margen (`WARN`), hueco grave entre semana, hueco de fin de
  semana tolerado, ausencia total de snapshots, retcode erróneo con reconexiones y errores,
  decisiones duplicadas, reinicios y eventos notables, semana sin violar nada, el informe de texto,
  el JSON y los cuatro códigos de salida de la CLI. Ninguna prueba abre MT5.
- Tras la auditoría de configuración (§16.8): 4 tests nuevos de `MARGIN_CRITICAL` en
  `tests/unit/test_fase4.py` (se dispara, no se dispara con el candado a 0, prioridad del suelo de
  equity, y `equity = 0` no divide por cero) y 2 en `tests/unit/test_config.py` (los umbrales del
  kill switch se leen del YAML; la clave retirada `max_feed_staleness_s` se rechaza en carga).
- `ruff check .` limpio; `mypy src/goldbot scripts` sin errores en 75 ficheros.
- La CLI se ejecutó contra la base real además de en los tests (§16.5), que es donde se ve que un
  informe vacío no es un fallo del programa.

### 16.7 Limitaciones conocidas

- ~~**El arranque en vivo sigue pendiente.** Nada de esta fase sustituye a operar una semana con
  el terminal abierto: la ejecución real de una orden IOC, su verificación SL/TP y el `Cerrar todo`
  contra el broker siguen sin ejercitarse (§12.9 y §13.8).~~ → La ruta de orden está ejercitada
  con dos operaciones reales el 2026-11-02 (§12.10) y la gestión completa —break-even, parcial,
  trailing y `Cerrar todo` con dos posiciones vivas— también, el mismo día (§12.11), con dos
  defectos de producción arreglados ahí. **Lo que sigue faltando es la semana de operación
  continua**: que un playbook —no el script de prueba— dispare la primera `ENTRY`, y que un TP se
  llene en el broker. La revisión semanal es la herramienta para comprobarlos cuando ocurra.
- **La revisión solo puede auditar lo que el bot escribió.** Si el proceso no llegó a arrancar, el
  informe sale con `REGISTRO: WARN` y cero en todo; eso no prueba que el bot se comportara bien,
  prueba que no hay dato. El texto lo dice explícitamente.
- El conteo por nivel de log depende de la retención (`storage.log_lines_retention`, 2000 filas):
  en una semana muy activa las líneas viejas ya no están y el informe lo advierte en su `nota`.
- Con 1–4 operaciones semanales no hay base estadística para nada; el `t` de la revisión se pinta
  `n/a` cuando las R salen todas del mismo stop, igual que en el walk-forward.
- La auditoría `RIESGO_POR_OPERACION` mira `risk_pct_actual` escrito por el RiskManager. Si una
  entrada se registrara sin ese campo, la comprobación degrada a `WARN` y el informe lo señala: no
  puede afirmar que el 1 % se cumplió, solo que no hay con qué comprobarlo.
- `MARGEN_LIBRE` depende de la columna `margin_free` de `equity_snapshots`. Un bot antiguo o un
  snapshot sin ese campo dan `WARN` con el texto `no se puede auditar el margen`, nunca un `OK`
  regalado.

## 16.8 Auditoría de configuración previa al arranque en vivo (2026-11-01)

Antes de la primera sesión con `--execute` se relayó el YAML contra el código que lo consume y
contra el `symbol_info` **live** de Pepperstone (`contract 100 oz`, `point 0.01`, `tick_value 1.0`,
`volume_min/step 0.01`, `volume_max 50`, `stops_level 0`, `freeze 0`, `allowed_fillings 2` → **solo
IOC**, `trade_mode 4`). Lo que ya estaba bien se documenta en §12.2 y §12.5: riesgo sobre equity,
redondeo de lote hacia abajo, rechazo cuando el lote mínimo no cabe, base del candado diario =
equity de inicio de día, doble candado demo.

Tres defectos encontrados y corregidos:

1. **Claves muertas.** `safety.max_feed_staleness_s` y `safety.min_free_margin_pct` se cargaban y no
   se usaban en ningún módulo. La primera se eliminó (duplicaba `data.max_feed_age_s`, que sí corta
   las entradas vía `DATA_STALE`); con `extra="forbid"` un YAML viejo con esa clave ahora **falla en
   carga** en vez de ignorarse, que es lo que se quiere. La segunda se convirtió en el candado real
   `MARGIN_CRITICAL` (§12.5) con su auditoría `MARGEN_LIBRE` (§16.3).
2. **Umbral hardcodeado.** `missing_grace_s` vivía solo como defecto del dataclass de
   `KillSwitchSettings`, contra la regla del spec de que ningún umbral esté hardcodeado. Ahora está
   en `safety.missing_grace_s` (600 s) y `run_bot.py` lo pasa explícitamente.
3. **El 1 % no es alcanzable con ~990 USD en oro.** El riesgo objetivo es 9,90 USD y el lote mínimo
   (0,01) mueve 1 USD por dólar de recorrido, así que la cuantización manda:

| distancia de stop | lote tras el redondeo | riesgo real |
|---|---|---|
| 2,0 USD | 0,04 | 0,81 % |
| 4,4 USD (`range_fade`, ATR M5 4,40) | 0,01 | **0,44 %** |
| 6,6 USD (`atr_sl_mult 1.5`) | 0,01 | **0,67 %** |
| > 9,9 USD | — | **rechazado**: el lote mínimo no cabe en el 1 % |

   Decisión del operador: **aceptarlo y documentarlo**, sin tocar `risk_pct`. Consecuencias legibles
   el primer día: las entradas arriesgan 0,4–0,9 % en vez del 1 % configurado, y un pico de ATR
   (noticias) convierte candidatos en rechazos del RiskManager. Ninguna de las dos rompe un candado:
   el techo del 1 % sigue siendo cierto, y el rechazo es el comportamiento seguro.

## 17. Puente de calendario v1.10 — histórico profundo y snapshot reproducible (2026-11-01)

§14.7 cerró con una limitación que no era de código: el walk-forward no era interpretable porque
el calendario exportado cubría ~44 días. Esta sección documenta la solución, que toca el EA MQL5,
la capa de lectura Python y la CLI de backtest.

### 17.1 Qué impedía operar con histórico

`CalendarValueHistory(from, to)` con una ventana ancha (700+ días) devuelve
**`ERR_CALENDAR_TIMEOUT` (5401)** y no datos. Antes de esta versión el EA pedía la ventana entera de
un solo golpe, así que subir `InpDaysBack` no ampliaba la cobertura: hacía que el export fallara y
el bot se quedara con el CSV viejo —o sin él, y con `calendar_unavailable_policy: block` no abre.
La recomendación de §14.7 («reexportar con `InpDaysBack` grande») no era aplicable tal cual contra
el código del puente.

Segundo problema, de diseño: el CSV en vivo se reescribe cada 60 s y el lector exige frescura
≤ `calendar_max_age_s` (180 s). Un tirón profundo que tarde minutos **degrada la frescura del
único fichero que lee el bot en vivo**, así que la cobertura larga y el contrato de frescura
peleaban por el mismo archivo.

### 17.2 EA v1.10: troceo, presupuesto de tiempo y un segundo fichero

`mt5/GoldbotCalendarExport.mq5` reescrito (compilado 0 errores / 0 warnings, desplegado en la
carpeta `MQL5\Experts\goldbot\` del terminal):

- **`FetchRange(out, dfrom, dto, depth, stats)`** pide el rango y, si devuelve 5401 y `depth > 0`
  y el span supera 4 días, **parte por la mitad y recursa**. Los dos sub-rangos son disjuntos
  (`mid - 1`), así que no hay duplicados que dedupearse. Hasta 3 niveles.
- **`CollectWindow`** recorre la ventana **de lo más reciente a lo más antiguo** en tajadas de
  `InpChunkDays` (20) bajo un presupuesto de `InpMaxExportSeconds` (25 s). Si se agota, se marca
  `stats.truncated` y se publica lo que hay —lo que corta es la cola antigua, nunca la franja que
  usa el filtro en vivo.
- **Dos ficheros, dos contratos** (`ExportLive` / `ExportHistory` en `OnTimer`):
  `calendar_events.csv` (30 atrás + 14 adelante, cada 60 s) es el que lee el bot y su frescura no
  se compromete nunca; `calendar_history.csv` (`InpHistoryDaysBack` 730, refresco cada
  `InpHistoryRefreshMinutes` 60) es el del backtest y puede tardar o quedarse corto sin afectar al
  primero. El histórico solo se exporta si `InpHistoryDaysBack > InpDaysBack`.
- `ExportWindow` **no toca el CSV si no consiguió eventos**: loguea
  `goldbot: <fichero> omitido: 0 eventos (ok=.. vacios=.. fallidos=.. err=..)` y sale, así un fallo
  temporal no borra la cobertura buena que ya estaba publicada.
- `ExportStats` publica en el log del terminal la línea de auditoría:
  `goldbot: calendar_history.csv N eventos (fecha -> fecha) trozos ok=.. vacios=.. fallidos=.. [err=..] [(cortado por presupuesto)], offset servidor ..s`.
  Sin esa línea no hay forma de saber si un "0 eventos" es falta de datos o fallo de petición.

### 17.3 Cobertura real medida: la retención la decide el broker

Con el EA recargado por el operador (el terminal **no** recarga un `.ex5` recompilado de un EA
adjunto; hay que compilar en MetaEditor o re-adjuntar):

| Fichero | Filas | Cobertura | Días |
|---|---|---|---|
| `calendar_events.csv` (en vivo) | 1 441 | 2026-10-02 → 2026-11-15 | 44 |
| `calendar_history.csv` | 28 649 | **2024-10-31 22:00 → 2026-11-14 22:00** | **744** |

26 meses con ~1 200 eventos/mes. Es decir: Pepperstone/MetaQuotes **sí** sirve dos años de
calendario; el límite de antes lo ponía el troceo, no el broker. El log confirma la v1.10 en
servicio (`12:10:20 … calendar_events.csv 1441 eventos … trozos ok=3 vacios=0 fallidos=0, offset
servidor 7200s`) y el CSV en vivo sigue validado tras el cambio
(`salud OK`, 20 eventos en la ventana actual).

### 17.4 Identidad de la ocurrencia: `event_id` no es único

Descubierto antes de cerrar el puente, y era un bug de pérdida de datos. `event_id` identifica el
**tipo** de evento y se repite cada mes (NFP, IPC, PMI…). Las cifras:

| CSV | Filas | `event_id` distintos | `value_id` distintos |
|---|---|---|---|
| en vivo | 1 441 | 864 | — |
| histórico | 28 649 | 1 361 | 28 630 |

Deduplicar por `event_id` (como hacía `CachedEconomicCalendar._merge`) habría **dejado fuera 577 de
las 1 441 filas del CSV en vivo** y ~95 % del histórico. La identidad de la ocurrencia es
`value_id`:

- `domain/types.py:CalendarEvent` lleva `value_id` y la propiedad
  `occurrence_key = value_id or f"{event_id}@{ts}"` (fallback para CSV antiguos sin columna).
- `_merge`, `_save_cache`, `_load_cache` y la unión multi-archivo deduplican por `occurrence_key`;
  `_CACHE_FIELDS` incluye `value_id`, así la caché de `CachedEconomicCalendar` ya no destruye
  ocurrencias al persistir.
- Tests de regresión en `tests/unit/test_calendar.py` (§17.5): identidad por `value_id`, fallback
  sin `value_id`, unión que conserva dos ocurrencias del mismo `event_id`, snapshot de ida y
  vuelta, y el caso concreto de `_merge` —que solo persiste lo que la ventana pedida devuelve, así
  que la prueba consulta una ventana por evento.

### 17.5 Lado Python: unión y snapshot congelado

`src/goldbot/infra/calendar.py`:

- `load_calendar_events_multi(paths)` une varios CSV del puente deduplicando por `occurrence_key`.
  **El primer fichero que trae una ocurrencia gana**, así que se le pasan en orden de frescura (el
  en vivo antes que el histórico) para conservar los valores ya publicados. Devuelve además la
  marca de exportación más nueva de las presentes y lanza `FileNotFoundError` si **ninguno** de los
  rutas existe —no un calendario vacío, que confundiría "sin fuente" con "sin eventos".
- `StaticEconomicCalendar.from_csv` acepta una ruta o un iterable; con varias va por la vía multi.
- `write_calendar_snapshot(target, sources)` escribe
  `data/cache/calendar_snapshot.csv` en el **mismo dialecto** que el puente (misma cabecera,
  `schema_version` 1, atómico con `.tmp`). Eso es lo que hace reproducible un walk-forward: el CSV
  del puente se reescribe cada minuto y un informe de hoy no sería repetible mañana.

`scripts/build_calendar_cache.py` (antes era un stub vacío) y `scripts/backtest.py`:

- `build_calendar_cache.py --source … --target …` informa cobertura, días y fuentes, avisa por
  stderr si falta alguno de los CSV y sale con código `2` si no existe **ninguno** (y `1` si la
  foto sale vacía). Lanza AVISO si la cobertura sigue por debajo de 180 días. Salida de la corrida
  real:
  `Caché: data\cache\calendar_snapshot.csv · 28630 eventos · cobertura 2024-10-31 22:00 -> 2026-11-14 22:00 UTC`.
- `--calendar` pasa a aceptar **varias rutas** (`nargs="+"`, se unen por ocurrencia) y su defecto
  es `[calendar_events.csv, calendar_history.csv]` del terminal. `--calendar none` sigue
  significando "sin fuente" y §14.7 mantiene la advertencia de que eso no mide la estrategia.

Estado medido tras el cambio: `ruff check .` limpio, `mypy src/goldbot scripts` sin errores en 75
ficheros y **259 tests verdes** en `pytest -o addopts=""`. En `tests/unit/test_calendar.py` (13)
los que cubren el puente nuevo son: el mismo `event_id` repetido como ocurrencias distintas, la
clave de ocurrencia sin `value_id`, la unión en vivo + histórico sin duplicar y con el reloj más
nuevo, que la unión exija al menos un fichero, que la caché de respaldo no pierda la ocurrencia
nueva y que `write_calendar_snapshot` se lea con el cargador del puente.

### 17.6 Lo que esto desbloquea y lo que sigue limitado

- Desbloqueado: el walk-forward con filtro de noticias **activo de verdad** en los cuatro pliegues
  (§14.10: cobertura 0 % → 100 %, 14 → 117 operaciones).
- El calendario **dejó de ser el cuello de botella**; ahora lo es el histórico de velas M5. Con
  60 000 velas cerradas (`scripts/sync_history.py --count 60000`) la caché llega a 309 días
  (2025-12-26 → 2026-10-30), que con 180 + 30 dan ~4 pliegues. Ocho pliegues exigirían ~430 días
  de M5, y el broker no los da (§9: M5 desde ~2025-10). Para más pliegues hay que bajar el
  `decision_timeframe` a M15, no estirar el calendario.
- El snapshot es una **foto**: hay que rehacer `build_calendar_cache.py` antes de una corrida que
  deba incluir eventos nuevos, y es deliberado que así sea.
- La tabla SQLite `calendar_events` sigue vacía (§5). Persistir ahí los eventos sería útil para
  el dashboard, pero no es requisito de nada y no se ha hecho.
- El histórico profundo depende de que el EA siga adjunto y del presupuesto de tiempo: si un
  refresco corta por presupuesto, `calendar_history.csv` queda más corto y el siguiente
  `build_calendar_cache.py` lo detecta por la línea de cobertura.

