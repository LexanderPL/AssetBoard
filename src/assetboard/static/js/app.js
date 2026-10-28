// F3.5: the forecast toggle of the month summary hides its Expected and Forecast columns and the
// forecast KPI line (Home keeps them). The choice is a per-device convenience kept in browser
// storage, and it is applied again after every htmx swap, since planned-value and transaction
// writes replace summary rows (or the whole summary) with freshly rendered, visible cells.
let showForecast = true;
try {
  showForecast = localStorage.getItem("forecast") !== "hidden";
} catch {
  // No storage: the forecast stays visible.
}

function applyForecast() {
  document.querySelectorAll("[data-forecast-toggle]").forEach((button) => {
    button.setAttribute("aria-pressed", String(showForecast));
  });
  document.querySelectorAll("[data-month-summary] [data-forecast-column]").forEach((column) => {
    column.classList.toggle("hidden", !showForecast);
  });
  // "not counted" spans Actual, Expected, Forecast and Difference; without the two forecast
  // columns it spans two.
  document.querySelectorAll("[data-month-summary] td[data-not-counted]").forEach((cell) => {
    if (cell instanceof HTMLTableCellElement) cell.colSpan = showForecast ? 4 : 2;
  });
}

applyForecast();
for (const eventName of ["htmx:afterSwap", "htmx:oobAfterSwap"]) {
  document.addEventListener(eventName, applyForecast);
}

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;

  const themeToggle = target.closest("[data-theme-toggle]");
  if (themeToggle) {
    const dark = document.documentElement.classList.toggle("dark");
    try {
      localStorage.setItem("theme", dark ? "dark" : "light");
    } catch {
      // The visual toggle still works when browser storage is unavailable.
    }
  }

  if (target.closest("[data-forecast-toggle]")) {
    showForecast = !showForecast;
    try {
      localStorage.setItem("forecast", showForecast ? "shown" : "hidden");
    } catch {
      // The toggle still works for this page when browser storage is unavailable.
    }
    applyForecast();
  }

  const themeReset = target.closest("[data-theme-reset]");
  if (themeReset) {
    // Drop this device's override: the saved profile theme (or the system setting) applies.
    try {
      localStorage.removeItem("theme");
    } catch {
      // Nothing stored, nothing to remove.
    }
    const saved = document.documentElement.dataset.theme;
    const dark =
      saved === "dark" ||
      (saved !== "light" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
  }

  const dismiss = target.closest("[data-toast-dismiss]");
  if (dismiss) dismiss.closest("[role='status']")?.remove();

  const cancel = target.closest("[data-confirm-cancel]");
  if (cancel) cancel.closest("dialog")?.close("cancel");

  const accept = target.closest("[data-confirm-accept]");
  if (accept) accept.closest("dialog")?.close("confirm");
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const dialog = document.getElementById("confirm-dialog");
  if (!(dialog instanceof HTMLDialogElement) || !dialog.open) return;
  event.preventDefault();
  dialog.close("cancel");
});

const toastTimers = new WeakMap();

function startToastTimer(toast) {
  if (!(toast instanceof HTMLElement) || !toast.matches("[data-toast]") || toastTimers.has(toast)) {
    return;
  }
  // An undo toast is strict: the server's undo window does not pause, so neither does it.
  const strict = toast.hasAttribute("data-toast-strict");
  const given = Number(toast.dataset.toastSeconds);
  const seconds = toast.dataset.toastSeconds !== undefined && Number.isFinite(given) ? given : 10;
  const remaining = strict || toast.querySelector("[data-toast-undo]") ? seconds * 1000 : 5000;
  const timer = { remaining, startedAt: 0, timeoutId: 0 };
  const pauseReasons = new Set();
  const resume = () => {
    if (timer.timeoutId || timer.remaining <= 0 || pauseReasons.size > 0) return;
    timer.startedAt = Date.now();
    timer.timeoutId = window.setTimeout(() => toast.remove(), timer.remaining);
  };
  const pause = (reason) => {
    pauseReasons.add(reason);
    if (!timer.timeoutId) return;
    window.clearTimeout(timer.timeoutId);
    timer.timeoutId = 0;
    timer.remaining -= Date.now() - timer.startedAt;
  };
  const unpause = (reason) => {
    pauseReasons.delete(reason);
    resume();
  };
  if (strict) {
    const countdown = toast.querySelector("[data-toast-countdown]");
    const end = Date.now() + remaining;
    const tick = () => {
      const left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      if (countdown) countdown.textContent = String(left);
      if (left > 0 && toast.isConnected) window.setTimeout(tick, 250);
    };
    tick();
  } else {
    toast.addEventListener("mouseenter", () => pause("hover"));
    toast.addEventListener("mouseleave", () => unpause("hover"));
    toast.addEventListener("focusin", () => pause("focus"));
    toast.addEventListener("focusout", (event) => {
      if (!toast.contains(event.relatedTarget)) unpause("focus");
    });
  }
  toastTimers.set(toast, timer);
  resume();
}

