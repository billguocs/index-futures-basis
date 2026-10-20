# -*- coding: utf-8 -*-
"""离线单测：合成内存 SQLite -> 全链路验证 analytics / signal。

运行方式（不需要网络）::

    python tests/test_analytics.py
    python -m unittest tests.test_analytics -v

覆盖点：
1. 空库不抛异常、返回契约约定的空体；
2. 无未来函数：截断 t+1 之后的数据，t 日因子读数必须逐位不变；
3. 滚动分位 / OLS / 分位胜率 / 回测的手工核对；
4. 输出可 ``json.dumps(..., allow_nan=False)``，无 NaN / Infinity；
5. guidance 字段齐全、取值域合法。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from backend import db  # noqa: E402
from backend import analytics  # noqa: E402
from backend import signal as sig  # noqa: E402

VARIETY = "IF"
INDEX_CODE = "000300"
MEMBER = "中信期货"


# ---------------------------------------------------------------- 合成数据
def _weekdays(start: str, n: int) -> list[str]:
    d = dt.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def build_db(n: int = 320, seed: int = 11, start: str = "2023-01-02"):
    """构造带真实结构的内存库：index_daily / variety_daily / fut_daily /
    member_rank / member_daily。因子与次日收益之间植入**负相关**（便于断言方向）。"""
    conn = db.connect(":memory:")
    rng = np.random.default_rng(seed)
    dates = _weekdays(start, n)

    e = rng.normal(0.0, 1.0, n)
    z = (e - e.mean()) / (e.std() or 1.0)
    y = -0.35 * z + rng.normal(0.0, 0.8, n)       # 次日对数收益(%)
    close = np.empty(n, dtype="float64")
    close[0] = 4000.0
    for i in range(n - 1):
        close[i + 1] = close[i] * math.exp(y[i] / 100.0)

    oi = (800000 + rng.integers(-20000, 20000, n)).astype("int64")
    dnet = np.round(z * 2500.0).astype("int64")
    net = -90000 + np.cumsum(dnet)
    long_pos = np.round(0.35 * oi + net / 2.0).astype("int64")
    short_pos = long_pos - net
    main_close = close + 5.0
    basis = main_close - close

    # index_daily：四个指数都灌一份，主用沪深300
    idx_rows = []
    for code in ("000300", "000016", "000905", "000852"):
        for i, d in enumerate(dates):
            idx_rows.append((code, d, close[i] * 0.99, close[i] * 1.01,
                             close[i] * 0.98, close[i], 1e8, 1e11, "test"))
    db.upsert_many(conn, "index_daily",
                   ["code", "date", "open", "high", "low", "close", "volume", "amount", "source"],
                   idx_rows, ["code", "date"])

    # variety_daily
    v_rows = []
    for i, d in enumerate(dates):
        v_rows.append((
            d, VARIETY, "IF2406", float(main_close[i]), 120000, int(oi[i]),
            int(long_pos[i] * 1.1), int(short_pos[i] * 1.05),
            int(long_pos[i] * 1.1 - short_pos[i] * 1.05),
            int(long_pos[i]), int(short_pos[i]), int(net[i]),
            float(basis[i]), float(basis[i] / close[i] * 100.0),
        ))
    db.upsert_many(conn, "variety_daily",
                   ["date", "variety", "main_contract", "main_close", "volume",
                    "open_interest", "top20_long", "top20_short", "top20_net",
                    "citic_long", "citic_short", "citic_net", "basis", "basis_pct"],
                   v_rows, ["date", "variety"])

    # fut_daily / member_rank / member_daily：只给最后 40 个交易日（ranks 只需要最新日）
    tail = dates[-40:]
    fut_rows = []
    for d in tail:
        for contract, oi_c in (("IF2406", 60000), ("IF2403", 30000)):
            fut_rows.append((d, VARIETY, contract, 4000.0, 4050.0, 3950.0,
                             4000.0, 4000.0, 4000.0, 1000, 1e9, oi_c, 0, 0))
    db.upsert_many(conn, "fut_daily",
                   ["date", "variety", "contract", "open", "high", "low", "close",
                    "settle", "prev_settle", "volume", "amount", "open_interest",
                    "oi_change", "is_main"],
                   fut_rows, ["date", "contract"])

    members = [MEMBER, "国泰君安", "海通期货"]
    rank_rows = []
    # 最新日 = 最后一天：IF2406 是主力（持仓量 60000 > 30000）
    for k, d in enumerate(tail[-1:], start=0):
        for contract in ("IF2406", "IF2403"):
            # 真实库 rank_type 口径：vol / long / short（契约第 3 节）
            for rtype in ("long", "short", "vol"):
                for rank in range(1, 21):
                    m = members[(rank - 1) % len(members)]
                    rank_rows.append((d, VARIETY, contract, rtype, rank, m, m,
                                      int(1000 * (21 - rank)), int(-10 * rank)))
    conn.executemany(
        "INSERT OR REPLACE INTO member_rank"
        "(date, variety, contract, rank_type, rank, member, member_raw, position, change)"
        " VALUES (?,?,?,?,?,?,?,?,?)", rank_rows)

    md_rows = []
    for i, d in enumerate(dates):
        for j, m in enumerate(members):
            net_m = net[i] + j * 5000
            long_m = long_pos[i] + j * 3000
            md_rows.append((d, VARIETY, m, int(long_m), int(long_m - net_m),
                            int(80000 - j * 5000), int(net_m)))
    db.upsert_many(conn, "member_daily",
                   ["date", "variety", "member", "long_pos", "short_pos",
                    "volume", "net_pos"],
                   md_rows, ["date", "variety", "member"])
    conn.commit()
    return conn


def _truncate(conn, t_date: str) -> None:
    """删掉 t_date 之后的所有行，用来做"无未来函数"断言。"""
    for table in ("index_daily", "variety_daily", "fut_daily", "member_rank", "member_daily"):
        conn.execute(f"DELETE FROM {table} WHERE date > ?", (t_date,))
    conn.commit()


# ---------------------------------------------------------------- 1. 空库
class TestEmptyDatabase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def test_dashboard_empty(self):
        out = analytics.compute_dashboard(self.conn, "IF", 720)
        self.assertFalse(out["ok"])
        self.assertTrue(out["empty"])
        self.assertEqual(out["error"], "数据不足")
        json.dumps(out, allow_nan=False)

    def test_ranks_empty(self):
        out = analytics.compute_ranks(self.conn, None, "IF")
        self.assertFalse(out["ok"])
        json.dumps(out, allow_nan=False)

    def test_members_empty(self):
        self.assertEqual(analytics.compute_members(self.conn, "IF", 720), [])

    def test_all_varieties_empty(self):
        for v in config.VARIETIES:
            out = analytics.compute_dashboard(self.conn, v, 480)
            self.assertFalse(out["ok"], v)
            self.assertTrue(out["empty"], v)


# ---------------------------------------------------------------- 2. 无未来函数
class TestNoLookAhead(unittest.TestCase):
    def setUp(self):
        self.conn = build_db(n=300)

    def test_truncation_does_not_change_past_features(self):
        df = analytics._load_frame(self.conn, VARIETY, MEMBER)
        full = analytics.derive_features(df)
        cols = ["dnet_z", "net_z", "net_pctile", "dnet_pctile",
                "citic_net_pct", "dnet_pct_of_oi"]
        for t in (70, 155, 240):
            sub = analytics.derive_features(df.iloc[: t + 1].reset_index(drop=True))
            for c in cols:
                a = full[c].iloc[t]
                b = sub[c].iloc[t]
                if pd.isna(a) or pd.isna(b):
                    self.assertTrue(pd.isna(a) and pd.isna(b), f"{c}@{t} nan mismatch")
                else:
                    self.assertAlmostEqual(float(a), float(b), places=12,
                                           msg=f"{c}@{t} changed by future data")

    def test_dashboard_snapshot_matches_series_at_same_date(self):
        full = analytics.compute_dashboard(self.conn, VARIETY, lookback=0)
        dates = full["series"]["date"]
        t_idx = len(dates) - 60
        t_date = dates[t_idx]
        conn2 = build_db(n=300)
        _truncate(conn2, t_date)
        cut = analytics.compute_dashboard(conn2, VARIETY, lookback=0)
        self.assertEqual(cut["as_of"], t_date)
        self.assertEqual(cut["window"]["n"], t_idx + 1)
        # 截断后最新一日的因子读数必须和完整库同一天逐位一致（4 位小数）
        for key in ("dnet_z", "net_pctile", "citic_net_pct"):
            a = cut["series"][key][-1]
            b = full["series"][key][t_idx]
            if a is None or b is None:
                self.assertIsNone(a)
                self.assertIsNone(b)
            else:
                self.assertAlmostEqual(float(a), float(b), places=9,
                                       msg=f"{key} 被未来数据改变了")
        # snapshot 是 2 位小数版本，允许舍入误差
        self.assertAlmostEqual(cut["snapshot"]["dnet_z"],
                               round(full["series"]["dnet_z"][t_idx], 2), places=9)

    def test_target_is_strictly_next_day_log_return(self):
        df = analytics._load_frame(self.conn, VARIETY, MEMBER)
        derived = analytics.derive_features(df)
        close = df["index_close"].to_numpy(dtype="float64")
        for t in (0, 50, len(df) - 2):
            expected = math.log(close[t + 1] / close[t]) * 100.0
            self.assertAlmostEqual(float(derived["y"].iloc[t]), expected, places=9)
        self.assertTrue(pd.isna(derived["y"].iloc[-1]))  # 最后一行没有次日收益

    def test_rolling_stats_use_252_60(self):
        s = pd.Series(np.random.default_rng(3).normal(size=300))
        z = analytics.rolling_z(s)
        self.assertTrue(pd.isna(z.iloc[58]))     # 59 个样本 < min_periods=60
        self.assertFalse(pd.isna(z.iloc[59]))    # 第 60 个样本开始有效
        # 手工核对第 100 个点的 z（窗口 252 -> 用前 101 个值）
        manual = (s.iloc[100] - s.iloc[:101].mean()) / s.iloc[:101].std(ddof=0)
        self.assertAlmostEqual(float(z.iloc[100]), float(manual), places=10)


# ---------------------------------------------------------------- 3. 滚动分位
class TestRollingPercentile(unittest.TestCase):
    def test_basic_positions(self):
        s = pd.Series(np.arange(100, dtype="float64"))
        pct = analytics.rolling_pctile(s)
        self.assertTrue(pd.isna(pct.iloc[58]))
        self.assertAlmostEqual(float(pct.iloc[59]), 100.0)
        self.assertAlmostEqual(float(pct.iloc[99]), 100.0)

    def test_descending_series(self):
        s = pd.Series(np.arange(10, 0, -1, dtype="float64"))   # 10..1
        pct = analytics.rolling_pctile(s, window=10, min_periods=5)
        self.assertTrue(pd.isna(pct.iloc[3]))
        self.assertAlmostEqual(float(pct.iloc[9]), 10.0)       # 1 是窗口最小值
        self.assertAlmostEqual(float(pct.iloc[4]), 20.0)       # 前缀 [10,9,8,7,6] 里 6 最小

    def test_ties_and_nan(self):
        s = pd.Series([5.0, 5.0, 5.0, np.nan, 5.0, 4.0] * 20)
        pct = analytics.rolling_pctile(s, window=10, min_periods=5)
        self.assertEqual(len(pct), len(s))
        # NaN 不计入样本：前 6 个里有效值为 [5,5,5,5,4]，4 最小 -> 1/5
        self.assertAlmostEqual(float(pct.iloc[5]), 20.0)
        self.assertTrue(0.0 <= float(pct.iloc[-1]) <= 100.0)


# ---------------------------------------------------------------- 4. OLS / 胜率
class TestStatistics(unittest.TestCase):
    def test_ols_exact_line(self):
        r = sig.ols([0, 1, 2, 3], [1, 3, 5, 7])
        self.assertAlmostEqual(r["slope"], 2.0, places=12)
        self.assertAlmostEqual(r["intercept"], 1.0, places=12)
        self.assertAlmostEqual(r["r2"], 1.0, places=12)
        self.assertAlmostEqual(r["corr"], 1.0, places=12)
        self.assertIsNone(r["t_stat"])          # 残差为 0，se=0，t 不可估
        self.assertFalse(r["significant"])

    def test_ols_standard_error_formula(self):
        x = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        y = np.array([1.0, 2.0, 1.5, 3.2, 2.8, 4.1])
        r = sig.ols(x, y)
        mx = x.mean()
        sxx = np.sum((x - mx) ** 2)
        slope = np.sum((x - mx) * (y - y.mean())) / sxx
        intercept = y.mean() - slope * mx
        resid = y - (intercept + slope * x)
        rss = float(np.sum(resid ** 2))
        se = math.sqrt(rss / (len(x) - 2) / sxx)
        t = slope / se
        p = math.erfc(abs(t) / math.sqrt(2.0))
        self.assertAlmostEqual(r["slope"], slope, places=12)
        self.assertAlmostEqual(r["intercept"], intercept, places=12)
        self.assertAlmostEqual(r["t_stat"], t, places=10)
        self.assertAlmostEqual(r["p_value"], p, places=12)
        self.assertEqual(r["significant"], bool(p < 0.05))
        self.assertEqual(r["n"], 6)

    def test_ols_degenerate(self):
        r = sig.ols([1, 1, 1], [1, 2, 3])
        self.assertEqual(r["n"], 3)
        self.assertIsNone(r["slope"])
        self.assertIsNone(r["p_value"])

    def test_norm_p(self):
        self.assertAlmostEqual(sig.norm_p(1.96), 0.0499958, places=6)
        self.assertAlmostEqual(sig.norm_p(0.0), 1.0, places=12)
        self.assertIsNone(sig.norm_p(None))
        self.assertIsNone(sig.norm_p(float("nan")))

    def test_bucket_stats_manual(self):
        x = np.arange(100, dtype="float64")
        yv = x * 0.01 - 0.5          # x>50 时为正
        df = pd.DataFrame({"dnet_z": x, "y": yv})
        bst = sig.bucket_stats(df, "dnet_z", "y")
        self.assertEqual(len(bst["rows"]), 5)
        self.assertEqual(sum(r["n"] for r in bst["rows"]), 100)
        for r in bst["rows"]:
            self.assertGreaterEqual(r["n"], 19)
            self.assertLessEqual(r["n"], 21)
        # Q1 全为负收益
        self.assertAlmostEqual(bst["rows"][0]["win_rate"], 0.0, places=9)
        self.assertAlmostEqual(bst["rows"][0]["mean_ret_pct"], np.mean(yv[:20]), places=9)
        # Q5 全为正收益
        self.assertAlmostEqual(bst["rows"][-1]["win_rate"], 100.0, places=9)
        self.assertAlmostEqual(bst["ic"], 1.0, places=9)   # 完全单调 -> Spearman=1

    def test_bucket_stats_empty_and_small(self):
        out = sig.bucket_stats(pd.DataFrame({"dnet_z": [], "y": []}))
        self.assertEqual(len(out["rows"]), 5)
        self.assertTrue(all(r["n"] == 0 for r in out["rows"]))
        self.assertIsNone(out["ic"])

    def test_wilson_ci(self):
        lo, hi = sig.wilson_ci(45, 100)
        self.assertLess(lo, 45.0)
        self.assertGreater(hi, 45.0)
        self.assertIsNone(sig.wilson_ci(0, 0))


# ---------------------------------------------------------------- 5. 回测规则
class TestBacktest(unittest.TestCase):
    def _win(self, n=40):
        rng = np.random.default_rng(5)
        y = rng.normal(0.0, 1.0, n)
        pct = np.where(np.arange(n) % 3 == 0, 10.0, np.where(np.arange(n) % 3 == 1, 90.0, 50.0))
        return pd.DataFrame({
            "date": [f"2024-01-{i + 1:02d}" for i in range(n)],
            "y": y,
            "dnet_pctile": pct,
        })

    def test_rule_and_cost_manual(self):
        win = self._win(40)
        out = analytics._backtest_block(win, cost_bp=0.5)
        y = win["y"].to_numpy()
        pct = win["dnet_pctile"].to_numpy()
        pos = np.where(pct <= 20, -1.0, np.where(pct >= 80, 1.0, 0.0))
        prev = np.concatenate(([0.0], pos[:-1]))
        turn = np.abs(pos - prev)
        strat = pos * (y / 100.0) - turn * 0.5 / 10000.0
        nav = np.cumprod(1.0 + strat)
        m = out["metrics"]   # 指标按契约保留 2~4 位小数，比较用 3 位容差
        self.assertAlmostEqual(m["cum_return"], float(nav[-1] - 1.0), places=3)
        self.assertAlmostEqual(m["benchmark_cum"],
                               float(np.cumprod(1 + y / 100.0)[-1] - 1.0), places=3)
        self.assertEqual(m["trades"], int(np.count_nonzero(turn > 0)))
        self.assertAlmostEqual(m["exposure"], float(np.count_nonzero(pos != 0)) / 40, places=3)
        # 最大回撤 = min(nav / cummax - 1)
        dd = nav / np.maximum.accumulate(nav) - 1.0
        self.assertAlmostEqual(m["max_drawdown"], float(dd.min()), places=3)
        self.assertEqual(m["n"], 40)
        self.assertEqual(len(out["equity"]["date"]), 40)
        self.assertEqual(len(out["equity"]["nav"]), 40)

    def test_backtest_short_series_returns_none_metrics(self):
        win = self._win(8)
        out = analytics._backtest_block(win)
        self.assertEqual(out["metrics"]["n"], 0)
        self.assertIsNone(out["metrics"]["cum_return"])


# ---------------------------------------------------------------- 6. 完整看板
class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = build_db(n=320)
        cls.out = analytics.compute_dashboard(cls.conn, VARIETY, lookback=240)

    def test_top_level_keys(self):
        keys = ["ok", "error", "empty", "variety", "variety_name", "index_code",
                "index_name", "member", "lookback", "as_of", "window", "snapshot",
                "series", "fit", "buckets", "backtest", "guidance", "ranks", "warnings"]
        for k in keys:
            self.assertIn(k, self.out)
        self.assertTrue(self.out["ok"])
        self.assertFalse(self.out["empty"])
        self.assertIsNone(self.out["error"])
        self.assertEqual(self.out["window"]["n"], 240)
        self.assertIsInstance(self.out["warnings"], list)

    def test_snapshot_keys(self):
        keys = ["date", "index_close", "index_chg_pct", "main_contract", "main_close",
                "basis", "basis_pct", "citic_long", "citic_short", "citic_net",
                "citic_net_pct", "citic_net_z", "citic_net_pctile", "dnet",
                "dnet_pct_of_oi", "dnet_z", "dnet_pctile", "top20_long",
                "top20_short", "top20_net", "market_oi", "citic_share_of_top20"]
        for k in keys:
            self.assertIn(k, self.out["snapshot"])
        snap = self.out["snapshot"]
        self.assertEqual(snap["citic_net"], snap["citic_long"] - snap["citic_short"])

    def test_series_alignment(self):
        s = self.out["series"]
        cols = ["date", "index_close", "index_ret_pct", "citic_net", "citic_net_pct",
                "citic_long", "citic_short", "market_oi", "main_close", "basis_pct",
                "dnet_pct_of_oi", "dnet_z", "net_pctile"]
        for c in cols:
            self.assertIn(c, s)
            self.assertEqual(len(s[c]), 240, c)
        self.assertEqual(len(set(s["date"])), 240)

    def test_fit_block(self):
        fit = self.out["fit"]
        for k in ("n", "sample", "corr_contemporaneous", "corr_predictive",
                  "models", "primary", "lag_corr", "rolling_corr", "stability",
                  "scatter", "x_label", "y_label"):
            self.assertIn(k, fit)
        self.assertEqual(fit["primary"], "dnet_z")
        self.assertEqual([m["key"] for m in fit["models"]],
                         ["dnet_z", "net_z", "net_pctile"])
        self.assertEqual([l["lag"] for l in fit["lag_corr"]], [-3, -2, -1, 0, 1, 2, 3])
        # 合成数据里因子与次日收益负相关 -> 斜率为负
        self.assertLess(fit["models"][0]["slope"], 0.0)
        self.assertLess(fit["corr_predictive"], 0.0)
        m = fit["models"][0]
        self.assertEqual(m["significant"], bool(m["p_value"] is not None and m["p_value"] < 0.05))
        years = fit["stability"]["years"]
        self.assertTrue(len(years) >= 1)
        for y in years:
            self.assertIn("year", y)
            self.assertIn("n", y)

    def test_buckets_block(self):
        b = self.out["buckets"]
        for k in ("x_label", "n_buckets", "ic", "ic_p", "rows"):
            self.assertIn(k, b)
        self.assertEqual(len(b["rows"]), 5)
        self.assertEqual(sum(r["n"] for r in b["rows"]), self.out["fit"]["models"][0]["n"])
        for r in b["rows"]:
            self.assertIn("label", r)
            self.assertIn("win_rate", r)
            if r["win_rate"] is not None:
                self.assertGreaterEqual(r["win_rate"], 0.0)
                self.assertLessEqual(r["win_rate"], 100.0)

    def test_backtest_block(self):
        bt = self.out["backtest"]
        self.assertIn("rule", bt)
        self.assertAlmostEqual(bt["cost_bp"], 0.5)
        m = bt["metrics"]
        for k in ("cum_return", "annual_return", "max_drawdown", "sharpe",
                  "hit_rate", "trades", "exposure", "benchmark_cum", "n"):
            self.assertIn(k, m)
        self.assertGreater(m["n"], 100)
        self.assertLessEqual(m["max_drawdown"], 0.0)
        self.assertGreaterEqual(m["exposure"], 0.0)
        self.assertLessEqual(m["exposure"], 1.0)

    def test_no_nan_no_inf(self):
        txt = json.dumps(self.out, allow_nan=False)
        self.assertNotIn("NaN", txt)
        self.assertNotIn("Infinity", txt)

    def test_guidance_in_dashboard(self):
        g = self.out["guidance"]
        self.assertIn("date", g)
        self.assertIn("direction", g)
        self.assertIn(g["direction"], ("偏多", "中性", "偏空"))
        self.assertIn(g["confidence"], ("低", "中", "高"))
        self.assertIsInstance(g["drivers"], list)
        self.assertEqual(len(g["drivers"]), 3)
        self.assertIsInstance(g["evidence"], list)
        self.assertIsInstance(g["caveats"], list)
        for k in ("n", "mean_ret_pct", "win_rate", "up_prob_ci", "last_examples"):
            self.assertIn(k, g["similar"])
        self.assertEqual(g["date"], self.out["as_of"])

    def test_lookback_all(self):
        out = analytics.compute_dashboard(self.conn, VARIETY, lookback=0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["window"]["n"], 320)
        json.dumps(out, allow_nan=False)

    def test_each_lookback_option(self):
        for lb in config.LOOKBACK_OPTIONS:
            out = analytics.compute_dashboard(self.conn, VARIETY, lookback=lb)
            self.assertTrue(out["ok"], lb)
            json.dumps(out, allow_nan=False)


# ---------------------------------------------------------------- 7. 其它品种/会员
class TestOtherVarietyAndMembers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = build_db(n=260, seed=21)

    def test_other_varieties_fall_back_to_empty(self):
        # 合成库里只有 IF 的 variety_daily
        for v in ("IH", "IC", "IM"):
            out = analytics.compute_dashboard(self.conn, v, 240)
            self.assertFalse(out["ok"], v)
            json.dumps(out, allow_nan=False)

    def test_api_style_keyword_calls(self):
        """api.py 的真实调用形状：compute_dashboard(conn, variety=, lookback=, member=)
        与 compute_ranks(conn, date=, variety=, member=)。"""
        d = analytics.compute_dashboard(self.conn, variety=VARIETY, lookback=480,
                                        member=MEMBER)
        self.assertTrue(d["ok"])
        r = analytics.compute_ranks(self.conn, date=None, variety=VARIETY,
                                    member="海通期货")
        self.assertTrue(r["ok"])
        self.assertEqual(r["highlight"], "海通期货")
        m = analytics.compute_members(self.conn, variety=VARIETY, lookback=480)
        self.assertIsInstance(m, list)

    def test_ranks_structure(self):
        out = analytics.compute_ranks(self.conn, None, VARIETY)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["long"]), 20)
        self.assertEqual(len(out["short"]), 20)
        self.assertEqual(len(out["volume"]), 20)
        # 回归防护：库里 rank_type='vol'，输出键必须是契约的 volume，且 rank 完整 1..20
        self.assertEqual([r["rank"] for r in out["volume"]], list(range(1, 21)))
        self.assertEqual([r["rank"] for r in out["long"]], list(range(1, 21)))
        self.assertEqual([r["rank"] for r in out["short"]], list(range(1, 21)))
        self.assertEqual(out["volume"][0]["position"], int(1000 * 20))
        self.assertEqual(out["highlight"], MEMBER)
        hr = out["highlight_row"]
        self.assertEqual(hr["member"], MEMBER)
        self.assertEqual(hr["net"], hr["long"] - hr["short"])
        self.assertIn("IF2406", out["contracts"])
        self.assertEqual(out["long"][0]["rank"], 1)
        json.dumps(out, allow_nan=False)

    def test_ranks_bad_date_falls_back_to_latest(self):
        out = analytics.compute_ranks(self.conn, "1999-01-01", VARIETY)
        self.assertTrue(out["ok"])
        self.assertNotEqual(out["date"], "1999-01-01")

    def test_members_structure(self):
        rows = analytics.compute_members(self.conn, VARIETY, lookback=240)
        self.assertIsInstance(rows, list)
        self.assertGreaterEqual(len(rows), 2)
        keys = ["member", "net_latest", "net_pct_latest", "net_avg20",
                "net_change20", "pred_corr", "n", "rank"]
        for r in rows:
            for k in keys:
                self.assertIn(k, r)
        self.assertEqual([r["rank"] for r in rows], list(range(1, len(rows) + 1)))
        corrs = [r["pred_corr"] for r in rows if r["pred_corr"] is not None]
        self.assertEqual(corrs, sorted(corrs))
        json.dumps(rows, allow_nan=False)

    def test_members_explicit_list(self):
        rows = analytics.compute_members(self.conn, VARIETY, 240, members=["海通期货"])
        self.assertEqual([r["member"] for r in rows], ["海通期货"])


# ---------------------------------------------------------------- 8. signal 独立
class TestSignalStandalone(unittest.TestCase):
    def test_build_guidance_on_derived_frame(self):
        conn = build_db(n=260)
        df = analytics._load_frame(conn, VARIETY, MEMBER)
        derived = analytics.derive_features(df)
        g = sig.build_guidance(derived, VARIETY, MEMBER)
        self.assertIn(g["direction"], ("偏多", "中性", "偏空"))
        self.assertIsNotNone(g["for_date"])
        self.assertEqual(g["similar"]["n"], g["sample_n"])
        json.dumps(g, allow_nan=False)

    def test_build_guidance_empty_frame(self):
        g = sig.build_guidance(pd.DataFrame(), VARIETY, MEMBER)
        self.assertEqual(g["direction"], "中性")
        self.assertEqual(g["confidence"], "低")
        self.assertEqual(g["drivers"], [])
        json.dumps(g, allow_nan=False)

    def test_for_date_skips_weekend(self):
        df = pd.DataFrame({
            "date": ["2024-01-05"],   # 周五
            "y": [np.nan], "dnet_z": [0.0], "net_z": [0.0], "net_pctile": [50.0],
            "dnet_pctile": [50.0], "citic_net_pct": [-10.0],
        })
        g = sig.build_guidance(df, VARIETY, MEMBER)
        self.assertEqual(g["for_date"], "2024-01-08")   # 跳过周六周日

    def test_num_cleaning(self):
        self.assertIsNone(sig.num(float("nan")))
        self.assertIsNone(sig.num(float("inf")))
        self.assertIsNone(sig.num(None))
        self.assertIsNone(sig.num("bad"))
        self.assertEqual(sig.num(0.0), 0.0)
        self.assertNotEqual(str(sig.num(-0.00001)), "-0.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
