/** API client, WebSocket connection and Material components (snackbar, dialogs, notifications). */

export const api = {
  async request(method, path, body) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      location.replace("/login");  // session expired or signed out elsewhere
      throw new Error("Authentication required");
    }
    if (!response.ok) {
      const error = new Error(detailText(data) || `${response.status} ${response.statusText}`);
      error.status = response.status;
      throw error;
    }
    return data;
  },
  get(path) { return this.request("GET", path); },
  post(path, body) { return this.request("POST", path, body ?? {}); },
  put(path, body) { return this.request("PUT", path, body); },
  patch(path, body) { return this.request("PATCH", path, body); },
  delete(path) { return this.request("DELETE", path); },
};

function detailText(data) {
  if (!data) return "";
  const detail = data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
  return "";
}

/** Auto-reconnecting WebSocket delivering parsed JSON messages to `onMessage`. */
export function connectEvents(onMessage, onState) {
  let socket = null;
  let closed = false;
  let delay = 1000;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/ws`);  // authenticated by the session cookie
    socket.onopen = () => { delay = 1000; onState?.(true); };
    socket.onmessage = (event) => { try { onMessage(JSON.parse(event.data)); } catch (err) { console.error(err); } };
    socket.onclose = (event) => {
      onState?.(false);
      if (event.code === 4401) { location.replace("/login"); return; }
      if (!closed) setTimeout(open, delay = Math.min(delay * 2, 15000));
    };
    socket.onerror = () => socket.close();
  };
  open();
  return { close() { closed = true; socket?.close(); } };
}

// ------------------------------------------------------------------ snackbar
const snackbar = { timer: null, queue: [] };

export function showSnackbar(text, { kind = "", duration = 6000, action = "Dismiss", onAction = null } = {}) {
  const el = document.getElementById("snackbar");
  const textEl = document.getElementById("snackbarText");
  const actionEl = document.getElementById("snackbarAction");
  clearTimeout(snackbar.timer);
  textEl.textContent = text;
  actionEl.textContent = action;
  el.className = `snackbar open ${kind}`;
  actionEl.onclick = () => { hideSnackbar(); onAction?.(); };
  snackbar.timer = setTimeout(hideSnackbar, duration);
}

export function hideSnackbar() {
  document.getElementById("snackbar").classList.remove("open");
}

// ------------------------------------------------------------------- dialogs
export function confirmDialog(title, text, { okLabel = "Confirm", danger = false } = {}) {
  return new Promise((resolve) => {
    const dialog = document.getElementById("confirmDialog");
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmText").textContent = text;
    const ok = document.getElementById("confirmOkButton");
    const cancel = document.getElementById("confirmCancelButton");
    ok.textContent = okLabel;
    ok.className = `btn filled${danger ? " error" : ""}`;
    const done = (value) => { dialog.close(); ok.onclick = cancel.onclick = null; resolve(value); };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
    dialog.onclose = () => resolve(false);
    if (!dialog.open) dialog.showModal();
  });
}

// -------------------------------------------------------------- notifications
export function notifyBrowser(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    try { new Notification(title, { body }); } catch (err) { console.warn(err); }
  } else if (Notification.permission !== "denied") {
    Notification.requestPermission().then((perm) => { if (perm === "granted") new Notification(title, { body }); });
  }
}

let audioContext = null;
export function beep() {
  try {
    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioContext.createOscillator();
    const gain = audioContext.createGain();
    osc.frequency.value = 880;
    gain.gain.value = 0.08;
    osc.connect(gain).connect(audioContext.destination);
    osc.start();
    osc.stop(audioContext.currentTime + 0.18);
  } catch (err) { /* audio not available */ }
}

// ------------------------------------------------------------------ helpers
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

export function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${name}`);
  svg.append(use);
  return svg;
}

export function fmt(n, digits = 2) {
  if (n == null || Number.isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtSigned(n, digits = 2, suffix = "") {
  if (n == null) return "-";
  return `${n >= 0 ? "+" : ""}${fmt(n, digits)}${suffix}`;
}

export function fmtTime(iso, withDate = false) {
  if (!iso) return "-";
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  const time = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return withDate ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${time}:${p(d.getSeconds())}` : time;
}

export function fmtSeconds(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}