document.querySelectorAll("[data-toast]").forEach(startToastTimer);
for (const eventName of ["htmx:oobAfterSwap", "htmx:afterSwap"]) {
  document.addEventListener(eventName, (event) => {
    const target = event.detail?.target;
    if (target instanceof Element && target.matches("[data-toast]")) startToastTimer(target);
    target?.querySelectorAll("[data-toast]").forEach(startToastTimer);
  });
}

const confirmedPlainForms = new WeakSet();

function showConfirmation(message, onConfirm) {
  const dialog = document.getElementById("confirm-dialog");
  if (!(dialog instanceof HTMLDialogElement)) return;
  const messageElement = dialog.querySelector("[data-confirm-message]");
  if (messageElement) messageElement.textContent = message;
  dialog.addEventListener(
    "close",
    () => {
      if (dialog.returnValue === "confirm") onConfirm();
    },
    { once: true },
  );
  dialog.returnValue = "";
  dialog.showModal();
}

document.addEventListener("htmx:confirm", (event) => {
  const trigger = event.target;
  if (!(trigger instanceof Element)) return;
  const message = trigger.getAttribute("data-confirm");
  if (message === null) return;
  event.preventDefault();
  showConfirmation(message, () => event.detail.issueRequest(true));
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) return;
  const hasHxAttributes = Array.from(form.attributes).some(
    ({ name }) => name.startsWith("hx-") || name.startsWith("data-hx-"),
  );
  if (hasHxAttributes) return;
  if (confirmedPlainForms.has(form)) {
    confirmedPlainForms.delete(form);
    return;
  }
  event.preventDefault();
  showConfirmation(form.dataset.confirm, () => {
    confirmedPlainForms.add(form);
    form.requestSubmit();
  });
});

// Dialogs marked data-dialog-modal (the transaction export): a link marked
// data-dialog-open="<id>" opens one as a modal, a link marked data-dialog-close inside closes
// it. Both are links, so without JavaScript they load a page that renders the dialog open (in
// the page flow) or closed. A dialog the server rendered open becomes a modal here; a submitted
// form inside one closes it (a download leaves the page as it is).
function openModalDialog(dialog) {
  if (!(dialog instanceof HTMLDialogElement) || !dialog.hasAttribute("data-dialog-modal")) {
    return false;
  }
  if (dialog.open) dialog.close();
  dialog.showModal();
  return true;
}

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const opener = target.closest("[data-dialog-open]");
  if (opener) {
    const dialog = document.getElementById(opener.getAttribute("data-dialog-open") || "");
    if (openModalDialog(dialog)) event.preventDefault();
    return;
  }
  const closer = target.closest("[data-dialog-close]");
  const dialog = closer?.closest("dialog[data-dialog-modal]");
  if (dialog instanceof HTMLDialogElement && dialog.open) {
    event.preventDefault();
    dialog.close();
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const dialog = form.closest("dialog[data-dialog-modal]");
  if (dialog instanceof HTMLDialogElement) window.setTimeout(() => dialog.close(), 0);
});

document.querySelectorAll("dialog[data-dialog-modal][open]").forEach(openModalDialog);

// The month picker is a <details>, so its summary opens it without JavaScript. Clicking
// anywhere else, or pressing Escape, closes an open picker.
document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  document.querySelectorAll("details[data-month-picker][open]").forEach((picker) => {
    if (!picker.contains(target)) picker.open = false;
  });
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  document.querySelectorAll("details[data-month-picker][open]").forEach((picker) => {
    picker.open = false;
  });
});

