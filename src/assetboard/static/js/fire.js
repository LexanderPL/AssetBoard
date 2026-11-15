// F8.11: the FIRE editor's unified adjustments, suggestion copy and unsaved-changes guard.
// The client only clones a template, toggles removal markers and copies server-formatted strings;
// every amount is parsed and validated by the server, and the projection stays server-side.
(function () {
  "use strict";

  const form = document.querySelector("[data-fire-form]");
  const status = document.querySelector("[data-fire-dirty-status]");
  let dirty = form instanceof HTMLElement && form.dataset.fireDirty === "1";

  function setDirty(value) {
    dirty = value;
    if (form instanceof HTMLElement) form.dataset.fireDirty = value ? "1" : "0";
    if (status instanceof HTMLElement) status.hidden = !value;
  }

  function notifyChange() {
    if (form instanceof HTMLElement) {
      form.dispatchEvent(new CustomEvent("fire:changed", { bubbles: true }));
    }
  }

  function announce(message) {
    const live = document.querySelector("[data-adjustment-status]");
    if (live instanceof HTMLElement) live.textContent = message;
  }

  function setRemovedState(checkbox) {
    const row = checkbox.closest("[data-adjustment-row], [data-fire-archived-adjustment]");
    if (!(row instanceof HTMLElement)) return;
    const note = row.querySelector("[data-adjustment-removed-note]");
    if (note instanceof HTMLElement) note.hidden = !checkbox.checked;
    row.setAttribute("data-removed", checkbox.checked ? "1" : "0");
  }

  function addAdjustment(button) {
    const rows = document.querySelector("[data-adjustment-rows]");
    const template = document.querySelector("[data-adjustment-template]");
    if (!(rows instanceof HTMLElement) || !(template instanceof HTMLTemplateElement)) return;
    const blank = rows.querySelector("[data-new-adjustment-row]");
    const index = Number(rows.dataset.nextIndex || "1");
    rows.dataset.nextIndex = String(index + 1);
    const holder = document.createElement("ol");
    holder.innerHTML = template.innerHTML.replaceAll("__index__", String(index));
    const row = holder.firstElementChild;
    if (!(row instanceof HTMLElement)) return;
    if (blank instanceof HTMLElement) rows.insertBefore(row, blank);
    else rows.appendChild(row);
    const legend = row.querySelector("[data-adjustment-legend]");
    if (legend instanceof HTMLElement) {
      const count = rows.querySelectorAll(
        "[data-adjustment-row]:not([data-new-adjustment-row])"
      ).length;
      legend.textContent = button.dataset.adjustmentLegend.replace(
        "__NUM__",
        String(count)
      );
    }
    const first = row.querySelector("[data-adjustment-year]");
    if (first instanceof HTMLElement) first.focus();
    const empty = document.querySelector("[data-fire-adjustments-empty]");
    if (empty instanceof HTMLElement) empty.hidden = true;
    announce(button.dataset.addedMessage || "");
    notifyChange();
    row.dispatchEvent(new Event("input", { bubbles: true }));
  }

  if (form instanceof HTMLElement) {
    form.addEventListener("input", () => {
      setDirty(true);
    });
    form.addEventListener("change", (event) => {
      const target = event.target;
      if (
        target instanceof HTMLInputElement &&
        (target.matches("[data-adjustment-remove]") ||
          target.matches("[data-adjustment-archived-remove]"))
      ) {
        setRemovedState(target);
        notifyChange();
      }
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

    const add = target.closest("[data-add-adjustment]");
    if (add instanceof HTMLElement) {
      addAdjustment(add);
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
      field = form.querySelector("[data-fire-adjustments]");
    }
    if (field instanceof HTMLElement) field.setAttribute("aria-invalid", "true");
  }

  for (const eventName of ["htmx:afterSwap", "htmx:oobAfterSwap"]) {
    document.addEventListener(eventName, syncPreviewError);
  }

  // F8.14: the withdrawal rate applies only to the withdrawal-rate rule. The server renders the
  // field visible so a no-JavaScript user can still use it; this script hides and disables it for
  // the other rule. The server ignores a valid posted rate for a non-SWR rule.
  function syncSwrFields() {
    document.querySelectorAll("[data-fire-swr-field]").forEach((wrapper) => {
      if (!(wrapper instanceof HTMLElement)) return;
      const owner = wrapper.closest("form");
      const rule = owner?.querySelector('select[name="rule"]');
      const input = wrapper.querySelector('input[name="swr"]');
      if (!(rule instanceof HTMLSelectElement) || !(input instanceof HTMLInputElement)) return;
      const active = rule.value === "swr";
      wrapper.hidden = !active;
      // Tailwind's `.grid` would override the user-agent `[hidden]` rule, so toggle the utility
      // class as well; the attribute keeps the semantic state for tests and assistive technology.
      wrapper.classList.toggle("hidden", !active);
      input.disabled = !active;
    });
  }

  document.querySelectorAll("[data-fire-form], [data-fire-wizard-form]").forEach((element) => {
    element.addEventListener("change", (event) => {
      if (event.target instanceof HTMLSelectElement && event.target.name === "rule") {
        syncSwrFields();
      }
    });
  });

  syncSwrFields();
})();
