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
    if (
      row.hasAttribute("data-adjustment-row") &&
      !row.hasAttribute("data-new-adjustment-row") &&
      row.dataset.adjustmentId === ""
    ) {
      const next = row.nextElementSibling;
      if (next instanceof HTMLElement && next.hasAttribute("data-adjustment-error-row")) {
        next.remove();
      }
      row.remove();
      const table = document.querySelector("[data-adjustment-rows]");
      if (table instanceof HTMLElement) {
        const current = table.querySelectorAll(
          "[data-adjustment-row]:not([data-new-adjustment-row])"
        ).length;
        const empty = document.querySelector("[data-fire-adjustments-empty]");
        if (empty instanceof HTMLElement) empty.hidden = current > 0;
      }
      const section = document.querySelector("[data-fire-adjustments]");
      announce(
        section instanceof HTMLElement ? section.dataset.adjustmentRemovedMessage || "" : ""
      );
      return;
    }
    const checked = checkbox.checked;
    const note = row.querySelector("[data-adjustment-removed-note]");
    if (note instanceof HTMLElement) {
      note.hidden = !checked;
      note.classList.toggle("hidden", !checked);
    }
    const text = row.querySelector("[data-adjustment-remove-text]");
    if (text instanceof HTMLElement) {
      text.textContent = checked
        ? checkbox.dataset.undoLabel || ""
        : checkbox.dataset.removeLabel || "";
    }
    checkbox.setAttribute(
      "aria-label",
      checked ? checkbox.dataset.undoAria || "" : checkbox.dataset.removeAria || ""
    );
    row.classList.toggle("opacity-60", checked);
    row.setAttribute("data-removed", checked ? "1" : "0");
  }

  function addAdjustment(button) {
    const rows = document.querySelector("[data-adjustment-rows]");
    const template = document.querySelector("[data-adjustment-template]");
    if (!(rows instanceof HTMLElement) || !(template instanceof HTMLTemplateElement)) return;
    const blank = rows.querySelector("[data-new-adjustment-row]");
    const index = Number(rows.dataset.nextIndex || "1");
    rows.dataset.nextIndex = String(index + 1);
    // A tbody holder gives the HTML parser the table-row context a <tr> template needs.
    const holder = document.createElement("tbody");
    holder.innerHTML = template.innerHTML.replaceAll("__index__", String(index));
    const row = holder.querySelector("[data-adjustment-row]");
    if (!(row instanceof HTMLElement)) return;
    const fragment = document.createDocumentFragment();
    for (const child of Array.from(holder.children)) fragment.appendChild(child);
    if (blank instanceof HTMLElement) rows.insertBefore(fragment, blank);
    else rows.appendChild(fragment);
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

  // F8.17: a native <details name> group is exclusive, but older browsers without `name`
  // support keep every section open. Closing the others on toggle makes the behaviour the same
  // everywhere and is idempotent when the browser already closed them.
  function setupExclusiveAccordion() {
    document.querySelectorAll("[data-fire-accordion]").forEach((group) => {
      const sections = [...group.querySelectorAll("details[data-fire-accordion-section]")];
      sections.forEach((section) => {
        section.addEventListener("toggle", () => {
          if (!section.open) return;
          sections.forEach((other) => {
            if (other !== section && other.open) other.open = false;
          });
        });
      });
    });
  }

  function openSectionFor(element) {
    const section = element.closest("details[data-fire-accordion-section]");
    if (section instanceof HTMLDetailsElement) section.open = true;
  }

  // A preview error arrives with the results fragment, not a full page. Mark the named input
  // without moving focus, open the section that owns it, and clear the marker when a later
  // preview succeeds.
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
    if (!field && name === "loans") {
      field = form.querySelector("[data-fire-loans]");
    }
    if (field instanceof HTMLElement) {
      field.setAttribute("aria-invalid", "true");
      openSectionFor(field);
    }
  }

  for (const eventName of ["htmx:afterSwap", "htmx:oobAfterSwap"]) {
    document.addEventListener(eventName, syncPreviewError);
  }

  // /fire/edit#amounts and the F8.16 review link open their section directly.
  function openSectionForHash() {
    const id = decodeURIComponent(window.location.hash.replace(/^#/, ""));
    if (!id) return;
    const target = document.getElementById(id);
    if (!(target instanceof HTMLElement)) return;
    openSectionFor(target);
    requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  }

  window.addEventListener("hashchange", openSectionForHash);

  // The grid is server-rendered; the button only re-fetches the saved plan. Without JavaScript
  // it stays hidden and nothing is lost.
  function revealSensitivityRefresh() {
    document.querySelectorAll("[data-fire-sensitivity-refresh]").forEach((button) => {
      if (button instanceof HTMLElement) button.classList.remove("hidden");
    });
  }

  setupExclusiveAccordion();
  revealSensitivityRefresh();
  openSectionForHash();

  // F8.15: the withdrawal rate applies only to the withdrawal-rate rule. The server renders the
  // field visible and enabled so a no-JavaScript user can still use it; this script disables it
  // and dims the field for the other rule. The server ignores a valid posted rate for a non-SWR
  // rule.
  function syncSwrFields() {
    document.querySelectorAll("[data-fire-swr-field]").forEach((wrapper) => {
      if (!(wrapper instanceof HTMLElement)) return;
      const owner = wrapper.closest("form");
      const rule = owner?.querySelector('select[name="rule"]');
      const input = wrapper.querySelector('input[name="swr"]');
      if (!(rule instanceof HTMLSelectElement) || !(input instanceof HTMLInputElement)) return;
      const active = rule.value === "swr";
      input.disabled = !active;
      wrapper.classList.toggle("opacity-60", !active);
      wrapper.dataset.fireSwrInactive = active ? "0" : "1";
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
