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

const toastTimers = new WeakSet();

function startToastTimer(toast) {
  if (!(toast instanceof Element) || !toast.matches("[data-toast]") || toastTimers.has(toast)) {
    return;
  }
  toastTimers.add(toast);
  window.setTimeout(() => toast.remove(), 5000);
}

document.querySelectorAll("[data-toast]").forEach(startToastTimer);
document.addEventListener("htmx:oobAfterSwap", (event) => {
  event.detail?.target?.querySelectorAll("[data-toast]").forEach(startToastTimer);
});

document.addEventListener("htmx:confirm", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) return;
  event.preventDefault();
  const dialog = document.getElementById("confirm-dialog");
  if (!(dialog instanceof HTMLDialogElement)) return;
  const message = dialog.querySelector("[data-confirm-message]");
  if (message) message.textContent = form.dataset.confirm;
  const issueRequest = event.detail.issueRequest;
  dialog.addEventListener(
    "close",
    () => {
      if (dialog.returnValue === "confirm") {
        issueRequest(true);
      }
    },
    { once: true },
  );
  dialog.returnValue = "";
  dialog.showModal();
});
