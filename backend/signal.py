# -*- coding: utf-8 -*-
"""次日研判引擎（guidance 块）。

契约：``contract/API_CONTRACT.md`` 5.2.1。

输入 ``df`` 由 :mod:`backend.analytics` 的 ``derive_features`` 产出，至少包含列：

* ``date``            YYYY-MM-DD
* ``y``               次日对数收益（%）：``ln(close_{t+1}/close_t)*100``
* ``dnet``            中信净持仓日变化（手）
* ``dnet_pct_of_oi``  净持仓日变化 / 全市场持仓（%）
* ``dnet_z``          上者滚动 252 日 z 值（min_periods=60）
* ``dnet_pctile``     上者滚动 252 日分位（0-100）
* ``net_z`` / ``net_pctile``  净持仓水平的滚动 z / 分位
* ``citic_net_pct``   净持仓 / 全市场持仓（%）

统计口径全部按契约第 6 节：OLS 用 numpy 手写标准误，p 值用正态近似
（``math.erfc``），不引入 scipy；输出里不允许出现 NaN / Infinity（一律转 None）。
"""
from __future__ import annotations

import datetime as _dt
import math

import numpy as np
import pandas as pd

import config

# ---------------------------------------------------------------- 常量
N_BUCKETS = int(getattr(config, "N_BUCKETS", 5))
ROLLING_CORR_WINDOW = int(getattr(config, "ROLLING_CORR_WINDOW", 60))

DIRECTION_LONG = "偏多"
DIRECTION_SHORT = "偏空"
DIRECTION_NEUTRAL = "中性"

CONF_LOW = "低"
CONF_MID = "中"
CONF_HIGH = "高"

# 分位组中文标签：Q1 = dnet_z 最小（净空单增加最多），Q5 = 最大（净空单减少最多）
BUCKET_LABELS = [
    "Q1 净空单大幅增加",
    "Q2 净空单小幅增加",
    "Q3 净空单基本持平",
    "Q4 净空单小幅减少",
    "Q5 净空单大幅减少",
]

# 三个候选因子的元信息（key -> 中文名 / 说明）
FACTOR_META = {
    "dnet_z": {
        "name": "净空单日变化(标准化)",
        "desc": "净持仓(多-空)日变化占全市场持仓比，滚动252日标准化",
    },
    "net_z": {
        "name": "净持仓水平(标准化)",
        "desc": "净持仓占全市场持仓比，滚动252日标准化",
    },
    "net_pctile": {
        "name": "净持仓历史分位",
        "desc": "净持仓占全市场持仓比在滚动252日中的分位(0-100)",
    },
}


# ---------------------------------------------------------------- 数值清洗
def num(v, nd: int = 4):
    """转成 JSON 安全的 float：NaN / Inf / 非法值 -> None，其余四舍五入。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    r = round(f, nd)
    if r == 0:
        return 0.0
    return r


def num_int(v):
    """转成 JSON 安全的 int（持仓手数）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return int(round(f))


def norm_p(t):
    """双侧 p 值：``2*(1-Phi(|t|)) = erfc(|t|/sqrt(2))``（正态近似）。"""
    if t is None:
        return None
    try:
        f = float(t)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return math.erfc(abs(f) / math.sqrt(2.0))


def _col(df, name):
    """取列并转 float Series；列不存在时返回等长 NaN Series。"""
    if df is None:
        return pd.Series(dtype="float64")
    if name not in df.columns:
        return pd.Series(np.full(len(df), np.nan), index=df.index, dtype="float64")
    return pd.to_numeric(pd.Series(df[name]), errors="coerce").astype("float64")


def _finite(a) -> np.ndarray:
    arr = np.asarray(a, dtype="float64")
    return arr[np.isfinite(arr)]


