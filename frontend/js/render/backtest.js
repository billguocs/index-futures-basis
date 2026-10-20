/* ==========================================================================
   frontend/js/render/backtest.js · ⑤ 分批回测（净持仓分位择时）
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;
  var fmt = util.fmt;
  var charts = App.charts;

  function render(dash) {
    var bt = dash && dash.backtest;
    var metricsEl = document.getElementById('btMetrics');
    var extraEl = document.getElementById('btExtra');
    var noteEl = document.getElementById('btNote');

    if (noteEl) {
      noteEl.textContent = bt
        ? '成本 ' + fmt.dec(bt.cost_bp, 1) + ' bp/次 · 样本 ' + fmt.num(bt.metrics && bt.metrics.n) + ' 日'
        : '—';
    }

    if (!bt || !bt.metrics) {
      charts.showEmpty('chart-backtest', '暂无回测数据');
      if (metricsEl) {
        metricsEl.innerHTML = util.emptyState(
          '暂无回测结果',
          '需要足够长的历史窗口才能构建择时回测，请扩大窗口或先同步数据。'
        );
      }
      if (extraEl) extraEl.innerHTML = '';
      return;
    }

    var m = bt.metrics;
    var tiles = [];
    tiles.push(util.tile('策略累计收益', fmt.signedFrac(m.cum_return, 2),
      '基准 ' + fmt.signedFrac(m.benchmark_cum, 2),
      { cls: util.signClass(m.cum_return) }));
    tiles.push(util.tile('年化收益', fmt.signedFrac(m.annual_return, 2),
      '按 252 交易日年化', { cls: util.signClass(m.annual_return) }));
    tiles.push(util.tile('最大回撤', fmt.signedFrac(m.max_drawdown, 2),
      m.max_drawdown != null ? (m.max_drawdown > -0.1 ? '回撤可控' : m.max_drawdown > -0.25 ? '回撤中等' : '回撤较大') : '',
      { cls: m.max_drawdown < 0 ? 'down' : '' }));
    tiles.push(util.tile('夏普比率', fmt.signedRatio(m.sharpe, 2),
      '无风险利率按 0 计', { cls: util.signClass(m.sharpe) }));
    if (metricsEl) metricsEl.innerHTML = tiles.join('');

    if (!extraEl) return;
    var rows = [
      ['累计收益（策略 / 买入持有）', fmt.signedFrac(m.cum_return, 2) + ' / ' + fmt.signedFrac(m.benchmark_cum, 2)],
      ['年化收益', fmt.signedFrac(m.annual_return, 2)],
      ['最大回撤', fmt.signedFrac(m.max_drawdown, 2)],
      ['夏普比率', fmt.signedRatio(m.sharpe, 2)],
      ['日胜率', fmt.pct(m.hit_rate, 1)],
      ['交易次数', fmt.num(m.trades)],
      ['持仓占比（暴露度）', fmt.frac(m.exposure, 1)],
      ['有效样本', fmt.num(m.n) + ' 日'],
      ['单边成本', fmt.dec(bt.cost_bp, 1) + ' bp']
    ].map(function (r) {
      return util.row([
        { text: r[0], cls: 'ta-l' },
        { text: r[1], cls: 'ta-r num' }
      ]);
    });

    var html = '<div class="table-wrap">' + util.table(
      [{ text: '指标', cls: 'ta-l' }, { text: '数值', cls: 'ta-r' }], rows) + '</div>';
    html += '<div class="rule-line" style="margin-top:12px">' +
      '<span>规则</span><code>' + util.esc(bt.rule || '—') + '</code></div>';
    html += '<ul class="note-list warn">' +
      '<li>回测使用滚动 252 日分位生成信号，信号在 t 日收盘后可知，按 t+1 收盘结算，已计入换仓成本。</li>' +
      '<li>净值曲线以 1 为起点（图中换算为累计收益 %），不含保证金利息、滑点与冲击成本。</li>' +
      '<li>样本内参数、单品种单窗口结果，存在过拟合与幸存偏差风险，不代表未来收益。</li>' +
      '</ul>';
    extraEl.innerHTML = html;

    charts.drawBacktest(dash);
  }

  App.render = App.render || {};
  App.render.backtest = render;
})(window);
