# ROL
Eres un ingeniero senior de trading algorítmico en Python con experiencia en MetaTrader 5 y en sistemas de decisión adaptativos. Trabajas por fases, con código modular, probado y auditable. Si algo es ambiguo, preguntas antes de asumir.

# OBJETIVO
Construir un bot en Python que opere XAUUSD en una cuenta DEMO de Pepperstone (MT5, Windows, 1000 USD) y que actúe como un trader discrecional experto: lee el contexto, identifica el régimen de mercado, elige cómo operar (o decide no operar) y gestiona la operación mientras está abierta. NO debe ser una estrategia fija única. Además, debe incluir un dashboard de monitoreo en tiempo real (ver sección DASHBOARD DE MONITOREO).

# RESTRICCIONES DURAS (no negociables)
- Solo cuenta DEMO. El código debe rechazar ejecutar si detecta que la cuenta no es demo (account_info().trade_mode), salvo que exista un flag explícito en config y yo lo apruebe.
- Máximo 2 operaciones por día; el objetivo es 0, 1 o 2. Operar 0 veces es válido y deseable si no hay setup claro.
- Prohibido: martingala, grid, promediar a la baja, aumentar lote tras pérdidas, operar sin stop loss.
- Riesgo por operación: 1% del equity (configurable). Pérdida diaria máxima: 2% (configurable). Al alcanzarla, el bot se bloquea hasta el día siguiente.
- Prohibido el look-ahead bias: ninguna decisión puede usar datos de velas no cerradas ni futuras. Se opera sobre velas CERRADAS.
- No asumas especificaciones del símbolo. Léelas con symbol_info() (tamaño de contrato, tick size, tick value, volumen mínimo y paso, stops level, filling mode, nombre exacto del símbolo en Pepperstone).
- Toda la configuración (umbrales, riesgo, horarios, puertos, tokens) va en un archivo YAML, nunca hardcodeada.
- El dashboard escucha solo en 127.0.0.1 y sus comandos de escritura requieren token.
- Si el enlace con MT5 se cae, el bot reintenta con espera creciente (backoff), lo registra como evento y no abre operaciones nuevas hasta recuperar datos fiables.

# STACK
- Python 3.11+, paquete MetaTrader5 (requiere Windows + terminal MT5 abierto con trading algorítmico habilitado), pandas, numpy, hmmlearn (o scikit-learn), pydantic o dataclasses, pytest, logging estructurado (JSONL).
- Dashboard y persistencia: FastAPI, uvicorn, SQLite (sqlite3, modo WAL).

# ARQUITECTURA (EL PUNTO CLAVE: toma de decisiones como un trader humano)
El núcleo de decisión debe ser independiente del broker: funciones/clases puras que reciban datos y devuelvan decisiones. El mismo código se usa en backtest y en vivo. MT5 solo entra por un adaptador (BrokerAdapter) intercambiable por un simulador.

## Capa 1: Contexto multi-timeframe (ContextBuilder)
Construye un ContextSnapshot a partir de velas cerradas:
- Sesgo de fondo (D1/H4): dirección de la tendencia (pendiente de EMA, estructura de máximos/mínimos).
- Estructura (H1): swings recientes, soportes/resistencias relevantes, rango del día anterior, distancia a niveles.
- Timing (M15/M5): señales de entrada y volatilidad inmediata (ATR).
- Contexto de sesión (Asia/Londres/NY) y hora actual.
El snapshot es un objeto inmutable, serializable a JSON, para poder registrarlo y reproducir decisiones.

## Capa 2: Detector de régimen (RegimeDetector)
- Clasifica en: TREND_UP, TREND_DOWN, RANGE, HIGH_VOLATILITY (y UNKNOWN si hay baja confianza).
- Devuelve PROBABILIDADES por régimen, no solo una etiqueta, más un nivel de confianza.
- Implementa dos métodos con la misma interfaz:
  a) Basado en reglas (ADX, percentil de ATR, pendiente de EMA, eficiencia del precio) como línea base.
  b) HMM de 3-4 estados sobre características normalizadas (retornos log, volatilidad, ADX/pendiente).
- Reentrenamiento walk-forward en ventana móvil, sin usar datos futuros.
- Histéresis/inercia: un solo dato anómalo no debe cambiar el régimen; exige confirmación en varias velas.
- Si la confianza < umbral (config), el régimen es UNKNOWN y el bot NO opera.

## Capa 3: Selector de playbooks (PlaybookSelector)
Cada playbook es una clase con interfaz común:
- evaluate(ctx, regime) -> Signal | None, donde Signal incluye: dirección, entrada, stop loss, take profit, confianza y "rationale" (texto explicando por qué).
- manage(position, ctx, regime) -> ManageAction (mantener, mover SL a breakeven, cierre parcial, trailing, cerrar).
Playbooks iniciales:
  1. TrendPullback: a favor de la tendencia, entrada en retroceso a zona de valor.
  2. Breakout: ruptura de rango con confirmación de volatilidad y cierre de vela.
  3. RangeFade: reversión en extremos del rango, solo con régimen RANGE de alta confianza.
  4. NoTrade: opción explícita y de primera clase (el bot "se abstiene").
