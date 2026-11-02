# goldbot — Bot XAUUSD (MT5 demo)

Bot de trading adaptativo para XAUUSD en cuenta DEMO de Pepperstone (MetaTrader 5, Windows),
con núcleo de decisión independiente del broker, backtest walk-forward y dashboard local.

> **Estado: Fases 1–6 completadas.** El bot conecta con MT5, descarga velas cerradas,
> construye el contexto multi-timeframe, detecta el régimen (reglas, HMM o ambos), pasa por
> selector, playbooks y filtros hasta una decisión auditable, y —solo en DEMO y con los
> guards activos— gestiona riesgo, posiciones y ejecución con kill switch. La Fase 5 añade el
> dashboard local en un proceso separado y la Fase 6 el walk-forward por pliegues con informe
> por régimen y playbook. Su conclusión, tal cual: **no hay ventaja estadística demostrada**. La
> primera corrida dejó interpretable solo uno de los cuatro pliegues porque el calendario económico
> no cubría el resto; con el puente v1.10 (§17 del diseño) la cobertura es del 100 % y el re-corrido
> de las tres variantes sigue sin ventaja —80 a 129 operaciones, expectancies de −0,14 a −0,27 R,
> significativa solo en `hmm` (t = −2,12) y en el sentido de perder, con la pérdida en USD siempre
> en `HIGH_VOLATILITY × breakout`—. **Fase 7 escrita y probada; el llenado real de una orden en el
> broker sigue pendiente**: hay que probarlo con el mercado abierto. Ver `docs/fase0_diseno.md`.

## Requisitos

- Windows 10/11, Python 3.11 (probado con 3.11.9).
- Terminal MetaTrader 5 de Pepperstone instalado y con sesión DEMO iniciada.
- Trading algorítmico habilitado en el terminal.

## Instalación

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -e ".[dev]"
cp .env.example .env   # opcional: credenciales y token del dashboard
```

Para montar el bot en **otra máquina Windows** y dejarlo operativo —terminal, cuenta demo, puente
de calendario, `.env`, arranque y checklist del primer día—, el manual paso a paso está en
[`docs/instalacion_otra_maquina.md`](docs/instalacion_otra_maquina.md). El día a día (arranque,
pausa, kill switch, revisión semanal) vive en [`docs/operacion_demo.md`](docs/operacion_demo.md).

## Fase 1: datos en vivo (solo lectura)

```bash
# Un ciclo: conecta, descarga histórico, sondea una vez y sale
python scripts/run_bot.py --config config/config.example.yaml --once

# Bucle de sondeo (Ctrl+C para salir); --cycles limita las iteraciones
python scripts/run_bot.py --config config/config.example.yaml --poll-interval 5

# Solo histórico, con timeframe/cantidad opcionales
python scripts/sync_history.py --config config/config.example.yaml
python scripts/sync_history.py --config config/config.example.yaml --timeframe M5 --count 2000
```

## Fase 2: contexto y régimen (solo lectura)

En cada arranque el `DataService` descarga el histórico de todos los timeframes
configurados, entrena el detector de régimen y, en cada vela de decisión nueva, reconstruye
el `ContextSnapshot` y emite `REGIME_CHANGE` cuando cambia la etiqueta confirmada.
`--no-analysis` arranca solo el feed de datos sin análisis.

```bash
# Ciclo único con resumen de régimen, sesión y hash del snapshot
python scripts/run_bot.py --config config/config.example.yaml --once

