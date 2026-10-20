#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
中信期货 · 股指期货持仓分析看板 —— 启动入口

用法
----
    python app.py                     # 启动 Web 服务（默认 http://127.0.0.1:8788）
    python app.py --open              # 启动并自动打开浏览器
    python app.py --sync              # 只跑一次数据同步，然后退出
    python app.py --sync --open       # 先同步再启动并打开浏览器
    python app.py --sync-if-empty     # 库为空才同步（run.ps1 默认走这个）
    python app.py --check             # 离线自检：模块、数据库、全部接口
    python app.py --port 9000 --host 0.0.0.0

依赖：Python 3.10+，numpy / pandas（分析用），无其他第三方包。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from backend import api, db  # noqa: E402


# --------------------------------------------------------------------------
def _rows_summary() -> dict:
    try:
        with db.session() as conn:
            stats = db.data_stats(conn)
        return stats
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def _is_empty() -> bool:
    stats = _rows_summary()
    vd = stats.get("variety_daily") or {}
    idd = stats.get("index_daily") or {}
    return not vd.get("rows") or not idd.get("rows")


def print_summary() -> None:
    stats = _rows_summary()
    if stats.get("error"):
        print("[db] 读取失败:", stats["error"])
        return
    print("-" * 64)
    print(" 数据概况")
    for table in ("index_daily", "fut_daily", "member_rank", "member_daily", "variety_daily"):
        row = stats.get(table) or {}
        print("  %-14s rows=%-9s %s ~ %s" % (
            table, row.get("rows", 0), row.get("start") or "-", row.get("end") or "-"))
    for v, row in (stats.get("by_variety") or {}).items():
        print("    %-2s           rows=%-9s %s ~ %s" % (
            v, row.get("rows", 0), row.get("start") or "-", row.get("end") or "-"))
    print("  last_sync     %s" % (stats.get("last_sync") or "从未同步"))
    print("-" * 64)


def run_sync(start: str | None = None, end: str | None = None) -> dict:
    from backend import sync as sync_mod

    t0 = time.time()
    last_print = [0.0]

    def progress(done, total, label):
        now = time.time()
        if now - last_print[0] < 0.5 and done not in (0, total):
            return
        last_print[0] = now
        pct = (100.0 * done / total) if total else 0.0
        sys.stdout.write("\r  [同步] %5d/%-5d (%5.1f%%) %-40s" % (
            done, total, pct, str(label)[:40]))
        sys.stdout.flush()

    print("开始同步数据（%s ~ %s）…" % (start or config.SYNC_START, end or "最新"))
    result = sync_mod.sync_all(start=start, end=end, progress=progress)
    print()
    rebuild = sync_mod.rebuild_aggregates()
    if isinstance(result, dict):
        result["rebuild"] = rebuild
    elapsed = time.time() - t0
    fails = (result or {}).get("failed") or (result or {}).get("failures") or []
    print("[同步] 完成，用时 %.1fs，失败日期 %d 个" % (elapsed, len(fails)))
    if fails:
        print("[同步] 前 10 个失败:", fails[:10])
    try:
        with db.session() as conn:
            db.kv_set(conn, "last_sync_result",
                      json.dumps(api._clean(result), ensure_ascii=False))
    except Exception:
        pass
    return result or {}


# --------------------------------------------------------------------------
def run_check() -> int:
    """离线自检：模块导入、数据库、全部接口（不起网络监听，直接用内部函数）。"""
    ok = True
    print("=" * 64)
    print(" 自检开始")
    print("=" * 64)

    # 1) 模块
    for mod_name in ("backend.db", "backend.sync", "backend.analytics",
                     "backend.signal", "backend.api"):
        try:
            __import__(mod_name)
            print("  [OK] import %s" % mod_name)
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  [FAIL] import %s -> %s: %s" % (mod_name, type(exc).__name__, exc))

    # 2) 数据库
    stats = _rows_summary()
    if stats.get("error"):
        ok = False
        print("  [FAIL] db:", stats["error"])
    else:
        print("  [OK] db rows:", {k: (v or {}).get("rows")
                                  for k, v in stats.items() if isinstance(v, dict)
                                  and "rows" in v})

    # 3) 接口（内部调用，不经过 socket）
    query = {"variety": ["IF"], "lookback": ["720"]}
    checks = [
        ("/api/health", lambda: api.api_health(query)),
        ("/api/meta", lambda: api.api_meta(query)),
        ("/api/dashboard", lambda: api.api_dashboard(query)),
        ("/api/ranks", lambda: api.api_ranks(query)),
        ("/api/members", lambda: api.api_members(query)),
        ("/api/sync/status", lambda: api.api_sync_status(query)),
    ]
    for name, fn in checks:
        try:
            payload = fn()
            body = api.dumps(payload)
            parsed = json.loads(body.decode("utf-8"))
            flag = "OK" if parsed.get("ok") else "WARN(%s)" % parsed.get("error")
            if not parsed.get("ok"):
                ok = False
            size = len(body)
            print("  [%s] %-18s %6d bytes" % (flag, name, size))
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  [FAIL] %-18s %s: %s" % (name, type(exc).__name__, exc))

    # 4) 前端文件
    for rel in ("frontend/index.html", "frontend/styles.css", "frontend/app.js",
                "frontend/vendor/echarts.min.js"):
        p = ROOT / rel
        exists = p.is_file()
        if not exists:
            ok = False
        print("  [%s] %s%s" % ("OK" if exists else "FAIL", rel,
                               "  (%d KB)" % (p.stat().st_size // 1024) if exists else ""))

    print("=" * 64)
    print(" 自检结果:", "通过" if ok else "存在失败项")
    print("=" * 64)
    return 0 if ok else 1


# --------------------------------------------------------------------------
def serve(host: str, port: int, do_open: bool) -> None:
    if _is_empty():
        print("[提示] 数据库为空。可在页面点「立即更新数据」，"
              "或运行: python app.py --sync")
    port = api.find_free_port(host, port)
    httpd = api.make_server(host, port)
    url = "http://%s:%d/" % (host, port)
    if do_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(" 服务已启动 -> %s" % url)
    print_summary()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="中信期货股指期货持仓分析看板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=config.SERVER_HOST, help="监听地址")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT, help="监听端口")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--sync", action="store_true", help="只同步数据后退出")
    parser.add_argument("--sync-if-empty", action="store_true",
                        help="库为空时先同步，再启动服务")
    parser.add_argument("--start", default=None, help="同步起始日 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="同步结束日 YYYY-MM-DD")
    parser.add_argument("--check", action="store_true", help="离线自检后退出")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()

    if args.sync:
        run_sync(args.start, args.end)
        print_summary()
        return 0

    if args.sync_if_empty and _is_empty():
        run_sync(args.start, args.end)
        print_summary()

    serve(args.host, args.port, args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
