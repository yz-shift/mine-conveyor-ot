"use strict";

const TREND_POINTS = 600; // one per poll, so ten minutes
const STALE_AFTER_MS = 3000;
const LOG_REFRESH_MS = 5000;
const LOG_ROWS = 100;
const RECONNECT_MS = 1000;
const CHART_WAIT_MS = 5000;

const $ = (id) => document.getElementById(id);

let config = null;
let lastMessageAt = 0;
let lastTimestamp = null; // timestamp of the last poll shown; a repeat of it is not new data
let speedFilled = false;
const tiles = new Map(); // reading name -> {box, value}
const trends = []; // {chart, signals, thresholds}

/* Text from the server only ever goes in through textContent, never as HTML. */
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function title(name) {
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function clockTime(unixSeconds) {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour12: false });
}

function decimals(scale) {
  return Math.round(Math.log10(scale));
}

function showMessage(text) {
  $("message").textContent = text;
  $("message").hidden = !text;
}

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const reply = await response.json();
  if (!response.ok) {
    const detail = typeof reply.detail === "string" ? reply.detail : `request failed (${response.status})`;
    throw new Error(detail);
  }
  return reply;
}

/* Every operator action goes through here, so a failure is always shown and the logs always catch up. */
async function act(path, body) {
  try {
    await api(path, "POST", body);
    showMessage("");
  } catch (error) {
    showMessage(error.message);
  }
  refreshLogs();
}

/* ---------- Building the page from /api/config ---------- */

function buildTiles() {
  for (const reading of config.readings) {
    const box = element("div", undefined, "tile");
    const value = element("span", "—", "value");
    box.append(element("div", title(reading.name), "name"), value, element("span", reading.unit, "unit"));
    box.title = reading.description;
    $("tiles").append(box);
    tiles.set(reading.name, { box, value, digits: decimals(reading.scale) });
  }
}

function buildFaults() {
  for (const fault of config.faults) {
    const label = element("label");
    const box = element("input");
    box.type = "checkbox";
    box.id = `fault-${fault}`;
    box.addEventListener("change", () => act(`/api/faults/${fault}`, { active: box.checked }));
    label.append(box, element("span", title(fault)));
    $("faults").append(label);
  }
}

const LINE_COLOURS = ["#1f4e79", "#7a7f85"];
const LIMIT_COLOURS = { warning: "#c79400", critical: "#c8201e" };

function lineDataset(label, colour, dashed) {
  return {
    label,
    data: [],
    borderColor: colour,
    borderWidth: dashed ? 1 : 1.5,
    borderDash: dashed ? [5, 4] : [],
    pointRadius: 0,
    tension: 0,
    spanGaps: false, // a loss of communication shows as a gap, not a straight line across it
  };
}

function buildTrend(unit, readings) {
  const figure = element("div", undefined, "trend");
  const plot = element("div", undefined, "plot");
  const canvas = element("canvas");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", `Trend of ${readings.map((r) => title(r.name)).join(" and ")}`);
  plot.append(canvas);
  figure.append(element("h3", `${readings.map((r) => title(r.name)).join(" / ")} (${unit})`), plot);
  $("trends").append(figure);

  const signals = readings.map((r) => r.name);
  const thresholds = config.alarms.filter((a) => signals.includes(a.signal));
  const datasets = [
    ...readings.map((r, i) => lineDataset(title(r.name), LINE_COLOURS[i % LINE_COLOURS.length], false)),
    ...thresholds.map((a) => lineDataset(`${a.severity} limit`, LIMIT_COLOURS[a.severity], true)),
  ];
  const chart = new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      animation: false,
      parsing: false,
      normalized: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 14, boxHeight: 1, font: { size: 11 } } },
        tooltip: { callbacks: { title: (items) => clockTime(items[0].parsed.x) } },
      },
      scales: {
        x: { type: "linear", ticks: { maxTicksLimit: 6, callback: clockTime, font: { size: 11 } } },
        y: { beginAtZero: true, ticks: { font: { size: 11 } } },
      },
    },
  });
  trends.push({ chart, signals, thresholds });
}

function buildTrends() {
  if (trends.length > 0) return;
  $("no-charts").hidden = true;
  const byUnit = new Map();
  for (const reading of config.readings) {
    if (!byUnit.has(reading.unit)) byUnit.set(reading.unit, []);
    byUnit.get(reading.unit).push(reading);
  }
  for (const [unit, readings] of byUnit) buildTrend(unit, readings);
}

