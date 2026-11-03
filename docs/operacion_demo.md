# Operación en DEMO — runbook (Fase 7)

Cómo se arranca, se mira y se revisa goldbot en la cuenta DEMO de Pepperstone. Este documento es
para un día concreto; lo que se aprende cada semana se consolida con `scripts/weekly_review.py`
(sección 7). Nada de lo de aquí sustituye al criterio del operador: el bot abre, gestiona y se
bloquea solo, pero **quién decide dejarlo operando eres tú**.

## 1. Qué se necesita antes de tocar nada

| Requisito | Cómo se comprueba |
|---|---|
| Terminal MT5 abierto, logueado en la cuenta **DEMO** (en esta máquina: 61623438) | la pestaña del terminal; si está en rojo, no hay bot |
| Algo trading habilitado en el terminal | `Herramientas → Opciones → Asesores Expertos`, y el botón `Algo Trading` |
| Mercado abierto, fuera del buffer de rollover (`filters.rollover_buffer_min`) | el gráfico de XAUUSD avanza · hora del servidor = `America/New_York + 7 h` |
| Calendario económico exportado y fresco | `Common\Files\goldbot\calendar_events.csv` con mtime < 180 s (EA `mt5/GoldbotCalendarExport.mq5` corriendo en cualquier gráfico) |
| `.env` con credenciales si se quiere login programático | `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (nunca en el YAML ni en la base) |
| Token del dashboard para los botones de escritura | `DASHBOARD_TOKEN=<token-largo>` en el entorno donde arranca `run_dashboard.py` |

Sin el CSV del calendario y con `filters.calendar_unavailable_policy: block` (el valor del YAML)
**el bot no abre operaciones**: el filtro de noticias bloquea toda candidata. Es el
comportamiento correcto —no operar sin datos—, no un fallo que haya que "arreglar" cambiando la
política a `allow` sin pensarlo.

## 2. Orden de arranque

Todo desde la raíz del proyecto y con el venv activado (`pip install -e ".[dev]"`, ver el README;
los comandos de este documento se probaron con 3.11.9).

```bash
# 1) (opcional) dejar el histórico en su punto si el bot estuvo varios días parado
python scripts/sync_history.py --config config/config.example.yaml

# 2) bot + ejecución DEMO. SIN --execute NO decide: solo mantiene velas y régimen (ver abajo).
export MT5_LOGIN=... MT5_PASSWORD=... MT5_SERVER=...   # solo si no estás ya logueado en el terminal
python scripts/run_bot.py --config config/config.example.yaml --execute