# ---------------------------------------------------------------- OLS / 相关性
def ols(xs, ys) -> dict:
    """一元 OLS（含经典标准误与正态近似 p 值）。

    返回 ``{n, corr, slope, intercept, r2, t_stat, p_value, significant, resid_std}``。
    样本不足或退化时相应字段为 None。
    """
    x = np.asarray(xs, dtype="float64")
    y = np.asarray(ys, dtype="float64")
    if x.size != y.size:
        n = min(x.size, y.size)
        x, y = x[:n], y[:n]
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(np.count_nonzero(mask))
    out = {
        "n": n, "corr": None, "slope": None, "intercept": None,
        "r2": None, "t_stat": None, "p_value": None,
        "significant": False, "resid_std": None,
    }
    if n < 3:
        return out
    x = x[mask]
    y = y[mask]
    mx = float(x.mean())
    my = float(y.mean())
    dx = x - mx
    dy = y - my
    sxx = float(np.sum(dx * dx))
    taxx = float(np.sum(dy * dy))
    sxy = float(np.sum(dx * dy))
    if sxx <= 0.0 or taxx <= 0.0:
        return out
    slope = sxy / sxx
    intercept = my - slope * mx
    resid = y - (intercept + slope * x)
    rss = float(np.sum(resid * resid))
    r2 = 1.0 - rss / taxx
    corr = sxy / math.sqrt(sxx * taxx)
    out.update({
        "corr": corr,
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "resid_std": math.sqrt(rss / max(n - 2, 1)),
    })
    if n > 2:
        sigma2 = rss / (n - 2)
        se = math.sqrt(sigma2 / sxx) if sigma2 > 0 and sxx > 0 else 0.0
        if se > 0:
            t = slope / se
            p = norm_p(t)
            out["t_stat"] = t
            out["p_value"] = p
            out["significant"] = bool(p is not None and p < 0.05)
    return out


def spearman(xs, ys) -> dict:
    """Spearman 秩相关（IC），p 值用正态近似。"""
    x = np.asarray(xs, dtype="float64")
    y = np.asarray(ys, dtype="float64")
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    mask = np.isfinite(x) & np.isfinite(y)
    cnt = int(np.count_nonzero(mask))
    out = {"n": cnt, "ic": None, "ic_p": None}
    if cnt < 3:
        return out
    rx = pd.Series(x[mask]).rank().to_numpy(dtype="float64")
    ry = pd.Series(y[mask]).rank().to_numpy(dtype="float64")
    dx = rx - rx.mean()
    dy = ry - ry.mean()
    sxx = float(np.sum(dx * dx))
    syy = float(np.sum(dy * dy))
    if sxx <= 0 or syy <= 0:
        return out
    r = float(np.sum(dx * dy)) / math.sqrt(sxx * syy)
    out["ic"] = r
    if cnt > 2 and abs(r) < 1.0:
        t = r * math.sqrt((cnt - 2) / max(1.0 - r * r, 1e-12))
        out["ic_p"] = norm_p(t)
    return out


def mean_test(vals) -> dict:
    """单样本 t 检验（H0: 均值=0），p 用正态近似。"""
    a = _finite(vals)
    n = int(a.size)
    out = {"n": n, "mean": None, "median": None, "std": None, "t_stat": None, "p_value": None,
           "win_rate": None, "wins": 0}
    if n == 0:
        return out
    out["mean"] = float(a.mean())
    out["median"] = float(np.median(a))
    sd = float(a.std(ddof=1)) if n > 1 else 0.0
    out["std"] = sd
    out["wins"] = int(np.count_nonzero(a > 0))
    out["win_rate"] = 100.0 * out["wins"] / n
    if n > 1 and sd > 0:
        t = out["mean"] / (sd / math.sqrt(n))
        out["t_stat"] = t
        out["p_value"] = norm_p(t)
    return out


