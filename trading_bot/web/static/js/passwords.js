/**
 * Password requirements, evaluated live while typing.
 * Mirrors trading_bot/web/auth.py (the server stays authoritative on submit).
 */

export const MIN_LENGTH = 10;
export const MAX_LENGTH = 128;
const COMMON = new Set(["password", "password1", "password123", "passw0rd", "p@ssw0rd", "123456", "12345678", "123456789",
  "1234567890", "qwerty", "qwerty123", "qwertyuiop", "abc123", "letmein", "welcome", "welcome1", "admin", "admin123",
  "administrator", "iloveyou", "monkey", "dragon", "sunshine", "princess", "football", "baseball", "master", "login",
  "trading", "trader", "trading123", "bitcoin", "changeme", "secret", "test1234"]);

export const RULES = [
  ["length", `At least ${MIN_LENGTH} characters`],
  ["lowercase", "A lowercase letter"],
  ["uppercase", "An uppercase letter"],
  ["digit", "A digit"],
  ["symbol", "A symbol (e.g. ! ? - _ #)"],
  ["no_edges", "No leading or trailing spaces"],
  ["not_username", "Does not contain the username"],
  ["not_common", "Not a commonly used password"],
  ["max_length", `At most ${MAX_LENGTH} characters`],
];

export function evaluatePassword(password, username = "") {
  const user = (username || "").trim().toLowerCase();
  const lowered = password.toLowerCase();
  const results = {
    length: password.length >= MIN_LENGTH,
    lowercase: /\p{Ll}/u.test(password),
    uppercase: /\p{Lu}/u.test(password),
    digit: /\d/.test(password),
    symbol: /[^\p{L}\p{N}\s]/u.test(password),
    no_edges: password === password.trim(),
    not_username: !user || user.length < 3 || !lowered.includes(user),
    not_common: !COMMON.has(lowered) && !COMMON.has(lowered.replace(/[0-9!?.]+$/, "")),
    max_length: password.length <= MAX_LENGTH,
  };
  const rules = RULES.map(([id, label]) => ({ id, label, passed: results[id] }));
  const passed = rules.every((r) => r.passed);
  const core = ["lowercase", "uppercase", "digit", "symbol"].filter((id) => results[id]).length;
  let score;
  if (!passed) score = Math.min(core, 2);
  else if (password.length >= 16 && core === 4) score = 4;
  else score = core >= 3 ? 3 : 2;
  return { rules, passed, score };
}

const LABELS = ["Too weak", "Weak", "Fair", "Good", "Strong"];

/**
 * Live rule checklist + strength meter under a password field.
 * `usernameInput` (element) or `username` (function returning a string) feeds the "must not contain the username" rule.
 */
export function attachPasswordFeedback({ input, usernameInput = null, username = null, container, confirmInput = null }) {
  const currentUsername = () => (usernameInput ? usernameInput.value : username ? username() : "");
  container.classList.add("pw-feedback");
  container.innerHTML = `
    <div class="pw-meter" role="meter" aria-label="Password strength" aria-valuemin="0" aria-valuemax="4"><span></span><span></span><span></span><span></span></div>
    <div class="pw-meter-label body-small muted"></div>
    <ul class="pw-rules body-small" aria-live="polite"></ul>`;
  const list = container.querySelector(".pw-rules");
  const meter = container.querySelector(".pw-meter");
  const label = container.querySelector(".pw-meter-label");
  list.replaceChildren(...RULES.map(([id, text]) => {
    const li = document.createElement("li");
    li.dataset.rule = id;
    li.innerHTML = `<span class="pw-icon" aria-hidden="true"></span><span>${text}</span>`;
    return li;
  }));
  let last = evaluatePassword("");

  const update = () => {
    last = evaluatePassword(input.value, currentUsername());
    for (const li of list.children) {
      const rule = last.rules.find((r) => r.id === li.dataset.rule);
      li.classList.toggle("passed", !!rule?.passed);
      li.classList.toggle("failed", !rule?.passed && input.value.length > 0);
    }
    meter.dataset.score = String(input.value ? last.score : 0);
    meter.setAttribute("aria-valuenow", String(last.score));
    label.textContent = !input.value ? "" : last.passed ? LABELS[last.score] : `${LABELS[last.score]} · requirements not met`;
    container.classList.toggle("all-passed", last.passed);
    if (confirmInput) {
      const matches = confirmInput.value === input.value;
      confirmInput.closest(".text-field")?.classList.toggle("invalid", confirmInput.value.length > 0 && !matches);
    }
  };
  input.addEventListener("input", update);
  usernameInput?.addEventListener("input", update);
  confirmInput?.addEventListener("input", update);
  update();
  return {
    isValid: () => last.passed,
    failures: () => last.rules.filter((r) => !r.passed).map((r) => r.label),
    refresh: update,
  };
}

/** Add a show/hide toggle to every password field inside `root` (Material icon button in the field). */
export function enhancePasswordFields(root = document) {
  for (const input of root.querySelectorAll('input[type="password"]')) {
    const field = input.closest(".field");
    if (!field || field.querySelector(".pw-toggle")) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "icon-btn pw-toggle";
    button.title = "Show password";
    button.setAttribute("aria-label", "Show password");
    button.setAttribute("aria-pressed", "false");
    button.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-eye"/></svg>';
    button.addEventListener("click", () => {
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.setAttribute("aria-pressed", reveal ? "true" : "false");
      button.title = reveal ? "Hide password" : "Show password";
      button.querySelector("use").setAttribute("href", reveal ? "#i-eye-off" : "#i-eye");
      input.focus();
    });
    field.append(button);
  }
}
