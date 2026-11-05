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
  if (totalNode) totalNode.textContent = `${total.toFixed(2)}%`;
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
