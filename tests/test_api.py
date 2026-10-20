# -*- coding: utf-8 -*-
"""API 层离线单测（不起外网、不需要真实数据）。

跑法（项目根目录）：
    python tests/test_api.py
    python -m unittest tests.test_api -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from backend import api, db  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_front = config.FRONTEND_DIR
        config.DB_PATH = os.path.join(self._tmp.name, "test.db")
        self.conn = db.connect()
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.conn.close()
        except Exception:
            pass
        config.DB_PATH = self._orig_db
        config.FRONTEND_DIR = self._orig_front
        self._tmp.cleanup()

    def seed(self, n=300):
        """造一段指数 + 品种日线，够 API 层跑通即可。"""
        import math
        from datetime import date, timedelta

        d0 = date(2024, 1, 1)
        idx_rows, vd_rows = [], []
        for i in range(n):
            d = d0 + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            ds = d.isoformat()
            close = 3500 + 200 * math.sin(i / 25.0) + i * 0.8
            idx_rows.append(("000300", ds, close, close, close, close, 1e8, 1e11, "seed"))
            net = -40000 - 3000 * math.sin(i / 9.0)
            vd_rows.append((ds, "IF", "IF2406", close, 1e5, 7e5,
                            6e5, 6.6e5, 6e5 - 6.6e5,
                            net / 2, net / 2, net, -1.0, -0.03))
        db.upsert_many(self.conn, "index_daily",
                       ["code", "date", "open", "high", "low", "close",
                        "volume", "amount", "source"], idx_rows, ["code", "date"])
        db.upsert_many(self.conn, "variety_daily",
                       ["date", "variety", "main_contract", "main_close", "volume",
                        "open_interest", "top20_long", "top20_short", "top20_net",
                        "citic_long", "citic_short", "citic_net", "basis", "basis_pct"],
                       vd_rows, ["date", "variety"])
        self.conn.commit()

    def call(self, fn, **query):
        q = {k: [str(v)] for k, v in query.items()}
        payload = fn(q)
        raw = api.dumps(payload)
        self.assertNotIn(b"NaN", raw)
        self.assertNotIn(b"Infinity", raw)
        return json.loads(raw.decode("utf-8"))


class TestSerialize(BaseCase):
    def test_clean_nan_and_numpy(self):
        self.assertIsNone(api._clean(float("nan")))
        self.assertIsNone(api._clean(float("inf")))
        self.assertEqual(api._clean(1.23456789), 1.234568)
        self.assertEqual(api.dumps({"a": float("nan")}), b'{"a":null}')
        try:
            import numpy as np
            self.assertIsNone(api._clean(np.float64("nan")))
            self.assertEqual(api._clean(np.int64(7)), 7)
            self.assertEqual(api._clean(np.bool_(True)), True)
        except ImportError:  # pragma: no cover
            pass

    def test_dumps_is_utf8_json(self):
        body = api.dumps({"中文": "值", "list": [1, None, 2.5]})
        self.assertIsInstance(body, bytes)
        self.assertEqual(json.loads(body.decode("utf-8"))["中文"], "值")


class TestStaticResolve(BaseCase):
    def test_traversal_is_blocked(self):
        self.assertIsNone(api.resolve_static("/../config.py"))
        self.assertIsNone(api.resolve_static("/..%2fconfig.py"))
        self.assertIsNone(api.resolve_static("/nope/nope.js"))

    def test_index_fallback(self):
        config.FRONTEND_DIR = self._tmp.name
        self.assertIsNone(api.resolve_static("/index.html"))
        (Path(self._tmp.name) / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
        self.assertEqual(Path(api.resolve_static("/")).name, "index.html")
        self.assertEqual(Path(api.resolve_static("/index.html")).name, "index.html")


class TestEndpoints(BaseCase):
    def test_health_and_meta(self):
        self.seed()
        health = self.call(api.api_health)
        self.assertTrue(health["ok"])
        self.assertGreater(health["rows"]["variety_daily"], 100)

        meta = self.call(api.api_meta)
        self.assertTrue(meta["ok"])
        self.assertEqual(len(meta["varieties"]), 4)
        self.assertIn("中信期货", meta["members"])
        self.assertEqual(meta["default_lookback"], 720)
        self.assertEqual(meta["data"]["by_variety"]["IF"]["end"][:4], "2024")

    def test_empty_db_is_graceful(self):
        # 不 seed：空库也不能抛异常
        health = self.call(api.api_health)
        self.assertTrue(health["ok"])
        self.assertFalse(health["data_ready"])
        meta = self.call(api.api_meta)
        self.assertTrue(meta["ok"])
        self.assertIsNone(meta["latest_trade_date"])
        ranks = self.call(api.api_ranks)
        self.assertFalse(ranks["ok"])
        self.assertTrue(ranks.get("empty"))

    def test_sync_status(self):
        st = self.call(api.api_sync_status)
        self.assertTrue(st["ok"])
        self.assertFalse(st["running"])
        self.assertIn("progress", st)


class TestLiveServer(BaseCase):
    """真正起 HTTP 服务，验证路由、静态托管与 Content-Type。"""

    def setUp(self):
        super().setUp()
        self.seed()
        self._front = Path(self._tmp.name) / "frontend"
        self._front.mkdir()
        (self._front / "index.html").write_text("<h1 id='t'>看板</h1>", encoding="utf-8")
        (self._front / "app.js").write_text("console.log(1)", encoding="utf-8")
        self._orig_front = config.FRONTEND_DIR
        config.FRONTEND_DIR = str(self._front)

        port = api.find_free_port("127.0.0.1", 18790)
        self.httpd = api.make_server("127.0.0.1", port, quiet=True)
        self.port = port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        config.FRONTEND_DIR = self._orig_front

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    def test_api_over_http(self):
        for path in ("/api/health", "/api/meta", "/api/dashboard?variety=IF&lookback=240",
                     "/api/ranks", "/api/members?variety=IF", "/api/sync/status"):
            status, ctype, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("application/json", ctype, path)
            self.assertNotIn(b"NaN", body, path)
            payload = json.loads(body.decode("utf-8"))
            self.assertIn("ok", payload, path)
            self.assertIn("error", payload, path)

    def test_static_over_http(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("看板".encode("utf-8"), body)

        status, ctype, _ = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)

        status, _, _ = self.get("/missing.js")
        self.assertEqual(status, 404)

        status, _, _ = self.get("/api/nope")
        self.assertEqual(status, 404)

    def test_export_csv(self):
        status, ctype, body = self.get("/api/export?variety=IF&lookback=120")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        text = body.decode("utf-8-sig")
        self.assertTrue(text.startswith("date,index_close"))
        self.assertGreater(len(text.splitlines()), 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
