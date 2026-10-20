/* ==========================================================================
   frontend/js/render/guidance.js · ① 次日研判卡
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;
  var fmt = util.fmt;

  function dirClass(direction) {
    if (direction === '偏多') return 'dir-up';
    if (direction === '偏空') return 'dir-down';
    return 'dir-flat';
  }

  function render(dash, ctx) {
    var body = document.getElementById('guidanceBody');
    var target = document.getElementById('gTarget');
    if (!body) return;

    if (!dash || dash.empty || !dash.guidance) {
      if (target) target.textContent = '—';
      body.innerHTML = util.emptyState(
        '暂无研判数据',
        dash && dash.empty
          ? '当前品种在该窗口内没有可用样本，请先点击「立即更新数据」同步行情与持仓。'
          : '未能获取 dashboard 数据，请确认接口可用或点击右上角「立即更新数据」。',
        '立即更新数据',
        'guidEmptySync'
      );
      return;
    }

    var g = dash.guidance || {};
    var snap = dash.snapshot || {};
    var similar = g.similar || {};
    var ci = similar.up_prob_ci || [];

    if (target) {
      target.textContent = '目标交易日 ' + (g.for_date || '—') +
        ' · 置信度 ' + (g.confidence || '—');
    }

    var html = '<div class="guid-grid">';

    /* ---------------- 左：方向与概率 ---------------- */
    html += '<div class="guid-hero">';
    html += '<div>';
    html += '<div class="dir-badge ' + dirClass(g.direction) + '">' + util.esc(g.direction || '—') + '</div>';
    html += '<div class="dir-sub">基于 ' + util.esc(g.date || '—') + ' 收盘数据 · 置信度 <b>' +
      util.esc(g.confidence || '—') + '</b> · 目标日 <b>' + util.esc(g.for_date || '—') + '</b></div>';
    html += '</div>';

    html += '<div class="hero-kpis">';
    html += '<div class="hero-kpi"><span>综合评分</span><b class="' + util.signClass(g.score) + '">' +
      fmt.signedRatio(g.score, 3) + '</b></div>';
    html += '<div class="hero-kpi"><span>上涨概率</span><b>' + fmt.pct(g.up_prob, 2) + '</b></div>';
    html += '<div class="hero-kpi"><span>预期次日收益</span><b class="' + util.signClass(g.expected_ret_pct) + '">' +
      fmt.signedPct(g.expected_ret_pct, 2) + '</b></div>';
    html += '<div class="hero-kpi"><span>同分位样本</span><b>' + fmt.num(g.sample_n) + '</b></div>';
    html += '</div>';

    // 上涨概率条（50% 中位线）
    var p = util.clamp(g.up_prob, 0, 100);
    html += '<div>';
    html += '<div class="prob-bar"><span class="prob-fill" style="width:' + p.toFixed(1) + '%"></span>' +
      '<span class="prob-mid" style="left:50%"></span></div>';
    html += '<div class="prob-caption" style="margin-top:5px">上涨概率 ' + fmt.pct(g.up_prob, 2) +
      ' · 基准 50%' + (ci.length === 2
        ? ' · 95% 区间 ' + fmt.pct(ci[0], 1) + ' ~ ' + fmt.pct(ci[1], 1)
        : '') + '</div>';
    html += '</div>';

    // 当日快照摘要
    html += '<div class="mini-tiles" style="margin-bottom:0">';
    html += '<div class="mini-tile"><span>指数收盘</span><b>' + fmt.dec(snap.index_close, 2) + '</b></div>';
    html += '<div class="mini-tile"><span>当日涨跌</span><b class="' + util.signClass(snap.index_chg_pct) + '">' +
      fmt.signedPct(snap.index_chg_pct, 2) + '</b></div>';
    html += '<div class="mini-tile"><span>中信净持仓</span><b class="' + util.signClass(snap.citic_net) + '">' +
      fmt.signedNum(snap.citic_net) + '</b></div>';
    html += '<div class="mini-tile"><span>净空单日变化</span><b class="' + util.signClass(snap.dnet) + '">' +
      fmt.signedNum(snap.dnet) + '</b></div>';
    html += '</div>';
    html += '</div>'; // /guid-hero

    /* ---------------- 右：驱动/证据/风险/相似日 ---------------- */
    html += '<div class="guid-list">';

    var drivers = g.drivers || [];
    html += '<div class="list-title">驱动因素</div>';
    if (drivers.length) {
      html += '<ul class="drivers">';
      drivers.forEach(function (d) {
        var w = util.clamp(d.weight, 0, 1) * 100;
        html += '<li class="driver">';
        html += '<div class="driver-top">';
        html += '<span class="driver-name">' + util.esc(d.name || d.key || '') + '</span>';
        html += '<span class="chip ' + util.impactClass(d.impact) + '">' + util.esc(d.impact || '—') + '</span>';
        html += '<span class="driver-val ' + util.signClass(d.value) + '">' + fmt.signedRatio(d.value, 3) + '</span>';
        html += '<span class="driver-weight">权重 ' + fmt.pct(w, 0) + '</span>';
        html += '</div>';
        if (d.desc) html += '<div class="driver-desc">' + util.esc(d.desc) + '</div>';
        html += '<div class="weight-bar"><span style="width:' + w.toFixed(1) + '%"></span></div>';
        html += '</li>';
      });
      html += '</ul>';
    } else {
      html += '<div class="muted">暂无驱动因素</div>';
    }

    var evidence = g.evidence || [];
    if (evidence.length) {
      html += '<div class="list-title">证据</div><ul class="note-list">';
      evidence.forEach(function (e) { html += '<li>' + util.esc(e) + '</li>'; });
      html += '</ul>';
    }

    var caveats = g.caveats || [];
    if (caveats.length) {
      html += '<div class="list-title">风险提示</div><ul class="note-list warn">';
      caveats.forEach(function (c) { html += '<li>' + util.esc(c) + '</li>'; });
      html += '</ul>';
    }

    // 历史相似日
    html += '<div class="similar-block">';
    html += '<div class="list-title">历史相似日统计</div>';
    html += '<div class="mini-tiles">';
    html += '<div class="mini-tile"><span>相似样本</span><b>' + fmt.num(similar.n) + '</b></div>';
    html += '<div class="mini-tile"><span>平均次日收益</span><b class="' + util.signClass(similar.mean_ret_pct) + '">' +
      fmt.signedPct(similar.mean_ret_pct, 2) + '</b></div>';
    html += '<div class="mini-tile"><span>上涨占比</span><b>' + fmt.pct(similar.win_rate, 2) + '</b></div>';
    html += '<div class="mini-tile"><span>95% 概率区间</span><b>' +
      (ci.length === 2 ? fmt.pct(ci[0], 1) + ' ~ ' + fmt.pct(ci[1], 1) : '—') + '</b></div>';
    html += '</div>';

    var examples = similar.last_examples || [];
    if (examples.length) {
      var rows = examples.map(function (e) {
        return util.row([
          { text: e.date || '—', cls: 'ta-l num' },
          { text: fmt.signedPct(e.next_ret_pct, 2), cls: 'ta-r num ' + util.signClass(e.next_ret_pct) }
        ]);
      });
      html += '<div class="table-wrap" style="margin-top:8px">' + util.table(
        [{ text: '相似日', cls: 'ta-l' }, { text: '次日收益', cls: 'ta-r' }],
        rows,
        { empty: '暂无相似日样本' }
      ) + '</div>';
    }
    html += '</div>';

    html += '</div>'; // /guid-list
    html += '</div>'; // /guid-grid

    body.innerHTML = html;
  }

  App.render = App.render || {};
  App.render.guidance = render;
})(window);
