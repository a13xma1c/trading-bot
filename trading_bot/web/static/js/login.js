/** Login / first-run setup page. */

import { attachPasswordFeedback, enhancePasswordFields } from "./passwords.js";

const $ = (id) => document.getElementById(id);
let setupMode = false;
let feedback = null;

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

function showNotice(text, error = false) {
  const notice = $("loginNotice");
  notice.textContent = text;
  notice.className = `notice ${error ? "error" : "success"}`;
}

$("resendButton").addEventListener("click", async () => {
  try {
    const result = await request("api/auth/resend-verification", { identifier: $("usernameInput").value.trim() });
    showNotice(result.message);
    showError("");
  } catch (err) {
    showError(err.message);
  }
});

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
  $("passwordFeedback").classList.toggle("hidden", !setup);
  $("setupDataFields").classList.toggle("hidden", !setup);
  $("emailField").classList.toggle("hidden", !setup);
  $("usernameLabel").textContent = setup ? "Username" : "Username or e-mail";
  $("usernameInput").autocomplete = setup ? "username" : "username email";
  if (setup && !feedback) {
    feedback = attachPasswordFeedback({ input: $("passwordInput"), usernameInput: $("usernameInput"),
                                        container: $("passwordFeedback"), confirmInput: $("confirmInput") });
  }
  $("confirmInput").required = setup;
  $("loginSubtitle").textContent = setup ? "Create the administrator account" : "Sign in to continue";
  $("loginButtonLabel").textContent = setup ? "Create account" : "Sign in";
  $("loginHint").textContent = setup
    ? "No administrator account exists yet. The account you create here is stored (password hashed) in the bot's database; change the password later under Settings or reset it with `trading-bot set-password`."
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
    $("signupLink").classList.toggle("hidden", !status.allow_signup);
    if (status.setup_required) $("usernameInput").value = "admin";
    const params = new URLSearchParams(location.search);
    if (params.get("verified")) showNotice("Your e-mail address is verified - you can sign in now.");
    if (params.get("verify_error")) showNotice(params.get("verify_error"), true);
    if (params.get("registered")) showNotice(`Account created. We sent a verification link to ${params.get("registered")}; open it, then sign in.`);
  } catch (err) {
    showError(`Cannot reach the server: ${err.message}`);
  }
}

$("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const username = $("usernameInput").value.trim();
  const password = $("passwordInput").value;
  if (setupMode && feedback && !feedback.isValid()) { showError(`Password requirements not met: ${feedback.failures().join("; ")}`); return; }
  if (setupMode && password !== $("confirmInput").value) { showError("Passwords do not match"); return; }
  const button = $("loginButton");
  button.disabled = true;
  try {
    const body = { username, password };
    if (setupMode) {
      body.provider = $("providerSelect").value;
      const key = $("apiKeyInput").value.trim();
      if (key) body.api_key = key;
      const email = $("emailInput").value.trim();
      if (email) body.email = email;
    }
    await request(setupMode ? "api/auth/setup" : "api/auth/login", body);
    location.replace("./");
  } catch (err) {
    showError(err.message);
    $("resendButton").classList.toggle("hidden", !/verify your e-mail/i.test(err.message));
    $("passwordInput").focus();
    $("passwordInput").select();
  } finally {
    button.disabled = false;
  }
});

enhancePasswordFields();
init();
