// F8.10: the FIRE editor's adjustment rows, suggestion copy and unsaved-changes guard. The
// client only clones a template and copies server-formatted strings; every amount is parsed and
// validated by the server, and the projection stays server-side.
(function () {
  "use strict";

  function notifyChange() {
    const form = document.querySelector("[data-fire-form]");
    if (form instanceof HTMLElement) {
      form.dispatchEvent(new CustomEvent("fire:changed", { bubbles: true }));
    }
  }

  function rowsFor(kind) {
    return document.querySelector(`[data-${kind}-rows]`);
  }

  function templateFor(kind) {
    return document.querySelector(`[data-${kind}-template]`);
  }

  function addRow(kind) {
    const rows = rowsFor(kind);
    const template = templateFor(kind);
    if (!(rows instanceof HTMLElement) || !(template instanceof HTMLTemplateElement)) {
      return null;
    }
    const index = Number(rows.dataset.nextIndex || "1");
    rows.dataset.nextIndex = String(index + 1);
    const holder = document.createElement("tbody");
    holder.innerHTML = template.innerHTML.replaceAll("__index__", String(index));
    const row = holder.firstElementChild;
    if (!(row instanceof HTMLElement)) return null;
    rows.appendChild(row);
    const first = row.querySelector("input");
    if (first instanceof HTMLElement) first.focus();
    return row;
  }

  // The unsaved-changes indicator and the native beforeunload prompt. A basis change is a view
  // preference, not an edit, so it never marks the form dirty.
  const form = document.querySelector("[data-fire-form]");
  const status = document.querySelector("[data-fire-dirty-status]");
  let dirty = form instanceof HTMLElement && form.dataset.fireDirty === "1";

  function setDirty(value) {
    dirty = value;
    if (form instanceof HTMLElement) form.dataset.fireDirty = value ? "1" : "0";
    if (status instanceof HTMLElement) status.hidden = !value;
  }

  if (form instanceof HTMLElement) {
    form.addEventListener("input", (event) => {
      const target = event.target;
      if (target instanceof HTMLInputElement && target.name === "basis") return;
      setDirty(true);
    });
    form.addEventListener("change", (event) => {
      const target = event.target;
      if (target instanceof HTMLInputElement && target.name === "basis") return;
      setDirty(true);
    });
    form.addEventListener("fire:changed", () => setDirty(true));
    form.addEventListener("submit", () => setDirty(false));
    setDirty(dirty);
  }

  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  // Copy a server-formatted suggestion into its field. No arithmetic or parsing happens here;
  // the value is already written in the interface locale.
  function useSuggestion(button) {
    const fieldName = button.dataset.useSuggestion || "";
    if (!(form instanceof HTMLFormElement) || !fieldName) return;
    const field = form.querySelector(`[name="${CSS.escape(fieldName)}"]`);
    if (!(field instanceof HTMLInputElement)) return;
    field.value = button.dataset.suggestedInput || "";
    field.focus();
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    if (target.closest("[data-add-event]")) {
      addRow("event");
      notifyChange();
      return;
    }
    const remove = target.closest("[data-remove-row]");
    if (remove) {
      remove.closest("[data-editor-row]")?.remove();
      notifyChange();
      return;
    }
    const discard = target.closest("[data-fire-discard]");
    if (discard) {
      setDirty(false);
      return;
    }
    const use = target.closest("[data-use-suggestion][data-suggested-input]");
    if (use instanceof HTMLElement) {
      event.preventDefault();
      useSuggestion(use);
    }
  });

  // A preview error arrives with the results fragment, not a full page. Mark the named input
  // without moving focus, and clear the marker when a later preview succeeds.
  function syncPreviewError() {
    if (!(form instanceof HTMLFormElement)) return;
    form.querySelectorAll("[aria-invalid='true']").forEach((field) => {
      field.removeAttribute("aria-invalid");
    });
    const alert = document.querySelector("[data-fire-preview-error]");
    const name = alert instanceof HTMLElement ? alert.dataset.firePreviewErrorField : "";
    if (!name) return;
    let field = form.querySelector(`[name="${CSS.escape(name)}"]`);
    if (!field && name === "events") {
      field = form.querySelector("[data-event-table]");
    }
    if (field instanceof HTMLElement) field.setAttribute("aria-invalid", "true");
  }

  for (const eventName of ["htmx:afterSwap", "htmx:oobAfterSwap"]) {
    document.addEventListener(eventName, syncPreviewError);
  }
})();
