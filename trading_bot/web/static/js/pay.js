/** Sandbox checkout page of the mock payment provider (served at pay/mock/<session>). */

const $ = (id) => document.getElementById(id);
const sessionId = decodeURIComponent(location.pathname.split("/").pop());
const API = "../../api/payments/mock/";
const fmt = (v, currency) => new Intl.NumberFormat(undefined, { style: "currency", currency }).format(v);

async function request(path, method = "GET") {
  const response = await fetch(path, { method, headers: { Accept: "application/json" } });
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `${response.status} ${response.statusText}`);
  return data;
}

function setBusy(busy) { $("payButton").disabled = busy; $("cancelButton").disabled = busy; }

async function init() {
  try {
    const session = await request(API + encodeURIComponent(sessionId));
    $("paySummary").replaceChildren(
      ...[["Merchant", "Trading Bot"], ["Account", session.account], ["Description", session.description || "Account deposit"],
          ["Amount", fmt(session.amount, session.currency)], ["Status", session.status]]
        .flatMap(([k, v]) => [Object.assign(document.createElement("dt"), { textContent: k }), Object.assign(document.createElement("dd"), { textContent: v })]));
    $("payButtonLabel").textContent = `Pay ${fmt(session.amount, session.currency)}`;
    if (session.status !== "open") {
      $("payNotice").textContent = `This checkout is already ${session.status}.`;
      $("payNotice").className = "notice error";
      $("payButton").disabled = true;
      return;
    }
    $("payNotice").textContent = "Confirm the payment to credit your paper account, or cancel to go back.";
    $("payNotice").className = "notice";
  } catch (err) {
    $("payNotice").textContent = err.message;
    $("payNotice").className = "notice error";
    $("payButton").disabled = true;
  }
}

async function act(action) {
  setBusy(true);
  $("payError").textContent = "";
  try {
    const result = await request(API + encodeURIComponent(sessionId) + "/" + action, "POST");
    location.replace(result.redirect || "../../");
  } catch (err) {
    $("payError").textContent = err.message;
    setBusy(false);
  }
}

$("payButton").addEventListener("click", () => act("pay"));
$("cancelButton").addEventListener("click", () => act("cancel"));
init();
