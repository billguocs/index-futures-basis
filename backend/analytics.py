# -*- coding: utf-8 -*-
"""统计分析层：净持仓因子、滚动分位、OLS 拟合、分位胜率、无未来函数回测。

契约：``contract/API_CONTRACT.md`` 第 4.2 / 5.2 / 5.3 / 5.4 / 6 节。

统计口径（硬性）
----------------
* 预测目标 ``y_t = ln(close_{t+1}/close_t) * 100``（次日对数收益，来自指数收盘价）。
* 标准化 ``z`` 与分位 ``pctile`` 全部用**滚动**窗口：252 日、``min_periods=60``；
  先在全历史上计算（保证窗口起点就有历史），再切出分析窗口，绝无未来函数。
* OLS 手写：``se(b) = sqrt(sigma2/Sxx)``、``sigma2 = RSS/(n-2)``；
  ``p = 2*(1-Phi(|t|)) = erfc(|t|/sqrt(2))``（见 :mod:`backend.signal`）。
* 回测：滚动分位 ≤20% 做空、≥80% 做多、其余空仓，次日结算，换仓成本 ``cost_bp``。
* 输出一律不含 NaN / Infinity（转 None）。
"""
from __future__ import annotations

import math
import sqlite3

import numpy as np
import pandas as pd

import config

# 允许 "import backend.analytics" 与 "python backend/analytics.py" 两种方式
try:  # pragma: no cover - 取决于导入方式
    from . import signal as sig
except ImportError:  # pragma: no cover
    import importlib.util as _ilu
    import os as _os

    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "signal.py")
    _spec = _ilu.spec_from_file_location("fc_signal", _p)
    sig = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(sig)  # type: ignore[union-attr]

WINDOW = 252          # 滚动 z / 分位的窗口长度（契约 6.1）
MIN_PERIODS = 60      # 滚动窗口最少样本
MIN_ROWS = 60         # 少于该行数直接判定"数据不足"
COST_BP = 0.5         # 换仓成本（bp）
LOW_PCT, HIGH_PCT = 20.0, 80.0   # 回测分位阈值
BACKTEST_RULE = "滚动252日分位：≤20% 做空、≥80% 做多、其余空仓，次日收盘结算"

MODEL_DEFS = [
    ("dnet_z", "净空单日变化(标准化)"),
    ("net_z", "净持仓水平(标准化)"),
    ("net_pctile", "净持仓历史分位"),
]

_NUM_COLS = [
    "index_close", "main_close", "basis", "basis_pct", "open_interest",
    "top20_long", "top20_short", "top20_net",
    "citic_long", "citic_short", "citic_net",
]


# ---------------------------------------------------------------- 基础工具
def _empty_dashboard():
    return {"ok": False, "error": "数据不足", "empty": True}


def _query_df(conn, sql: str, params=()) -> pd.DataFrame:
    """执行查询并返回 DataFrame；空结果也保留列名。"""
    cur = conn.execute(sql, tuple(params))
    cols = [d[0] for d in (cur.description or [])]
    rows = [tuple(r) for r in cur.fetchall()]
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})
    return pd.DataFrame(rows, columns=cols)


def _num(v, nd: int = 4):
    return sig.num(v, nd)


def _num_int(v):
    return sig.num_int(v)


def _to_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _kv(conn, key: str):
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:  # pragma: no cover - 表结构由 db.connect 保证
        return None


# ---------------------------------------------------------------- 特征派生
def rolling_z(series, window: int = WINDOW, min_periods: int = MIN_PERIODS) -> pd.Series:
    """滚动 z 值（总体标准差 ddof=0；标准差为 0 -> NaN）。"""
    s = pd.to_numeric(pd.Series(series), errors="coerce").astype("float64")
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std(ddof=0)
    z = (s - mean) / std.replace(0.0, np.nan)
    return z


