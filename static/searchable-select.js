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
    input.placeholder = emptyOption ? optionLabel(emptyOption) : "اختر حسابا";

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

    function filteredOptions(query) {
      const q = normalize(query);
      return Array.from(select.options).filter(function (option) {
        if (!option.value) return false;
        return !q || normalize(optionLabel(option)).indexOf(q) !== -1;
      });
    }

    function positionMenu() {
      const rect = input.getBoundingClientRect();
      const width = Math.max(rect.width, 260);
      menu.style.position = "fixed";
      menu.style.zIndex = "2400";
      menu.style.minWidth = width + "px";
      menu.style.width = width + "px";
      menu.style.left = (document.documentElement.dir === "rtl"
        ? (rect.right - width)
        : rect.left) + "px";
      const spaceBelow = window.innerHeight - rect.bottom;
      const maxH = Math.min(280, Math.max(140, spaceBelow - 8));
      menu.style.maxHeight = maxH + "px";
      if (spaceBelow < 160 && rect.top > spaceBelow) {
        menu.style.top = "auto";
        menu.style.bottom = (window.innerHeight - rect.top + 4) + "px";
      } else {
        menu.style.top = (rect.bottom + 4) + "px";
        menu.style.bottom = "auto";
      }
    }

    function renderMenu() {
      const query = input.value === selectedText() ? "" : input.value;
      const items = filteredOptions(query);
      menu.innerHTML = "";
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "searchable-select-empty";
        empty.textContent = "لا توجد حسابات مطابقة";
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
    }

    function highlight() {
      const rows = menu.querySelectorAll(".searchable-select-option");
      rows.forEach(function (row, index) {
        row.classList.toggle("is-active", index === activeIndex);
        if (index === activeIndex) row.scrollIntoView({ block: "nearest" });
      });
    }

    function openMenu() {
      if (open) {
        positionMenu();
        return;
      }
      closeAll(wrap);
      open = true;
      wrap.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
      activeIndex = 0;
      renderMenu();
      positionMenu();
    }

    function closeMenu(restore) {
      if (!open) {
        if (restore) syncInputFromSelect();
        return;
      }
      open = false;
      wrap.classList.remove("is-open");
      input.setAttribute("aria-expanded", "false");
      menu.innerHTML = "";
      if (restore) syncInputFromSelect();
    }

    function choose(value) {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      syncInputFromSelect();
      closeMenu(false);
    }

    wrap._searchableClose = closeMenu;

    select.addEventListener("invalid", function () {
      input.focus();
      openMenu();
    });

    input.addEventListener("focus", function () {
      openMenu();
      input.select();
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
        if (open && rows[activeIndex]) {
          event.preventDefault();
          choose(rows[activeIndex].dataset.value);
        }
      } else if (event.key === "Escape") {
        closeMenu(true);
        input.blur();
      }
    });
    input.addEventListener("blur", function () {
      window.setTimeout(function () {
        if (!wrap.contains(document.activeElement)) closeMenu(true);
      }, 120);
    });

    window.addEventListener("scroll", function () {
      if (open) positionMenu();
    }, true);
    window.addEventListener("resize", function () {
      if (open) positionMenu();
    });

    syncInputFromSelect();
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

  document.addEventListener("mousedown", function (event) {
    if (event.target.closest(".searchable-select")) return;
    closeAll(null);
  });

  document.addEventListener("DOMContentLoaded", function () {
    initSearchableSelects(document);
  });
})();
