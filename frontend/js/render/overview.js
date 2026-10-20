/* ==========================================================================
   frontend/js/render/overview.js · ② 持仓总览（中信期货）+ 三品种横向对比
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;
  var fmt = util.fmt;

  var FALLBACK_VARIETIES = [
    { code: 'IF', name: '沪深300股指期货', color: '#4d7cfe' },
    { code: 'IH', name: '上证50股指期货', color: '#f0a020' },
    { code: 'IC', name: '中证500股指期货', color: '#a855f7' },
    { code: 'IM', name: '中证1000股指期货', color: '#2dd4bf' }
  ];

  function tileList(snap, dash) {
    var t = [];
    t.push(util.tile(
      (dash.index_name || '指数') + ' 收盘',
      fmt.dec(snap.index_close, 2),
      (snap.index_chg_pct > 0 ? '涨 ' : snap.index_chg_pct < 0 ? '跌 ' : '') + fmt.signedPct(snap.index_chg_pct, 2),
      { cls: util.signClass(snap.index_chg_pct) }
    ));
    t.push(util.tile(
      '主力合约',
      snap.main_contract || util.DASH,
      '收盘 ' + fmt.dec(snap.main_close, 2)
    ));
    t.push(util.tile(
      '基差（主力−指数）',
      fmt.signedNum(snap.basis),
      '基差率 ' + fmt.signedPct(snap.basis_pct, 3),
      { cls: util.signClass(snap.basis) }
    ));
    t.push(util.tile('中信多单', fmt.num(snap.citic_long), '手', {}));
    t.push(util.tile('中信空单', fmt.num(snap.citic_short), '手', {}));
    t.push(util.tile(
      '中信净持仓',
      fmt.signedNum(snap.citic_net),
      '手 · 占比 ' + fmt.signedPct(snap.citic_net_pct, 2),
      { cls: util.signClass(snap.citic_net) }
    ));
    t.push(util.tile(
      '净持仓占市场持仓',
      fmt.signedPct(snap.citic_net_pct, 2),
      'z 值 ' + fmt.signedRatio(snap.citic_net_z, 2)
    ));
    t.push(util.tile(
      '净持仓历史分位',
      fmt.pct(snap.citic_net_pctile, 1),
      '滚动 252 日 · 越低越偏空',
      { bar: snap.citic_net_pctile }
    ));
    t.push(util.tile(
      '净空单日变化',
      fmt.signedNum(snap.dnet),
      '占持仓 ' + fmt.signedPct(snap.dnet_pct_of_oi, 2),
      { cls: util.signClass(snap.dnet) }
    ));
    t.push(util.tile(
      '日变化 z 值',
      fmt.signedRatio(snap.dnet_z, 2),
      '分位 ' + fmt.pct(snap.dnet_pctile, 1),
      { bar: snap.dnet_pctile }
    ));
    t.push(util.tile('市场总持仓', fmt.num(snap.market_oi), '手'));
    t.push(util.tile(
      'Top20 净持仓',
      fmt.signedNum(snap.top20_net),
      '多 ' + fmt.num(snap.top20_long) + ' / 空 ' + fmt.num(snap.top20_short),
      { cls: util.signClass(snap.top20_net) }
    ));
    return t.join('');
  }

  function vcard(v, d, isActive) {
    var cls = 'vcard' + (isActive ? ' is-active' : '');
    var color = v.color || '#4d7cfe';
    var html = '<button type="button" class="' + cls + '" data-variety="' + util.esc(v.code) + '">';

    var s = d && d.snapshot;
    var g = d && d.guidance;
    if (!s) {
      cls += ' is-empty';
      html = '<button type="button" class="' + cls + '" data-variety="' + util.esc(v.code) + '">';
      html += '<div class="vcard-top">';
      html += '<span class="vcode" style="background:' + util.esc(color) + '">' + util.esc(v.code) + '</span>';
      html += '<span class="vname">' + util.esc(v.name || '') + '</span>';
      html += '</div>';
      html += '<div class="vnet">—</div>';
      html += '<div class="vcard-rows"><div>数据加载中或暂无持仓</div></div>';
      html += '</button>';
      return html;
    }

    html += '<div class="vcard-top">';
    html += '<span class="vcode" style="background:' + util.esc(color) + '">' + util.esc(v.code) + '</span>';
    html += '<span class="vname" title="' + util.esc(v.name || '') + '">' + util.esc(v.name || '') + '</span>';
    if (g && g.direction) {
      var chip = g.direction === '偏多' ? 'chip-up' : g.direction === '偏空' ? 'chip-down' : 'chip-flat';
      html += '<span class="chip ' + chip + '">' + util.esc(g.direction) + '</span>';
    }
    html += '</div>';

    html += '<div><span class="vnet ' + util.signClass(s.citic_net) + '">' + fmt.signedNum(s.citic_net) +
      '</span><span class="vunit">手 · 净持仓</span></div>';

    html += '<div class="vcard-rows">';
    html += '<div>占市场持仓 <b class="' + util.signClass(s.citic_net_pct) + '">' + fmt.signedPct(s.citic_net_pct, 2) + '</b></div>';
    html += '<div>历史分位 <b>' + fmt.pct(s.citic_net_pctile, 1) + '</b> · 日变化 <b class="' +
      util.signClass(s.dnet) + '">' + fmt.signedNum(s.dnet) + '</b></div>';
    html += '</div>';

    var w = util.clamp(s.citic_net_pctile, 0, 100);
    var barCls = w >= 80 ? 'is-high' : w <= 20 ? 'is-low' : '';
    html += '<div class="vbar"><span class="' + barCls + '" style="width:' + w.toFixed(1) + '%;background:' +
      util.esc(color) + '"></span></div>';
    html += '</button>';
    return html;
  }

  function render(dash, ctx) {
    ctx = ctx || {};
    var tilesEl = document.getElementById('overviewTiles');
    var cardsEl = document.getElementById('varietyCards');
    var dateEl = document.getElementById('oSnapshotDate');

    var snap = dash && dash.snapshot;
    if (tilesEl) {
      if (!snap) {
        tilesEl.innerHTML = util.emptyState(
          '暂无持仓数据',
          '当前品种在该窗口内没有可用快照，请先点击「立即更新数据」同步中金所排名与指数日线。',
          '立即更新数据',
          'overviewEmptySync'
        );
      } else {
        tilesEl.innerHTML = tileList(snap, dash);
      }
    }
    if (dateEl) {
      dateEl.textContent = snap && snap.date ? dash.member + ' · 数据日 ' + snap.date : '—';
    }

    if (!cardsEl) return;
    var varieties = (ctx.meta && ctx.meta.varieties && ctx.meta.varieties.length)
      ? ctx.meta.varieties : FALLBACK_VARIETIES;
    var allDash = ctx.allDash || {};
    var html = varieties.map(function (v) {
      return vcard(v, allDash[v.code], v.code === ctx.variety);
    }).join('');
    cardsEl.innerHTML = html;
  }

  App.render = App.render || {};
  App.render.overview = render;
})(window);
