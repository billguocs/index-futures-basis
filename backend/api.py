# -*- coding: utf-8 -*-
"""
HTTP 路由层（Lead 实现）。

设计要点
--------
* 零第三方依赖：http.server + urllib.parse + sqlite3。
* 所有 /api/* 恒返回 HTTP 200，响应体恒含 ok/error（见 contract/API_CONTRACT.md 第 5 节）。
* 计算类接口（dashboard/members/ranks）带进程内缓存，键包含「数据版本」，
  数据一变缓存自动失效，前端切换品种/窗口基本是毫秒级。
* 同步（POST /api/sync）在后台线程跑，前端轮询 /api/sync/status。
* 静态资源直接由本服务从 frontend/ 目录托管（同源，免 CORS、免额外服务器）。
"""

from __future__ import annotations

import csv
import io
import json
import math
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 允许 backend/api.py 被直接 import（如测试脚本）时也能找到项目根的 config.py
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402

from . import db  # noqa: E402

# 北京时区（无夏令时，固定 +08:00，避免依赖 zoneinfo 数据库）
BEIJING = timezone(timedelta(hours=8))

DASH_CACHE: dict = {}
DASH_CACHE_LOCK = threading.Lock()
CACHE_MAX = 48


# --------------------------------------------------------------------------
# 序列化工具：numpy 标量 / NaN / inf 一律转成 JSON 合法值
# --------------------------------------------------------------------------
def _clean(obj):
    """递归清洗：numpy 标量 -> python 标量；NaN/Inf -> None。"""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if isinstance(obj, int):
        return obj
    # numpy 标量（不 import numpy，鸭子类型判断）
    cls_name = type(obj).__name__
    if cls_name in ("float64", "float32", "float16"):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else round(v, 6)
    if cls_name in ("int64", "int32", "int16", "int8", "uint64", "uint32", "uint16", "uint8"):
        return int(obj)
    if cls_name == "bool_" or cls_name == "bool8":
        return bool(obj)
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    # date 对象
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            pass
    return obj


def dumps(payload) -> bytes:
    try:
        text = json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
    except (ValueError, TypeError) as exc:  # 兜底：绝不让 NaN 泄漏成非法 JSON
        text = json.dumps(
            {"ok": False, "error": "序列化失败: %s" % exc},
            ensure_ascii=False, separators=(",", ":"),
        )
    return text.encode("utf-8")


def _err(msg: str) -> dict:
    return {"ok": False, "error": str(msg)}