/* Chart.js loads asynchronously from a CDN. Until it arrives, or if it never does, the rest of the page works. */
function buildTrendsWhenReady() {
  if (window.Chart) {
    buildTrends();
    return;
  }
  const script = $("chartjs");
  script.addEventListener("load", () => { if (window.Chart) buildTrends(); });
  script.addEventListener("error", () => { $("no-charts").hidden = false; });
  // The error event may already have fired, or the request may hang; say so after a while either way.
  setTimeout(() => { if (!window.Chart) $("no-charts").hidden = false; }, CHART_WAIT_MS);
}

/* ---------- Showing a state from the server ---------- */

function renderTiles(state) {
  const worst = new Map();
  for (const alarm of state.alarms) {
    if (!alarm.state.startsWith("active")) continue;
    const signal = config.alarms.find((a) => a.name === alarm.name).signal;
    if (alarm.severity === "critical" || !worst.has(signal)) worst.set(signal, alarm.severity);
  }
  for (const [name, tile] of tiles) {
    const value = state.readings ? state.readings[name] : null;
    tile.value.textContent = value === null ? "—" : value.toFixed(tile.digits);
    tile.box.className = `tile ${worst.get(name) || ""}`.trim();
  }
}

function renderStatus(state) {
  $("run-state").textContent = state.run_state ? title(state.run_state) : "Unknown";
  $("run-state").classList.toggle("bad", !state.run_state || state.run_state === "emergency_stop");
  $("link").textContent = state.connected ? "Online" : "Offline";
  $("link").classList.toggle("bad", !state.connected);
  const active = state.alarms.filter((a) => a.state.startsWith("active")).length;
  $("alarm-count").textContent = String(active);
  $("alarm-count").classList.toggle("bad", active > 0);
}

function renderControls(state) {
  const run = state.run_state;
  const stationary = state.readings && state.readings.belt_speed === 0 && state.readings.motor_speed === 0;
  $("start").disabled = run !== "stopped";
  $("stop").disabled = run !== "starting" && run !== "running";
  $("reset").disabled = run !== "emergency_stop" || !stationary;
  // Offered whenever the plant is reachable, whatever state it claims to be in.
  $("estop").disabled = !state.connected;
  $("speed-set").disabled = !state.connected;

  $("setpoint").textContent = state.setpoint === null ? "—" : `${state.setpoint.toFixed(2)} ${config.speed.unit}`;
  if (state.setpoint !== null && !speedFilled) {
    $("speed").value = state.setpoint.toFixed(1); // once only, so a poll never overwrites what is being typed
    speedFilled = true;
  }
  for (const fault of config.faults) {
    const box = $(`fault-${fault}`);
    box.disabled = !state.connected;
    if (state.injections) box.checked = state.injections[fault];
  }
}

const STATE_TEXT = {
  active_unacked: "Active, not acknowledged",
  active_acked: "Active, acknowledged",
  cleared_unacked: "Cleared, not acknowledged",
};

function alarmRow(alarm) {
  const row = element("tr");
  if (alarm.state.endsWith("unacked")) row.classList.add("unacked");
  if (alarm.state.startsWith("cleared")) row.classList.add("cleared");
  const severity = element("td");
  severity.append(element("span", title(alarm.severity), `badge ${alarm.severity}`));
  const action = element("td");
  if (alarm.state.endsWith("unacked")) {
    const ack = element("button", "Acknowledge");
    ack.type = "button";
    ack.addEventListener("click", () => act(`/api/alarms/${alarm.name}/ack`));
    action.append(ack);
  }
  const value = element("td", undefined, "number");
  alarmValueCells.set(alarm.name, value);
  row.append(severity, element("td", alarm.message), element("td", STATE_TEXT[alarm.state]),
             value, element("td", clockTime(alarm.raised_at)), action);
  return row;
}

let shownAlarms = "";
const alarmValueCells = new Map();

function renderAlarms(state) {
  // Rows are rebuilt only when an alarm changes state, so a button being clicked survives polls.
  const key = JSON.stringify(state.alarms.map((a) => [a.name, a.state, a.raised_at]));
  if (key !== shownAlarms) {
    shownAlarms = key;
    alarmValueCells.clear();
    $("alarms").replaceChildren(...state.alarms.map(alarmRow));
    $("no-alarms").hidden = state.alarms.length > 0;
    refreshLogs();
  }
  for (const alarm of state.alarms) alarmValueCells.get(alarm.name).textContent = alarm.value.toFixed(2);
}

