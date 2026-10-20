/* ==========================================================================
   frontend/js/render/buckets.js · ④ 分位胜率（五分位分组）
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;
  var fmt = util.fmt;
  var charts = App.charts;

  /** 主因子 → snapshot 字段名 */
  var FACTOR_SNAP_MAP = {
    dnet_z: 'dnet_z',
    net_z: 'citic_net_z',
    net_pctile: 'citic_net_pctile'
  };

  function currentFactorValue(dash) {
    var key = (dash.fit && dash.fit.primary) || 'dnet_z';
    var snapKey = FACTOR_SNAP_MAP[key] || key;
    var snap = dash.snapshot || {};
    var v = snap[snapKey];
    return typeof v === 'number' && isFinite(v) ? v : null;
  }

  function findCurrentRow(rows, value) {
    if (value === null) return null;
    for (var i = 0; i < rows.length; i++) {
      var lo = rows[i].lo, hi = rows[i].hi;
      var okLo = (lo === null || lo === undefined) || value >= lo;
      var okHi = (hi === null || hi === undefined) || value <= hi;
      if (okLo && okHi) return rows[i];
    }
    return null;
  }

  function render(dash) {
    var body = document.getElementById('bucketBody');
    var note = document.getElementById('bucketNote');
    var b = dash && dash.buckets;

    if (note) {
      note.textContent = b
        ? 'IC = ' + fmt.signedRatio(b.ic, 3) + '（p = ' + fmt.pvalue(b.ic_p) + '） · ' +
          fmt.num(b.n_buckets) + ' 组'
        : '—';
    }

    if (!b || !b.rows || !b.rows.length) {
      charts.showEmpty('chart-buckets', '暂无分位分组数据');
      if (body) {
        body.innerHTML = util.emptyState(
          '暂无分位胜率数据',
          '该窗口内有效样本不足，无法按分位分组。请扩大时间窗口或先同步更多历史数据。'
        );
      }
      return;
    }

    var curVal = currentFactorValue(dash);
    var curRow = findCurrentRow(b.rows, curVal);

    var rows = b.rows.map(function (r) {
      var isCur = curRow && curRow.bucket === r.bucket;
      return util.row([
        {
          text: '<span class="' +
            (isCur ? 'chip chip-accent' : '') + '">Q' + util.esc(r.bucket) + '</span> ' +
            util.esc(r.label || '') + (isCur ? ' <span class="chip chip-accent">当前</span>' : ''),
          raw: true,
          cls: 'ta-l'
        },
        { text: fmt.range(r.lo, r.hi, 2), cls: 'ta-r num' },
        { text: fmt.num(r.n), cls: 'ta-r num' },
        {
          text: fmt.signedPct(r.mean_ret_pct, 2),
          cls: 'ta-r num ' + util.signClass(r.mean_ret_pct)
        },
        {
          text: fmt.signedPct(r.median_ret_pct, 2),
          cls: 'ta-r num ' + util.signClass(r.median_ret_pct)
        },
        { text: fmt.pct(r.win_rate, 2), cls: 'ta-r num' },
        { text: fmt.signedRatio(r.t_stat, 2), cls: 'ta-r num' },
        { text: fmt.pvalue(r.p_value), cls: 'ta-r num' }
      ], isCur ? 'is-current' : '');
    });

    var html = '';
    if (curVal !== null) {
      html += '<div class="rule-line" style="margin-top:12px;margin-bottom:8px">';
      html += '<span>当前主因子值</span><code>' + fmt.signedRatio(curVal, 3) + '</code>';
      html += '<span>落在</span><code>Q' + (curRow ? curRow.bucket : '—') + ' ' +
        util.esc(curRow ? curRow.label : '（超出分组区间）') + '</code>';
      if (curRow) {
        html += '<span>该组历史次日平均收益</span><code class="' +
          (curRow.mean_ret_pct >= 0 ? 'up' : 'down') + '">' + fmt.signedPct(curRow.mean_ret_pct, 2) + '</code>';
        html += '<span>胜率</span><code>' + fmt.pct(curRow.win_rate, 1) + '</code>';
      }
      html += '</div>';
    }
    html += '<div class="table-wrap">' + util.table([
      { text: '分位组', cls: 'ta-l' },
      { text: '因子区间', cls: 'ta-r' },
      { text: '样本 n', cls: 'ta-r' },
      { text: '次日平均收益', cls: 'ta-r' },
      { text: '中位收益', cls: 'ta-r' },
      { text: '胜率', cls: 'ta-r' },
      { text: 't 值', cls: 'ta-r' },
      { text: 'p 值', cls: 'ta-r' }
    ], rows) + '</div>';

    html += '<ul class="note-list" style="margin-top:10px">' +
      '<li>分组基于当前窗口全样本（含后验信息），仅作描述性统计，不代表可交易的实时信号。</li>' +
      '<li>Q1 = 净空单日变化最负（空头加仓最多），Q' + fmt.num(b.n_buckets) + ' = 最正（空头减仓/多头加仓最多）。</li>' +
      '<li>“IC”为因子值与次日收益的秩相关，绝对值 &lt; 0.03 通常视为几乎无预测力。</li>' +
      '</ul>';

    if (body) body.innerHTML = html;
    charts.drawBuckets(dash);
  }

  App.render = App.render || {};
  App.render.buckets = render;
})(window);