def rolling_pctile(series, window: int = WINDOW, min_periods: int = MIN_PERIODS) -> pd.Series:
    """滚动分位（0-100）：当前值在自身窗口（含当日）中的百分位。

    向量化实现：先算窗口尾部的 sliding-window 部分，再用小循环补前 ``window-1`` 行
    （min_periods=60，因此不足 60 个有效值处为 NaN）。
    """
    raw = pd.Series(series)
    s = pd.to_numeric(raw, errors="coerce").astype("float64")
    a = s.to_numpy(dtype="float64")
    n = a.size
    out = np.full(n, np.nan, dtype="float64")
    if n == 0:
        return pd.Series(out, index=raw.index)

    head = min(window - 1, n)
    for i in range(head):
        w = a[: i + 1]
        w = w[np.isfinite(w)]
        if w.size >= min_periods and np.isfinite(a[i]):
            out[i] = 100.0 * float(np.count_nonzero(w <= a[i])) / float(w.size)

    if n >= window:
        from numpy.lib.stride_tricks import sliding_window_view

        sw = sliding_window_view(a, window)          # (n-window+1, window)
        last = sw[:, -1]
        finite = np.isfinite(sw)
        cnt = finite.sum(axis=1)
        le = np.where(finite, sw <= last[:, None], False).sum(axis=1)
        good = np.isfinite(last) & (cnt >= min_periods) & (cnt > 0)
        with np.errstate(invalid="ignore", divide="ignore"):
            vals = 100.0 * le / np.maximum(cnt, 1)
        vals = np.where(good, vals, np.nan)
        out[window - 1:] = vals
    return pd.Series(out, index=raw.index)


