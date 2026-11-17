// F9.12: the Profile sidebar arrows reorder the rows in place; only the Save button submits.
// The HTML keeps the arrows as submit buttons so a browser without JavaScript can still move
// a page (the server applies the move and saves, as before). When this script runs it turns
// them into plain buttons and moves the <li> rows, so the hidden item fields and the visible
// checkboxes travel with their row and the form's DOM order is what Save persists.
(() => {
  // With JavaScript the arrows stay local, so the no-JavaScript fallback note does not apply.
  document.querySelector("[data-sidebar-nojs-note]")?.remove();
  const form = document.querySelector("[data-sidebar-form]");
  const list = form?.querySelector("[data-sidebar-list]");
  if (!(form instanceof HTMLFormElement) || !(list instanceof HTMLElement)) return;

  const rows = () => Array.from(list.children).filter((row) => row.matches("[data-sidebar-item]"));

  function update() {
    const items = rows();
    items.forEach((item, index) => {
      const up = item.querySelector('[data-sidebar-move$=":up"]');
      const down = item.querySelector('[data-sidebar-move$=":down"]');
      if (up instanceof HTMLButtonElement) {
        up.disabled = index === 0 || items[index - 1].dataset.sidebarItem === "home";
      }
      if (down instanceof HTMLButtonElement) {
        down.disabled = index === items.length - 1;
      }
    });
  }

  form.querySelectorAll("[data-sidebar-move]").forEach((button) => {
    if (button instanceof HTMLButtonElement) button.type = "button";
  });

  form.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest("[data-sidebar-move]");
    if (!(button instanceof HTMLButtonElement) || !form.contains(button)) return;
    event.preventDefault();

    const row = button.closest("[data-sidebar-item]");
    if (!(row instanceof HTMLElement)) return;
    const items = rows();
    const index = items.indexOf(row);
    const up = button.dataset.sidebarMove?.endsWith(":up") ?? false;
    const targetIndex = up ? index - 1 : index + 1;
    if (index < 0 || targetIndex < 0 || targetIndex >= items.length) return;

    if (up) {
      list.insertBefore(row, items[targetIndex]);
    } else {
      list.insertBefore(items[targetIndex], row);
    }
    update();
    button.focus();
  });

  update();
})();