// HTMX requests carry the CSRF token in a header; plain forms use their hidden input.
document.addEventListener("htmx:configRequest", (event) => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  if (meta instanceof HTMLMetaElement && meta.content) {
    event.detail.headers["X-CSRF-Token"] = meta.content;
  }
});

// Fields marked data-autosubmit submit their form on change; their submit button sits in
// <noscript>, for use without JavaScript. A month field submits only a complete month in its
// min-max range that differs from the shown one (keyboard entry changes it segment by segment);
// where the browser shows a text box instead (10.2026), a changed, non-empty value submits.
document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLSelectElement || target instanceof HTMLInputElement)) return;
  if (!target.hasAttribute("data-autosubmit")) return;
  if (target instanceof HTMLInputElement) {
    const value = target.value.trim();
    if (!value || value === target.defaultValue) return;
    if (target.type === "month") {
      if (!/^\d{4}-\d{2}$/.test(value)) return;
      if ((target.min && value < target.min) || (target.max && value > target.max)) return;
    }
  }
  target.form?.requestSubmit();
});

// Progressive disclosure: an element marked data-hidden-when="<select id>=<value>" is hidden
// while that select holds that value (the recurring form hides the repeat fields of a one-time
// item). Without JavaScript everything stays visible and the form explains what is ignored.
function applyHiddenWhen() {
  document.querySelectorAll("[data-hidden-when]").forEach((element) => {
    if (!(element instanceof HTMLElement)) return;
    const [id, value] = (element.dataset.hiddenWhen ?? "").split("=");
    const select = document.getElementById(id ?? "");
    if (select instanceof HTMLSelectElement) element.hidden = select.value === value;
  });
}
document.addEventListener("change", (event) => {
  if (event.target instanceof HTMLSelectElement) applyHiddenWhen();
});
document.addEventListener("DOMContentLoaded", applyHiddenWhen);

// Buttons marked data-copy-target copy the referenced input's value (invitation links).
document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const button = target.closest("[data-copy-target]");
  if (!(button instanceof HTMLElement)) return;
  const input = document.getElementById(button.dataset.copyTarget ?? "");
  if (!(input instanceof HTMLInputElement)) return;
  input.select();
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    // Without clipboard access the selected text can still be copied by hand.
  }
});

// Month page transaction tables (F2.2): entry and edit rows, focus after a swap, and the
// description autocomplete (an ARIA combobox). Everything hangs off data-* attributes, so it
// keeps working after HTMX swaps without any set-up; the forms also work without this script.

// The server marks the element to focus after a swap (or on a page with a refused form).
function focusRequested() {
  const element = document.querySelector("[data-autofocus]");
  if (!(element instanceof HTMLElement)) return;
  element.removeAttribute("data-autofocus");
  // Someone typing a planned value (F2.3) while a transaction save completes keeps typing there.
  const active = document.activeElement;
  if (active instanceof HTMLElement && active.matches("[data-plan-input]") && active !== element) {
    return;
  }
  element.focus();
  if (element instanceof HTMLInputElement) element.select();
}
document.addEventListener("htmx:load", (event) => {
  restoreEntryRows();
  focusRequested();
  // HTMX sets up swapped-in content a moment after the swap: a description typed into the new
  // row before that asks for its suggestions now.
  const active = document.activeElement;
  if (
    active instanceof HTMLInputElement &&
    active.matches("[data-suggest-input]") &&
    active.value &&
    event.target instanceof Element &&
    event.target.contains(active)
  ) {
    active.dispatchEvent(new Event("input", { bubbles: true }));
  }
});

// Entry rows survive swaps of their table. After Enter the row is reset at once (the request
// already holds its values), so typing goes on; whatever is typed before the response arrives
// is carried into the new section with the focus and the selection. The same holds when
// another write or an undo replaces the section. A refused save (4xx) shows the server's echo
// of the submitted values and messages instead.
const entryStash = new Map(); // section id -> {section, values, focus, selection}

function entryRowOf(section) {
  return section instanceof Element ? section.querySelector("[data-entry-row]") : null;
}

