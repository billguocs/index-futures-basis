/* ==========================================================================
   frontend/js/render/ranks.js · ⑥ 会员龙虎榜 + 会员净持仓对比
   ========================================================================== */
(function (global) {
  'use strict';

  var App = global.App || (global.App = {});
  var util = App.util;
  var fmt = util.fmt;

  var TITLES = {
    long: '多单持仓榜 Top20',
    short: '空单持仓榜 Top20',
    volume: '成交量榜 Top20'
  };

  function rankTable(kind, rows, highlight) {
    var head = [
      { text: '#', cls: 'ta-r' },
      { text: '会员', cls: 'ta-l' },
      { text: kind === 'volume' ? '成交量' : (kind === 'long' ? '多单持仓' : '空单持仓'), cls: 'ta-r' },
      { text: '增减', cls: 'ta-r' }
    ];
    var trs = (rows || []).map(function (r) {
      var isHl = highlight && r.member === highlight;
      var chg = r.change;
      var chgTxt = util.isNil(chg) ? util.DASH : fmt.signedNum(chg);
      return util.row([
        { text: String(r.rank), cls: 'ta-r num' },
        { text: util.esc(r.member), cls: 'ta-l' },
        { text: fmt.num(r.position), cls: 'ta-r num' },
        { text: chgTxt, cls: 'ta-r num ' + util.signClass(chg) }
      ], isHl ? 'is-hl' : '');
    });
    return util.table(head, trs, { empty: '暂无龙虎榜数据' });
  }

  function renderHighlight(ranks, member) {
    var el = document.getElementById('rankHighlight');
    if (!el) return;
    var hr = ranks && ranks.highlight_row;
    if (!hr) {
      el.innerHTML = '<div class="empty-state"><div class="empty-title">暂无重点会员数据</div>' +
        '<div class="empty-hint">该交易日没有 ' + util.esc(member || '中信期货') + ' 的排名记录。</div></div>';
      return;
    }
    var html = '<div class="rank-highlight">';
    html += '<div class="rh-name">' + util.esc(hr.member || member || '—') +
      ' <span class="chip chip-accent">当前会员</span></div>';
    html += '<div class="rh-item">多单<b class="num up">' + fmt.num(hr.long) + '</b></div>';
    html += '<div class="rh-item">空单<b class="num down">' + fmt.num(hr.short) + '</b></div>';
    html += '<div class="rh-item">净持仓<b class="num ' + util.signClass(hr.net) + '">' +
      fmt.signedNum(hr.net) + '</b></div>';
    if (ranks.date) html += '<div class="rh-item">数据日<b class="num">' + util.esc(ranks.date) + '</b></div>';
    html += '</div>';
    el.innerHTML = html;
  }

  function renderMembers(rows, loading, member) {
    var el = document.getElementById('rankMembers');
    if (!el) return;
    if (loading) {
      el.innerHTML = util.skeleton('会员净持仓加载中…');
      return;
    }
    if (!rows || !rows.length) {
      el.innerHTML = '<div class="empty-state"><div class="empty-title">暂无会员对比数据</div>' +
        '<div class="empty-hint">' + util.esc(member || '') +
        ' · 该窗口内可比较会员不足。</div></div>';
      return;
    }
    var trs = rows.map(function (r) {
      var isHl = member && r.member === member;
      return util.row([
        { text: String(r.rank), cls: 'ta-r num' },
        { text: util.esc(r.member), cls: 'ta-l' },
        { text: fmt.signedNum(r.net_latest), cls: 'ta-r num ' + util.signClass(r.net_latest) },
        { text: fmt.signedPct(r.net_pct_latest, 2), cls: 'ta-r num ' + util.signClass(r.net_pct_latest) },
        { text: fmt.signedNum(r.net_avg20), cls: 'ta-r num ' + util.signClass(r.net_avg20) },
        { text: fmt.signedNum(r.net_change20), cls: 'ta-r num ' + util.signClass(r.net_change20) },
        { text: fmt.signedRatio(r.pred_corr, 3), cls: 'ta-r num ' + util.signClass(r.pred_corr) },
        { text: fmt.num(r.n), cls: 'ta-r num' }
      ], isHl ? 'is-hl' : '');
    });
    el.innerHTML = util.table([
      { text: '排名', cls: 'ta-r' },
      { text: '会员', cls: 'ta-l' },
      { text: '最新净持仓', cls: 'ta-r' },
      { text: '占持仓 %', cls: 'ta-r' },
      { text: '20 日均净持仓', cls: 'ta-r' },
      { text: '20 日变化', cls: 'ta-r' },
      { text: '预测相关', cls: 'ta-r' },
      { text: '样本 n', cls: 'ta-r' }
    ], trs, { empty: '暂无会员对比数据' });
  }

  function render(dash, ctx) {
    ctx = ctx || {};
    var ranks = (ctx.ranks && ctx.ranks.ok !== false) ? ctx.ranks : (dash && dash.ranks);
    var member = (dash && dash.member) || ctx.member || '中信期货';

    var noteEl = document.getElementById('rankNote');
    if (noteEl) {
      if (!ranks) {
        noteEl.textContent = '—';
      } else {
        var parts = [];
        if (ranks.date) parts.push('数据日 ' + ranks.date);
        if (ranks.variety) parts.push(ranks.variety);
        if (ranks.contracts && ranks.contracts.length) parts.push('合约 ' + ranks.contracts.join(' / '));
        parts.push('按会员净持仓排名');
        noteEl.textContent = parts.join(' · ');
      }
    }

    renderHighlight(ranks, member);

    var box = document.getElementById('rankTables');
    if (box) {
      if (!ranks) {
        box.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="empty-title">暂无龙虎榜数据</div>' +
          '<div class="empty-hint">请先点击「立即更新数据」同步中金所会员排名。</div></div>';
      } else {
        var hl = ranks.highlight || member;
        box.innerHTML = ['long', 'short', 'volume'].map(function (kind) {
          return '<div class="rank-col"><h4 class="list-title">' + TITLES[kind] + '</h4>' +
            '<div class="table-wrap">' + rankTable(kind, ranks[kind], hl) + '</div></div>';
        }).join('');
      }
    }

    renderMembers(ctx.membersRows, ctx.membersLoading, member);
  }

  App.render = App.render || {};
  App.render.ranks = render;
})(window);
