const toggle = document.querySelector("[data-filter-toggle]");
const advanced = document.querySelector("#advanced-filters");

if (toggle && advanced) {
  const update = (expanded) => {
    toggle.setAttribute("aria-expanded", String(expanded));
    advanced.classList.toggle("is-open", expanded);
  };
  update(toggle.getAttribute("aria-expanded") === "true");
  toggle.addEventListener("click", () => {
    update(toggle.getAttribute("aria-expanded") !== "true");
  });
}

document.querySelectorAll("[data-auto-submit]").forEach((field) => {
  field.addEventListener("change", () => field.form?.requestSubmit());
});
