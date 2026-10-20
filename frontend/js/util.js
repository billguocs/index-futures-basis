/* ==========================================================================
   frontend/js/util.js · 通用工具：格式化 / 模板 / Toast
   全局命名空间：window.App.util
   格式化约定：
     - 持仓、成交量等整数 → 千分位
     - 百分比（后端已是百分数，如 -12.3 表示 -12.3%）→ 2 位小数 + "%"
     - 比率（如 cum_return=0.2981 之类的小数）→ frac*100 后 2 位
     - null / undefined / NaN / '' → "—"
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var DASH = '—';

  function isNum(v) {
    return typeof v === 'number' && isFinite(v);
  }

  function toNum(v) {
    if (isNum(v)) return v;
    if (typeof v === 'string' && v.trim() !== '') {
      var n = Number(v);
      return isFinite(n) ? n : null;
    }
    return null;
  }

  function group(digits) {
    return String(digits).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function fixedGroup(n, d) {
    var neg = n < 0;
    var s = Math.abs(n).toFixed(d);
    var parts = s.split('.');
    var out = group(parts[0]) + (parts.length > 1 && d > 0 ? '.' + parts[1] : '');
    return (neg ? '-' : '') + out;
  }

  /* ----------------------------- 数值格式化 ----------------------------- */

  var fmt = {
    /** 千分位整数：1234567 → 1,234,567 */
    num: function (v) {
      var n = toNum(v);
      if (n === null) return DASH;
      return fixedGroup(Math.round(n), 0);
    },
    /** 带符号千分位整数：-1234 → -1,234；1234 → +1,234 */
    signedNum: function (v) {
      var n = toNum(v);
      if (n === null) return DASH;
      var s = fixedGroup(Math.round(Math.abs(n)), 0);
      return (n > 0 ? '+' : n < 0 ? '-' : '') + s;
    },
    /** 千分位小数：1234.5 → 1,234.50 */
    dec: function (v, d) {
      var n = toNum(v);
      if (n === null) return DASH;
      return fixedGroup(n, d == null ? 2 : d);
    },
    /** 百分数（入参已是 %）：-12.34 → -12.34% */
    pct: function (v, d) {
      var n = toNum(v);
      if (n === null) return DASH;
      return n.toFixed(d == null ? 2 : d) + '%';
    },
    /** 带符号百分数：-0.54 → -0.54%；0.54 → +0.54% */
    signedPct: function (v, d) {
      var n = toNum(v);
      if (n === null) return DASH;
      var s = Math.abs(n).toFixed(d == null ? 2 : d) + '%';
      return (n > 0 ? '+' : n < 0 ? '-' : '') + s;
    },
    /** 小数比率 → 百分数：0.2981 → 29.81% */
    frac: function (v, d) {
      var n = toNum(v);
      if (n === null) return DASH;
      return (n * 100).toFixed(d == null ? 2 : d) + '%';
    },
    /** 带符号比率：-0.1966 → -19.66% */
    signedFrac: function (v, d) {
      var n = toNum(v);
      if (n === null) return DASH;
      var s = Math.abs(n * 100).toFixed(d == null ? 2 : d) + '%';
      return (n > 0 ? '+' : n < 0 ? '-' : '') + s;
    },
    /** 相关系数等比率（不带 %） */
    ratio: function (v, d) {
      var n = toNum(v);
      if (n === null) return DASH;
      return n.toFixed(d == null ? 3 : d);
    },
    signedRatio: function (v, d) {
      var n = toNum(v);
      if (n === null) return DASH;
      var s = Math.abs(n).toFixed(d == null ? 3 : d);
      return (n > 0 ? '+' : n < 0 ? '-' : '') + s;
    },
    /** p 值：0.063 → 0.063；<0.001 → <0.001 */
    pvalue: function (v) {
      var n = toNum(v);
      if (n === null) return DASH;
      if (n < 0.001) return '<0.001';
      return n.toFixed(3);
    },
    /** 轴标签用的紧凑金额：-1234567 → -123.5万 */
    wan: function (v) {
      var n = toNum(v);
      if (n === null) return DASH;
      var a = Math.abs(n);
      var sign = n < 0 ? '-' : '';
      if (a >= 1e8) return sign + (a / 1e8).toFixed(2) + '亿';
      if (a >= 1e4) return sign + (a / 1e4).toFixed(a >= 1e6 ? 0 : 1) + '万';
      return sign + a.toFixed(0);
    },
    /** 区间 [lo, hi] 文案 */
    range: function (lo, hi, d) {
      var l = toNum(lo), h = toNum(hi);
      if (l === null && h === null) return DASH;
      if (l === null) return '≤ ' + fmt.ratio(h, d);
      if (h === null) return '> ' + fmt.ratio(l, d);
      return fmt.ratio(l, d) + ' ~ ' + fmt.ratio(h, d);
    }
  };

  /* ----------------------------- 颜色 / 语义 ----------------------------- */

  function signClass(v) {
    var n = toNum(v);
    if (n === null || n === 0) return 'flat';
    return n > 0 ? 'up' : 'down';
  }

  function signHex(v) {
    var n = toNum(v);
    if (n === null || n === 0) return '#8b949e';
    return n > 0 ? '#f6465d' : '#0ecb81';
  }

  /** impact 文案 → chip 类名 */
  function impactClass(text) {
    var s = String(text || '');
    if (s.indexOf('空') >= 0) return 'chip-down';
    if (s.indexOf('多') >= 0) return 'chip-up';
    return 'chip-flat';
  }

  /* ----------------------------- DOM / 模板 ----------------------------- */

  function q(sel, root) {
    return (root || document).querySelector(sel);
  }

  function qq(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function setHTML(el, html) {
    if (typeof el === 'string') el = q(el);
    if (el) el.innerHTML = html;
  }

  /** 指标磁贴 */
  function tile(label, value, sub, opts) {
    opts = opts || {};
    var cls = opts.cls ? ' ' + opts.cls : '';
    var html = '<div class="tile">';
    html += '<div class="tile-label">' + esc(label) + '</div>';
    html += '<div class="tile-value' + cls + '">' + (opts.raw ? value : esc(value)) + '</div>';
    if (sub !== null && sub !== undefined && sub !== '') {
      html += '<div class="tile-sub">' + (opts.rawSub ? sub : esc(sub)) + '</div>';
    }
    if (opts.bar != null) {
      var w = Math.max(0, Math.min(100, Number(opts.bar) || 0));
      var barCls = w >= 80 ? ' is-high' : w <= 20 ? ' is-low' : '';
      html += '<div class="mini-bar"><span class="' + barCls + '" style="width:' + w.toFixed(1) + '%"></span></div>';
    }
    html += '</div>';
    return html;
  }

  /** 空状态 */
  function emptyState(title, hint, btnLabel, btnId) {
    var html = '<div class="empty-state">';
    html += '<div class="empty-title">' + esc(title || '暂无数据') + '</div>';
    if (hint) html += '<div class="empty-hint">' + esc(hint) + '</div>';
    if (btnLabel) {
      html += '<button class="btn btn-primary" type="button" id="' + esc(btnId || 'emptySyncBtn') + '">' + esc(btnLabel) + '</button>';
    }
    html += '</div>';
    return html;
  }

  function skeleton(text) {
    return '<div class="skeleton">' + esc(text || '加载中…') + '</div>';
  }

  /** 表格一行（cells 为 [{text, cls, raw}] 或字符串） */
  function row(cells, rowCls) {
    var html = '<tr' + (rowCls ? ' class="' + rowCls + '"' : '') + '>';
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      if (c === null || c === undefined) c = { text: DASH };
      else if (typeof c === 'string' || typeof c === 'number') c = { text: c };
      var cls = c.cls ? ' class="' + c.cls + '"' : '';
      var text = c.raw ? c.text : esc(c.text);
      html += '<td' + cls + '>' + text + '</td>';
    }
    return html + '</tr>';
  }

  function table(headers, rows, opts) {
    opts = opts || {};
    var html = '<table class="data-table">';
    html += '<thead><tr>';
    for (var i = 0; i < headers.length; i++) {
      var h = headers[i];
      if (typeof h === 'string') h = { text: h };
      html += '<th' + (h.cls ? ' class="' + h.cls + '"' : '') + '>' + esc(h.text) + '</th>';
    }
    html += '</tr></thead><tbody>';
    if (!rows || !rows.length) {
      html += '<tr><td class="empty-cell" colspan="' + headers.length + '">' + esc(opts.empty || '暂无数据') + '</td></tr>';
    } else {
      html += rows.join('');
    }
    html += '</tbody></table>';
    return html;
  }

  /* ----------------------------- Toast ----------------------------- */

  var toastTimer = {};
  function toast(msg, type, ms) {
    var wrap = q('#toastWrap');
    if (!wrap) return;
    var el = document.createElement('div');
    el.className = 'toast' + (type ? ' is-' + type : '');
    el.textContent = msg;
    wrap.appendChild(el);
    var t = setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, ms || 3200);
    toastTimer[el] = t;
    // 最多保留 4 条
    while (wrap.children.length > 4) wrap.removeChild(wrap.firstChild);
  }

  /* ----------------------------- 其他 ----------------------------- */

  function debounce(fn, wait) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, wait || 150);
    };
  }

  function clamp(v, lo, hi) {
    var n = toNum(v);
    if (n === null) return lo;
    return Math.max(lo, Math.min(hi, n));
  }

  function isNil(v) {
    return v === null || v === undefined || (typeof v === 'number' && !isFinite(v));
  }

  /** 数组尾部截取（用于 mock 窗口裁剪） */
  function tail(arr, n) {
    if (!Array.isArray(arr)) return [];
    if (!n || n <= 0 || n >= arr.length) return arr;
    return arr.slice(arr.length - n);
  }

  App.util = {
    DASH: DASH,
    fmt: fmt,
    isNum: isNum,
    toNum: toNum,
    isNil: isNil,
    signClass: signClass,
    signHex: signHex,
    impactClass: impactClass,
    q: q,
    qq: qq,
    esc: esc,
    setHTML: setHTML,
    tile: tile,
    emptyState: emptyState,
    skeleton: skeleton,
    row: row,
    table: table,
    toast: toast,
    debounce: debounce,
    clamp: clamp,
    tail: tail
  };
})(window);
