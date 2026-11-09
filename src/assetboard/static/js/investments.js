// F6.1: the instrument allocation editor's live total. The server is the source of truth; this
// only mirrors it so the user sees when the weights are not exactly 100% before saving.
// Amounts are percentages (not money), so plain Number arithmetic is enough here.

function allocationTotal(editor) {
  let total = 0;
  let invalid = false;
  let entered = false;
  for (const input of editor.querySelectorAll("[data-allocation-weight]")) {
    const raw = input.value.trim().replace(",", ".");
    if (raw === "") continue;
    entered = true;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      invalid = true;
      continue;
    }
    total += value;
  }
  return { total: Math.round(total * 100) / 100, invalid, entered };
}

function updateAllocationEditor(editor) {
  const { total, invalid, entered } = allocationTotal(editor);
  const totalNode = editor.querySelector("[data-allocation-total]");
  const warning = editor.querySelector("[data-allocation-warning]");
  const submit = editor.querySelector("[data-allocation-submit]");
  const separator = editor.getAttribute("data-decimal-separator") || ".";
  if (totalNode) totalNode.textContent = `${total.toFixed(2).replace(".", separator)}%`;
  const valid = !invalid && total === 100;
  // A fresh form (nothing typed yet, e.g. right after a successful add) is not an error: the
  // warning waits for the first weight. Saving still needs exactly 100%.
  if (warning) warning.classList.toggle("hidden", valid || !entered);
  if (submit) submit.disabled = !valid;
}

function updateAllocationEditors(root = document) {
  for (const editor of root.querySelectorAll("[data-allocation-editor]")) {
    updateAllocationEditor(editor);
  }
}

document.addEventListener("input", (event) => {
  const input = event.target;
  if (!(input instanceof Element) || !input.matches("[data-allocation-weight]")) return;
  const editor = input.closest("[data-allocation-editor]");
  if (editor) updateAllocationEditor(editor);
});

for (const eventName of ["DOMContentLoaded", "htmx:afterSwap", "htmx:oobAfterSwap"]) {
  document.addEventListener(eventName, () => updateAllocationEditors());
}

updateAllocationEditors();

// F6.2: the global transaction form knows the selected instrument only after a change. Keep the
// currency-instrument price at 1 and hide the FX field for base-currency instruments; the
// server enforces both regardless of JavaScript.
function updateTransactionForm(form) {
  const select = form.querySelector("[data-transaction-instrument]");
  if (!select) return;
  const option = select.selectedOptions[0];
  const type = option ? option.dataset.type || "" : "";
  const currency = option ? option.dataset.currency || "" : "";
  const kindField = form.querySelector("[name=type]");
  const kind = kindField ? kindField.value : "buy";
  const isTrade = kind === "buy" || kind === "sell";
  for (const field of form.querySelectorAll("[data-trade-field]")) {
    field.classList.toggle("hidden", !isTrade);
  }
  for (const field of form.querySelectorAll("[data-dividend-field]")) {
    field.classList.toggle("hidden", kind !== "dividend");
  }
  for (const field of form.querySelectorAll("[data-split-field]")) {
    field.classList.toggle("hidden", kind !== "split");
  }
  const price = form.querySelector("[data-transaction-price]");
  if (price) {
    const isCurrency = type === "currency";
    price.readOnly = isCurrency;
    if (isCurrency) price.value = "1";
  }
  const fx = form.querySelector("[data-transaction-fx]");
  if (fx) {
    fx.classList.toggle(
      "hidden",
      kind === "split" || currency === form.dataset.baseCurrency,
    );
  }
}

