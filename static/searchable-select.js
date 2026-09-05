(function () {
  function normalize(text) {
    return (text || "").toString().trim().toLowerCase();
  }

  function unwrap(wrap) {
    const select = wrap.querySelector("select");
    if (!select || !wrap.parentNode) return;
    select.removeAttribute("data-search-ready");
    wrap.parentNode.insertBefore(select, wrap);
    wrap.remove();
  }

  function bindSelect(select) {
    if (!select || select.dataset.searchReady === "1") return;
    if (select.closest(".searchable-select")) return;

    const wrap = document.createElement("div");
    wrap.className = "searchable-select";
    select.parentNode.insertBefore(wrap, select);

    const filter = document.createElement("input");
    filter.type = "search";
    filter.className = "form-control form-control-sm searchable-select-filter";
    filter.placeholder = "ابحث بالكلمة...";
    filter.autocomplete = "off";
    filter.setAttribute("aria-label", "بحث في قائمة الحسابات");

    wrap.appendChild(filter);
    wrap.appendChild(select);
    select.dataset.searchReady = "1";

    const options = Array.from(select.options).map(function (option) {
      return {
        value: option.value,
        text: option.textContent,
      };
    });

    function applyFilter() {
      const query = normalize(filter.value);
      const current = select.value;
      select.innerHTML = "";
      options.forEach(function (item) {
        const keepEmpty = !item.value;
        const keepSelected = item.value === current;
        const match = !query || normalize(item.text).indexOf(query) !== -1;
        if (!keepEmpty && !keepSelected && !match) return;
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = item.text;
        select.appendChild(option);
      });
      if (Array.from(select.options).some(function (option) { return option.value === current; })) {
        select.value = current;
      }
    }

    filter.addEventListener("input", applyFilter);
    filter.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        filter.value = "";
        applyFilter();
        select.focus();
      }
    });
  }

  function initSearchableSelects(root) {
    const scope = root || document;
    const nodes = scope.querySelectorAll
      ? scope.querySelectorAll("select.js-searchable-select")
      : [];
    nodes.forEach(bindSelect);
  }

  window.initSearchableSelects = initSearchableSelects;
  window.resetSearchableSelectClone = function (root) {
    if (!root) return;
    root.querySelectorAll(".searchable-select").forEach(unwrap);
    initSearchableSelects(root);
  };

  document.addEventListener("DOMContentLoaded", function () {
    initSearchableSelects(document);
  });
})();
