/* إجمالي تلقائي تحت أعمدة الفلوس + فلتر لكل عمود في سجلات البرنامج.
   عرض بس: مفيش أي حاجة بتتبعت للسيرفر ولا بتغيّر رقم متسجل. */
(function () {
  'use strict';

  var SKIP_HEAD = /^(|#|م|✕|إجراءات|الإجراءات|تفاصيل|مرفق|تحكم|خيارات|دورة)$/;
  var NO_SUM_HEAD = /(كود|رقم|تاريخ|وقت|الكمية|كمية|سابق|منفذ|نسبة|%|سعر|عدد|ساعات|المسافة|موبايل|تليفون|هاتف|البون|الوحدة|الشهر|كشوف|نقلات|اللوحة|الترخيص|هامش|تكعيب|المستلم|المسحوب|وارد|منصرف|رصيد)|(^|\s)(يوم|أيام|الأيام)(\s|$)/;
  var DATE_HEAD = /(تاريخ|البداية|النهاية|الترخيص|وقت التسجيل|^من$|^إلى$)/;
  var TOTAL_ROW_START = /^(الإجمالي|الإجمالى|إجمالي|الاجمالي|اجمالي|المجموع)/;
  var ENTRY_FIELDS = 'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), select, textarea';
  var ARABIC_DIGITS = '٠١٢٣٤٥٦٧٨٩';

  function norm(text) {
    return (text || '').replace(/\s+/g, ' ').trim();
  }

  function headText(th) {
    var clone = th.cloneNode(true);
    clone.querySelectorAll('.reg-filter-btn, input, button').forEach(function (node) { node.remove(); });
    return norm(clone.textContent);
  }

  /* المبالغ المخفية بالعين السحرية (js-secret-real) ما تتقريش أبداً: لا تتجمع ولا تظهر في قائمة الفلتر */
  var CELL_NOISE = 'button, form, .btn, select, input, script, .js-secret-real';

  function cellText(cell) {
    if (!cell.querySelector(CELL_NOISE)) return norm(cell.textContent);
    var clone = cell.cloneNode(true);
    clone.querySelectorAll(CELL_NOISE).forEach(function (node) { node.remove(); });
    return norm(clone.textContent);
  }

  /* نفس شكل الأرقام في البرنامج: 1.234.567 أو 1.234,50 */
  function parseAmount(text) {
    var t = (text || '').replace(/[\s\u00a0]/g, '').replace(/[٠-٩]/g, function (d) { return String(ARABIC_DIGITS.indexOf(d)); });
    t = t.replace(/(ج\.?م|جنيه|EGP)$/i, '');
    if (!t || t === '-' || t === '—') return null;
    var negative = false;
    if (/^\(.*\)$/.test(t)) { negative = true; t = t.slice(1, -1); }
    if (t.charAt(0) === '-' || t.charAt(0) === '−') { negative = !negative; t = t.slice(1); }
    if (!/^[\d.,]+$/.test(t)) return NaN;
    if (/^0\d/.test(t)) return NaN;
    var value;
    if (t.indexOf(',') !== -1) {
      if (/^\d{1,3}(\.\d{3})*,\d+$/.test(t)) value = parseFloat(t.replace(/\./g, '').replace(',', '.'));
      else if (/^\d{1,3}(,\d{3})+(\.\d+)?$/.test(t)) value = parseFloat(t.replace(/,/g, ''));
      else return NaN;
    } else if (/^\d{1,3}(\.\d{3})+$/.test(t)) {
      value = parseFloat(t.replace(/\./g, ''));
    } else if (/^\d+(\.\d+)?$/.test(t)) {
      value = parseFloat(t);
    } else {
      return NaN;
    }
    return negative ? -value : value;
  }

  function formatCents(cents) {
    var negative = cents < 0;
    var abs = Math.abs(cents);
    var whole = Math.floor(abs / 100);
    var frac = abs % 100;
    var text = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    if (frac) text += ',' + (frac < 10 ? '0' : '') + frac;
    return (negative ? '-' : '') + text;
  }

  function amountMatcher(expr) {
    var text = norm(expr);
    if (!text) return null;
    var m = text.match(/^(>=|<=|>|<)\s*(.+)$/);
    if (m) {
      var limit = parseAmount(m[2]);
      if (limit === null || isNaN(limit)) return function () { return false; };
      return function (n) {
        if (n === null || isNaN(n)) return false;
        if (m[1] === '>') return n > limit;
        if (m[1] === '<') return n < limit;
        if (m[1] === '>=') return n >= limit;
        return n <= limit;
      };
    }
    m = text.match(/^([^-–]+?)\s*(?:-|–|إلى|الى)\s*([^-–]+)$/);
    if (m) {
      var low = parseAmount(m[1]);
      var high = parseAmount(m[2]);
      if (low !== null && high !== null && !isNaN(low) && !isNaN(high)) {
        if (low > high) { var swap = low; low = high; high = swap; }
        return function (n) { return n !== null && !isNaN(n) && n >= low - 0.005 && n <= high + 0.005; };
      }
    }
    var exact = parseAmount(text);
    if (exact === null || isNaN(exact)) return function () { return false; };
    return function (n) { return n !== null && !isNaN(n) && Math.abs(n - exact) < 0.005; };
  }

  function isoDate(text) {
    var m = (text || '').match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
    if (!m) return '';
    return m[1] + '-' + (m[2].length < 2 ? '0' : '') + m[2] + '-' + (m[3].length < 2 ? '0' : '') + m[3];
  }

  function rowShown(row) {
    return !row.hidden && row.style.display !== 'none';
  }

  function Register(table) {
    this.table = table;
    this.headRow = table.tHead.rows[0];
    this.heads = Array.prototype.slice.call(this.headRow.cells);
    this.filters = {};
    this.footRow = null;
    this.status = null;
    this.scanRows();
    this.detectColumns();
    this.buildFooter();
    this.buildFilterButtons();
    this.refresh();
    this.watch();
  }

  Register.prototype.scanRows = function () {
    var width = this.heads.length;
    var rows = [];
    var ownTotal = false;
    Array.prototype.forEach.call(this.table.tBodies, function (body) {
      Array.prototype.forEach.call(body.rows, function (row) {
        if (row.cells.length && TOTAL_ROW_START.test(cellText(row.cells[0]))) { ownTotal = true; return; }
        if (row.cells.length !== width) return;
        if (Array.prototype.some.call(row.cells, function (c) { return c.colSpan > 1; })) return;
        var texts = Array.prototype.map.call(row.cells, cellText);
        rows.push({ el: row, texts: texts, nums: texts.map(parseAmount) });
      });
    });
    this.rows = rows;
    this.ownTotal = ownTotal;
  };

  Register.prototype.detectColumns = function () {
    var self = this;
    this.columns = this.heads.map(function (th, index) {
      var label = headText(th);
      var forced = (th.getAttribute('data-sum') || '').toLowerCase();
      var skip = SKIP_HEAD.test(label) || th.querySelector('input[type="checkbox"]') !== null || th.getAttribute('data-filter') === 'off';
      var filled = 0;
      var numeric = true;
      var dates = 0;
      self.rows.forEach(function (row) {
        var text = row.texts[index];
        if (!text || text === '-' || text === '—') return;
        filled += 1;
        if (isNaN(row.nums[index]) || row.nums[index] === null) numeric = false;
        if (isoDate(text)) dates += 1;
      });
      numeric = numeric && filled > 0;
      var isDate = DATE_HEAD.test(label) || (filled > 0 && dates === filled);
      var sum = forced === 'on' || (forced !== 'off' && numeric && !isDate && !skip && !NO_SUM_HEAD.test(label));
      return { index: index, label: label, skip: skip, numeric: numeric && !isDate, isDate: isDate, sum: sum };
    });
    this.sumColumns = this.columns.filter(function (c) { return c.sum; });
  };

  Register.prototype.buildFooter = function () {
    if (!this.sumColumns.length || !this.rows.length || this.table.getAttribute('data-totals') === 'off') return;
    var table = this.table;
    var existing = table.tFoot;
    var row = document.createElement('tr');
    row.className = 'reg-total-row';
    var labelIndex = -1;
    this.columns.forEach(function (col) {
      if (labelIndex < 0 && !col.sum && !col.skip) labelIndex = col.index;
    });
    if (labelIndex < 0) {
      this.columns.forEach(function (col) {
        if (labelIndex < 0 && !col.sum) labelIndex = col.index;
      });
    }
    this.labelIndex = labelIndex;
    this.footCells = this.heads.map(function (th) {
      var td = document.createElement('td');
      if (th.classList.contains('sticky-actions')) td.classList.add('sticky-actions');
      if (th.classList.contains('sticky-actions-start')) td.classList.add('sticky-actions-start');
      row.appendChild(td);
      return td;
    });
    this.onlyWhenFiltered = Boolean(existing) || this.ownTotal;
    if (existing) {
      existing.insertBefore(row, existing.firstChild);
    } else {
      table.createTFoot().appendChild(row);
    }
    this.footRow = row;
  };

  Register.prototype.buildFilterButtons = function () {
    var self = this;
    if (!this.rows.length) return;
    this.columns.forEach(function (col) {
      if (col.skip) return;
      var th = self.heads[col.index];
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'reg-filter-btn no-print';
      btn.title = 'فلتر العمود';
      btn.setAttribute('aria-label', 'فلتر ' + col.label);
      btn.innerHTML = '<i class="bi bi-funnel"></i>';
      btn.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        Panel.open(self, col, btn);
      });
      th.appendChild(btn);
    });
  };

  Register.prototype.activeFilters = function () {
    var self = this;
    return Object.keys(this.filters).filter(function (key) {
      var f = self.filters[key];
      return f && (f.values || f.term || f.amount || f.from || f.to);
    });
  };

  Register.prototype.matches = function (row, active, amountTests) {
    var self = this;
    return active.every(function (key) {
      var f = self.filters[key];
      var index = Number(key);
      var text = row.texts[index] || '';
      if (f.values && !f.values.has(text)) return false;
      if (f.term && text.toLocaleLowerCase('ar').indexOf(f.term) === -1) return false;
      if (amountTests[key] && !amountTests[key](row.nums[index])) return false;
      if (f.from || f.to) {
        var day = isoDate(text);
        if (!day) return false;
        if (f.from && day < f.from) return false;
        if (f.to && day > f.to) return false;
      }
      return true;
    });
  };

  Register.prototype.applyFilters = function () {
    var self = this;
    var active = this.activeFilters();
    var amountTests = {};
    active.forEach(function (key) {
      if (self.filters[key].amount) amountTests[key] = amountMatcher(self.filters[key].amount);
    });
    this.rows.forEach(function (row) {
      var show = !active.length || self.matches(row, active, amountTests);
      if (row.el.hidden === !show) return;
      row.el.hidden = !show;
      if (!show) {
        row.el.querySelectorAll('input[type="checkbox"]:checked').forEach(function (box) {
          box.checked = false;
          box.dispatchEvent(new Event('change', { bubbles: true }));
        });
      }
    });
    this.heads.forEach(function (th, index) {
      th.classList.toggle('reg-filter-active', active.indexOf(String(index)) !== -1);
    });
    this.refresh();
  };

  Register.prototype.refresh = function () {
    var shownRows = this.rows.filter(function (row) { return rowShown(row.el); });
    var filtered = shownRows.length !== this.rows.length;
    this.renderStatus(shownRows.length);
    if (!this.footRow) return;
    var self = this;
    this.footRow.hidden = this.onlyWhenFiltered && !filtered;
    this.sumColumns.forEach(function (col) {
      var cents = 0;
      shownRows.forEach(function (row) {
        var n = row.nums[col.index];
        if (n !== null && !isNaN(n)) cents += Math.round(n * 100);
      });
      self.footCells[col.index].textContent = formatCents(cents);
      self.footCells[col.index].classList.toggle('text-danger', cents < 0);
    });
    if (this.labelIndex >= 0) {
      this.footCells[this.labelIndex].textContent = filtered
        ? 'إجمالي المعروض (' + shownRows.length + ' من ' + this.rows.length + ')'
        : 'الإجمالي';
    }
  };

  Register.prototype.renderStatus = function (shown) {
    var active = this.activeFilters().length > 0;
    if (!active) {
      if (this.status) this.status.hidden = true;
      return;
    }
    if (!this.status) {
      var self = this;
      this.status = document.createElement('div');
      this.status.className = 'reg-filter-status no-print';
      this.status.innerHTML = '<i class="bi bi-funnel-fill"></i><span class="reg-filter-count"></span>' +
        '<button type="button" class="btn btn-link btn-sm p-0 reg-filter-clear">مسح كل الفلاتر</button>';
      this.status.querySelector('.reg-filter-clear').addEventListener('click', function () {
        self.filters = {};
        Panel.close();
        self.applyFilters();
      });
      var anchor = this.table.closest('.table-responsive') || this.table;
      anchor.parentNode.insertBefore(this.status, anchor);
    }
    this.status.hidden = false;
    this.status.querySelector('.reg-filter-count').textContent = 'المعروض ' + shown + ' من ' + this.rows.length;
  };

  Register.prototype.watch = function () {
    var self = this;
    var pending = false;
    var rebuilt = false;
    var observer = new MutationObserver(function (mutations) {
      if (mutations.some(function (m) { return m.type === 'childList'; })) rebuilt = true;
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(function () {
        pending = false;
        if (rebuilt) {
          rebuilt = false;
          self.scanRows();
          observeRows();
          self.applyFilters();
        } else {
          self.refresh();
        }
      });
    });
    function observeRows() {
      self.rows.forEach(function (row) {
        observer.observe(row.el, { attributes: true, attributeFilter: ['style', 'hidden'] });
      });
    }
    Array.prototype.forEach.call(this.table.tBodies, function (body) {
      observer.observe(body, { childList: true });
    });
    observeRows();
  };

  var Panel = {
    el: null,
    register: null,
    column: null,
    close: function () {
      if (Panel.el) Panel.el.remove();
      Panel.el = null;
      Panel.register = null;
      Panel.column = null;
    },
    open: function (register, col, button) {
      if (Panel.el && Panel.register === register && Panel.column === col) {
        Panel.close();
        return;
      }
      Panel.close();
      var state = register.filters[col.index] || {};
      var counts = new Map();
      register.rows.forEach(function (row) {
        var text = row.texts[col.index] || '';
        counts.set(text, (counts.get(text) || 0) + 1);
      });
      var values = Array.from(counts.keys()).sort(function (a, b) {
        if (col.numeric) return (parseAmount(a) || 0) - (parseAmount(b) || 0);
        return a.localeCompare(b, 'ar');
      }).slice(0, 500);

      var el = document.createElement('div');
      el.className = 'reg-filter-panel';
      el.setAttribute('role', 'dialog');
      var html = '<div class="reg-filter-head"><strong></strong>' +
        '<button type="button" class="btn-close btn-sm" aria-label="إغلاق"></button></div>';
      if (col.isDate) {
        html += '<div class="row g-1 mb-1"><div class="col-6"><label class="form-label mb-0 small">من</label>' +
          '<input type="date" class="form-control form-control-sm" data-f="from"></div>' +
          '<div class="col-6"><label class="form-label mb-0 small">إلى</label>' +
          '<input type="date" class="form-control form-control-sm" data-f="to"></div></div>';
      }
      if (col.numeric) {
        html += '<label class="form-label mb-0 small">المبلغ</label>' +
          '<input type="text" class="form-control form-control-sm mb-1" data-f="amount" inputmode="decimal" ' +
          'placeholder="100000 أو 1000-5000 أو >5000">';
      }
      html += '<input type="search" class="form-control form-control-sm" data-f="term" placeholder="بحث في العمود">' +
        '<div class="reg-filter-list"></div>' +
        '<div class="d-flex gap-2 justify-content-between">' +
        '<button type="button" class="btn btn-sm btn-outline-secondary" data-act="clear">مسح فلتر العمود</button>' +
        '<button type="button" class="btn btn-sm btn-primary" data-act="close">تم</button></div>';
      el.innerHTML = html;
      el.querySelector('.reg-filter-head strong').textContent = col.label;

      var list = el.querySelector('.reg-filter-list');
      var allLabel = document.createElement('label');
      allLabel.className = 'reg-filter-all';
      allLabel.innerHTML = '<input type="checkbox" class="form-check-input"><span>الكل</span>';
      list.appendChild(allLabel);
      var allBox = allLabel.querySelector('input');
      var boxes = values.map(function (value) {
        var label = document.createElement('label');
        label.innerHTML = '<input type="checkbox" class="form-check-input"><span class="reg-value"></span><span class="reg-count"></span>';
        label.querySelector('.reg-value').textContent = value || '(فاضي)';
        label.querySelector('.reg-count').textContent = counts.get(value);
        var box = label.querySelector('input');
        box.checked = !state.values || state.values.has(value);
        box.value = value;
        list.appendChild(label);
        return box;
      });
      allBox.checked = boxes.every(function (box) { return box.checked; });

      function save() {
        var next = register.filters[col.index] || {};
        var checked = boxes.filter(function (box) { return box.checked; });
        next.values = checked.length === boxes.length ? null : new Set(checked.map(function (box) { return box.value; }));
        el.querySelectorAll('[data-f]').forEach(function (input) {
          var key = input.getAttribute('data-f');
          var value = (input.value || '').trim();
          next[key] = key === 'term' ? value.toLocaleLowerCase('ar') : value;
        });
        register.filters[col.index] = next;
        allBox.checked = checked.length === boxes.length;
        allBox.indeterminate = checked.length > 0 && checked.length < boxes.length;
        register.applyFilters();
      }

      el.querySelectorAll('[data-f]').forEach(function (input) {
        var key = input.getAttribute('data-f');
        input.value = key === 'term' ? (state.termRaw || '') : (state[key] || '');
        input.addEventListener('input', function () {
          if (key === 'term') {
            var needle = input.value.trim().toLocaleLowerCase('ar');
            boxes.forEach(function (box) {
              box.parentNode.hidden = needle && box.value.toLocaleLowerCase('ar').indexOf(needle) === -1;
            });
            (register.filters[col.index] = register.filters[col.index] || {}).termRaw = input.value;
          }
          save();
        });
      });
      allBox.addEventListener('change', function () {
        boxes.forEach(function (box) {
          if (!box.parentNode.hidden) box.checked = allBox.checked;
        });
        save();
      });
      boxes.forEach(function (box) { box.addEventListener('change', save); });
      el.querySelector('.btn-close').addEventListener('click', Panel.close);
      el.querySelector('[data-act="close"]').addEventListener('click', Panel.close);
      el.querySelector('[data-act="clear"]').addEventListener('click', function () {
        delete register.filters[col.index];
        Panel.close();
        register.applyFilters();
      });
      el.addEventListener('click', function (event) { event.stopPropagation(); });
      el.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') Panel.close();
        if (event.key === 'Enter') event.preventDefault();
      });

      document.body.appendChild(el);
      var rect = button.getBoundingClientRect();
      var width = el.offsetWidth;
      var left = rect.right + window.scrollX - width;
      left = Math.max(window.scrollX + 8, Math.min(left, window.scrollX + document.documentElement.clientWidth - width - 8));
      el.style.top = (rect.bottom + window.scrollY + 6) + 'px';
      el.style.left = left + 'px';
      Panel.el = el;
      Panel.register = register;
      Panel.column = col;
      var first = el.querySelector('[data-f]');
      if (first) first.focus();
    }
  };

  document.addEventListener('click', function () { Panel.close(); });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') Panel.close();
  });

  function eligible(table) {
    if (table.getAttribute('data-register') === 'off') return false;
    if (table.closest('.modal')) return false;
    if (!table.tHead || table.tHead.rows.length !== 1 || !table.tBodies.length) return false;
    if (Array.prototype.some.call(table.tHead.rows[0].cells, function (th) { return th.colSpan > 1; })) return false;
    /* خانة إدخال جوه فورم صغير في خلية واحدة (رفع مرفق مثلاً) زرار للصف، مش جدول إدخال */
    return !Array.prototype.some.call(table.tBodies, function (body) {
      return Array.prototype.some.call(body.querySelectorAll(ENTRY_FIELDS), function (field) {
        var form = field.closest('form');
        return !form || form.contains(table);
      });
    });
  }

  document.querySelectorAll('table.table').forEach(function (table) {
    if (table.__register || !eligible(table)) return;
    try {
      table.__register = new Register(table);
    } catch (err) {
      if (window.console) console.warn('register tools skipped a table', err);
    }
  });
})();
