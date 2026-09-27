(function () {
  function normalize(text) {
    return (text || "").toString().trim().toLowerCase();
  }

  function optionLabel(option) {
    return (option.textContent || "").trim();
  }

  function unwrap(wrap) {
    const select = wrap.querySelector("select");
    if (!select || !wrap.parentNode) return;
    select.classList.remove("searchable-select-native");
    select.removeAttribute("tabindex");
    select.removeAttribute("data-search-ready");
    wrap.parentNode.insertBefore(select, wrap);
    wrap.remove();
  }

  function closeAll(exceptWrap) {
    document.querySelectorAll(".searchable-select.is-open").forEach(function (wrap) {
      if (wrap !== exceptWrap) wrap._searchableClose && wrap._searchableClose(true);
    });
  }

  function bindSelect(select) {
    if (!select || select.dataset.searchReady === "1") return;
    if (select.closest(".searchable-select")) return;
    if (select.multiple) return;

    const allowCustom = select.getAttribute("data-allow-custom") === "1";
    const wrap = document.createElement("div");
    wrap.className = "searchable-select";
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.classList.add("searchable-select-native");
    select.tabIndex = -1;
    select.dataset.searchReady = "1";

    const input = document.createElement("input");
    input.type = "text";
    input.className = "form-select searchable-select-input";
    input.autocomplete = "off";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    const emptyOption = Array.from(select.options).find(function (option) { return !option.value; });
    input.placeholder = select.getAttribute("data-placeholder") || (emptyOption ? optionLabel(emptyOption) : "ابحث واختر");

    const menu = document.createElement("div");
    menu.className = "searchable-select-menu";
    menu.setAttribute("role", "listbox");

    wrap.appendChild(input);
    wrap.appendChild(menu);

    let activeIndex = -1;
    let open = false;

    function selectedText() {
      const option = select.options[select.selectedIndex];
      if (!option || !option.value) return "";
      return optionLabel(option);
    }

    function syncInputFromSelect() {
      input.value = selectedText();
    }

    function ensureOption(value, label) {
      const exists = Array.from(select.options).some(function (option) {
        return option.value === value;
      });
      if (exists) return;
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label || value;
      select.appendChild(option);
    }

    function filteredOptions(query) {
      const q = normalize(query);
      return Array.from(select.options).filter(function (option) {
        if (!option.value) return false;
        if (option.disabled || option.hidden) return false;
        return !q || normalize(optionLabel(option)).indexOf(q) !== -1 || normalize(option.value).indexOf(q) !== -1;
      });
    }

    function positionMenu() {
      const wrapRect = wrap.getBoundingClientRect();
      const spaceBelow = window.innerHeight - wrapRect.bottom;
      const spaceAbove = wrapRect.top;
      const maxH = Math.min(240, Math.max(120, (spaceBelow > 140 ? spaceBelow : spaceAbove) - 16));
      menu.style.maxHeight = maxH + "px";
      if (spaceBelow < 140 && spaceAbove > spaceBelow) {
        wrap.classList.add("is-drop-up");
      } else {
        wrap.classList.remove("is-drop-up");
      }
    }

    function renderMenu() {
      const query = input.value === selectedText() ? "" : input.value.trim();
      const items = filteredOptions(query);
      menu.innerHTML = "";
      const exact = items.some(function (option) {
        return optionLabel(option) === query || option.value === query;
      });

      if (!items.length && !(allowCustom && query)) {
        const empty = document.createElement("div");
        empty.className = "searchable-select-empty";
        empty.textContent = "لا توجد نتائج مطابقة";
        menu.appendChild(empty);
        activeIndex = -1;
        return;
      }

      if (activeIndex >= items.length) activeIndex = 0;
      items.forEach(function (option, index) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "searchable-select-option";
        row.setAttribute("role", "option");
        row.dataset.value = option.value;
        row.textContent = optionLabel(option);
        if (option.value === select.value) row.classList.add("is-selected");
        if (index === activeIndex) row.classList.add("is-active");
        row.addEventListener("mousedown", function (event) {
          event.preventDefault();
          choose(option.value);
        });
        menu.appendChild(row);
      });

      if (allowCustom && query && !exact) {
        const addRow = document.createElement("button");
        addRow.type = "button";
        addRow.className = "searchable-select-option searchable-select-add";
        addRow.dataset.value = query;
        addRow.textContent = "إضافة «" + query + "»";
        addRow.addEventListener("mousedown", function (event) {
          event.preventDefault();
          choose(query);
        });
        menu.appendChild(addRow);
        if (activeIndex < 0) activeIndex = items.length;
      }
    }

    function highlight() {
      const rows = menu.querySelectorAll(".searchable-select-option");
      rows.forEach(function (row, index) {
        row.classList.toggle("is-active", index === activeIndex);
        if (index === activeIndex) row.scrollIntoView({ block: "nearest" });
      });
    }

    function openMenu() {
      closeAll(wrap);
      open = true;
      wrap.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
      if (activeIndex < 0) activeIndex = 0;
      renderMenu();
      positionMenu();
    }

    function closeMenu(restore) {
      if (!open) {
        if (restore && !allowCustom) syncInputFromSelect();
        return;
      }
      open = false;
      wrap.classList.remove("is-open");
      wrap.classList.remove("is-drop-up");
      input.setAttribute("aria-expanded", "false");
      menu.innerHTML = "";
      if (restore && !allowCustom) syncInputFromSelect();
    }

    function choose(value) {
      if (value && allowCustom) ensureOption(value, value);
      select.value = value || "";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      syncInputFromSelect();
      closeMenu(false);
    }

    function commitTyped() {
      const typed = input.value.trim();
      if (!allowCustom) {
        closeMenu(true);
        return;
      }
      if (!typed) {
        choose("");
        return;
      }
      const match = Array.from(select.options).find(function (option) {
        return option.value && (optionLabel(option) === typed || option.value === typed);
      });
      choose(match ? match.value : typed);
    }

    wrap._searchableClose = closeMenu;
    wrap._searchableCommit = allowCustom ? commitTyped : function () { closeMenu(true); };

    select.addEventListener("invalid", function () {
      input.focus();
      openMenu();
    });

    input.addEventListener("focus", function () {
      openMenu();
    });
    input.addEventListener("click", function () {
      openMenu();
    });
    input.addEventListener("input", function () {
      activeIndex = 0;
      openMenu();
      renderMenu();
      positionMenu();
    });
    input.addEventListener("keydown", function (event) {
      const rows = menu.querySelectorAll(".searchable-select-option");
      if (event.key === "ArrowDown") {
        event.preventDefault();
        openMenu();
        activeIndex = Math.min(activeIndex + 1, rows.length - 1);
        highlight();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        openMenu();
        activeIndex = Math.max(activeIndex - 1, 0);
        highlight();
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (open && rows[activeIndex]) choose(rows[activeIndex].dataset.value);
        else commitTyped();
      } else if (event.key === "Escape") {
        closeMenu(!allowCustom);
        input.blur();
      }
    });
    input.addEventListener("blur", function () {
      window.setTimeout(function () {
        if (wrap.contains(document.activeElement)) return;
        if (allowCustom) commitTyped();
        else closeMenu(true);
      }, 120);
    });

    syncInputFromSelect();
  }

  function initSearchableSelects(root) {
    const scope = root || document;
    if (!scope.querySelectorAll) return;
    scope.querySelectorAll("select.js-searchable-select").forEach(bindSelect);
  }

  window.initSearchableSelects = initSearchableSelects;
  window.resetSearchableSelectClone = function (root) {
    if (!root) return;
    root.querySelectorAll(".searchable-select").forEach(unwrap);
    initSearchableSelects(root);
  };

  document.addEventListener("mousedown", function (event) {
    if (event.target.closest(".searchable-select")) return;
    closeAll(null);
  });

  document.addEventListener("submit", function (event) {
    if (!event.target || !event.target.querySelectorAll) return;
    event.target.querySelectorAll(".searchable-select").forEach(function (wrap) {
      wrap._searchableCommit && wrap._searchableCommit();
    });
  }, true);

  document.addEventListener("scroll", function () {
    document.querySelectorAll(".searchable-select.is-open").forEach(function (wrap) {
      wrap._searchableClose && wrap._searchableClose(true);
    });
  }, true);

  document.addEventListener("DOMContentLoaded", function () {
    initSearchableSelects(document);
  });
})();
