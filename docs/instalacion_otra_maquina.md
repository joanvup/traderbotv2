# Instalación de goldbot en una máquina Windows nueva

Manual paso a paso para dejar el bot **operativo en DEMO** en un equipo distinto al de
desarrollo. Cubre desde el clon hasta el primer día mirando el panel.

Qué **no** es este documento: el runbook de cada día (arranque, pausa, kill switch, revisión
semanal) está en [`docs/operacion_demo.md`](operacion_demo.md) y lo que hace cada fase está en el
[`README.md`](../README.md). Aquí solo la puesta a punto en hardware nuevo.

Tiempo real de una instalación limpia: **40–60 min**, de los cuales 15 son descargar el terminal
de Pepperstone. Los pasos 4, 5 y 9 son los que se saltan la gente y los que dejan el bot
inútil sin que nadie lo note.

---

## 0. Qué viaja en el repo y qué hay que rehacer en la máquina nueva

`.gitignore` deja fuera todo lo que es secreto, estado o derivable. No es un olvido: si algo de
esta tabla aparece en el clon, hay que borrarlo del historial.

| No viaja | Por qué | Cómo se recupera en la máquina nueva |
|---|---|---|
| `.env` | credenciales de la cuenta y token del dashboard | paso 6 (desde `.env.example`) |
| `.venv/` | entorno local | paso 3 (`pip install -e ".[dev]"`) |
| `data/state.db` | ledger del bot (decisiones, trades, eventos) | se crea vacío en el primer arranque |
| `data/cache/XAUUSD/*.csv.gz` | velas descargadas | paso 8 (`sync_history.py`, las pide al servidor) |
| `data/cache/calendar_snapshot.csv` | foto del calendario para walk-forward | `scripts/build_calendar_cache.py` cuando haga falta |
| `logs/`, `reports/` | artefactos locales | se crean solos al escribir |
| `mt5/GoldbotCalendarExport.ex5` | binario MQL5 compilado | **paso 5** (compilar el `.mq5`, que sí viaja) |

El modelo de régimen no se copia: no hay `.joblib` versionado. Se reentrena al arrancar con las
velas que se descarguen en el paso 8.

---

## 1. Requisitos del equipo

| Requisito | Detalle |
|---|---|
| Windows 10 o 11, **64 bits** | el paquete `MetaTrader5` solo publica wheels `win_amd64` |
| Python 3.11 | `pyproject.toml` pide `>=3.11,<3.13`; probado con **3.11.9**. No instales 3.13: el instalador se niega |
| Git for Windows | para el clon; el resto de comandos están en PowerShell |
| Terminal MetaTrader 5 de **Pepperstone** | build probada: 6295 |
| Cuenta **DEMO** del mismo broker | el bot rechaza cuentas que no son demo (`safety.require_demo`) |
| ~1 GB libres | el venv con numpy, pandas y hmmlearn ocupa eso; la caché de velas es pequeña |

Al instalar Python, marca **"Add python.exe to PATH"** y elige **"Install for all users"** o
solo tu usuario, da igual; lo que no puede cambiar es la versión: el bot y los 290 tests corren
sobre 3.11.

Comprobación:

```powershell
python --version    # debe decir Python 3.11.x
```

---

## 2. Clonar el proyecto

El repo es privado, así que el clon necesita autenticación (PAT o clave SSH ya dada de alta en
GitHub).

```powershell
cd $HOME
git clone https://github.com/joanvup/traderbotv2.git goldbot
cd $HOME\goldbot
git log --oneline -1
```

**Siempre en la raíz del proyecto a partir de aquí.** No es una manía: `app.db_path`
(`data/state.db`), `app.data_dir`, `app.log_dir` y el `.env` se resuelven contra el directorio
de trabajo, no contra la ubicación del script. Desde otra carpeta el bot levanta una base vacía
en ese otro sitio y el dashboard se queda en "sin dato" mirando la que no es.

Detalle de la primera ejecución en Windows: si el clon te trae los `.py` con saltos de línea
`CRLF` y algo extraño con las tildes, fijar `git config core.autocrlf false` y volver a clonar. El
repo todavía no lleva `.gitattributes`.

---

## 3. Entorno virtual y dependencias

```powershell
cd $HOME\goldbot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Se usa la ruta directa al intérprete en vez de `Activate.ps1`. No es pereza: la directiva de
ejecución de PowerShell suele venir en `Restricted` y el `Activate.ps1` falla con un mensaje de
política que no tiene nada que ver con este proyecto. Si prefieres activar, es
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` y luego `.\.venv\Scripts\Activate.ps1`.