def wilson_ci(wins: int, n: int, z: float = 1.96):
    """二项比例的 Wilson 置信区间，返回百分数区间 [lo, hi]。"""
    if not n or n <= 0:
        return None
    p = max(0.0, min(1.0, float(wins) / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return [round(100.0 * lo, 1), round(100.0 * hi, 1)]


# ---------------------------------------------------------------- 分位分组
def _edges(x: pd.Series, n_buckets: int = N_BUCKETS):
    vals = _finite(x.to_numpy(dtype="float64"))
    if vals.size < max(n_buckets, 10):
        return None
    qs = np.linspace(0.0, 1.0, n_buckets + 1)
    edges = np.quantile(vals, qs)
    # 极端情况下分位边界重复（数据高度离散），去重后仍保留 n_buckets+1 长度
    return edges


def assign_buckets(x: pd.Series, edges) -> np.ndarray:
    """按分位边界把 x 映射到 0..n_buckets-1；非有限值 -> -1。"""
    a = np.asarray(x, dtype="float64")
    if edges is None:
        return np.full(a.shape, -1, dtype="int64")
    inner = np.asarray(edges[1:-1], dtype="float64")
    idx = np.searchsorted(inner, a, side="right")
    idx = np.clip(idx, 0, len(edges) - 2)
    idx = np.where(np.isfinite(a), idx, -1)
    return idx.astype("int64")


def bucket_stats(df, xcol: str = "dnet_z", ycol: str = "y",
                 n_buckets: int = N_BUCKETS) -> dict:
    """按 xcol 的窗口内分位分 n_buckets 组，统计 ycol 的次日收益特征。

    返回 ``{n_buckets, edges, rows, ic, ic_p, ic_n}``。
    Q1 = x 最小（净空单增加最多），Q5 = x 最大。
    """
    x = _col(df, xcol)
    y = _col(df, ycol)
    edges = _edges(x, n_buckets)
    rows = []
    if edges is None:
        for b in range(n_buckets):
            rows.append({
                "bucket": b + 1, "label": BUCKET_LABELS[b] if b < len(BUCKET_LABELS) else f"Q{b + 1}",
                "lo": None, "hi": None, "n": 0, "mean_ret_pct": None,
                "median_ret_pct": None, "win_rate": None, "t_stat": None, "p_value": None,
            })
        return {"n_buckets": n_buckets, "edges": None, "rows": rows, "ic": None, "ic_p": None, "ic_n": 0}

    bkt = assign_buckets(x, edges)
    for b in range(n_buckets):
        mask = bkt == b
        sub = y[mask]
        st = mean_test(sub.to_numpy(dtype="float64"))
        rows.append({
            "bucket": b + 1,
            "label": BUCKET_LABELS[b] if b < len(BUCKET_LABELS) else f"Q{b + 1}",
            "lo": None if b == 0 else num(edges[b], 4),
            "hi": None if b == n_buckets - 1 else num(edges[b + 1], 4),
            "n": st["n"],
            "mean_ret_pct": num(st["mean"], 4),
            "median_ret_pct": num(st["median"], 4),
            "win_rate": num(st["win_rate"], 1),
            "t_stat": num(st["t_stat"], 2),
            "p_value": num(st["p_value"], 4),
        })
    sp = spearman(x.to_numpy(dtype="float64"), y.to_numpy(dtype="float64"))
    return {
        "n_buckets": n_buckets,
        "edges": [num(e, 4) for e in edges],
        "rows": rows,
        "ic": num(sp["ic"], 4),
        "ic_p": num(sp["ic_p"], 4),
        "ic_n": sp["n"],
    }


# ---------------------------------------------------------------- 工具
def _next_weekday(date_str):
    """下一交易日近似：跳周末（节假日需要交易日历，这里只做工作日近似）。"""
    if not date_str:
        return None
    try:
        d = _dt.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None
    d = d + _dt.timedelta(days=1)
    while d.weekday() >= 5:
        d = d + _dt.timedelta(days=1)
    return d.isoformat()


def _last_valid(series):
    if series is None or len(series) == 0:
        return None
    s = _finite(pd.Series(series).to_numpy(dtype="float64"))
    return float(s[-1]) if s.size else None


def _impact(z, slope) -> str:
    """因子方向到多空的映射：用窗口内拟合到的经验斜率决定，而非先验假设。"""
    if z is None or slope is None:
        return "中性"
    try:
        v = float(z) * float(slope)
    except (TypeError, ValueError):
        return "中性"
    if not math.isfinite(v):
        return "中性"
    if v > 0.005:
        return "利多"
    if v < -0.005:
        return "利空"
    return "中性"


def _rolling_corr(df, xcol: str, ycol: str, window: int = ROLLING_CORR_WINDOW):
    x = _col(df, xcol)
    y = _col(df, ycol)
    if len(df) < 10:
        return None
    k = min(window, len(df))
    xx = x.tail(k).to_numpy(dtype="float64")
    yy = y.tail(k).to_numpy(dtype="float64")
    mask = np.isfinite(xx) & np.isfinite(yy)
    if int(np.count_nonzero(mask)) < 5:
        return None
    return float(np.corrcoef(xx[mask], yy[mask])[0, 1])


# ---------------------------------------------------------------- 主入口
def build_guidance(df, variety: str = "IF", member: str = config.DEFAULT_MEMBER) -> dict:
    """把最新一日的因子读数翻译成次日研判（契约 5.2.1）。"""
    empty_similar = {"n": 0, "mean_ret_pct": None, "win_rate": None,
                     "up_prob_ci": None, "last_examples": []}
    out = {
        "date": None,
        "for_date": None,
        "direction": DIRECTION_NEUTRAL,
        "score": 0.0,
        "confidence": CONF_LOW,
        "up_prob": None,
        "expected_ret_pct": None,
        "sample_n": 0,
        "drivers": [],
        "evidence": [],
        "caveats": [],
        "similar": dict(empty_similar),
    }
    if df is None or len(df) == 0:
        out["caveats"].append("无可用数据，无法研判")
        return out

    dates = [str(d) for d in df["date"].tolist()] if "date" in df.columns else []
    last_date = dates[-1] if dates else None
    out["date"] = last_date
    out["for_date"] = _next_weekday(last_date)

    y = _col(df, "y")
    x_dnet = _col(df, "dnet_z")
    x_net = _col(df, "net_z")
    x_pct = _col(df, "net_pctile")

    # 各因子最新读数（最新日缺失时退回最近一个有效值，并在 caveats 里说明）
    latest = {}
    for key, s in (("dnet_z", x_dnet), ("net_z", x_net), ("net_pctile", x_pct),
                   ("dnet_pctile", _col(df, "dnet_pctile")),
                   ("citic_net_pct", _col(df, "citic_net_pct"))):
        latest[key] = _last_valid(s)
    missing_latest = [k for k in ("dnet_z",) if latest.get(k) is None]
    if missing_latest:
        out["caveats"].append("最新一日净持仓因子缺失（可能当日持仓排名尚未披露），研判基于最近有效数据")

    # 模型
    models = {}
    for key in ("dnet_z", "net_z", "net_pctile"):
        models[key] = ols(_col(df, key).to_numpy(dtype="float64"),
                          y.to_numpy(dtype="float64"))
    primary = models["dnet_z"]

    # 分位组 + 当前读数落在哪一组
    bst = bucket_stats(df, "dnet_z", "y")
    edges_arr = None
    if bst.get("edges"):
        edges_arr = np.asarray([e if e is not None else np.nan for e in bst["edges"]],
                               dtype="float64")
    cur_bkt = None
    if latest.get("dnet_z") is not None and edges_arr is not None and np.isfinite(edges_arr).all():
        cur_bkt = int(assign_buckets(pd.Series([latest["dnet_z"]]), edges_arr)[0])
        if cur_bkt < 0:
            cur_bkt = None
    bkt_row = bst["rows"][cur_bkt] if (cur_bkt is not None and cur_bkt < len(bst["rows"])) else None

    # 期望收益 / 胜率：优先用历史同分位组（经验分布），样本不足时退回全窗口
    y_all = _finite(y.to_numpy(dtype="float64"))
    overall = mean_test(y_all)
    expected = None
    up_prob = None
    sample_n = 0
    if bkt_row is not None and (bkt_row.get("n") or 0) >= 10:
        expected = bkt_row.get("mean_ret_pct")
        up_prob = bkt_row.get("win_rate")
        sample_n = int(bkt_row.get("n") or 0)
    else:
        if overall["n"] >= 10:
            expected = num(overall["mean"], 4)
            up_prob = num(overall["win_rate"], 1)
            sample_n = int(overall["n"])
            if bst.get("rows") and cur_bkt is not None:
                out["caveats"].append(
                    f"所在分位组样本量不足（n={bkt_row.get('n') if bkt_row else 0}），退化为全窗口均值")
    out["expected_ret_pct"] = expected
    out["up_prob"] = up_prob
    out["sample_n"] = sample_n

    # ------------------------------------------------------------ 方向 / 评分
    model_exp = None
    if primary["slope"] is not None and latest.get("dnet_z") is not None:
        model_exp = primary["intercept"] + primary["slope"] * latest["dnet_z"]
    if expected is None and model_exp is not None:
        expected = num(model_exp, 4)
        out["expected_ret_pct"] = expected

    score = 0.0
    if expected is not None:
        # 以 0.3%（约 1/3 个日波动）为满仓刻度
        score = float(np.clip(expected / 0.3, -1.0, 1.0))
    out["score"] = num(score, 2)

    if expected is not None and expected <= -0.05:
        out["direction"] = DIRECTION_SHORT
    elif expected is not None and expected >= 0.05:
        out["direction"] = DIRECTION_LONG
    else:
        out["direction"] = DIRECTION_NEUTRAL

    # 置信度：同时要求幅度、样本量、分组均值显著性
    bkt_p = bkt_row.get("p_value") if bkt_row else None
    abs_e = abs(expected) if expected is not None else 0.0
    if bkt_row is None or sample_n < 10 or abs_e < 0.05:
        out["confidence"] = CONF_LOW
    elif abs_e >= 0.15 and sample_n >= 30 and bkt_p is not None and bkt_p < 0.10:
        out["confidence"] = CONF_HIGH
    elif abs_e >= 0.06 and sample_n >= 20:
        out["confidence"] = CONF_MID
    else:
        out["confidence"] = CONF_LOW

    # ------------------------------------------------------------ drivers
    corr_sum = 0.0
    for key in ("dnet_z", "net_z", "net_pctile"):
        c = models[key]["corr"]
        corr_sum += abs(c) if c is not None else 0.0
    drivers = []
    for key in ("dnet_z", "net_z", "net_pctile"):
        m = models[key]
        val = latest.get(key)
        if val is None:
            continue
        c = m["corr"]
        if c is not None and corr_sum > 0:
            weight = num(abs(c) / corr_sum, 2)
        elif c is not None:
            weight = 0.0
        else:
            weight = 0.0
        slope = m["slope"]
        impact = _impact(val, slope)
        if key == "dnet_z":
            pct = latest.get("dnet_pctile")
            desc = (f"净空单日变化 z={num(val, 2)}"
                    + (f"（滚动252日分位 {num(pct, 1)}%）" if pct is not None else "")
                    + (f"，经验斜率 {num(slope, 4)}，条件预期 {num(m['intercept'] + slope * val, 3)}%"
                       if slope is not None else "，斜率不可估计"))
        elif key == "net_z":
            desc = (f"净持仓水平 z={num(val, 2)}"
                    + (f"，经验斜率 {num(slope, 4)}" if slope is not None else "，斜率不可估计"))
        else:
            desc = (f"净持仓分位 {num(val, 1)}%"
                    + (f"，经验斜率 {num(slope, 4)}" if slope is not None else "，斜率不可估计"))
        drivers.append({
            "key": key,
            "name": FACTOR_META[key]["name"],
            "value": num(val, 4),
            "impact": impact,
            "weight": weight,
            "desc": desc,
        })
    out["drivers"] = drivers

    # ------------------------------------------------------------ evidence
    ev = []
    z = latest.get("dnet_z")
    pct = latest.get("dnet_pctile")
    if z is not None:
        part = f"净空单日变化 z={num(z, 2)}"
        if pct is not None:
            part += f"（滚动 252 日 {num(pct, 1)}% 分位）"
        if bkt_row is not None:
            part += (f"；历史同分位组次日平均收益 {num(bkt_row.get('mean_ret_pct'), 3)}%，"
                     f"上涨概率 {num(bkt_row.get('win_rate'), 1)}%（n={bkt_row.get('n')}）")
        ev.append(part)
    if primary["corr"] is not None:
        sig = "达到" if primary["significant"] else "未达"
        ev.append(
            f"窗口内 dnet_z 与次日收益相关系数 {num(primary['corr'], 4)}（n={primary['n']}），"
            f"OLS 斜率 {num(primary['slope'], 4)}，R²={num(primary['r2'], 4)}，"
            f"t={num(primary['t_stat'], 2)}，p={num(primary['p_value'], 4)}，{sig} 5% 显著水平")
    rc = _rolling_corr(df, "dnet_z", "y")
    if rc is not None and primary["corr"] is not None:
        same = (rc >= 0) == (primary["corr"] >= 0)
        ev.append(f"近 {min(ROLLING_CORR_WINDOW, len(df))} 日滚动相关系数 {num(rc, 4)}，"
                  f"方向与全窗口{'一致' if same else '相反'}")
    if model_exp is not None:
        rs = primary["resid_std"]
        ev.append(f"回归隐含次日预期 {num(model_exp, 4)}%"
                  + (f"（残差标准差 {num(rs, 3)}%）" if rs is not None else ""))
    out["evidence"] = ev

    # ------------------------------------------------------------ caveats
    cav = []
    if primary["r2"] is not None:
        cav.append(f"单因子解释力弱（R²={num(primary['r2'], 4)}，n={primary['n']}），"
                   "仅作概率参考，不可作为交易依据")
    cav.append("分位分组边界基于窗口内全样本分位（含后验信息），仅用于描述历史特征，不是实时信号")
    if sample_n and sample_n < 100:
        cav.append(f"样本量偏小（n={sample_n}），统计推断不稳定")
    if primary["p_value"] is not None and not primary["significant"]:
        cav.append(f"主因子 dnet_z 未达 5% 显著水平（p={num(primary['p_value'], 4)}），"
                   "方向判断的统计基础薄弱")
    cav.append("会员持仓排名为每日收盘后披露（收盘后才有数据，无法用于当日交易）；"
               "披露口径在样本期内有过调整，跨期比较需谨慎")
    out["caveats"] = cav

    # ------------------------------------------------------------ similar
    similar = dict(empty_similar)
    if bkt_row is not None and (bkt_row.get("n") or 0) > 0:
        n_b = int(bkt_row["n"])
        wr = bkt_row.get("win_rate")
        wins = int(round((wr or 0.0) * n_b / 100.0))
        similar = {
            "n": n_b,
            "mean_ret_pct": num(bkt_row.get("mean_ret_pct"), 4),
            "win_rate": num(wr, 1),
            "up_prob_ci": wilson_ci(wins, n_b),
            "last_examples": [],
        }
        if edges_arr is not None and cur_bkt is not None:
            bkt_all = assign_buckets(x_dnet, edges_arr)
            mask = (bkt_all == cur_bkt) & np.isfinite(y.to_numpy(dtype="float64"))
            sub = df.loc[mask, ["date", "y"]].copy()
            sub = sub.sort_values("date", ascending=False).head(5)
            examples = []
            for _, row in sub.iterrows():
                yr = num(row["y"], 3)
                if yr is None:
                    continue
                examples.append({"date": str(row["date"]), "next_ret_pct": yr})
            similar["last_examples"] = examples
    out["similar"] = similar
    return out
