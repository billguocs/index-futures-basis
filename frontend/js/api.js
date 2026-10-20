/* ==========================================================================
   frontend/js/api.js · 数据访问层
   - 真实模式：同源 /api/*
   - mock 模式：读取 frontend/mock/*.json（后端未就绪时自动降级）
   强制 mock：URL 加 ?mock=1；强制真实：URL 加 ?api=1
   全局命名空间：window.App.api
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;

  var params = new URLSearchParams(global.location.search);
  var FORCE_MOCK = params.get('mock') === '1' || params.get('mock') === 'true';
  var FORCE_REAL = params.get('api') === '1' || params.get('api') === 'true';
  var MOCK_DIR = 'mock/';
  var TIMEOUT = 8000;

  var state = {
    mode: null,          // 'real' | 'mock'
    apiAvailable: null,  // 探测结果
    meta: null
  };

  var cache = {
    dashboard: {},   // key: variety + ':' + lookback
    members: {},     // key: variety + ':' + lookback
    ranks: {}        // key: variety + ':' + date
  };

  /* ----------------------------- 基础请求 ----------------------------- */

  function withTimeout(promise, ms) {
    var timer = null;
    var timeout = new Promise(function (_, reject) {
      timer = setTimeout(function () {
        reject(new Error('请求超时（' + (ms / 1000) + 's）'));
      }, ms || TIMEOUT);
    });
    return Promise.race([promise, timeout]).then(
      function (v) { clearTimeout(timer); return v; },
      function (e) { clearTimeout(timer); throw e; }
    );
  }

  function getJSON(url, ms) {
    return withTimeout(
      fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } }).then(function (res) {
        if (!res.ok) {
          var err = new Error('HTTP ' + res.status + ' · ' + url);
          err.status = res.status;
          throw err;
        }
        var ct = res.headers.get('content-type') || '';
        if (ct.indexOf('json') < 0) {
          // 静态服务器对 /api/* 常返回 404 HTML 页面
          throw new Error('非 JSON 响应（' + url + '）');
        }
        return res.json();
      }),
      ms
    );
  }

  function mockURL(name) {
    return MOCK_DIR + name + '.json?v=' + (state.mockStamp || '1');
  }

  function mockMode() {
    return state.mode === 'mock';
  }

  /* ----------------------------- 模式探测 ----------------------------- */

  function detectMode() {
    if (FORCE_MOCK) {
      state.mode = 'mock';
      state.apiAvailable = false;
      return Promise.resolve('mock');
    }
    return getJSON('api/meta', 3500).then(
      function (meta) {
        if (meta && meta.ok) {
          state.mode = 'real';
          state.apiAvailable = true;
          return 'real';
        }
        // 接口在但返回 ok:false（例如库还没数据）→ 仍走真实模式，界面显示空态
        state.mode = 'real';
        state.apiAvailable = true;
        return 'real';
      },
      function () {
        if (FORCE_REAL) return Promise.reject(new Error('无法连接 /api/meta，请确认后端已启动（去掉 ?api=1 可回退示例数据）'));
        state.mode = 'mock';
        state.apiAvailable = false;
        return 'mock';
      }
    );
  }

  /* ----------------------------- meta ----------------------------- */

  function getMeta() {
    var p = mockMode() ? getJSON(mockURL('meta')) : getJSON('api/meta');
    return p.then(function (meta) {
      state.meta = meta;
      return meta;
    });
  }

  /* ----------------------------- dashboard ----------------------------- */

  function cacheKey(variety, lookback) {
    return variety + ':' + (lookback || 0);
  }

  /**
   * mock 下按 lookback 裁剪尾部，模拟真实接口的窗口语义。
   */
  function trimToLookback(dash, lookback) {
    var n = Number(lookback) || 0;
    if (!dash || !dash.series || !n) return dash;
    var dates = dash.series.date || [];
    if (dates.length <= n) {
      dash.window = { start: dates[0] || null, end: dates[dates.length - 1] || null, n: dates.length };
      dash.lookback = n;
      return dash;
    }
    Object.keys(dash.series).forEach(function (k) {
      dash.series[k] = util.tail(dash.series[k], n);
    });
    if (dash.fit && dash.fit.rolling_corr) {
      Object.keys(dash.fit.rolling_corr).forEach(function (k) {
        dash.fit.rolling_corr[k] = util.tail(dash.fit.rolling_corr[k], n);
      });
    }
    if (dash.backtest && dash.backtest.equity) {
      Object.keys(dash.backtest.equity).forEach(function (k) {
        dash.backtest.equity[k] = util.tail(dash.backtest.equity[k], n);
      });
    }
    dash.lookback = n;
    dash.window = { start: dates[dates.length - n], end: dates[dates.length - 1], n: n };
    // 诚实标注：mock 只裁剪曲线，统计类指标仍是全样本结果
    if (!dash.__mockTrimWarned) {
      dash.__mockTrimWarned = true;
      dash.warnings = (dash.warnings || []).concat([
        '示例数据模式：切换时间窗口仅裁剪图表曲线；拟合、分位、回测、次日研判等统计仍基于 mock 的完整 ' +
        dates.length + ' 日样本计算，接入真实接口后将按所选窗口重算。'
      ]);
    }
    return dash;
  }

  function getDashboard(variety, lookback, member) {
    var key = cacheKey(variety, lookback);
    if (cache.dashboard[key]) return Promise.resolve(cache.dashboard[key]);

    var p;
    if (mockMode()) {
      p = getJSON(mockURL('dashboard_' + variety)).then(function (d) {
        return trimToLookback(d, lookback);
      });
    } else {
      var qs = '?variety=' + encodeURIComponent(variety) +
        '&lookback=' + encodeURIComponent(lookback || 0) +
        (member ? '&member=' + encodeURIComponent(member) : '');
      p = getJSON('api/dashboard' + qs);
    }

    return p.then(function (dash) {
      if (!dash || typeof dash !== 'object') throw new Error('dashboard 返回体为空');
      cache.dashboard[key] = dash;
      return dash;
    });
  }

  /* ----------------------------- members ----------------------------- */

  function getMembers(variety, lookback) {
    var key = cacheKey(variety, lookback);
    if (cache.members[key]) return Promise.resolve(cache.members[key]);
    var p;
    if (mockMode()) {
      p = getJSON(mockURL('members_' + variety)).then(function (m) {
        if (m && m.rows && m.rows.length) m.lookback = lookback || m.lookback;
        return m;
      });
    } else {
      p = getJSON('api/members?variety=' + encodeURIComponent(variety) +
        '&lookback=' + encodeURIComponent(lookback || 0));
    }
    return p.then(function (m) {
      cache.members[key] = m;
      return m;
    });
  }

  /* ----------------------------- ranks ----------------------------- */

  function getRanks(variety, date, member) {
    var key = variety + ':' + (date || '');
    if (cache.ranks[key]) return Promise.resolve(cache.ranks[key]);
    var p;
    if (mockMode()) {
      p = getJSON(mockURL('ranks_' + variety));
    } else {
      p = getJSON('api/ranks?variety=' + encodeURIComponent(variety) +
        (date ? '&date=' + encodeURIComponent(date) : '') +
        (member ? '&member=' + encodeURIComponent(member) : ''));
    }
    return p.then(function (r) {
      cache.ranks[key] = r;
      return r;
    });
  }

  /* ----------------------------- 同步 ----------------------------- */

  function getSyncStatus() {
    if (mockMode()) return Promise.resolve({ ok: true, running: false, progress: null, last: null, mock: true });
    return getJSON('api/sync/status', 5000);
  }

  function postSync(start) {
    if (mockMode()) {
      // 示例数据模式：不请求后端，前端模拟一个"同步完成"，仅用于演示交互
      return new Promise(function (resolve) {
        setTimeout(function () { resolve({ ok: true, started: true, mock: true }); }, 700);
      });
    }
    return withTimeout(
      fetch('api/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: start || '2022-01-03' })
      }).then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status + ' · api/sync');
        return res.json();
      }),
      15000
    );
  }

  /* ----------------------------- 导出 ----------------------------- */

  function exportUrl(variety, lookback) {
    return 'api/export?variety=' + encodeURIComponent(variety) +
      '&lookback=' + encodeURIComponent(lookback || 0);
  }

  /** mock 模式下由 series 拼 CSV（真实模式直接下载 export 接口） */
  function buildMockCSV(dash) {
    var s = (dash && dash.series) || {};
    var cols = ['date', 'index_close', 'index_ret_pct', 'citic_net', 'citic_net_pct', 'dnet_pct_of_oi', 'market_oi'];
    var keys = ['date', 'index_close', 'index_ret_pct', 'citic_net', 'citic_net_pct', 'dnet_pct_of_oi', 'market_oi'];
    var lines = [cols.join(',')];
    var n = (s.date || []).length;
    for (var i = 0; i < n; i++) {
      var cells = [];
      for (var j = 0; j < keys.length; j++) {
        var v = s[keys[j]] ? s[keys[j]][i] : null;
        cells.push(v === null || v === undefined ? '' : v);
      }
      lines.push(cells.join(','));
    }
    return '\ufeff' + lines.join('\n');
  }

  function downloadMockCSV(dash, filename) {
    var blob = new Blob([buildMockCSV(dash)], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  }

  /* ----------------------------- 缓存控制 ----------------------------- */

  function clearCache() {
    cache.dashboard = {};
    cache.members = {};
    cache.ranks = {};
  }

  function bustMock() {
    state.mockStamp = String(Date.now());
  }

  App.api = {
    state: state,
    detectMode: detectMode,
    mockMode: mockMode,
    getMeta: getMeta,
    getDashboard: getDashboard,
    getMembers: getMembers,
    getRanks: getRanks,
    getSyncStatus: getSyncStatus,
    postSync: postSync,
    exportUrl: exportUrl,
    downloadMockCSV: downloadMockCSV,
    clearCache: clearCache,
    bustMock: bustMock,
    params: params
  };
})(window);
