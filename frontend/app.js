/* ==========================================================================
   frontend/app.js · 应用编排：模式探测 → meta → Tabs → 拉数 → 渲染六区块
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App;
  var util = App.util;
  var fmt = util.fmt;
  var api = App.api;
  var charts = App.charts;

  var LOOKBACK_LABEL = { 240: '1年', 480: '2年', 720: '3年', 0: '全部' };
  var FALLBACK_VARIETIES = [
    { code: 'IF', name: '沪深300股指期货', color: '#4d7cfe' },
    { code: 'IH', name: '上证50股指期货', color: '#f0a020' },
    { code: 'IC', name: '中证500股指期货', color: '#a855f7' },
    { code: 'IM', name: '中证1000股指期货', color: '#2dd4bf' }
  ];

  var state = {
    meta: null,
    variety: 'IF',
    lookback: 720,
    member: '中信期货',
    dash: null,
    allDash: {},
    membersRows: null,
    membersLoading: false,
    loading: false,
    syncing: false,
    syncTimer: null,
    syncDeadline: 0
  };

  function varieties() {
    var list = state.meta && state.meta.varieties;
    return (list && list.length) ? list : FALLBACK_VARIETIES;
  }

  function lookbackOptions() {
    var opts = state.meta && state.meta.lookback_options;
    if (!opts || !opts.length) return [240, 480, 720, 0];
    return opts;
  }

  function currentVariety() {
    var list = varieties();
    for (var i = 0; i < list.length; i++) if (list[i].code === state.variety) return list[i];
    return list[0] || null;
  }

  function ctx() {
    return {
      meta: state.meta,
      variety: state.variety,
      lookback: state.lookback,
      member: state.member,
      allDash: state.allDash,
      ranks: null,
      membersRows: state.membersRows,
      membersLoading: state.membersLoading
    };
  }

  /* ============================== 提示 / 骨架 ============================== */

  function showBanner(type, text, actions) {
    var el = document.getElementById('banner');
    if (!el) return;
    el.hidden = false;
    el.className = 'banner banner-' + (type || 'info');
    var html = '<span>' + util.esc(text) + '</span><div class="banner-actions">';
    (actions || []).forEach(function (a) {
      html += '<a class="btn btn-ghost" href="' + util.esc(a.href || '#') + '">' + util.esc(a.label) + '</a>';
    });
    html += '<button type="button" data-dismiss="banner">关闭</button></div>';
    el.innerHTML = html;
  }

  function hideBanner() {
    var el = document.getElementById('banner');
    if (el) { el.hidden = true; el.innerHTML = ''; }
  }

  function setSyncText(text, cls) {
    var el = document.getElementById('syncStatus');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'sync-status' + (cls ? ' is-' + cls : '');
  }

  function setLoading(on) {
    state.loading = !!on;
    var btn = document.getElementById('btnExport');
    if (btn) btn.disabled = !!on || !state.dash;
    document.body.classList.toggle('is-loading', !!on);
  }

  function renderSkeletons() {
    ['overviewTiles', 'fitStats', 'btMetrics'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.innerHTML = new Array(6).join(util.skeleton('加载中') );
    });
    ['fitModels', 'fitStability'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.innerHTML = util.skeleton('加载中…');
    });
    var body = document.getElementById('bucketBody');
    if (body) body.innerHTML = util.skeleton('加载中…');
    var extra = document.getElementById('btExtra');
    if (extra) extra.innerHTML = '';
    var gh = document.getElementById('guidanceBody');
    if (gh) gh.innerHTML = util.skeleton('正在生成次日研判…');
    var cards = document.getElementById('varietyCards');
    if (cards) cards.innerHTML = util.skeleton('加载四品种对比…');
    var rh = document.getElementById('rankHighlight');
    if (rh) rh.innerHTML = util.skeleton('加载龙虎榜…');
    var rt = document.getElementById('rankTables');
    if (rt) rt.innerHTML = util.skeleton('加载龙虎榜…');
    var rm = document.getElementById('rankMembers');
    if (rm) rm.innerHTML = util.skeleton('加载会员净持仓…');
  }

  /* ============================== 顶栏 / 章台 ============================== */

  function renderChrome() {
    var badge = document.getElementById('modeBadge');
    var mock = api.mockMode();
    if (badge) {
      badge.hidden = false;
      badge.className = 'badge ' + (mock ? 'badge-mock' : 'badge-real');
      badge.textContent = mock ? '示例数据' : '实时接口';
      badge.title = mock
        ? '当前读取 frontend/mock/*.json 示例数据（结构符合契约，数值为模拟）'
        : '当前读取同源 /api/* 真实接口数据';
    }

    if (mock) {
      showBanner('info',
        '当前为示例数据（前端内置 mock，字段结构严格遵循契约，数值为模拟）。后端接口就绪后刷新本页，或点右侧按钮尝试连接真实接口。',
        [{ label: '尝试连接真实接口', href: '?api=1' }]);
    }

    var v = currentVariety();
    var vm = document.getElementById('varietyMeta');
    if (vm && v) {
      vm.textContent = v.name + ' · 指数 ' + (v.index_name || '') + ' ' + (v.index_code || '') +
        ' · 合约乘数 ' + fmt.num(v.multiplier);
    }
    renderMetaInfo();
    renderNotes();
    renderTabs();
    renderFooter();
  }

  function renderMetaInfo() {
    var el = document.getElementById('metaInfo');
    if (!el) return;
    var m = state.meta || {};
    var parts = [];
    if (m.latest_trade_date) parts.push('最新交易日 ' + m.latest_trade_date);
    if (m.generated_at) parts.push('接口生成 ' + String(m.generated_at).replace('T', ' ').slice(0, 19));
    if (m.data && m.data.last_sync) parts.push('上次同步 ' + m.data.last_sync);
    el.textContent = parts.join(' · ');
  }

  function renderTabs() {
    var vt = document.getElementById('varietyTabs');
    if (vt) {
      vt.innerHTML = varieties().map(function (v) {
        var on = v.code === state.variety;
        return '<button type="button" class="tab' + (on ? ' is-active' : '') +
          '" data-variety="' + util.esc(v.code) + '" role="tab" aria-selected="' + (on ? 'true' : 'false') + '">' +
          util.esc(v.code) + '<span class="tab-sub">' + util.esc(String(v.name || '').replace('股指期货', '')) + '</span></button>';
      }).join('');
    }
    var lt = document.getElementById('lookbackTabs');
    if (lt) {
      lt.innerHTML = lookbackOptions().map(function (n) {
        var on = Number(n) === Number(state.lookback);
        var label = LOOKBACK_LABEL[n] || (n ? n + '日' : '全部');
        return '<button type="button" class="tab' + (on ? ' is-active' : '') +
          '" data-lookback="' + n + '" role="tab" aria-selected="' + (on ? 'true' : 'false') + '">' +
          label + '<span class="tab-sub">' + (n ? n + '日' : '全部历史') + '</span></button>';
      }).join('');
    }
    var vv = document.getElementById('variety');
    // 兼容：index.html 里未预留 #variety（无此节点时静默跳过）
    if (vv) vv.textContent = state.variety;
  }

  function renderFooter() {
    var el = document.getElementById('footer');
    if (!el) return;
    var m = state.meta || {};
    var mode = api.mockMode() ? '示例数据模式（mock）' : '真实接口模式（/api）';
    el.innerHTML =
      '<span>股指期货持仓研判看板 · 六大区块：次日研判 / 持仓总览 / 拟合分析 / 分位胜率 / 回测 / 会员龙虎榜</span>' +
      '<span>数据来源：中国金融期货交易所会员持仓排名 + 中证指数公司指数日线 · ' + util.esc(mode) + '</span>' +
      '<span class="disclaimer">' + util.esc(m.disclaimer || '本页所有统计与信号均为历史数据描述，不构成任何投资建议；期货交易有杠杆，风险自负。') + '</span>';
  }

  function renderNotes() {
    var el = document.getElementById('notesBody');
    if (!el) return;
    var m = state.meta || {};
    var d = m.data || {};
    function span(x) {
      if (!x) return '—';
      return (x.start || '—') + ' ~ ' + (x.end || '—') + '（' + fmt.num(x.rows) + ' 行）';
    }
    var html = '<ul class="notes-list">';
    html += '<li><b>数据覆盖</b>：指数日线 ' + util.esc(span(d.index_daily)) +
      '；品种日线 ' + util.esc(span(d.variety_daily)) +
      '；会员持仓排名 ' + util.esc(span(d.member_rank)) + '；品种 ' +
      util.esc(varieties().map(function (v) { return v.code; }).join(' / ')) + '。</li>';
    html += '<li><b>时点口径</b>：T 日盘后可知 T 日持仓排名与收盘价，因此「次日研判」的下一个交易日为 T+1；' +
      '所有预测相关指标均为因子 t 与收益 t+1 的相关。</li>';
    html += '<li><b>标准化</b>：逗号后的 z 值与分位均基于滚动 252 个交易日（至少 60 个样本）计算，' +
      '避免全样本后视偏差；分位 0 表示历史最低（净空最重），100 表示最高。</li>';
    html += '<li><b>胜率与 IC</b>：胜率 = 次日收益 &gt; 0 的比例；IC 为因子值与次日收益的秩相关，' +
      '|IC| &lt; 0.03 视为几乎无预测力；回归显著性以 p &lt; 0.05 判定。</li>';
    html += '<li><b>回测口径</b>：信号用 T 日盘后分位，T+1 收盘结算；净值为策略累计净值，' +
      '已扣换仓成本，未计保证金利息与滑点；样本内结果存在过拟合风险。</li>';
    html += '<li><b>颜色约定</b>：遵循 A 股习惯，<span class="up">红色 = 上涨/偏多</span>，' +
      '<span class="down">绿色 = 下跌/偏空</span>；「—”表示该字段在接口中为 null。</li>';
    var warnings = (state.dash && state.dash.warnings) || [];
    warnings.forEach(function (w) {
      if (!w) return;
      html += '<li class="warn-item">⚠ ' + util.esc(w) + '</li>';
    });
    html += '</ul>';
    el.innerHTML = html;
  }

  function updateAsOf() {
    var d = state.dash;
    var asOf = document.getElementById('asOf');
    if (asOf) {
      asOf.textContent = (d && ((d.window && d.window.end) || d.as_of)) || (state.meta && state.meta.latest_trade_date) || '—';
    }
    var mi = document.getElementById('metaInfo');
    if (mi && d && d.window) {
      var parts = ['可视窗口 ' + d.window.start + ' ~ ' + d.window.end + ' · ' + fmt.num(d.window.n) + ' 个交易日'];
      if (d.as_of) parts.push('数据日 ' + d.as_of);
      if (d.member) parts.push('会员 ' + d.member);
      mi.textContent = parts.join(' · ');
    }
  }

  /* ============================== 渲染 ============================== */

  function renderAll() {
    var c = ctx();
    var sections = [
      ['guidance', function () { App.render.guidance(state.dash, c); }],
      ['overview', function () { App.render.overview(state.dash, c); }],
      ['fit', function () { App.render.fit(state.dash, c); }],
      ['buckets', function () { App.render.buckets(state.dash); }],
      ['backtest', function () { App.render.backtest(state.dash); }],
      ['ranks', function () { App.render.ranks(state.dash, c); }]
    ];
    sections.forEach(function (s) {
      try {
        s[1]();
      } catch (err) {
        if (global.console) console.error('[render:' + s[0] + ']', err);
      }
    });
    renderNotes();
    updateAsOf();
    charts.resizeAll();
  }

  /* ============================== 数据加载 ============================== */

  function loadMeta() {
    return api.getMeta().catch(function (err) {
      if (global.console) console.error('[meta]', err);
      return null;
    });
  }

  function initStateFromMeta() {
    var m = state.meta || {};
    var p = api.params;
    var qv = p.get('variety');
    var codes = varieties().map(function (v) { return v.code; });
    state.variety = (qv && codes.indexOf(qv.toUpperCase()) >= 0) ? qv.toUpperCase() : codes[0] || 'IF';

    var ql = p.get('lookback');
    var opts = lookbackOptions();
    var l = ql === null ? Number(m.default_lookback) : Number(ql);
    if (opts.map(Number).indexOf(l) < 0) l = Number(m.default_lookback) || opts[0];
    state.lookback = isFinite(l) ? l : 720;

    state.member = p.get('member') || m.default_member || '中信期货';
  }

  function syncURL() {
    try {
      var p = new URLSearchParams(global.location.search);
      p.set('variety', state.variety);
      p.set('lookback', String(state.lookback));
      p.set('member', state.member);
      var qs = p.toString();
      global.history.replaceState(null, '', global.location.pathname + (qs ? '?' + qs : ''));
    } catch (e) { /* 忽略（file:// 等环境） */ }
  }

  function loadDashboard() {
    return api.getDashboard(state.variety, state.lookback, state.member).then(function (dash) {
      state.dash = dash;
      state.allDash[state.variety] = dash;
      if (dash && dash.ok === false) {
        showBanner('error', '接口返回错误：' + (dash.error || '未知错误') + '。可尝试点击「立即更新数据」或切换窗口。');
      } else if (dash && dash.empty) {
        showBanner('info', '当前品种在该窗口内没有可用数据（empty=true）。可扩大时间窗口或点击「立即更新数据」。');
      } else if (!api.mockMode()) {
        hideBanner();
      }
      renderAll();
      return dash;
    }, function (err) {
      state.dash = null;
      renderAll();
      showBanner('error', '数据加载失败：' + err.message + '。请确认后端已启动（或使用 ?mock=1 查看示例数据）。');
      return null;
    });
  }

  function loadAllVarieties() {
    var codes = varieties().map(function (v) { return v.code; }).filter(function (c) { return c !== state.variety; });
    return Promise.all(codes.map(function (code) {
      return api.getDashboard(code, state.lookback, state.member).then(function (d) {
        state.allDash[code] = d;
      }, function () {
        state.allDash[code] = null;
      }).then(function () {
        try { App.render.overview(state.dash, ctx()); } catch (e) { /* noop */ }
      });
    }));
  }

  function loadMembers() {
    state.membersLoading = true;
    try { App.render.ranks(state.dash, ctx()); } catch (e) { /* noop */ }
    return api.getMembers(state.variety, state.lookback).then(function (m) {
      state.membersRows = (m && m.rows) || null;
    }, function () {
      state.membersRows = null;
    }).then(function () {
      state.membersLoading = false;
      try { App.render.ranks(state.dash, ctx()); } catch (e) { /* noop */ }
    });
  }

  function refresh(bust) {
    if (bust) {
      api.bustMock();
      api.clearCache();
    }
    state.allDash = {};
    state.membersRows = null;
    state.membersLoading = true;
    setLoading(true);
    renderSkeletons();
    setSyncText(state.syncing ? '同步中…' : '');
    return loadDashboard().then(function () {
      syncURL();
      return Promise.all([loadAllVarieties(), loadMembers(), pollSyncStatus(true)]);
    }).then(function () {
      setLoading(false);
    }, function () {
      setLoading(false);
    });
  }

  /* ============================== 更新数据 ============================== */

  function setSyncBusy(on) {
    state.syncing = !!on;
    var btn = document.getElementById('btnSync');
    if (!btn) return;
    btn.disabled = !!on;
    btn.classList.toggle('is-loading', !!on);
    var label = btn.querySelector('.btn-label');
    if (label) label.textContent = on ? '同步中…' : '立即更新数据';
  }

  function doSync() {
    if (state.syncing) return;
    setSyncBusy(true);
    setSyncText('正在提交更新请求…');

    api.postSync('2022-01-03').then(function (res) {
      if (api.mockMode()) {
        return mockSyncFlow();
      }
      if (!res || res.ok === false) throw new Error((res && res.error) || '更新请求被拒绝');
      setSyncText('已启动后台同步，正在等待…');
      return waitForSync();
    }).then(function () {
      setSyncText('同步完成，正在重新加载…', 'ok');
      util.toast('数据更新完成，正在刷新看板', 'ok');
      return refresh(true);
    }).catch(function (err) {
      setSyncText('同步失败：' + err.message, 'error');
      util.toast('同步失败：' + err.message, 'error', 5000);
    }).then(function () {
      setSyncBusy(false);
    });
  }

  function mockSyncFlow() {
    return new Promise(function (resolve) {
      var steps = [
        [0, '示例模式：校验 mock 数据结构…'],
        [500, '示例模式：重新载入 JSON…'],
        [1100, '示例模式：准备重绘…']
      ];
      steps.forEach(function (s) {
        setTimeout(function () { setSyncText(s[1]); }, s[0]);
      });
      setTimeout(function () {
        util.toast('示例数据模式不会真正更新行情；接入后端后此按钮将触发中金所数据同步', 'warn', 4600);
        resolve();
      }, 1500);
    });
  }

  function pollSyncStatus(silent) {
    return api.getSyncStatus().then(function (st) {
      if (!st) return;
      if (st.running && st.progress) {
        var done = fmt.num(st.progress.done), total = fmt.num(st.progress.total);
        setSyncText((st.progress.label || '同步中') + ' ' + done + ' / ' + total);
        if (!silent) schedulePoll();
      } else if (!silent && st.last && st.last.at) {
        setSyncText('上次同步 ' + st.last.at + ' · ' + (st.last.result || ''), 'ok');
      }
    }, function () { /* 同步状态不可用时不打扰用户 */ });
  }

  function schedulePoll() {
    clearTimeout(state.syncTimer);
    state.syncTimer = setTimeout(function () { pollSyncStatus(false); }, 2500);
  }

  function waitForSync() {
    var start = Date.now();
    return new Promise(function (resolve, reject) {
      function tick() {
        api.getSyncStatus().then(function (st) {
          if (!st || !st.running) {
            setSyncText('同步已完成', 'ok');
            return resolve(st);
          }
          if (st.progress) {
            setSyncText((st.progress.label || '同步中') + ' ' +
              fmt.num(st.progress.done) + ' / ' + fmt.num(st.progress.total));
          }
          if (Date.now() - start > 12 * 60 * 1000) return reject(new Error('等待同步超时（12 分钟）'));
          setTimeout(tick, 1500);
        }, function (err) {
          if (Date.now() - start > 12 * 60 * 1000) return reject(err);
          setTimeout(tick, 2500);
        });
      }
      tick();
    });
  }

  /* ============================== 导出 ============================== */

  function doExport() {
    if (!state.dash) {
      util.toast('当前没有可导出的数据', 'warn');
      return;
    }
    var name = 'cffex_' + state.variety + '_' + (state.lookback || 'all') + '_' + (state.dash.as_of || 'latest') + '.csv';
    if (api.mockMode()) {
      api.downloadMockCSV(state.dash, name);
      util.toast('已按当前窗口导出示例 CSV（含 BOM，可直接用 Excel 打开）', 'ok');
    } else {
      global.location.href = api.exportUrl(state.variety, state.lookback);
      util.toast('正在导出 CSV…', 'ok');
    }
  }

  /* ============================== 事件 ============================== */

  function bindEvents() {
    document.addEventListener('click', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;

      var dismiss = t.closest('[data-dismiss="banner"]');
      if (dismiss) { hideBanner(); return; }

      var vtab = t.closest('[data-variety]');
      if (vtab) {
        var code = vtab.getAttribute('data-variety');
        if (code && code !== state.variety) {
          state.variety = code;
          state.dash = null;
          state.membersRows = null;
          state.membersLoading = true;
          renderTabs();
          renderChromeLight();
          refresh(false);
        }
        return;
      }

      var ltab = t.closest('[data-lookback]');
      if (ltab) {
        var n = Number(ltab.getAttribute('data-lookback'));
        if (isFinite(n) && n !== state.lookback) {
          state.lookback = n;
          renderTabs();
          refresh(false);
        }
        return;
      }

      var tgt = t.closest('button');
      if (tgt) {
        if (tgt.id === 'btnSync') { doSync(); return; }
        if (tgt.id === 'btnExport') { doExport(); return; }
        // 空态里的「立即更新数据」按钮（id 以 EmptySync 结尾或位于 .empty-state 内）
        if ((tgt.id && /Sync$/.test(tgt.id)) || tgt.closest('.empty-state')) {
          if (tgt.id !== 'btnSync') { doSync(); return; }
        }
      }
    });

    global.addEventListener('resize', util.debounce(function () { charts.resizeAll(); }, 120));
    global.addEventListener('keydown', function (e) {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      var idx = varieties().map(function (v) { return v.code; }).indexOf(state.variety);
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        if (idx < 0) return;
        var next = (idx + (e.key === 'ArrowRight' ? 1 : -1) + varieties().length) % varieties().length;
        state.variety = varieties()[next].code;
        state.dash = null;
        state.membersRows = null;
        state.membersLoading = true;
        renderTabs();
        renderChromeLight();
        refresh(false);
      }
    });
  }

  function renderChromeLight() {
    var v = currentVariety();
    var vm = document.getElementById('varietyMeta');
    if (vm && v) {
      vm.textContent = v.name + ' · 指数 ' + (v.index_name || '') + ' ' + (v.index_code || '') +
        ' · 合约乘数 ' + fmt.num(v.multiplier);
    }
    renderTabs();
  }

  /* ============================== 启动 ============================== */

  function boot() {
    charts.bindResize();
    bindEvents();
    setSyncText('正在检测数据接口…');

    api.detectMode().then(function (mode) {
      return loadMeta();
    }).then(function (meta) {
      state.meta = meta || {};
      initStateFromMeta();
      renderChrome();
      setSyncText('');
      return refresh(false);
    }).catch(function (err) {
      var msg = (err && err.message) || String(err);
      setSyncText('接口不可用：' + msg, 'error');
      showBanner('error', '初始化失败：' + msg + '。可尝试在 URL 后加 ?mock=1 使用示例数据。',
        [{ label: '使用示例数据', href: '?mock=1' }]);
      if (global.console) console.error('[boot]', err);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