# 3) dashboard, en OTRA terminal/consola (proceso separado; nunca dentro del bucle del bot)
export DASHBOARD_TOKEN="$(openssl rand -hex 16)"
python scripts/run_dashboard.py --config config/config.example.yaml
```

Detalles que importan:

- `run_bot.py --execute` es la única forma de que salga una orden. Al arrancar imprime
  `EJECUCIÓN DEMO HABILITADA (Fase 4)` y, antes de construir el bot, verifica cuenta DEMO y
  algo trading; si algo falla, sale con código **3** y mensaje `EJECUCIÓN RECHAZADA: ...` sin
  haber tocado el mercado.
- **Sin `--execute` el bot no decide nada.** No es "solo lectura de mercado", es que no se
  construye el `Bot` y el proceso queda en `service.run()` (`scripts/run_bot.py:198-204`): velas,
  régimen, equity y `bot_state`, pero **cero filas en `decisions` y cero en `decisions.jsonl`**.
  El síntoma en el panel es el chip ámbar del Cerebro con una decisión vieja cada vez más vieja
  (la última que escribió un bot con `--execute`); los KPIs y los chips de feed siguen verdes
  porque esos sí los alimenta el servicio de datos. Es el arranque que se usa para mirar el feed
  sin riesgo, no para validar la estrategia: si quieres ver decisiones, tiene que llevar
  `--execute`. Y el token: `run_dashboard.py` lee `.env`, así que exportar uno nuevo en cada
  arranque (`openssl rand -hex 16`) rompe los botones del navegador hasta que lo pegues otra vez.
- Al arrancar el bot descarga velas cerradas (`--skip-history` lo evita) y aplica la retención de
  90 días / 2000 líneas de log sobre el StateStore.
- El dashboard escucha solo en `127.0.0.1:8787`. `--check` lista los endpoints sin abrir puerto,
  útil tras tocar config.
- Sin `DASHBOARD_TOKEN`, la API de comandos responde **401**: los botones existen pero no
  escriben. Es la postura segura.
- **Editar el YAML no reconfigura nada vivo.** Ni el bot ni el dashboard releen la configuración:
  hay que reiniciar los dos procesos. Y el detalle que engaña: el encabezado del panel pinta
  `ventanas …` desde la config que cargó **el dashboard** (`api.py`, `config.sessions.entries`),
  mientras el `config_hash` viene del estado que escribe el bot — así, si solo reinicias el bot, el
  JSON del bot ya es nuevo pero el panel sigue mostrando la ventana vieja. Reinicia ambos y
  comprueba que las dos cosas cuadran.
- Ventana de entradas desplegada (2026-11-01): `sessions.entries: ["00:15-20:00"]` en hora del
  servidor, para operar **desde la apertura dominical**. La apertura del oro cae en ~00:00 del
  servidor (22:00 UTC con GMT+2, 01:00 con GMT+3) y `avoid_rollover` corta 23:45–00:15, de ahí el
  inicio en 00:15 en vez de 00:00: lo primero que vería el bot sería el gap de apertura con spread
  ancho, y el filtro de spread (`max_spread_multiplier`) lo rechazaría igualmente.

## 3. Comprobaciones del primer día (una vez, antes de irte)

1. Cabecera del dashboard: `DEMO · Pepperstone-Demo · 61623438` y estado `OPERATIVO`. **Si el
   banner rojo de "no es demo" aparece, para el bot inmediatamente.**
2. Chips: `feed OK`, spread dentro del umbral, sesión correcta para la hora del servidor.
3. Panel *Cerebro*: régimen con probabilidad y confianza coherentes, playbook elegido y el
   rationale legible. Con confianza < `regime.confidence_min` (0,6) el régimen es `UNKNOWN` y no
   se opera.
4. `logs/decisions.jsonl` crece: una línea por vela de decisión cerrada (M5). Si no crece, el bot
   está mirando velas sin cerrar el ciclo.
5. `logs/bot.jsonl` sin `ERROR` y con el bucle avanzando; en el feed de eventos del dashboard deben
   verse los `STARTUP` (uno por arranque) y las revisiones `REVIEW` de cada ciclo.
6. En las primeras dos horas hay **una de las dos cosas**: una entrada (evento `ENTRY` con ticket,
   SL y TP) o una abstención con motivo legible (el DecisionLog registra `NO_SETUP`, `BLOCKED` o
   `NO_TRADE` con su razón). Si no hay ninguna de las dos, el bot no está decidiendo:
   revisa `logs/bot.jsonl` y el estado del feed antes de dejarlo corriendo solo.

## 4. Qué mirar cada día (5 minutos)

- **Tarjetas KPI**: equity y PnL del día. Con el PnL diario en −2 % del equity del día
  (`risk.daily_loss_pct`) el bot no abre nada más hoy y el candado queda latcheado en la base; la
  cabecera del dashboard pasa a `DAILY_LOCK` (y a `KILLED` si lo que saltó fue el kill switch).
- **Candados** (§7 los audita en serio): nunca más de 2 entradas en el día, toda posición con SL
  desde el primer segundo, riesgo real **≤** 1 % del equity.
- **El riesgo real no va a ser 1 %, y eso es correcto.** Con ~990 USD, `risk_pct: 1.0` son 9,90 USD,
  el XAUUSD mueve 1 USD por dólar de recorrido con el lote mínimo 0,01, y el lote se redondea
  **siempre hacia abajo** al `volume_step`:

  | stop | lote | riesgo real |
  |---|---|---|
  | 4,4 USD (ATR M5 ≈ 4,40, `range_fade`) | 0.01 | 0,44 % |
  | 6,6 USD (`atr_sl_mult 1.5`) | 0.01 | 0,67 % |
  | 2,0 USD | 0.04 | 0,81 % |
  | > ~9,9 USD | — | **rechazado**: el lote mínimo no cabe en el 1 % |

  Dos cosas que verás y no son bugs: entradas a 0,4–0,7 % en vez de 1 %, y candidatos rechazados por
  el RiskManager cuando el ATR se dispara (noticias). El techo del 1 % nunca se rompe; lo que pasa es
  que con este capital la granularidad del lote no deja llegar a él. Si algún día el riesgo real
  supera el 1 %, eso sí es `FALLO RIESGO_POR_OPERACION` y hay que parar.
- **Huecos del feed**: si el panel de equity aparece atrasado ("hace 3 min") el enlace se cayó; el
  bot reintenta con backoff y **no abre nada nuevo** hasta recuperar datos fiables. Eso es
  correcto, no lo "arregles" reiniciando cada dos minutos. Dos matices vistos en vivo el
  2026-11-02: la foto de equity la escribe el sondeo de datos (`poll_once` sin `--execute`,
  `refresh_bars` con él), así que si el gráfico lleva horas clavado en un número mientras
  `bot_state` sigue avanzando no era el enlace, era que ese sondeo no escribía la fila; y el
  upsert es uno por minuto, no por poll, así que "hace 40 s" es sano y no un hueco.
- **Cada chip, su cadencia** (§13.3 del diseño): un panel puede estar perfecto y salir en ámbar si se
  mide con el reloj equivocado —fallo visto en vivo el 2026-11-02 y el 2026-11-03—. `dashboard.stale_after_s` (15 s) es
  el reloj de lo que se escribe en cada poll (`bot_state`, `account`, `health`); `decision`, `context`,
  la barra de PnL por día y el panel de posiciones se juzgan con la cadencia del marco de decisión
  (`data.stale_after_s.M5` = 360 s) porque solo se reescriben con vela cerrada; y la curva de equity se
  juzga con `dashboard.equity_stale_after_s` (120 s) porque su upsert es una foto por minuto. Las
  posiciones van con la vela porque su marca no es la fila: `positions.updated_at_utc` solo avanza
  cuando la gestión actúa, así que una operación viva que no necesita acción la deja congelada horas y
  el chip se ambaraba a los 60 s con el bot perfecto. Lo que se vigila es el ciclo que concilia con el
  broker —en eso la lista vacía y la viva son el mismo bloque—, y con el bot plano el chip dice
  `sin posiciones · hace X` en verde. Lo que **sí** debe salir ámbar con el mercado cerrado —el corte
  diario del broker, el fin de semana— son `decision`, `context`, `positions` y `feed_status`: no hay
  vela nueva que cerrar. La antigüedad de la foto de cada operación sigue viéndose por fila
  (`photo_age_s`) y es informativa: no es un fallo.
- **Chip `entradas` en rojo**: la puerta de entradas la cierran exactamente dos cosas, el marco de
  decisión (M5) sin vela cerrada dentro de `data.stale_after_s.M5` (360 s) o un tick más viejo que
  `data.max_feed_age_s` (120 s). El bloque `feed_freshness` del panel lista también H1/H4/D1: esos son
  **contexto**, se muestran y se auditan, pero no bloquean. No es un compromiso de seguridad: el
  precio de entrada sale del tick y el stop se fija en el broker en el mismo envío. Lo que gana es
  que el lunes, cuando el daily todavía no ha cerrado ninguna vela de la semana, el bot puede operar
  con M5 fresco en lugar de quedarse bloqueado hasta la medianoche del servidor. El motor aplica el
  mismo criterio antes de enviar la orden (`guards.assert_feed_fresh`), así que el chip y lo que hace
  el bot son la misma cuenta: si aparece `feed de datos STALE` con el mercado abierto, mira la edad
  del M5 y la del tick en `feed_freshness`; ahí sí es un problema de enlace.
- **Reconstrucción de posiciones**: si ves `RECONCILE` en eventos, el broker cerró algo que el bot
  no esperaba (manual, SL del broker, apagón). Comprueba el PnL y el motivo.
- **El número de operaciones NO es el KPI**. El objetivo es 0, 1 o 2. Un día sin operaciones con
  un rationale coherente vale más que dos operaciones forzadas.

## 5. Cuándo pausar y cómo

`Pausa` (botón del dashboard, requiere token) **solo bloquea nuevas entradas**: las posiciones
abiertas siguen gestionándose (breakeven, parciales, trailing, time stop). Es lo que quieres si
llega un evento de riesgo y no quieres abrir más, o si vas a tocar el terminal.

Pausa o para el bot (`Ctrl+C` en su consola) cuando:

- el terminal se desconecta y no recupera en un par de minutos; las **tres** lecturas seguidas sin
  enlace (`safety.disconnect_tolerance_polls`, ~15 s con el poll de 5 s) son las que latchean: un
  fallo suelto no bloquea el día;
- aparece un `KILL_SWITCH` (desconexión del terminal, errores de ejecución repetidos, equity por
  debajo del suelo de 800 USD, margen libre por debajo del `safety.min_free_margin_pct` del equity o
  el marco de decisión en `MISSING` más de `safety.missing_grace_s`). **Desde 2026-11-02 el latcheo
  se escribe como evento `KILL_SWITCH` de nivel `ERROR` con el motivo y el momento**, una sola vez
  (no en cada ciclo), así que la causa está en la tabla `events` y en el panel de eventos aunque el
  proceso se reinicie; queda
  latcheado y **no se rearma solo**. Primero se entiende la causa en `logs/bot.jsonl` y en el panel
  de eventos. Con el candado latcheado la cabecera muestra el botón **`Rearmar kill switch`** (con el
  motivo en el tooltip), que exige un segundo clic igual que `Cerrar todo` y envía
  `reset_kill_switch` con el token. Si prefieres la API, o el panel está inaccesible:

  ```bash
  curl -X POST http://127.0.0.1:8787/api/command \
    -H "Content-Type: application/json" -H "X-Dashboard-Token: $DASHBOARD_TOKEN" \
    -d '{"command": "reset_kill_switch", "idempotency_key": "rearm-2026-11-02"}'
  ```

  El botón sólo aparece con el latch activo; con el candado suelto está oculto, porque rearmar sin
  nada que rearmar no es una acción, es ruido. El rearme queda registrado como `KILL_RESET` con su
  origen y **no caduca**: si el bot estaba caído cuando se pulsó, se aplica al recuperar.
- el calendario deja de estar fresco y aun así ves entradas (`ENTRY`) en el log: contradice el
  filtro de noticias;
- el PnL flotante de una posición supera lo que el plan admite (una posición a −1 R es el diseño;
  una a −3 R es otro mundo).

`Cerrar todo` sí cierra posiciones en el broker, con doble confirmación y TTL de 120 s: si el bot
no la lee a tiempo, la orden caduca y se registra. **Nunca** abre operaciones ni toca parámetros
de riesgo. Está ejercitada contra el broker real el 2026-11-02: en cola → ejecutada en 2,78 s,
posición cerrada y deal conciliado en el cierre de la vela siguiente (§9).

## 6. Detener limpio

`Ctrl+C` en la consola del bot: cierra el bucle, escribe `SHUTDOWN` y deja el SQLite en WAL
correctamente checkpointado. No mates el proceso con el terminal MT5 todavía conectado: la última
posición puede quedar sin conciliar hasta el arranque siguiente, que la reconcilia por deals
(`RECONCILE`), pero es mejor no provocar eso.

## 7. Revisión semanal (obligatoria, es la Fase 7)

```bash
# informe de los últimos 7 días sobre el StateStore, en solo lectura
python scripts/weekly_review.py --config config/config.example.yaml

