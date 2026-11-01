/** Login / first-run setup page. */

const $ = (id) => document.getElementById(id);
let setupMode = false;

async function request(path, body) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
                                       body: JSON.stringify(body) });
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `${response.status} ${response.statusText}`);
  return data;
}

function showError(text) {
  $("loginError").textContent = text;
}

let providers = [];

function updateProviderNote() {
  const selected = providers.find((p) => p.name === $("providerSelect").value);
  if (!selected) return;
  $("apiKeyField").classList.toggle("hidden", !selected.requires_api_key);
  $("providerNote").textContent = selected.requires_api_key
    ? `Free tier: ${selected.per_minute} requests/min, ${selected.per_day}/day.`
    : selected.name === "demo" ? "Deterministic synthetic prices - explore the app without an API key."
    : "Reads <SYMBOL>.csv files from the configured directory.";
}

function setMode(setup) {
  setupMode = setup;
  $("confirmField").classList.toggle("hidden", !setup);
  $("setupDataFields").classList.toggle("hidden", !setup);
  $("confirmInput").required = setup;
  $("loginSubtitle").textContent = setup ? "Create the administrator account" : "Sign in to continue";
  $("loginButtonLabel").textContent = setup ? "Create account" : "Sign in";
  $("loginHint").textContent = setup
    ? "No password has been configured yet. The account you create here is stored (hashed) in config.json; you can change it later under Settings or with `trading-bot set-password`."
    : "";
  $("passwordInput").autocomplete = setup ? "new-password" : "current-password";
}

async function init() {
  try {
    const response = await fetch("api/auth/status", { headers: { Accept: "application/json" } });
    const status = await response.json();
    if (status.authenticated) { location.replace("./"); return; }
    providers = status.providers || [];
    const select = $("providerSelect");
    select.replaceChildren(...providers.map((p) => { const o = document.createElement("option"); o.value = p.name; o.textContent = p.name; return o; }));
    select.value = status.provider || "demo";
    select.addEventListener("change", updateProviderNote);
    updateProviderNote();
    setMode(!!status.setup_required);
    if (status.configured_username && !status.setup_required) $("usernameInput").value = status.configured_username;
    if (status.setup_required) $("usernameInput").value = status.configured_username || "admin";
  } catch (err) {
    showError(`Cannot reach the server: ${err.message}`);
  }
}

$("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const username = $("usernameInput").value.trim();
  const password = $("passwordInput").value;
  if (setupMode && password !== $("confirmInput").value) { showError("Passwords do not match"); return; }
  const button = $("loginButton");
  button.disabled = true;
  try {
    const body = { username, password };
    if (setupMode) {
      body.provider = $("providerSelect").value;
      const key = $("apiKeyInput").value.trim();
      if (key) body.api_key = key;
    }
    await request(setupMode ? "api/auth/setup" : "api/auth/login", body);
    location.replace("./");
  } catch (err) {
    showError(err.message);
    $("passwordInput").focus();
    $("passwordInput").select();
  } finally {
    button.disabled = false;
  }
});

init();