# --------------------------------------------------------------------------
# 后台同步管理器
# --------------------------------------------------------------------------
class SyncManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = {
            "running": False,
            "progress": {"done": 0, "total": 0, "label": ""},
            "started_at": None,
            "finished_at": None,
            "last": None,
        }

    # --- 对外接口 ---------------------------------------------------------
    def start(self, start: str | None = None, end: str | None = None) -> dict:
        with self._lock:
            if self._state["running"]:
                return {"ok": True, "started": False, "error": None,
                        "message": "同步已在进行中"}
            self._state.update({
                "running": True,
                "progress": {"done": 0, "total": 0, "label": "准备中"},
                "started_at": _now_str(),
                "finished_at": None,
                "last": None,
            })
            self._thread = threading.Thread(
                target=self._run, args=(start, end), name="sync-worker", daemon=True
            )
            self._thread.start()
        return {"ok": True, "started": True, "error": None}

    def status(self) -> dict:
        with self._lock:
            snapshot = json.loads(json.dumps(self._state))
        if snapshot["last"] is None:
            try:
                with db.session() as conn:
                    raw = db.kv_get(conn, "last_sync_result")
                if raw:
                    snapshot["last"] = {"at": db.kv_get(conn, "last_sync_at"),
                                        "result": json.loads(raw)}
            except Exception:
                pass
        return snapshot

    def wait(self, timeout: float | None = None) -> bool:
        th = self._thread
        if th is None:
            return True
        th.join(timeout)
        return not th.is_alive()

    # --- 内部 -------------------------------------------------------------
    def _progress(self, done, total, label):
        with self._lock:
            self._state["progress"] = {"done": int(done), "total": int(total),
                                       "label": str(label)}

    def _run(self, start, end):
        t0 = time.time()
        result = None
        try:
            from . import sync as sync_mod  # 延迟导入，避免循环依赖

            result = sync_mod.sync_all(start=start, end=end, progress=self._progress)
            rebuild = sync_mod.rebuild_aggregates()
            if isinstance(result, dict):
                result["rebuild"] = rebuild
            with db.session() as conn:
                db.kv_set(conn, "last_sync_result", json.dumps(_clean(result),
                                                               ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["finished_at"] = _now_str()
                self._state["last"] = {"at": self._state["finished_at"],
                                       "result": result}
                self._state["progress"] = {
                    "done": 1, "total": 1,
                    "label": "完成（%.1fs）" % (time.time() - t0),
                }
            DASH_CACHE.clear()


SYNC = SyncManager()


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _now_str() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%dT%H:%M:%S%z")


def _parse_int(value, default: int, lo: int | None = None,
               hi: int | None = None) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


TABLES = ("index_daily", "fut_daily", "member_rank", "member_daily", "variety_daily")


def _row_counts(conn) -> dict:
    return {t: db.table_count(conn, t) for t in TABLES}


def _data_version() -> str:
    """数据版本号：任一表行数/最新日期变化 -> 缓存失效。"""
    try:
        with db.session() as conn:
            counts = _row_counts(conn)
            latest = db.latest_date(conn, "variety_daily")
        parts = ["%s:%s" % (k, counts.get(k, 0)) for k in sorted(counts)]
        parts.append("latest:%s" % latest)
        return "|".join(parts)
    except Exception:
        return "noversion"


def _cached(key_tuple, producer):
    ver = _data_version()
    key = (ver,) + tuple(key_tuple)
    with DASH_CACHE_LOCK:
        if key in DASH_CACHE:
            return DASH_CACHE[key]
    payload = producer()
    with DASH_CACHE_LOCK:
        if len(DASH_CACHE) > CACHE_MAX:
            DASH_CACHE.clear()
        DASH_CACHE[key] = payload
    return payload


def _variety_from_query(q: dict) -> str:
    v = (q.get("variety") or [config.VARIETIES[0]])[0].upper()
    return v if v in config.VARIETIES else config.VARIETIES[0]


# --------------------------------------------------------------------------
# 各接口实现
# --------------------------------------------------------------------------
def api_health(q):
    with db.session() as conn:
        counts = _row_counts(conn)
    has_data = bool(counts.get("variety_daily")) and bool(counts.get("index_daily"))
    return {"ok": True, "error": None, "db": True,
            "data_ready": has_data, "rows": counts}


def api_meta(q):
    with db.session() as conn:
        stats = db.data_stats(conn)          # 已含各表 start/end/rows 与 by_variety
        latest = (stats.get("variety_daily") or {}).get("end") or \
                 (stats.get("index_daily") or {}).get("end")
        members = [r[0] for r in conn.execute(
            "SELECT member FROM member_daily GROUP BY member "
            "ORDER BY SUM(long_pos + short_pos) DESC LIMIT 30"
        ).fetchall()]
        naming_break = db.kv_get(conn, "member_naming_break_date")

    if config.DEFAULT_MEMBER not in members:
        members = [config.DEFAULT_MEMBER] + members

    varieties = []
    for v in config.VARIETIES:
        info = config.VARIETY_INFO[v]
        varieties.append({
            "code": v, "name": info["name"], "index_code": info["index_code"],
            "index_name": info["index_name"], "multiplier": info["multiplier"],
            "color": info["color"],
        })

    return {
        "ok": True, "error": None,
        "generated_at": _now_str(),
        "latest_trade_date": latest,
        "data": {**stats, "member_naming_break_date": naming_break},
        "varieties": varieties,
        "members": members,
        "default_member": config.DEFAULT_MEMBER,
        "default_lookback": config.DEFAULT_LOOKBACK,
        "lookback_options": config.LOOKBACK_OPTIONS,
        "disclaimer": config.DISCLAIMER,
    }


def api_dashboard(q):
    variety = _variety_from_query(q)
    lookback = _parse_int((q.get("lookback") or [config.DEFAULT_LOOKBACK])[0],
                          config.DEFAULT_LOOKBACK, 0, 5000)
    member = (q.get("member") or [config.DEFAULT_MEMBER])[0]

    def produce():
        from . import analytics  # 延迟导入
        with db.session() as conn:
            payload = analytics.compute_dashboard(
                conn, variety=variety, lookback=lookback, member=member
            )
        if not isinstance(payload, dict):
            return _err("analytics.compute_dashboard 返回类型错误")
        payload.setdefault("ok", True)
        payload.setdefault("error", None)
        payload["member"] = payload.get("member") or member
        return payload

    return _cached(("dashboard", variety, lookback, member), produce)


def api_ranks(q):
    variety = _variety_from_query(q)
    date = (q.get("date") or [""])[0].strip() or None
    member = (q.get("member") or [config.DEFAULT_MEMBER])[0]

    with db.session() as conn:
        if not date:
            date = db.latest_date(conn, "member_rank") or db.latest_date(conn, "variety_daily")
    if not date:
        return {"ok": False, "error": "无持仓排名数据，请先同步", "empty": True,
                "date": None, "variety": variety}
    from . import analytics
    with db.session() as conn:
        payload = analytics.compute_ranks(conn, date=date, variety=variety, member=member)
    payload.setdefault("ok", True)
    payload.setdefault("error", None)
    return payload


def api_members(q):
    variety = _variety_from_query(q)
    lookback = _parse_int((q.get("lookback") or [config.DEFAULT_LOOKBACK])[0],
                          config.DEFAULT_LOOKBACK, 0, 5000)
    member = (q.get("member") or [config.DEFAULT_MEMBER])[0]

    def produce():
        from . import analytics
        with db.session() as conn:
            # 契约 4.2：compute_members(conn, variety, lookback=720, members=None) -> list[dict]
            rows = analytics.compute_members(conn, variety=variety, lookback=lookback)
        return {"ok": True, "error": None, "variety": variety,
                "lookback": lookback, "member": member, "rows": rows}

    return _cached(("members", variety, lookback, member), produce)


def api_sync_status(q):
    st = SYNC.status()
    return {"ok": True, "error": None, **st}


def api_sync_start(q, body: dict | None):
    start = None
    if body and body.get("start"):
        start = str(body["start"])
    return SYNC.start(start=start)


def api_export(q):
    """导出分析用 CSV：date,index_close,index_ret_pct,citic_net,citic_net_pct,dnet,market_oi

    注意：variety_daily 里没有 citic_net_pct 列（派生指标不落库），这里用
    citic_net / market_oi * 100 现算；index_ret_pct 为对数收益 %。
    """
    variety = _variety_from_query(q)
    lookback = _parse_int((q.get("lookback") or [config.DEFAULT_LOOKBACK])[0],
                          config.DEFAULT_LOOKBACK, 0, 5000)
    idx = config.VARIETY_INFO[variety]["index_code"]
    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT v.date, i.close, v.citic_net, v.open_interest,
                   v.citic_long, v.citic_short, v.top20_net, v.main_close
            FROM variety_daily v
            LEFT JOIN index_daily i ON i.code = ? AND i.date = v.date
            WHERE v.variety = ?
            ORDER BY v.date
            """,
            (idx, variety),
        ).fetchall()
    if lookback and len(rows) > lookback:
        rows = rows[-lookback:]

    def _ret_pct(prev_close, close):
        if not prev_close or not close or prev_close <= 0 or close <= 0:
            return None
        return round(math.log(close / prev_close) * 100, 4)

    buff = io.StringIO()
    writer = csv.writer(buff)
    writer.writerow(["date", "index_close", "index_ret_pct", "citic_net",
                     "citic_net_pct", "dnet", "market_oi", "citic_long",
                     "citic_short", "top20_net", "main_close"])
    prev_net = None
    prev_close = None
    for r in rows:
        date_s, close, net, oi, cl, cs, t20, mc = r
        net_pct = round(net * 100.0 / oi, 4) if (net is not None and oi) else None
        dnet = (net - prev_net) if (net is not None and prev_net is not None) else None
        writer.writerow([date_s, close, _ret_pct(prev_close, close), net, net_pct,
                         dnet, oi, cl, cs, t20, mc])
        prev_net = net if net is not None else prev_net
        prev_close = close if close else prev_close
    fname = "citic_%s_%s.csv" % (variety, datetime.now(BEIJING).strftime("%Y%m%d"))
    return {"__csv__": True,
            "filename": fname,
            "body": "\ufeff" + buff.getvalue()}


ROUTES_GET = {
    "/api/health": api_health,
    "/api/meta": api_meta,
    "/api/dashboard": api_dashboard,
    "/api/ranks": api_ranks,
    "/api/members": api_members,
    "/api/sync/status": api_sync_status,
    "/api/export": api_export,
}


# --------------------------------------------------------------------------
# 静态资源
# --------------------------------------------------------------------------
MIME_OVERRIDES = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}

_FALLBACK_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>中信期货股指持仓看板 · 安装中</title>
<style>body{background:#0d1117;color:#c9d1d9;font-family:system-ui,"Microsoft YaHei";
padding:48px;line-height:1.8}code{background:#161b22;padding:2px 6px;border-radius:4px}
h1{color:#e6edf3;font-size:20px}a{color:#58a6ff}</style>
<h1>看板尚未安装完整</h1>
<p>前端静态文件 <code>frontend/index.html</code> 还不存在。请先运行：</p>
<p><code>run.ps1</code> 或 <code>python app.py</code>（会自动提示缺少的组件）</p>
<p>接口自检：<a href="/api/health">/api/health</a> ·
<a href="/api/meta">/api/meta</a></p></html>"""


def resolve_static(path: str):
    """把 URL path 映射到 frontend 目录下的真实文件，防目录穿越。"""
    root = os.path.realpath(config.FRONTEND_DIR)
    clean = urllib.parse.unquote(path.split("?", 1)[0])
    if clean in ("", "/"):
        clean = "/index.html"
    target = os.path.realpath(os.path.join(root, clean.lstrip("/")))
    if not (target == root or target.startswith(root + os.sep)):
        return None
    if os.path.isdir(target):
        idx = os.path.join(target, "index.html")
        if os.path.isfile(idx):
            return idx
        return None
    if os.path.isfile(target):
        return target
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "CiticFuturesDash/1.0"
    protocol_version = "HTTP/1.1"

    # 关掉默认的烦人日志，只打印错误
    def log_message(self, fmt, *args):  # noqa: A003
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            print("[http] %s %s" % (args[0], args[1]))

    # --- 工具 -------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, payload, status: int = 200):
        self._send(status, dumps(payload), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    # --- 请求入口 ---------------------------------------------------------
    def do_GET(self):  # noqa: N802
        try:
            self._handle("GET")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(_err("%s: %s" % (type(exc).__name__, exc)), 500)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_POST(self):  # noqa: N802
        try:
            self._handle("POST")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(_err("%s: %s" % (type(exc).__name__, exc)), 500)

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"", "text/plain", {
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    def _handle(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if path.startswith("/api/"):
            return self._handle_api(method, path, query)

        if method == "POST":
            return self._send_json({"ok": False, "error": "not found"}, 404)
        return self._handle_static(path)

    def _handle_api(self, method: str, path: str, query: dict):
        if method == "POST" and path == "/api/sync":
            body = {}
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    raw = self.rfile.read(min(length, 1 << 20))
                    body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            return self._send_json(api_sync_start(query, body))

        handler = ROUTES_GET.get(path)
        if handler is None:
            return self._send_json({"ok": False, "error": "接口不存在: %s" % path}, 404)
        if method not in ("GET", "HEAD"):
            return self._send_json({"ok": False, "error": "method not allowed"}, 405)
        try:
            payload = handler(query)
        except Exception as exc:  # noqa: BLE001
            # 契约要求接口恒 200，前端据此展示错误提示而不是白屏
            traceback.print_exc()
            return self._send_json({
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "empty": True,
            })
        if isinstance(payload, dict) and payload.get("__csv__"):
            body = payload["body"].encode("utf-8")
            return self._send(200, body, "text/csv; charset=utf-8", {
                "Content-Disposition": 'attachment; filename="%s"' % payload["filename"],
                "Cache-Control": "no-store",
            })
        return self._send_json(payload)

    def _handle_static(self, path: str):
        target = resolve_static(path)
        if target is None:
            if path in ("/", "/index.html") and not os.path.isfile(
                    os.path.join(config.FRONTEND_DIR, "index.html")):
                return self._send(200, _FALLBACK_HTML.encode("utf-8"),
                                  "text/html; charset=utf-8")
            return self._send(404, b"404 not found",
                              "text/plain; charset=utf-8")
        ext = os.path.splitext(target)[1].lower()
        ctype = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(target)[0] or \
            "application/octet-stream"
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, b"404 file read error", "text/plain; charset=utf-8")
        # 开发期不缓存，改完刷新即生效
        return self._send(200, body, ctype, {"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def make_server(host: str = config.SERVER_HOST, port: int = config.SERVER_PORT,
                quiet: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    if not quiet:
        print("=" * 64)
        print(" 中信期货 · 股指期货持仓分析看板")
        print(" 地址: http://%s:%d/" % (host, port))
        print(" 按 Ctrl+C 停止")
        print("=" * 64)
    return httpd


def find_free_port(host: str, start_port: int) -> int:
    for port in range(start_port, start_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port near %d" % start_port)