// F6.3: the server renders each option's stored FX (manual or provider) for today. When the user
// picks another instrument, a field that is empty or still holds the previous pre-fill follows
// it; a rate the user typed is kept. Only a real change does this, so a refused form keeps
// exactly what was submitted.
function applyStoredFx(form) {
  const select = form.querySelector("[data-transaction-instrument]");
  const input = form.querySelector("[data-fx-input]");
  if (!select || !(input instanceof HTMLInputElement)) return;
  // The options' rates are for the date the form was rendered with; after a date change the
  // F6.2a refill asks the server for the new date instead.
  if (form.dataset.fxDateChanged === "1") {
    refillStoredFx(form);
    return;
  }
  const option = select.selectedOptions[0];
  if (!option || !("fx" in option.dataset)) return;
  const previous = input.dataset.prefilled || "";
  if (input.value === "" || input.value === previous) {
    input.value = option.dataset.fx || "";
    input.dataset.prefilled = input.value;
  }
  showFxHint(form);
}

function showFxHint(form) {
  const input = form.querySelector("[data-fx-input]");
  const hint = form.querySelector("[data-fx-hint]");
  if (!(input instanceof HTMLInputElement) || !hint) return;
  const prefilled = input.dataset.prefilled || "";
  hint.classList.toggle("hidden", prefilled === "" || input.value !== prefilled);
}

// A pre-filled stored rate belongs to the form's date. Changing the date clears a rate the user
// has not touched (it would be the wrong day's rate, frozen into the cost basis), then F6.2a
// re-fills it with the stored rate for the new date when one exists; a typed rate is kept.
function dateChanged(form) {
  form.dataset.fxDateChanged = "1";
  const input = form.querySelector("[data-fx-input]");
  if (!(input instanceof HTMLInputElement)) return;
  const prefilled = input.dataset.prefilled || "";
  const untouched = input.value === "" || (prefilled !== "" && input.value === prefilled);
  if (prefilled !== "" && input.value === prefilled) input.value = "";
  input.dataset.prefilled = "";
  showFxHint(form);
  if (untouched) refillStoredFx(form);
}

for (const eventName of ["change", "input"]) {
  document.addEventListener(eventName, (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    const form = target.closest("[data-transaction-form]");
    if (!form) return;
    if (target.name === "date") dateChanged(form);
    else if (target.matches("[data-fx-input]")) showFxHint(form);
  });
}

document.addEventListener("change", (event) => {
  const select = event.target;
  if (!(select instanceof Element) || !select.matches("[data-transaction-instrument]")) return;
  const form = select.closest("[data-transaction-form]");
  if (!form) return;
  updateTransactionForm(form);
  applyStoredFx(form);
});

for (const eventName of ["DOMContentLoaded", "htmx:afterSwap", "htmx:oobAfterSwap"]) {
  document.addEventListener(eventName, () => {
    for (const form of document.querySelectorAll("[data-transaction-form]")) {
      updateTransactionForm(form);
    }
  });
}

for (const form of document.querySelectorAll("[data-transaction-form]")) {
  updateTransactionForm(form);
}


// F6.4: the provider fields only apply to priced instruments, not currency or bond-model rows.
function updateInstrumentProvider(form) {
  const fields = form.querySelector("[data-provider-fields]");
  if (!fields) return;
  const typeSelect = form.querySelector("select[name=type]");
  const type = typeSelect ? typeSelect.value : "";
  const hidden = type === "currency" || type === "bond_model";
  fields.classList.toggle("hidden", hidden);
  if (hidden) {
    const provider = form.querySelector("[data-provider-select]");
    const symbol = form.querySelector("input[name=provider_symbol]");
    if (provider) provider.value = "";
    if (symbol) symbol.value = "";
  }
}

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (!target.matches("select[name=type], [data-provider-select]")) return;
  const form = target.closest("[data-instrument-form]");
  if (form) updateInstrumentProvider(form);
});

for (const eventName of ["DOMContentLoaded", "htmx:afterSwap", "htmx:oobAfterSwap"]) {
  document.addEventListener(eventName, () => {
    for (const form of document.querySelectorAll("[data-instrument-form]")) {
      updateInstrumentProvider(form);
    }
  });
}

for (const form of document.querySelectorAll("[data-instrument-form]")) {
  updateInstrumentProvider(form);
}

