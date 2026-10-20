# -*- coding: utf-8 -*-
"""契约一致性 + 数据合理性验收脚本（Lead 用，直接对真实库跑，不需要起服务）。

    python tools/verify.py                # 四品种 × 四个窗口全量校验
    python tools/verify.py --quick        # 只校验 IF/720
    python tools/verify.py --variety IM   # 指定品种

校验内容
--------
1. 顶层 JSON 结构：/api/dashboard、/api/ranks、/api/members、/api/meta、/api/export
   是否包含 contract/API_CONTRACT.md 要求的全部字段；
2. 数值合理性：无穷/NaN、日期格式、各序列等长、分位在 0–100、direction 枚举、
   score ∈ [-1,1]、窗口起点与 lookback 匹配、回测净值为有限数；
3. 打印每个组合的核心结论，便于人工复核（IC / p 值 / 回测年化 / 次日研判）。

退出码 0 = 全部通过；1 = 有失败项。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from backend import api, db  # noqa: E402

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DASH_KEYS = ["ok", "error", "empty", "variety", "variety_name", "index_code",
             "index_name", "member", "lookback", "as_of", "window", "snapshot",
             "series", "fit", "buckets", "backtest", "guidance", "ranks",
             "warnings"]
SNAP_KEYS = ["date", "index_close", "index_chg_pct", "main_contract", "main_close",
             "basis", "basis_pct", "citic_long", "citic_short", "citic_net",
             "citic_net_pct", "citic_net_z", "citic_net_pctile", "dnet",
             "dnet_pct_of_oi", "dnet_z", "dnet_pctile", "top20_long",
             "top20_short", "top20_net", "market_oi", "citic_share_of_top20"]
SERIES_KEYS = ["date", "index_close", "index_ret_pct", "citic_net",
               "citic_net_pct", "citic_long", "citic_short", "market_oi",
               "main_close", "basis_pct", "dnet_pct_of_oi", "dnet_z",
               "net_pctile"]
FIT_KEYS = ["n", "sample", "corr_contemporaneous", "corr_predictive", "models",
            "primary", "lag_corr", "rolling_corr", "stability", "scatter",
            "x_label", "y_label"]
MODEL_KEYS = ["key", "name", "n", "corr", "slope", "intercept", "r2", "t_stat",
              "p_value", "significant"]
BUCKET_KEYS = ["bucket", "label", "lo", "hi", "n", "mean_ret_pct",
               "median_ret_pct", "win_rate", "t_stat", "p_value"]
BT_METRIC_KEYS = ["cum_return", "annual_return", "max_drawdown", "sharpe",
                  "hit_rate", "trades", "exposure", "benchmark_cum", "n"]
GUIDE_KEYS = ["date", "for_date", "direction", "score", "confidence", "up_prob",
              "expected_ret_pct", "sample_n", "drivers", "evidence", "caveats",
              "similar"]
RANK_KEYS = ["ok", "date", "variety", "long", "short", "volume", "highlight",
             "highlight_row", "contracts"]
MEMBER_ROW_KEYS = ["member", "net_latest", "net_pct_latest", "net_avg20",
                   "net_change20", "pred_corr", "n", "rank"]

failures: list[str] = []


def fail(where: str, msg: str):
    failures.append("%s: %s" % (where, msg))


def need(obj, keys, where: str):
    if not isinstance(obj, dict):
        fail(where, "期望 dict，得到 %s" % type(obj).__name__)
        return
    for k in keys:
        if k not in obj:
            fail(where, "缺少字段 %r" % k)


def finite(value, where: str):
    if value is None:
        return
    if not isinstance(value, (int, float)):
        fail(where, "非数值: %r" % (value,))
        return
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        fail(where, "NaN/Inf: %r" % value)


def check_series(series: dict, where: str):
    need(series, SERIES_KEYS, where)
    dates = series.get("date") or []
    if not dates:
        fail(where, "date 序列为空")
        return
    for d in (dates[0], dates[-1]):
        if not DATE_RE.match(str(d)):
            fail(where, "日期格式错误: %r" % d)
    n = len(dates)
    if n != len(set(dates)):
        fail(where, "日期有重复")
    if list(dates) != sorted(dates):
        fail(where, "日期未升序")
    for k in SERIES_KEYS[1:]:
        col = series.get(k) or []
        if len(col) != n:
            fail(where, "序列 %s 长度 %d != date 长度 %d" % (k, len(col), n))
        for v in col:
            finite(v, "%s.%s" % (where, k))


def check_dashboard(payload: dict, variety: str, lookback: int):
    where = "dashboard[%s/%s]" % (variety, lookback)
    need(payload, DASH_KEYS, where)
    if not payload.get("ok"):
        fail(where, "ok=False: %s" % payload.get("error"))
        return
    if payload.get("empty"):
        fail(where, "empty=True（数据不足）")
        return

    if payload.get("variety") != variety:
        fail(where, "variety 回显错误: %r" % payload.get("variety"))
    if int(payload.get("lookback") or 0) != lookback:
        fail(where, "lookback 回显错误: %r" % payload.get("lookback"))

    win = payload.get("window") or {}
    need(win, ["start", "end", "n"], where + ".window")
    if lookback and win.get("n") and abs(int(win["n"]) - lookback) > 1:
        fail(where, "window.n=%s 与 lookback=%s 不匹配" % (win.get("n"), lookback))
    if win.get("end") != payload.get("as_of"):
        fail(where, "window.end != as_of")

    snap = payload.get("snapshot") or {}
    need(snap, SNAP_KEYS, where + ".snapshot")
    if snap.get("date") != payload.get("as_of"):
        fail(where, "snapshot.date != as_of")
    for k in SNAP_KEYS:
        if k in ("date", "main_contract"):
            continue
        finite(snap.get(k), "%s.snapshot.%s" % (where, k))
    if snap.get("citic_net") is not None and snap.get("citic_long") is not None \
            and snap.get("citic_short") is not None:
        expect = snap["citic_long"] - snap["citic_short"]
        if abs(expect - snap["citic_net"]) > 1:
            fail(where, "citic_net 不自洽: %s != %s-%s" %
                 (snap["citic_net"], snap["citic_long"], snap["citic_short"]))
    for key in ("citic_net_pctile", "dnet_pctile"):
        v = snap.get(key)
        if v is not None and not (0 <= v <= 100):
            fail(where, "分位越界 %s=%s" % (key, v))

    check_series(payload.get("series") or {}, where + ".series")

    fit = payload.get("fit") or {}
    need(fit, FIT_KEYS, where + ".fit")
    models = fit.get("models") or []
    if len(models) < 1:
        fail(where + ".fit", "models 为空")
    for m in models:
        need(m, MODEL_KEYS, where + ".fit.models[]")
        for k in ("corr", "slope", "r2", "t_stat", "p_value"):
            finite(m.get(k), "%s.fit.models.%s.%s" % (where, m.get("key"), k))
        if m.get("corr") is not None and not (-1 <= m["corr"] <= 1):
            fail(where + ".fit", "corr 越界: %r" % m.get("corr"))
        if m.get("p_value") is not None and not (0 <= m["p_value"] <= 1):
            fail(where + ".fit", "p_value 越界: %r" % m.get("p_value"))
    if not isinstance(fit.get("lag_corr"), list) or not fit["lag_corr"]:
        fail(where + ".fit", "lag_corr 为空")
    rc = fit.get("rolling_corr") or {}
    need(rc, ["date", "contemporaneous", "predictive"], where + ".fit.rolling_corr")
    if len(rc.get("date") or []) != len(rc.get("predictive") or []):
        fail(where + ".fit.rolling_corr", "date/predictive 长度不一致")
    stab = fit.get("stability") or {}
    if isinstance(stab, dict):
        stab = stab.get("years") or []
    if not stab:
        fail(where + ".fit", "stability 为空")
    for row in stab:
        need(row, ["year", "corr_predictive", "n"], where + ".fit.stability[]")

    buckets = payload.get("buckets") or {}
    need(buckets, ["x_label", "n_buckets", "ic", "ic_p", "rows"], where + ".buckets")
    rows = buckets.get("rows") or []
    if len(rows) != 5:
        fail(where + ".buckets", "rows 应为 5 组，实际 %d" % len(rows))
    total_n = 0
    for row in rows:
        need(row, BUCKET_KEYS, where + ".buckets.rows[]")
        total_n += int(row.get("n") or 0)
        wr = row.get("win_rate")
        if wr is not None and not (0 <= wr <= 100):
            fail(where + ".buckets", "win_rate 越界: %r" % wr)
    if total_n <= 0:
        fail(where + ".buckets", "分组样本量为 0")

    bt = payload.get("backtest") or {}
    need(bt, ["rule", "cost_bp", "equity", "metrics"], where + ".backtest")
    eq = bt.get("equity") or {}
    need(eq, ["date", "nav", "nav_bh"], where + ".backtest.equity")
    if len(eq.get("date") or []) != len(eq.get("nav") or []):
        fail(where + ".backtest.equity", "date/nav 长度不一致")
    for k in BT_METRIC_KEYS:
        if k not in (bt.get("metrics") or {}):
            fail(where + ".backtest.metrics", "缺少 %r" % k)

    guide = payload.get("guidance") or {}
    need(guide, GUIDE_KEYS, where + ".guidance")
    if guide.get("direction") not in ("偏多", "中性", "偏空"):
        fail(where + ".guidance", "direction 非法: %r" % guide.get("direction"))
    if guide.get("confidence") not in ("低", "中", "高"):
        fail(where + ".guidance", "confidence 非法: %r" % guide.get("confidence"))
    score = guide.get("score")
    if score is not None and not (-1.001 <= score <= 1.001):
        fail(where + ".guidance", "score 越界: %r" % score)
    up = guide.get("up_prob")
    if up is not None and not (0 <= up <= 100):
        fail(where + ".guidance", "up_prob 越界: %r" % up)
    if not guide.get("drivers"):
        fail(where + ".guidance", "drivers 为空")
    if not guide.get("caveats"):
        fail(where + ".guidance", "caveats 为空（必须包含风险提示）")
    sim = guide.get("similar") or {}
    need(sim, ["n", "mean_ret_pct", "win_rate", "up_prob_ci", "last_examples"],
         where + ".guidance.similar")

    ranks = payload.get("ranks") or {}
    need(ranks, RANK_KEYS, where + ".ranks")
    if not (ranks.get("long") or []):
        fail(where + ".ranks", "long 榜单为空")
    if not (ranks.get("highlight_row") or {}):
        fail(where + ".ranks", "highlight_row 为空")

    # 关键结论打印
    primary = fit.get("primary")
    pmod = next((m for m in models if m.get("key") == primary), models[0])
    print("  %-2s lb=%-4s n=%-4s corr_pred=%+.3f p=%.3f %s | 回测年化=%+.1f%% 回撤=%+.1f%% "
          "夏普=%s | 研判=%s up=%.1f%%" % (
              variety, lookback if lookback else "ALL",
              fit.get("n"), pmod.get("corr") or 0, pmod.get("p_value") or 1,
              "显著" if pmod.get("significant") else "不显著",
              100 * (bt["metrics"].get("annual_return") or 0),
              100 * (bt["metrics"].get("max_drawdown") or 0),
              ("%.2f" % bt["metrics"]["sharpe"]) if bt["metrics"].get("sharpe") is not None else "—",
              guide.get("direction"), guide.get("up_prob") or 0))


def check_ranks(payload: dict):
    where = "ranks"
    need(payload, RANK_KEYS, where)
    if not payload.get("ok"):
        fail(where, "ok=False: %s" % payload.get("error"))
        return
    for key in ("long", "short", "volume"):
        rows = payload.get(key) or []
        if len(rows) != 20:
            fail(where, "%s 应为 20 行，实际 %d" % (key, len(rows)))
        for r in rows:
            need(r, ["rank", "member", "position", "change"], where + "." + key)
    hr = payload.get("highlight_row") or {}
    need(hr, ["member", "long", "short", "net"], where + ".highlight_row")
    if hr.get("member") != payload.get("highlight"):
        fail(where, "highlight_row.member != highlight")
    if hr.get("net") is not None and hr.get("long") is not None and hr.get("short") is not None:
        if abs((hr["long"] - hr["short"]) - hr["net"]) > 1:
            fail(where, "highlight_row 净持仓不自洽")


def check_members(payload: dict, variety: str, lookback: int):
    where = "members[%s/%s]" % (variety, lookback)
    need(payload, ["ok", "error", "variety", "lookback", "member", "rows"], where)
    if not payload.get("ok"):
        fail(where, "ok=False: %s" % payload.get("error"))
        return
    rows = payload.get("rows") or []
    if len(rows) < 3:
        fail(where, "rows 太少: %d" % len(rows))
    for i, r in enumerate(rows):
        need(r, MEMBER_ROW_KEYS, where + ".rows[%d]" % i)
        if r.get("rank") != i + 1:
            fail(where, "rank 未从 1 连续递增，第 %d 行为 %r" % (i + 1, r.get("rank")))
    print("  %-2s lb=%-4s 会员数=%-2d 中信期货排名=%s pred_corr=%s" % (
        variety, lookback, len(rows),
        next((r["rank"] for r in rows if r.get("member") == config.DEFAULT_MEMBER), "—"),
        next((r["pred_corr"] for r in rows if r.get("member") == config.DEFAULT_MEMBER), "—")))


def check_meta(payload: dict):
    need(payload, ["ok", "error", "generated_at", "latest_trade_date", "data",
                   "varieties", "members", "default_member", "default_lookback",
                   "lookback_options", "disclaimer"], "meta")
    need(payload.get("data") or {}, ["index_daily", "by_variety", "last_sync"], "meta.data")
    if len(payload.get("varieties") or []) != 4:
        fail("meta", "varieties 应为 4 个")


def check_export(q: dict):
    out = api.api_export(q)
    if not out.get("__csv__"):
        fail("export", "未返回 CSV: %r" % (out.get("error"),))
        return
    lines = out["body"].lstrip("\ufeff").splitlines()
    if len(lines) < 10:
        fail("export", "CSV 行数太少: %d" % len(lines))
    header = lines[0].split(",")
    for col in ("date", "index_close", "citic_net", "dnet", "market_oi"):
        if col not in header:
            fail("export", "缺少列 %r" % col)
    print("  export %s 行数=%d 列=%s" % (q.get("variety"), len(lines) - 1, ",".join(header)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--variety", default=None)
    parser.add_argument("--lookback", type=int, default=None)
    args = parser.parse_args()

    varieties = [args.variety.upper()] if args.variety else list(config.VARIETIES)
    lookbacks = [args.lookback] if args.lookback else ([720] if args.quick
                                                       else list(config.LOOKBACK_OPTIONS))

    with db.session() as conn:
        stats = db.data_stats(conn)
    print("=" * 100)
    print(" 数据概况")
    for table in ("index_daily", "fut_daily", "member_rank", "member_daily", "variety_daily"):
        row = stats.get(table) or {}
        print("   %-14s rows=%-8s %s ~ %s" % (table, row.get("rows"),
                                              row.get("start") or "-", row.get("end") or "-"))
    print("=" * 100)

    q0 = {"variety": ["IF"], "lookback": ["720"]}
    check_meta(api.api_meta(q0))
    check_ranks(api.api_ranks({"variety": ["IF"]}))
    check_export({"variety": ["IF"], "lookback": ["720"]})

    print("-" * 100)
    print(" 四品种 × 窗口")
    for v in varieties:
        for lb in lookbacks:
            q = {"variety": [v], "lookback": [str(lb)]}
            try:
                payload = json.loads(api.dumps(api.api_dashboard(q)).decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                fail("dashboard[%s/%s]" % (v, lb), "异常 %s: %s" % (type(exc).__name__, exc))
                continue
            check_dashboard(payload, v, lb)
            try:
                mp = json.loads(api.dumps(api.api_members(q)).decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                fail("members[%s/%s]" % (v, lb), "异常 %s: %s" % (type(exc).__name__, exc))
                continue
            check_members(mp, v, lb)

    print("-" * 100)
    if failures:
        print(" 验收失败 %d 项：" % len(failures))
        for f in failures[:60]:
            print("   ✗ %s" % f)
        if len(failures) > 60:
            print("   … 另有 %d 项" % (len(failures) - 60))
        return 1
    print(" ✅ 契约结构与数值合理性全部通过（%d 组合）" % (len(varieties) * len(lookbacks)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
