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

  const dismiss = target.closest("[data-toast-dismiss]");
  if (dismiss) dismiss.closest("[role='status']")?.remove();

  const cancel = target.closest("[data-confirm-cancel]");
  if (cancel) cancel.closest("dialog")?.close("cancel");

  const accept = target.closest("[data-confirm-accept]");
  if (accept) accept.closest("dialog")?.close("confirm");
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) return;
  if (form.dataset.confirmed === "true") {
    delete form.dataset.confirmed;
    return;
  }
  event.preventDefault();
  const dialog = document.getElementById("confirm-dialog");
  if (!(dialog instanceof HTMLDialogElement)) return;
  const message = dialog.querySelector("[data-confirm-message]");
  if (message) message.textContent = form.dataset.confirm;
  dialog.addEventListener(
    "close",
    () => {
      if (dialog.returnValue === "confirm") {
        form.dataset.confirmed = "true";
        form.requestSubmit();
      }
    },
    { once: true },
  );
  dialog.showModal();
});
