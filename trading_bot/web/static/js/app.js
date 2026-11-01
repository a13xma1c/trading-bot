/** Trading Bot single-page application. */

import { CandleChart, drawEquityCurve } from "./chart.js";
import { api, askToken, beep, confirmDialog, connectEvents, el, fmt, fmtSeconds, fmtSigned, fmtTime, icon, notifyBrowser, showSnackbar } from "./ui.js";

const $ = (id) => document.getElementById(id);

const state = {
  status: null,
  watchlist: [],
  selected: null,
  strategies: [],
  editing: null,          // name of the strategy loaded in the editor (null = new)
  editorTouched: false,   // user started a new strategy (do not auto-load the first one)
  overlayStrategy: "",
  settings: null,
  lastBacktest: null,
  signalsCount: 0,
};

const chart = new CandleChart($("chartCanvas"));
window.__app = { state, chart };  // introspection hook for end-to-end tests

// =========================================================================
// Navigation
// =========================================================================

function showView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  location.hash = name;
  if (name === "positions") loadPositions();
  if (name === "settings") loadSettings();
  if (name === "dashboard") chart.draw();
}

document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

// =========================================================================
// Status / top app bar
// =========================================================================

function applyStatus(status) {
  state.status = status;
  const running = status.running;
  $("engineStateChip").className = `chip ${running ? "on" : "off"}`;
  $("engineStateText").textContent = running ? "Engine running" : "Engine stopped";
  $("startButtonLabel").textContent = running ? "Stop" : "Start";
  $("startButton").querySelector("use").setAttribute("href", running ? "#i-stop" : "#i-play");
  const market = $("marketChip");
  if (status.market_open) {
    market.className = "chip on";
    $("marketChipText").textContent = `Market open · refresh every ${fmtSeconds(status.refresh_interval)}`;
  } else {
    market.className = "chip warn";
    const when = status.next_market_open ? new Date(status.next_market_open) : null;
    $("marketChipText").textContent = `Market closed · opens ${when ? when.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "?"} ${status.market_timezone}`;
  }
  $("budgetLabel").textContent = `${status.provider} · ${status.budget.text}`;
}

async function refreshStatus() {
  applyStatus(await api.get("/api/status"));
}

