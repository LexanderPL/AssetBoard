// F9.1: the Customise Home arrows reorder the rows in place; only Save submits. Without this
// script the arrows stay submit buttons: each one rerenders the draft on the server, so the
// plain-form fallback keeps working. Moving a row also refreshes the hidden order field, because
// that field is what Save reads.
(() => {
  const form = document.querySelector("[data-customise-form]");
  const list = form?.querySelector("[data-customise-list]");
  if (!(form instanceof HTMLFormElement) || !(list instanceof HTMLElement)) return;
  // Re-running the script must not stack click handlers on the same form.
  if (form.dataset.customiseEnhanced === "1") return;

  // With JavaScript the arrows stay local, so the no-JavaScript fallback note does not apply.
  form.querySelector("[data-customise-nojs-note]")?.remove();

  const rows = () => Array.from(list.children).filter((row) => row.matches("[data-customise-widget]"));

  function update() {
    const items = rows();
    const order = form.querySelector("[data-customise-order]");
    items.forEach((item, index) => {
      const up = item.querySelector('[data-customise-move$=":up"]');
      const down = item.querySelector('[data-customise-move$=":down"]');
      if (up instanceof HTMLButtonElement) up.disabled = index === 0;
      if (down instanceof HTMLButtonElement) down.disabled = index === items.length - 1;
    });
    if (order instanceof HTMLInputElement) {
      order.value = items.map((item) => item.getAttribute("data-customise-widget") || "").join(",");
    }
  }

  function keepFocus(row, button) {
    // Moving into the first or last slot disables the arrow just used. A disabled button cannot
    // keep focus, so continue on the other arrow of the same row; if the row has only one other
    // destination, keep the focus on the row itself rather than dropping it to <body>.
    if (!button.disabled) {
      button.focus();
      return;
    }
    const alternative = Array.from(row.querySelectorAll("[data-customise-move]")).find(
      (candidate) =>
        candidate instanceof HTMLButtonElement && candidate !== button && !candidate.disabled
    );
    if (alternative instanceof HTMLButtonElement) {
      alternative.focus();
      return;
    }
    const checkbox = row.querySelector('input[type="checkbox"]');
    if (checkbox instanceof HTMLInputElement) {
      checkbox.focus();
      return;
    }
    row.tabIndex = -1;
    row.focus();
  }

  form.querySelectorAll("[data-customise-move]").forEach((button) => {
    if (button instanceof HTMLButtonElement) button.type = "button";
  });

  form.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest("[data-customise-move]");
    if (!(button instanceof HTMLButtonElement) || !form.contains(button)) return;
    event.preventDefault();

    const row = button.closest("[data-customise-widget]");
    if (!(row instanceof HTMLElement)) return;
    const items = rows();
    const index = items.indexOf(row);
    const up = button.dataset.customiseMove?.endsWith(":up") ?? false;
    const targetIndex = up ? index - 1 : index + 1;
    if (index < 0 || targetIndex < 0 || targetIndex >= items.length) return;

    if (up) {
      list.insertBefore(row, items[targetIndex]);
    } else {
      list.insertBefore(items[targetIndex], row);
    }
    update();
    keepFocus(row, button);
  });

  update();
  form.dataset.customiseEnhanced = "1";
})();
