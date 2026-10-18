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
  const seconds = Number(toast.dataset.toastSeconds) || 10;
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

// HTMX requests carry the CSRF token in a header; plain forms use their hidden input.
document.addEventListener("htmx:configRequest", (event) => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  if (meta instanceof HTMLMetaElement && meta.content) {
    event.detail.headers["X-CSRF-Token"] = meta.content;
  }
});

// Selects marked data-autosubmit submit their form on change (the button stays for no-JS use).
document.addEventListener("change", (event) => {
  const target = event.target;
  if (target instanceof HTMLSelectElement && target.hasAttribute("data-autosubmit")) {
    target.form?.requestSubmit();
  }
});

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
  element.focus();
  if (element instanceof HTMLInputElement) element.select();
}
document.addEventListener("htmx:load", focusRequested);

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
  options[next].scrollIntoView({ block: "nearest" });
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
    if (field instanceof HTMLSelectElement) field.selectedIndex = 0;
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
