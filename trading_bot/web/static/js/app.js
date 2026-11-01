/** Trading Bot single-page application. */

import { attachSymbolAutocomplete } from "./autocomplete.js";
import { CandleChart, drawEquityCurve, drawLineSeries, drawPnlCurve } from "./chart.js";
import { attachPasswordFeedback, enhancePasswordFields } from "./passwords.js";
import { api, beep, confirmDialog, connectEvents, el, fmt, fmtSeconds, fmtSigned, fmtTime, icon, notifyBrowser, showSnackbar } from "./ui.js";

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
  user: null,          // {username, email, role, auto_trade, ...} from api/auth/status
  history: null,
  analytics: null,
  wallet: null,
  fx: { base: null, quote: null, days: 90, latest: null, currencies: [] },
  broker: null,          // api/broker/connection
  lastSimulation: null,
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
  if (name === "trade") loadTrade();
  if (name === "wallet") loadWallet();
  if (name === "fx") loadFx();
  if (name === "history") loadHistory();
  if (name === "insights") loadAnalytics();
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
  const banner = $("apiKeyBanner");
  banner.classList.toggle("hidden", !status.requires_api_key);
  if (status.requires_api_key) {
    $("apiKeyBannerText").textContent = `The ${status.provider} provider needs an API key before market data can be loaded. Enter it under Settings, or switch to demo data to explore the app.`;
  }
}

