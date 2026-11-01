/** Sign-up page with live password requirements and the e-mail verification flow. */

import { attachPasswordFeedback, enhancePasswordFields } from "./passwords.js";

const $ = (id) => document.getElementById(id);
let verificationRequired = false;

async function request(path, body) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
                                       body: JSON.stringify(body) });
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `${response.status} ${response.statusText}`);
  return data;
}

const feedback = attachPasswordFeedback({ input: $("passwordInput"), usernameInput: $("usernameInput"),
                                          container: $("passwordFeedback"), confirmInput: $("confirmInput") });
enhancePasswordFields();

async function init() {
  try {
    const status = await (await fetch("api/auth/status", { headers: { Accept: "application/json" } })).json();
    if (status.authenticated) { location.replace("./"); return; }
    if (status.setup_required || !status.allow_signup) { location.replace("login"); return; }
    verificationRequired = !!status.verification_required;
    $("emailInput").required = verificationRequired;
    $("emailLabel").textContent = verificationRequired ? "E-mail" : "E-mail (optional)";
    $("emailHint").textContent = verificationRequired
      ? "We will send a verification link; the account is activated once you open it."
      : "Used to sign in and for notifications. E-mail verification is not enabled on this server.";
  } catch (err) {
    $("signupError").textContent = `Cannot reach the server: ${err.message}`;
  }
}

$("signupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("signupError");
  error.textContent = "";
  if (!feedback.isValid()) { error.textContent = `Password requirements not met: ${feedback.failures().join("; ")}`; return; }
  if ($("passwordInput").value !== $("confirmInput").value) { error.textContent = "Passwords do not match"; return; }
  const body = { username: $("usernameInput").value.trim(), password: $("passwordInput").value };
  const email = $("emailInput").value.trim();
  if (email) body.email = email;
  $("signupButton").disabled = true;
  try {
    const result = await request("api/auth/signup", body);
    if (result.verification_required) {
      $("signupForm").classList.add("hidden");
      $("verifyPanel").classList.remove("hidden");
      $("verifyText").textContent = `Account created. We sent a verification link to ${result.email}. Open it to activate your account, then sign in.`;
    } else {
      location.replace("./");
    }
  } catch (err) {
    error.textContent = err.message;
  } finally {
    $("signupButton").disabled = false;
  }
});

$("resendButton").addEventListener("click", async () => {
  try {
    const result = await request("api/auth/resend-verification", { identifier: $("usernameInput").value.trim() });
    $("resendStatus").textContent = result.message;
  } catch (err) {
    $("resendStatus").textContent = err.message;
  }
});

init();