# ventana distinta, JSON para comparar semana a semana, y semana ya cerrada
python scripts/weekly_review.py --config config/config.example.yaml --days 14 \
  --json reports/revision_14d.json
python scripts/weekly_review.py --config config/config.example.yaml \
  --until 2026-11-01T21:00:00Z --days 7
```

`--until` corta la ventana en esa marca UTC (por defecto, ahora): sirve para repasar una semana
cerrada sin reejecutar nada. Códigos de salida: **0** sin fallos duros, **2** con al menos un
`FALLO` de auditoría, **3** si no hay base que revisar, **4** si los argumentos no valen. Así se
puede automatizar (Task Scheduler) y mirar solo los días que salen distintos de cero.

Qué hace y qué no:

- Lee el SQLite con `mode=ro`. **No puede escribir en la base del bot**, así que se puede ejecutar
  con el bot funcionando.
- Agrega por día operativo (hora del servidor), no por día UTC: decisiones, entradas, bloqueos por
  filtro, operaciones cerradas, PnL realizado y si el candado diario se activó.
- Reutiliza las métricas del backtest sobre las operaciones cerradas de la ventana: expectancy con
  su `t` (que se pinta `n/a` si las R salen todas del mismo stop, porque un `t` gigante en ese caso
  es mentira), profit factor, drawdown, distribución de R, desglose por motivo de salida y quién lo
  registró.
- Audita los 14 candados: `DEMO`, `MAX_ENTRADAS_DIA`, `SL_OBLIGATORIO`, `RIESGO_POR_OPERACION`,
  `CANDADO_DIARIO`, `EQUITY_FLOOR`, `MARGEN_LIBRE`, `HUECOS_FEED`, `RECONEXIONES`, `ERRORES`,
  `RETCODES`, `KILL_SWITCH`, `DECISIONES_DUPLICADAS`, `REGISTRO`. Los umbrales se leen del YAML
  vigente, no de un valor recordado.
- Los huecos del feed que caen en fin de semana se toleran y se dicen como tales; un hueco ≥ 30 min
  en día de mercado es `FALLO`.
- **La ausencia de operar es un resultado.** `sin operar 100 %` con motivo de filtro coherente es
  una semana correcta; la revisión lo dice sin maquillar.

Cómo leer los hallazgos:

| Hallazgo | Qué hacer |
|---|---|
| `FALLO DEMO` | parar el bot ya; algo se conectó a una cuenta que no es demo |
| `FALLO MAX_ENTRADAS_DIA` | el candado de 2/día no se aplicó: abrir el DecisionLog de ese día y buscar el candado roto antes de seguir |
| `FALLO SL_OBLIGATORIO` | posición sin stop registrada: comprobar en el terminal y en `order_attempts` |
| `FALLO RIESGO_POR_OPERACION` | el riesgo real superó el 1 %: revisar `risk_json` y el redondeo de lote |
| `FALLO MARGEN_LIBRE` | se entrou con el margen libre bajo `safety.min_free_margin_pct`: el bot debió latchear; revisar `risk_json` de esa entrada y el tamaño de lote |
| `WARN MARGEN_LIBRE` | o bien hubo margen bajo y no entró (el candado hizo su trabajo), o bien ningún snapshot trae `margin_free` y no se puede auditar |
| `FALLO HUECOS_FEED` | caída del enlace no explicada por fin de semana: ver `bot.jsonl` y el backoff |
| `FALLO KILL_SWITCH` (entradas después del kill) | el latch no cortó: es el hallazgo más grave del lote |
| `WARN RIESGO_POR_OPERACION` | no hubo entradas que auditar: informativo, en semanas sin operar es normal |
| `WARN DECISIONES_DUPLICADAS` | reinicios/`--once` sobre la misma vela: mirar cuántos `STARTUP` hubo |
| `WARN CANDADO_DIARIO` | se alcanzó la pérdida diaria: comprobar que después no hubo entradas |

## 8. Registro que conviene llevar aparte del log

Una línea por semana en `docs/revisiones.md` (o donde quieras): fecha, número de operaciones,
PnL neto, hallazgos `FALLO`/`WARN`, y **qué cambiaste y por qué** (o por qué no cambiaste nada).
El DecisionLog te dice qué hizo el bot; este registro te dice qué hiciste tú. Sin eso, a las seis
semanas no se puede distinguir una estrategia que no funciona de una que nadie llegó a revisar.

## 9. Prueba de la ruta de orden (solo con mercado abierto)

Hay un tramo que el simulador no cubre: el marshalling real de `MqlTradeRequest`, el llenado IOC
y que el broker devuelva la posición con el SL/TP que crees que enviaste. Se ejercita con
`scripts/live_round_trip.py`, que fabrica una señal (**no** es una señal de la estrategia) y la
manda por la misma ruta de producción (`ExecutionEngine` → `Mt5BrokerAdapter.place_order`).

```bash
# 1) dry run: lee specs, calcula lote, SL/TP y riesgo; no toca el mercado
.venv/Scripts/python.exe -X utf8 scripts/live_round_trip.py --config config/config.example.yaml

