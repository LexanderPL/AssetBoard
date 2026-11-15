// F8.8: the FIRE plan form's adjustment rows. The client only clones a template; every amount
// is parsed and validated by the server.
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
    }
  });

  // A preview error arrives with the results fragment, not a full page. Mark the named input
  // without moving focus, and clear the marker when a later preview succeeds.
  function syncPreviewError() {
    const form = document.querySelector("[data-fire-form]");
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