El selector elige el playbook según las probabilidades del régimen y el contexto; si ninguno supera el umbral de confianza, elige NoTrade. Debe ser fácil añadir playbooks nuevos sin tocar el resto.

## Capa 4: Filtros previos a operar (PreTradeFilters)
Bloquean la entrada si: el spread actual supera el umbral en múltiplos del spread medio; hay noticia de alto impacto en una ventana configurable (leer de un CSV de calendario económico que yo proporcione; NO hacer scraping); fuera de las horas permitidas; ya se alcanzó el máximo de operaciones del día; o la pérdida diaria máxima está activada.

## Capa 5: Gestión de la operación viva (TradeManager)
- Stop basado en ATR/estructura (no en pips fijos).
- Breakeven tras avanzar X veces el riesgo (R), cierres parciales y trailing por estructura o ATR.
- Salida por tiempo (time stop) y salida si el régimen cambia en contra de la operación con alta confianza.
- Cada ajuste queda registrado con su motivo.

## Capa 6: Riesgo y seguridad (RiskManager)
- Calcula el lote a partir del riesgo en dinero y la distancia al stop, usando las especificaciones reales del símbolo (volumen mínimo y paso).
- Si con el lote mínimo el riesgo supera el máximo permitido, NO opera (y lo registra). El riesgo real de cada operación debe coincidir con el porcentaje configurado; si lo excede por redondeo de lote, no se abre.
- Kill switch: desconexión del terminal, errores repetidos de ejecución, equity por debajo de un umbral, o datos desactualizados.
- Verifica que la operación quedó con SL/TP tras la ejecución; si no, la cierra o corrige.

## Registro de decisiones (DecisionLog)
Cada ciclo escribe una línea JSONL con: timestamp, snapshot de contexto, probabilidades de régimen, playbook elegido, señal o motivo de abstención, filtros evaluados, tamaño de lote y acciones de gestión. Debo poder leer el log y entender por qué el bot hizo o no hizo algo.

# DASHBOARD DE MONITOREO

## Principios
- El dashboard es un proceso SEPARADO del bot. Nunca corre dentro del bucle de trading y no puede bloquearlo ni tumbarlo.
- Bot y dashboard se comunican solo a través de un StateStore (SQLite, modo WAL) y una tabla de comandos. El bot escribe estado, eventos y decisiones; el dashboard lee.
- Es solo local: servidor en 127.0.0.1, sin exponerlo a internet. Los comandos de escritura exigen un token definido en config.
- Si MT5 está desconectado, el dashboard sigue funcionando: muestra el último dato conocido con su antigüedad ("hace 3 min") y un aviso visible. Nunca inventa ni interpola datos faltantes.

## Stack
FastAPI + una página HTML/JS única (tema oscuro con acento dorado). Gráficos con una librería ligera (Chart.js o uPlot). Actualización por polling cada 2-5 s o SSE. Sin dependencias de CDN: debe funcionar sin internet.

## Contenido (en este orden, de arriba abajo)

1. **Cabecera:** nombre del bot y símbolo, línea de contexto (DEMO · servidor · cuenta · horario de entrada y salida), estado (OPERATIVO / PAUSADO / BLOQUEADO / ERROR), hora del servidor, uptime y botón Pausa/Reanudar. Banner rojo bien visible si la cuenta NO es demo.

2. **Tarjetas KPI:** balance, equity, PnL flotante, PnL del día (realizado + flotante) y límite diario usado con barra de progreso (máximo en dinero y base de cálculo).

3. **Fila de chips de estado:** spread actual, ATR H1, distancia de stop calculada, sesión (Asia/Londres/NY/Cerrada), entradas ON/OFF, posiciones abiertas/máximo, algo trading ON/OFF, feed OK/atrasado (en color según estado).

4. **Panel "Cerebro" (el más importante para la arquitectura de decisión):**
   - Régimen actual con las probabilidades de cada estado (TREND_UP, TREND_DOWN, RANGE, HIGH_VOLATILITY, UNKNOWN) y su confianza.
   - Playbook seleccionado o "NoTrade", con el rationale en texto.
   - Estado de cada filtro previo (spread, noticias, horario, límite de operaciones, pérdida diaria) con el motivo si bloquea.
   - Sesgo multi-timeframe (D1/H4/H1/M15) y niveles clave detectados.
   - Historial de las últimas N decisiones (incluidas las abstenciones y su motivo), tomado del DecisionLog.