def derive_features(df: pd.DataFrame) -> pd.DataFrame:
    """在原始日频表上派生因子列（只使用 ≤t 的数据，末行 y 为下一步收益）。"""
    df = df.copy()
    close = pd.to_numeric(df["index_close"], errors="coerce").astype("float64")
    oi = pd.to_numeric(df["open_interest"], errors="coerce").astype("float64")
    oi = oi.replace(0.0, np.nan)
    net = pd.to_numeric(df["citic_net"], errors="coerce").astype("float64")

    df["index_close"] = close
    df["index_ret_pct"] = close.pct_change() * 100.0
    nxt = close.shift(-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        df["y"] = (np.log(nxt) - np.log(close)) * 100.0

    df["citic_net_pct"] = net / oi * 100.0
    df["dnet"] = net.diff()
    df["dnet_pct_of_oi"] = df["dnet"] / oi * 100.0
    df["net_z"] = rolling_z(df["citic_net_pct"])
    df["net_pctile"] = rolling_pctile(df["citic_net_pct"])
    df["dnet_z"] = rolling_z(df["dnet_pct_of_oi"])
    df["dnet_pctile"] = rolling_pctile(df["dnet_pct_of_oi"])
    # 快照字段别名（契约 5.2 snapshot 用 citic_net_z / citic_net_pctile 命名）
    df["citic_net_z"] = df["net_z"]
    df["citic_net_pctile"] = df["net_pctile"]
    return df


# ---------------------------------------------------------------- 数据装载
def _load_frame(conn, variety: str, member: str) -> pd.DataFrame:
    info = config.VARIETY_INFO[variety]
    idx_code = info["index_code"]
    if member == config.DEFAULT_MEMBER:
        sql = """
            SELECT v.date AS date, v.main_contract AS main_contract, v.main_close AS main_close,
                   v.basis AS basis, v.basis_pct AS basis_pct, v.open_interest AS open_interest,
                   v.top20_long AS top20_long, v.top20_short AS top20_short, v.top20_net AS top20_net,
                   v.citic_long AS citic_long, v.citic_short AS citic_short, v.citic_net AS citic_net,
                   i.close AS index_close
            FROM variety_daily v
            LEFT JOIN index_daily i ON i.code = ? AND i.date = v.date
            WHERE v.variety = ?
            ORDER BY v.date
        """
        df = _query_df(conn, sql, (idx_code, variety))
        numeric = df["citic_net"].apply(pd.to_numeric, errors="coerce") if "citic_net" in df else None
        if df.empty or (numeric is not None and numeric.notna().sum() == 0):
            # variety_daily 的中信列尚未回填时，退回 member_daily（同一口径）
            df2 = _load_member_frame(conn, variety, member, idx_code)
            if not df2.empty:
                df = df2
        return _prep_frame(df)
    return _prep_frame(_load_member_frame(conn, variety, member, idx_code))


def _load_member_frame(conn, variety: str, member: str, idx_code: str) -> pd.DataFrame:
    sql = """
        SELECT v.date AS date, v.main_contract AS main_contract, v.main_close AS main_close,
               v.basis AS basis, v.basis_pct AS basis_pct, v.open_interest AS open_interest,
               v.top20_long AS top20_long, v.top20_short AS top20_short, v.top20_net AS top20_net,
               m.long_pos AS citic_long, m.short_pos AS citic_short, m.net_pos AS citic_net,
               i.close AS index_close
        FROM variety_daily v
        LEFT JOIN member_daily m
               ON m.date = v.date AND m.variety = v.variety AND m.member = ?
        LEFT JOIN index_daily i ON i.code = ? AND i.date = v.date
        WHERE v.variety = ?
        ORDER BY v.date
    """
    return _query_df(conn, sql, (member, idx_code, variety))


def _prep_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["date"] = out["date"].astype(str)
    for c in _NUM_COLS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
        else:
            out[c] = np.nan
    keep = out["index_close"].notna() & (out["index_close"] > 0)
    out = out.loc[keep]
    if out.empty:
        return pd.DataFrame()
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return out


# ---------------------------------------------------------------- 各分析块
def _snapshot(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    idx_close = _num(last.get("index_close"), 2)
    main_close = _num(last.get("main_close"), 2)
    basis = _num(last.get("basis"), 2)
    if basis is None and main_close is not None and idx_close is not None:
        basis = _num(main_close - idx_close, 2)
    basis_pct = _num(last.get("basis_pct"), 2)
    if basis_pct is None and basis is not None and idx_close:
        basis_pct = _num(basis / idx_close * 100.0, 2)
    top20_net = _num_int(last.get("top20_net"))
    citic_net = _num_int(last.get("citic_net"))
    share = None
    if citic_net is not None and top20_net not in (None, 0):
        share = _num(citic_net / top20_net * 100.0, 1)
    return {
        "date": str(last.get("date")),
        "index_close": idx_close,
        "index_chg_pct": _num(last.get("index_ret_pct"), 2),
        "main_contract": (None if pd.isna(last.get("main_contract")) else str(last.get("main_contract"))) or None,
        "main_close": main_close,
        "basis": basis,
        "basis_pct": basis_pct,
        "citic_long": _num_int(last.get("citic_long")),
        "citic_short": _num_int(last.get("citic_short")),
        "citic_net": citic_net,
        "citic_net_pct": _num(last.get("citic_net_pct"), 2),
        "citic_net_z": _num(last.get("net_z"), 2),
        "citic_net_pctile": _num(last.get("net_pctile"), 1),
        "dnet": _num_int(last.get("dnet")),
        "dnet_pct_of_oi": _num(last.get("dnet_pct_of_oi"), 2),
        "dnet_z": _num(last.get("dnet_z"), 2),
        "dnet_pctile": _num(last.get("dnet_pctile"), 1),
        "top20_long": _num_int(last.get("top20_long")),
        "top20_short": _num_int(last.get("top20_short")),
        "top20_net": top20_net,
        "market_oi": _num_int(last.get("open_interest")),
        "citic_share_of_top20": share,
    }


def _series_block(win: pd.DataFrame) -> dict:
    def col(name, nd=4):
        if name not in win.columns:
            return [None] * len(win)
        return [_num(v, nd) for v in win[name].tolist()]

    def icol(name):
        if name not in win.columns:
            return [None] * len(win)
        return [_num_int(v) for v in win[name].tolist()]

    return {
        "date": [str(d) for d in win["date"].tolist()],
        "index_close": col("index_close"),
        "index_ret_pct": col("index_ret_pct"),
        "citic_net": icol("citic_net"),
        "citic_net_pct": col("citic_net_pct"),
        "citic_long": icol("citic_long"),
        "citic_short": icol("citic_short"),
        "market_oi": icol("open_interest"),
        "main_close": col("main_close"),
        "basis_pct": col("basis_pct"),
        "dnet_pct_of_oi": col("dnet_pct_of_oi"),
        "dnet_z": col("dnet_z"),
        "net_pctile": col("net_pctile"),
    }


def _corr(a, b):
    x = np.asarray(a, dtype="float64")
    y = np.asarray(b, dtype="float64")
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(np.count_nonzero(mask))
    if n < 3:
        return None, n
    xx = x[mask]
    yy = y[mask]
    if float(xx.std()) == 0.0 or float(yy.std()) == 0.0:
        return None, n
    return float(np.corrcoef(xx, yy)[0, 1]), n


def _fit_block(win: pd.DataFrame) -> dict:
    y = pd.to_numeric(win["y"], errors="coerce").to_numpy(dtype="float64")
    x_dnet = pd.to_numeric(win["dnet_z"], errors="coerce").to_numpy(dtype="float64")
    x_ret = pd.to_numeric(win["index_ret_pct"], errors="coerce").to_numpy(dtype="float64")

    models = []
    for key, name in MODEL_DEFS:
        x = pd.to_numeric(win[key], errors="coerce").to_numpy(dtype="float64")
        r = sig.ols(x, y)
        models.append({
            "key": key,
            "name": name,
            "n": r["n"],
            "corr": _num(r["corr"]),
            "slope": _num(r["slope"]),
            "intercept": _num(r["intercept"]),
            "r2": _num(r["r2"]),
            "t_stat": _num(r["t_stat"], 2),
            "p_value": _num(r["p_value"], 4),
            "significant": bool(r["significant"]),
        })

    corr_contemp, _ = _corr(x_dnet, x_ret)
    corr_pred, _ = _corr(x_dnet, y)

    max_lag = int(getattr(config, "MAX_LAG", 3))
    lag_corr = []
    T = len(win)
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a = x_dnet[: T - lag] if lag else x_dnet
            b = y[lag:]
        else:
            a = x_dnet[-lag:]
            b = y[: T + lag]
        c, n = _corr(a, b)
        lag_corr.append({"lag": lag, "corr": _num(c), "n": n})

    # 滚动相关（60 日）
    rc_win = int(getattr(config, "ROLLING_CORR_WINDOW", 60))
    rc_min = max(20, rc_win // 3)
    xs = pd.Series(x_dnet)
    ys = pd.Series(y)
    rs = pd.Series(x_ret)
    rc_pred = xs.rolling(rc_win, min_periods=rc_min).corr(ys)
    rc_cont = xs.rolling(rc_win, min_periods=rc_min).corr(rs)
    rolling_corr = {
        "date": [str(d) for d in win["date"].tolist()],
        "contemporaneous": [_num(v) for v in rc_cont.tolist()],
        "predictive": [_num(v) for v in rc_pred.tolist()],
    }

    # 年度稳定性
    years = []
    year_key = win["date"].astype(str).str.slice(0, 4)
    for year, g in win.groupby(year_key):
        xx = pd.to_numeric(g["dnet_z"], errors="coerce").to_numpy(dtype="float64")
        yy = pd.to_numeric(g["y"], errors="coerce").to_numpy(dtype="float64")
        c, n = _corr(xx, yy)
        if n >= 20:
            years.append({"year": str(year), "corr_predictive": _num(c), "n": n})
    years.sort(key=lambda d: d["year"])

    # 散点 + 回归线
    py = models[0]
    mask = np.isfinite(x_dnet) & np.isfinite(y)
    sx = x_dnet[mask]
    sy = y[mask]
    line = {"x": [], "y": []}
    if sx.size >= 3 and py["slope"] is not None and py["intercept"] is not None:
        x0 = float(np.min(sx))
        x1 = float(np.max(sx))
        line = {
            "x": [_num(x0, 3), _num(x1, 3)],
            "y": [_num(py["intercept"] + py["slope"] * x0, 4),
                  _num(py["intercept"] + py["slope"] * x1, 4)],
        }

    return {
        "n": int(T),
        "sample": {"start": str(win["date"].iloc[0]), "end": str(win["date"].iloc[-1])},
        "corr_contemporaneous": _num(corr_contemp),
        "corr_predictive": _num(corr_pred),
        "models": models,
        "primary": "dnet_z",
        "lag_corr": lag_corr,
        "rolling_corr": rolling_corr,
        "stability": {"years": years},
        "scatter": {
            "x": [_num(v, 3) for v in sx.tolist()],
            "y": [_num(v, 3) for v in sy.tolist()],
            "line": line,
        },
        "x_label": "净空单日变化(滚动252日标准化)",
        "y_label": "次日指数对数收益(%)",
    }


def _buckets_block(win: pd.DataFrame) -> dict:
    bst = sig.bucket_stats(win, "dnet_z", "y")
    return {
        "x_label": "净空单日变化分位组",
        "n_buckets": bst["n_buckets"],
        "ic": bst["ic"],
        "ic_p": bst["ic_p"],
        "rows": bst["rows"],
    }


def _backtest_block(win: pd.DataFrame, cost_bp: float = COST_BP) -> dict:
    pct = pd.to_numeric(win["dnet_pctile"], errors="coerce").to_numpy(dtype="float64")
    y = pd.to_numeric(win["y"], errors="coerce").to_numpy(dtype="float64")
    dates = win["date"].astype(str).to_numpy()
    mask = np.isfinite(pct) & np.isfinite(y)
    empty_metrics = {
        "cum_return": None, "annual_return": None, "max_drawdown": None,
        "sharpe": None, "hit_rate": None, "trades": None, "exposure": None,
        "benchmark_cum": None, "n": 0,
    }
    if int(mask.sum()) < 20:
        return {"rule": BACKTEST_RULE, "cost_bp": cost_bp,
                "equity": {"date": [], "nav": [], "nav_bh": []},
                "metrics": empty_metrics}

    idx = np.where(mask)[0]
    pos = np.where(pct[idx] <= LOW_PCT, -1.0,
                   np.where(pct[idx] >= HIGH_PCT, 1.0, 0.0))
    prev = np.concatenate(([0.0], pos[:-1]))
    turnover = np.abs(pos - prev)
    strat_ret = pos * (y[idx] / 100.0) - turnover * (cost_bp / 10000.0)
    nav = np.cumprod(1.0 + strat_ret)
    nav_bh = np.cumprod(1.0 + y[idx] / 100.0)
    n = int(idx.size)

    cum = float(nav[-1]) - 1.0
    annual = None
    if n > 0 and (1.0 + cum) > 0:
        annual = math.pow(1.0 + cum, 252.0 / n) - 1.0
    peak = np.maximum.accumulate(nav)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = nav / peak - 1.0
    max_dd = float(np.min(dd)) if n else None

    sd = float(np.std(strat_ret, ddof=1)) if n > 1 else 0.0
    sharpe = None
    if n > 1 and sd > 0:
        sharpe = float(np.mean(strat_ret) * math.sqrt(252.0) / sd)

    active = pos != 0.0
    hit = None
    if int(np.count_nonzero(active)) > 0:
        hit = 100.0 * float(np.count_nonzero(strat_ret[active] > 0)) / float(np.count_nonzero(active))

    return {
        "rule": BACKTEST_RULE,
        "cost_bp": cost_bp,
        "equity": {
            "date": [str(dates[i]) for i in idx.tolist()],
            "nav": [_num(v) for v in nav.tolist()],
            "nav_bh": [_num(v) for v in nav_bh.tolist()],
        },
        "metrics": {
            "cum_return": _num(cum),
            "annual_return": _num(annual),
            "max_drawdown": _num(max_dd),
            "sharpe": _num(sharpe, 2),
            "hit_rate": _num(hit, 1),
            "trades": int(np.count_nonzero(turnover > 0)),
            "exposure": _num(float(np.count_nonzero(active)) / n),
            "benchmark_cum": _num(float(nav_bh[-1]) - 1.0),
            "n": n,
        },
    }


# ---------------------------------------------------------------- 对外接口
def compute_dashboard(conn, variety: str = "IF", lookback: int = 720,
                      member: str = config.DEFAULT_MEMBER) -> dict:
    """契约 5.2：单品种完整看板数据。数据不足时返回约定空体，绝不抛异常。"""
    variety = str(variety or "IF").upper()
    if variety not in config.VARIETIES:
        variety = "IF"
    lookback = _to_int(lookback, config.DEFAULT_LOOKBACK)
    member = member or config.DEFAULT_MEMBER
    info = config.VARIETY_INFO[variety]

    try:
        df = _load_frame(conn, variety, member)
    except Exception:  # pragma: no cover - 任何 SQL 异常都降级为空
        return _empty_dashboard()
    if df is None or len(df) < MIN_ROWS:
        return _empty_dashboard()

    df = derive_features(df)
    if lookback and lookback > 0:
        win = df.tail(lookback).reset_index(drop=True)
    else:
        win = df.reset_index(drop=True)
    if len(win) < 10:
        return _empty_dashboard()

    warnings = []
    starts = win["date"].astype(str).tolist()
    ends = df["date"].astype(str).tolist()[-1]
    break_date = _kv(conn, "member_naming_break_date")
    if break_date and break_date >= starts[0] and break_date <= ends:
        warnings.append(f"{break_date} 起会员持仓披露口径有变化，跨期比较需谨慎")
    null_net = int(df["citic_net"].isna().sum()) if "citic_net" in df else 0
    if null_net > 0:
        pct = null_net * 100.0 / max(len(df), 1)
        warnings.append(
            f"citic_net 有 {null_net} 天缺失（{pct:.1f}%，ccpm 缺文件日），滚动统计样本相应减少")

    fit = _fit_block(win)
    primary = fit["models"][0]
    if primary["p_value"] is not None and not primary["significant"]:
        warnings.append(f"主因子 dnet_z 未达统计显著（p={primary['p_value']}），结论仅作参考")
    if lookback and len(df) < lookback:
        warnings.append(f"请求窗口 {lookback} 日，实际可用仅 {len(df)} 日")
    if member != config.DEFAULT_MEMBER:
        warnings.append(f"当前分析会员为 {member}（默认 {config.DEFAULT_MEMBER}）")
    gaps = 0
    dts = pd.to_datetime(pd.Series(df["date"]), errors="coerce")
    delta = dts.diff().dt.days
    gaps = int((delta > 10).sum())
    if gaps > 0:
        warnings.append(f"数据存在 {gaps} 处超过 10 个自然日的缺口，次日收益会跨越缺口")

    dashboard = {
        "ok": True,
        "error": None,
        "empty": False,
        "variety": variety,
        "variety_name": info["name"],
        "index_code": info["index_code"],
        "index_name": info["index_name"],
        "member": member,
        "lookback": lookback,
        "as_of": str(df["date"].iloc[-1]),
        "window": {
            "start": str(win["date"].iloc[0]),
            "end": str(win["date"].iloc[-1]),
            "n": int(len(win)),
        },
        "snapshot": _snapshot(df),
        "series": _series_block(win),
        "fit": fit,
        "buckets": _buckets_block(win),
        "backtest": _backtest_block(win),
        "guidance": sig.build_guidance(win, variety=variety, member=member),
        "ranks": compute_ranks(conn, str(df["date"].iloc[-1]), variety, member),
        "warnings": warnings,
    }
    return dashboard


_RANK_TYPE_ALIAS = {"long": ("long",), "short": ("short",),
                    "volume": ("vol", "volume")}


def compute_ranks(conn, date: str | None, variety: str,
                  member: str = config.DEFAULT_MEMBER,
                  highlight: str | None = None) -> dict:
    """契约 5.3：指定交易日某品种的主力合约前 20 龙虎榜 + 高亮会员汇总。

    ``member`` 与 ``highlight`` 同义（api.py 传 ``member=``，契约文字用 ``highlight``）。
    """
    variety = str(variety or "IF").upper()
    if variety not in config.VARIETIES:
        variety = "IF"
    highlight = highlight or member or config.DEFAULT_MEMBER

    used_date = str(date) if date else None
    if not used_date or not _rank_rows(conn, used_date, variety):
        latest = _latest_rank_date(conn, variety)
        if not latest:
            return {"ok": False, "error": "数据不足", "empty": True,
                    "date": used_date, "variety": variety}
        used_date = latest

    contracts = [r[0] for r in conn.execute(
        "SELECT DISTINCT contract FROM member_rank WHERE date=? AND variety=? ORDER BY contract",
        (used_date, variety)).fetchall()]
    if not contracts:
        return {"ok": False, "error": "数据不足", "empty": True,
                "date": used_date, "variety": variety}

    main_contract = _main_contract(conn, used_date, variety, contracts)
    lists = {}
    # 契约 5.3 的键是 volume，库里 rank_type 落库为 'vol'（db.py 文档口径）
    for rtype in ("long", "short", "volume"):
        db_types = _RANK_TYPE_ALIAS.get(rtype, (rtype,))
        marks = ",".join("?" * len(db_types))
        rows = conn.execute(
            "SELECT rank, member, position, change FROM member_rank "
            "WHERE date=? AND variety=? AND contract=? "
            f"AND rank_type IN ({marks}) ORDER BY rank",
            (used_date, variety, main_contract, *db_types)).fetchall()
        lists[rtype] = [{
            "rank": int(r[0]),
            "member": r[1],
            "position": _num_int(r[2]),
            "change": _num_int(r[3]),
        } for r in rows]

    hl_row = _highlight_row(conn, used_date, variety, highlight)
    return {
        "ok": True,
        "error": None,
        "empty": False,
        "date": used_date,
        "variety": variety,
        "long": lists["long"],
        "short": lists["short"],
        "volume": lists["volume"],
        "highlight": highlight,
        "highlight_row": {
            "member": highlight,
            "long": hl_row.get("long"),
            "short": hl_row.get("short"),
            "net": hl_row.get("net"),
        },
        "contracts": contracts,
    }


def _rank_rows(conn, date, variety) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM member_rank WHERE date=? AND variety=?",
            (date, variety)).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:  # pragma: no cover
        return 0


def _latest_rank_date(conn, variety):
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM member_rank WHERE variety=?", (variety,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:  # pragma: no cover
        return None


def _main_contract(conn, date, variety, contracts):
    try:
        row = conn.execute(
            "SELECT contract FROM fut_daily WHERE date=? AND variety=? "
            "ORDER BY COALESCE(open_interest, 0) DESC LIMIT 1",
            (date, variety)).fetchone()
        if row and row[0] in contracts:
            return row[0]
    except sqlite3.Error:  # pragma: no cover
        pass
    row = conn.execute(
        "SELECT contract, COALESCE(SUM(position), 0) AS s FROM member_rank "
        "WHERE date=? AND variety=? AND rank_type IN ('long','short') "
        "GROUP BY contract ORDER BY s DESC LIMIT 1",
        (date, variety)).fetchone()
    if row and row[0]:
        return row[0]
    return contracts[0]


def _highlight_row(conn, date, variety, member) -> dict:
    row = conn.execute(
        "SELECT long_pos, short_pos, net_pos FROM member_daily "
        "WHERE date=? AND variety=? AND member=?", (date, variety, member)).fetchone()
    if row is not None:
        long_pos = _num_int(row[0])
        short_pos = _num_int(row[1])
        net_pos = _num_int(row[2])
        if net_pos is None and long_pos is not None and short_pos is not None:
            net_pos = long_pos - short_pos
        return {"long": long_pos, "short": short_pos, "net": net_pos}
    agg = conn.execute(
        "SELECT rank_type, COALESCE(SUM(position), 0) FROM member_rank "
        "WHERE date=? AND variety=? AND member=? AND rank_type IN ('long','short') "
        "GROUP BY rank_type", (date, variety, member)).fetchall()
    vals = {r[0]: _num_int(r[1]) for r in agg}
    long_pos = vals.get("long")
    short_pos = vals.get("short")
    net_pos = None
    if long_pos is not None and short_pos is not None:
        net_pos = long_pos - short_pos
    return {"long": long_pos, "short": short_pos, "net": net_pos}


def compute_members(conn, variety: str = "IF", lookback: int = 720,
                    members: list[str] | None = None) -> list[dict]:
    """契约 4.2 / 5.4：会员横向对比（返回 list[dict]，api.py 包成 5.4 的响应体）。"""
    variety = str(variety or "IF").upper()
    if variety not in config.VARIETIES:
        variety = "IF"
    lookback = _to_int(lookback, config.DEFAULT_LOOKBACK)
    info = config.VARIETY_INFO[variety]
    idx_code = info["index_code"]

    base = _query_df(
        conn, "SELECT date, open_interest FROM variety_daily WHERE variety=? ORDER BY date",
        (variety,))
    if base.empty:
        return []
    idx = _query_df(
        conn, "SELECT date, close FROM index_daily WHERE code=? ORDER BY date", (idx_code,))
    base["date"] = base["date"].astype(str)
    close_map = dict(zip(idx["date"].astype(str).tolist(), idx["close"].tolist())) if not idx.empty else {}
    base["index_close"] = base["date"].map(close_map)
    base["open_interest"] = pd.to_numeric(base["open_interest"], errors="coerce")
    base["index_close"] = pd.to_numeric(base["index_close"], errors="coerce")
    base = base[base["index_close"].notna() & (base["index_close"] > 0)].reset_index(drop=True)
    if base.empty:
        return []
    close = base["index_close"].astype("float64")
    base["y"] = (np.log(close.shift(-1)) - np.log(close)) * 100.0

    available = [r[0] for r in conn.execute(
        "SELECT member, COUNT(*) c FROM member_daily WHERE variety=? "
        "GROUP BY member ORDER BY c DESC", (variety,)).fetchall()]
    if members:
        wanted = [m for m in members if m in available] or list(members)
    else:
        wanted = []
        for m in list(config.TRACK_MEMBERS) + [config.DEFAULT_MEMBER]:
            if m in available and m not in wanted:
                wanted.append(m)
        if not wanted:
            wanted = available[:20]
    if not wanted:
        return []

    marks = ",".join("?" * len(wanted))
    mrows = _query_df(
        conn,
        f"SELECT date, member, long_pos, short_pos, net_pos FROM member_daily "
        f"WHERE variety=? AND member IN ({marks})",
        (variety, *wanted))
    if mrows.empty:
        return []
    mrows["date"] = mrows["date"].astype(str)
    mrows["net_pos"] = pd.to_numeric(mrows["net_pos"], errors="coerce")
    by_member = {m: g.set_index("date")["net_pos"] for m, g in mrows.groupby("member")}

    n_total = len(base)
    if lookback and lookback > 0:
        win_start = max(0, n_total - lookback)
    else:
        win_start = 0
    oi_all = base["open_interest"].astype("float64")
    oi_all = oi_all.replace(0.0, np.nan)
    y_win = base["y"].iloc[win_start:]

    rows = []
    for m in wanted:
        series = by_member.get(m)
        if series is None:
            continue
        net = base["date"].map(series).astype("float64")
        dnet = net.diff()
        dnet_pct = dnet / oi_all * 100.0
        z = rolling_z(dnet_pct)
        z_win = z.iloc[win_start:]
        r = sig.ols(z_win.to_numpy(dtype="float64"), y_win.to_numpy(dtype="float64"))
        valid = net.dropna()
        net_latest = float(valid.iloc[-1]) if len(valid) else None
        oi_latest = None
        if net_latest is not None:
            idx_latest = valid.index[-1]
            oi_latest = oi_all.loc[idx_latest]
        net_pct_latest = None
        if net_latest is not None and oi_latest is not None and np.isfinite(oi_latest) and oi_latest != 0:
            net_pct_latest = net_latest / float(oi_latest) * 100.0
        net_avg20 = float(valid.tail(20).mean()) if len(valid) else None
        net_change20 = None
        if len(valid) >= 21:
            net_change20 = float(valid.iloc[-1]) - float(valid.iloc[-21])
        rows.append({
            "member": m,
            "net_latest": _num_int(net_latest),
            "net_pct_latest": _num(net_pct_latest, 2),
            "net_avg20": _num(net_avg20),
            "net_change20": _num(net_change20),
            "pred_corr": _num(r["corr"], 4),
            "n": int(r["n"]),
            "rank": None,
        })

    rows.sort(key=lambda d: (d["pred_corr"] is None, d["pred_corr"] if d["pred_corr"] is not None else 0.0))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows
