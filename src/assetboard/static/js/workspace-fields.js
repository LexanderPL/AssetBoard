// W-1: the workspace forms (first-run setup and "Create workspace") ask for the country before
// the base currency. Selecting a country fills the base currency from the currency the server
// put on that country's option (Babel's first supported current tender currency). A fresh form
// also seeds the time zone from the browser; a zone the user picked is never overwritten, and a
// country change only re-applies the browser's zone while the field is untouched. Without this
// script the server-rendered values are submitted unchanged.
(() => {
  function browserZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch {
      return "";
    }
  }

  function selectZone(select, zone) {
    if (!zone) return;
    if (!Array.from(select.options).some((option) => option.value === zone)) return;
    select.value = zone;
  }

  function enhance(form) {
    const country = form.querySelector("[data-country-select]");
    const currency = form.querySelector("#base_currency");
    const timezone = form.querySelector("#timezone");
    if (!(country instanceof HTMLSelectElement)) return;
    if (!(currency instanceof HTMLSelectElement)) return;
    if (!(timezone instanceof HTMLSelectElement)) return;

    const zone = browserZone();
    // The marker is rendered only for the first GET; a form shown again after an error already
    // holds the user's value, so it starts as "touched" and nothing replaces it.
    const seeded = form.dataset.seedTimezone === "1";
    let touched = !seeded;
    if (seeded) selectZone(timezone, zone);

    timezone.addEventListener("change", () => {
      touched = true;
    });

    country.addEventListener("change", () => {
      const code = country.selectedOptions[0]?.dataset.currency;
      if (code && Array.from(currency.options).some((option) => option.value === code)) {
        currency.value = code;
      }
      if (!touched) selectZone(timezone, zone);
    });
  }

  document.querySelectorAll("form[data-workspace-fields]").forEach(enhance);
})();