# 2) orden real de 0,01 lote (~5-6 USD de riesgo) con SL y TP en el mismo envío
.venv/Scripts/python.exe -X utf8 scripts/live_round_trip.py --config config/config.example.yaml --atr-mult 1.3 --send
```

Conviene `--atr-mult 1.3`: con el ATR bajo el RiskManager sube a 0,02 lote y el techo de la
prueba (`--max-lots 0.01`) lo rechaza — ese rechazo es el comportamiento correcto, no un fallo
del script. El lote **no** lo baja la prueba a la fuerza: lo recalcula `ExecutionEngine` con el
`RiskManager`, así que `--max-lots` es solo una puerta de aborto y la única palanca real del
riesgo es el ancho del stop (`--atr-mult`). No existe `--force-lots`: la knob imprimía un lote
que nunca llegaba al broker.

Para probar **gestión** (breakeven, parcial, trailing) hacen falta dos cosas que la prueba de humo
de arriba no da: que la posición corra ≥1 R sin que el TP la cierre antes, y poder apilar una
segunda pata. Con `--rr 6` u `--rr 8` el objetivo queda lejos y manda la gestión; con un
`--atr-mult` pequeño el riesgo por punto es estrecho (0,05 lote con stop de 1,67 USD) y basta un
movimiento de ~2 USD en XAUUSD para ver las ramas:

```bash
# pata 1 y pata 2 (lados opuestos); --allow-stack es lo que permite la segunda encima
.venv/Scripts/python.exe -X utf8 scripts/live_round_trip.py --config config/config.example.yaml \
  --side buy --atr-mult 0.5 --rr 8 --max-lots 0.05 --allow-stack --send