Comprobación de que el conector MT5 está (esto es lo único que no funciona en Linux):

```powershell
.\.venv\Scripts\python.exe -m pip show MetaTrader5
```

Salida esperada: `Name: metatrader5`, `Version: 5.0.6xxx`. Si `pip` intenta compilar o dice
`ERROR: Could not find a version that satisfies the requirement MetaTrader5`, el Python que
estás usando no es 3.11 de 64 bits.

---

## 4. Terminal MT5 y sesión DEMO

1. Descargar el terminal desde Pepperstone e instalarlo con las opciones por defecto.
2. `Archivo → Conectarse a cuenta de trading` → **demo** → servidor y login de la cuenta
   (en esta configuración: `Pepperstone-Demo`, cuenta `61623438`).
3. `Herramientas → Opciones → Asesores Expertos` → marcar **Permitir trading algorítmico**.
   Sin esto el guard `assert_algo_trading` aborta el arranque con código 3.
4. `Archivo → Abrir carpeta de datos`. **Anota esa ruta**: es la carpeta con el identificador de
   esta máquina (una sucesión de hex distinto en cada equipo) y la necesitas en el paso 5.
5. Dejar **XAUUSD visible en el Market Watch**: `Vista → Símbolos` (Ctrl+U), buscar `XAUUSD` en el
   grupo del servidor demo y doble clic para que entre en la lista de observación. Si el símbolo
   no está, `symbol_info()` devuelve vacío y el bot no puede leer el tick ni las especificaciones.

El terminal tiene que permanecer **abierto y con sesión iniciada** mientras corra el bot: el
conector Python habla con el proceso del terminal por IPC. No hace falta tener ningún gráfico de
XAUUSD abierto ni ningún otro EA corriendo (salvo el del paso 5).

El bot puede funcionar de dos formas:

- **Sesión del terminal** (por defecto, `mt5.use_terminal_session: true`): no pone credenciales,
  usa la cuenta en la que tú ya entraste por la interfaz. Menos superficie de secretos.
- **Login programático**: rellenas `MT5_LOGIN/PASSWORD/SERVER` en `.env` (paso 6) y el bot inicia
  sesión él.

---

## 5. Puente de calendario (obligatorio para que opere)

La API Python de MT5 no expone el calendario económico; lo exporta un EA del terminal a un CSV.
Con el valor del YAML `filters.calendar_unavailable_policy: block`, **sin ese CSV el bot no abre
ninguna operación** y todo se registra como `BLOCKED` por `NEWS`. Es el comportamiento correcto
(no operar sin datos), no un fallo que se arregla cambiando la política a `allow`.

El `.ex5` no está en el repo (`.gitignore` excluye `mt5/*.ex5`), así que hay que compilarlo:

1. Crear la carpeta `<datos>\MQL5\Experts\goldbot\` dentro de la carpeta de datos del paso 4.
2. Copiar ahí `mt5/GoldbotCalendarExport.mq5`.
3. Abrir MetaEditor (F4 en el terminal), abrir ese `.mq5`, **Compilar (F7)**. Debe salir
   `0 errors`. El `.ex5` queda junto al fuente.
4. Volver al terminal, refrescar el Navegador (clic derecho → Actualizar), abrir **cualquier**
   gráfico y arrastrar `GoldbotCalendarExport`. No importa el símbolo ni el timeframe: el EA solo
   lee el calendario y escribe CSV, nunca envía órdenes.
5. En ~1 minuto aparece `<Common>\Files\goldbot\calendar_events.csv`. La carpeta `Common` es
   **compartida entre terminales**, no depende del identificador de la instalación:
   `%APPDATA%\MetaQuotes\Terminal\Common\Files\goldbot\calendar_events.csv`.

Comprobación con el propio código del proyecto, desde la raíz (usa la misma ruta que consume el
filtro de noticias, así que no puede mentirte):

```powershell
cd $HOME\goldbot
.\.venv\Scripts\python.exe -X utf8 -c "import time; from goldbot.infra.calendar import default_bridge_path; f = default_bridge_path(); print(f); print('existe:', f.exists()); print('edad_s:', round(time.time() - f.stat().st_mtime, 1) if f.exists() else 'sin fichero')"
```

Salida en una instalación ya válida:

```
C:\Users\<usuario>\AppData\Roaming\MetaQuotes\Terminal\Common\Files\goldbot\calendar_events.csv
existe: True
edad_s: 44.2
```

El `edad_s` de esa comprobación debe quedar **por debajo de 180** (`filters.calendar_max_age_s`).
Si sale viejo, el EA no está en el gráfico, el terminal no tiene sesión iniciada, o el calendario
del broker todavía no se ha descargado (en el primer arranque tarda unos minutos; abrir la ventana
del calendario del terminal una vez suele provocar la descarga).

---

## 6. `.env`: credenciales y token

```powershell
cd $HOME\goldbot
Copy-Item .env.example .env
notepad .env
```

```dotenv
# Deja estas tres vacías si vas a usar la sesión abierta del terminal (paso 4).
MT5_LOGIN=61623438
MT5_PASSWORD=<clave de la cuenta demo>
MT5_SERVER=Pepperstone-Demo