function addTrendSample(state) {
  for (const { chart, signals, thresholds } of trends) {
    const previous = chart.data.datasets[0].data.at(-1);
    // A gap in the samples (the HMI backend was down, or this tab was asleep) is drawn as a gap.
    const gap = previous && state.timestamp - previous.x > STALE_AFTER_MS / 1000;
    const values = [
      ...signals.map((name) => (state.readings ? state.readings[name] : null)),
      ...thresholds.map((a) => a.threshold),
    ];
    chart.data.datasets.forEach((dataset, i) => {
      if (gap) dataset.data.push({ x: (previous.x + state.timestamp) / 2, y: null });
      dataset.data.push({ x: state.timestamp, y: values[i] });
      while (dataset.data.length > TREND_POINTS) dataset.data.shift();
    });
    chart.update("none");
  }
}

function render(state) {
  renderStatus(state);
  renderTiles(state);
  renderControls(state);
  renderAlarms(state);
}

/* When the HMI backend itself goes quiet the page must not keep showing its last values as if live. */
function renderNoBackend() {
  renderStatus({ run_state: null, connected: false, alarms: [] });
  $("alarm-count").textContent = "—"; // unknown, which is not the same as zero
  renderTiles({ readings: null, alarms: [] });
  renderControls({ run_state: null, connected: false, readings: null, setpoint: null, injections: null });
  showMessage("No data from the HMI backend. Values are blank until it returns.");
}

/* ---------- Logs ---------- */

function eventRow(event) {
  const row = element("tr");
  row.append(element("td", clockTime(event.timestamp)), element("td", event.message),
             element("td", title(event.kind)), element("td", event.value.toFixed(2), "number"));
  return row;
}

function operationRow(operation) {
  const row = element("tr");
  row.append(element("td", clockTime(operation.timestamp)), element("td", operation.action),
             element("td", operation.detail), element("td", operation.ok ? "Sent" : `Failed: ${operation.error}`));
  return row;
}

async function refreshLogs() {
  try {
    const [events, operations] = await Promise.all([
      api(`/api/alarm-events?limit=${LOG_ROWS}`),
      api(`/api/operations?limit=${LOG_ROWS}`),
    ]);
    $("events").replaceChildren(...events.map(eventRow));
    $("operations").replaceChildren(...operations.map(operationRow));
  } catch {
    // The stale-data banner already says the backend is gone.
  }
}

/* ---------- Live connection ---------- */

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket.addEventListener("message", (message) => {
    const state = JSON.parse(message.data);
    // An acknowledgement re-sends the last poll's readings with that poll's timestamp. Only a new
    // timestamp proves the backend is still polling, and only a new one goes on the trend.
    const fresh = state.timestamp !== lastTimestamp;
    if (fresh) {
      const wasStale = Date.now() - lastMessageAt > STALE_AFTER_MS;
      lastMessageAt = Date.now();
      lastTimestamp = state.timestamp;
      if (wasStale) showMessage("");
    }
    if (Date.now() - lastMessageAt > STALE_AFTER_MS) return; // an old snapshot must not make the page look live
    render(state);
    if (fresh) addTrendSample(state);
  });
  socket.addEventListener("close", () => setTimeout(connect, RECONNECT_MS));
}

function wireControls() {
  for (const command of ["start", "stop", "reset", "estop"]) {
    $(command).addEventListener("click", () => act(`/api/commands/${command}`));
  }
  $("speed").min = config.speed.min;
  $("speed").max = config.speed.max;
  $("speed-unit").textContent = `(${config.speed.unit})`;
  $("speed-form").addEventListener("submit", (event) => {
    event.preventDefault();
    act("/api/speed", { speed: Number($("speed").value) });
  });
}

async function loadConfig() {
  for (;;) {
    try {
      return await api("/api/config");
    } catch {
      showMessage("Waiting for the HMI backend…");
      await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS));
    }
  }
}

async function main() {
  setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false }); }, 1000);
  config = await loadConfig();
  showMessage("");
  buildTiles();
  buildFaults();
  buildTrendsWhenReady();
  wireControls();
  connect();
  refreshLogs();
  setInterval(refreshLogs, LOG_REFRESH_MS);
  setInterval(() => {
    if (Date.now() - lastMessageAt > STALE_AFTER_MS) renderNoBackend();
  }, 1000);
}

main();
