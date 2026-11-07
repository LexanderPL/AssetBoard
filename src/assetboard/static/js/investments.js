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
