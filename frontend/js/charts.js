/* ==========================================================================
   frontend/js/charts.js · ECharts 图表封装
   - 容器在 index.html 中静态存在，实例复用，避免重建
   - 统一深色主题 + 涨红跌绿
   全局命名空间：window.App.charts
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;
  var fmt = util.fmt;

  var C = {
    text: '#8b949e',
    textStrong: '#e6edf3',
    axis: '#30363d',
    split: 'rgba(48, 54, 61, 0.5)',
    up: '#f6465d',
    down: '#0ecb81',
    accent: '#4d7cfe',
    accentSoft: 'rgba(77, 124, 254, 0.16)',
    purple: '#a855f7',
    warn: '#e3b341',
    orange: '#f0883e',
    dim: '#6e7681',
    tooltipBg: 'rgba(22, 27, 34, 0.96)'
  };

  var instances = {};
  var resizeBound = false;

  /* ----------------------------- 实例管理 ----------------------------- */

  function getChart(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    if (!global.echarts) {
      el.innerHTML = '<div class="chart-fallback">图表库未加载：vendor/echarts.min.js</div>';
      return null;
    }
    var inst = echarts.getInstanceByDom(el);
    if (!inst) {
      if (el.querySelector('.chart-fallback')) el.innerHTML = '';
      inst = echarts.init(el, null, { renderer: 'canvas' });
    }
    instances[id] = inst;
    return inst;
  }

  function draw(id, option) {
    var inst = getChart(id);
    if (!inst) return null;
    inst.setOption(option, true);
    return inst;
  }

  function showEmpty(id, text) {
    var el = document.getElementById(id);
    if (!el) return;
    if (global.echarts) {
      var inst = echarts.getInstanceByDom(el);
      if (inst) { inst.dispose(); }
    }
    delete instances[id];
    el.innerHTML = '<div class="chart-fallback">' + util.esc(text || '暂无数据') + '</div>';
  }

  function resizeAll() {
    Object.keys(instances).forEach(function (id) {
      var inst = instances[id];
      if (!inst || inst.isDisposed || inst.isDisposed()) return;
      try { inst.resize(); } catch (e) { /* ignore */ }
    });
  }

  function bindResize() {
    if (resizeBound) return;
    resizeBound = true;
    var onResize = util.debounce(function () {
      requestAnimationFrame(resizeAll);
    }, 120);
    global.addEventListener('resize', onResize);
    global.addEventListener('orientationchange', onResize);
    if (global.ResizeObserver) {
      var ro = new ResizeObserver(util.debounce(function () { requestAnimationFrame(resizeAll); }, 80));
      util.qq('.chart').forEach(function (el) { ro.observe(el); });
    }
  }

  /* ----------------------------- 公共配置 ----------------------------- */

  function tooltipBase(extra) {
    var t = {
      trigger: 'axis',
      backgroundColor: C.tooltipBg,
      borderColor: '#30363d',
      borderWidth: 1,
      padding: [7, 10],
      textStyle: { color: C.textStrong, fontSize: 11 },
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(139,148,158,0.4)', width: 1 } }
    };
    if (extra) Object.keys(extra).forEach(function (k) { t[k] = extra[k]; });
    return t;
  }

  function legendBase(extra) {
    var l = {
      top: 0,
      left: 2,
      itemWidth: 10,
      itemHeight: 8,
      itemGap: 12,
      textStyle: { color: C.text, fontSize: 10.5 }
    };
    if (extra) Object.keys(extra).forEach(function (k) { l[k] = extra[k]; });
    return l;
  }

  function catAxis(dates, extra) {
    var a = {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLine: { lineStyle: { color: C.axis } },
      axisTick: { show: false },
      axisLabel: { color: C.dim, fontSize: 10, hideOverlap: true },
      splitLine: { show: false }
    };
    if (extra) Object.keys(extra).forEach(function (k) { a[k] = extra[k]; });
    return a;
  }

  function valAxis(extra) {
    var a = {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: C.dim, fontSize: 10 },
      splitLine: { lineStyle: { color: C.split, type: 'dashed' } }
    };
    if (extra) Object.keys(extra).forEach(function (k) { a[k] = extra[k]; });
    return a;
  }

  function zoomBase(showSlider) {
    var z = [{ type: 'inside', zoomOnMouseWheel: true, moveOnMouseWheel: false }];
    if (showSlider !== false) {
      z.push({
        type: 'slider',
        height: 12,
        bottom: 4,
        borderColor: 'transparent',
        backgroundColor: 'rgba(110,118,129,0.10)',
        fillerColor: 'rgba(77,124,254,0.16)',
        dataBackground: { lineStyle: { color: C.axis }, areaStyle: { color: 'rgba(48,54,61,0.4)' } },
        handleStyle: { color: '#3a434e', borderColor: '#3a434e' },
        moveHandleSize: 0,
        textStyle: { color: C.dim, fontSize: 9 }
      });
    }
    return z;
  }

  function markZero() {
    return {
      silent: true,
      symbol: 'none',
      lineStyle: { color: 'rgba(139,148,158,0.55)', type: 'dashed', width: 1 },
      data: [{ yAxis: 0 }]
    };
  }

  /* ----------------------------- 1. 净持仓 vs 指数 ----------------------------- */

  function drawFitNet(dash) {
    var s = (dash && dash.series) || {};
    var dates = s.date || [];
    if (!dates.length || !s.citic_net) {
      showEmpty('chart-fit-net', '暂无净持仓序列数据');
      return;
    }
    var net = s.citic_net;
    var close = s.index_close || [];
    draw('chart-fit-net', {
      backgroundColor: 'transparent',
      animationDuration: 350,
      grid: { left: 60, right: 56, top: 26, bottom: 40 },
      legend: legendBase({ data: ['中信净持仓', '指数收盘'] }),
      tooltip: tooltipBase({
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var out = '<div style="color:' + C.text + ';font-size:10.5px;margin-bottom:3px">' + ps[0].axisValue + '</div>';
          ps.forEach(function (p) {
            var val = p.seriesName === '中信净持仓' ? fmt.signedNum(p.value) + ' 手' : fmt.dec(p.value, 2);
            out += '<div style="display:flex;gap:8px;align-items:center">' + p.marker +
              '<span style="flex:1;color:' + C.text + '">' + p.seriesName + '</span>' +
              '<b style="font-family:monospace">' + val + '</b></div>';
          });
          return out;
        }
      }),
      xAxis: catAxis(dates, { boundaryGap: false }),
      yAxis: [
        valAxis({
          name: '净持仓(手)',
          nameTextStyle: { color: C.dim, fontSize: 10, padding: [0, 0, 0, -6] },
          axisLabel: {
            color: C.dim, fontSize: 10,
            formatter: function (v) { return fmt.wan(v); }
          }
        }),
        valAxis({
          name: '指数',
          nameTextStyle: { color: C.dim, fontSize: 10 },
          splitLine: { show: false },
          axisLabel: { color: C.dim, fontSize: 10, formatter: function (v) { return fmt.dec(v, 0); } },
          scale: true
        })
      ],
      dataZoom: zoomBase(true),
      series: [
        {
          name: '中信净持仓',
          type: 'line',
          data: net,
          showSymbol: false,
          symbol: 'circle',
          symbolSize: 4,
          smooth: false,
          sampling: 'lttb',
          lineStyle: { width: 1.5, color: C.accent },
          itemStyle: { color: C.accent },
          areaStyle: {
            color: {
              type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(77,124,254,0.28)' },
                { offset: 1, color: 'rgba(77,124,254,0.01)' }
              ]
            }
          },
          markLine: markZero()
        },
        {
          name: '指数收盘',
          type: 'line',
          yAxisIndex: 1,
          data: close,
          showSymbol: false,
          sampling: 'lttb',
          lineStyle: { width: 1.2, color: C.warn, opacity: 0.9 },
          itemStyle: { color: C.warn }
        }
      ]
    });
  }

  /* ----------------------------- 2. 散点 + 回归线 ----------------------------- */

  function drawScatter(dash) {
    var fit = (dash && dash.fit) || {};
    var sc = fit.scatter || {};
    var xs = sc.x || [], ys = sc.y || [];
    if (!xs.length) {
      showEmpty('chart-fit-scatter', '暂无散点数据');
      return;
    }
    var pts = [];
    var len = Math.min(xs.length, ys.length);
    for (var i = 0; i < len; i++) pts.push([xs[i], ys[i]]);

    var lineData = [];
    if (sc.line && sc.line.x && sc.line.y && sc.line.x.length >= 2) {
      for (var j = 0; j < sc.line.x.length; j++) lineData.push([sc.line.x[j], sc.line.y[j]]);
    }

    draw('chart-fit-scatter', {
      backgroundColor: 'transparent',
      animationDuration: 350,
      grid: { left: 52, right: 20, top: 22, bottom: 44 },
      tooltip: tooltipBase({
        trigger: 'item',
        formatter: function (p) {
          if (p.seriesType === 'line') return '';
          return '<div style="color:' + C.text + ';font-size:10.5px">' +
            util.esc(fit.x_label || 'x') + '：<b style="font-family:monospace">' + fmt.ratio(p.value[0], 2) + '</b></div>' +
            '<div style="color:' + C.text + ';font-size:10.5px">' +
            util.esc(fit.y_label || 'y') + '：<b style="font-family:monospace">' + fmt.ratio(p.value[1], 2) + '</b></div>';
        }
      }),
      xAxis: valAxis({
        name: fit.x_label || '',
        nameLocation: 'middle',
        nameGap: 26,
        nameTextStyle: { color: C.dim, fontSize: 10 },
        scale: true,
        splitLine: { lineStyle: { color: C.split, type: 'dashed' } }
      }),
      yAxis: valAxis({
        name: fit.y_label || '',
        nameTextStyle: { color: C.dim, fontSize: 10, align: 'left' },
        scale: true
      }),
      series: [
        {
          name: '样本',
          type: 'scatter',
          data: pts,
          symbolSize: 5,
          itemStyle: { color: 'rgba(77,124,254,0.52)' },
          emphasis: { itemStyle: { color: C.accent } }
        },
        {
          name: '回归线',
          type: 'line',
          data: lineData,
          showSymbol: false,
          silent: true,
          lineStyle: { width: 1.8, color: C.up, type: 'solid' },
          itemStyle: { color: C.up }
        }
      ],
      markLine: markZero()
    });
  }

  /* ----------------------------- 3. 滚动相关 ----------------------------- */

  function drawRolling(dash) {
    var rc = (dash && dash.fit && dash.fit.rolling_corr) || {};
    var dates = rc.date || [];
    if (!dates.length) {
      showEmpty('chart-fit-roll', '暂无滚动相关数据');
      return;
    }
    draw('chart-fit-roll', {
      backgroundColor: 'transparent',
      animationDuration: 350,
      grid: { left: 50, right: 18, top: 26, bottom: 30 },
      legend: legendBase({ data: ['同期相关', '预测相关'] }),
      tooltip: tooltipBase({
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var out = '<div style="color:' + C.text + ';font-size:10.5px;margin-bottom:3px">' + ps[0].axisValue + '</div>';
          ps.forEach(function (p) {
            out += '<div style="display:flex;gap:8px;align-items:center">' + p.marker +
              '<span style="flex:1;color:' + C.text + '">' + p.seriesName + '</span>' +
              '<b style="font-family:monospace">' + (p.value === null || p.value === undefined ? '—' : fmt.signedRatio(p.value, 3)) + '</b></div>';
          });
          return out;
        }
      }),
      xAxis: catAxis(dates),
      yAxis: valAxis({
        name: '相关系数',
        nameTextStyle: { color: C.dim, fontSize: 10 },
        scale: true,
        axisLabel: { color: C.dim, fontSize: 10, formatter: function (v) { return v.toFixed(2); } }
      }),
      series: [
        {
          name: '同期相关',
          type: 'line',
          data: rc.contemporaneous || [],
          showSymbol: false,
          connectNulls: false,
          sampling: 'lttb',
          lineStyle: { width: 1.4, color: C.accent },
          itemStyle: { color: C.accent },
          markLine: markZero()
        },
        {
          name: '预测相关',
          type: 'line',
          data: rc.predictive || [],
          showSymbol: false,
          connectNulls: false,
          sampling: 'lttb',
          lineStyle: { width: 1.4, color: C.orange },
          itemStyle: { color: C.orange }
        }
      ]
    });
  }

  /* ----------------------------- 4. 领先滞后 ----------------------------- */

  function drawLag(dash) {
    var lags = (dash && dash.fit && dash.fit.lag_corr) || [];
    if (!lags.length) {
      showEmpty('chart-fit-lag', '暂无滞后相关数据');
      return;
    }
    var data = lags.map(function (d) {
      return {
        value: d.corr,
        itemStyle: { color: d.corr >= 0 ? 'rgba(246,70,93,0.78)' : 'rgba(14,203,129,0.78)' },
        n: d.n,
        lag: d.lag
      };
    });
    var labels = lags.map(function (d) {
      if (d.lag === 0) return 't';
      return d.lag > 0 ? 't+' + d.lag : 't' + d.lag;
    });
    draw('chart-fit-lag', {
      backgroundColor: 'transparent',
      animationDuration: 350,
      grid: { left: 52, right: 18, top: 24, bottom: 30 },
      tooltip: tooltipBase({
        type: 'axis',
        formatter: function (ps) {
          var p = ps[0];
          var d = lags[p.dataIndex] || {};
          var lagTxt = d.lag === 0 ? 't（当期）' : (d.lag > 0 ? 't+' + d.lag : 't' + d.lag);
          return '<div style="color:' + C.text + ';font-size:10.5px;margin-bottom:3px">' + util.esc(lagTxt) + '</div>' +
            '<div style="display:flex;gap:10px;align-items:center;color:' + C.text + ';font-size:10.5px">' +
            '<span style="flex:1">相关系数</span><b style="font-family:monospace;color:' + C.textStrong + '">' +
            fmt.signedRatio(d.corr, 3) + '</b></div>' +
            '<div style="display:flex;gap:10px;align-items:center;color:' + C.text + ';font-size:10.5px">' +
            '<span style="flex:1">样本 n</span><b style="font-family:monospace;color:' + C.textStrong + '">' +
            fmt.num(d.n) + '</b></div>';
        }
      }),
      xAxis: catAxis(labels, { boundaryGap: true }),
      yAxis: valAxis({
        name: '相关',
        nameTextStyle: { color: C.dim, fontSize: 10 },
        axisLabel: { color: C.dim, fontSize: 10, formatter: function (v) { return v.toFixed(2); } }
      }),
      series: [{
        name: '领先滞后相关',
        type: 'bar',
        data: data,
        barWidth: '42%',
        itemStyle: { borderRadius: [2, 2, 0, 0] },
        markLine: markZero()
      }]
    });
  }

  /* ----------------------------- 5. 分位胜率 ----------------------------- */

  function drawBuckets(dash) {
    var b = (dash && dash.buckets) || {};
    var rows = b.rows || [];
    if (!rows.length) {
      showEmpty('chart-buckets', '暂无分位分组数据');
      return;
    }
    var labels = rows.map(function (r) {
      var s = String(r.label || ('Q' + r.bucket));
      return s.replace(/^Q\d+\s*/, '');
    });
    var means = rows.map(function (r) {
      return {
        value: r.mean_ret_pct,
        itemStyle: { color: (r.mean_ret_pct || 0) >= 0 ? C.up : C.down }
      };
    });
    var wins = rows.map(function (r) { return r.win_rate; });

    draw('chart-buckets', {
      backgroundColor: 'transparent',
      animationDuration: 350,
      grid: { left: 54, right: 56, top: 30, bottom: 34 },
      legend: legendBase({ data: ['次日平均收益', '胜率'] }),
      tooltip: tooltipBase({
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var i = ps[0].dataIndex;
          var r = rows[i] || {};
          var line = function (k, v) {
            return '<div style="display:flex;gap:12px;align-items:center;color:' + C.text + ';font-size:10.5px">' +
              '<span style="flex:1">' + util.esc(k) + '</span>' +
              '<b style="font-family:monospace;color:' + C.textStrong + '">' + v + '</b></div>';
          };
          return '<div style="color:' + C.textStrong + ';font-size:11px;margin-bottom:4px">' +
            util.esc(r.label || ('Q' + r.bucket)) + '</div>' +
            line('区间', fmt.range(r.lo, r.hi, 2)) +
            line('样本 n', fmt.num(r.n)) +
            line('平均收益', fmt.signedPct(r.mean_ret_pct, 2)) +
            line('中位收益', fmt.signedPct(r.median_ret_pct, 2)) +
            line('胜率', fmt.pct(r.win_rate, 2)) +
            line('t 值', fmt.ratio(r.t_stat, 2)) +
            line('p 值', fmt.pvalue(r.p_value));
        }
      }),
      xAxis: catAxis(labels, { boundaryGap: true, axisLabel: { color: C.text, fontSize: 10.5, interval: 0, hideOverlap: false } }),
      yAxis: [
        valAxis({
          name: '平均收益 %',
          nameTextStyle: { color: C.dim, fontSize: 10 },
          axisLabel: { color: C.dim, fontSize: 10, formatter: function (v) { return v.toFixed(2); } }
        }),
        valAxis({
          name: '胜率 %',
          nameTextStyle: { color: C.dim, fontSize: 10 },
          position: 'right',
          min: 0,
          max: 100,
          splitLine: { show: false },
          axisLabel: { color: C.dim, fontSize: 10, formatter: function (v) { return v.toFixed(0); } }
        })
      ],
      series: [
        {
          name: '次日平均收益',
          type: 'bar',
          data: means,
          barWidth: '46%',
          itemStyle: { borderRadius: [3, 3, 0, 0] },
          label: {
            show: true,
            position: 'top',
            color: C.text,
            fontSize: 9.5,
            formatter: function (p) { return fmt.signedPct(p.value, 2); }
          },
          markLine: markZero()
        },
        {
          name: '胜率',
          type: 'line',
          yAxisIndex: 1,
          data: wins,
          symbol: 'circle',
          symbolSize: 6,
          lineStyle: { width: 1.8, color: C.warn },
          itemStyle: { color: C.warn },
          label: {
            show: true,
            position: 'bottom',
            color: C.warn,
            fontSize: 9.5,
            formatter: function (p) { return fmt.pct(p.value, 1); }
          }
        }
      ]
    });
  }

  /* ----------------------------- 6. 回测净值 ----------------------------- */

  function drawBacktest(dash) {
    var eq = (dash && dash.backtest && dash.backtest.equity) || {};
    var dates = eq.date || [];
    if (!dates.length) {
      showEmpty('chart-backtest', '暂无回测数据');
      return;
    }
    var nav = (eq.nav || []).map(function (v) { return v === null ? null : (v - 1) * 100; });
    var bh = (eq.nav_bh || []).map(function (v) { return v === null ? null : (v - 1) * 100; });

    draw('chart-backtest', {
      backgroundColor: 'transparent',
      animationDuration: 350,
      grid: { left: 56, right: 22, top: 28, bottom: 44 },
      legend: legendBase({ data: ['策略净值', '指数买入持有'] }),
      tooltip: tooltipBase({
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var out = '<div style="color:' + C.text + ';font-size:10.5px;margin-bottom:3px">' + ps[0].axisValue + '</div>';
          ps.forEach(function (p) {
            out += '<div style="display:flex;gap:8px;align-items:center">' + p.marker +
              '<span style="flex:1;color:' + C.text + '">' + p.seriesName + '</span>' +
              '<b style="font-family:monospace">' + (p.value === null || p.value === undefined ? '—' : fmt.signedPct(p.value, 2)) + '</b></div>';
          });
          return out;
        }
      }),
      xAxis: catAxis(dates),
      yAxis: valAxis({
        name: '累计收益 %',
        nameTextStyle: { color: C.dim, fontSize: 10 },
        axisLabel: { color: C.dim, fontSize: 10, formatter: function (v) { return v.toFixed(0) + '%'; } }
      }),
      dataZoom: zoomBase(true),
      series: [
        {
          name: '策略净值',
          type: 'line',
          data: nav,
          showSymbol: false,
          sampling: 'lttb',
          lineStyle: { width: 1.6, color: C.accent },
          itemStyle: { color: C.accent },
          areaStyle: {
            color: {
              type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: 'rgba(77,124,254,0.26)' },
                { offset: 1, color: 'rgba(77,124,254,0.01)' }
              ]
            }
          },
          markLine: markZero()
        },
        {
          name: '指数买入持有',
          type: 'line',
          data: bh,
          showSymbol: false,
          sampling: 'lttb',
          lineStyle: { width: 1.2, color: C.dim, opacity: 0.95 },
          itemStyle: { color: C.dim }
        }
      ]
    });
  }

  App.charts = {
    C: C,
    drawFitNet: drawFitNet,
    drawScatter: drawScatter,
    drawRolling: drawRolling,
    drawLag: drawLag,
    drawBuckets: drawBuckets,
    drawBacktest: drawBacktest,
    showEmpty: showEmpty,
    resizeAll: resizeAll,
    bindResize: bindResize,
    draw: draw,
    getChart: getChart
  };
})(window);