.venv/Scripts/python.exe -X utf8 scripts/live_round_trip.py --config config/config.example.yaml \
  --side sell --atr-mult 0.5 --rr 8 --max-lots 0.05 --allow-stack --send
```

Dos patas a ~1 % son ~2 % de riesgo **abierto** a la vez (el spec no lo prohíbe; el tope del 2 % es
de pérdida realizada del día), y el bot no abre solo una segunda entrada — sólo el script, con
esta bandera explícita. Lo demás es observación: `positions` y `events` se rellenan en los cierres
de vela M5, y el `Cerrar todo` del panel con las dos vivas se concilia en la vela siguiente.

Qué tiene que salir:

- `placed=True retcode=10009 (DONE)` y la línea `posición <ticket> … SL … TP …`, con el SL/TP que
  devolvió `positions_get` idéntico al enviado.
- La posición queda registrada en `positions` con su `decision_id_entry`. **Sin esa fila el bot
  gestiona la posición pero nunca la concilia**: `_reconcile_closed` solo cierra el ledger sobre
  las que conoce. Si se hiciera la prueba a mano desde el terminal, o el deal no aparecerá en
  `trades`.
- Después, `Cerrar todo` desde el panel (§5): el bot cierra con comment `goldbot close` y la
  fila en `trades` + `daily_stats` aparece **al cerrar la siguiente vela M5**, con su evento
  `RECONCILE`. No es inmediato a propósito: la conciliación va en el ciclo de vela.

```bash
sqlite3 data/state.db "SELECT position_id, close_deal_id, net_profit, exit_reason, reconciled_at_utc
                       FROM trades ORDER BY rowid DESC LIMIT 3;"
