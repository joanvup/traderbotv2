/* Dashboard de goldbot: página única que sólo habla con la API local del propio proceso.
 *
 * Reglas que se respetan aquí:
 * - Nunca se calcula nada que el bot no haya publicado: si un campo viene `null` se pinta
 *   "—" y su insignia de antigüedad, no un cero ni un valor supuesto.
 * - Los huecos del gráfico de equity se dejan vacíos (uPlot los corta) porque una caída del
 *   enlace con el servidor tiene que verse, no interpolarse.
 * - Las únicas acciones posibles son Pausar, Reanudar y Cerrar todo; no hay ningún control
 *   que abra una operación ni que toque parámetros de riesgo.
 */

"use strict";

const ui = {
  offsetMin: 0,
  refreshMs: 3000,
  staleAfterS: 15,
  closeAllEnabled: true,
  closeAllTtlS: 120,
  timer: null,
  tradesDays: 30,
  charts: { equity: null, daily: null },
  pending: {},
  armedCloseAll: 0,
  armedResetKill: 0,
  inFlight: new Set(),
};

/* -- utilidades de formato -------------------------------------------------- */

const nf = (digits) =>
  new Intl.NumberFormat("es-ES", { minimumFractionDigits: digits, maximumFractionDigits: digits });

function num(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return nf(digits).format(Number(value));
}

function money(value, currency) {
  if (value === null || value === undefined) return "—";
  const signed = Number(value);
  const sign = signed > 0 ? "+" : signed < 0 ? "−" : "";
  return `${sign}${num(Math.abs(signed))} ${currency || "USD"}`;
}

/** Saldo bruto sin signo: el balance y el equity no son un resultado, son una cifra. */
function amount(value, currency) {
  if (value === null || value === undefined) return "—";
  return `${num(Number(value))} ${currency || "USD"}`;
}

function signedClass(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "pos" : "neg";
}

/** Hora del servidor: la zona la fija el bot; aquí sólo se desplaza el UTC publicado. */
function serverDate(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return new Date(t + ui.offsetMin * 60_000);
}

function fmtTime(iso) {
  const d = serverDate(iso);
  if (!d) return "—";
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getUTCDate())}-${p(d.getUTCMonth() + 1)} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

