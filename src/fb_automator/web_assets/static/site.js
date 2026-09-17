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

document.querySelectorAll("[data-gallery]").forEach((gallery) => {
  const images = [...gallery.querySelectorAll("[data-gallery-image]")];
  const placeholder = gallery.querySelector("[data-gallery-placeholder]");
  const counter = gallery.querySelector("[data-gallery-count]");
  let index = Number(gallery.dataset.galleryIndex || -1);

  const show = (nextIndex) => {
    if (!images.length) return;
    index = (nextIndex + images.length) % images.length;
    images.forEach((image, imageIndex) => {
      image.classList.toggle("is-hidden", imageIndex !== index);
    });
    placeholder?.classList.add("is-hidden");
    gallery.classList.add("has-photo");
    gallery.querySelector(".gallery-reveal")?.remove();
    if (counter) counter.textContent = `${index + 1} / ${images.length}`;
  };

  gallery.querySelectorAll("[data-gallery-next]").forEach((button) => {
    button.addEventListener("click", () => show(index + 1));
  });
  gallery.querySelectorAll("[data-gallery-prev]").forEach((button) => {
    button.addEventListener("click", () => show(index - 1));
  });
});

document.querySelectorAll("[data-summary]").forEach((summary) => {
  const button = summary.parentElement?.querySelector("[data-summary-toggle]");
  if (!button || summary.scrollHeight <= summary.clientHeight + 1) return;
  button.hidden = false;
  button.addEventListener("click", () => {
    const expanded = summary.classList.toggle("is-expanded");
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = expanded ? "Réduire" : "Lire la suite";
  });
});