```

Las filas que deja la prueba **no se borran**: son parte del rastro de auditoría. Se reconocen
porque su `decision_id_entry` empieza por `smoke-` y, si salieron del script, el `playbook_id`
del `trades` vale `live_round_trip` o vacío. La revisión semanal las cuenta como operaciones del
día, así que al leer el PnL de una semana con pruebas ten presente cuánto es estrategia y cuánto
es smoke test. Su PnL sí computa contra el techo de −2 % del día (el `entries_used` no, porque lo
alimenta la decisión del bot, no el broker).

| Síntoma | Causa real (corregidas el 2026-11-02, `docs/fase0_diseno.md` §12.10 y §12.11) |
|---|---|
| `order_send` devuelve `None` → 10012 `NO_RESULT_FROM_TERMINAL` con latencia 0 ms | el `comment` supera los **29 caracteres** que acepta el paquete Python: rechaza la petición localmente (`last_error = (-2, 'Invalid "comment" argument')`). El script imprime `raw del terminal` con ese error y el request exacto |
| el bot deja de decidir con una posición abierta y lo reporta como caída del enlace | `AttributeError` dentro de `_read_positions`: esta build no expone `commission` ni `position` en `TradePosition` |
| `trades` no recibe nunca un deal propio | `history_deals_get(fecha, fecha)` devuelve vacío en builds recientes; hay que consultar por `position` |
| un time stop que cierra a los pocos minutos de abrir | `_bars_since_open` mezclaba unidades (tabla de minutos sobre segundos) |
| tras el breakeven, el R de una operación viva se lee 20-30 y el bot cierra un parcial "porque corrió 1,5 R" | el adaptador re-derivaba `initial_risk_price` como `abs(sl − entry)`, y al llevar el SL al breakeven ese divisor se hunde (3,66 USD → 0,26). El riesgo se congela en la fila `positions` que se escribe al abrir |
| una posición pierde el TP poco después de que el bot le mueva el SL | `TRADE_ACTION_SLTP` aplica el **par** sl/tp: el lado omitido viaja como 0 y el broker lo lee como "borrar". El adaptador rellena el lado ausente con el valor vivo; si no puede leer la posición, aborta el movimiento |
| un `TRADE_ACTION_SLTP` devuelve `10025 NO_CHANGES` y el bot avisa que no pudo mover el SL | el terminal rechaza una petición idéntica a lo ya vigente. Es benigno (categoría `NONE`, `filled=False`, no cambia estado), pero conviene saber que no es una caída del enlace |

## 10. Lo que esta operación todavía no puede dar por bueno

- ~~Ningún llenado real de orden se ha confrontado con el broker~~ → resuelto el 2026-11-02 con
  dos operaciones reales de 0,01 lote (§9): IOC 10009 DONE, SL/TP verificados en el broker y
  deals conciliados en `trades`/`daily_stats`.
- ~~`Cerrar todo` contra el broker real no se ha ejercitado~~ → resuelto el 2026-11-02: comando
  en cola desde el panel, ejecutado en 2,78 s, posición cerrada y conciliada. **Y con dos
  posiciones vivas a la vez** (§9 y diseño §12.11): 3,4 s de punta a punta y las dos patas
  conciliadas en la vela M5 siguiente, con su `daily_stats`.
- **La estrategia sigue sin haber entrado en vivo**: todas las operaciones de las pruebas las
  fabricó el script de ruta de orden. Falta la primera `ENTRY` disparada por un playbook.
- ~~break-even, parcial y trailing sobre una posición viva~~ → ejercitados el 2026-11-02 sobre
  operaciones reales de demo (§9; diseño §12.11), y ahí salieron los dos defectos que solo se ven
  con órdenes vivas: el divisor de R re-derivado del SL y el TP que `TRADE_ACTION_SLTP` borraba.
- ~~**El R que pinta `trades` está inflado en las operaciones con parcial cerrado**: el neto de todos
  los deals se divide por el riesgo del volumen que queda al final, no por el riesgo inicial~~ →
  arreglado el 2026-11-02 sin tocar el esquema: el divisor usa ahora el volumen del deal `IN`, que
  el histórico del broker sí conserva, y `trades.volume` pasa a ser el lote de entrada en vez del
  restante. El neto en USD ya era correcto, así que el PnL y el candado del −2 % nunca se vieron
  afectados; lo que estaba inflado era la distribución de R del panel y de la revisión semanal, y
  solo en las operaciones que llegaron a parcial. **Las dos filas escritas antes del arreglo se
  quedan como están** (diseño §12.11): 414168566 aparece con 2,69 R cuando eran 1,61, y 414064025
  arrastra además el divisor re-derivado del SL, así que su R honesto ya no es recuperable. El
  proceso que está vivo ahora mismo carga el código anterior en memoria: hay que reiniciar el bot
  para que la próxima conciliación use el divisor nuevo.
- **Nadie ha visto un TP llenándose en el broker**, ni un SL saltando mientras el bot estaba caído,
  ni el kill switch latcheándose en vivo, ni el cierre por régimen en contra: son las ramas
  que faltan y solo las da una semana de operación continua.
- ~~El panel no tenía botón de rearme del kill switch~~ → resuelto el 2026-11-01: la cabecera ofrece
  `Rearmar kill switch` sólo con el candado latcheado, con doble clic y token (§5). Queda pendiente
  ejercitarlo contra un kill real, que es justo lo que el primer `--execute` puede producir.
- **La cobertura del calendario** manda: si el CSV no llega al tramo que se revisa, la semana
  entera es "no operé por falta de datos", no "no operé por criterio". En vivo eso lo cubre
  `calendar_events.csv` (30 días atrás + 14 adelante, refresco de 60 s); el histórico profundo es
  solo para validar (`docs/fase0_diseno.md` §17).
- Con 1–4 operaciones semanales **no hay base estadística para nada**. La revisión semanal sirve
  para detectar candados rotos y enlaces caídos; la ventaja, si existe, solo la dice un
  walk-forward con calendario completo —y ese ya está corrido (§14.10): 117 operaciones, −0,199 R,
  t = −1,40, **sin ventaja demostrada**. Lo que la semana operativa puede aportar ahora no es
  estadística, es confirmar que el bot real hace lo que el backtest simula.
