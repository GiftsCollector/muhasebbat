(function () {
  function normalize(text) {
    return (text || "").toString().trim().toLowerCase();
  }

  function optionLabel(option) {
    return (option.textContent || "").trim();
  }

  function unwrap(wrap) {
    const select = wrap.querySelector("select");
    const menu = wrap._searchableMenu;
    if (menu && menu.parentNode) menu.remove();
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
    menu.className = "searchable-select-menu searchable-select-menu-portal";
    menu.setAttribute("role", "listbox");

    wrap.appendChild(input);
    wrap._searchableMenu = menu;

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

    function optionGroupLabel(option) {
      const group = option && option.parentElement;
      if (group && group.tagName === "OPTGROUP") return (group.label || "").trim();
      return "";
    }

    function filteredOptions(query) {
      const q = normalize(query);
      return Array.from(select.options).filter(function (option) {
        if (!option.value) return false;
        if (option.disabled || option.hidden) return false;
        const group = optionGroupLabel(option);
        return !q
          || normalize(optionLabel(option)).indexOf(q) !== -1
          || normalize(option.value).indexOf(q) !== -1
          || normalize(group).indexOf(q) !== -1;
      });
    }

    function positionMenu() {
      const wrapRect = wrap.getBoundingClientRect();
      const spaceBelow = window.innerHeight - wrapRect.bottom;
      const spaceAbove = wrapRect.top;
      const dropUp = spaceBelow < 140 && spaceAbove > spaceBelow;
      const maxH = Math.min(420, Math.max(160, (dropUp ? spaceAbove : spaceBelow) - 12));
      const width = Math.max(wrapRect.width, 140);
      menu.style.position = "fixed";
      menu.style.zIndex = "4000";
      menu.style.width = width + "px";
      menu.style.maxHeight = maxH + "px";
      menu.style.left = wrapRect.left + "px";
      menu.style.right = "auto";
      if (dropUp) {
        menu.style.top = "auto";
        menu.style.bottom = (window.innerHeight - wrapRect.top + 4) + "px";
        wrap.classList.add("is-drop-up");
      } else {
        menu.style.top = (wrapRect.bottom + 4) + "px";
        menu.style.bottom = "auto";
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
      let lastGroup = "";
      items.forEach(function (option, index) {
        const group = optionGroupLabel(option);
        if (group && group !== lastGroup) {
          lastGroup = group;
          const header = document.createElement("div");
          header.className = "searchable-select-group";
          header.textContent = group;
          menu.appendChild(header);
        }
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
          choose(option.value, optionLabel(option));
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
      menu.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
      if (!menu.isConnected) document.body.appendChild(menu);
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
      menu.classList.remove("is-open");
      input.setAttribute("aria-expanded", "false");
      menu.innerHTML = "";
      if (menu.parentNode) menu.remove();
      if (restore && !allowCustom) syncInputFromSelect();
    }

    function choose(value, label) {
      if (value && allowCustom) ensureOption(value, label || value);
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
      const options = Array.from(select.options).filter(function (option) { return option.value; });
      function namePart(option) {
        const label = optionLabel(option);
        const parts = label.split("—");
        return parts.length > 1 ? parts[parts.length - 1].trim() : label;
      }
      function codePart(option) {
        const label = optionLabel(option);
        const parts = label.split("—");
        return parts.length > 1 ? parts[0].trim() : "";
      }
      const exact = options.find(function (option) {
        const label = optionLabel(option);
        return label === typed || option.value === typed || namePart(option) === typed || codePart(option) === typed;
      });
      const contains = exact ? [] : options.filter(function (option) {
        return typed.length >= 3 && optionLabel(option).indexOf(typed) !== -1;
      });
      const match = exact || (contains.length === 1 ? contains[0] : null);
      choose(match ? match.value : typed, match ? optionLabel(match) : typed);
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
        if (open && rows[activeIndex]) choose(rows[activeIndex].dataset.value, rows[activeIndex].textContent);
        else commitTyped();
      } else if (event.key === "Escape") {
        closeMenu(!allowCustom);
        input.blur();
      }
    });
    input.addEventListener("blur", function () {
      window.setTimeout(function () {
        if (wrap.contains(document.activeElement) || menu.contains(document.activeElement)) return;
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
    if (event.target.closest(".searchable-select") || event.target.closest(".searchable-select-menu")) return;
    closeAll(null);
  });

  document.addEventListener("submit", function (event) {
    if (!event.target || !event.target.querySelectorAll) return;
    event.target.querySelectorAll(".searchable-select").forEach(function (wrap) {
      wrap._searchableCommit && wrap._searchableCommit();
    });
  }, true);

  document.addEventListener("scroll", function (event) {
    if (event.target && event.target.closest && event.target.closest(".searchable-select-menu")) return;
    document.querySelectorAll(".searchable-select.is-open").forEach(function (wrap) {
      const menu = wrap._searchableMenu;
      if (!menu) return;
      const rect = wrap.getBoundingClientRect();
      if (rect.width === 0 || rect.bottom < 8 || rect.top > window.innerHeight - 8) {
        wrap._searchableClose && wrap._searchableClose(true);
        return;
      }
      const wrapRect = wrap.getBoundingClientRect();
      const spaceBelow = window.innerHeight - wrapRect.bottom;
      const spaceAbove = wrapRect.top;
      const dropUp = spaceBelow < 140 && spaceAbove > spaceBelow;
      const maxH = Math.min(420, Math.max(160, (dropUp ? spaceAbove : spaceBelow) - 12));
      menu.style.width = Math.max(wrapRect.width, 140) + "px";
      menu.style.maxHeight = maxH + "px";
      menu.style.left = wrapRect.left + "px";
      if (dropUp) {
        menu.style.top = "auto";
        menu.style.bottom = (window.innerHeight - wrapRect.top + 4) + "px";
      } else {
        menu.style.top = (wrapRect.bottom + 4) + "px";
        menu.style.bottom = "auto";
      }
    });
  }, true);

  window.addEventListener("resize", function () {
    document.querySelectorAll(".searchable-select.is-open").forEach(function (wrap) {
      wrap._searchableClose && wrap._searchableClose(true);
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    initSearchableSelects(document);
  });
})();