function fmtClock(iso) {
  const d = serverDate(iso);
  if (!d) return "--:--:--";
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "sin dato";
  const s = Math.max(0, Math.round(Number(seconds)));
  if (s < 60) return `hace ${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `hace ${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `hace ${h} h`;
  return `hace ${Math.floor(h / 24)} d`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.round(Number(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}

function dash(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

/** Importe negativo en pantalla: el límite de pérdida se muestra como lo que es, una resta. */
function negMoney(value, currency) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return money(-n, currency);
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function put(id, html) {
  const node = document.getElementById(id);
  if (node) node.innerHTML = html;
}

function text(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

/** Insignia de antigüedad por fuente: cada panel se marca solo, no toda la página.
 *
 * `ciclo` (por defecto) son las fuentes que el bot debe refrescar cada vela cerrada: si
 * envejecen, el enlace falló y hay que avisar en ámbar. `registro` son las tablas de sólo
 * inserción (eventos, log, operaciones): que no haya filas nuevas es lo normal en un
 * mercado tranquilo, así que su insignia informa sin alarmar. `flat` es el mismo aviso que
 * `ciclo` pero con el texto del caso normal: sin posiciones abiertas no hay fila que pueda
 * estar atrasada, y lo que se vigila es que el ciclo que reconcilia siga vivo.
 */
function setStale(name, entry, kind = "ciclo") {
  document.querySelectorAll(`[data-stale="${name}"]`).forEach((node) => {
    if (!entry) {
      node.className = "stale stale-unknown";
      node.textContent = "sin dato";
      node.title = "Esta fuente nunca escribió en la base.";
      return;
    }
    if (kind === "registro") {
      node.className = "stale stale-info";
      node.textContent = `último registro ${fmtAge(entry.age_s)}`;
      node.title = `Última escritura: ${dash(entry.as_of_utc)} (UTC)`;
      return;
    }
    const stale = Boolean(entry.stale);
    node.className = `stale ${stale ? "stale-on" : "stale-ok"}`;
    node.textContent = stale
      ? `dato atrasado · ${fmtAge(entry.age_s)}`
      : kind === "flat"
        ? `sin posiciones · ${fmtAge(entry.age_s)}`
        : fmtAge(entry.age_s);
    node.title = `Última escritura: ${dash(entry.as_of_utc)} (UTC)`;
  });
}

function table(headers, rows, cls = "") {
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.length
    ? rows.map((cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")
    : `<tr><td class="empty" colspan="${headers.length}">Sin registros en la base.</td></tr>`;
  return `<table class="${cls}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

const mono = (value, digits = 2) => `<span class="mono">${num(value, digits)}</span>`;

/* -- acceso a la API -------------------------------------------------------- */

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    throw new Error(`${res.status}: ${detail}`);
  }
  return body;
}

function tokenHeaders() {
  const token = (document.getElementById("token").value || "").trim();
  return token ? { "X-Dashboard-Token": token } : {};
}

function toast(message, kind = "info") {
  const node = document.getElementById("toast");
  node.className = `toast toast-${kind}`;
  node.textContent = message;
  clearTimeout(node._t);
  node._t = setTimeout(() => node.classList.add("hidden"), 6000);
}

/* -- comandos --------------------------------------------------------------- */

/* Un doble clic no puede producir dos órdenes: se reutiliza la clave mientras el intento
   sigue vivo y el backend deduplica por `idempotency_key` en la misma transacción. */
function idempotencyKey(command) {
  const prev = ui.pending[command];
  if (prev && Date.now() - prev.at < 120_000 && !prev.done) return prev.key;
  const key = `${command}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  ui.pending[command] = { key, at: Date.now(), done: false };
  return key;
}

const COMMAND_LABEL = {
  pause: "Pausa enviada: no se abrirán nuevas operaciones (las abiertas siguen gestionándose).",
  resume: "Reanudación enviada.",
  close_all: null, // se compone con el TTL en sendCommand
  reset_kill_switch: "Reset del kill switch enviado.",
};

async function sendCommand(command) {
  if (ui.inFlight.has(command)) return;
  ui.inFlight.add(command);
  try {
    const res = await api("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tokenHeaders() },
      body: JSON.stringify({ command, idempotency_key: idempotencyKey(command) }),
    });
    if (ui.pending[command]) ui.pending[command].done = true;
    const label =
      command === "close_all"
        ? `Cerrar todo en cola; caduca a los ${ui.closeAllTtlS} s si el bot no llega a leerlo.`
        : COMMAND_LABEL[command];
    toast(`${label} (estado ${res.status})`, command === "close_all" ? "warn" : "ok");
    refreshNow();
  } catch (err) {
    delete ui.pending[command];
    toast(`Comando rechazado — ${err.message}`, "error");
  } finally {
    ui.inFlight.delete(command);
  }
}

/* Doble confirmación en dos pasos: el primer clic arma el botón, el segundo ejecuta. */
function armCloseAll() {
  const btn = document.getElementById("btn-close-all");
  if (ui.armedCloseAll + 5000 < Date.now()) {
    ui.armedCloseAll = Date.now();
    btn.classList.add("is-armed");
    btn.textContent = "Confirmar cierre total";
    toast("Segundo clic para cerrar TODAS las posiciones propias.", "warn");
    return;
  }
  disarmCloseAll();
  sendCommand("close_all");
}

function disarmCloseAll() {
  const btn = document.getElementById("btn-close-all");
  ui.armedCloseAll = 0;
  btn.classList.remove("is-armed");
  btn.textContent = "Cerrar todo";
}

/* Rearmar el kill switch devuelve el bot a operar, así que exige el mismo doble clic que
   cerrar posiciones. Solo tiene sentido con el candado latcheado: con él suelto el botón se
   oculta, para no ofrecer un botón que no hace nada. */
function armResetKill() {
  const btn = document.getElementById("btn-reset-kill");
  if (ui.armedResetKill + 5000 < Date.now()) {
    ui.armedResetKill = Date.now();
    btn.classList.add("is-armed");
    btn.textContent = "Confirmar rearme";
    toast("Segundo clic: el bot vuelve a poder abrir operaciones.", "warn");
    return;
  }
  disarmResetKill();
  sendCommand("reset_kill_switch");
}

function disarmResetKill() {
  const btn = document.getElementById("btn-reset-kill");
  ui.armedResetKill = 0;
  btn.classList.remove("is-armed");
  btn.textContent = "Rearmar kill switch";
}

/* -- panel 1: cabecera ------------------------------------------------------ */

function zoneLabel(st) {
  if (!st) return "hora del servidor";
  const minutes = Number(st.offset_min ?? 0);
  const sign = minutes < 0 ? "−" : "+";
  const abs = Math.abs(minutes);
  return `hora del servidor · ${dash(st.server_zone)} · UTC${sign}${Math.floor(abs / 60)}:${String(abs % 60).padStart(2, "0")}`;
}

function renderHeader(ov) {
  const h = ov.header;
  ui.offsetMin = Number(h.server_time?.offset_min ?? 0);
  text("hdr-symbol", dash(h.symbol));
  text("hdr-context", [
    dash(h.mode),
    dash(h.account?.server),
    h.account?.login ? `cuenta ${h.account.login}` : "sin cuenta",
    (h.entry_windows || []).length ? `ventanas ${h.entry_windows.join(", ")}` : "sin ventanas",
    h.avoid_rollover ? "evita rollover" : "rollover permitido",
    h.cycle_seq ? `ciclo ${h.cycle_seq}` : "sin ciclos",
    h.app_version ? `v${h.app_version}` : "",
  ].filter(Boolean).join(" · "));

  const state = String(h.state || "SIN DATOS");
  const node = document.getElementById("hdr-state");
  node.className = `state ${
    h.kill_switch_latched ? "state-killed"
      : state === "OPERATIVO" ? "state-ok"
        : state === "PAUSADO" ? "state-paused"
          : state === "KILLED" ? "state-killed"
            : state === "ERROR" ? "state-error" : "state-unknown"
  }`;
  node.textContent = h.paused && state === "PAUSADO" ? "PAUSADO (por comando)" : state;

  text("hdr-server-time", h.server_time?.server ? String(h.server_time.server).slice(11) : "--:--:--");
  text("hdr-server-zone", zoneLabel(h.server_time));
  text("hdr-uptime", h.uptime_s === null || h.uptime_s === undefined
    ? "uptime —" : `uptime ${fmtDuration(h.uptime_s)}`);
  document.getElementById("hdr-uptime").title = `último STARTUP: ${dash(h.last_startup_utc)} UTC`;

  document.getElementById("banner-live").classList.toggle("hidden", h.is_demo !== false);
  document.getElementById("btn-pause").disabled = h.paused === true;
  document.getElementById("btn-resume").disabled = h.paused !== true;

  const v = ov.view || {};
  ui.closeAllEnabled = v.close_all_enabled !== false;
  ui.closeAllTtlS = v.close_all_ttl_s ?? 120;
  ui.staleAfterS = v.stale_after_s ?? 15;
  ui.refreshMs = Math.max(1, v.refresh_s ?? 3) * 1000;
  document.getElementById("btn-close-all").hidden = !ui.closeAllEnabled;

  const latched = h.kill_switch_latched === true || state === "KILLED";
  const resetBtn = document.getElementById("btn-reset-kill");
  resetBtn.hidden = !latched;
  if (!latched && ui.armedResetKill) disarmResetKill();
  if (latched) {
    const reason = dash(h.kill_reason);
    resetBtn.title = `Kill switch latcheado por: ${reason}. El rearme es un comando con token y queda registrado como KILL_RESET.`;
  }

  const box = document.querySelector(".tokenbox");
  box.classList.toggle("is-warn", !v.token_present);
  box.title = `Los comandos se firman con el token de la variable ${dash(v.token_env)} del proceso del dashboard`
    + (v.token_present ? "" : " · AVISO: esa variable está vacía en el servidor, ningún comando será aceptado (401)");
}

function renderBanners(ov) {
  const node = document.getElementById("banner-degraded");
  const reasons = [];
  const s = ov.staleness || {};
  if (!s.bot_state || s.bot_state.age_s === null) {
    reasons.push("el bot nunca publicó estado en esta base");
  } else if (s.bot_state.stale) {
    reasons.push(`el bot no escribe desde ${fmtAge(s.bot_state.age_s)}`);
  }
  if (ov.header?.kill_switch_latched) reasons.push(`kill switch: ${dash(ov.header.kill_reason)}`);
  if (ov.kpis?.daily_lock_latched) {
    reasons.push("pérdida diaria máxima alcanzada: sin entradas hasta el día siguiente");
  }
  if (ov.header?.state === "ERROR") reasons.push("el último ciclo terminó con error");
  node.textContent = reasons.length
    ? `⚠ ${reasons.join(" · ")}. Se muestran los últimos datos conocidos; no se rellena ni se interpola.`
    : "";
  node.classList.toggle("hidden", reasons.length === 0);
}

/* -- panel 2: KPIs ---------------------------------------------------------- */

function renderKpis(ov) {
  const k = ov.kpis || {};
  const cur = ov.header?.account?.currency || "USD";
  const cards = [
    ["Balance", amount(k.balance_usd, cur), ""],
    ["Equity", amount(k.equity_usd, cur), ""],
    ["PnL flotante", money(k.floating_usd, cur), signedClass(k.floating_usd)],
    ["PnL del día (realizado + flotante)", money(k.day_total_usd, cur), signedClass(k.day_total_usd)],
    ["Riesgo por operación", k.risk_pct === undefined ? "—" : `${num(k.risk_pct)} %`, ""],
    ["Límite de pérdida diario", negMoney(k.daily_limit_usd, cur), "neg"],
  ];
  put("kpis", cards.map(([label, value, cls]) => `<div class="kpi">
    <div class="kpi-label">${esc(label)}</div>
    <div class="kpi-value mono ${cls}">${esc(value)}</div></div>`).join(""));

  const used = k.daily_limit_used_pct;
  const fill = document.getElementById("limit-fill");
  fill.style.width = `${Math.min(100, Math.max(0, (used ?? 0) * 100))}%`;
  fill.className = `fill ${used >= 1 ? "fill-max" : used >= 0.5 ? "fill-half" : ""}`;
  text("limit-text", used === null || used === undefined
    ? `límite ${negMoney(k.daily_limit_usd, cur)} (sin equity de referencia)`
    : `${num(used * 100, 1)} % de ${negMoney(k.daily_limit_usd, cur)}`);
  text("limit-base", `${dash(k.base_calculo)} · ${num(k.daily_loss_pct)} % · realizado ${money(k.day_realized_usd, cur)} + flotante ${money(k.floating_usd, cur)}`);
}

/* -- panel 3: chips --------------------------------------------------------- */

function renderChips(ov) {
  put("chips", (ov.chips || []).map((c) => {
    const cls = c.ok === true ? "chip-ok" : c.ok === false ? "chip-bad" : "chip-neutral";
    return `<div class="chip ${cls}">
      <span class="chip-label">${esc(dash(c.label))}</span>
      <span class="chip-value mono">${esc(c.value === null || c.value === undefined ? "—" : String(c.value))}</span>
    </div>`;
  }).join(""));

  const fresh = ov.feed_freshness || {};
  const tfs = Object.entries(fresh);
  put("feed", tfs.length
    ? `<div class="tiny muted feed-title">última vela CERRADA por temporalidad — con el mercado cerrado o el enlace caído el bot no decide</div>
      ${tfs.map(([tf, f]) => `<div class="feed-row">
        <span class="mono">${esc(tf)}</span>
        <span class="mono">${esc(fmtTime(f.last_bar_close_utc))}</span>
        <span class="tag ${String(f.status).toUpperCase() === "OK" ? "tag-ok" : "tag-bad"}">${esc(dash(f.status))}</span>
        <span class="tiny muted">${esc(fmtAge(f.age_s))}</span></div>`).join("")}`
    : `<div class="tiny muted">El bot no publicó frescura de velas todavía.</div>`);

  ["bot_state", "account", "context", "health", "decision", "equity_series", "positions"]
    .forEach((key) => {
      const flat = key === "positions" && (ov.positions || []).length === 0;
      setStale(key, (ov.staleness || {})[key], flat ? "flat" : "ciclo");
    });
}

/* -- panel 4: cerebro ------------------------------------------------------- */

function probBar(label, value, color) {
  const v = Number(value ?? 0);
  return `<div class="prob"><span class="prob-label">${esc(label)}</span>
    <span class="prob-track"><span class="prob-fill" style="width:${(v * 100).toFixed(1)}%;background:${color}"></span></span>
    <span class="prob-value mono">${num(v * 100, 1)} %</span></div>`;
}

function renderBrain(brain) {
  const reg = brain.regime || {};
  const latest = brain.latest || {};

  put("brain-regime", `<div class="brain-title">Régimen detectado</div>
    <div class="regime-line">
      <span class="regime-label">${esc(dash(reg.label))}</span>
      <span class="tiny muted mono">confianza ${num((reg.confidence ?? 0) * 100, 1)} % · método ${esc(dash(reg.method))} · ${esc(dash(reg.confirmed_bars))} velas confirmadas</span>
    </div>`);

  const probs = reg.probs || brain.probs || {};
  const palette = { TREND_UP: "#3fb950", TREND_DOWN: "#e05252", RANGE: "#d4af37", HIGH_VOLATILITY: "#8957e5" };
  put("brain-probs", Object.keys(probs).length
    ? Object.entries(probs).sort((a, b) => b[1] - a[1])
      .map(([k, v]) => probBar(k, v, palette[k] || "#58a6ff")).join("")
    : `<div class="tiny muted">Sin probabilidades de régimen publicadas.</div>`);

  const action = dash(latest.action);
  const actionCls = { ENTER: "tag-ok", BLOCKED: "tag-bad" }[action] || "tag-neutral";
  const risk = latest.risk;
  put("brain-playbook", `<div class="brain-title">Playbook y veredicto</div>
    <div class="playbook-line">
      <span class="mono strong">${esc(dash(brain.playbook))}</span>
      <span class="tag ${actionCls}">${esc(action)}</span>
    </div>
    <div class="rationale">${esc(dash(brain.rationale))}</div>
    <div class="tiny muted">vela de decisión ${esc(fmtTime(latest.bar_time_utc))} · ciclo ${esc(dash(latest.cycle_id))} · decisión ${esc(dash(latest.decision_id))}</div>
    ${risk ? `<div class="tiny">dimensionamiento: aprobado <span class="mono">${esc(dash(risk.approved))}</span> ·
      lotes <span class="mono">${esc(dash(risk.lots))}</span> · riesgo <span class="mono">${esc(dash(risk.risk_money))}</span> USD
      (${esc(dash(risk.risk_pct_actual))} %) · stop <span class="mono">${esc(dash(risk.sl_distance_points))}</span> puntos
      ${risk.reason ? ` · ${esc(risk.reason)}` : ""}</div>` : ""}`);

  const f = brain.filters;
  if (!f) {
    put("brain-filters", `<div class="brain-title">Filtros pre-trade</div>
      <div class="tiny muted">No se evaluaron: sin señal válida el bot aborta antes de correrlos. No es un "todo OK", es que no hubo nada que filtrar.</div>`);
  } else {
    const rows = (f.results || []).map((r) => [
      `<span class="mono">${esc(dash(r.code))}</span>`,
      `<span class="tag ${r.blocked ? "tag-bad" : "tag-ok"}">${r.blocked ? "BLOQUEA" : "OK"}</span>`,
      `<span class="mono">${esc(dash(r.observed))}</span>`,
      `<span class="mono">${esc(dash(r.threshold))}</span>`,
      `<span class="tiny">${esc(dash(r.message))}</span>`,
    ]);
    put("brain-filters", `<div class="brain-title">Filtros pre-trade</div>
      <div class="tiny ${f.blocked ? "neg strong" : "muted"}">${f.blocked
        ? `Bloqueo principal: ${dash(f.primary_reason)}`
        : `Sin bloqueos${f.primary_reason ? ` (${f.primary_reason})` : ""}`}</div>
      ${table(["filtro", "estado", "observado", "umbral", "detalle"], rows, "compact")}`);
  }

  const bias = brain.bias || {};
  const biasRows = Object.entries(bias).map(([tf, b]) => {
    const dir = String(b.direction || "");
    const cls = ["BUY", "LONG"].includes(dir) ? "tag-ok" : ["SELL", "SHORT"].includes(dir) ? "tag-bad" : "tag-neutral";
    return [
      `<span class="mono">${esc(tf)}</span>`,
      `<span class="tag ${cls}">${esc(dash(dir))}</span>`,
      mono(b.strength),
      `<span class="tiny">${esc(dash(b.rationale))}</span>`,
    ];
  });
  put("brain-bias", `<div class="brain-title">Sesgo multi-temporal</div>
    ${table(["tf", "dirección", "fuerza", "motivo"], biasRows, "compact")}`);

  const st = brain.structure || {};
  const levelRows = (st.levels || []).slice(0, 8).map((l) => [
    mono(l.price),
    `<span class="tiny">${esc(dash(l.kind))}</span>`,
    mono(l.strength),
    `<span class="mono">${esc(dash(l.touches))}</span>`,
  ]);
  put("brain-levels", `<div class="brain-title">Niveles clave</div>
    <div class="tiny muted">posición dentro del rango ${mono(st.range_position)} ·
      día anterior ${mono(st.prev_day_low)}–${mono(st.prev_day_high)} ·
      distancia al nivel más cercano ${mono(st.nearest_level_distance)}</div>
    ${table(["precio", "tipo", "fuerza", "toques"], levelRows, "compact")}`);

  const histRows = (brain.history || []).map((d) => [
    `<span class="mono tiny">${esc(fmtTime(d.ts_utc))}</span>`,
    `<span class="mono tiny">${esc(dash(d.cycle_id))}</span>`,
    `<span class="tag ${({ ENTER: "tag-ok", BLOCKED: "tag-bad" })[d.action] || "tag-neutral"}>${esc(dash(d.action))}</span>`,
    `<span class="mono tiny">${esc(dash(d.playbook_id))}</span>`,
    `<span class="mono tiny">${esc(dash(d.regime?.label))}</span>`,
    `<span class="tiny">${esc(dash(d.rationale))}</span>`,
  ]);
  put("brain-history", `<div class="brain-title">Últimas decisiones (las abstenciones también)</div>
    ${table(["hora", "ciclo", "acción", "playbook", "régimen", "motivo"], histRows, "compact")}`);

  setStale("decision", (brain.staleness || {}).decision);
}

/* -- panel 5: posiciones ---------------------------------------------------- */

function renderPositions(ov) {
  const cur = ov.header?.account?.currency || "USD";
  const rows = (ov.positions || []).map((p) => [
    `<span class="mono">${esc(dash(p.ticket ?? p.position_id))}</span>`,
    `<span class="tag ${p.side === "BUY" ? "tag-ok" : "tag-bad"}">${esc(dash(p.side))}</span>`,
    mono(p.volume),
    mono(p.entry_price),
    mono(p.sl),
    mono(p.tp),
    mono(p.current_price),
    `<span class="mono ${signedClass(p.pnl_money)}">${esc(money(p.pnl_money, cur))}</span>`,
    `${mono(p.r_multiple)} R`,
    `<span class="mono tiny">${esc(dash(p.playbook_id))}</span>`,
    `<span class="tiny">${esc(dash(p.last_action))}</span>`,
    `<span class="tiny muted">${esc(fmtAge(p.photo_age_s))}</span>`,
  ]);
  put("positions", rows.length
    ? `${table(["ticket", "lado", "lotes", "entrada", "SL", "TP", "precio actual", "PnL", "R", "playbook", "última acción", "foto"], rows, "compact")}
      <div class="tiny muted">El precio actual y el PnL los publica el bot en su ciclo: si la foto está atrasada, el mercado puede estar en otro lado.</div>`
    : `<div class="empty-line">Sin posiciones abiertas.</div>`);
}

/* -- panel 6: gráficos ------------------------------------------------------ */

function equityTick() {
  return (v) => {
    const d = new Date(v * 1000 + ui.offsetMin * 60_000);
    const p = (x) => String(x).padStart(2, "0");
    return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  };
}

function nearestIndex(xs, tsIso) {
  const t = Date.parse(tsIso) / 1000;
  if (!Number.isFinite(t) || !xs.length) return -1;
  let lo = 0;
  let hi = xs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function buildEquityChart() {
  const el = document.getElementById("chart-equity");
  ui.charts.equity = new uPlot(
    {
      width: el.clientWidth || 640,
      height: 220,
      legend: { show: true },
      axes: [
        { stroke: "#8b8f98", grid: { stroke: "#2a2e35" }, values: (self, ticks) => ticks.map(equityTick()) },
        { stroke: "#8b8f98", grid: { stroke: "#2a2e35" }, size: 62 },
      ],
      series: [
        {},
        { label: "equity (hueco = sin datos)", stroke: "#d4af37", width: 1.6, points: { show: false } },
        { label: "entrada", stroke: "#3fb950", fill: "#3fb950", points: { show: true, size: 10, width: 2 }, paths: () => null },
        { label: "salida", stroke: "#e05252", fill: "#e05252", points: { show: true, size: 10, width: 2 }, paths: () => null },
      ],
    },
    [[0], [null], [null], [null]],
    el,
  );
}

function renderEquity(data) {
  const chart = ui.charts.equity;
  if (!chart) return;
  const xs = (data.x || []).map(Number);
  // `null` se conserva a propósito: uPlot corta la línea en vez de inventar la recta.
  const eq = (data.equity || []).map((v) => (v === null || v === undefined ? null : Number(v)));
  const entry = new Array(xs.length).fill(null);
  const exit = new Array(xs.length).fill(null);
  (data.markers || []).forEach((m) => {
    const i = nearestIndex(xs, m.ts_utc);
    if (i < 0 || eq[i] === null) return;
    (m.kind === "entry" ? entry : exit)[i] = eq[i];
  });
  chart.setData([xs, eq, entry, exit]);
  const sum = data.summary || {};
  const cur = "USD";
  text("equity-note", [
    sum.last_equity === null || sum.last_equity === undefined
      ? "último equity: — (la base aún no tiene ninguna foto)"
      : `último equity ${amount(sum.last_equity, cur)}`,
    sum.last_ts_utc ? `lectura ${fmtTime(sum.last_ts_utc)} (corte ${fmtAge(sum.current_gap_s)})` : "sin lecturas en la ventana",
    `cobertura ${num(sum.coverage_pct, 1)} % de ${num(sum.window_min, 0)} min`,
    `huecos ${num(sum.gap_min, 0)} min`,
    sum.min_equity === null ? "mín/máx —" : `rango ${num(sum.min_equity)} – ${num(sum.max_equity)}`,
    `marcadores ${num((data.markers || []).length, 0)}`,
  ].join("  ·  "));
  setStale("equity_series", data.staleness);
}

function buildDailyChart() {
  const el = document.getElementById("chart-daily");
  ui.charts.daily = new uPlot(
    {
      width: el.clientWidth || 640,
      height: 130,
      legend: { show: false },
      scales: { x: { time: false } },
      axes: [
        {
          stroke: "#8b8f98",
          grid: { stroke: "transparent" },
          size: 48,
          values: (u, vals) => vals.map((i) => (u._days && u._days[i] ? String(u._days[i]).slice(5) : "")),
        },
        { stroke: "#8b8f98", grid: { stroke: "#2a2e35" }, size: 58 },
      ],
      series: [
        { label: "día" },
        { label: "PnL realizado (+)", stroke: "#3fb950", fill: "rgba(63,185,80,0.75)", paths: uPlot.paths.bars({ size: 0.62 }) },
        { label: "PnL realizado (−)", stroke: "#e05252", fill: "rgba(224,82,82,0.75)", paths: uPlot.paths.bars({ size: 0.62 }) },
      ],
    },
    [[0], [null], [null]],
    el,
  );
}

function renderDaily(data) {
  const chart = ui.charts.daily;
  if (!chart) return;
  const days = data.days || [];
  chart._days = days;
  const xs = days.map((_, i) => i);
  const values = data.realized_usd || [];
  const pos = values.map((v) => (v === null || v < 0 ? null : v));
  const neg = values.map((v) => (v === null || v > 0 ? null : v));
  chart.setData([xs, pos, neg]);
  const locked = (data.locked || []).filter(Boolean).length;
  chart.root.title = locked
    ? `${locked} día(s) en que se alcanzó la pérdida diaria máxima`
    : "PnL realizado por día operativo";

  const rows = (data.rows || []).filter((r) => r.realized_usd !== null && r.realized_usd !== undefined);
  const best = rows.length ? Math.max(...rows.map((r) => r.realized_usd)) : null;
  const worst = rows.length ? Math.min(...rows.map((r) => r.realized_usd)) : null;
  const net = rows.reduce((acc, r) => acc + Number(r.realized_usd), 0);
  const entries = rows.reduce((acc, r) => acc + Number(r.entries ?? 0), 0);
  text("daily-note", [
    rows.length
      ? `mejor día ${money(best)} · peor ${money(worst)} · neto ${money(net)} · entradas ${num(entries, 0)}`
      : "ningún día operativo con PnL realizado en la ventana",
    locked ? `${num(locked, 0)} día(s) bloqueados por alcanzar la pérdida diaria máxima` : "ningún día bloqueado",
  ].join("  ·  "));
  setStale("daily", (data.staleness || {}).daily);
}

/* -- panel 7: historial ----------------------------------------------------- */

const TRADE_HEADERS = ["cierre", "ticket", "lado", "lotes", "entrada", "salida", "PnL", "R", "motivo", "duración", "playbook", "régimen"];

function renderTrades(data) {
  const s = data.summary || {};
  put("trades-summary", [
    ["operaciones", num(s.trades, 0)],
    ["acierto", s.win_rate_pct === null || s.win_rate_pct === undefined ? "—" : `${num(s.win_rate_pct, 1)} %`],
    ["factor beneficio", s.profit_factor === null ? "—" : num(s.profit_factor, 2)],
    ["expectancia", s.expectancy_r === null ? "—" : `${num(s.expectancy_r, 2)} R`],
    ["neto", money(s.net_usd)],
    ["brutos", `${money(s.gross_profit_usd)} / ${money(s.gross_loss_usd)}`],
  ].map(([label, value]) => `<div class="sum"><span class="sum-label">${esc(label)}</span><span class="mono sum-value">${esc(value)}</span></div>`).join(""));

  const rows = (data.rows || []).map((t) => [
    `<span class="mono tiny">${esc(fmtTime(t.close_time_utc))}</span>`,
    `<span class="mono">${esc(dash(t.position_id))}</span>`,
    `<span class="tag ${t.side === "BUY" ? "tag-ok" : "tag-bad"}">${esc(dash(t.side))}</span>`,
    mono(t.volume),
    mono(t.entry_price),
    mono(t.exit_price),
    `<span class="mono ${signedClass(t.net_profit)}">${esc(money(t.net_profit))}</span>`,
    `${mono(t.r_multiple)} R`,
    `<span class="mono tiny">${esc(dash(t.exit_reason))}${t.exit_reason_source && t.exit_reason_source !== "MT5" ? ` (${esc(t.exit_reason_source)})` : ""}</span>`,
    `<span class="mono tiny">${esc(fmtDuration(t.duration_s))}</span>`,
    `<span class="mono tiny">${esc(dash(t.playbook_id))}</span>`,
    `<span class="mono tiny">${esc(dash(t.regime_at_entry))}</span>`,
  ]);
  const note = rows.length ? ""
    : `<div class="tiny muted">Sin operaciones cerradas en ${esc(String(data.days))} días: 0 no es un mal resultado, es ausencia de datos.</div>`;
  put("trades", `${table(TRADE_HEADERS, rows, "compact")}${note}`);
  setStale("trades", data.staleness, "registro");
}

/* -- paneles 8 y 9: eventos y log ------------------------------------------- */

const LEVEL_CLASS = { INFO: "lv-info", WARNING: "lv-warn", ERROR: "lv-error", DEBUG: "lv-debug" };

function renderEvents(data) {
  const rows = (data.rows || []).slice().reverse(); // orden cronológico, el más reciente al pie
  put("events", rows.length
    ? rows.map((e) => {
      const cls = LEVEL_CLASS[String(e.level).toUpperCase()] || "lv-info";
      return `<div class="row ${cls}">
        <span class="mono tiny time">${esc(fmtTime(e.ts_utc))}</span>
        <span class="mono tiny src">${esc(dash(e.source))}</span>
        <span class="tag tag-neutral tiny">${esc(dash(e.type))}</span>
        <span class="msg">${esc(dash(e.message))}</span></div>`;
    }).join("")
    : `<div class="empty-line">Sin eventos registrados.</div>`);
  setStale("events", data.staleness, "registro");
}

function renderLogs(data) {
  const list = document.getElementById("logs");
  const rows = data.rows || [];
  put("logs", rows.length
    ? rows.map((l) => {
      const cls = LEVEL_CLASS[String(l.level).toUpperCase()] || "lv-info";
      const extra = l.extra_json ? ` <span class="tiny muted">${esc(JSON.stringify(l.extra_json).slice(0, 160))}</span>` : "";
      return `<div class="log ${cls}">
        <span class="mono tiny time">${esc(fmtClock(l.ts_utc))}</span>
        <span class="mono tiny lvl">${esc(dash(l.level))}</span>
        <span class="tiny lg">${esc(dash(l.logger).replace("goldbot.", ""))}</span>
        <span class="msg">${esc(dash(l.message))}${extra}</span></div>`;
    }).join("")
    : `<div class="empty-line">Sin líneas de log.</div>`);
  if (document.getElementById("autoscroll").checked) list.scrollTop = list.scrollHeight;
  setStale("logs", data.staleness, "registro");
}

/* -- auditoría de comandos e intentos de orden ------------------------------ */

function renderAudit(commands, attempts) {
  const cmdRows = (commands.rows || []).map((c) => [
    `<span class="mono tiny">${esc(fmtTime(c.created_at_utc))}</span>`,
    `<span class="mono">${esc(dash(c.command))}</span>`,
    `<span class="tag ${c.status === "DONE" ? "tag-ok" : ["FAILED", "EXPIRED"].includes(String(c.status)) ? "tag-bad" : "tag-neutral"}">${esc(dash(c.status))}</span>`,
    `<span class="mono tiny">${esc(dash(c.origin))}</span>`,
    `<span class="mono tiny">${esc(dash(c.idempotency_key))}</span>`,
    `<span class="tiny">${esc(dash(c.result_json ? JSON.stringify(c.result_json) : null))}</span>`,
  ]);
  put("commands", `<div class="brain-title">Comandos enviados desde el panel</div>
    ${table(["hora", "comando", "estado", "origen", "clave de idempotencia", "resultado"], cmdRows, "compact")}`);

  const attRows = (attempts.rows || []).map((a) => [
    `<span class="mono tiny">${esc(fmtTime(a.ts_utc))}</span>`,
    `<span class="mono">${esc(dash(a.retcode_name))}</span>`,
    `<span class="mono">${esc(dash(a.retcode))}</span>`,
    `<span class="mono tiny">${esc(dash(a.retryable))}</span>`,
    `<span class="tiny">${esc(a.request_json ? JSON.stringify(a.request_json).slice(0, 140) : "—")}</span>`,
  ]);
  put("attempts", `<div class="brain-title">Intentos de orden (order_send)</div>
    ${table(["hora", "retcode", "código", "reintentable", "petición"], attRows, "compact")}`);
}

/* -- panel 10: pie ---------------------------------------------------------- */

function renderFooter(ov) {
  const f = ov.footer || {};
  const c = f.counts || {};
  const s = ov.staleness || {};
  const sync = f.history_sync;
  text("footer", [
    `magic ${dash(f.magic)}`,
    `modo ${dash(f.mode)}`,
    `cuenta ${dash(f.account_login)}`,
    `cerradas 30 d: ${num(f.closed_trades_30d ?? c.trades, 0)}`,
    `feed ${fmtAge(s.bot_state?.age_s)}`,
    sync ? `historial: ${dash(sync.key)} · ${fmtTime(sync.last_sync_utc)}` : "historial: sin sincronizar",
    `filas: ${num(c.events, 0)} eventos · ${num(c.decisions, 0)} decisiones · ${num(c.equity_snapshots, 0)} equity · ${num(c.log_lines, 0)} log · ${num(c.order_attempts, 0)} intentos`,
  ].join("  ·  "));
}

/* -- ciclo de refresco ------------------------------------------------------ */

async function safeGet(path) {
  try {
    return { ok: true, data: await api(path) };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

async function refreshNow() {
  const [ov, brain, eq, daily, trades, ev, logs, cmds, att] = await Promise.all([
    safeGet("/api/overview"),
    safeGet("/api/brain?limit=12"),
    safeGet("/api/charts/equity?hours=24"),
    safeGet("/api/charts/daily-pnl?days=14"),
    safeGet(`/api/trades?days=${ui.tradesDays}`),
    safeGet("/api/events?limit=60"),
    safeGet("/api/logs?limit=160"),
    safeGet("/api/commands?limit=20"),
    safeGet("/api/order-attempts?limit=20"),
  ]);

  if (!ov.ok) {
    const node = document.getElementById("banner-degraded");
    node.classList.remove("hidden");
    node.textContent = `⚠ No se pudo leer /api/overview — ${ov.error}. El navegador no puede hablar con el proceso del dashboard.`;
    return;
  }
  renderHeader(ov.data);
  renderBanners(ov.data);
  renderKpis(ov.data);
  renderChips(ov.data);
  renderPositions(ov.data);
  renderFooter(ov.data);
  if (brain.ok) renderBrain(brain.data);
  if (eq.ok) renderEquity(eq.data);
  if (daily.ok) renderDaily(daily.data);
  if (trades.ok) renderTrades(trades.data);
  if (ev.ok) renderEvents(ev.data);
  if (logs.ok) renderLogs(logs.data);
  if (cmds.ok && att.ok) renderAudit(cmds.data, att.data);
  const failed = [brain, eq, daily, trades, ev, logs].filter((r) => !r.ok);
  if (failed.length) toast(`Paneles sin actualizar: ${failed.map((r) => r.error).join(" · ")}`, "error");
}

function startLoop() {
  clearTimeout(ui.timer);
  const tick = async () => {
    try {
      await refreshNow();
    } catch (err) {
      toast(`Refresco fallido — ${err.message}`, "error");
    } finally {
      ui.timer = setTimeout(tick, ui.refreshMs);
    }
  };
  tick();
}

/* -- arranque --------------------------------------------------------------- */

function wireControls() {
  const token = document.getElementById("token");
  token.value = sessionStorage.getItem("goldbot.token") || "";
  token.addEventListener("input", () => sessionStorage.setItem("goldbot.token", token.value.trim()));

  document.getElementById("btn-pause").addEventListener("click", () => sendCommand("pause"));
  document.getElementById("btn-resume").addEventListener("click", () => sendCommand("resume"));
  document.getElementById("btn-close-all").addEventListener("click", armCloseAll);
  document.getElementById("btn-reset-kill").addEventListener("click", armResetKill);

  document.querySelectorAll("#sec-trades .chip-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#sec-trades .chip-btn").forEach((b) => b.classList.remove("is-on"));
      btn.classList.add("is-on");
      ui.tradesDays = Number(btn.dataset.days);
      api(`/api/trades?days=${ui.tradesDays}`).then(renderTrades).catch((e) => toast(String(e), "error"));
    });
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") disarmCloseAll();
  });

  let resizeT = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => {
      ui.charts.equity?.setSize({ width: document.getElementById("chart-equity").clientWidth, height: 220 });
      ui.charts.daily?.setSize({ width: document.getElementById("chart-daily").clientWidth, height: 130 });
    }, 200);
  });
}

function boot() {
  wireControls();
  buildEquityChart();
  buildDailyChart();
  startLoop();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