function snapshotEntryRow(section, row) {
  const values = {};
  row.querySelectorAll("[data-ledger-field]").forEach((field) => {
    if (field instanceof HTMLInputElement || field instanceof HTMLSelectElement) {
      values[field.name] = field.value;
    }
  });
  const active = document.activeElement;
  const focus = active instanceof HTMLElement && row.contains(active) ? active.id : null;
  const selection =
    focus && active instanceof HTMLInputElement
      ? [active.selectionStart, active.selectionEnd, active.selectionDirection]
      : null;
  return { section, values, focus, selection };
}

function stashSection(section) {
  if (!(section instanceof HTMLElement) || !section.matches("[data-ledger]")) return;
  const row = entryRowOf(section);
  if (row) entryStash.set(section.id, snapshotEntryRow(section, row));
}

function restoreEntryRows() {
  for (const [id, snapshot] of entryStash) {
    const section = document.getElementById(id);
    if (!section || section === snapshot.section) continue; // not swapped yet
    entryStash.delete(id);
    const row = entryRowOf(section);
    if (!row) continue;
    row.querySelectorAll("[data-ledger-field]").forEach((field) => {
      const value = snapshot.values[field.getAttribute("name") ?? ""];
      if (value !== undefined && "value" in field) field.value = value;
    });
    const field = snapshot.focus ? document.getElementById(snapshot.focus) : null;
    if (!(field instanceof HTMLElement) || !row.contains(field)) continue;
    section.querySelectorAll("[data-autofocus]").forEach((e) => e.removeAttribute("data-autofocus"));
    field.focus();
    if (field instanceof HTMLInputElement && snapshot.selection) {
      try {
        field.setSelectionRange(...snapshot.selection);
      } catch {
        // Not every input type has a selection.
      }
    }
  }
}

document.addEventListener("htmx:beforeSwap", (event) => {
  const section = event.detail?.target;
  if (!(section instanceof HTMLElement) || !section.matches("[data-ledger]")) return;
  const status = event.detail.xhr?.status ?? 0;
  if (status >= 200 && status < 300) stashSection(section);
  else entryStash.delete(section.id); // the server's echo wins
});
document.addEventListener("htmx:oobBeforeSwap", (event) => stashSection(event.detail?.target));
document.addEventListener("htmx:afterSwap", restoreEntryRows);

// The day the next entry keeps (owner decision): a full date in the viewed month becomes its
// day number, anything else stays as typed. The server applies the same rule.
function keptDay(text) {
  const value = text.trim();
  const month = document.querySelector("[data-month]")?.getAttribute("data-month") ?? "";
  let match = /^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})$/.exec(value);
  let parts = match ? [match[1], match[2], match[3]] : null;
  match = /^([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})$/.exec(value);
  if (match) parts = [match[3], match[2], match[1]];
  if (parts && `${parts[0]}-${parts[1].padStart(2, "0")}` === month) return String(Number(parts[2]));
  return value;
}

const submittedEntries = new Map(); // entry form id -> values sent (restored if sending fails)

