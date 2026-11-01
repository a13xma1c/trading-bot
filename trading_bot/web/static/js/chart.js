/**
 * Candlestick chart rendered on a <canvas>: candles, volume, indicator panes,
 * overlays, buy/sell markers, crosshair with OHLCV read-out, wheel zoom and
 * drag pan. Bars are positioned by index so sessions have no gaps.
 */

const UP = "#4ecca3";
const DOWN = "#ff7b72";
const GRID = "rgba(196, 198, 207, 0.10)";
const TEXT = "#c4c6cf";
const AXIS_BG = "rgba(25, 28, 32, 0.92)";
const OVERLAY_COLORS = ["#ffb74d", "#64b5f6", "#ba68c8", "#fff176", "#4dd0e1", "#f06292", "#aed581"];
const RIGHT_AXIS = 68;
const BOTTOM_AXIS = 26;
const VOLUME_HEIGHT = 72;
const PANE_HEIGHT = 96;
const PAD = 8;

export class CandleChart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.bars = [];            // [t, o, h, l, c, v]
    this.symbol = "";
    this.overlay = null;       // {price: [], volume: [], panes: [[]], buy: [], sell: []}
    this.markers = { buy: [], sell: [] };
    this.from = 0;             // first visible index (may be fractional while panning)
    this.to = 0;               // one past the last visible index
    this.cursor = null;        // {x, y} in CSS pixels
    this.drag = null;
    this._layout = null;
    this._bind();
    if (typeof ResizeObserver !== "undefined") {
      this._observer = new ResizeObserver(() => this.draw());
      this._observer.observe(canvas.parentElement || canvas);
    }
  }

  // ------------------------------------------------------------ data
  setData(symbol, bars, { keepView = true, message = "" } = {}) {
    this.message = message;
    const previousLen = this.bars.length;
    const sameSymbol = symbol === this.symbol;
    this.symbol = symbol;
    this.bars = bars || [];
    if (!keepView || !sameSymbol || previousLen === 0) {
      this.resetView();
    } else if (this.to >= previousLen - 1) {
      const shift = this.bars.length - previousLen;
      this.from += shift;
      this.to = this.bars.length;
    }
    this.draw();
  }

  setOverlay(overlay) {
    this.overlay = overlay;
    this.draw();
  }

  setMarkers(markers) {
    this.markers = { buy: markers?.buy || [], sell: markers?.sell || [] };
    this.draw();
  }

  resetView() {
    const n = this.bars.length;
    const visible = Math.min(n, 150);
    this.from = Math.max(0, n - visible);
    this.to = n;
    this.draw();
  }

  getState() {
    return { symbol: this.symbol, bars: this.bars.length, from: Math.round(this.from), to: Math.round(this.to),
             overlay: this.overlay ? this.overlay.strategy : null, markers: this.markers,
             panes: this.overlay ? this.overlay.panes.length : 0 };
  }

  // --------------------------------------------------------- interaction
  _bind() {
    const c = this.canvas;
    c.addEventListener("wheel", (e) => {
      if (!this.bars.length) return;
      e.preventDefault();
      const rect = c.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const span = this.to - this.from;
      const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
      const newSpan = Math.min(this.bars.length, Math.max(10, span * factor));
      const anchor = this._xToIndex(x);
      const ratio = (anchor - this.from) / span;
      let from = anchor - ratio * newSpan;
      from = Math.max(0, Math.min(from, this.bars.length - newSpan));
      this.from = from;
      this.to = from + newSpan;
      this.draw();
    }, { passive: false });
    c.addEventListener("mousedown", (e) => { this.drag = { x: e.clientX, from: this.from, to: this.to }; });
    window.addEventListener("mouseup", () => { this.drag = null; });
    c.addEventListener("mousemove", (e) => {
      const rect = c.getBoundingClientRect();
      this.cursor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      if (this.drag && this.bars.length) {
        const span = this.drag.to - this.drag.from;
        const perBar = this._layout ? this._layout.plotWidth / span : 1;
        let from = this.drag.from - (e.clientX - this.drag.x) / perBar;
        from = Math.max(0, Math.min(from, this.bars.length - span));
        this.from = from;
        this.to = from + span;
      }
      this.draw();
    });
    c.addEventListener("mouseleave", () => { this.cursor = null; this.drag = null; this.draw(); });
    c.addEventListener("dblclick", () => this.resetView());
  }

  _xToIndex(x) {
    const L = this._layout;
    if (!L) return 0;
    return this.from + ((x - L.left) / L.plotWidth) * (this.to - this.from);
  }

  // ------------------------------------------------------------- layout
  _computeLayout(width, height) {
    const paneCount = this.overlay ? this.overlay.panes.length : 0;
    const left = PAD;
    const right = width - RIGHT_AXIS;
    const plotWidth = Math.max(10, right - left);
    const bottom = height - BOTTOM_AXIS;
    const volumeTop = bottom - VOLUME_HEIGHT;
    const panes = [];
    let y = volumeTop;
    for (let i = paneCount - 1; i >= 0; i--) {
      y -= PANE_HEIGHT;
      panes.unshift({ top: y + 4, bottom: y + PANE_HEIGHT - 4, series: this.overlay.panes[i] });
    }
    const priceTop = PAD + 18;
    const priceBottom = (paneCount ? panes[0].top - 4 : volumeTop) - 6;
    return { width, height, left, right, plotWidth, bottom, volumeTop, panes, priceTop, priceBottom };
  }

  // --------------------------------------------------------------- draw
  draw() {
    const canvas = this.canvas;
    const host = canvas.parentElement || canvas;
    const width = Math.max(50, host.clientWidth || canvas.clientWidth || 600);
    const height = Math.max(50, host.clientHeight || canvas.clientHeight || 400);
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "11px Roboto, system-ui, sans-serif";
    const L = this._computeLayout(width, height);
    this._layout = L;
    if (!this.bars.length) {
      ctx.fillStyle = TEXT;
      ctx.textAlign = "center";
      ctx.fillText(this.message || (this.symbol ? `Loading ${this.symbol}…` : "No data"), width / 2, height / 2);
      return;
    }
    const from = Math.max(0, Math.floor(this.from));
    const to = Math.min(this.bars.length, Math.ceil(this.to));
    const span = this.to - this.from;
    const perBar = L.plotWidth / span;
    const xOf = (i) => L.left + (i + 0.5 - this.from) * perBar;
    const bodyWidth = Math.max(1, Math.min(perBar * 0.7, 24));

    // ---- price scale
    let lo = Infinity, hi = -Infinity;
    for (let i = from; i < to; i++) { lo = Math.min(lo, this.bars[i][3]); hi = Math.max(hi, this.bars[i][2]); }
    for (const s of this.overlay?.price || []) {
      for (let i = from; i < to; i++) { const v = s.values[i]; if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    }
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
    const padY = (hi - lo || 1) * 0.06;
    lo -= padY; hi += padY;
    const yOf = (v) => L.priceBottom - ((v - lo) / (hi - lo)) * (L.priceBottom - L.priceTop);

    // ---- grid + price axis
    const ticks = niceTicks(lo, hi, Math.max(3, Math.floor((L.priceBottom - L.priceTop) / 48)));
    ctx.strokeStyle = GRID; ctx.lineWidth = 1;
    ctx.fillStyle = TEXT; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    for (const t of ticks) {
      const y = Math.round(yOf(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(L.left, y); ctx.lineTo(L.right, y); ctx.stroke();
      ctx.fillText(formatPrice(t), L.right + 6, y);
    }

    // ---- time axis
    const timeTicks = this._timeTicks(from, to, perBar);
    for (const i of timeTicks) {
      const x = Math.round(xOf(i)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, L.priceTop); ctx.lineTo(x, L.bottom); ctx.stroke();
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (const i of timeTicks) {
      const label = this._timeLabel(i);
      const half = ctx.measureText(label).width / 2;
      const x = Math.min(Math.max(xOf(i), L.left + half), L.right - half);
      ctx.fillText(label, x, L.bottom + 6);
    }

    // ---- candles
    for (let i = from; i < to; i++) {
      const [, o, h, l, c] = this.bars[i];
      const x = xOf(i);
      const color = c >= o ? UP : DOWN;
      ctx.strokeStyle = color; ctx.fillStyle = color;
      ctx.beginPath(); ctx.moveTo(x, yOf(h)); ctx.lineTo(x, yOf(l)); ctx.stroke();
      const top = yOf(Math.max(o, c)), bot = yOf(Math.min(o, c));
      ctx.fillRect(x - bodyWidth / 2, top, bodyWidth, Math.max(1, bot - top));
    }

    // ---- price overlays + legend
    let colorIndex = 0;
    let legendX = L.left + 4;
    const legend = (name, color) => {
      ctx.fillStyle = color; ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText(name, legendX, PAD);
      legendX += ctx.measureText(name).width + 14;
    };
    for (const s of this.overlay?.price || []) {
      const color = OVERLAY_COLORS[colorIndex++ % OVERLAY_COLORS.length];
      this._line(s.values, from, to, xOf, yOf, color);
      legend(s.name, color);
    }

    // ---- markers
    const span3 = (hi - lo) * 0.025;
    for (const i of this.markers.buy.concat(this.overlay?.buy || [])) {
      if (i < from || i >= to) continue;
      this._triangle(xOf(i), yOf(this.bars[i][3] - span3), true, UP);
    }
    for (const i of this.markers.sell.concat(this.overlay?.sell || [])) {
      if (i < from || i >= to) continue;
      this._triangle(xOf(i), yOf(this.bars[i][2] + span3), false, DOWN);
    }

    // ---- indicator panes
    for (const pane of L.panes) {
      let plo = Infinity, phi = -Infinity;
      for (const s of pane.series) for (let i = from; i < to; i++) { const v = s.values[i]; if (v != null) { plo = Math.min(plo, v); phi = Math.max(phi, v); } }
      if (!isFinite(plo)) { plo = 0; phi = 1; }
      if (phi === plo) { phi += 1; plo -= 1; }
      const pad = (phi - plo) * 0.1; plo -= pad; phi += pad;
      const py = (v) => pane.bottom - ((v - plo) / (phi - plo)) * (pane.bottom - pane.top);
      ctx.strokeStyle = GRID;
      ctx.beginPath(); ctx.moveTo(L.left, pane.top - 4 + 0.5); ctx.lineTo(L.right, pane.top - 4 + 0.5); ctx.stroke();
      ctx.fillStyle = TEXT; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      for (const t of niceTicks(plo, phi, 2)) ctx.fillText(formatPrice(t), L.right + 6, py(t));
      let lx = L.left + 4;
      for (const s of pane.series) {
        const color = OVERLAY_COLORS[colorIndex++ % OVERLAY_COLORS.length];
        this._line(s.values, from, to, xOf, py, color);
        ctx.fillStyle = color; ctx.textAlign = "left"; ctx.textBaseline = "top";
        ctx.fillText(s.name, lx, pane.top);
        lx += ctx.measureText(s.name).width + 14;
      }
    }

    // ---- volume
    let vmax = 0;
    for (let i = from; i < to; i++) vmax = Math.max(vmax, this.bars[i][5]);
    for (const s of this.overlay?.volume || []) for (let i = from; i < to; i++) { const v = s.values[i]; if (v != null) vmax = Math.max(vmax, v); }
    vmax = vmax || 1;
    const vy = (v) => L.bottom - (v / vmax) * (VOLUME_HEIGHT - 10);
    ctx.strokeStyle = GRID;
    ctx.beginPath(); ctx.moveTo(L.left, L.volumeTop + 0.5); ctx.lineTo(L.right, L.volumeTop + 0.5); ctx.stroke();
    for (let i = from; i < to; i++) {
      const b = this.bars[i];
      ctx.fillStyle = b[4] >= b[1] ? "rgba(78,204,163,0.55)" : "rgba(255,123,114,0.55)";
      const x = xOf(i);
      ctx.fillRect(x - bodyWidth / 2, vy(b[5]), bodyWidth, L.bottom - vy(b[5]));
    }
    ctx.fillStyle = TEXT; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(formatVolume(vmax), L.right + 6, L.volumeTop + 8);
    let vlx = L.left + 4;
    for (const s of this.overlay?.volume || []) {
      const color = OVERLAY_COLORS[colorIndex++ % OVERLAY_COLORS.length];
      this._line(s.values, from, to, xOf, vy, color);
      ctx.fillStyle = color; ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText(s.name, vlx, L.volumeTop + 2);
      vlx += ctx.measureText(s.name).width + 14;
    }

    // ---- crosshair
    if (this.cursor && this.cursor.x >= L.left && this.cursor.x <= L.right) {
      const i = Math.round(this._xToIndex(this.cursor.x) - 0.5);
      if (i >= 0 && i < this.bars.length) {
        const x = Math.round(xOf(i)) + 0.5;
        ctx.save();
        ctx.setLineDash([4, 4]); ctx.strokeStyle = "rgba(196,198,207,0.5)";
        ctx.beginPath(); ctx.moveTo(x, L.priceTop); ctx.lineTo(x, L.bottom); ctx.stroke();
        const y = Math.round(this.cursor.y) + 0.5;
        ctx.beginPath(); ctx.moveTo(L.left, y); ctx.lineTo(L.right, y); ctx.stroke();
        ctx.restore();
        if (this.cursor.y >= L.priceTop && this.cursor.y <= L.priceBottom) {
          const price = lo + (1 - (this.cursor.y - L.priceTop) / (L.priceBottom - L.priceTop)) * (hi - lo);
          this._axisTag(L.right + 2, y, formatPrice(price), "right");
        }
        const [t, o, h, l, c, v] = this.bars[i];
        const text = `${formatTime(t, true)}   O ${formatPrice(o)}  H ${formatPrice(h)}  L ${formatPrice(l)}  C ${formatPrice(c)}  V ${formatVolume(v)}`;
        ctx.fillStyle = AXIS_BG;
        const w = ctx.measureText(text).width + 16;
        ctx.fillRect(L.left, L.priceTop - 4, w, 18);
        ctx.fillStyle = "#e2e2e6"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
        ctx.fillText(text, L.left + 8, L.priceTop + 5);
      }
    }
  }

  _line(values, from, to, xOf, yOf, color) {
    const ctx = this.ctx;
    ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    ctx.beginPath();
    let pen = false;
    for (let i = Math.max(0, from - 1); i < Math.min(values.length, to + 1); i++) {
      const v = values[i];
      if (v == null || !isFinite(v)) { pen = false; continue; }
      const x = xOf(i), y = yOf(v);
      if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); pen = true; }
    }
    ctx.stroke();
    ctx.lineWidth = 1;
  }

  _triangle(x, y, up, color) {
    const ctx = this.ctx;
    ctx.fillStyle = color;
    ctx.beginPath();
    if (up) { ctx.moveTo(x, y - 6); ctx.lineTo(x - 6, y + 6); ctx.lineTo(x + 6, y + 6); }
    else { ctx.moveTo(x, y + 6); ctx.lineTo(x - 6, y - 6); ctx.lineTo(x + 6, y - 6); }
    ctx.closePath(); ctx.fill();
  }

  _axisTag(x, y, text, align) {
    const ctx = this.ctx;
    const w = ctx.measureText(text).width + 10;
    ctx.fillStyle = "#3c4758";
    ctx.fillRect(x, y - 9, w, 18);
    ctx.fillStyle = "#d8e3f8"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(text, x + 5, y);
  }

  _timeTicks(from, to, perBar) {
    const minPixels = 90;
    const every = Math.max(1, Math.ceil(minPixels / perBar));
    const out = [];
    for (let i = Math.ceil(from / every) * every; i < to; i += every) out.push(i);
    return out;
  }

  _isDaily() {
    if (this.bars.length < 2) return false;
    return this.bars[1][0] - this.bars[0][0] >= 86_400_000;
  }

  _timeLabel(i) {
    return formatTime(this.bars[i][0], !this._isDaily());
  }
}

export function niceTicks(lo, hi, count) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const rough = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

export function formatPrice(v) {
  if (v == null || !isFinite(v)) return "";
  const a = Math.abs(v);
  if (a >= 10000) return v.toFixed(0);
  if (a >= 100) return v.toFixed(2);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(4);
}

export function formatVolume(v) {
  if (v == null) return "";
  if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(0) + "K";
  return String(Math.round(v));
}

export function formatTime(ms, intraday) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  return intraday ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}` : date;
}

/** Small line chart for the backtest equity curve. */
export function drawEquityCurve(canvas, values) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600, height = canvas.clientHeight || 180;
  canvas.width = width * dpr; canvas.height = height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const data = values.filter((v) => v != null);
  if (!data.length) return;
  const lo = Math.min(...data), hi = Math.max(...data);
  const y = (v) => height - 16 - ((v - lo) / ((hi - lo) || 1)) * (height - 32);
  const x = (i) => 64 + (i / Math.max(1, values.length - 1)) * (width - 76);
  ctx.strokeStyle = GRID; ctx.fillStyle = TEXT; ctx.font = "11px Roboto, system-ui, sans-serif"; ctx.textBaseline = "middle";
  for (const t of niceTicks(lo, hi, 4)) {
    ctx.beginPath(); ctx.moveTo(64, y(t) + 0.5); ctx.lineTo(width - 8, y(t) + 0.5); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(formatPrice(t), 60, y(t));
  }
  ctx.strokeStyle = "#8fb6ff"; ctx.lineWidth = 2; ctx.beginPath();
  values.forEach((v, i) => { if (v == null) return; if (i === 0) ctx.moveTo(x(i), y(v)); else ctx.lineTo(x(i), y(v)); });
  ctx.stroke();
}

/**
 * Cumulative realized P&L after each closed trade.
 * `points` is [{time, pnl, cumulative}] in chronological order; the area above zero is shaded green,
 * below zero red so a glance tells whether the account is ahead.
 */
export function drawPnlCurve(canvas, points) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600, height = canvas.clientHeight || 220;
  canvas.width = width * dpr; canvas.height = height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.font = "11px Roboto, system-ui, sans-serif"; ctx.textBaseline = "middle"; ctx.fillStyle = TEXT;
  if (!points || !points.length) {
    ctx.textAlign = "center";
    ctx.fillText("No closed trades yet", width / 2, height / 2);
    return;
  }
  const values = [0, ...points.map((p) => p.cumulative)];
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  const top = 12, bottom = height - BOTTOM_AXIS, left = 64, right = width - 12;
  const y = (v) => bottom - ((v - lo) / ((hi - lo) || 1)) * (bottom - top);
  const x = (i) => left + (i / Math.max(1, values.length - 1)) * (right - left);
  ctx.strokeStyle = GRID; ctx.lineWidth = 1;
  for (const t of niceTicks(lo, hi, 5)) {
    ctx.beginPath(); ctx.moveTo(left, y(t) + 0.5); ctx.lineTo(right, y(t) + 0.5); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(formatPrice(t), left - 6, y(t));
  }
  // zero line
  ctx.strokeStyle = "rgba(196, 198, 207, 0.45)"; ctx.beginPath(); ctx.moveTo(left, y(0) + 0.5); ctx.lineTo(right, y(0) + 0.5); ctx.stroke();
  // shaded area, split at zero
  const fill = (color, clipTop, clipBottom) => {
    ctx.save(); ctx.beginPath(); ctx.rect(left, clipTop, right - left, clipBottom - clipTop); ctx.clip();
    ctx.beginPath(); ctx.moveTo(x(0), y(0));
    values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
    ctx.lineTo(x(values.length - 1), y(0)); ctx.closePath();
    ctx.fillStyle = color; ctx.fill(); ctx.restore();
  };
  fill("rgba(78, 204, 163, 0.22)", top - 2, y(0));
  fill("rgba(255, 123, 114, 0.22)", y(0), bottom + 2);
  // line
  const last = values[values.length - 1];
  ctx.strokeStyle = last >= 0 ? UP : DOWN; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath();
  values.forEach((v, i) => { if (i === 0) ctx.moveTo(x(i), y(v)); else ctx.lineTo(x(i), y(v)); });
  ctx.stroke();
  // markers per trade
  points.forEach((p, i) => {
    ctx.fillStyle = p.pnl >= 0 ? UP : DOWN;
    ctx.beginPath(); ctx.arc(x(i + 1), y(p.cumulative), values.length > 60 ? 1.5 : 3, 0, Math.PI * 2); ctx.fill();
  });
  // time axis: first, middle, last
  ctx.fillStyle = TEXT; ctx.textBaseline = "top";
  const labelAt = (i, align) => {
    const ms = Date.parse(points[i].time);
    if (Number.isNaN(ms)) return;
    ctx.textAlign = align; ctx.fillText(formatTime(ms, false), x(i + 1), bottom + 6);
  };
  labelAt(0, points.length > 1 ? "left" : "center");
  if (points.length > 2) labelAt(Math.floor((points.length - 1) / 2), "center");
  if (points.length > 1) labelAt(points.length - 1, "right");
}

/**
 * Generic time series line (e.g. an exchange rate).  `points` is [{date|time, value}] in chronological order.
 * Options: `decimals` for the axis labels, `color`.
 */
export function drawLineSeries(canvas, points, { decimals = 4, color = "#8fb6ff", emptyText = "No data" } = {}) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600, height = canvas.clientHeight || 240;
  canvas.width = width * dpr; canvas.height = height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.font = "11px Roboto, system-ui, sans-serif"; ctx.textBaseline = "middle"; ctx.fillStyle = TEXT;
  const values = (points || []).map((p) => Number(p.value ?? p.rate)).filter((v) => Number.isFinite(v));
  if (values.length < 2) { ctx.textAlign = "center"; ctx.fillText(emptyText, width / 2, height / 2); return; }
  let lo = Math.min(...values), hi = Math.max(...values);
  if (hi === lo) { lo -= Math.abs(lo) * 0.01 || 1; hi += Math.abs(hi) * 0.01 || 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const top = 12, bottom = height - BOTTOM_AXIS, left = 72, right = width - 12;
  const y = (v) => bottom - ((v - lo) / (hi - lo)) * (bottom - top);
  const x = (i) => left + (i / (values.length - 1)) * (right - left);
  const fmt = (v) => v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  ctx.strokeStyle = GRID; ctx.lineWidth = 1;
  for (const t of niceTicks(lo, hi, 5)) {
    ctx.beginPath(); ctx.moveTo(left, y(t) + 0.5); ctx.lineTo(right, y(t) + 0.5); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(fmt(t), left - 6, y(t));
  }
  const up = values[values.length - 1] >= values[0];
  const line = color || (up ? UP : DOWN);
  ctx.save(); ctx.beginPath(); ctx.moveTo(x(0), bottom);
  values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(values.length - 1), bottom); ctx.closePath();
  const gradient = ctx.createLinearGradient(0, top, 0, bottom);
  gradient.addColorStop(0, up ? "rgba(78, 204, 163, 0.28)" : "rgba(255, 123, 114, 0.28)");
  gradient.addColorStop(1, "rgba(0, 0, 0, 0)");
  ctx.fillStyle = gradient; ctx.fill(); ctx.restore();
  ctx.strokeStyle = up ? UP : DOWN; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath();
  values.forEach((v, i) => { if (i === 0) ctx.moveTo(x(i), y(v)); else ctx.lineTo(x(i), y(v)); });
  ctx.stroke();
  // last value marker
  ctx.fillStyle = line === color ? (up ? UP : DOWN) : line;
  ctx.beginPath(); ctx.arc(x(values.length - 1), y(values[values.length - 1]), 3.5, 0, Math.PI * 2); ctx.fill();
  // time axis
  ctx.fillStyle = TEXT; ctx.textBaseline = "top";
  const label = (i, align) => {
    const raw = points[i].date || points[i].time;
    const ms = Date.parse(raw);
    if (Number.isNaN(ms)) return;
    ctx.textAlign = align; ctx.fillText(formatTime(ms, false), x(i), bottom + 6);
  };
  label(0, "left"); label(Math.floor((points.length - 1) / 2), "center"); label(points.length - 1, "right");
}
