/**
 * Ticker autocomplete: Material menu under a text field showing symbol + company name.
 * Suggestions come from the local directory (free, instant); an explicit action row can query
 * the data provider, which costs one request of its budget.
 */

import { api, el } from "./ui.js";

export function attachSymbolAutocomplete({ input, onSelect, minChars = 1, debounceMs = 150, limit = 8 }) {
  const menu = el("div", { class: "menu hidden", role: "listbox", id: `${input.id}Menu` });
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", menu.id);
  input.parentElement.insertAdjacentElement("afterend", menu);

  let items = [];
  let active = -1;
  let timer = null;
  let requestId = 0;
  let lastResponse = null;

  const close = () => { menu.classList.add("hidden"); menu.replaceChildren(); items = []; active = -1; input.setAttribute("aria-expanded", "false"); };

  const render = (response, remote = false) => {
    lastResponse = response;
    items = response.results;
    active = -1;
    menu.replaceChildren();
    for (const [index, item] of items.entries()) {
      const row = el("div", { class: "menu-item", role: "option", id: `${menu.id}-${index}`, "data-symbol": item.symbol, "aria-selected": "false" },
        el("span", { class: "label-large menu-symbol", text: item.symbol }),
        el("span", { class: "body-small muted menu-name", text: item.name }),
        el("span", { class: "label-small menu-kind", text: item.source === "local" ? item.type.toUpperCase() : item.source }));
      row.addEventListener("mousedown", (e) => { e.preventDefault(); select(index); });
      row.addEventListener("mousemove", () => setActive(index));
      menu.append(row);
    }
    if (!items.length) menu.append(el("div", { class: "menu-empty body-small muted", text: `No match for "${response.query}"` }));
    if (response.remote_supported && !remote && response.query.length >= 2) {
      const action = el("div", { class: "menu-item menu-action", role: "option", "aria-selected": "false" },
        el("span", { class: "label-large", text: `Search ${response.provider} for "${response.query}"` }),
        el("span", { class: "body-small muted", text: "uses 1 API request" }));
      action.addEventListener("mousedown", (e) => { e.preventDefault(); search(response.query, true); });
      menu.append(action);
    }
    if (response.remote_error) menu.append(el("div", { class: "menu-empty body-small error-text", text: response.remote_error }));
    menu.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
  };

  const search = async (query, remote = false) => {
    const id = ++requestId;
    try {
      const response = await api.get(`api/symbols/search?q=${encodeURIComponent(query)}&limit=${limit}${remote ? "&remote=true" : ""}`);
      if (id === requestId && document.activeElement === input) render(response, remote);
    } catch { close(); }
  };

  const setActive = (index) => {
    active = index;
    for (const [i, row] of [...menu.querySelectorAll(".menu-item")].entries()) {
      row.classList.toggle("active", i === index);
      row.setAttribute("aria-selected", i === index ? "true" : "false");
    }
    input.setAttribute("aria-activedescendant", index >= 0 ? `${menu.id}-${index}` : "");
  };

  const select = (index) => {
    const item = items[index];
    if (!item) return;
    input.value = item.symbol;
    close();
    onSelect(item);
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < minChars) { close(); return; }
    timer = setTimeout(() => search(query), debounceMs);
  });
  input.addEventListener("keydown", (e) => {
    if (menu.classList.contains("hidden")) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(items.length - 1, active + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(0, active - 1)); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); select(active); }
    else if (e.key === "Escape") { close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("focus", () => { if (input.value.trim().length >= minChars && lastResponse) search(input.value.trim()); });

  return { close, search };
}