$("bannerSettingsButton").addEventListener("click", () => { showView("settings"); $("settingsApiKey").focus(); });
$("bannerUseDemoButton").addEventListener("click", async () => {
  try {
    const result = await api.post("api/settings/provider", { provider: "demo" });
    applyStatus(result.status);
    showSnackbar("Using demo market data", { duration: 4000 });
    await loadWatchlist();
    if (state.selected) await loadBars(state.selected);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

async function refreshStatus() {
  applyStatus(await api.get("api/status"));
}

$("startButton").addEventListener("click", async () => {
  try {
    const status = state.status?.running ? await api.post("api/engine/stop") : await api.post("api/engine/start");
    applyStatus(status);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

$("scanNowButton").addEventListener("click", async () => {
  try {
    await api.post("api/engine/scan");
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
    const tr = el("tr", { class: `clickable${row.symbol === state.selected ? " selected" : ""}`, "data-symbol": row.symbol, title: row.error || row.name || "" },
      el("td", { class: "label-large" }, el("span", { text: row.symbol }), row.name ? el("span", { class: "watchlist-name", text: row.name }) : null),
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
  state.watchlist = await api.get("api/watchlist");
  if (!state.selected || !state.watchlist.some((r) => r.symbol === state.selected)) {
    state.selected = state.watchlist[0]?.symbol || null;
    if (state.selected) await loadBars(state.selected);
  }
  renderWatchlist();
}

async function addSymbol(symbol) {
  const input = $("watchlistSymbolInput");
  symbol = symbol.trim().toUpperCase();
  if (!symbol) return;
  try {
    const result = await api.post("api/watchlist", { symbol });
    input.value = "";
    if (!result.added) { showSnackbar(`${symbol} is already in the watchlist`, { duration: 3000 }); return; }
    await loadWatchlist();
    selectSymbol(symbol);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

$("watchlistForm").addEventListener("submit", (e) => { e.preventDefault(); addSymbol($("watchlistSymbolInput").value); });
attachSymbolAutocomplete({ input: $("watchlistSymbolInput"), onSelect: (item) => addSymbol(item.symbol) });

async function removeSymbol(symbol) {
  try {
    await api.delete(`api/watchlist/${encodeURIComponent(symbol)}`);
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
  const name = state.watchlist.find((r) => r.symbol === symbol)?.name;
  $("chartTitle").textContent = symbol;
  $("chartTitle").title = name || "";
  $("chartName").textContent = name || "";
  try {
    const data = await api.get(`api/bars/${encodeURIComponent(symbol)}`);
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
    chart.setData(symbol, [], { message: `No data for ${symbol}` });
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
    const overlay = await api.get(`api/bars/${encodeURIComponent(state.selected)}/overlay/${encodeURIComponent(name)}`);
    chart.setMarkers({ buy: [], sell: [] });
    chart.setOverlay(overlay);
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

async function applySignalMarkers() {
  if (!state.selected) return;
  try {
    const markers = await api.get(`api/signals/markers/${encodeURIComponent(state.selected)}`);
    chart.setMarkers(markers);
  } catch { /* markers are optional */ }
}

$("overlayStrategySelect").addEventListener("change", (e) => { state.overlayStrategy = e.target.value; applyOverlay(); });
$("resetViewButton").addEventListener("click", () => chart.resetView());
$("refreshButton").addEventListener("click", async () => {
  if (!state.selected) return;
  try {
    $("chartStatusLabel").textContent = "refreshing…";
    await api.post(`api/bars/${encodeURIComponent(state.selected)}/refresh`);
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
  const rows = await api.get("api/signals?limit=200");
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
  state.strategies = await api.get("api/strategies");
  renderStrategyList();
  renderOverlayOptions();
  renderBacktestSymbols();
  if (state.editing === null && !state.editorTouched && state.strategies.length) loadIntoEditor(state.strategies[0].name);
}

async function setEnabled(name, enabled) {
  try {
    await api.patch(`api/strategies/${encodeURIComponent(name)}`, { enabled });
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
    const result = await api.post("api/strategies/validate", { ...body, symbol: state.selected });
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
    await api.post("api/strategies", { ...body, previous_name: state.editing });
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
    await api.delete(`api/strategies/${encodeURIComponent(name)}`);
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
  const help = await api.get("api/strategies/help");
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
    const result = await api.post("api/backtest", { symbol, definition: body, cash: Number($("backtestCashInput").value || 10000),
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
  const data = await api.get("api/positions");
  const a = data.account;
  $("positionsAccountName").textContent = a.name ? `${a.name} · ${a.broker?.label || ""} · ${a.auto_trade ? "auto-trading on" : "auto-trading off"}` : "";
  if (a.error) showSnackbar(`Broker unavailable: ${a.error}`, { kind: "error", duration: 8000 });
  $("accountSummary").replaceChildren(
    el("dt", { text: "Cash" }), el("dd", { id: "accountCash", text: a.cash == null ? "-" : fmt(a.cash) }),
    el("dt", { text: "Equity" }), el("dd", { id: "accountEquity", text: a.equity == null ? "-" : fmt(a.equity) }),
    el("dt", { text: "Open positions" }), el("dd", { id: "accountOpenPositions", text: String(a.open_positions) }),
    el("dt", { text: "Realized P&L" }), el("dd", { class: a.realized_pnl >= 0 ? "up" : "down", text: a.realized_pnl == null ? "-" : fmtSigned(a.realized_pnl) }),
    el("dt", { text: "Venue" }), el("dd", { text: `${a.broker?.label || (a.paper ? "Paper trading" : "Live")}` }),
    el("dt", { text: "Auto-trade" }), el("dd", { id: "accountAutoTrade", text: a.auto_trade ? "on (signals are executed in this account)" : "off (signals are only notified)" }),
  );
  const tbody = $("positionsTable").querySelector("tbody");
  tbody.replaceChildren(...data.positions.map((p) => el("tr", { "data-symbol": p.symbol },
    el("td", { class: "label-large", text: p.symbol }), el("td", { class: "num", text: String(p.quantity) }),
    el("td", { class: "num", text: fmt(p.avg_price) }), el("td", { class: "num", text: fmt(p.last) }),
    el("td", { class: `num ${p.pnl >= 0 ? "up" : "down"}`, text: fmtSigned(p.pnl) }),
    el("td", { class: `num ${p.pnl >= 0 ? "up" : "down"}`, text: fmtSigned(p.pnl_pct, 2, "%") }),
    el("td", { class: "num", text: p.stop_loss ? fmt(p.stop_loss) : "-" }), el("td", { class: "num", text: p.take_profit ? fmt(p.take_profit) : "-" }),
    el("td", { text: p.strategy }),
    el("td", { class: "actions" }, closeButton(p)))));
  if (!data.positions.length) tbody.append(el("tr", { class: "empty" }, el("td", { colspan: 10, text: "No open positions" })));
  renderPendingOrders($("pendingOrdersTable"), $("pendingOrdersCount"), data.pending_orders || []);
  const fills = $("fillsTable").querySelector("tbody");
  fills.replaceChildren(...data.fills.map((f) => el("tr", {},
    el("td", { text: fmtTime(f.timestamp, true) }), el("td", { text: f.symbol }), el("td", {}, el("span", { class: `pill ${f.side.toLowerCase()}`, text: f.side })),
    el("td", { class: "num", text: String(f.quantity) }), el("td", { class: "num", text: fmt(f.price) }), el("td", { text: f.strategy || "" }),
    el("td", { class: `num ${f.realized_pnl >= 0 ? "up" : "down"}`, text: fmtSigned(f.realized_pnl) }))));
}

$("resetPaperAccountButton").addEventListener("click", async () => {
  if (!(await confirmDialog("Reset paper account", "Close all paper positions and restore the initial cash?", { okLabel: "Reset", danger: true }))) return;
  try {
    await api.post("api/account/reset");
    await loadPositions();
    showSnackbar("Paper account reset", { duration: 3000 });
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
});

// =========================================================================
// Trade (order ticket, simulation, manual position management)
// =========================================================================

function closeButton(position) {
  return el("button", { class: "icon-btn close-position", title: `Close ${position.symbol} at market`, "aria-label": `Close ${position.symbol}`,
    onclick: (e) => { e.stopPropagation(); closePosition(position); } }, icon("i-close", "icon small"));
}

async function closePosition(position) {
  const ok = await confirmDialog(`Close ${position.symbol}?`, `Sell ${position.quantity} ${position.symbol} at the current market price (~${fmt(position.last)}).`,
                                 { okLabel: "Sell", danger: true });
  if (!ok) return;
  try {
    const result = await api.delete(`api/positions/${encodeURIComponent(position.symbol)}`);
    showSnackbar(result.message, { duration: 5000 });
    await refreshAccountViews();
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

function renderPendingOrders(table, countLabel, orders) {
  const tbody = table.querySelector("tbody");
  tbody.replaceChildren(...orders.map((o) => el("tr", { "data-order-id": o.id },
    el("td", { text: fmtTime(o.created_at, true) }), el("td", { class: "label-large", text: o.symbol }),
    el("td", {}, el("span", { class: `pill ${o.side.toLowerCase()}`, text: o.side })),
    el("td", { class: "num", text: String(o.quantity) }), el("td", { class: "num", text: fmt(o.limit_price) }),
    el("td", { text: o.strategy || "" }),
    el("td", { class: "actions" }, el("button", { class: "icon-btn cancel-order", title: "Cancel order", "aria-label": `Cancel ${o.side} ${o.symbol}`,
      onclick: () => cancelOrder(o) }, icon("i-close", "icon small"))))));
  if (!orders.length) tbody.append(el("tr", { class: "empty" }, el("td", { colspan: 7, text: "No resting orders" })));
  if (countLabel) countLabel.textContent = orders.length ? `${orders.length} resting` : "";
}

async function cancelOrder(order) {
  try {
    await api.delete(`api/orders/${encodeURIComponent(order.id)}`);
    showSnackbar(`Cancelled ${order.side} ${order.quantity} ${order.symbol}`, { duration: 4000 });
    await refreshAccountViews();
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

async function refreshAccountViews() {
  const tasks = [];
  if ($("view-positions").classList.contains("active")) tasks.push(loadPositions());
  if ($("view-trade").classList.contains("active")) tasks.push(loadTradePositions());
  if ($("view-wallet").classList.contains("active")) tasks.push(loadWallet());
  if ($("view-history").classList.contains("active")) tasks.push(loadHistory());
  await Promise.all(tasks);
}

function renderBrokerChip(connection) {
  state.broker = connection;
  const chip = $("tradeBrokerChip");
  const active = connection?.active || { label: "Paper account", paper: true };
  chip.className = `chip ${active.paper ? "off" : "warn"}`;
  $("tradeBrokerLabel").textContent = active.label;
  chip.title = active.paper ? "Orders are simulated" : "Orders go to your live brokerage account";
}

async function loadTradePositions() {
  const data = await api.get("api/positions");
  const tbody = $("tradePositionsTable").querySelector("tbody");
  tbody.replaceChildren(...data.positions.map((p) => el("tr", { class: "clickable", "data-symbol": p.symbol, onclick: () => prefillTicket(p.symbol, "SELL", p.quantity) },
    el("td", { class: "label-large", text: p.symbol }), el("td", { class: "num", text: String(p.quantity) }),
    el("td", { class: "num", text: fmt(p.avg_price) }), el("td", { class: "num", text: fmt(p.last) }),
    el("td", { class: `num ${p.pnl >= 0 ? "up" : "down"}`, text: fmtSigned(p.pnl) }),
    el("td", { class: `num ${p.pnl >= 0 ? "up" : "down"}`, text: fmtSigned(p.pnl_pct, 2, "%") }),
    el("td", { text: p.strategy }), el("td", { class: "actions" }, closeButton(p)))));
  if (!data.positions.length) tbody.append(el("tr", { class: "empty" }, el("td", { colspan: 8, text: "No open positions - use the ticket to add one" })));
  renderPendingOrders($("tradePendingTable"), $("tradePendingCount"), data.pending_orders || []);
  return data;
}

async function loadTrade() {
  try {
    const [connection] = await Promise.all([api.get("api/broker/connection"), loadTradePositions()]);
    renderBrokerChip(connection);
    if (!$("orderSymbolInput").value && state.selected) { $("orderSymbolInput").value = state.selected; updateOrderQuote(); }
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

function prefillTicket(symbol, side, quantity) {
  $("orderSymbolInput").value = symbol;
  $(side === "SELL" ? "orderSideSell" : "orderSideBuy").checked = true;
  if (quantity != null) { $("orderQuantityInput").value = quantity; $("orderNotionalInput").value = ""; }
  updateOrderQuote();
  $("orderQuantityInput").focus();
}

function updateOrderQuote() {
  const symbol = $("orderSymbolInput").value.trim().toUpperCase();
  const row = state.watchlist.find((r) => r.symbol === symbol);
  $("orderQuote").textContent = row?.price != null ? `${row.name ? row.name + " · " : ""}last ${fmt(row.price)}`
    : row ? `${row.name ? row.name + " · " : ""}price is fetched when you simulate`
    : symbol ? "Not in the watchlist - it is added when the order is placed" : "";
}

attachSymbolAutocomplete({ input: $("orderSymbolInput"), onSelect: (item) => { $("orderSymbolInput").value = item.symbol; updateOrderQuote(); } });
$("orderSymbolInput").addEventListener("input", updateOrderQuote);
$("orderTypeSelect").addEventListener("change", () => {
  const limit = $("orderTypeSelect").value === "limit";
  $("orderLimitInput").disabled = !limit;
  if (!limit) $("orderLimitInput").value = "";
});
$("orderQuantityInput").addEventListener("input", () => { if ($("orderQuantityInput").value) $("orderNotionalInput").value = ""; });
$("orderNotionalInput").addEventListener("input", () => { if ($("orderNotionalInput").value) $("orderQuantityInput").value = ""; });

function readOrder() {
  const symbol = $("orderSymbolInput").value.trim().toUpperCase();
  if (!symbol) throw new Error("Enter a symbol");
  const quantity = Number($("orderQuantityInput").value);
  const notional = Number($("orderNotionalInput").value);
  if (!(quantity > 0) && !(notional > 0)) throw new Error("Enter a quantity or a cash amount");
  const body = { symbol, side: document.querySelector("input[name=orderSide]:checked").value, order_type: $("orderTypeSelect").value };
  if (quantity > 0) body.quantity = quantity; else body.notional = notional;
  if (body.order_type === "limit") {
    const limit = Number($("orderLimitInput").value);
    if (!(limit > 0)) throw new Error("Enter a limit price");
    body.limit_price = limit;
  }
  const stop = Number($("orderStopInput").value), target = Number($("orderTargetInput").value);
  if (stop > 0) body.stop_loss_pct = stop;
  if (target > 0) body.take_profit_pct = target;
  return body;
}

function renderSimulation(sim) {
  state.lastSimulation = sim;
  $("simulationEmpty").classList.add("hidden");
  $("simulationResult").classList.remove("hidden");
  $("simulationModel").textContent = `slippage model: ${sim.model}`;
  const verdict = $("simulationVerdict");
  const pending = sim.order_type === "limit" && !sim.marketable;
  verdict.className = `sim-verdict${!sim.accepted ? " rejected" : pending ? " pending" : ""}`;
  verdict.textContent = !sim.accepted ? `Rejected: ${sim.reason}`
    : pending ? `Would rest as a limit order: ${sim.side} ${sim.quantity} ${sim.symbol} @ ${fmt(sim.fill_price)} (last ${fmt(sim.reference_price)})`
    : `${sim.side} ${sim.quantity} ${sim.symbol} would fill @ ${fmt(sim.fill_price)} (last ${fmt(sim.reference_price)})`;
  const pairs = [
    ["Gross value", fmt(sim.gross_value)], ["Commission", fmt(sim.commission)],
    ["Slippage", `${fmtSigned(sim.slippage_cost ? -sim.slippage_cost : 0)} (${fmt(sim.slippage_bps, 1)} bps)`], [sim.side === "BUY" ? "Total cost" : "Net proceeds", fmt(sim.total_cost)],
    ["Cash before", fmt(sim.cash_before)], ["Cash after", fmt(sim.cash_after)],
    ["Position before", String(sim.position_before)], ["Position after", String(sim.position_after)],
  ];
  if (sim.avg_price_after != null) pairs.push(["Avg price after", fmt(sim.avg_price_after)]);
  if (sim.realized_pnl != null) pairs.push(["Realized P&L", fmtSigned(sim.realized_pnl)]);
  if (sim.stop_loss) pairs.push(["Stop loss", fmt(sim.stop_loss)]);
  if (sim.take_profit) pairs.push(["Take profit", fmt(sim.take_profit)]);
  if (sim.participation_pct != null) pairs.push(["Share of avg volume", sim.participation_pct < 0.01 ? "<0.01%" : `${fmt(sim.participation_pct, 2)}%`]);
  $("simulationTable").replaceChildren(...pairs.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v, class: k === "Realized P&L" ? pnlCls(sim.realized_pnl) : "" })]));
  $("simulationWarnings").replaceChildren(...(sim.warnings || []).map((w) => el("li", { text: w })));
}

$("simulateOrderButton").addEventListener("click", async () => {
  const status = $("orderStatus");
  try {
    const body = readOrder();
    status.className = "body-medium muted"; status.textContent = "Simulating…";
    const sim = await api.post("api/orders/simulate", body);
    renderSimulation(sim);
    status.textContent = "";
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});

$("orderForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("orderStatus");
  let body;
  try { body = readOrder(); } catch (err) { status.className = "body-medium error-text"; status.textContent = err.message; return; }
  const live = state.broker?.active && !state.broker.active.paper;
  const what = `${body.side} ${body.quantity ? body.quantity + " shares of" : fmt(body.notional) + " worth of"} ${body.symbol}` +
               (body.order_type === "limit" ? ` at limit ${fmt(body.limit_price)}` : " at market");
  const ok = await confirmDialog(live ? "Place LIVE order?" : "Place order?", `${what} in ${state.broker?.active?.label || "your account"}.${live ? " This uses real money." : ""}`,
                                 { okLabel: body.side === "BUY" ? "Buy" : "Sell", danger: live || body.side === "SELL" });
  if (!ok) return;
  try {
    status.className = "body-medium muted"; status.textContent = "Sending…";
    const result = await api.post("api/orders", body);
    renderSimulation(result.simulation);
    status.className = "body-medium success-text"; status.textContent = result.message;
    showSnackbar(result.message, { kind: result.status === "filled" ? `signal ${body.side.toLowerCase()}` : "", duration: 6000 });
    await loadTradePositions();
    if (!state.watchlist.some((r) => r.symbol === body.symbol)) { await api.post("api/watchlist", { symbol: body.symbol }).catch(() => {}); loadWatchlist(); }
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});

// =========================================================================
// Wallet (deposits, withdrawals, transactions)
// =========================================================================

const fmtMoney = (v, currency) => (v == null ? "-" : `${fmt(v)} ${currency || state.wallet?.currency || ""}`.trim());

function renderWallet(w) {
  state.wallet = w;
  const pendingDeposits = w.pending.filter((t) => t.kind === "deposit").reduce((a, t) => a + t.amount, 0);
  $("walletKpis").replaceChildren(
    kpi(`Cash (${w.currency})`, fmt(w.cash), { hint: w.broker.label }),
    kpi("Available to withdraw", fmt(w.available), { hint: w.reserved ? `${fmt(w.reserved)} reserved by resting orders` : "" }),
    kpi(`Equity (${w.currency})`, fmt(w.equity)),
    kpi("Net deposits", fmtSigned(w.net_deposits), { cls: pnlCls(w.net_deposits), hint: `${fmt(w.totals.deposits)} in · ${fmt(w.totals.withdrawals)} out` }),
    kpi("Pending", w.pending.length ? `${w.pending.length}` : "0", { hint: pendingDeposits ? `${fmt(pendingDeposits)} awaiting payment` : "" }),
    kpi("Fees paid", fmt(w.totals.fees), { hint: `${w.totals.count} transactions` }),
  );
  const enabled = w.funding_enabled;
  $("depositForm").querySelectorAll("input, select, button").forEach((n) => { n.disabled = !enabled; });
  $("withdrawForm").querySelectorAll("input, button").forEach((n) => { n.disabled = !enabled; });
  $("depositProviderLabel").textContent = enabled ? `via ${w.provider}${w.provider === "mock" ? " (sandbox)" : ""} · ${fmt(w.min_amount, 0)}–${fmt(w.max_amount, 0)} ${w.currency}` : (w.funding_disabled_reason || "");
  $("withdrawAvailable").textContent = enabled ? `${fmtMoney(w.available)} available` : (w.funding_disabled_reason || "");
  const select = $("depositCurrencySelect");
  const current = select.value || w.currency;
  select.replaceChildren(...(w.currencies.length ? w.currencies : [w.currency]).map((c) => el("option", { value: c, text: c })));
  select.value = w.currencies.includes(current) ? current : w.currency;
  updateDepositQuote();
}

let quoteTimer = null;
function updateDepositQuote() {
  clearTimeout(quoteTimer);
  const amount = Number($("depositAmountInput").value), currency = $("depositCurrencySelect").value;
  const out = $("depositQuote");
  if (!(amount > 0) || !state.wallet) { out.textContent = ""; return; }
  if (currency === state.wallet.currency) { out.textContent = `Credits ${fmtMoney(amount)} to your account.`; return; }
  quoteTimer = setTimeout(async () => {
    try {
      const q = await api.get(`api/wallet/quote?amount=${encodeURIComponent(amount)}&currency=${encodeURIComponent(currency)}`);
      out.textContent = `≈ ${fmtMoney(q.amount)} at ${fmt(q.rate, 4)} ${q.currency}/${q.source_currency}${q.rate_date ? ` (rate of ${q.rate_date})` : ""}`;
    } catch (err) { out.textContent = err.message; }
  }, 250);
}
$("depositAmountInput").addEventListener("input", updateDepositQuote);
$("depositCurrencySelect").addEventListener("change", updateDepositQuote);

async function loadWallet() {
  try {
    renderWallet(await api.get("api/wallet"));
    await loadTransactions();
  } catch (err) {
    showSnackbar(err.message, { kind: "error" });
  }
}

$("depositForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("depositStatus");
  try {
    status.className = "body-medium muted"; status.textContent = "Creating checkout…";
    const result = await api.post("api/wallet/deposit", { amount: Number($("depositAmountInput").value), currency: $("depositCurrencySelect").value });
    status.textContent = "Redirecting to the payment page…";
    location.assign(result.checkout_url);
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});

$("withdrawForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("withdrawStatus");
  const amount = Number($("withdrawAmountInput").value);
  const ok = await confirmDialog("Withdraw?", `Withdraw ${fmtMoney(amount)} from your account${$("withdrawDestinationInput").value ? ` to ${$("withdrawDestinationInput").value}` : ""}.`, { okLabel: "Withdraw" });
  if (!ok) return;
  try {
    const tx = await api.post("api/wallet/withdraw", { amount, destination: $("withdrawDestinationInput").value.trim() });
    status.className = "body-medium success-text"; status.textContent = tx.status === "completed" ? "Withdrawal completed." : `Withdrawal ${tx.status}.`;
    $("withdrawAmountInput").value = "";
    showSnackbar(`Withdrew ${fmtMoney(amount)}`, { duration: 4000 });
    await loadWallet();
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});

function txRow(t) {
  const amountCls = t.status !== "completed" ? "muted" : pnlCls(t.amount);
  return el("tr", { "data-kind": t.kind, "data-status": t.status },
    el("td", { text: fmtTime(t.timestamp, true) }),
    el("td", {}, el("span", { class: `pill ${t.kind}`, text: t.kind })),
    el("td", { text: t.description + (t.source_currency && t.source_currency !== t.currency ? ` (${fmt(t.source_amount)} ${t.source_currency})` : "") }),
    el("td", { class: `num ${amountCls}`, text: fmtSigned(t.amount) }),
    el("td", { class: "num muted", text: t.fee ? fmt(t.fee) : "" }),
    el("td", { class: "num", text: t.balance_after != null ? fmt(t.balance_after) : "" }),
    el("td", {}, el("span", { class: `pill ${t.status}`, text: t.status })));
}

async function loadTransactions() {
  const kind = $("txKindFilter").value, days = $("txDaysFilter").value;
  const params = new URLSearchParams();
  if (kind) params.set("kind", kind);
  if (days) params.set("days", days);
  const query = params.toString();
  const data = await api.get(`api/transactions${query ? "?" + query : ""}`);
  const tbody = $("transactionsTable").querySelector("tbody");
  tbody.replaceChildren(...data.transactions.map(txRow));
  if (!data.transactions.length) tbody.append(el("tr", { class: "empty" }, el("td", { colspan: 7, text: "No transactions yet" })));
  $("txCount").textContent = data.transactions.length ? `${data.transactions.length} entries` : "";
  params.set("format", "csv");
  $("txExportLink").href = `api/transactions?${params.toString()}`;
}
$("txKindFilter").addEventListener("change", () => loadTransactions().catch((err) => showSnackbar(err.message, { kind: "error" })));
$("txDaysFilter").addEventListener("change", () => loadTransactions().catch((err) => showSnackbar(err.message, { kind: "error" })));

async function handleDepositReturn() {
  const params = new URLSearchParams(location.search);
  const id = params.get("deposit");
  if (!id) return;
  history.replaceState(null, "", location.pathname + "#wallet");
  showView("wallet");
  const notice = $("walletNotice");
  try {
    const tx = await api.post(`api/wallet/deposits/${encodeURIComponent(id)}/sync`);
    notice.classList.remove("hidden");
    if (tx.status === "completed") {
      notice.className = "notice success";
      notice.textContent = `Deposit of ${fmtMoney(tx.amount, tx.currency)} received - your balance is ${fmtMoney(tx.balance_after, tx.currency)}.`;
    } else if (tx.status === "pending") {
      notice.className = "notice";
      notice.textContent = "Your deposit is still being processed; the balance updates as soon as the payment is confirmed.";
    } else {
      notice.className = "notice error";
      notice.textContent = `The deposit was ${tx.status}. No money was taken.`;
    }
    await loadWallet();
  } catch (err) {
    notice.classList.remove("hidden"); notice.className = "notice error"; notice.textContent = err.message;
  }
}

// =========================================================================
// Exchange rates
// =========================================================================

function fillCurrencySelect(select, codes, value) {
  select.replaceChildren(...codes.map((c) => el("option", { value: c, text: c })));
  if (value && codes.includes(value)) select.value = value;
}

async function loadFx() {
  try {
    if (!state.fx.currencies.length) {
      const list = await api.get("api/fx/currencies");
      state.fx.currencies = list.currencies.map((c) => c.code);
      state.fx.names = Object.fromEntries(list.currencies.map((c) => [c.code, c.name]));
      state.fx.base = state.fx.base || list.base;
      fillCurrencySelect($("fxBaseSelect"), state.fx.currencies, state.fx.base);
      fillCurrencySelect($("convertFromSelect"), state.fx.currencies, list.base);
      fillCurrencySelect($("convertToSelect"), state.fx.currencies, list.base === "USD" ? "EUR" : "USD");
    }
    const data = await api.get(`api/fx/latest?base=${encodeURIComponent(state.fx.base)}`);
    state.fx.latest = data;
    $("fxSubtitle").textContent = `${data.provider} · ${data.date}${data.stale ? " · stale" : ""}`;
    $("fxSubtitle").title = data.error || "";
    const tbody = $("fxTable").querySelector("tbody");
    if (!state.fx.quote || state.fx.quote === state.fx.base) {
      const preferred = ["EUR", "USD", "GBP"].find((c) => c !== state.fx.base && data.rows.some((r) => r.currency === c));
      state.fx.quote = preferred || data.rows.find((r) => r.currency !== state.fx.base)?.currency || null;
    }
    tbody.replaceChildren(...data.rows.map((r) => el("tr", { class: `clickable${r.currency === state.fx.quote ? " selected" : ""}`, "data-currency": r.currency,
        onclick: () => { state.fx.quote = r.currency; loadFx(); } },
      el("td", { class: "label-large", text: r.currency }), el("td", { class: "fx-flag", text: r.name }),
      el("td", { class: "num", text: fmt(r.rate, r.rate >= 100 ? 2 : 4) }), el("td", { class: "num muted", text: r.inverse ? fmt(r.inverse, r.inverse >= 100 ? 2 : 4) : "-" }))));
    await loadFxHistory();
  } catch (err) {
    $("fxSubtitle").textContent = err.message;
    showSnackbar(err.message, { kind: "error" });
  }
}

async function loadFxHistory() {
  if (!state.fx.quote) return;
  const data = await api.get(`api/fx/history?base=${encodeURIComponent(state.fx.base)}&quote=${encodeURIComponent(state.fx.quote)}&days=${state.fx.days}`);
  state.fx.history = data;
  $("fxChartTitle").textContent = `${data.base}/${data.quote}`;
  const last = data.points[data.points.length - 1];
  $("fxChartStats").textContent = last ? `${fmt(last.rate, 4)} · ${fmtSigned(data.change_pct, 2, "%")} over ${data.days} days · low ${fmt(data.min, 4)} · high ${fmt(data.max, 4)}` : "No history";
  $("fxChartStats").className = `body-small ${pnlCls(data.change_pct)}`;
  drawLineSeries($("fxCanvas"), data.points.map((p) => ({ date: p.date, value: p.rate })), { decimals: last && last.rate >= 100 ? 2 : 4, emptyText: "No history for this pair" });
}

$("fxBaseSelect").addEventListener("change", () => { state.fx.base = $("fxBaseSelect").value; state.fx.quote = null; loadFx(); });
$("fxRangeGroup").addEventListener("change", (e) => { state.fx.days = Number(e.target.value); loadFxHistory().catch((err) => showSnackbar(err.message, { kind: "error" })); });
window.addEventListener("resize", () => { if (state.fx.history && $("view-fx").classList.contains("active")) loadFxHistory(); });

$("convertForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const amount = Number($("convertAmountInput").value);
    const result = await api.get(`api/fx/convert?amount=${encodeURIComponent(amount)}&from=${$("convertFromSelect").value}&to=${$("convertToSelect").value}`);
    $("convertResult").textContent = `${fmt(result.amount)} ${result.from} = ${fmt(result.result)} ${result.to}`;
    $("convertDetail").textContent = `1 ${result.from} = ${fmt(result.rate, 6)} ${result.to}${result.date ? ` · ${result.source} rate of ${result.date}` : ""}`;
  } catch (err) {
    $("convertResult").textContent = ""; $("convertDetail").textContent = err.message;
  }
});
$("convertSwapButton").addEventListener("click", () => {
  const from = $("convertFromSelect").value; $("convertFromSelect").value = $("convertToSelect").value; $("convertToSelect").value = from;
});

// =========================================================================
// History (fills, closed trades, P&L curve)
// =========================================================================

function kpi(label, value, { cls = "", hint = "" } = {}) {
  return el("div", { class: "kpi" }, el("span", { class: "kpi-label", text: label }),
    el("span", { class: `kpi-value ${cls}`, text: value }), hint ? el("span", { class: "kpi-hint", text: hint }) : null);
}

const pnlCls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const fmtPf = (pf) => (pf == null ? "∞" : fmt(pf));
const fmtMinutes = (m) => (m == null ? "-" : m < 90 ? `${Math.round(m)} min` : m < 48 * 60 ? `${(m / 60).toFixed(1)} h` : `${(m / 1440).toFixed(1)} d`);

function renderKpis(container, stats, account) {
  container.replaceChildren(
    kpi("Realized P&L", fmtSigned(stats.total_pnl), { cls: pnlCls(stats.total_pnl), hint: account ? `Equity ${fmt(account.equity)}` : "" }),
    kpi("Closed trades", String(stats.trades), { hint: `${stats.wins} wins · ${stats.losses} losses` }),
    kpi("Win rate", stats.trades ? `${fmt(stats.win_rate_pct, 1)}%` : "-", { cls: stats.trades ? pnlCls(stats.win_rate_pct - 50) : "" }),
    kpi("Profit factor", stats.trades ? fmtPf(stats.profit_factor) : "-", { cls: stats.profit_factor == null ? "" : pnlCls(stats.profit_factor - 1) }),
    kpi("Expectancy / trade", stats.trades ? fmtSigned(stats.expectancy) : "-", { cls: pnlCls(stats.expectancy) }),
    kpi("Max drawdown", stats.trades ? fmtSigned(stats.max_drawdown) : "-", { cls: pnlCls(stats.max_drawdown) }),
    kpi("Avg holding", stats.trades ? fmtMinutes(stats.avg_holding_minutes) : "-"),
  );
}

function tradeRow(t) {
  return el("tr", {},
    el("td", { text: fmtTime(t.entry_time, true) }), el("td", { text: fmtTime(t.exit_time, true) }),
    el("td", { class: "label-large", text: t.symbol }), el("td", { text: t.strategy || "" }),
    el("td", { class: "num", text: String(t.quantity) }),
    el("td", { class: "num", text: fmt(t.entry_price) }), el("td", { class: "num", text: fmt(t.exit_price) }),
    el("td", { class: `num ${pnlCls(t.pnl)}`, text: fmtSigned(t.pnl) }),
    el("td", { class: `num ${pnlCls(t.return_pct)}`, text: fmtSigned(t.return_pct, 2, "%") }),
    el("td", { class: "num", text: fmtMinutes(t.holding_minutes) }));
}

async function loadHistory() {
  const data = await api.get("api/history");
  state.history = data;
  renderKpis($("historyKpis"), data.stats, data.account_summary);
  $("historySubtitle").textContent = data.stats.trades
    ? `${data.account} · ${data.stats.trades} closed trades between ${fmtTime(data.stats.first_trade, true)} and ${fmtTime(data.stats.last_trade, true)}`
    : `${data.account} · no closed trades yet`;
  drawPnlCurve($("pnlCanvas"), data.pnl_curve);
  const trades = $("closedTradesTable").querySelector("tbody");
  trades.replaceChildren(...[...data.trades].reverse().map(tradeRow));
  if (!data.trades.length) trades.append(el("tr", { class: "empty" }, el("td", { colspan: 10, text: "No closed trades yet - a trade is closed when a position is sold" })));
  $("closedTradesCount").textContent = data.trades.length ? `${data.trades.length} trades` : "";
  const fills = $("historyFillsTable").querySelector("tbody");
  fills.replaceChildren(...data.fills.map((f) => el("tr", {},
    el("td", { text: fmtTime(f.timestamp, true) }), el("td", { class: "label-large", text: f.symbol }),
    el("td", {}, el("span", { class: `pill ${f.side.toLowerCase()}`, text: f.side })),
    el("td", { class: "num", text: String(f.quantity) }), el("td", { class: "num", text: fmt(f.price) }), el("td", { text: f.strategy || "" }),
    el("td", { class: `num ${pnlCls(f.realized_pnl)}`, text: f.side === "SELL" ? fmtSigned(f.realized_pnl) : "" }),
    el("td", { class: "muted", text: f.reason || "" }))));
  if (!data.fills.length) fills.append(el("tr", { class: "empty" }, el("td", { colspan: 8, text: "No fills yet" })));
  $("historyFillsCount").textContent = data.fills.length ? `${data.fills.length} fills` : "";
}

window.addEventListener("resize", () => { if (state.history && $("view-history").classList.contains("active")) drawPnlCurve($("pnlCanvas"), state.history.pnl_curve); });

// =========================================================================
// Insights (analytics dashboard + optional AI analysis)
// =========================================================================

function renderBreakdown(table, rows) {
  const tbody = table.querySelector("tbody");
  tbody.replaceChildren(...rows.map((r) => el("tr", {},
    el("td", { text: r.name }), el("td", { class: "num", text: String(r.trades) }),
    el("td", { class: "num", text: `${fmt(r.win_rate_pct, 1)}%` }),
    el("td", { class: `num ${pnlCls(r.total_pnl)}`, text: fmtSigned(r.total_pnl) }),
    el("td", { class: "num", text: r.profit_factor == null ? (r.wins ? "∞" : "-") : fmt(r.profit_factor) }))));
  if (!rows.length) tbody.append(el("tr", { class: "empty" }, el("td", { colspan: 5, text: "No data" })));
}

function renderInsights(list) {
  $("insightList").replaceChildren(...list.map((i) => el("div", { class: `insight ${i.severity}`, "data-metric": i.metric || "" },
    el("div", {}, el("div", { class: "insight-title", text: i.title }), el("div", { class: "insight-text", text: i.detail })))));
}

async function loadAnalytics() {
  const data = await api.get("api/analytics");
  state.analytics = data;
  renderKpis($("insightsKpis"), data.stats);
  $("insightsSubtitle").textContent = `${data.account} · ${data.stats.trades} closed trades · ${data.open_positions} open`;
  renderInsights(data.insights);
  renderBreakdown($("byStrategyTable"), data.by_strategy);
  renderBreakdown($("bySymbolTable"), data.by_symbol);
  renderBreakdown($("byHourTable"), data.by_hour);
  renderBreakdown($("byWeekdayTable"), data.by_weekday);
  const button = $("aiAnalyzeButton");
  button.disabled = !data.ai_available || !data.stats.trades;
  $("aiNote").textContent = data.ai_available
    ? (data.stats.trades ? `Sends the aggregated statistics above to ${data.ai_model} and asks for a written assessment.` : "Available once the account has closed trades.")
    : (state.user?.is_admin ? "No AI provider configured - add an OpenAI-compatible endpoint under Settings → AI insights." : "No AI provider configured - ask an administrator to enable it under Settings.");
}

$("aiAnalyzeButton").addEventListener("click", async () => {
  const out = $("aiOutput");
  const button = $("aiAnalyzeButton");
  button.disabled = true;
  out.className = "ai-output body-medium pending";
  out.textContent = "Asking the model…";
  try {
    const result = await api.post("api/analytics/ai");
    out.className = "ai-output body-medium";
    out.replaceChildren(document.createTextNode(result.text), el("div", { class: "ai-meta", text: `\n— ${result.model} · ${fmtTime(result.generated_at, true)}` }));
  } catch (err) {
    out.className = "ai-output body-medium error-text";
    out.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

// =========================================================================
// Settings
// =========================================================================

async function loadSettings() {
  const s = await api.get("api/settings");
  state.settings = s;
  const provider = $("settingsProvider");
  provider.replaceChildren(...s.options.providers.map((p) => el("option", { value: p, text: p })));
  provider.value = s.data.provider;
  const interval = $("settingsInterval");
  interval.replaceChildren(...s.options.intervals.map((p) => el("option", { value: p, text: p })));
  interval.value = s.data.interval;
  $("settingsApiKey").value = "";
  $("apiKeyNote").textContent = s.data.api_key_set ? "A key is stored. Leave blank to keep it; or set TRADING_BOT_API_KEY." : "No key stored yet - required for alphavantage / twelvedata (demo and csv need none).";
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
  $("settingsAiEnabled").checked = !!s.ai.enabled;
  $("settingsAiBaseUrl").value = s.ai.base_url || "";
  $("settingsAiModel").value = s.ai.model || "";
  $("settingsAiApiKey").value = "";
  $("aiKeyNote").textContent = s.ai.api_key_set ? "A key is stored. Leave blank to keep it; or set TRADING_BOT_AI_API_KEY." : "No key stored yet (local servers such as Ollama accept any value).";
  $("settingsAllowSignup").checked = !!s.web.allow_signup;
  $("settingsEmailVerification").value = s.web.email_verification || "auto";
  $("settingsPublicUrl").value = s.web.public_url || "";
  const mail = s.notifications.email || {};
  $("settingsEmailTransport").value = mail.transport || "smtp";
  $("settingsSmtpHost").value = mail.host || "";
  $("settingsSmtpPort").value = mail.port || 587;
  $("settingsSmtpUser").value = mail.username || "";
  $("settingsSmtpPassword").value = "";
  $("settingsSmtpFrom").value = mail.sender || "";
  $("emailConfiguredNote").textContent = s.email_configured ? "E-mail delivery is configured: new accounts must verify their address (unless set to Off)."
    : "E-mail delivery is not configured: new accounts are activated immediately (unless set to Always required).";
  const ex = s.execution;
  $("settingsSlippageModel").value = ex.slippage_model;
  $("settingsSlippageBps").value = ex.slippage_bps;
  $("settingsImpact").value = ex.impact_coefficient;
  $("settingsParticipation").value = ex.max_participation_pct;
  $("settingsCommissionOrder").value = ex.commission_per_order;
  $("settingsCommissionShare").value = ex.commission_per_share;
  $("settingsCommissionPct").value = ex.commission_pct;
  $("settingsMinCommission").value = ex.min_commission;
  $("settingsLimitExpiry").value = ex.limit_order_expiry_bars;
  $("settingsPaymentsProvider").value = s.payments.provider;
  $("settingsAccountCurrency").value = s.payments.currency;
  $("settingsMinDeposit").value = s.payments.min_amount;
  $("settingsMaxDeposit").value = s.payments.max_amount;
  $("settingsStripeKey").value = ""; $("settingsStripeWebhook").value = "";
  $("stripeKeyNote").textContent = s.payments.stripe.secret_key_set ? "A key is stored. Leave blank to keep it; or set TRADING_BOT_STRIPE_SECRET_KEY." : "No key stored (sk_test_… for test mode).";
  $("stripeFields").classList.toggle("hidden", s.payments.provider !== "stripe");
  $("settingsFxProvider").value = s.fx.provider;
  $("settingsFxBase").value = s.fx.base_currency;
  $("settingsFxQuotes").value = (s.fx.quote_currencies || []).join(", ");
  document.querySelectorAll(".admin-only").forEach((node) => node.classList.toggle("hidden", !state.user?.is_admin));
  renderProfile();
  loadBrokerSettings();
  updateProviderNote();
}
$("settingsPaymentsProvider").addEventListener("change", () => $("stripeFields").classList.toggle("hidden", $("settingsPaymentsProvider").value !== "stripe"));

// ---- broker connection (per user)
let brokerProviders = [];
async function loadBrokerSettings() {
  try {
    const [providers, connection] = await Promise.all([api.get("api/broker/providers"), api.get("api/broker/connection")]);
    brokerProviders = providers.providers;
    renderBrokerChip(connection);
    const select = $("brokerProviderSelect");
    select.replaceChildren(...brokerProviders.map((p) => el("option", { value: p.name, text: p.label })));
    select.value = connection.provider;
    $("brokerPaperSwitch").checked = connection.paper !== false;
    $("brokerKeyInput").value = "";
    $("brokerSecretInput").value = "";
    $("brokerCurrent").textContent = connection.connected ? `Connected: ${connection.active.label} (key ${connection.key_hint})` : `Currently trading in the ${connection.active.label.toLowerCase()}.`;
    updateBrokerFields();
  } catch (err) {
    $("brokerStatus").className = "body-medium error-text"; $("brokerStatus").textContent = err.message;
  }
}
function updateBrokerFields() {
  const provider = brokerProviders.find((p) => p.name === $("brokerProviderSelect").value);
  $("brokerCredentialFields").classList.toggle("hidden", !provider?.needs_credentials);
  $("brokerProviderNote").textContent = provider?.needs_credentials
    ? `Create API keys in your ${provider.label} dashboard.${provider.docs ? " Docs: " + provider.docs : ""}`
    : "Simulated fills in your paper account - no credentials needed.";
  $("brokerSaveButton").textContent = provider?.needs_credentials ? "Connect" : "Use paper account";
  $("brokerCheckButton").classList.toggle("hidden", !state.broker?.connected);
}
$("brokerProviderSelect").addEventListener("change", updateBrokerFields);
$("brokerSaveButton").addEventListener("click", async () => {
  const status = $("brokerStatus");
  try {
    status.className = "body-medium muted"; status.textContent = "Verifying credentials…";
    const result = await api.put("api/broker/connection", { provider: $("brokerProviderSelect").value, key: $("brokerKeyInput").value.trim() || null,
                                                            secret: $("brokerSecretInput").value || null, paper: $("brokerPaperSwitch").checked });
    status.className = "body-medium success-text";
    status.textContent = result.connected ? `Connected to ${result.active.label}${result.status?.account_number ? ` · account ${result.status.account_number} · cash ${fmt(result.status.cash)}` : ""}` : "Using the paper account.";
    showSnackbar(status.textContent, { duration: 5000 });
    await loadBrokerSettings();
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});
$("brokerCheckButton").addEventListener("click", async () => {
  const status = $("brokerStatus");
  try {
    const result = await api.get("api/broker/connection?check=true");
    if (result.error) { status.className = "body-medium error-text"; status.textContent = result.error; return; }
    status.className = "body-medium success-text";
    status.textContent = result.status ? `OK · account ${result.status.account_number} (${result.status.status}) · cash ${fmt(result.status.cash)} · equity ${fmt(result.status.equity)}` : "OK";
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});

function renderProfile() {
  const u = state.user;
  if (!u) return;
  $("profileSummary").textContent = `Signed in as ${u.username}${u.is_admin ? " (administrator)" : ""}${u.email ? ` · ${u.email_verified ? "e-mail verified" : "e-mail not verified"}` : ""}`;
  $("profileEmail").value = u.email || "";
  $("profileAutoTrade").checked = !!u.auto_trade;
}

$("profileSaveButton").addEventListener("click", async () => {
  const status = $("profileStatus");
  try {
    const user = await api.patch("api/auth/profile", { email: $("profileEmail").value.trim(), auto_trade: $("profileAutoTrade").checked });
    applyUser(user);
    renderProfile();
    status.className = "body-medium success-text"; status.textContent = "Profile saved.";
    showSnackbar("Profile saved", { duration: 3000 });
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});

function updateProviderNote() {
  const provider = $("settingsProvider").value;
  const budget = state.settings?.options.budgets[provider];
  $("providerNote").textContent = budget ? `Free tier: ${budget.per_minute} requests/min, ${budget.per_day}/day. Changes are applied immediately.` : "";
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
  if (state.user?.is_admin) {
    body.ai = { enabled: $("settingsAiEnabled").checked, base_url: $("settingsAiBaseUrl").value.trim(), model: $("settingsAiModel").value.trim(),
                api_key: $("settingsAiApiKey").value.trim() };
    body.web = { allow_signup: $("settingsAllowSignup").checked, email_verification: $("settingsEmailVerification").value,
                 public_url: $("settingsPublicUrl").value.trim() };
    body.notifications.email = { transport: $("settingsEmailTransport").value, host: $("settingsSmtpHost").value.trim(),
                                 port: Number($("settingsSmtpPort").value) || 587, username: $("settingsSmtpUser").value.trim(),
                                 password: $("settingsSmtpPassword").value, sender: $("settingsSmtpFrom").value.trim() };
    body.execution = { slippage_model: $("settingsSlippageModel").value, slippage_bps: Number($("settingsSlippageBps").value),
                       impact_coefficient: Number($("settingsImpact").value), max_participation_pct: Number($("settingsParticipation").value),
                       commission_per_order: Number($("settingsCommissionOrder").value), commission_per_share: Number($("settingsCommissionShare").value),
                       commission_pct: Number($("settingsCommissionPct").value), min_commission: Number($("settingsMinCommission").value),
                       limit_order_expiry_bars: Number($("settingsLimitExpiry").value) };
    body.payments = { provider: $("settingsPaymentsProvider").value, currency: $("settingsAccountCurrency").value.trim().toUpperCase(),
                      min_amount: Number($("settingsMinDeposit").value), max_amount: Number($("settingsMaxDeposit").value),
                      stripe: { secret_key: $("settingsStripeKey").value.trim(), webhook_secret: $("settingsStripeWebhook").value.trim() } };
    body.fx = { provider: $("settingsFxProvider").value, base_currency: $("settingsFxBase").value.trim().toUpperCase(),
                quote_currencies: $("settingsFxQuotes").value.split(",").map((c) => c.trim().toUpperCase()).filter(Boolean) };
  }
  const status = $("settingsStatus");
  try {
    const result = await api.put("api/settings", body);
    applyStatus(result.status);
    await loadSettings();
    status.className = "body-medium success-text";
    status.textContent = result.data_reconfigured ? "Settings saved and applied - market data is being reloaded." : "Settings saved.";
    showSnackbar("Settings saved", { duration: 3000 });
    if (result.data_reconfigured) {
      await loadWatchlist();
      if (state.selected) await loadBars(state.selected);
    }
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
      if (!message.message.includes("requires an API key")) {  // the banner already says so
        showSnackbar(`${message.symbol}: ${message.message}`, { kind: "error", duration: 8000 });
      }
      break;
    }
    case "status":
      if (message.level === "error") showSnackbar(message.message, { kind: "error", duration: 8000 });
      else if (message.message.startsWith("Market data provider set to")) { refreshStatus(); loadWatchlist(); }
      break;
    case "scan":
      applyStatus(message.status);
      if (state.watchlist.some((r) => r.price == null)) loadWatchlist();
      break;
    case "engine":
      applyStatus(message.status);
      break;
    case "positions":
      if (message.account && state.user && message.account !== state.user.username) break;
      refreshAccountViews();
      if ($("view-insights").classList.contains("active")) loadAnalytics();
      break;
    case "order":
      if (state.user && message.account !== state.user.username) break;
      if (message.kind === "filled") {
        showSnackbar(message.message, { kind: `signal ${message.side.toLowerCase()}`, duration: 8000, action: "Positions", onAction: () => showView("positions") });
        const notifications = state.settings?.notifications;
        if (!notifications || notifications.desktop) notifyBrowser("Order filled", message.message);
        refreshAccountViews();
      } else if (message.kind === "rejected") {
        showSnackbar(message.message, { kind: "error", duration: 8000 });
      }
      break;
    case "wallet":
      if (state.user && message.account !== state.user.username) break;
      if ($("view-wallet").classList.contains("active")) loadWallet();
      break;
    default:
      break;
  }
}

// =========================================================================
// Boot
// =========================================================================

function applyUser(user) {
  state.user = user;
  $("userName").textContent = user.username;
  $("userName").title = user.email || user.username;
  $("userAvatar").textContent = (user.username || "?").slice(0, 1).toUpperCase();
}

async function loadUser() {
  const auth = await api.get("api/auth/status");
  if (!auth.authenticated) { location.replace("login"); return false; }
  applyUser(auth.user || { username: auth.username });
  return true;
}

$("logoutButton").addEventListener("click", async () => {
  try { await api.post("api/auth/logout"); } finally { location.replace("login"); }
});

enhancePasswordFields(document);
const accountFeedback = attachPasswordFeedback({ input: $("accountNewPassword"), username: () => state.user?.username || "",
                                                 container: $("accountPasswordFeedback"), confirmInput: $("accountConfirmPassword") });

$("accountSaveButton").addEventListener("click", async () => {
  const status = $("accountStatus");
  const newPassword = $("accountNewPassword").value;
  if (!accountFeedback.isValid()) {
    status.className = "body-medium error-text"; status.textContent = `Password requirements not met: ${accountFeedback.failures().join("; ")}`; return;
  }
  if (newPassword !== $("accountConfirmPassword").value) {
    status.className = "body-medium error-text"; status.textContent = "Passwords do not match"; return;
  }
  try {
    const result = await api.post("api/auth/password", { current_password: $("accountCurrentPassword").value, new_password: newPassword });
    if (result.user) applyUser(result.user);
    for (const id of ["accountCurrentPassword", "accountNewPassword", "accountConfirmPassword"]) $(id).value = "";
    accountFeedback.refresh();
    status.className = "body-medium success-text"; status.textContent = "Password changed.";
    showSnackbar("Password changed", { duration: 3000 });
  } catch (err) {
    status.className = "body-medium error-text"; status.textContent = err.message;
  }
});

async function boot() {
  try {
    if (!(await loadUser())) return;
    await refreshStatus();
  } catch (err) {
    if (err.message === "Authentication required") return;
    showSnackbar(`Cannot reach the server: ${err.message}`, { kind: "error" });
    return;
  }
  connectEvents(onEvent, (connected) => { if (!connected) $("progress").classList.add("active"); else $("progress").classList.remove("active"); });
  await Promise.all([loadStrategies(), loadRulesHelp()]);
  await loadWatchlist();
  renderBacktestSymbols();
  await loadSignals();
  const view = location.hash.replace("#", "");
  if (["dashboard", "strategies", "positions", "trade", "wallet", "fx", "history", "insights", "settings"].includes(view)) showView(view);
  await handleDepositReturn();
}

boot();
