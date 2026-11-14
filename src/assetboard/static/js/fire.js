// F8.1: the FIRE scenario form's row editors and the contribution paste helper. The client
// only clones a template and fills values; every amount is parsed and validated by the server.
(function () {
  "use strict";

  const PAIR = /^\s*(\d{4})\s*[,\t]\s*(.+?)\s*$/;

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

  function parsePaste(text, startYear) {
    const normalized = text.replace(/\r\n?/g, "\n");
    const lines = normalized.split("\n");
    const filled = lines.filter((line) => line.trim() !== "");
    if (filled.length === 0) return [];
    if (filled.every((line) => PAIR.test(line))) {
      const pairs = filled.map((line) => {
        const match = PAIR.exec(line);
        return { year: Number(match[1]), amount: match[2] };
      });
      pairs.sort((left, right) => left.year - right.year);
      return pairs;
    }
    const result = [];
    let year = startYear + 1;
    let started = false;
    for (const line of lines) {
      if (line.trim() === "") {
        // Leading blank lines are ignored; a blank line inside the column skips a year.
        if (started) year += 1;
        continue;
      }
      started = true;
      result.push({ year, amount: line.trim() });
      year += 1;
    }
    return result;
  }

  function fillContributions(button) {
    const textarea = document.querySelector("[data-contribution-paste]");
    const status = document.querySelector("[data-paste-status]");
    const rows = rowsFor("contribution");
    if (!(textarea instanceof HTMLTextAreaElement) || !(rows instanceof HTMLElement)) return;
    const form = button.closest("[data-fire-form]");
    const startYear = Number(form instanceof HTMLElement ? form.dataset.startYear : "");
    const items = parsePaste(
      textarea.value,
      Number.isFinite(startYear) ? startYear : new Date().getFullYear()
    );
    if (items.length === 0) {
      if (status instanceof HTMLElement) {
        status.textContent = button.dataset.emptyMessage || "";
      }
      return;
    }
    rows.replaceChildren();
    rows.dataset.nextIndex = "1";
    for (const item of items) {
      const row = addRow("contribution");
      if (!(row instanceof HTMLElement)) continue;
      const year = row.querySelector("[data-contribution-year]");
      const amount = row.querySelector("[data-contribution-amount]");
      if (year instanceof HTMLInputElement) year.value = String(item.year);
      if (amount instanceof HTMLInputElement) amount.value = item.amount;
    }
    if (status instanceof HTMLElement) {
      status.textContent = button.dataset.filledMessage || "";
    }
  }

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    if (target.closest("[data-add-contribution]")) {
      addRow("contribution");
      notifyChange();
      return;
    }
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
    const paste = target.closest("[data-paste-contributions]");
    if (paste instanceof HTMLElement) {
      fillContributions(paste);
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
    if (!field && name === "contributions") {
      field = form.querySelector("[data-contribution-table]");
    }
    if (!field && name === "events") {
      field = form.querySelector("[data-event-table]");
    }
    if (field instanceof HTMLElement) field.setAttribute("aria-invalid", "true");
  }

  for (const eventName of ["htmx:afterSwap", "htmx:oobAfterSwap"]) {
    document.addEventListener(eventName, syncPreviewError);
  }
})();