$("startButton").addEventListener("click", async () => {
  try {
    const status = state.status?.running ? await api.post("/api/engine/stop") : await api.post("/api/engine/start");
    applyStatus(status);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

$("scanNowButton").addEventListener("click", async () => {
  try {
    await api.post("/api/engine/scan");
    showSnackbar("Refreshing all symbols…", { duration: 2500 });
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

// =========================================================================
// Watchlist
// =========================================================================

function renderWatchlist() {
  const tbody = $("watchlistTable").querySelector("tbody");
  tbody.replaceChildren();
  if (!state.watchlist.length) {
    tbody.append(el("tr", { class: "empty" }, el("td", { colspan: 5, text: "Add a symbol to get started" })));
    return;
  }
  for (const row of state.watchlist) {
    const changeCls = row.change_pct == null ? "" : row.change_pct >= 0 ? "up" : "down";
    let updated = row.time ? fmtTime(row.time) + (row.stale ? " (stale)" : "") : "-";
    let updatedCls = "";
    if (row.error) { updated = row.time ? `${fmtTime(row.time)} (error)` : "error"; updatedCls = "error-text"; }
    const tr = el("tr", { class: `clickable${row.symbol === state.selected ? " selected" : ""}`, "data-symbol": row.symbol, title: row.error || "" },
      el("td", { class: "label-large", text: row.symbol }),
      el("td", { class: "num", text: row.price == null ? "-" : fmt(row.price) }),
      el("td", { class: `num ${changeCls}`, text: row.change_pct == null ? "-" : fmtSigned(row.change_pct, 2, "%") }),
      el("td", { class: `${updatedCls}`, text: updated }),
      el("td", { class: "actions" }, el("button", { class: "icon-btn remove-symbol", title: `Remove ${row.symbol}`, "aria-label": `Remove ${row.symbol}`,
        onclick: (e) => { e.stopPropagation(); removeSymbol(row.symbol); } }, icon("i-close", "icon small"))),
    );
    tr.addEventListener("click", () => selectSymbol(row.symbol));
    tbody.append(tr);
  }
}

async function loadWatchlist() {
  state.watchlist = await api.get("/api/watchlist");
  if (!state.selected || !state.watchlist.some((r) => r.symbol === state.selected)) {
    state.selected = state.watchlist[0]?.symbol || null;
    if (state.selected) await loadBars(state.selected);
  }
  renderWatchlist();
}

$("watchlistForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("watchlistSymbolInput");
  const symbol = input.value.trim().toUpperCase();
  if (!symbol) return;
  try {
    const result = await api.post("/api/watchlist", { symbol });
    input.value = "";
    if (!result.added) { showSnackbar(`${symbol} is already in the watchlist`, { duration: 3000 }); return; }
    await loadWatchlist();
    selectSymbol(symbol);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

async function removeSymbol(symbol) {
  try {
    await api.delete(`/api/watchlist/${encodeURIComponent(symbol)}`);
    if (state.selected === symbol) state.selected = null;
    await loadWatchlist();
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

// =========================================================================
// Chart
// =========================================================================

async function selectSymbol(symbol) {
  state.selected = symbol;
  renderWatchlist();
  await loadBars(symbol);
}

async function loadBars(symbol) {
  $("chartTitle").textContent = symbol;
  try {
    const data = await api.get(`/api/bars/${encodeURIComponent(symbol)}`);
    if (state.selected !== symbol) return;
    chart.setData(symbol, data.bars);
    const row = state.watchlist.find((r) => r.symbol === symbol);
    if (row && row.price == null && data.bars.length) {
      const last = data.bars[data.bars.length - 1], prev = data.bars[data.bars.length - 2] || last;
      Object.assign(row, { price: last[4], change_pct: prev[4] ? (last[4] / prev[4] - 1) * 100 : 0, time: new Date(last[0]).toISOString(), stale: data.stale });
      renderWatchlist();
    }
    updateChartStatus(data);
    await applyOverlay();
  } catch (err) {
    chart.setData(symbol, []);
    $("chartStatusLabel").textContent = `ERROR: ${err.message}`;
    $("chartStatusLabel").className = "chart-status error-text";
  }
}

function updateChartStatus(data) {
  const parts = [];
  if (data.bars?.length) parts.push(`data as of ${fmtTime(new Date(data.bars[data.bars.length - 1][0]).toISOString(), true)}`);
  if (data.fetched_at) parts.push(`fetched ${fmtTime(new Date(data.fetched_at * 1000).toISOString(), true)}`);
  if (data.stale) parts.push("stale");
  const error = data.error || state.status?.errors_by_symbol?.[data.symbol];
  if (error) parts.push(`ERROR: ${error}`);
  const label = $("chartStatusLabel");
  label.textContent = parts.join(" · ");
  label.title = label.textContent;
  label.className = `chart-status${error ? " error-text" : ""}`;
}

async function applyOverlay() {
  const name = state.overlayStrategy;
  if (!name || !state.selected) {
    chart.setOverlay(null);
    await applySignalMarkers();
    return;
  }
  try {
    const overlay = await api.get(`/api/bars/${encodeURIComponent(state.selected)}/overlay/${encodeURIComponent(name)}`);
    chart.setMarkers({ buy: [], sell: [] });
    chart.setOverlay(overlay);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

async function applySignalMarkers() {
  if (!state.selected) return;
  try {
    const markers = await api.get(`/api/signals/markers/${encodeURIComponent(state.selected)}`);
    chart.setMarkers(markers);
  } catch { /* markers are optional */ }
}

$("overlayStrategySelect").addEventListener("change", (e) => { state.overlayStrategy = e.target.value; applyOverlay(); });
$("resetViewButton").addEventListener("click", () => chart.resetView());
$("refreshButton").addEventListener("click", async () => {
  if (!state.selected) return;
  try {
    $("chartStatusLabel").textContent = "refreshing…";
    await api.post(`/api/bars/${encodeURIComponent(state.selected)}/refresh`);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

function renderOverlayOptions() {
  const select = $("overlayStrategySelect");
  const current = state.overlayStrategy;
  select.replaceChildren(el("option", { value: "", text: "None" }));
  for (const s of state.strategies) select.append(el("option", { value: s.name, text: s.name }));
  select.value = state.strategies.some((s) => s.name === current) ? current : "";
  state.overlayStrategy = select.value;
}

// =========================================================================
// Signals
// =========================================================================

function signalRow(signal) {
  const action = signal.action;
  return el("tr", { "data-signal-id": signal.id },
    el("td", { text: fmtTime(signal.created_at, true) }),
    el("td", { class: "label-large", text: signal.symbol }),
    el("td", {}, el("span", { class: `pill ${action.toLowerCase()}`, text: action })),
    el("td", { class: "num", text: fmt(signal.price) }),
    el("td", { text: signal.strategy }),
    el("td", { text: signal.executed ? "yes" : "no" }),
    el("td", { class: "muted", text: signal.reason || "" }),
  );
}

async function loadSignals() {
  const rows = await api.get("/api/signals?limit=200");
  const tbody = $("signalsTable").querySelector("tbody");
  tbody.replaceChildren(...rows.map(signalRow));
  state.signalsCount = rows.length;
  $("signalsCount").textContent = rows.length ? `${rows.length} recent` : "No signals yet";
}

function addSignal(signal) {
  const tbody = $("signalsTable").querySelector("tbody");
  tbody.prepend(signalRow(signal));
  state.signalsCount += 1;
  $("signalsCount").textContent = `${state.signalsCount} recent`;
  const title = `${signal.executed ? "Executed" : "Trade signal"}: ${signal.headline}`;
  showSnackbar(`${title} — ${signal.strategy}`, { kind: `signal ${signal.action.toLowerCase()}`, duration: 10000, action: "Show",
    onAction: () => { showView("dashboard"); selectSymbol(signal.symbol); } });
  const notifications = state.settings?.notifications;
  if (!notifications || notifications.desktop) notifyBrowser(title, signal.description);
  if (!notifications || notifications.sound) beep();
  if (signal.symbol === state.selected && !state.overlayStrategy) applySignalMarkers();
}

// =========================================================================
// Strategies
// =========================================================================

function renderStrategyList() {
  const list = $("strategyList");
  list.replaceChildren();
  for (const s of state.strategies) {
    const toggle = el("label", { class: "switch", title: "Enabled for live scanning", onclick: (e) => e.stopPropagation() },
      el("input", { type: "checkbox", class: "strategy-enabled", "data-name": s.name, onchange: (e) => setEnabled(s.name, e.target.checked) }),
      el("span", { class: "track" }));
    toggle.querySelector("input").checked = !!s.enabled;
    const item = el("div", { class: `list-item${state.editing === s.name ? " selected" : ""}`, "data-name": s.name, role: "button", tabindex: 0 },
      el("div", { class: "text" }, el("span", { class: "label-large", text: s.name }),
        el("span", { class: "supporting", text: (s.kind === "plugin" ? "Python plugin · " : "") + (s.description || s.buy_rule || "") })),
      toggle);
    item.addEventListener("click", () => loadIntoEditor(s.name));
    item.addEventListener("keydown", (e) => { if (e.key === "Enter") loadIntoEditor(s.name); });
    list.append(item);
  }
}

async function loadStrategies() {
  state.strategies = await api.get("/api/strategies");
  renderStrategyList();
  renderOverlayOptions();
  renderBacktestSymbols();
  if (state.editing === null && !state.editorTouched && state.strategies.length) loadIntoEditor(state.strategies[0].name);
}

async function setEnabled(name, enabled) {
  try {
    await api.patch(`/api/strategies/${encodeURIComponent(name)}`, { enabled });
    const s = state.strategies.find((x) => x.name === name);
    if (s) s.enabled = enabled;
    if (state.editing === name) $("strategyEnabledSwitch").checked = enabled;
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
    await loadStrategies();
  }
}

function setEditorEnabled(enabled) {
  $("strategyForm").querySelectorAll("input, textarea, button").forEach((node) => { node.disabled = !enabled; });
}

function loadIntoEditor(name) {
  const s = state.strategies.find((x) => x.name === name);
  if (!s) return;
  state.editing = name;
  renderStrategyList();
  fillEditor(s);
  $("editorTitle").textContent = s.kind === "plugin" ? `${s.name} (Python plugin)` : s.name;
  setEditorEnabled(s.kind !== "plugin");
  setEditorStatus(s.kind === "plugin" ? "Python plugin strategies are edited in their source file." : "", "");
}

function fillEditor(s) {
  $("strategyNameInput").value = s.name || "";
  $("strategyDescriptionInput").value = s.description || "";
  $("strategySymbolsInput").value = (s.symbols || []).join(", ");
  $("buyRuleInput").value = s.buy_rule || "";
  $("sellRuleInput").value = s.sell_rule || "";
  $("stopLossInput").value = s.stop_loss_pct || 0;
  $("takeProfitInput").value = s.take_profit_pct || 0;
  $("cooldownInput").value = s.cooldown_bars || 0;
  $("strategyEnabledSwitch").checked = s.enabled !== false;
  const tbody = $("paramsTable").querySelector("tbody");
  tbody.replaceChildren();
  for (const [key, value] of Object.entries(s.params || {})) addParamRow(key, value);
}

function addParamRow(key = "", value = "") {
  const tbody = $("paramsTable").querySelector("tbody");
  const tr = el("tr", { class: "param-row" },
    el("td", {}, el("input", { class: "param-name", value: key, placeholder: "name", "aria-label": "Parameter name" })),
    el("td", {}, el("input", { class: "param-value", value: value, placeholder: "value", "aria-label": "Parameter value" })),
    el("td", {}, el("button", { type: "button", class: "icon-btn", title: "Remove parameter", onclick: () => tr.remove() }, icon("i-close", "icon small"))));
  tbody.append(tr);
  return tr;
}

function readEditor() {
  const params = {};
  for (const row of $("paramsTable").querySelectorAll("tr.param-row")) {
    const key = row.querySelector(".param-name").value.trim();
    const raw = row.querySelector(".param-value").value.trim();
    if (!key) continue;
    const value = Number(raw);
    if (raw === "" || Number.isNaN(value)) throw new Error(`Parameter '${key}' must be numeric`);
    params[key] = value;
  }
  return {
    name: $("strategyNameInput").value.trim(),
    description: $("strategyDescriptionInput").value.trim(),
    symbols: $("strategySymbolsInput").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
    buy_rule: $("buyRuleInput").value.trim(),
    sell_rule: $("sellRuleInput").value.trim(),
    params,
    enabled: $("strategyEnabledSwitch").checked,
    stop_loss_pct: Number($("stopLossInput").value || 0),
    take_profit_pct: Number($("takeProfitInput").value || 0),
    cooldown_bars: Number($("cooldownInput").value || 0),
  };
}

function setEditorStatus(text, kind) {
  const label = $("strategyStatusLabel");
  label.textContent = text;
  label.className = `body-medium ${kind === "error" ? "error-text" : kind === "ok" ? "success-text" : "muted"}`;
}

async function validateStrategy() {
  let body;
  try { body = readEditor(); } catch (err) { setEditorStatus(err.message, "error"); return null; }
  try {
    const result = await api.post("/api/strategies/validate", { ...body, symbol: state.selected });
    if (!result.valid) { setEditorStatus(result.problems.join("\n"), "error"); return null; }
    setEditorStatus(result.bars ? `Valid. On ${result.bars} bars of ${result.symbol}: ${result.buy} buy and ${result.sell} sell signals.`
                                 : "Valid (no market data loaded yet to test against).", "ok");
    return body;
  } catch (err) {
    setEditorStatus(err.message, "error");
    return null;
  }
}

$("validateStrategyButton").addEventListener("click", validateStrategy);

$("strategyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  let body;
  try { body = readEditor(); } catch (err) { setEditorStatus(err.message, "error"); return; }
  try {
    await api.post("/api/strategies", { ...body, previous_name: state.editing });
    state.editing = body.name;
    await loadStrategies();
    loadIntoEditor(body.name);
    setEditorStatus(`Saved '${body.name}'.`, "ok");
  } catch (err) {
    setEditorStatus(err.message, "error");
  }
});

$("newStrategyButton").addEventListener("click", () => {
  state.editing = null;
  state.editorTouched = true;
  renderStrategyList();
  fillEditor({ name: "", buy_rule: "", params: {}, enabled: true });
  $("editorTitle").textContent = "New strategy";
  setEditorEnabled(true);
  setEditorStatus("Fill in the form and click Save.", "");
  $("strategyNameInput").focus();
});

$("duplicateStrategyButton").addEventListener("click", () => {
  let body;
  try { body = readEditor(); } catch (err) { setEditorStatus(err.message, "error"); return; }
  state.editing = null;
  renderStrategyList();
  fillEditor({ ...body, name: `${body.name} copy` });
  $("editorTitle").textContent = "New strategy";
  setEditorEnabled(true);
  setEditorStatus("Duplicated; adjust and Save.", "");
});

$("deleteStrategyButton").addEventListener("click", async () => {
  const name = state.editing;
  if (!name) return;
  if (!(await confirmDialog("Delete strategy", `Delete '${name}'? This cannot be undone.`, { okLabel: "Delete", danger: true }))) return;
  try {
    await api.delete(`/api/strategies/${encodeURIComponent(name)}`);
    state.editing = null;
    await loadStrategies();
    fillEditor({ name: "", buy_rule: "", params: {}, enabled: true });
    $("editorTitle").textContent = "Strategy editor";
    setEditorStatus(`Deleted '${name}'.`, "");
  } catch (err) {
    setEditorStatus(err.message, "error");
  }
});

$("addParamButton").addEventListener("click", () => addParamRow().querySelector(".param-name").focus());

$("showOnChartButton").addEventListener("click", async () => {
  const body = await validateStrategy();
  if (!body) return;
  if (!state.strategies.some((s) => s.name === body.name) || state.editing !== body.name) {
    setEditorStatus("Save the strategy first to show it on the chart.", "error");
    return;
  }
  state.overlayStrategy = body.name;
  $("overlayStrategySelect").value = body.name;
  showView("dashboard");
  await applyOverlay();
});

async function loadRulesHelp() {
  const help = await api.get("/api/strategies/help");
  const box = $("rulesHelp");
  box.replaceChildren(
    el("p", { html: "Rules are boolean expressions over the bar series <b>open, high, low, close, volume</b> and the functions below. Parameters defined in the table can be used by name." }),
    el("p", { html: `Operators: <span class="code">${help.operators.join(" ")}</span>` }),
    el("p", { html: "Examples:<br><span class='code'>crossover(sma(fast), sma(slow))</span><br><span class='code'>rsi(14) &lt; 30 and close &gt; ema(200)</span><br><span class='code'>close &lt; bb_lower(20, 2) and volume &gt; sma(20, volume) * 1.5</span>" }),
    el("table", { class: "help-table" }, el("tbody", {}, ...help.functions.map((f) => el("tr", {}, el("td", {}, el("code", { text: f.signature })), el("td", { text: f.description }))))),
  );
}

// =========================================================================
// Backtest
// =========================================================================

function renderBacktestSymbols() {
  const select = $("backtestSymbolSelect");
  const current = select.value;
  select.replaceChildren(...state.watchlist.map((r) => el("option", { value: r.symbol, text: r.symbol })));
  select.value = state.watchlist.some((r) => r.symbol === current) ? current : (state.selected || state.watchlist[0]?.symbol || "");
}

$("backtestButton").addEventListener("click", async () => {
  const body = await validateStrategy();
  if (!body) return;
  renderBacktestSymbols();
  $("backtestTitle").textContent = `Backtest — ${body.name || "unsaved strategy"}`;
  $("backtestStatus").textContent = "";
  $("backtestStatsTable").querySelector("tbody").replaceChildren();
  $("backtestTradesTable").querySelector("tbody").replaceChildren();
  state.lastBacktest = null;
  $("backtestDialog").showModal();
});

$("backtestRunButton").addEventListener("click", async () => {
  let body;
  try { body = readEditor(); } catch (err) { $("backtestStatus").textContent = err.message; return; }
  const symbol = $("backtestSymbolSelect").value;
  $("backtestStatus").textContent = "Running…";
  try {
    const result = await api.post("/api/backtest", { symbol, definition: body, cash: Number($("backtestCashInput").value || 10000),
                                                     commission: Number($("backtestCommissionInput").value || 0) });
    state.lastBacktest = { symbol, ...result };
    renderBacktest(result);
    $("backtestStatus").textContent = `${result.summary.trades} trades, ${fmtSigned(result.summary.total_return_pct, 2, "%")}`;
  } catch (err) {
    $("backtestStatus").textContent = err.message;
  }
});

function renderBacktest(result) {
  const stats = $("backtestStatsTable").querySelector("tbody");
  stats.replaceChildren(...Object.entries(result.summary).map(([k, v]) => el("tr", {}, el("td", { text: k.replace(/_/g, " ") }), el("td", { class: "num", text: String(v) }))));
  const trades = $("backtestTradesTable").querySelector("tbody");
  trades.replaceChildren(...result.trades.map((t) => el("tr", {},
    el("td", { text: fmtTime(t.entry_time, true) }), el("td", { text: fmtTime(t.exit_time, true) }),
    el("td", { class: "num", text: fmt(t.entry_price) }), el("td", { class: "num", text: fmt(t.exit_price) }),
    el("td", { class: `num ${t.pnl >= 0 ? "up" : "down"}`, text: fmtSigned(t.pnl) }),
    el("td", { class: `num ${t.return_pct >= 0 ? "up" : "down"}`, text: fmtSigned(t.return_pct, 2, "%") }),
    el("td", { text: t.exit_reason }))));
  drawEquityCurve($("equityCanvas"), result.equity);
}

const closeBacktest = () => $("backtestDialog").close();
$("backtestCloseButton").addEventListener("click", closeBacktest);
$("backtestDoneButton").addEventListener("click", closeBacktest);
$("backtestShowButton").addEventListener("click", async () => {
  const bt = state.lastBacktest;
  if (!bt) return;
  closeBacktest();
  showView("dashboard");
  if (state.selected !== bt.symbol) await selectSymbol(bt.symbol);
  state.overlayStrategy = "";
  $("overlayStrategySelect").value = "";
  chart.setOverlay(null);
  chart.setMarkers({ buy: bt.buy, sell: bt.sell });
  showSnackbar(`Backtest ${bt.summary.strategy} on ${bt.symbol}: ${fmtSigned(bt.summary.total_return_pct, 2, "%")} over ${bt.summary.trades} trades`, { duration: 8000 });
});

// =========================================================================
// Positions
// =========================================================================

async function loadPositions() {
  const data = await api.get("/api/positions");
  const a = data.account;
  $("accountSummary").replaceChildren(
    el("dt", { text: "Cash" }), el("dd", { id: "accountCash", text: fmt(a.cash) }),
    el("dt", { text: "Equity" }), el("dd", { id: "accountEquity", text: fmt(a.equity) }),
    el("dt", { text: "Open positions" }), el("dd", { id: "accountOpenPositions", text: String(a.open_positions) }),
    el("dt", { text: "Realized P&L" }), el("dd", { class: a.realized_pnl >= 0 ? "up" : "down", text: fmtSigned(a.realized_pnl) }),
    el("dt", { text: "Mode" }), el("dd", { text: a.paper ? "Paper trading" : "Live" }),
  );
  const tbody = $("positionsTable").querySelector("tbody");
  tbody.replaceChildren(...data.positions.map((p) => el("tr", { "data-symbol": p.symbol },
    el("td", { class: "label-large", text: p.symbol }), el("td", { class: "num", text: String(p.quantity) }),
    el("td", { class: "num", text: fmt(p.avg_price) }), el("td", { class: "num", text: fmt(p.last) }),
    el("td", { class: `num ${p.pnl >= 0 ? "up" : "down"}`, text: fmtSigned(p.pnl) }),
    el("td", { class: `num ${p.pnl >= 0 ? "up" : "down"}`, text: fmtSigned(p.pnl_pct, 2, "%") }),
    el("td", { class: "num", text: p.stop_loss ? fmt(p.stop_loss) : "-" }), el("td", { class: "num", text: p.take_profit ? fmt(p.take_profit) : "-" }),
    el("td", { text: p.strategy }))));
  if (!data.positions.length) tbody.append(el("tr", { class: "empty" }, el("td", { colspan: 9, text: "No open positions" })));
  const fills = $("fillsTable").querySelector("tbody");
  fills.replaceChildren(...data.fills.map((f) => el("tr", {},
    el("td", { text: fmtTime(f.timestamp, true) }), el("td", { text: f.symbol }), el("td", {}, el("span", { class: `pill ${f.side.toLowerCase()}`, text: f.side })),
    el("td", { class: "num", text: String(f.quantity) }), el("td", { class: "num", text: fmt(f.price) }), el("td", { text: f.strategy || "" }),
    el("td", { class: `num ${f.realized_pnl >= 0 ? "up" : "down"}`, text: fmtSigned(f.realized_pnl) }))));
}

$("resetPaperAccountButton").addEventListener("click", async () => {
  if (!(await confirmDialog("Reset paper account", "Close all paper positions and restore the initial cash?", { okLabel: "Reset", danger: true }))) return;
  try {
    await api.post("/api/account/reset");
    await loadPositions();
    showSnackbar("Paper account reset", { duration: 3000 });
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

// =========================================================================
// Settings
// =========================================================================

async function loadSettings() {
  const s = await api.get("/api/settings");
  state.settings = s;
  const provider = $("settingsProvider");
  provider.replaceChildren(...s.options.providers.map((p) => el("option", { value: p, text: p })));
  provider.value = s.data.provider;
  const interval = $("settingsInterval");
  interval.replaceChildren(...s.options.intervals.map((p) => el("option", { value: p, text: p })));
  interval.value = s.data.interval;
  $("settingsApiKey").value = "";
  $("apiKeyNote").textContent = s.data.api_key_set ? "A key is stored. Leave blank to keep it; or set TRADING_BOT_API_KEY." : "No key stored. Required for alphavantage / twelvedata.";
  $("settingsBars").value = s.data.bars;
  $("settingsScanInterval").value = s.scan_interval;
  const budget = s.options.budgets[s.data.provider] || { per_minute: 5, per_day: 25 };
  $("settingsPerMinute").value = s.data.requests_per_minute || budget.per_minute;
  $("settingsPerDay").value = s.data.requests_per_day || budget.per_day;
  $("settingsClosedBarsOnly").checked = !!s.data.closed_bars_only;
  $("settingsMarketHoursEnabled").checked = !!s.market_hours.enabled;
  $("settingsTimezone").value = s.market_hours.timezone;
  $("settingsOpen").value = s.market_hours.open;
  $("settingsClose").value = s.market_hours.close;
  $("settingsDesktop").checked = !!s.notifications.desktop;
  $("settingsSound").checked = !!s.notifications.sound;
  $("settingsWebhook").value = s.notifications.webhook_url || "";
  $("settingsTradingEnabled").checked = !!s.trading.trading_enabled;
  $("settingsInitialCash").value = s.trading.initial_cash;
  $("settingsPositionSize").value = Math.round(s.trading.position_size_pct * 100);
  $("settingsMaxPositions").value = s.trading.max_positions;
  updateProviderNote();
}

function updateProviderNote() {
  const provider = $("settingsProvider").value;
  const budget = state.settings?.options.budgets[provider];
  $("providerNote").textContent = budget ? `Free tier: ${budget.per_minute} requests/min, ${budget.per_day}/day. Provider and API key changes take effect after restarting the server.` : "";
}
$("settingsProvider").addEventListener("change", updateProviderNote);

$("settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    scan_interval: Number($("settingsScanInterval").value),
    data: {
      provider: $("settingsProvider").value, api_key: $("settingsApiKey").value.trim(), interval: $("settingsInterval").value,
      bars: Number($("settingsBars").value), requests_per_minute: Number($("settingsPerMinute").value),
      requests_per_day: Number($("settingsPerDay").value), closed_bars_only: $("settingsClosedBarsOnly").checked,
    },
    market_hours: { enabled: $("settingsMarketHoursEnabled").checked, timezone: $("settingsTimezone").value.trim(),
                    open: $("settingsOpen").value.trim(), close: $("settingsClose").value.trim() },
    notifications: { desktop: $("settingsDesktop").checked, sound: $("settingsSound").checked, webhook_url: $("settingsWebhook").value.trim() },
    trading: { trading_enabled: $("settingsTradingEnabled").checked, initial_cash: Number($("settingsInitialCash").value),
               position_size_pct: Number($("settingsPositionSize").value) / 100, max_positions: Number($("settingsMaxPositions").value) },
  };
  const status = $("settingsStatus");
  try {
    const result = await api.put("/api/settings", body);
    applyStatus(result.status);
    await loadSettings();
    status.className = "body-medium success-text";
    status.textContent = result.restart_required ? "Settings saved. Restart the server to apply the new provider / API key." : "Settings saved.";
    showSnackbar("Settings saved", { duration: 3000 });
  } catch (err) {
    status.className = "body-medium error-text";
    status.textContent = err.message;
  }
});

// =========================================================================
// Live events
// =========================================================================

function onEvent(message) {
  switch (message.type) {
    case "hello":
      applyStatus(message.status);
      break;
    case "bars": {
      const row = state.watchlist.find((r) => r.symbol === message.symbol);
      if (row && message.quote) Object.assign(row, { price: message.quote.price, change_pct: message.quote.change_pct, time: message.quote.time,
                                                     stale: message.stale, error: message.error || null });
      if (row) renderWatchlist(); else if (state.watchlist.length) loadWatchlist();
      if (message.symbol === state.selected) loadBars(message.symbol);
      break;
    }
    case "signal":
      addSignal(message.signal);
      break;
    case "fetch_failed": {
      const row = state.watchlist.find((r) => r.symbol === message.symbol);
      if (row) row.error = message.message;
      renderWatchlist();
      if (message.symbol === state.selected) {
        const label = $("chartStatusLabel");
        label.textContent = `${message.has_cache ? label.textContent.replace(/ · ERROR.*$/, "") + " · " : ""}ERROR: ${message.message}`;
        label.className = "chart-status error-text";
        label.title = label.textContent;
      }
      showSnackbar(`${message.symbol}: ${message.message}`, { kind: "error", duration: 8000 });
      break;
    }
    case "status":
      if (message.level === "error") showSnackbar(message.message, { kind: "error", duration: 8000 });
      break;
    case "scan":
      applyStatus(message.status);
      if (state.watchlist.some((r) => r.price == null)) loadWatchlist();
      break;
    case "engine":
      applyStatus(message.status);
      break;
    case "positions":
      if ($("view-positions").classList.contains("active")) loadPositions();
      break;
    default:
      break;
  }
}

// =========================================================================
// Boot
// =========================================================================

async function boot() {
  try {
    await refreshStatus();
  } catch (err) {
    if (err.status === 401) {
      api.setToken(await askToken());
      return boot();
    }
    showSnackbar(`Cannot reach the server: ${err.message}`, { kind: "error" });
    return;
  }
  connectEvents(onEvent, (connected) => { if (!connected) $("progress").classList.add("active"); else $("progress").classList.remove("active"); });
  await Promise.all([loadStrategies(), loadRulesHelp()]);
  await loadWatchlist();
  renderBacktestSymbols();
  await loadSignals();
  const view = location.hash.replace("#", "");
  if (["dashboard", "strategies", "positions", "settings"].includes(view)) showView(view);
  if (state.status?.requires_api_key) {
    showSnackbar(`The ${state.status.provider} provider needs an API key - set it under Settings.`, { kind: "error", duration: 12000, action: "Settings",
      onAction: () => showView("settings") });
  }
}

boot();
