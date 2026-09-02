(() => {
  function updateHeaders(form, field, direction) {
    form.querySelectorAll(".sort-button").forEach((button) => {
      const header = button.closest("th");
      const active = button.dataset.sort === field;
      const nextDirection = active && direction === "asc" ? "descending" : "ascending";

      header.setAttribute("aria-sort", active ? `${direction}ending` : "none");
      button.setAttribute(
        "aria-label",
        `Sort by ${button.dataset.label} ${nextDirection}`,
      );
    });
  }

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest(".sort-button");
    if (!button) return;

    const form = button.closest("form");
    const sortInput = form?.querySelector('input[name="sort"]');
    const directionInput = form?.querySelector('input[name="direction"]');
    if (!form || !sortInput || !directionInput) return;

    const field = button.dataset.sort;
    const direction =
      sortInput.value === field && directionInput.value === "asc" ? "desc" : "asc";
    sortInput.value = field;
    directionInput.value = direction;
    updateHeaders(form, field, direction);
    form.requestSubmit();
  });
})();