// F6.5: the bond rate schedule grows and shrinks with simple row controls.
document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const add = target.closest("[data-add-period]");
  if (add) {
    const form = add.closest("[data-bond-form]");
    if (!form) return;
    const first = form.querySelector("[data-bond-period]");
    if (!first) return;
    const clone = first.cloneNode(true);
    for (const input of clone.querySelectorAll("input")) input.value = "";
    add.before(clone);
    const input = clone.querySelector("input");
    if (input) input.focus();
    return;
  }
  const remove = target.closest("[data-remove-period]");
  if (remove) {
    const row = remove.closest("[data-bond-period]");
    if (!row) return;
    const list = row.parentElement;
    if (list && list.querySelectorAll("[data-bond-period]").length > 1) {
      row.remove();
    } else {
      for (const input of row.querySelectorAll("input")) input.value = "";
    }
  }
});

// F6.2a: "Amounts in" quote | base. Only the chosen group of amount fields is shown; the server
// reads the group that matches the choice, so without JavaScript both groups simply stay visible.
// On the global form the choice applies only to an instrument quoted in another currency (not a
// currency instrument); for the others it is hidden and reset to the quote currency.
function selectedInstrument(form) {
  const select = form.querySelector("[data-transaction-instrument]");
  return select ? select.selectedOptions[0] || null : null;
}

function updateAmounts(form) {
  const choice = form.querySelector("[data-amounts-choice]");
  if (!choice) return;
  const option = selectedInstrument(form);
  if (option) {
    const currency = option.dataset.currency || "";
    const allowed = currency !== "" && currency !== form.dataset.baseCurrency
      && option.dataset.type !== "currency";
    choice.classList.toggle("hidden", !allowed);
    const label = choice.querySelector("[data-amounts-quote-label]");
    if (label && currency) label.textContent = currency;
    if (!allowed) {
      const quote = choice.querySelector("[data-amounts-radio][value=quote]");
      if (quote) quote.checked = true;
    }
  }
  const checked = choice.querySelector("[data-amounts-radio]:checked");
  const mode = checked ? checked.value : "quote";
  for (const group of form.querySelectorAll("[data-amounts-group]")) {
    group.classList.toggle("hidden", group.dataset.amountsGroup !== mode);
  }
}

// F6.2a: after a date change, re-fill an empty FX field from the stored rate for the new date
// (read-only, workspace-scoped endpoint). Without a stored rate the field stays empty, so a stale
// rate never sticks. A rate typed while the request runs is kept; only the latest request counts.
let fxRequest = 0;

async function refillStoredFx(form) {
  const input = form.querySelector("[data-fx-input]");
  const url = form.dataset.fxUrl;
  if (!(input instanceof HTMLInputElement) || !url) return;
  if (input.value !== "" && input.value !== (input.dataset.prefilled || "")) return;
  const instrument = form.querySelector("[name=instrument_id]");
  const day = form.querySelector("[name=date]");
  if (!instrument || !instrument.value || !day || !day.value) return;
  const ticket = ++fxRequest;
  const query = new URLSearchParams({ instrument_id: instrument.value, date: day.value });
  let data;
  try {
    const response = await fetch(`${url}?${query}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    data = await response.json();
  } catch {
    return;
  }
  if (ticket !== fxRequest) return;
  if (input.value !== "" && input.value !== (input.dataset.prefilled || "")) return;
  input.value = data.rate || "";
  input.dataset.prefilled = input.value;
  const hint = form.querySelector("[data-fx-hint]");
  if (hint && data.hint) hint.textContent = data.hint;
  showFxHint(form);
}

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const form = target.closest("[data-transaction-form]");
  if (!form) return;
  if (target.matches("[data-amounts-radio], [data-transaction-instrument]")) updateAmounts(form);
  if (target.matches("[name=type]")) {
    updateTransactionForm(form);
    updateAmounts(form);
  }
});

for (const eventName of ["DOMContentLoaded", "htmx:afterSwap", "htmx:oobAfterSwap"]) {
  document.addEventListener(eventName, () => {
    for (const form of document.querySelectorAll("[data-transaction-form]")) updateAmounts(form);
  });
}

for (const form of document.querySelectorAll("[data-transaction-form]")) updateAmounts(form);