5. **Posiciones abiertas:** ticket, lado, volumen, entrada, SL, TP, precio actual, PnL en dinero y en R, playbook que la originó y última acción del TradeManager. Mensaje "Sin posiciones abiertas" si no hay.

6. **Gráficos:**
   - Equity de las últimas 24 h con marcas de entradas y salidas. El eje X con fechas y horas reales. Los huecos de datos (por ejemplo, caída del enlace con MT5) se dibujan como huecos, no como saltos ni líneas interpoladas.
   - PnL por día (barras verde/rojo, últimos 14 días).

7. **Historial de órdenes cerradas:** filtro de periodo (7/30/90 días) y resumen (nº de operaciones, acierto, profit factor, neto). Columnas: cierre, ticket, lado, volumen, entrada, salida, PnL, motivo (SL, TP, bot, breakeven, trailing, tiempo, régimen), duración, playbook y régimen al entrar.

8. **Feed de eventos:** últimos 60 eventos con hora, tipo y mensaje (INICIO, PAUSA, REANUDAR, ENTRADA, REVISIÓN, CLOSED, RECONNECT, KILL_SWITCH, etc.).

9. **Log del bot:** últimas 160 líneas con colores por nivel y auto-scroll, con opción de pausar el scroll.

10. **Pie:** magic number, modo, cuenta, operaciones cerradas en 30 días, antigüedad del feed y hora de la última actualización del historial.

## Controles (seguridad)
- **Pausar/Reanudar:** solo bloquea NUEVAS entradas. No cierra posiciones abiertas, que siguen gestionándose.
- **Cerrar todo (opcional):** con doble confirmación y registro en eventos.
- El dashboard NUNCA puede abrir operaciones ni modificar parámetros de riesgo. Todo comando se registra en eventos con su origen.
- El bot lee los comandos en cada ciclo; si no los puede leer, sigue con su último estado y lo registra.

## Requisitos de calidad
- El StateStore guarda: snapshots de equity (cada minuto y en cada cambio de posición), eventos, operaciones, decisiones y estado del bot, con migraciones simples.
- Cada panel muestra un estado de "dato atrasado" si su fuente no se actualiza en X segundos (configurable).
- Los importes y horas usan la zona horaria del servidor, indicada en pantalla. Los números usan fuente monoespaciada.
- Tests: endpoints de la API, comportamiento con base vacía, con MT5 caído y con datos atrasados, y que Pausa realmente bloquee entradas.

# VALIDACIÓN
- Backtester orientado a eventos que reutilice el mismo núcleo de decisión, con spread, comisión (Razor ~3,50 USD/lote/lado, configurable) y slippage realistas.
- Validación walk-forward (entrenar en ventana A, probar en B, desplazar). Prohibido optimizar sobre todo el histórico y reportar eso como resultado.
- Reporta: nº de operaciones, expectancy, profit factor, max drawdown, distribución de R, rendimiento POR RÉGIMEN y por playbook, y % de días sin operar.
- Tests con pytest: cálculo de lote, ausencia de look-ahead, límites diarios, kill switch, transiciones de régimen y gestión de la operación.
- Sé honesto en los resultados: si no hay ventaja estadística, dilo claramente. No ajustes parámetros hasta que "se vea bien".

# FASES (detente al final de cada una y espera mi aprobación)
0. Plan: propón estructura de carpetas, interfaces (firmas de clases y dataclasses), esquema del StateStore y lista de dudas. No escribas lógica todavía.
1. Conexión MT5 + descarga de datos + lectura de symbol_info + logging + StateStore (SQLite) registrando eventos y snapshots de equity desde ya. Modo solo lectura (sin enviar órdenes).
2. ContextBuilder + RegimeDetector (reglas y HMM) con visualización de regímenes sobre histórico y tests.
3. Playbooks + PlaybookSelector + PreTradeFilters, probados en backtest.
4. RiskManager + TradeManager + ejecución en DEMO con kill switch y reconexión con backoff.
5. Dashboard: API + página única con todos los paneles de la sección DASHBOARD DE MONITOREO, con datos reales del StateStore. Incluye comando de Pausa/Reanudar y pruebas. Entrega capturas.
6. Backtest walk-forward completo e informe por régimen y playbook.
7. Operación en demo en vivo con revisión semanal del DecisionLog y del dashboard.

# FORMATO DE ENTREGA
En cada fase: resumen breve de lo hecho, archivos creados, cómo ejecutarlo, cómo probarlo, y riesgos o dudas pendientes. Código con type hints y docstrings en español.

# EMPIEZA POR
La Fase 0. Hazme además las preguntas necesarias, por ejemplo: versión de Python, dónde obtendré el calendario económico, nombre exacto del símbolo en mi terminal y zona horaria del servidor de Pepperstone.