# Informe HTML autocontenido con bandas de régimen sobre las últimas 600 velas
python scripts/visualize_regimes.py --config config/config.example.yaml --limit 600
```

`scripts/visualize_regimes.py` entrena fuera de muestra (las velas visibles nunca se usan
para ajustar) y escribe `reports/regimes.html`, sin CDN ni escrituras en el StateStore.
Además de la pista del detector configurado, dibuja dos pistas comparativas (reglas y HMM)
para auditar desacuerdos. Con `regime.method: both` la etiqueta solo se confirma cuando
reglas y HMM coinciden; el desacuerdo se publica como `UNKNOWN`.

Qué se persiste en `bot_state`:

- `context_json`: `snapshot` completo (sesgo por timeframe, estructura, timing, frescura,
  sesión, hashes), `session`, `model` (`name`, `version`, `params_hash`) y offset del servidor.
- `regime_json`: etiqueta, confianza, probabilidades, velas confirmadas y método.

El snapshot incluye `snapshot_hash` (reproducible) y `config_hash`; solo se construye sobre
velas cerradas y con features causales (`ewm`, `rolling`, `shift`), sin look-ahead.

Artefactos que produce:

- `data/state.db`: `bot_state`, `equity_snapshots`, `events`, `log_lines`, `history_sync`.
- `data/cache/XAUUSD/*.csv.gz`: velas cerradas por timeframe (CSV gzip, sin pyarrow).
- `logs/bot.jsonl`: una línea JSON por registro, con rotación 10 MB × 5.

Reglas horarias y de datos:

- La hora del servidor es `America/New_York + 7 h` (GMT+3 con DST de EE. UU., GMT+2 si no).
  Todo se persiste en UTC; el `trading_day` se calcula en hora de servidor.
- Solo se usan velas cerradas (`closed_only=True`, `as_of_utc`); la vela en formación nunca
  entra en la caché ni en el feed.
- Si el mercado está cerrado, el feed aparece como `STALE` con la edad real del tick; no se
  inventan datos. La reconexión usa backoff exponencial (1 → 60 s).
- El calendario económico se exporta desde MT5 con `mt5/GoldbotCalendarExport.mq5`
  (ver `mt5/README.md`) y lo consume el filtro de noticias del backtest y del núcleo de
  decisión. El EA publica **dos CSV**: `calendar_events.csv` (ventana corta, cada 60 s —es el que
  vale en vivo, con su control de frescura—) y `calendar_history.csv` (744 días medidos, refresco
  lento). Las peticiones al calendario van troceadas porque una ventana ancha devuelve
  `ERR_CALENDAR_TIMEOUT`. Para backtests, `scripts/build_calendar_cache.py` congela la unión de
  ambos en `data/cache/calendar_snapshot.csv`.

## Fase 3: playbooks, selector y filtros en backtest

La decisión pasa por: selector por régimen → playbook → filtros previos → `ENTER`,
`NO_SETUP`, `BLOCKED` o `NO_TRADE`. El backtest reutiliza exactamente esa cadena y simula
entradas en la apertura siguiente con spread, slippage y comisión, asumiendo el stop antes
que el objetivo si ambos se tocan en la misma vela.

```bash
# Configuración completa (reglas + HMM) sobre la caché y el calendario reales
python scripts/backtest.py --config config/config.example.yaml

# Comparativas solo diagnóstico (no son ajustes de parámetros)
python scripts/backtest.py --config config/config.example.yaml --method rules
python scripts/backtest.py --config config/config.example.yaml --method hmm

# Log de decisiones y ventana concreta
python scripts/backtest.py --config config/config.example.yaml \
  --start 2026-09-30 --end 2026-10-30 --jsonl logs/backtest_decisions.jsonl
```

Salida: informe de texto, `reports/backtest_phase3.json` y, opcionalmente, JSONL con un
registro por decisión (snapshot, régimen, filtros, rationale).

**Resultado real de la ventana 2026-09-30 → 2026-10-30 (config `both`):** 25 operaciones,
20 % de acierto, expectancy −0,31 R, profit factor 0,52, −54,93 USD, drawdown 5,49 %. No hay
ventaja estadística demostrada; la muestra es pequeña y la gestión de salida llega en la
Fase 4. No se ajustaron parámetros para maquillar el resultado. Detalle y comparativa de
métodos en `docs/fase0_diseno.md` (§11).

## Fase 4: riesgo, gestión y ejecución DEMO

Habilita la ejecución (`--execute`) sobre cuenta **DEMO**, con candados duros antes de
construir el bot: `assert_demo_account` y `assert_algo_trading`. Sin ellos el proceso se niega
a arrancar (`run_bot` devuelve 3).

```bash
# Una pasada: decide y, si hay setup, ejecuta en DEMO
python scripts/run_bot.py --config config/config.example.yaml --execute --once

# Bucle con gestión de posiciones, kill switch y backoff
python scripts/run_bot.py --config config/config.example.yaml --execute --poll-interval 5
```

El ciclo es siempre: lecturas → kill switch → **reconciliación de deals** → gestión de lo
abierto → candado diario → kill/pausa → decisión y ejecución. Lote a `risk_pct` % del equity
redondeado **hacia abajo** al step del broker (si el lote mínimo excede el riesgo, no se
opera), `validate_order` como puerta obligatoria, verificación del SL post-ejecución (si el
broker no lo dejó puesto, se corrige o se cierra) y gestión por ATR/estructura con
break-even, parciales, trailing y time stop.

**Corrida real en DEMO (2026-10-31)**: `--execute --once` contra la terminal Pepperstone build
6295 pasó los guards (cuenta `trade_mode 0`, Algo trading activo), leyó las specs reales de
`XAUUSD` (`trade_tick_size` 0,01, `trade_tick_value` 1,0, contrato 100 oz, step 0,01,
`stops_level` 0), resolvió el filling IOC desde el bitmask `2` del símbolo y calculó 0,02 lotes
a 0,89 % de riesgo sobre 989,82 USD. La decisión fue `NO_SETUP` y **no se envió ninguna orden**:
era sábado, el feed reportaba `STALE` (tick de 76 413 s) y el mercado está cerrado. Tampoco se
tocó nada ajeno: la cuenta tiene 105 deals de otros `magic` y `positions(20261030)` /
`closed_deals()` devolvieron 0.

**Corrida real con orden enviada (2026-11-02, mercado abierto)**: dos operaciones de 0,01 lote
por la ruta de producción se llenaron con `retcode 10009 (DONE)` y filling IOC, el broker devolvió
su SL/TP exactos, la primera la cerró el bot por time stop (+0,23 USD netos) y a la segunda la
mató el `Cerrar todo` del panel (ejecutado en 2,78 s, +0,87 USD). Ambas quedaron conciliadas en
`trades` y `daily_stats` al cerrar la vela siguiente. La prueba destapó **cinco fallos que solo
aparecen con orden real** — `TradePosition.commission` y `.position` no existen en la build
5.0.6231, `history_deals_get(fecha, fecha)` devuelve vacío, el time stop contaba minutos como
segundos, y el `comment` de la orden tenía tope de **29** caracteres, no 31: concatenar el
`decision_id` hacía que `order_send` rechazara la petición localmente y el bot lo leyera como un
fallo de enlace. Sin ese último arreglo **ninguna entrada del bot habría llegado al mercado**.
Detalle en `docs/fase0_diseno.md` §12.9 y §12.10; procedimiento de la prueba en
`docs/operacion_demo.md` §9. Sigue sin probarse: una `ENTRY` disparada por un playbook (estas dos
las fabricó el script) y un SL saltando en el servidor con el bot caído.

## Fase 5: dashboard local

Proceso **separado** del bot: no importa `MetaTrader5`, no corre en su bucle y no decide nada.
Lee la base SQLite que escribe el bot (conexión de solo lectura) y su única escritura es
encolar un comando en la tabla `commands`.

```bash
# token de escritura en el entorno; sin él los botones quedan bloqueados con 401
export DASHBOARD_TOKEN="$(openssl rand -hex 16)"
python scripts/run_dashboard.py --config config/config.example.yaml

# inventario de endpoints y estado de la base sin abrir puerto
python scripts/run_dashboard.py --config config/config.example.yaml --check
```

Abre en `http://127.0.0.1:8787` (el arranque se niega si `dashboard.host` no es loopback).
Diez paneles: cabecera con hora del servidor y uptime, KPIs de cuenta y riesgo del día, chips
de enlace y mercado con la última vela **cerrada** por temporalidad, "cerebro" (régimen,
playbook, filtros, sesgo multi-temporal, decisiones incluidas las abstenciones), posiciones
abiertas, gráficos de equity 24 h y PnL por día, historial cerrado con filtro 7/30/90 d,
eventos, log y auditoría de comandos. Sin CDN: uPlot y los estáticos se sirven en local, así
que la página funciona sin internet.

**Si falta un dato, lo dice**: cada fuente lleva su antigüedad y su panel la pinta como
"dato atrasado · hace 2 h" (fuentes de ciclo) o "último registro hace 6 h" (tablas de
registro), con un banner cuando el bot dejó de escribir. El equity se dibuja sobre una rejilla
de minutos donde los huecos del enlace son cortes de línea, nunca interpolación.

Los únicos comandos admisibles son `pause`, `resume`, `close_all` y `reset_kill_switch`; el
modelo de la petición no tiene campos para abrir una operación ni para cambiar riesgo, lote o
símbolo. Pausar **no cierra posiciones**: el bot sigue gestionando lo abierto y solo bloquea
entradas nuevas (así está probado contra el `Bot` real en `tests/integration/`). "Cerrar todo"
exige dos clics, caduca a los `close_all_ttl_s` y se puede deshabilitar con
`dashboard.close_all_enabled: false` (403). "Rearmar kill switch" también va a dos clics, solo
se muestra mientras el candado está puesto (el botón desaparece en `OPERATIVO`), no caduca y su
tooltip dice por qué latcheó. Detalle en `docs/fase0_diseno.md` (§13) y capturas en
`docs/capturas_fase5/`.

## Fase 6: validación walk-forward

Ningún número de este proyecto sale de optimizar sobre todo el histórico. `--walk-forward`
corta la caché en pliegues anclados en la primera vela: cada uno entrena su modelo de régimen
solo con datos anteriores a `train_end` y prueba en la ventana siguiente, y la ventana se
desplaza `step_days` (180 días de entrenamiento / 30 de prueba / paso de 30 en el YAML). Los
tramos de prueba partiales se descartan y se informan (`cola sin evaluar 8 días`); un pliegue
que el motor rechaza se lista con su motivo, no se borra del recuento.

```bash
# Congela el calendario del puente (CSV en vivo + histórico) en un snapshot reproducible
python scripts/build_calendar_cache.py
#   Caché: data\cache\calendar_snapshot.csv · 28630 eventos
#   cobertura 2024-10-31 22:00 -> 2026-11-14 22:00 UTC · 744 días

# Las tres variantes del detector, sobre la caché y el calendario reales
python scripts/backtest.py --config config/config.example.yaml --walk-forward --method both \
  --calendar data/cache/calendar_snapshot.csv --report reports/walk_forward_both.json
python scripts/backtest.py --config config/config.example.yaml --walk-forward --method rules \
  --calendar data/cache/calendar_snapshot.csv --report reports/walk_forward_rules.json
python scripts/backtest.py --config config/config.example.yaml --walk-forward --method hmm \
  --calendar data/cache/calendar_snapshot.csv --report reports/walk_forward_hmm.json

# Ventanas distintas sin tocar el YAML (solo diagnóstico)
python scripts/backtest.py --config config/config.example.yaml --walk-forward \
  --train-days 120 --test-days 20 --step-days 20
```

`--calendar` acepta **varias rutas** y las une por ocurrencia; sin pasarle nada usa los dos CSV del
puente (`calendar_events.csv` y `calendar_history.csv`). Con el snapshot se corre sobre una foto
congelada: el CSV del puente se reescribe cada 60 segundos y un informe no sería repetible sin ella.

El informe (texto y JSON) trae lo que pide la especificación: número de operaciones, expectancy,
profit factor, drawdown, distribución de R, rendimiento **por régimen**, **por playbook** y en la
cuadrícula régimen × playbook, % de días sin operar, estabilidad entre pliegues (mediana de la
expectancy, pliegues rentables) y una sección final de *lectura honesta*. `backtest.min_cell_trades`
(10) marca como `no concluyente` toda celda con menos operaciones; donde las R salen todas del
mismo stop la columna `t` se pinta `n/a` en vez de un número gigante y falso.

**Corrida original (2026-10-31), con el calendario de solo 44 días:**

| método | ops | acierto | expectancy | t | PF | neto USD | días sin operar | pliegues con ops |
|---|---|---|---|---|---|---|---|---|
| `both` | 14 | 28,6 % | −0,31 R | −0,92 | 0,66 | −18,51 | 91,0 % | 1 de 4 |
| `rules` | 18 | 22,2 % | −0,32 R | −1,06 | 0,63 | −27,58 | 88,8 % | 1 de 4 |
| `hmm` | 17 | 17,6 % | −0,66 R | −3,13 | 0,34 | −49,16 | 88,8 % | 1 de 4 |

Con `filters.calendar_unavailable_policy: block`, **3 de los 4 pliegues caían fuera de la cobertura
del calendario exportado** (2026-10-01 → 2026-11-14) y el filtro de noticias los bloqueaba enteros
—en esos pliegues el conteo de `BLOCKED` era exactamente el de rechazos por `NEWS`—. Esos pliegues
no medían la estrategia: medían la falta de datos.

**Re-corrido (2026-11-01) con el puente v1.10 y cobertura del calendario del 100 %** (misma
caché, misma geometría, ningún umbral tocado):

| métrica (`both`) | con 44 días de calendario | con 744 días |
|---|---|---|
| operaciones agrupadas | 14 | **117** (31 ganadoras / 86 perdedoras, 26,5 % de acierto) |
| expectancy | −0,31 R | **−0,199 R** (σ 1,53 R, t = −1,40) |
| PF / neto / drawdown | 0,66 / −18,51 / 35,49 USD | 0,62 / **−261,72** / **266,41** USD |
| días sin operar | 91,0 % | **25,8 %** |
| pliegues con operaciones | 1 de 4 | **4 de 4** (32 / 28 / 34 / 23) |
| pliegues rentables | 0 de 4 | 1 de 4 (mediana −0,22 R) |

**Sigue sin haber ventaja demostrada**, y ahora la cifra significa algo: |t| = 1,40 no llega a 2, así
que la muestra no autoriza a afirmar que la estrategia sea mala aunque el agregado sea negativo. Dos
cosas cambian con los datos completos: el 91 % de días sin operar era en su mayoría artefacto del
calendario, y la media de ganancia (+2,05 R) frente a la de pérdida (−1,01 R) pide ~33 % de acierto
para el punto de equilibrio mientras se acierta 26,5 %.

Las tres variantes sobre el mismo snapshot:

| variante | ops | acierto | expectancy | t | PF | neto USD | drawdown / racha | pliegues rentables |
|---|---|---|---|---|---|---|---|---|
| `both` | 117 | 26,5 % | −0,199 R | −1,40 | 0,62 | −261,72 | 266,41 / 11 | 1 de 4 |
| `rules` | 80 | 25,0 % | −0,143 R | −0,79 | 0,85 | −59,80 | 180,45 / 16 | 2 de 4 |
| `hmm` | 129 | 24,0 % | **−0,274 R** | **−2,12** | 0,58 | **−310,12** | 313,30 / 13 | 0 de 4 |

Tres variantes, tres expectancies negativas; solo `hmm` pasa el umbral de significación, y en el
sentido de perder. Lo estable es **dónde se va el dinero: `HIGH_VOLATILITY × breakout`** —−256,84 USD
en `both` (84 operaciones), −283,20 en `hmm` (95) y también el mayor perdido en `rules` (−43,00 con
37)—. Eso **refuta la hipótesis de la corrida corta**, que con 9 operaciones sostenía que el problema
era el rango; `RANGE` es la celda que cambia de signo según el detector (−0,08 R en `both`, −0,25 R
en `rules`). Y el solo hecho de que el modo de detección mueva el resultado de 80 a 129 operaciones
con los mismos umbrales dice que esto no es una ventaja medible, es un ajuste.

Sigue valiendo el caveat de la corrida corta: **85 de las 86 perdedoras caen en el tramo `<=-1` de
R** y el motor de backtest solo corta por stop, objetivo o `time_stop_bars` —no aplica la gestión de
la Fase 4 (breakeven, parciales, trailing) ni el candado de pérdida diaria del 2 %—, así que lo
medido son entradas con stop fijo, no la estrategia con su gestión.

Detalle, geometría y limitaciones en `docs/fase0_diseno.md` (§14, §14.10 y el puente en §17).

## Fase 7: operación en DEMO y revisión semanal

La especificación pide operar en demo **con revisión semanal del DecisionLog y del dashboard**.
Esa revisión es código, no buena voluntad: `goldbot/review/` lee el StateStore **en modo solo
lectura** (`mode=ro`, así que puede correr con el bot funcionando), agrega la ventana por **día
operativo del servidor** y audita los candados duros contra el YAML vigente.

```bash
# los últimos 7 días, informe de texto por consola
python scripts/weekly_review.py --config config/config.example.yaml

# ventana a medida + JSON para comparar semana a semana
python scripts/weekly_review.py --config config/config.example.yaml --days 14 \
  --json reports/revision_14d.json

# repasar una semana ya cerrada (la ventana termina en --until)
python scripts/weekly_review.py --config config/config.example.yaml --until 2026-11-01T21:00:00Z
```

Sale con **0** si todo pasa, **2** si alguna auditoría dura está en `FALLO`, **3** si no hay base
y **4** si los argumentos no valen, para poder dejarla automatizada y solo mirar los días que
devuelven distinto de cero. El informe trae: actividad por día (decisiones, entradas, bloqueos por
filtro, PnL realizado, candado diario), las métricas de las operaciones cerradas de la ventana
reutilizando el módulo del backtest —expectancy con su `t`, que se pinta `n/a` si todas las R salen
del mismo stop—, salud del enlace (huecos de equity, con el fin de semana tolerado y dicho como
tal), retcodes y niveles de log, y la auditoría de 14 candados: `DEMO`, `MAX_ENTRADAS_DIA`,
`SL_OBLIGATORIO`, `RIESGO_POR_OPERACION`, `CANDADO_DIARIO`, `EQUITY_FLOOR`, `MARGEN_LIBRE`,
`HUECOS_FEED`, `RECONEXIONES`, `ERRORES`, `RETCODES`, `KILL_SWITCH`, `DECISIONES_DUPLICADAS`,
`REGISTRO`.

**Corrida real sobre la base de la DEMO (ventana 2026-10-25 → 2026-11-01, 7 días, un domingo con
mercado cerrado):** 3 decisiones en 1 día operativo, `NO_SETUP` las tres, **0 operaciones** y 100 %
de días sin operar; 13 snapshots de equity con 7 huecos, todos de fin de semana (tolerados); cuenta
`61623438 · Pepperstone-Demo · DEMO`, equity 989,82 USD sobre el suelo de 800; 0 intentos de orden
y 0 errores. Tres `WARN` y ningún `FALLO`: `DECISIONES_DUPLICADAS` (2 velas con más de una fila por
los reinicios y los `--once`), `HUECOS_FEED` (13 avisos `FEED_STALE` del fin de semana) y
`RIESGO_POR_OPERACION` (sin entradas que auditar). El cambio de hora del 1 de noviembre también se
ve: el offset del servidor pasa de +180 a **+120 min** y la revisión lo lee del estado vigente en
lugar de suponerlo. Artefacto: `reports/revision_7d.json`.

**Auditoría de config antes del live.** Los umbrales del kill switch están todos en `safety`
(`max_consecutive_execution_errors`, `missing_grace_s`, `min_free_margin_pct`); se retiró
`max_feed_staleness_s`, que no consumía nada, y `Section` lleva `extra="forbid"`, así que un YAML
con claves viejas falla en carga en vez de ignorarse. El margen libre es ahora un candado real: por
debajo del 30 % del equity el bot latchea `MARGIN_CRITICAL` en lugar de dejar que el broker cierre
por stop out.

**El 1 % de riesgo con ~990 USD en oro se cuantiza.** Con `contract_size 100`, lote mínimo 0,01 y
1 % = 9,90 USD, el redondeo hacia abajo del lote deja un riesgo real de **0,44 % con stop de 4,4 USD**
y **0,67 % con 6,6 USD**, y **cualquier stop por encima de ~9,9 USD se rechaza** porque el lote mínimo
no cabe en el riesgo. No es un fallo: es la aritmética del capital, y el techo del 1 % sigue
cumpliéndose. Conviene saberlo el primer día para no leer una entrada a 0,5 % como un bug. Detalle
medido contra `symbol_info` en `docs/fase0_diseno.md` (§16.8).

El arranque en vivo sigue pendiente de lo que no es código: el terminal abierto con el mercado
abierto. Orden de arranque, checklist del primer día, cuándo pausar y cómo leer los hallazgos en
`docs/operacion_demo.md`; decisiones de diseño en `docs/fase0_diseno.md` (§16).

## Verificación rápida

```bash
python -c "import goldbot, goldbot.domain.types, goldbot.domain.interfaces"
python -c "from goldbot.config import load_config; load_config('config/config.example.yaml')"
pytest -q
```

Los imports no cargan `MetaTrader5`: el adaptador lo importa de forma perezosa, así que los
tests y el backtest funcionan sin terminal.

## Estructura

```
src/goldbot/domain/     núcleo puro: tipos, regímenes, playbooks, filtros, riesgo, gestión
src/goldbot/adapters/   MT5 (import perezoso) y simulador de broker
src/goldbot/infra/      SQLite WAL, migraciones, caché de velas, reloj, calendario, logging
src/goldbot/engine/     servicio de datos (Fase 1), bucle del bot, guards y kill switch
src/goldbot/backtest/   motor event-driven, walk-forward, métricas por grupo e informe
src/goldbot/dashboard/  FastAPI + estáticos locales (Fase 5)
src/goldbot/review/     revisión semanal de solo lectura: agregados, auditorías e informe (Fase 7)
mt5/                    EA del calendario nativo (MQL5) y documentación
scripts/                run_bot, backtest, sync_history, build_calendar_cache, visualize_regimes,
                        run_dashboard, weekly_review
config/                 configuración YAML de ejemplo
docs/                   diseño, fases 2–7, runbook de operación demo y capturas del dashboard
```

## Seguridad

- El bot se niega a ejecutar en cuenta no-demo salvo `safety.allow_live: true` explícito.
- El dashboard escucha solo en `127.0.0.1` y exige token para comandos de escritura.
- Las credenciales viven en `.env` (ignorado por git), nunca en YAML ni en la base de datos.
- La revisión semanal abre la base con `mode=ro`: no puede escribir en el estado del bot ni
  enviar órdenes, solo leer y auditar.

## Fases

0. Diseño y esqueleto. ✅
1. Conexión MT5, descarga de datos, `symbol_info`, logging y StateStore en modo lectura. ✅
2. ContextBuilder + RegimeDetector (reglas, HMM y blend) con visualización histórica. ✅
3. Playbooks, selector, filtros y backtest de señal aislada. ✅
4. Riesgo, gestión y ejecución DEMO con kill switch. ✅ (validada contra el terminal real en
   solo lectura y con **orden IOC llenada de verdad**, SL/TP verificados en el broker y deal
   conciliado en `trades`/`daily_stats`; break-even, parcial y trailing ejercitados sobre
   posiciones reales —2026-11-02, `docs/fase0_diseno.md` §12.10 y §12.11)
5. Dashboard local en proceso separado, con comandos por token. ✅ (`Cerrar todo` ejecutado
   contra el broker real con **dos posiciones vivas a la vez** y las dos conciliadas)
6. Backtest walk-forward e informe. ✅ (4 pliegues, informe por régimen y playbook; sin ventaja
   demostrada. La primera corrida tenía 3 de 4 pliegues sin cobertura de calendario; con el puente
   v1.10 la cobertura es del 100 % y el re-corrido —117 operaciones, −0,199 R, t = −1,40— sigue sin
   demostrar ventaja, ahora con datos que sí miden la estrategia)
7. Operación demo en vivo y revisión semanal. 🟡 (capa de revisión escrita, probada y corrida
   contra la base real: 14 auditorías, informe de texto y JSON, **289 tests**; el bot corre
   `--execute` en DEMO y la ruta de orden está verificada —queda la primera `ENTRY` disparada por
   un playbook y la semana de operación continua, ver `docs/operacion_demo.md`)
8. Puente de calendario v1.10: histórico profundo (744 días), troceo de peticiones y snapshot
   reproducible para validación. ✅