// A future-dated entry is planned as a one-time recurring item; the one button says so. The
// server makes the same decision, so this only keeps the label in step while typing.
function entryDayISO(text) {
  const value = text.trim();
  const month = document.querySelector("[data-month]")?.getAttribute("data-month") ?? "";
  let match = /^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})$/.exec(value);
  let parts = match ? [match[1], match[2], match[3]] : null;
  if (!parts) {
    match = /^([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})$/.exec(value);
    if (match) parts = [match[3], match[2], match[1]];
  }
  if (!parts && /^[0-9]{1,2}$/.test(value) && /^[0-9]{4}-[0-9]{2}$/.test(month)) {
    parts = [month.slice(0, 4), month.slice(5), value];
  }
  if (!parts) return null;
  const [year, monthNumber, day] = parts.map(Number);
  const parsed = new Date(Date.UTC(year, monthNumber - 1, day));
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== monthNumber - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null;
  }
  return `${String(year).padStart(4, "0")}-${String(monthNumber).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function updateEntryLabel(form) {
  if (!(form instanceof HTMLFormElement) || !form.matches("[data-entry-form]")) return;
  const button = form.querySelector("[data-entry-add]");
  const row = document.getElementById(`${form.id}-row`);
  const day = row?.querySelector("[name='day']");
  if (!(button instanceof HTMLElement) || !(day instanceof HTMLInputElement)) return;
  const date = entryDayISO(day.value);
  const today = form.dataset.today ?? "";
  button.textContent = date !== null && today !== "" && date > today
    ? (button.dataset.labelPlan ?? "Plan")
    : (button.dataset.labelAdd ?? "Add");
}

function updateEntryLabels(root) {
  root.querySelectorAll("[data-entry-form]").forEach(updateEntryLabel);
}

document.addEventListener("input", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.name !== "day") return;
  if (input.form instanceof HTMLFormElement) updateEntryLabel(input.form);
});
document.addEventListener("DOMContentLoaded", () => updateEntryLabels(document));
document.addEventListener("htmx:load", () => updateEntryLabels(document));
document.addEventListener("htmx:afterSwap", () => updateEntryLabels(document));

document.addEventListener("htmx:beforeRequest", (event) => {
  const form = event.detail?.elt;
  if (!(form instanceof HTMLFormElement) || !form.matches("[data-entry-form]")) return;
  const row = document.getElementById(`${form.id}-row`);
  if (!row) return;
  submittedEntries.set(form.id, snapshotEntryRow(null, row).values);
  const day = row.querySelector("[name='day']");
  const kept = day instanceof HTMLInputElement ? keptDay(day.value) : "";
  clearEntryRow(row);
  if (day instanceof HTMLInputElement) {
    day.value = kept;
    day.select();
  }
});

document.addEventListener("htmx:sendError", (event) => {
  const form = event.detail?.elt;
  if (!(form instanceof HTMLFormElement) || !submittedEntries.has(form.id)) return;
  const values = submittedEntries.get(form.id);
  document.getElementById(`${form.id}-row`)?.querySelectorAll("[data-ledger-field]").forEach((field) => {
    const value = values[field.getAttribute("name") ?? ""];
    if (value !== undefined && "value" in field) field.value = value;
  });
});

function suggestionList(input) {
  return document.getElementById(input.getAttribute("aria-controls") ?? "");
}

function suggestionOptions(list) {
  return Array.from(list?.querySelectorAll("[role='option']") ?? []);
}

function closeSuggestions(input) {
  const list = suggestionList(input);
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  if (!list) return;
  list.hidden = true;
  suggestionOptions(list).forEach((option) => option.setAttribute("aria-selected", "false"));
}

function openSuggestions(input) {
  const list = suggestionList(input);
  if (!list || suggestionOptions(list).length === 0) {
    closeSuggestions(input);
    return;
  }
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

function moveSuggestion(input, step) {
  const options = suggestionOptions(suggestionList(input));
  if (options.length === 0) return;
  openSuggestions(input);
  const current = options.findIndex((option) => option.getAttribute("aria-selected") === "true");
  const next = current < 0 ? (step > 0 ? 0 : options.length - 1) : (current + step + options.length) % options.length;
  options.forEach((option, index) => option.setAttribute("aria-selected", String(index === next)));
  input.setAttribute("aria-activedescendant", options[next].id);
  // Scroll the list only: scrollIntoView would also scroll the table's wrapper.
  const option = options[next];
  const list = suggestionList(input);
  if (list && option.offsetTop < list.scrollTop) list.scrollTop = option.offsetTop;
  else if (list && option.offsetTop + option.offsetHeight > list.scrollTop + list.clientHeight) {
    list.scrollTop = option.offsetTop + option.offsetHeight - list.clientHeight;
  }
}

// Picking a suggestion sets the description and pre-selects its most used category.
function pickSuggestion(input, option) {
  input.value = option.dataset.description ?? option.textContent ?? "";
  input.dataset.picked = input.value;
  const select = input.form?.elements.namedItem("category_id");
  const categoryId = option.dataset.categoryId;
  if (
    select instanceof HTMLSelectElement &&
    categoryId &&
    Array.from(select.options).some((choice) => choice.value === categoryId)
  ) {
    select.value = categoryId;
  }
  closeSuggestions(input);
  input.focus();
}

document.addEventListener("htmx:afterSwap", (event) => {
  const list = event.detail?.target;
  if (!(list instanceof HTMLElement) || !list.matches("[data-suggestions]")) return;
  const input = document.querySelector(`[aria-controls="${list.id}"]`);
  if (!(input instanceof HTMLInputElement)) return;
  // Show the list only while the field has focus and was not just filled from it.
  if (document.activeElement === input && input.dataset.picked !== input.value) {
    input.removeAttribute("aria-activedescendant");
    openSuggestions(input);
  } else {
    closeSuggestions(input);
  }
});

document.addEventListener("focusout", (event) => {
  const input = event.target;
  if (input instanceof HTMLInputElement && input.matches("[data-suggest-input]")) {
    closeSuggestions(input);
  }
});

// mousedown would move the focus away (and close the list) before the click lands.
document.addEventListener("mousedown", (event) => {
  if (event.target instanceof Element && event.target.closest("[data-suggestions] [role='option']")) {
    event.preventDefault();
  }
});

document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) return;
  const option = event.target.closest("[data-suggestions] [role='option']");
  if (!(option instanceof HTMLElement)) return;
  const list = option.closest("[data-suggestions]");
  const input = document.querySelector(`[aria-controls="${list?.id}"]`);
  if (input instanceof HTMLInputElement) pickSuggestion(input, option);
});

// Esc in the entry row clears it (fields and messages); the row keeps the focus.
function clearEntryRow(row) {
  row.querySelectorAll("[data-ledger-field]").forEach((field) => {
    if (field instanceof HTMLSelectElement) {
      // With a category filter (F2.4) the entry row keeps that category.
      field.value = field.dataset.default ?? "";
      if (field.selectedIndex < 0) field.selectedIndex = 0;
    }
    else if (field instanceof HTMLInputElement) field.value = "";
    field.removeAttribute("aria-invalid");
    const hint = field.getAttribute("data-describedby");
    if (hint) field.setAttribute("aria-describedby", hint);
    else field.removeAttribute("aria-describedby");
    if (field instanceof HTMLInputElement && field.matches("[data-suggest-input]")) {
      delete field.dataset.picked;
      closeSuggestions(field);
    }
  });
  row.querySelectorAll("[data-field-error]").forEach((message) => message.remove());
  row.closest("[data-ledger]")?.querySelector("[data-ledger-error]")?.remove();
  row.querySelector("[name='day']")?.focus();
}

function handleSuggestionKey(input, event) {
  const expanded = input.getAttribute("aria-expanded") === "true";
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (suggestionOptions(suggestionList(input)).length === 0) return false;
    event.preventDefault();
    moveSuggestion(input, event.key === "ArrowDown" ? 1 : -1);
    return true;
  }
  if (!expanded) return false;
  if (event.key === "Escape") {
    event.preventDefault();
    closeSuggestions(input);
    return true;
  }
  if (event.key === "Enter") {
    const active = document.getElementById(input.getAttribute("aria-activedescendant") ?? "");
    if (!active) return false; // nothing chosen: Enter saves the row
    event.preventDefault();
    pickSuggestion(input, active);
    return true;
  }
  return false;
}

document.addEventListener("keydown", (event) => {
  const field = event.target;
  if (!(field instanceof HTMLElement)) return;
  const row = field.closest("[data-ledger-row-form]");
  if (!row) return;
  if (field instanceof HTMLInputElement && field.matches("[data-suggest-input]")) {
    if (handleSuggestionKey(field, event)) return;
  }
  if (event.key === "Enter" && field instanceof HTMLSelectElement) {
    // A select does not submit on Enter by itself; text fields do (implicit submission).
    event.preventDefault();
    field.form?.requestSubmit();
  } else if (event.key === "Escape") {
    event.preventDefault();
    if (row.hasAttribute("data-edit-row")) {
      const cancel = row.querySelector("[data-edit-cancel]");
      if (cancel instanceof HTMLElement) cancel.click();
    } else {
      clearEntryRow(row);
    }
  }
});

// Planned values in the month summary (F2.3). Each cell is a one-input form that saves on
// change (and on Enter without this script). Enter moves to the next cell, which saves the one
// left behind; on the last cell it saves in place. Esc restores the saved value. A transaction
// write replaces the whole summary out of band: a planned value being typed and its focus are
// carried into the new block, and it is saved when the field is left.
const restoredPlanInputs = new WeakSet();
let planStash = null;

function planInputs() {
  return Array.from(document.querySelectorAll("[data-plan-input]"));
}

document.addEventListener("keydown", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.matches("[data-plan-input]")) return;
  if (event.key === "Enter") {
    event.preventDefault();
    const inputs = planInputs();
    const next = inputs[inputs.indexOf(input) + 1];
    if (next instanceof HTMLInputElement) {
      next.focus();
      next.select();
    } else {
      input.form?.requestSubmit();
    }
  } else if (event.key === "Escape") {
    event.preventDefault();
    input.value = input.dataset.savedValue ?? "";
    input.removeAttribute("aria-invalid");
    input.setAttribute("aria-describedby", "plan-hint");
    input.form?.querySelector("[data-field-error]")?.remove();
    input.select();
  }
});

// A carried-over value that the browser does report as changed is saved by that change
// (hx-trigger), so the focusout below must not submit it a second time.
document.addEventListener(
  "change",
  (event) => {
    if (event.target instanceof HTMLInputElement) restoredPlanInputs.delete(event.target);
  },
  true,
);

document.addEventListener("focusout", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !restoredPlanInputs.has(input)) return;
  restoredPlanInputs.delete(input);
  // A value carried over a swap is not a "change" to the browser: save it explicitly.
  if (input.value !== (input.dataset.savedValue ?? "")) input.form?.requestSubmit();
});

document.addEventListener("htmx:oobBeforeSwap", (event) => {
  const block = event.detail?.target;
  if (!(block instanceof HTMLElement) || block.id !== "month-summary") return;
  const active = document.activeElement;
  if (!(active instanceof HTMLInputElement) || !block.contains(active)) return;
  if (!active.matches("[data-plan-input]")) return;
  planStash = {
    block,
    id: active.id,
    value: active.value,
    selection: [active.selectionStart, active.selectionEnd, active.selectionDirection],
  };
});

function restorePlanInput() {
  if (!planStash) return;
  const block = document.getElementById("month-summary");
  if (!block || block === planStash.block) return; // not swapped yet
  const { id, value, selection } = planStash;
  planStash = null;
  const input = document.getElementById(id);
  if (!(input instanceof HTMLInputElement) || !block.contains(input)) return;
  input.value = value;
  input.focus();
  try {
    input.setSelectionRange(...selection);
  } catch {
    // Not every input type has a selection.
  }
  if (value !== (input.dataset.savedValue ?? "")) restoredPlanInputs.add(input);
}
document.addEventListener("htmx:oobAfterSwap", restorePlanInput);
document.addEventListener("htmx:afterSwap", restorePlanInput);

// A cell's save can finish after a transaction write replaced the whole summary out of band:
// its target row is then detached, and HTMX fires htmx:beforeSwap on that detached row (the
// event never reaches the document). So the listener sits on the row itself, added when the
// request starts, and swaps into the row of the same id in the new block instead.
document.addEventListener("htmx:beforeRequest", (event) => {
  const form = event.detail?.elt;
  const row = event.detail?.target;
  if (!(form instanceof HTMLFormElement) || !form.matches("[data-plan-form]")) return;
  if (!(row instanceof HTMLElement) || !row.matches("[data-plan-row]")) return;
  row.addEventListener(
    "htmx:beforeSwap",
    (swap) => {
      if (row.isConnected) return;
      const live = document.getElementById(row.id);
      if (live) swap.detail.target = live;
    },
    { once: true },
  );
});

// The category filter (F2.4). Every request about the transaction tables carries the filter
// the page shows when it is sent (forms also have it as a hidden field, for use without this
// script), so answers stay filtered: row, edit, cancel, create, update, delete and undo.
document.addEventListener("htmx:configRequest", (event) => {
  const path = event.detail?.path ?? "";
  if (!/\/m\/[^/]+\/transactions(\/|$)/.test(path)) return; // not the suggestions
  const chosen = document.getElementById("ledger")?.getAttribute("data-ledger-filter") ?? "";
  if (chosen) event.detail.parameters.filter = chosen;
});

// After "Show all" swaps the tables (#ledger), the change is announced on the ledger's status
// line and the focus goes to the table heading.
document.addEventListener("htmx:afterSettle", (event) => {
  const trigger = event.detail?.requestConfig?.elt;
  if (!(trigger instanceof Element) || !trigger.matches("[data-ledger-show-all]")) return;
  const ledger = document.getElementById("ledger");
  const status = document.getElementById("ledger-status");
  if (status) status.textContent = ledger?.getAttribute("data-ledger-announce") ?? "";
  const heading = ledger?.querySelector("h2");
  if (heading instanceof HTMLElement) heading.focus();
});
