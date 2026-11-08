// F6.1: the instrument allocation editor's live total. The server is the source of truth; this
// only mirrors it so the user sees when the weights are not exactly 100% before saving.
// Amounts are percentages (not money), so plain Number arithmetic is enough here.

function allocationTotal(editor) {
  let total = 0;
  let invalid = false;
  for (const input of editor.querySelectorAll("[data-allocation-weight]")) {
    const raw = input.value.trim().replace(",", ".");
    if (raw === "") continue;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      invalid = true;
      continue;
    }
    total += value;
  }
  return { total: Math.round(total * 100) / 100, invalid };
}

function updateAllocationEditor(editor) {
  const { total, invalid } = allocationTotal(editor);
  const totalNode = editor.querySelector("[data-allocation-total]");
  const warning = editor.querySelector("[data-allocation-warning]");
  const submit = editor.querySelector("[data-allocation-submit]");
  const separator = editor.getAttribute("data-decimal-separator") || ".";
  if (totalNode) totalNode.textContent = `${total.toFixed(2).replace(".", separator)}%`;
  const valid = !invalid && total === 100;
  if (warning) warning.classList.toggle("hidden", valid);
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
  const price = form.querySelector("[data-transaction-price]");
  if (price) {
    const isCurrency = type === "currency";
    price.readOnly = isCurrency;
    if (isCurrency) price.value = "1";
  }
  const fx = form.querySelector("[data-transaction-fx]");
  if (fx) fx.classList.toggle("hidden", currency === form.dataset.baseCurrency);
}

document.addEventListener("change", (event) => {
  const select = event.target;
  if (!(select instanceof Element) || !select.matches("[data-transaction-instrument]")) return;
  const form = select.closest("[data-transaction-form]");
  if (form) updateTransactionForm(form);
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
  if (!target.matches("#instrument-type, [data-provider-select]")) return;
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