# Token del dashboard: se genera una vez y se queda fijo en esta máquina.
DASHBOARD_TOKEN=<32 hex>
```

Para generar el token, uno solo de estos:

```powershell
-join (1..16 | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
```

```bash
openssl rand -hex 16   # si usas Git Bash
```

Dos cosas que conviene fijar aquí y no cada arranque:

- El `DASHBOARD_TOKEN` se **guarda** en `.env` y se reusa. Generarlo de nuevo en cada arranque
  (como muestra el runbook para una prueba rápida) rompe los botones del navegador: el panel
  sigue en pie pero cada comando de escritura responde 401 hasta que pegues el token nuevo en la
  casilla.
- `load_environment()` lee `.env` desde el directorio de trabajo. Si arrancas desde otra carpeta,
  no lo encuentra, y el bot solo lo nota si esperabas login programático o token.

Nunca pongas credenciales en el YAML ni en la base de datos. `.env` está en `.gitignore`;
compruébalo antes de hacer ningún commit con `git status`.

---

## 7. Repasar la configuración

`--config` es obligatorio en todos los scripts y no tiene valor por defecto; toda la documentación
usa `config/config.example.yaml`, que **es** el fichero de configuración de esta máquina. No hay
que crear un `config.yaml` aparte salvo que quieras mantener dos cuentas o dos riesgos.

Claves que dependen de la máquina o del broker:

| Clave | Valor por defecto | Qué mirar en la máquina nueva |
|---|---|---|
| `app.db_path`, `app.data_dir`, `app.log_dir` | `data/state.db`, `data`, `logs` | relativas: por eso se corre desde la raíz |
| `mt5.terminal_path` | `null` (auto) | si hay **varios** terminales instalados, pon la ruta exacta del `.exe` de Pepperstone |
| `mt5.symbol` / `symbol_override` | `XAUUSD` / `null` | con sufijo de broker (`XAUUSD.a`) el resolver lo encuentra; si no, fija `symbol_override` |
| `app.magic` | `20261030` | **ve el paso 9**: dos máquinas con el mismo magic sobre la misma cuenta se pisan |
| `filters.calendar_bridge_path` | `null` (ruta `%APPDATA%` por defecto) | cámbialo solo si el CSV del EA cae en otro sitio |
| `risk.*`, `safety.*`, `sessions.*` | los del spec | no los toques en el primer arranque; el 1 % sobre ~990 USD ya tiene su aritmética explicada en el runbook §4 |
| `dashboard.host` | `127.0.0.1` | el arranque se niega con código 2 si le pones otra cosa. No es un límite que se salte: el panel no debe ser alcanzable desde la red |

`Section` lleva `extra="forbid"`: si copias un YAML viejo con claves retiradas (por ejemplo
`max_feed_staleness_s`), la carga falla en vez de ignorarlas. Es intencionado; el mensaje dice la
clave.

---

## 8. Verificación antes de tocar el mercado

Cinco comprobaciones, en orden. Ninguna envía órdenes.

```powershell
cd $HOME\goldbot

# 1) imports y config (si la config no carga, nada de lo de abajo tiene sentido)
.\.venv\Scripts\python.exe -X utf8 -c "from goldbot.config import load_config; c = load_config('config/config.example.yaml'); print(c.app.name, c.data.decision_timeframe, c.dashboard.port)"

# 2) suite completa: 290 tests
.\.venv\Scripts\python.exe -m pytest -o addopts="" -q -p no:warnings --tb=line

# 3) dashboard sin abrir puerto
.\.venv\Scripts\python.exe -X utf8 scripts/run_dashboard.py --config config/config.example.yaml --check

# 4) velas al día (crea data/ y la caché)
.\.venv\Scripts\python.exe -X utf8 scripts/sync_history.py --config config/config.example.yaml

# 5) un ciclo en solo lectura: descarga, contexto y régimen
.\.venv\Scripts\python.exe -X utf8 scripts/run_bot.py --config config/config.example.yaml --once
```

Qué debe salir y qué significa si no:

- **2)** `290 passed`. `MetaTrader5` no se importa en los tests (el adaptador es perezoso), así que
  esta línea no necesita terminal.
- **3)** `app construida · base alcanzable=False · endpoints=...` y `return 0`. Con la base todavía
  vacía es normal el `AVISO: la base data/state.db no existe todavía`; el panel pinta "sin datos"
  hasta que el bot escribe. Si aparece `AVISO: falta DASHBOARD_TOKEN`, el `.env` no está en el
  directorio de trabajo.
- **4)** todas las temporalidades con sus `warmup_bars` (M5 600, M15 400, H1 600, H4 400, D1 300).
  Tarda el primer lazo porque el servidor reconstruye el histórico. Si falla con el mercado
  cerrado, inténtalo con el mercado abierto.
- **5)** régimen, sesión y hash del snapshot por consola. Con el mercado cerrado el feed sale
  `STALE` con la edad real del tick: correcto, no es un fallo de instalación.

El paso 5 **no decide**. Sin `--execute` no se construye el `Bot` y el proceso queda en el servicio
de datos (`scripts/run_bot.py:198-204`): velas, régimen, equity y estado, cero filas en
`decisions`. Si quieres ver decisiones en el panel, el arranque tiene que llevar `--execute`.

---

## 9. No corras dos máquinas sobre la misma cuenta con el mismo `magic`

Esto es lo que puede convertir la nueva instalación en un problema en lugar de una mejora.

Si el equipo viejo y el nuevo operan la misma cuenta demo con el mismo `app.magic` (20261030):

- cada bot **reconcilia las posiciones del otro** como si fueran suyas, y los dos intentan
  gestionar la misma posición (SL, parciales, trailing) desde dos procesos que no comparten
  estado;
- los candados por día son **por proceso**: `max_trades_day: 2` pasa a ser hasta 4 entradas y el
  techo de −2 % de pérdida diaria se calcula sobre el equity visto por cada uno;
- el ledger de cada máquina cuenta operaciones que no ve la otra, así que la revisión semanal ya
  no describe la cuenta.

Elige una:

1. **Sustitución** (recomendado): el equipo viejo se apaga para siempre antes de arrancar el
   nuevo. Migra sin posiciones abiertas (paso 12).
2. **Las dos encendidas**: cambia `app.magic` en una de ellas a un número distinto y acepta que
   cada una gestiona solo lo suyo, con riesgo diario independiente. Sigue siendo una cuenta demo
   con un solo colchón de 2 %; el riesgo agregado de la cuenta es la suma.

Ninguna de las dos opciones cambia la regla dura: **una sola instancia con `--execute` por
cuenta** es la postura con la que este código está probado.

---

## 10. Arranque y dejarlo operativo

Dos procesos, dos consolas, ambos desde la raíz del proyecto. El dashboard **nunca** se arranca
dentro del bucle del bot.

```powershell
# consola 1 — bot con ejecución DEMO
cd $HOME\goldbot
.\.venv\Scripts\python.exe -X utf8 scripts/run_bot.py --config config/config.example.yaml --execute
```

Al arrancar imprime `EJECUCIÓN DEMO HABILITADA (Fase 4)`; antes de construir el bot comprueba que
la cuenta es demo y que el algo trading está activo. Si algo falla sale con código **3** y
`EJECUCIÓN RECHAZADA: ...` sin haber tocado el mercado. Ese código 3 es buena señal: los candados
funcionan.

```powershell
# consola 2 — dashboard (lee el token de .env, no lo regeneres aquí)
cd $HOME\goldbot
.\.venv\Scripts\python.exe -X utf8 scripts/run_dashboard.py --config config/config.example.yaml
```

Abre `http://127.0.0.1:8787` en el navegador de **esa** máquina. No está accesible desde otra por
diseño.

### Que sobreviva a un reinicio

Dos cosas, y las dos sin programar el arranque del bot:

1. **Que la máquina no duerma.** Un equipo suspendido no ve velas: al despertar el feed aparece
   atrasado y el bot entra en backoff.

   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```

   En la opción de energía "Apagar la pantalla" deja lo que quieras; lo que no puede pasar es que
   entre sueño o hibernación.

2. **Que alguien mire si el proceso murió.** El 2026-11-02 el bot dejó de escribir a las 11:45 UTC
   y nada lo avisó: lo detectó el panel, porque el chip se puso ámbar. Antes de automatizar el
   arranque, usa la revisión semanal, que está para eso y sale con código distinto de cero:

   ```powershell
   cd $HOME\goldbot
   .\.venv\Scripts\python.exe -X utf8 scripts/weekly_review.py --config config/config.example.yaml
   echo $LASTEXITCODE   # 0 todo bien · 2 hay un FALLO de auditoría · 3 no hay base
   ```

**Arranque automático con Task Scheduler: decisión del operador, no de este manual.** Se puede
programar una tarea "al iniciar sesión" que lance el bot con `--execute` y la carpeta de inicio
puesta en la raíz del proyecto (si no, ver paso 2: base y `.env` en el sitio equivocado). Ten
presente lo que implica: el bot abrirá posiciones sin que nadie esté delante, y si la máquina
entra en sesión bloqueada o el terminal pierde la conexión, el kill switch latchea y el día queda
bloqueado hasta el rearme manual. Con este historial de pruebas, el arranque con una persona
delante es la postura por defecto.

Detalle si programas la tarea o la lanzas en segundo plano: el `python.exe` del venv levanta un
proceso hijo con el intérprete base. Dos PIDs = un proceso; no mates el segundo creyendo que es
un duplicado, porque es el que está decidiendo.

### Qué mirar el primer día (una vez, antes de irte)

El checklist completo está en el runbook §3; lo esencial:

1. Cabecera del panel: `DEMO · Pepperstone-Demo · 61623438` y estado `OPERATIVO`. Si aparece el
   banner rojo de "no es demo", **parar el bot ya**.
2. Chips de feed y mercado en verde, sesión coherente con la hora del servidor.
3. Panel *Cerebro* con una decisión reciente (no "dato atrasado"). Si el chip está ámbar con una
   decisión cada vez más vieja, casi siempre es que el bot se arrancó **sin** `--execute`.
4. `logs/decisions.jsonl` crece: una línea por vela M5 cerrada.
5. En las dos primeras horas: o una `ENTRY` con SL/TP, o una abstención con motivo legible
   (`NO_SETUP` / `BLOCKED` / `NO_TRADE`). **Un día sin operaciones es un resultado válido**, no una
   instalación fallida; lo que no vale es que no haya ni una cosa ni la otra, porque entonces el
   bot no está decidiendo.

---

## 11. Si algo no arranca

| Síntoma | Causa más probable | Qué hacer |
|---|---|---|
| `pip install` no encuentra `MetaTrader5` | Python de 32 bits, o 3.13 | `python -c "import struct; print(struct.calcsize('P')*8)"` → 64; instalar 3.11 |
| `Activate.ps1` no carga | directiva de ejecución de PowerShell | usa `.\.venv\Scripts\python.exe` (paso 3) |
| El bot sale con código **3** y `EJECUCIÓN RECHAZADA` | cuenta no demo, o algo trading desactivado | `Herramientas → Opciones → Asesores Expertos`; verificar que la sesión del terminal es la demo |
| `AVISO: la base data/state.db no existe todavía` con el bot corriendo | se arrancó desde otra carpeta | `cd` a la raíz y relanzar; borrar la base que se creó donde no tocaba |
| Faltan `.env`: login programático falla o el token no existe | `.env` no está en el directorio de trabajo | Copiarlo en la raíz (paso 6) |
| `symbol_info()` vacío, el bot no lee el precio | XAUUSD no está en el Market Watch | `Vista → Símbolos`, activar el símbolo del servidor demo |
| El panel nunca muestra una decisión | el bot corre sin `--execute` | relanzar con `--execute` (paso 10 y runbook §2) |
| El panel sale con los estilos aplicados pero **vacío del todo** —ni las etiquetas de KPIs ni de chips— y la consola del navegador (F12) dice `El recurso "http://127.0.0.1:8787/app.js" fue bloqueado debido a una discordancia del tipo MIME ("text/plain")` | el registro de esa máquina tiene `HKCR\.js` con `Content Type = text/plain`; `mimetypes` lo sobrepone a su tabla base al leer el registro, el panel sirve sus scripts como texto y el navegador los rechaza por el `X-Content-Type-Options: nosniff` que el propio panel manda. `boot()` no llega a correr: por eso no hay datos ni aviso en pantalla | con el panel actualizado no hace falta tocar nada: `create_app` fija el tipo de `.js` y `.css` al construirse. Si el código de esa máquina es anterior, `git pull` y relanzar; o sin actualizar: `reg add "HKCU\Software\Classes\.js" /v "Content Type" /t REG_SZ /d "text/javascript" /f`, reiniciar el panel y `Ctrl+Shift+R`. Desde el centinela, un panel con el guion sin ejecutar lo dice en una banda roja arriba en vez de quedarse mudo |
| `BLOCKED` por `NEWS` en todas las candidatas | el CSV del calendario no existe o está atrasado | paso 5; la comprobación de `edad_s` |
| Todo verde pero el chip del Cerebro ámbar con "hace 2 min" | umbral del bloque de decisión contra el paso de vela M5 | es correcto: una vela M5 tarda 300 s y la decisión solo se escribe al cerrarla |
| Botones del panel responden 401 | el navegador tiene un token distinto al del proceso del dashboard | pegar en la casilla el `DASHBOARD_TOKEN` del `.env` con el que arrancó el dashboard |
| El bot se reconecta en bucle y latchea `TERMINAL_DISCONNECTED` | el terminal se cerró, se actualizó o perdió la sesión | reabrir el terminal y iniciar sesión; rearma el kill switch desde el panel cuando entiendas el motivo (runbook §5) |
| Dos posiciones del bot "desaparecen" o se duplican en el panel | las dos máquinas con el mismo `magic` | paso 9 |

---

## 12. Migrar desde la máquina vieja

Orden, para que no queden posiciones huérfanas:

1. **Sin posiciones abiertas.** El ledger de posiciones (con el riesgo inicial congelado) viaja en
   la base; con una posición viva en la máquina vieja y un ledger vacío en la nueva, la gestión y
   la conciliación quedan mal. Primero cierra o termina el día.
2. Parar el bot nuevo **antes** de arrancar el viejo si vas a volver atrás: no hay dos instancias
   con el mismo `magic` (paso 9).
3. En la vieja, `Ctrl+C` en la consola del bot: escribe `SHUTDOWN` y checkpointa WAL. No lo mates
   con el terminal conectado (runbook §6).
4. Archivo del rastro (recomendado, no obligatorio): copiar `data/state.db*` y los `logs/*.jsonl`
   a un zip fuera del repo. Son la auditoría de las pruebas de octubre y noviembre.
5. La base nueva se queda **vacía y limpia**. Es más honesto para la revisión semanal: si
   importas el ledger viejo en la máquina nueva, mezclas en la misma tabla operaciones de la
   estrategia y fabricadas por `live_round_trip.py`, y la semana se lee mal.
6. Volver a empezar por el paso 8.

Qué se pierde con la base vacía: el histórico de decisiones y los dos `trades` de las pruebas
quedan en el zip, no en el panel. Qué se gana: un ledger que corresponde exactamente a lo que hace
esa máquina desde el primer día.

---

## 13. Comprobación final de que la instalación es completa

```powershell
cd $HOME\goldbot
git status --short                                  # vacío, salvo data/, logs/, reports/ (ignorados)
git check-ignore .env .venv data/state.db logs/bot.jsonl   # los cuatro: ignorados
.\.venv\Scripts\python.exe -X utf8 scripts/run_dashboard.py --config config/config.example.yaml --check | Select-Object -First 1
```

`git check-ignore` debe listar los cuatro caminos: si alguno no aparece, ese secreto o ese estado
está a punto de acabar en GitHub. Y en `git status --short` no debe salir nada — si sale
`.env`, no lo subas.

Estado "operativo" quiere decir, en términos del proyecto: el bot decide por vela M5 cerrada, con
candados de demo, 2 operaciones/día, stop obligatorio, 1 % de riesgo y −2 % diario; el panel
refleja lo que hay y avisa de lo que falta; y la revisión semanal sale sin `FALLO`. Lo que **no**
dice la instalación en una máquina nueva: que exista ventaja estadística. El walk-forward con
calendario completo ya está corrido (README, Fase 6): 117 operaciones, −0,199 R, t = −1,40, sin
ventaja demostrada. Instalar bien no cambia ese resultado.
