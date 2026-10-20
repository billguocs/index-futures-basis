/* ==========================================================================
   frontend/js/render/fit.js · ③ 拟合分析（净持仓 ↔ 指数）
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;
  var fmt = util.fmt;
  var charts = App.charts;

  function primaryModel(fit) {
    var models = (fit && fit.models) || [];
    var key = fit && fit.primary;
    for (var i = 0; i < models.length; i++) {
      if (models[i].key === key) return models[i];
    }
    return models[0] || null;
  }

  function renderStats(fit) {
    var el = document.getElementById('fitStats');
    if (!el) return;
    if (!fit) {
      el.innerHTML = util.emptyState('暂无拟合数据', '请先同步数据后再查看净持仓与指数的拟合结果。');
      return;
    }
    var pm = primaryModel(fit);
    var t = [];
    t.push(util.tile('同期相关', fmt.signedRatio(fit.corr_contemporaneous, 3),
      '净持仓 vs 指数同日', { cls: util.signClass(fit.corr_contemporaneous) }));
    t.push(util.tile('预测相关', fmt.signedRatio(fit.corr_predictive, 3),
      '因子 → 次日收益', { cls: util.signClass(fit.corr_predictive) }));
    t.push(util.tile('主因子', pm ? (pm.name || pm.key) : '—',
      pm ? '相关 ' + fmt.signedRatio(pm.corr, 3) : ''));
    t.push(util.tile('R²（主因子）', pm ? fmt.ratio(pm.r2, 4) : util.DASH,
      '解释力' + (pm && pm.r2 != null ? (pm.r2 < 0.01 ? '很弱' : pm.r2 < 0.05 ? '偏弱' : '可参考') : '')));
    t.push(util.tile('p 值（主因子）', pm ? fmt.pvalue(pm.p_value) : util.DASH,
      pm ? (pm.significant ? '达到 5% 显著' : '未达 5% 显著') : '',
      { cls: pm && pm.significant ? 'up' : '' }));
    t.push(util.tile('有效样本', fmt.num(fit.n),
      (fit.sample && fit.sample.start ? fit.sample.start + ' ~ ' + fit.sample.end : '')));
    el.innerHTML = t.join('');
  }

  function renderModels(fit) {
    var el = document.getElementById('fitModels');
    if (!el) return;
    if (!fit || !fit.models || !fit.models.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-title">暂无回归结果</div>' +
        '<div class="empty-hint">样本不足或该窗口内无有效因子。</div></div>';
      return;
    }
    var rows = fit.models.map(function (m) {
      var isPrimary = m.key === fit.primary;
      var nameHTML = util.esc(m.name || m.key) +
        (isPrimary ? ' <span class="chip chip-accent">主因子</span>' : '');
      var sigHTML = m.significant
        ? '<span class="chip chip-up">显著</span>'
        : '<span class="chip chip-flat">不显著</span>';
      return util.row([
        { text: nameHTML, raw: true, cls: 'ta-l' },
        { text: fmt.num(m.n), cls: 'ta-r num' },
        { text: fmt.signedRatio(m.corr, 3), cls: 'ta-r num ' + util.signClass(m.corr) },
        { text: fmt.signedRatio(m.slope, 4), cls: 'ta-r num' },
        { text: fmt.signedRatio(m.intercept, 4), cls: 'ta-r num' },
        { text: fmt.ratio(m.r2, 4), cls: 'ta-r num' },
        { text: fmt.signedRatio(m.t_stat, 2), cls: 'ta-r num' },
        { text: fmt.pvalue(m.p_value), cls: 'ta-r num' },
        { text: sigHTML, raw: true, cls: 'ta-r' }
      ], isPrimary ? 'is-current' : '');
    });
    el.innerHTML = util.table([
      { text: '因子', cls: 'ta-l' }, { text: '样本 n', cls: 'ta-r' },
      { text: '相关 r', cls: 'ta-r' }, { text: '斜率', cls: 'ta-r' },
      { text: '截距', cls: 'ta-r' }, { text: 'R²', cls: 'ta-r' },
      { text: 't 值', cls: 'ta-r' }, { text: 'p 值', cls: 'ta-r' },
      { text: '结论', cls: 'ta-r' }
    ], rows, { empty: '暂无回归结果' });
  }

  function renderStability(fit) {
    var el = document.getElementById('fitStability');
    if (!el) return;
    var years = (fit && fit.stability && fit.stability.years) || [];
    if (!years.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-title">暂无年度稳定性数据</div></div>';
      return;
    }
    var rows = years.map(function (y) {
      return util.row([
        { text: String(y.year), cls: 'ta-l num' },
        { text: fmt.signedRatio(y.corr_predictive, 3), cls: 'ta-r num ' + util.signClass(y.corr_predictive) },
        { text: fmt.num(y.n), cls: 'ta-r num' }
      ]);
    });
    el.innerHTML = util.table(
      [{ text: '年度', cls: 'ta-l' }, { text: '预测相关', cls: 'ta-r' }, { text: '样本 n', cls: 'ta-r' }],
      rows,
      { empty: '暂无年度稳定性数据' }
    );
  }

  function render(dash, ctx) {
    var fit = dash && dash.fit;
    var note = document.getElementById('fitNote');
    if (note) {
      note.textContent = fit && fit.sample
        ? '主因子 ' + (fit.primary || '—') + ' · 样本 ' + (fit.sample.start || '') +
          ' ~ ' + (fit.sample.end || '') + ' · n=' + fmt.num(fit.n)
        : '—';
    }

    renderStats(fit);
    renderModels(fit);
    renderStability(fit);

    if (!fit) {
      charts.showEmpty('chart-fit-net', '暂无数据');
      charts.showEmpty('chart-fit-scatter', '暂无数据');
      charts.showEmpty('chart-fit-roll', '暂无数据');
      charts.showEmpty('chart-fit-lag', '暂无数据');
      return;
    }
    charts.drawFitNet(dash);
    charts.drawScatter(dash);
    charts.drawRolling(dash);
    charts.drawLag(dash);
  }

  App.render = App.render || {};
  App.render.fit = render;
})(window);
