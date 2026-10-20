/**
 * mock 数据生成器（仅用于前端开发，后端就绪后可删除或保留）
 *
 * 用法：node frontend/mock/generate.js
 *
 * 生成文件（结构严格对齐 contract/API_CONTRACT.md 第 5 节）：
 *   mock/meta.json            -> GET /api/meta
 *   mock/dashboard_IF.json    -> GET /api/dashboard?variety=IF
 *   mock/dashboard_IH.json
 *   mock/dashboard_IC.json
 *   mock/dashboard_IM.json
 *   mock/ranks_IF.json        -> GET /api/ranks?variety=IF
 *   mock/ranks_IH.json / _IC / _IM
 *   mock/members_IF.json      -> GET /api/members?variety=IF
 *   mock/members_IH.json / _IC / _IM
 *
 * 数据是「编造的」：但内部统计自洽（滚动 z、分位、OLS t/p、回测都由同一条净持仓序列真实算出），
 * 因此前端所有图表、格式化、空值降级路径都能被真实数据驱动到。
 */
'use strict';

const fs = require('fs');
const path = require('path');

const OUT_DIR = __dirname;
const TODAY = '2026-10-20';
const START = '2022-07-28';
const MEMBER_HL = '中信期货';

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gauss(rand) {
  const u = Math.max(rand(), 1e-12);
  const v = rand();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function round(v, d) {
  if (v === null || v === undefined || !isFinite(v)) return null;
  const p = Math.pow(10, d === undefined ? 2 : d);
  return Math.round(v * p) / p;
}

function tradingDays(startISO, endISO) {
  const out = [];
  const d = new Date(startISO + 'T00:00:00Z');
  const end = new Date(endISO + 'T00:00:00Z');
  while (d <= end) {
    const wd = d.getUTCDay();
    if (wd !== 0 && wd !== 6) out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

function nextWeekday(iso) {
  const d = new Date(iso + 'T00:00:00Z');
  do {
    d.setUTCDate(d.getUTCDate() + 1);
  } while (d.getUTCDay() === 0 || d.getUTCDay() === 6);
  return d.toISOString().slice(0, 10);
}

function mean(a) {
  if (!a.length) return null;
  return a.reduce((s, v) => s + v, 0) / a.length;
}

function median(a) {
  if (!a.length) return null;
  const s = a.slice().sort((x, y) => x - y);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function stdev(a) {
  if (a.length < 2) return null;
  const m = mean(a);
  return Math.sqrt(a.reduce((s, v) => s + (v - m) * (v - m), 0) / (a.length - 1));
}

/** 正态 CDF（Abramowitz-Stegun 7.1.26 近似，误差 < 1.5e-7） */
function phi(z) {
  const x = Math.abs(z);
  const t = 1 / (1 + 0.2316419 * x);
  const poly =
    t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  const p = 1 - (Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI)) * poly;
  return z >= 0 ? p : 1 - p;
}

function pValueFromT(t, n) {
  if (t === null || n === null || n < 3) return null;
  return 2 * (1 - phi(Math.abs(t)));
}

/** 顺序统计的 Pearson 相关 */
function corr(xs, ys) {
  const pairs = [];
  for (let i = 0; i < xs.length; i++) {
    const x = xs[i];
    const y = ys[i];
    if (x === null || y === null || x === undefined || y === undefined) continue;
    if (!isFinite(x) || !isFinite(y)) continue;
    pairs.push([x, y]);
  }
  const n = pairs.length;
  if (n < 3) return { corr: null, n: n };
  const mx = mean(pairs.map((p) => p[0]));
  const my = mean(pairs.map((p) => p[1]));
  let sxy = 0;
  let sxx = 0;
  let syy = 0;
  for (const [x, y] of pairs) {
    sxy += (x - mx) * (y - my);
    sxx += (x - mx) * (x - mx);
    syy += (y - my) * (y - my);
  }
  if (sxx <= 0 || syy <= 0) return { corr: null, n: n };
  return { corr: sxy / Math.sqrt(sxx * syy), n: n };
}

function spearman(xs, ys) {
  const pairs = [];
  for (let i = 0; i < xs.length; i++) {
    if (xs[i] === null || ys[i] === null) continue;
    pairs.push([xs[i], ys[i]]);
  }
  if (pairs.length < 3) return { ic: null, n: pairs.length };
  const rank = (idx) => {
    const sorted = pairs.map((p, i) => [p[idx], i]).sort((a, b) => a[0] - b[0]);
    const r = new Array(pairs.length);
    for (let i = 0; i < sorted.length; i++) r[sorted[i][1]] = i + 1;
    return r;
  };
  const rx = rank(0);
  const ry = rank(1);
  return { ic: corr(rx, ry).corr, n: pairs.length };
}

/** 经典 OLS：y = a + b x，返回 slope/intercept/r2/t_stat/p_value/n */
function ols(xs, ys) {
  const pairs = [];
  for (let i = 0; i < xs.length; i++) {
    const x = xs[i];
    const y = ys[i];
    if (x === null || y === null || x === undefined || y === undefined) continue;
    if (!isFinite(x) || !isFinite(y)) continue;
    pairs.push([x, y]);
  }
  const n = pairs.length;
  if (n < 5) {
    return { n: n, slope: null, intercept: null, r2: null, t_stat: null, p_value: null, corr: null, significant: false };
  }
  const mx = mean(pairs.map((p) => p[0]));
  const my = mean(pairs.map((p) => p[1]));
  let sxx = 0;
  let sxy = 0;
  let syy = 0;
  for (const [x, y] of pairs) {
    sxx += (x - mx) * (x - mx);
    sxy += (x - mx) * (y - my);
    syy += (y - my) * (y - my);
  }
  if (sxx <= 0 || syy <= 0) {
    return { n: n, slope: null, intercept: null, r2: null, t_stat: null, p_value: null, corr: null, significant: false };
  }
  const slope = sxy / sxx;
  const intercept = my - slope * mx;
  let rss = 0;
  for (const [x, y] of pairs) {
    const e = y - (intercept + slope * x);
    rss += e * e;
  }
  const sigma2 = rss / (n - 2);
  const se = Math.sqrt(sigma2 / sxx);
  const t = se > 0 ? slope / se : null;
  const p = t === null ? null : pValueFromT(t, n);
  return {
    n: n,
    slope: slope,
    intercept: intercept,
    r2: 1 - rss / syy,
    t_stat: t,
    p_value: p,
    corr: sxy / Math.sqrt(sxx * syy),
    significant: p !== null && p < 0.05,
  };
}

/** 滚动 z-score（win=252, min=60） */
function rollingZ(arr, win, minP) {
  const out = new Array(arr.length).fill(null);
  for (let i = 0; i < arr.length; i++) {
    const from = Math.max(0, i - win + 1);
    const slice = [];
    for (let k = from; k <= i; k++) if (arr[k] !== null) slice.push(arr[k]);
    if (slice.length < minP) continue;
    const m = mean(slice);
    const s = stdev(slice);
    if (s === null || s <= 0) continue;
    out[i] = (arr[i] - m) / s;
  }
  return out;
}

/** 滚动分位（百分数 0-100，win 内 <= 当前值的比例） */
function rollingPctile(arr, win, minP) {
  const out = new Array(arr.length).fill(null);
  for (let i = 0; i < arr.length; i++) {
    const from = Math.max(0, i - win + 1);
    const slice = [];
    for (let k = from; k <= i; k++) if (arr[k] !== null) slice.push(arr[k]);
    if (slice.length < minP) continue;
    let le = 0;
    for (const v of slice) if (v <= arr[i]) le++;
    out[i] = (100 * le) / slice.length;
  }
  return out;
}

/** 滚动相关系数（win 日，min = win/2） */
function rollingCorr(xs, ys, win) {
  const out = new Array(xs.length).fill(null);
  const minP = Math.max(10, Math.floor(win / 2));
  for (let i = 0; i < xs.length; i++) {
    const from = Math.max(0, i - win + 1);
    const X = [];
    const Y = [];
    for (let k = from; k <= i; k++) {
      if (xs[k] === null || ys[k] === null) continue;
      X.push(xs[k]);
      Y.push(ys[k]);
    }
    if (X.length < minP) continue;
    out[i] = corr(X, Y).corr;
  }
  return out;
}

function sum(a) {
  return a.reduce((s, v) => s + v, 0);
}

function pick(obj, fallback) {
  return obj === undefined || obj === null ? fallback : obj;
}

/* ------------------------------------------------------------------ */
/* 品种参数                                                            */
/* ------------------------------------------------------------------ */

const VARIETIES = [
  {
    code: 'IF',
    name: '沪深300股指期货',
    index_code: '000300',
    index_name: '沪深300',
    multiplier: 300,
    color: '#4d7cfe',
    seed: 20261020,
    endClose: 4152.11,
    oi: 790000,
    longShare: 0.0907,
    shortShare: 0.2148,
    contracts: ['IF2611', 'IF2612', 'IF2703'],
    main: 'IF2612',
    corrInject: 0.00072,
    netCenterRatio: -0.124,
  },
  {
    code: 'IH',
    name: '上证50股指期货',
    index_code: '000016',
    index_name: '上证50',
    multiplier: 300,
    color: '#f0a020',
    seed: 20261021,
    endClose: 2901.35,
    oi: 380000,
    longShare: 0.105,
    shortShare: 0.19,
    contracts: ['IH2611', 'IH2612', 'IH2703'],
    main: 'IH2612',
    corrInject: 0.00061,
    netCenterRatio: -0.085,
  },
  {
    code: 'IC',
    name: '中证500股指期货',
    index_code: '000905',
    index_name: '中证500',
    multiplier: 200,
    color: '#a855f7',
    seed: 20261022,
    endClose: 6218.44,
    oi: 520000,
    longShare: 0.13,
    shortShare: 0.192,
    contracts: ['IC2611', 'IC2612', 'IC2703'],
    main: 'IC2612',
    corrInject: 0.00052,
    netCenterRatio: -0.062,
    },
  {
    code: 'IM',
    name: '中证1000股指期货',
    index_code: '000852',
    index_name: '中证1000',
    multiplier: 200,
    color: '#2dd4bf',
    seed: 20261023,
    endClose: 6852.09,
    oi: 640000,
    longShare: 0.12,
    shortShare: 0.218,
    contracts: ['IM2611', 'IM2612', 'IM2703'],
    main: 'IM2612',
    corrInject: 0.00066,
    netCenterRatio: -0.098,
  },
];

const MEMBERS = [
  '中信期货',
  '国泰君安',
  '海通期货',
  '华泰期货',
  '东证期货',
  '永安期货',
  '银河期货',
  '中信建投',
  '申银万国',
  '国投安信',
  '光大期货',
  '招商期货',
  '中财期货',
  '方正中期',
  '广发期货',
  '南华期货',
  '宝城期货',
  '兴证期货',
  '东吴期货',
  '宏源期货',
];

const DISCLAIMER =
  '本站所有统计结论均为历史数据的概率性描述，不构成任何投资建议。期货交易有杠杆风险，请独立判断并自负盈亏。';

/* ------------------------------------------------------------------ */
/* 单品种数据构造                                                      */
/* ------------------------------------------------------------------ */

function buildVariety(cfg, dates, rand) {
  const n = dates.length;
  const oi = cfg.oi;

  /* 1) 因子新息（用于生成净持仓变化），与指数收益弱相关 */
  const zInnov = new Array(n);
  for (let i = 0; i < n; i++) zInnov[i] = gauss(rand);

  /* 2) 指数对数收益：噪声 + 上一日净持仓新息（弱正相关，制造一点"预测力"） */
  const sigmaEps = 0.0108;
  const logRet = new Array(n).fill(0);
  for (let i = 1; i < n; i++) {
    logRet[i] = 0.00018 + sigmaEps * gauss(rand) + cfg.corrInject * zInnov[i - 1];
  }
  const cum = new Array(n).fill(0);
  for (let i = 1; i < n; i++) cum[i] = cum[i - 1] + logRet[i];
  const scale = cfg.endClose / Math.exp(cum[n - 1]);
  const close = cum.map((c) => round(Math.exp(c) * scale, 2));

  const indexRetPct = new Array(n).fill(null);
  for (let i = 1; i < n; i++) indexRetPct[i] = (close[i] / close[i - 1] - 1) * 100;

  /* 3) 市场持仓量（缓慢波动） */
  const marketOi = new Array(n);
  for (let i = 0; i < n; i++) {
    marketOi[i] = Math.round(oi * (1 + 0.12 * Math.sin(i / 55) + 0.03 * gauss(rand)));
  }

  /* 4) 中信期货净持仓：围绕缓慢漂移中心做均值回复随机游走；
        最后一年叠加下行趋势（净空单持续加仓），使"当日"落在低分位，便于检验极端分位 UI */
  const sigmaD = 0.006 * oi;
  const endNet = cfg.netCenterRatio * oi;
  const trendStart = n - 252;
  const center = (t) => {
    const base = endNet * (1 + 0.1 * Math.sin(t / 70) + 0.05 * Math.cos(t / 23));
    if (t <= trendStart) return base;
    const u = Math.min(1, (t - trendStart) / 252);
    return base * (1 - 0.24 * (u * u * (3 - 2 * u)));
  };
  // 让最后一日出现"净空单大幅增加"（dnet 强负），对应契约示例的极端分位场景
  zInnov[n - 1] = -2.35;
  const net = new Array(n).fill(0);
  net[0] = center(0);
  for (let i = 1; i < n; i++) {
    const dnet = sigmaD * zInnov[i] + 0.035 * (center(i) - net[i - 1]);
    net[i] = net[i - 1] + dnet;
  }
  const shift = endNet - net[n - 1];
  for (let i = 0; i < n; i++) net[i] += shift;

  /* 5) 多空拆解：多头按占比小幅波动，空头 = 多头 - 净持仓 */
  const citicLong = new Array(n);
  const citicShort = new Array(n);
  for (let i = 0; i < n; i++) {
    let long = Math.round(marketOi[i] * cfg.longShare * (1 + 0.05 * gauss(rand)));
    let short = long - Math.round(net[i]);
    if (short < Math.round(oi * 0.01)) {
      short = Math.round(oi * 0.01);
      long = short + Math.round(net[i]);
    }
    citicLong[i] = long;
    citicShort[i] = short;
  }
  const citicNet = citicLong.map((v, i) => v - citicShort[i]);

  /* 6) 变化与滚动标准化 / 分位 */
  const dnet = new Array(n).fill(null);
  for (let i = 1; i < n; i++) dnet[i] = citicNet[i] - citicNet[i - 1];
  const dnetPctOi = dnet.map((v) => (v === null ? null : (v / oi) * 100));
  const citicNetPct = citicNet.map((v) => (v / oi) * 100);

  const dnetZ = rollingZ(dnetPctOi, 252, 60);
  const netZ = rollingZ(citicNet, 252, 60);
  const netPctile = rollingPctile(citicNet, 252, 60);
  const dnetPctile = rollingPctile(dnetPctOi, 252, 60);

  /* 7) 主力合约价格与基差 */
  const mainClose = new Array(n);
  const basis = new Array(n);
  const basisPct = new Array(n);
  for (let i = 0; i < n; i++) {
    const b = close[i] * (0.0006 * gauss(rand) + 0.0002 * Math.sin(i / 40));
    basis[i] = round(b, 2);
    mainClose[i] = round(close[i] + b, 2);
    basisPct[i] = round((b / close[i]) * 100, 4);
  }

  /* 8) 次日对数收益（%） */
  const yNext = new Array(n).fill(null);
  for (let i = 0; i < n - 1; i++) yNext[i] = Math.log(close[i + 1] / close[i]) * 100;
  const logRetPct = logRet.map((v, i) => (i === 0 ? null : v * 100));

  /* 9) Top20 合计 */
  const top20Long = new Array(n);
  const top20Short = new Array(n);
  for (let i = 0; i < n; i++) {
    const tl = Math.round(marketOi[i] * (0.74 + 0.03 * gauss(rand)));
    const ts = Math.round(marketOi[i] * (0.82 + 0.03 * gauss(rand)));
    top20Long[i] = tl;
    top20Short[i] = ts;
  }
  const top20Net = top20Long.map((v, i) => v - top20Short[i]);

  /* 10) 拟合统计 */
  const pairsPredictive = ols(dnetZ, yNext);
  const contemporaneous = corr(citicNetPct, indexRetPct);
  const corrPredictive = pairsPredictive.corr;
  const models = [
    {
      key: 'dnet_z',
      name: '净空单日变化(标准化)',
      n: pairsPredictive.n,
      corr: round(pairsPredictive.corr, 4),
      slope: round(pairsPredictive.slope, 5),
      intercept: round(pairsPredictive.intercept, 5),
      r2: round(pairsPredictive.r2, 5),
      t_stat: round(pairsPredictive.t_stat, 3),
      p_value: round(pairsPredictive.p_value, 4),
      significant: pairsPredictive.significant,
    },
  ];
  const mNetZ = ols(netZ, yNext);
  models.push({
    key: 'net_z',
    name: '净持仓水平(标准化)',
    n: mNetZ.n,
    corr: round(mNetZ.corr, 4),
    slope: round(mNetZ.slope, 5),
    intercept: round(mNetZ.intercept, 5),
    r2: round(mNetZ.r2, 5),
    t_stat: round(mNetZ.t_stat, 3),
    p_value: round(mNetZ.p_value, 4),
    significant: mNetZ.significant,
  });
  const mNetPct = ols(netPctile, yNext);
  models.push({
    key: 'net_pctile',
    name: '净持仓历史分位',
    n: mNetPct.n,
    corr: round(mNetPct.corr, 4),
    slope: round(mNetPct.slope, 5),
    intercept: round(mNetPct.intercept, 5),
    r2: round(mNetPct.r2, 5),
    t_stat: round(mNetPct.t_stat, 3),
    p_value: round(mNetPct.p_value, 4),
    significant: mNetPct.significant,
  });

  /* 领先滞后：x = 当日净空单变化(占持仓比)，y = 指数对数收益(shift) */
  const lagCorr = [];
  for (let lag = -3; lag <= 3; lag++) {
    const xs = [];
    const ys = [];
    for (let i = 0; i < n; i++) {
      const j = i + lag;
      if (j < 0 || j >= n) continue;
      xs.push(dnetPctOi[i]);
      ys.push(logRetPct[j]);
    }
    const c = corr(xs, ys);
    lagCorr.push({ lag: lag, corr: round(c.corr, 4), n: c.n });
  }

  /* 滚动相关（60 日） */
  const rollContemp = rollingCorr(citicNetPct, indexRetPct, 60);
  const rollPredictive = rollingCorr(dnetZ, yNext, 60);

  /* 年度稳定性 */
  const byYear = new Map();
  for (let i = 0; i < n; i++) {
    const yr = Number(dates[i].slice(0, 4));
    if (!byYear.has(yr)) byYear.set(yr, { x: [], y: [] });
    const b = byYear.get(yr);
    b.x.push(dnetZ[i]);
    b.y.push(yNext[i]);
  }
  const years = Array.from(byYear.keys())
    .sort((a, b) => a - b)
    .map((yr) => {
      const b = byYear.get(yr);
      const c = corr(b.x, b.y);
      return { year: yr, corr_predictive: round(c.corr, 4), n: c.n };
    });

  /* 散点（最多 400 个点） */
  const scatterPairs = [];
  for (let i = 0; i < n; i++) {
    if (dnetZ[i] === null || yNext[i] === null) continue;
    scatterPairs.push([dnetZ[i], yNext[i]]);
  }
  const step = Math.max(1, Math.floor(scatterPairs.length / 400));
  const sampled = scatterPairs.filter((_, i) => i % step === 0);
  const xLine = [-2.5, 2.5];
  const yLine = xLine.map((x) => pairsPredictive.intercept + pairsPredictive.slope * x);

  /* 11) 分位分组（5 组，按 dnet_z 排序切分） */
  const valid = [];
  for (let i = 0; i < n; i++) {
    if (dnetZ[i] === null || yNext[i] === null) continue;
    valid.push({ i: i, x: dnetZ[i], y: yNext[i] });
  }
  valid.sort((a, b) => a.x - b.x);
  const labels = ['Q1 净空单大幅增加', 'Q2 净空单小幅增加', 'Q3 净空单基本持平', 'Q4 净空单小幅减少', 'Q5 净空单大幅减少'];
  const bucketRows = [];
  const groupSize = Math.floor(valid.length / 5);
  for (let g = 0; g < 5; g++) {
    const from = g * groupSize;
    const to = g === 4 ? valid.length : (g + 1) * groupSize;
    const chunk = valid.slice(from, to);
    if (!chunk.length) continue;
    chunk.forEach((c) => {
      c.group = g + 1;
    });
    const ys = chunk.map((c) => c.y);
    const m = mean(ys);
    const sd = stdev(ys);
    const nB = chunk.length;
    const t = sd && sd > 0 ? m / (sd / Math.sqrt(nB)) : null;
    bucketRows.push({
      bucket: g + 1,
      label: labels[g],
      lo: g === 0 ? null : round(chunk[0].x, 3),
      hi: g === 4 ? null : round(chunk[chunk.length - 1].x, 3),
      n: nB,
      mean_ret_pct: round(m, 3),
      median_ret_pct: round(median(ys), 3),
      win_rate: round((100 * ys.filter((v) => v > 0).length) / nB, 2),
      t_stat: round(t, 3),
      p_value: round(pValueFromT(t, nB), 4),
    });
  }
  const sp = spearman(dnetZ, yNext);
  const icP = sp.ic === null ? null : pValueFromT(sp.ic * Math.sqrt((sp.n - 2) / Math.max(1e-9, 1 - sp.ic * sp.ic)), sp.n);

  /* 12) 回测：滚动 252 日分位 ≤20% 做空、≥80% 做多、其余空仓 */
  const costBp = 0.5;
  const nav = [];
  const navBh = [];
  let equity = 1;
  let bh = 1;
  let pos = 0;
  let trades = 0;
  let exposureDays = 0;
  let validDays = 0;
  const stratDaily = [];
  let wins = 0;
  let closes = 0;
  for (let i = 1; i < n; i++) {
    const sig = dnetPctile[i - 1];
    let target = 0;
    if (sig !== null) {
      if (sig <= 20) target = -1;
      else if (sig >= 80) target = 1;
    }
    const dayRet = logRetPct[i] / 100; // 第 i 日实现收益
    if (target !== pos) {
      equity *= 1 - costBp / 10000;
      trades++;
      pos = target;
    }
    const stratRet = pos * dayRet;
    equity *= 1 + stratRet;
    bh *= 1 + dayRet;
    if (pos !== 0) exposureDays++;
    validDays++;
    if (pos !== 0 && i > 1) {
      closes++;
      if (stratRet > 0) wins++;
    }
    stratDaily.push(stratRet);
    nav.push(round(equity, 4));
    navBh.push(round(bh, 4));
  }
  const navDates = dates.slice(1);
  const cumReturn = equity - 1;
  const annual = Math.pow(1 + cumReturn, 252 / Math.max(1, validDays)) - 1;
  let peak = nav[0] || 1;
  let mdd = 0;
  for (const v of nav) {
    if (v > peak) peak = v;
    const dd = v / peak - 1;
    if (dd < mdd) mdd = dd;
  }
  const sdDaily = stdev(stratDaily);
  const sharpe = sdDaily && sdDaily > 0 ? (mean(stratDaily) / sdDaily) * Math.sqrt(252) : null;

  /* 13) 次日研判 */
  const lastIdx = n - 1;
  const lastDate = dates[lastIdx];
  const curDnetZ = dnetZ[lastIdx];
  /* 当日 dnet_z 落在第几组（按 valid 内分位） */
  let curGroup = 3;
  if (curDnetZ !== null && valid.length) {
    const pctOfDnet = (100 * valid.filter((v) => v.x <= curDnetZ).length) / valid.length;
    curGroup = Math.min(5, Math.max(1, Math.ceil(pctOfDnet / 20) || 1));
  }
  const bucket = bucketRows.find((b) => b.bucket === curGroup) || bucketRows[2] || null;
  const upProb = bucket ? bucket.win_rate : null;
  const expected = bucket ? bucket.mean_ret_pct : null;
  const sampleN = bucket ? bucket.n : null;
  let score = 0;
  const w1 = 0.6;
  const contributions = [];
  const drvNetZ = netZ[lastIdx];
  const drvNetPct = netPctile[lastIdx];
  if (curDnetZ !== null) {
    const sign = Math.sign(curDnetZ);
    score += w1 * sign * Math.min(1, Math.abs(curDnetZ) / 2);
    contributions.push({
      key: 'dnet_z',
      name: '净空单日变化(标准化)',
      value: round(curDnetZ, 3),
      impact: curDnetZ > 0.15 ? '利多' : curDnetZ < -0.15 ? '利空' : '中性',
      weight: w1,
      desc:
        '今日净空单较昨日' +
        (curDnetZ > 0 ? '减少' : '增加') +
        '，位于近一年 ' +
        round(dnetPctile[lastIdx], 1) +
        '% 分位',
    });
  }
  if (drvNetZ !== null) {
    score += 0.25 * Math.sign(drvNetZ) * Math.min(1, Math.abs(drvNetZ) / 2);
    contributions.push({
      key: 'net_z',
      name: '净持仓水平(标准化)',
      value: round(drvNetZ, 3),
      impact: drvNetZ > 0.15 ? '利多' : drvNetZ < -0.15 ? '利空' : '中性',
      weight: 0.25,
      desc: '当前净持仓处于滚动 252 日 ' + round(drvNetPct, 1) + '% 分位',
    });
  }
  if (drvNetPct !== null) {
    score += 0.15 * Math.sign(drvNetPct - 50) * Math.min(1, Math.abs(drvNetPct - 50) / 40);
    contributions.push({
      key: 'net_pctile',
      name: '净持仓历史分位',
      value: round(drvNetPct, 2),
      impact: drvNetPct < 50 ? '偏空' : '偏多',
      weight: 0.15,
      desc: '净持仓分位 ' + round(drvNetPct, 1) + '%（历史越低=净空单越重）',
    });
  }
  score = round(Math.max(-1, Math.min(1, score)), 3);
  const direction = score >= 0.2 ? '偏多' : score <= -0.2 ? '偏空' : '中性';
  const confidence = Math.abs(score) >= 0.55 ? '高' : Math.abs(score) >= 0.25 ? '中' : '低';

  /* Wilson 区间 */
  function wilson(p, nB) {
    if (p === null || !nB) return null;
    const z = 1.96;
    const ph = p / 100;
    const denom = 1 + (z * z) / nB;
    const center2 = ph + (z * z) / (2 * nB);
    const margin = z * Math.sqrt((ph * (1 - ph)) / nB + (z * z) / (4 * nB * nB));
    return [round((100 * Math.max(0, center2 - margin)) / denom, 1), round((100 * Math.min(1, center2 + margin)) / denom, 1)];
  }

  const lastExamples = bucket
    ? valid
        .filter((v) => v.group === curGroup && dates[v.i] !== lastDate)
        .slice(-3)
        .reverse()
        .map((v) => ({ date: dates[v.i], next_ret_pct: round(v.y, 2) }))
    : [];

  const guidance = {
    date: lastDate,
    for_date: nextWeekday(lastDate),
    direction: direction,
    score: score,
    confidence: confidence,
    up_prob: upProb,
    expected_ret_pct: expected,
    sample_n: sampleN,
    drivers: contributions,
    evidence: [
      '净空单日变化处于滚动 252 日 ' +
        (dnetPctile[lastIdx] === null ? '—' : round(dnetPctile[lastIdx], 1)) +
        '% 分位，历史上该分位组次日上涨概率 ' +
        (upProb === null ? '—' : round(upProb, 1)) +
        '%（n=' +
        (sampleN === null ? '—' : sampleN) +
        '）',
      '近 60 日「净空单变化→次日收益」滚动相关系数 ' +
        (() => {
          const rc = rollPredictive[lastIdx];
          return rc === null ? '—' : round(rc, 2);
        })() +
        '，全样本 ' +
        (corrPredictive === null ? '—' : round(corrPredictive, 3)) +
        '，方向一致',
    ],
    caveats: [
      '单因子解释力弱（R²=' + round(pairsPredictive.r2, 4) + '），仅作概率参考，不可作为交易依据',
      '该分位分组基于全窗口样本，含后验信息，仅供描述',
      '会员持仓披露口径在 2024-05 变化，跨期比较需谨慎',
    ],
    similar: {
      n: sampleN,
      mean_ret_pct: expected,
      win_rate: upProb,
      up_prob_ci: wilson(upProb, sampleN),
      last_examples: lastExamples,
    },
  };

  /* 14) 快照 */
  const snapshot = {
    date: lastDate,
    index_close: close[lastIdx],
    index_chg_pct: round(indexRetPct[lastIdx], 2),
    main_contract: cfg.main,
    main_close: mainClose[lastIdx],
    basis: basis[lastIdx],
    basis_pct: basisPct[lastIdx],
    citic_long: citicLong[lastIdx],
    citic_short: citicShort[lastIdx],
    citic_net: citicNet[lastIdx],
    citic_net_pct: round(citicNetPct[lastIdx], 2),
    citic_net_z: round(netZ[lastIdx], 3),
    citic_net_pctile: round(netPctile[lastIdx], 1),
    dnet: dnet[lastIdx],
    dnet_pct_of_oi: round(dnetPctOi[lastIdx], 3),
    dnet_z: round(dnetZ[lastIdx], 3),
    dnet_pctile: round(dnetPctile[lastIdx], 1),
    top20_long: top20Long[lastIdx],
    top20_short: top20Short[lastIdx],
    top20_net: top20Net[lastIdx],
    market_oi: marketOi[lastIdx],
    citic_share_of_top20: round((citicNet[lastIdx] / Math.abs(top20Net[lastIdx])) * 100, 1),
  };

  /* 15) 龙虎榜 */
  const rankMembers = MEMBERS.slice();
  const hlLong = citicLong[lastIdx];
  const hlShort = citicShort[lastIdx];
  const others = rankMembers.filter((m) => m !== MEMBER_HL);
  const totalLong = Math.round(marketOi[lastIdx] * 0.76);
  const totalShort = Math.round(marketOi[lastIdx] * 0.84);

  function buildList(total, hlValue, decay, rand2) {
    const rest = total - hlValue;
    let weights = others.map((_, i) => Math.pow(decay, i));
    const wsum = sum(weights);
    const rows = others.map((m, i) => ({
      member: m,
      position: Math.max(100, Math.round((rest * weights[i]) / wsum + 300 * gauss(rand2))),
      change: Math.round(900 * gauss(rand2)),
    }));
    rows.push({ member: MEMBER_HL, position: hlValue, change: Math.round(900 * gauss(rand2)) });
    rows.sort((a, b) => b.position - a.position);
    return rows.map((r, i) => ({ rank: i + 1, member: r.member, position: r.position, change: r.change }));
  }

  const longList = buildList(totalLong, hlLong, 0.9, rand);
  const shortList = buildList(totalShort, hlShort, 0.9, rand);
  const volList = MEMBERS.map((m) => ({
    member: m,
    position: Math.round((marketOi[lastIdx] * 0.55) / MEMBERS.length * (1.6 + 0.9 * rand())),
    change: Math.round(1200 * gauss(rand)),
  }))
    .sort((a, b) => b.position - a.position)
    .slice(0, 20)
    .map((r, i) => ({ rank: i + 1, member: r.member, position: r.position, change: r.change }));

  const ranks = {
    ok: true,
    error: null,
    date: lastDate,
    variety: cfg.code,
    long: longList,
    short: shortList,
    volume: volList,
    highlight: MEMBER_HL,
    highlight_row: {
      member: MEMBER_HL,
      long: hlLong,
      short: hlShort,
      net: hlLong - hlShort,
    },
    contracts: cfg.contracts,
  };

  /* 16) 会员对比 */
  const memberRows = [];
  const others2 = MEMBERS.filter((m) => m !== MEMBER_HL).slice(0, 11);
  others2.forEach((m, i) => {
    const netLatest = Math.round(-oi * (0.01 + 0.03 * rand()));
    memberRows.push({
      member: m,
      net_latest: netLatest,
      net_pct_latest: round((netLatest / oi) * 100, 2),
      net_avg20: Math.round(netLatest * (0.9 + 0.2 * rand())),
      net_change20: Math.round(netLatest * 0.12 * gauss(rand)),
      pred_corr: round(0.04 * gauss(rand), 4),
      n: 600 + Math.round(80 * rand()),
      rank: i + 2,
    });
  });
  memberRows.unshift({
    member: MEMBER_HL,
    net_latest: citicNet[lastIdx],
    net_pct_latest: round(citicNetPct[lastIdx], 2),
    net_avg20: Math.round(mean(citicNet.slice(-20))),
    net_change20: Math.round(citicNet[lastIdx] - mean(citicNet.slice(-21, -1))),
    pred_corr: round(corrPredictive, 4),
    n: pairsPredictive.n,
    rank: 1,
  });

  const windowStart = dates[Math.max(0, n - 720)];
  const dashboard = {
    ok: true,
    error: null,
    empty: false,
    variety: cfg.code,
    variety_name: cfg.name,
    index_code: cfg.index_code,
    index_name: cfg.index_name,
    member: MEMBER_HL,
    lookback: 720,
    as_of: lastDate,
    window: { start: windowStart, end: lastDate, n: n - Math.max(0, n - 720) },
    snapshot: snapshot,
    series: {
      date: dates.slice(),
      index_close: close.slice(),
      index_ret_pct: indexRetPct.map((v) => round(v, 3)),
      citic_net: citicNet.slice(),
      citic_net_pct: citicNetPct.map((v) => round(v, 3)),
      citic_long: citicLong.slice(),
      citic_short: citicShort.slice(),
      market_oi: marketOi.slice(),
      main_close: mainClose.slice(),
      basis_pct: basisPct.slice(),
      dnet_pct_of_oi: dnetPctOi.map((v) => round(v, 4)),
      dnet_z: dnetZ.map((v) => round(v, 3)),
      net_pctile: netPctile.map((v) => round(v, 2)),
    },
    fit: {
      n: pairsPredictive.n,
      sample: { start: dates[0], end: lastDate },
      corr_contemporaneous: round(contemporaneous.corr, 4),
      corr_predictive: round(corrPredictive, 4),
      models: models,
      primary: 'dnet_z',
      lag_corr: lagCorr,
      rolling_corr: {
        date: dates.slice(),
        contemporaneous: rollContemp.map((v) => round(v, 3)),
        predictive: rollPredictive.map((v) => round(v, 3)),
      },
      stability: { years: years },
      scatter: {
        x: sampled.map((p) => round(p[0], 3)),
        y: sampled.map((p) => round(p[1], 3)),
        line: { x: xLine, y: yLine.map((v) => round(v, 4)) },
      },
      x_label: '净空单日变化(滚动252日标准化)',
      y_label: '次日指数对数收益(%)',
    },
    buckets: {
      x_label: '净空单日变化分位组',
      n_buckets: bucketRows.length,
      ic: round(sp.ic, 4),
      ic_p: round(icP, 4),
      rows: bucketRows,
    },
    backtest: {
      rule: '滚动252日分位：≤20% 做空、≥80% 做多、其余空仓，次日收盘结算',
      cost_bp: costBp,
      equity: { date: navDates, nav: nav, nav_bh: navBh },
      metrics: {
        cum_return: round(cumReturn, 4),
        annual_return: round(annual, 4),
        max_drawdown: round(mdd, 4),
        sharpe: round(sharpe, 3),
        hit_rate: closes ? round((100 * wins) / closes, 2) : null,
        trades: trades,
        exposure: validDays ? round(exposureDays / validDays, 3) : null,
        benchmark_cum: round(bh - 1, 4),
        n: validDays,
      },
    },
    guidance: guidance,
    ranks: ranks,
    warnings: [
      '2024-05-06 起会员持仓披露改为代客口径，跨期比较需谨慎',
      '当前为前端 mock 示例数据（非真实行情），仅用于界面预览',
    ],
  };

  return { dashboard: dashboard, ranks: ranks, members: { ok: true, error: null, variety: cfg.code, lookback: 720, member: MEMBER_HL, rows: memberRows } };
}

/* ------------------------------------------------------------------ */
/* 自检：结构对齐契约                                                   */
/* ------------------------------------------------------------------ */

function assertKeys(obj, label, keys) {
  const miss = keys.filter((k) => !(k in obj));
  if (miss.length) throw new Error('[结构不符] ' + label + ' 缺少字段: ' + miss.join(', '));
}

function validateMeta(m) {
  assertKeys(m, 'meta', [
    'ok', 'error', 'generated_at', 'latest_trade_date', 'data', 'varieties', 'members',
    'default_member', 'default_lookback', 'lookback_options', 'disclaimer',
  ]);
  assertKeys(m.data, 'meta.data', ['index_daily', 'variety_daily', 'member_rank', 'by_variety', 'last_sync']);
  assertKeys(m.varieties[0], 'meta.varieties[0]', ['code', 'name', 'index_code', 'index_name', 'multiplier', 'color']);
}

function validateDashboard(d) {
  assertKeys(d, 'dashboard', [
    'ok', 'error', 'empty', 'variety', 'variety_name', 'index_code', 'index_name', 'member',
    'lookback', 'as_of', 'window', 'snapshot', 'series', 'fit', 'buckets', 'backtest', 'guidance', 'ranks', 'warnings',
  ]);
  assertKeys(d.window, 'window', ['start', 'end', 'n']);
  assertKeys(d.snapshot, 'snapshot', [
    'date', 'index_close', 'index_chg_pct', 'main_contract', 'main_close', 'basis', 'basis_pct',
    'citic_long', 'citic_short', 'citic_net', 'citic_net_pct', 'citic_net_z', 'citic_net_pctile',
    'dnet', 'dnet_pct_of_oi', 'dnet_z', 'dnet_pctile', 'top20_long', 'top20_short', 'top20_net',
    'market_oi', 'citic_share_of_top20',
  ]);
  assertKeys(d.series, 'series', [
    'date', 'index_close', 'index_ret_pct', 'citic_net', 'citic_net_pct', 'citic_long',
    'citic_short', 'market_oi', 'main_close', 'basis_pct', 'dnet_pct_of_oi', 'dnet_z', 'net_pctile',
  ]);
  assertKeys(d.fit, 'fit', [
    'n', 'sample', 'corr_contemporaneous', 'corr_predictive', 'models', 'primary',
    'lag_corr', 'rolling_corr', 'stability', 'scatter', 'x_label', 'y_label',
  ]);
  assertKeys(d.fit.models[0], 'fit.models[0]', ['key', 'name', 'n', 'corr', 'slope', 'intercept', 'r2', 't_stat', 'p_value', 'significant']);
  assertKeys(d.fit.lag_corr[0], 'fit.lag_corr[0]', ['lag', 'corr', 'n']);
  assertKeys(d.fit.rolling_corr, 'fit.rolling_corr', ['date', 'contemporaneous', 'predictive']);
  assertKeys(d.fit.stability.years[0], 'fit.stability.years[0]', ['year', 'corr_predictive', 'n']);
  assertKeys(d.buckets, 'buckets', ['x_label', 'n_buckets', 'ic', 'ic_p', 'rows']);
  assertKeys(d.buckets.rows[0], 'buckets.rows[0]', ['bucket', 'label', 'lo', 'hi', 'n', 'mean_ret_pct', 'median_ret_pct', 'win_rate', 't_stat', 'p_value']);
  assertKeys(d.backtest, 'backtest', ['rule', 'cost_bp', 'equity', 'metrics']);
  assertKeys(d.backtest.equity, 'backtest.equity', ['date', 'nav', 'nav_bh']);
  assertKeys(d.backtest.metrics, 'backtest.metrics', ['cum_return', 'annual_return', 'max_drawdown', 'sharpe', 'hit_rate', 'trades', 'exposure', 'benchmark_cum', 'n']);
  assertKeys(d.guidance, 'guidance', ['date', 'for_date', 'direction', 'score', 'confidence', 'up_prob', 'expected_ret_pct', 'sample_n', 'drivers', 'evidence', 'caveats', 'similar']);
  assertKeys(d.guidance.drivers[0], 'guidance.drivers[0]', ['key', 'name', 'value', 'impact', 'weight', 'desc']);
  assertKeys(d.guidance.similar, 'guidance.similar', ['n', 'mean_ret_pct', 'win_rate', 'up_prob_ci', 'last_examples']);
  assertKeys(d.ranks, 'ranks', ['ok', 'date', 'variety', 'long', 'short', 'volume', 'highlight', 'highlight_row', 'contracts']);
  assertKeys(d.ranks.long[0], 'ranks.long[0]', ['rank', 'member', 'position', 'change']);
  assertKeys(d.ranks.highlight_row, 'ranks.highlight_row', ['member', 'long', 'short', 'net']);
}

function validateRanks(r) {
  assertKeys(r, 'ranks', ['ok', 'date', 'variety', 'long', 'short', 'volume', 'highlight', 'highlight_row', 'contracts']);
  if (r.long.length !== 20 || r.short.length !== 20) throw new Error('[结构不符] ranks 多空应各 20 条');
}

function validateMembers(m) {
  assertKeys(m, 'members', ['ok', 'variety', 'lookback', 'member', 'rows']);
  assertKeys(m.rows[0], 'members.rows[0]', ['member', 'net_latest', 'net_pct_latest', 'net_avg20', 'net_change20', 'pred_corr', 'n', 'rank']);
}

/* ------------------------------------------------------------------ */
/* 主流程                                                              */
/* ------------------------------------------------------------------ */

function main() {
  const dates = tradingDays(START, TODAY);
  const rand = mulberry32(20261020);
  const byVariety = {};
  const metaVarieties = [];
  let out = {};

  for (const cfg of VARIETIES) {
    const { dashboard, ranks, members } = buildVariety(cfg, dates, rand);
    validateDashboard(dashboard);
    validateRanks(ranks);
    validateMembers(members);
    out['dashboard_' + cfg.code + '.json'] = JSON.stringify(dashboard);
    out['ranks_' + cfg.code + '.json'] = JSON.stringify(ranks);
    out['members_' + cfg.code + '.json'] = JSON.stringify(members);
    byVariety[cfg.code] = {
      start: dates[0],
      end: dates[dates.length - 1],
      rows: dates.length,
    };
    metaVarieties.push({
      code: cfg.code,
      name: cfg.name,
      index_code: cfg.index_code,
      index_name: cfg.index_name,
      multiplier: cfg.multiplier,
      color: cfg.color,
    });
  }

  const meta = {
    ok: true,
    error: null,
    generated_at: '2026-10-20T22:40:00+08:00',
    latest_trade_date: TODAY,
    data: {
      index_daily: { start: dates[0], end: TODAY, rows: dates.length },
      variety_daily: { start: dates[0], end: TODAY, rows: dates.length * 4 },
      member_rank: { start: dates[0], end: TODAY, rows: dates.length * 4 * 60 },
      by_variety: byVariety,
      last_sync: '2026-10-20T22:31:00',
    },
    varieties: metaVarieties,
    members: MEMBERS.slice(),
    default_member: MEMBER_HL,
    default_lookback: 720,
    lookback_options: [240, 480, 720, 0],
    disclaimer: DISCLAIMER,
  };
  validateMeta(meta);
  out['meta.json'] = JSON.stringify(meta, null, 2);

  for (const name of Object.keys(out)) {
    fs.writeFileSync(path.join(OUT_DIR, name), out[name], 'utf8');
  }

  const sizes = Object.keys(out).map((f) => f + ' ' + (Buffer.byteLength(out[f]) / 1024).toFixed(0) + 'KB');
  const ifd = JSON.parse(out['dashboard_IF.json']);
  console.log('生成日期数: ' + dates.length + ' (' + dates[0] + ' ~ ' + dates[dates.length - 1] + ')');
  console.log(sizes.join(' | '));
  console.log('IF 快照: ' + JSON.stringify(ifd.snapshot));
  console.log('IF guidance: direction=' + ifd.guidance.direction + ' score=' + ifd.guidance.score +
    ' up_prob=' + ifd.guidance.up_prob + ' expected=' + ifd.guidance.expected_ret_pct + ' n=' + ifd.guidance.sample_n);
  console.log('IF fit: corr_pred=' + ifd.fit.corr_predictive + ' r2=' + ifd.fit.models[0].r2 +
    ' p=' + ifd.fit.models[0].p_value + ' ic=' + ifd.buckets.ic);
  console.log('IF backtest: ' + JSON.stringify(ifd.backtest.metrics));
  console.log('OK 所有 mock 文件已生成并通过结构校验');
}

main();
