# Puente de calendario económico (MQL5 → CSV)

La API Python de `MetaTrader5` **no** expone el calendario económico (`mt5.calendar_*`
no existe). El calendario nativo solo está disponible en MQL5, así que un EA del
terminal lo exporta a CSV y el bot lo lee desde la carpeta común.

## Instalación

1. Abrir el terminal y `Archivo → Abrir carpeta de datos`: el nombre de esa carpeta es un
   identificador **distinto en cada instalación**, no lo copies de otra máquina. Copiar
   `GoldbotCalendarExport.mq5` a `<carpeta de datos>\MQL5\Experts\goldbot\`
   (o compilar desde MetaEditor, que deja el `.ex5` junto al `.mq5`).
2. En MetaTrader 5: `Herramientas → Opciones → Expert Advisors` → marcar
   **Permitir trading algorítmico** (necesario para que los EAs se ejecuten).
3. Abrir cualquier gráfico y arrastrar `goldbot → GoldbotCalendarExport` desde el
   Navegador. No requiere símbolo ni timeframe concretos.
4. En ~1 minuto aparece el CSV:
   `%APPDATA%\MetaQuotes\Terminal\Common\Files\goldbot\calendar_events.csv`

El EA no envía órdenes ni modifica la cuenta: solo lee el calendario y escribe CSV.

## Salida

- Delimitador `;`, UTF-8 sin BOM, cabecera fija.
- Publicación atómica: escribe `.tmp` y lo renombra; si falla, el CSV anterior queda
  intacto y el bot lo verá atrasado (política `block`).
- CSV en vivo (`calendar_events.csv`): ventana de `InpDaysBack` días atrás y
  `InpDaysForward` adelante, reexportada cada `InpTimerSeconds`.
- Las peticiones al calendario van **troceadas** (`InpChunkDays`): una ventana ancha
  devuelve `ERR_CALENDAR_TIMEOUT` (5401), así que el trozo que falla se parte por la
  mitad hasta tres niveles. Los trozos se piden **de lo más reciente a lo más
  antiguo** con un presupuesto de tiempo (`InpMaxExportSeconds`): si se agota, lo
  publicado sigue cubriendo la franja que usa el filtro de noticias en vivo.

## Histórico profundo (`calendar_history.csv`)

El mismo EA publica un segundo CSV, `calendar_history.csv`, con `InpHistoryDaysBack`
días hacia atrás y refresco lento (`InpHistoryRefreshMinutes`). Es un fichero aparte a
propósito: un pull profundo puede tardar o quedar cortado y no debe afectar nunca a la
frescura del CSV que lee el bot. Solo se exporta si
`InpHistoryDaysBack > InpDaysBack`; comparte esquema y columnas con el CSV en vivo.

| Input | Defecto | Para qué |
|---|---|---|
| `InpDaysBack` | 30 | ventana del CSV en vivo |
| `InpDaysForward` | 14 | eventos futuros |
| `InpTimerSeconds` | 60 | refresco del CSV en vivo |
| `InpChunkDays` | 20 | días por petición al calendario |
| `InpMaxExportSeconds` | 25 | presupuesto de tiempo por exportación |
| `InpHistoryEnabled` | `true` | publica o no el histórico |
| `InpHistoryDaysBack` | 730 | profundidad del histórico |
| `InpHistoryRefreshMinutes` | 60 | refresco del histórico |

**La retención del calendario la decide el broker, no este EA.** El alcance real se ve
en el log del terminal (línea `goldbot: calendar_history.csv N eventos (fecha -> fecha)
trozos ok=… vacios=… fallidos=…`) y en la salida de `scripts/build_calendar_cache.py`.
Es normal que las fechas antiguas devuelvan cero eventos: si el histórico sale corto,
los pliegues del walk-forward fuera de esa cobertura quedan `DEGRADED` y se dice, no se
disfraza.

### Columnas

| Columna | Significado |
|---|---|
| `schema_version` | 1 |
| `value_id` / `event_id` | ids de la ocurrencia y del tipo de evento |
| `event_time_server_epoch` | hora del evento en hora del servidor (epoch) |
| `event_time_utc_epoch` | hora del evento en UTC (epoch) |
| `export_gmt_epoch` / `export_server_epoch` | relojes en el momento de exportar |
| `server_offset_s` | `server - GMT` en segundos (7200 en invierno, 10800 en verano) |
| `currency` | divisa del país del evento (`USD`, `EUR`…) |
| `impact` / `impact_name` | 0 none, 1 low, 2 moderate, 3 high |
| `event_type` / `event_type_name` | 0 event, 1 indicator, 2 holiday |
| `event_name` / `event_code` | nombre y código MQL5 |
| `country_code` | ISO del país (`US`, `EU`…) |
| `actual_raw` / `prev_raw` / `forecast_raw` | valor escalado por 1e6; `-9223372036854775808` = sin dato |
| `digits` / `unit` | decimales y unidad del indicador |
| `period_server_epoch` | periodo de referencia del dato |

`event_id` identifica el **tipo** de evento y se repite cada mes (en un CSV de 1 441
filas hay 864 `event_id` distintos): la identidad de la ocurrencia es `value_id`, y por
ella deduplican las uniones y cachés del lado Python.

## Lectura desde Python

`goldbot.infra.calendar.CsvBridgeEconomicCalendar` valida:

- existencia del fichero y antigüedad (`calendar_max_age_s`, 180 s por defecto);
- `schema_version` y cabeceras;
- offset del servidor coherente (~7200/10800 s).

Si el CSV no supera la validación, el filtro de noticias devuelve
`CalendarHealth.UNAVAILABLE` y se aplica `calendar_unavailable_policy`
(`block` por defecto ⇒ no se opera).

## Backtest

`load_calendar_events_multi` une el CSV en vivo y el histórico por ocurrencia, y
`scripts/build_calendar_cache.py` congela esa unión en
`data/cache/calendar_snapshot.csv` (mismo dialecto que el puente, así
`backtest --calendar data/cache/calendar_snapshot.csv` lo lee tal cual). El snapshot es
lo que hace reproducible un walk-forward: el CSV del puente cambia cada minuto.

**No existe caché en SQLite.** La tabla `calendar_events` se crea con el esquema pero
ningún código la escribe; la persistencia del calendario es el CSV puente y el snapshot
congelado.
