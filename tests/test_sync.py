# -*- coding: utf-8 -*-
"""离线单测：数据层（HTTP 工具 / 中金所解析 / 指数解析 / 聚合 / 同步编排）。

运行方式（不需要网络）::

    python tests/test_sync.py
    python -m unittest tests.test_sync -v

覆盖点：
1. soft-404 判定（HTTP 200 + 2132 字节错误页）与 GBK 解码、重试收敛为 FetchError；
2. 中金所排名 CSV：两行表头、合约右补空格、rank 1..20、非期货公司占位行、9 列残行；
3. 中金所日行情 CSV：只要 IF/IH/IC/IM 期货合约，跳过期权行与「合计」行；
4. 会员名括号后缀归一化（全角/半角）与改名断点识别；
5. 指数三源解析 + 多源合并优先级（腾讯 > 新浪 > 东财）；
6. rebuild_aggregates 的 is_main / member_daily / variety_daily 口径（含 basis、citic_*）；
7. sync_all：逐日落库、partial/failed 不中断、非交易日记为 empty、二次运行幂等跳过、
   progress 回调单调、kv 元数据写入。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from backend import db, http_util, sync  # noqa: E402
from backend.sources import cffex, index_data  # noqa: E402

MEMBER = config.DEFAULT_MEMBER          # 中信期货
OTHER = "国泰君安"


# ------------------------------------------------------------------ 夹具
RANK_HEADER_1 = "交易日,合约,排名,成交量排名,,,持买单量排名,,,持卖单量排名,,"
RANK_HEADER_2 = (
    ",,,会员简称,成交量,比上一交易日增减,会员简称,持买单量,比上一交易日增减,"
    "会员简称,持卖单量,比上一交易日增减"
)
RANK_CSV = "\n".join(
    [
        RANK_HEADER_1,
        RANK_HEADER_2,
        "20220104,IF2201                        ,1,国泰君安,14738,3347,国泰君安,9587,838,中信期货,10403,650",
        "20220104,IF2201                        ,2,中信期货,12000,100,中信期货,8000,200,国泰君安,9000,300",
        "20220104,IF2201                        ,3,非期货公司,0,0,非期货公司,0,0,非期货公司,0,0",
        "20220104,IF2201,4,9列残行,1,2",  # 残行：9 列，必须丢弃
        "20220104,IF2206                        ,1,中信期货,7000,-100,中信期货,4000,50,中信期货,4500,60",
        "20220104,IF2206                        ,2,华泰期货,6000,10,华泰期货,3000,20,华泰期货,3100,30",
    ]
)

DAILY_HEADER = (
    "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,"
    "今收盘,今结算,前结算,涨跌1,涨跌2,Delta"
)
DAILY_CSV = "\n".join(
    [
        DAILY_HEADER,
        "IF2201,4906.6,4930.0,4880.2,89876,131862419520.0,112084,25714,4910.2,4906.8,4900.0,10.2,6.8,0.0",
        "IH2201,3300.0,3320.0,3280.5,40000,66000000000.0,50000,1000,3310.0,3308.0,3301.0,9.0,7.0,0.0",
        "IC2201,7100.0,7150.0,7050.0,30000,210000000000.0,60000,2000,7120.0,7110.0,7090.0,30.0,20.0,0.0",
        "IM2208,6800.0,6850.0,6750.0,20000,136000000000.0,40000,5000,6820.0,6810.0,6790.0,30.0,20.0,0.0",
        "HO2201-C-3000,25.0,30.0,20.0,500,1500000.0,800,10,26.0,25.5,24.0,2.0,1.5,0.5",
        "合计,,,,455966,41327409385.0,1120843.0,25714.0,,,,,,",
    ]
)

THUMB_404 = ("<html><head><title>网页错误</title></head><body>" + "x" * 2000 + "</body></html>").encode("gbk")

TENCENT_JSON = json.dumps(
    {
        "code": 0,
        "data": {
            "sh000300": {
                "qfqday": [
                    ["2022-01-04", "4900.00", "4910.00", "4920.00", "4890.00", "123456"],
                    ["2022-01-05", "4910.00", "4925.50", "4930.00", "4905.00", "223456"],
                ]
            }
        },
    }
)
SINA_JSON = json.dumps(
    [
        {"day": "2022-01-04", "open": "4900.00", "high": "4920.00", "low": "4890.00",
         "close": "4910.00", "volume": "12345678"},
        {"day": "2022-01-05", "open": "4910.00", "high": "4930.00", "low": "4905.00",
         "close": "4925.50", "volume": "22345678"},
    ]
)
EASTMONEY_JSON = json.dumps(
    {
        "rc": 0,
        "data": {
            "code": "000300",
            "klines": [
                "2022-01-04,4900.00,4910.00,4920.00,4890.00,12345678,1.2e10,0.6,0.1,0.2,0.3",
                "2022-01-05,4910.00,4925.50,4930.00,4905.00,22345678,2.3e10,0.5,0.1,0.2,0.3",
            ],
        },
    }
)


def rank_row(date, variety, contract, rank_type, rank, member, position,
             member_raw=None, change=0):
    return {
        "date": date, "variety": variety, "contract": contract, "rank_type": rank_type,
        "rank": rank, "member": member, "member_raw": member_raw or member,
        "position": position, "change": change,
    }


def fut_row(date, variety, contract, close, open_interest, settle=None, volume=1000):
    return {
        "date": date, "variety": variety, "contract": contract,
        "open": close - 5, "high": close + 10, "low": close - 10,
        "close": close, "settle": settle if settle is not None else close - 1,
        "prev_settle": close - 2, "volume": volume, "amount": volume * close * 300,
        "open_interest": open_interest, "oi_change": 10, "is_main": 0,
    }


# ------------------------------------------------------------------ HTTP 工具
class TestHttpUtil(unittest.TestCase):
    def test_decode_gbk(self):
        self.assertEqual(http_util.decode_text(b"\xd6\xd0\xd0\xc5\xc6\xda\xbb\xf5", "gbk"), "中信期货")

    def test_soft_404_empty_and_marker(self):
        self.assertTrue(http_util.is_soft_404(b""))
        self.assertTrue(http_util.is_soft_404(THUMB_404))

    def test_soft_404_size_only(self):
        # 2132 字节、无中文标记、不是 CSV -> 判定为错误页
        self.assertTrue(http_util.is_soft_404(b"y" * http_util.SOFT_404_SIZE))

    def test_real_csv_not_soft_404(self):
        body = DAILY_CSV.encode("gbk")
        self.assertFalse(http_util.is_soft_404(body))
        self.assertTrue(http_util.looks_like_csv(DAILY_CSV))
        # 长度恰好逼近 2132 字节的真实 CSV 也不能误判
        small_csv = "20220104,IF2201,1,国泰君安,1,2\n" * 68
        body = small_csv.encode("gbk")
        self.assertLess(abs(len(body) - http_util.SOFT_404_SIZE), http_util.SOFT_404_SIZE_TOL)
        self.assertFalse(http_util.is_soft_404(body))

    def test_fetch_bytes_http_404_raises_soft404(self):
        err = urllib.error.HTTPError("http://x/y.csv", 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(http_util.Soft404):
                http_util.fetch_bytes("http://x/y.csv", retries=2)

    def test_fetch_bytes_retries_then_ok(self):
        calls = {"n": 0}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ok-body"

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("boom")
            return Resp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                mock.patch("backend.http_util.time.sleep"):
            out = http_util.fetch_bytes("http://x/y.csv", retries=3)
        self.assertEqual(out, b"ok-body")
        self.assertEqual(calls["n"], 3)

    def test_fetch_bytes_exhausted_raises_fetch_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")), \
                mock.patch("backend.http_util.time.sleep"):
            with self.assertRaises(http_util.FetchError):
                http_util.fetch_bytes("http://x/y.csv", retries=1)


# ------------------------------------------------------------------ 中金所解析
class TestCffexParsing(unittest.TestCase):
    def test_norm_date(self):
        self.assertEqual(cffex.norm_date("20220104"), "2022-01-04")
        self.assertEqual(cffex.norm_date("2022-01-04"), "2022-01-04")
        self.assertEqual(cffex.norm_date("2022/01/04"), "2022-01-04")
        with self.assertRaises(ValueError):
            cffex.norm_date("")

    def test_normalize_member(self):
        self.assertEqual(cffex.normalize_member("中信期货(代客)"), "中信期货")
        self.assertEqual(cffex.normalize_member("中信期货（代客）"), "中信期货")
        self.assertEqual(cffex.normalize_member("中信期货（自营）"), "中信期货")
        self.assertEqual(cffex.normalize_member("中信期货"), "中信期货")
        self.assertEqual(cffex.normalize_member(" 国泰君安 "), "国泰君安")
        # 中间括号不能被当成后缀误删
        self.assertEqual(cffex.normalize_member("中(信)期货"), "中(信)期货")
        self.assertTrue(cffex.member_raw_has_suffix("中信期货(代客)"))
        self.assertFalse(cffex.member_raw_has_suffix("中信期货"))

    def test_urls(self):
        self.assertIn("/sj/ccpm/202201/04/IF_1.csv", cffex.url_rank("2022-01-04", "if"))
        self.assertTrue(cffex.url_daily("2022-01-04").endswith("/sj/hqsj/rtj/202201/04/20220104_1.csv"))

    def test_parse_rank_csv(self):
        rows = cffex.parse_rank_csv(RANK_CSV, date="2022-01-04")
        # 4 条有效数据行 × 3 榜单；非期货公司行与 9 列残行被丢弃
        self.assertEqual(len(rows), 12)
        self.assertEqual({r["rank_type"] for r in rows}, {"vol", "long", "short"})
        self.assertEqual({r["contract"] for r in rows}, {"IF2201", "IF2206"})
        self.assertEqual({r["variety"] for r in rows}, {"IF"})
        self.assertTrue(all(r["rank"] in (1, 2) for r in rows))
        self.assertTrue(all(r["date"] == "2022-01-04" for r in rows))
        self.assertNotIn("非期货公司", {r["member"] for r in rows})
        if_row = [r for r in rows if r["contract"] == "IF2201" and r["rank"] == 1]
        long_row = [r for r in if_row if r["rank_type"] == "long"][0]
        self.assertEqual(long_row["member"], OTHER)
        self.assertEqual(long_row["position"], 9587)
        self.assertEqual(long_row["change"], 838)
        self.assertEqual(long_row["member_raw"], OTHER)

    def test_parse_rank_csv_suffix_and_filter(self):
        csv_text = "\n".join(
            [
                RANK_HEADER_1,
                RANK_HEADER_2,
                "20240506,IF2405                        ,1,中信期货(代客),11057,-6404,"
                "国泰君安(代客),10140,-730,中信期货(代客),9000,10",
                "20240506,IH2405                        ,1,中信期货(代客),9000,1,"
                "国泰君安(代客),8000,2,中信期货(代客),7000,3",
            ]
        )
        rows = cffex.parse_rank_csv(csv_text, variety="IF", date="2024-05-06")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["variety"] == "IF" for r in rows))
        self.assertEqual({r["member"] for r in rows}, {MEMBER, OTHER})
        self.assertEqual({r["member_raw"] for r in rows}, {"中信期货(代客)", "国泰君安(代客)"})

    def test_parse_daily_csv(self):
        rows = cffex.parse_daily_csv(DAILY_CSV, date="2022-01-04")
        self.assertEqual(len(rows), 4)  # 期权行与「合计」行被丢弃
        self.assertEqual({r["contract"] for r in rows},
                         {"IF2201", "IH2201", "IC2201", "IM2208"})
        if_row = [r for r in rows if r["contract"] == "IF2201"][0]
        self.assertEqual(if_row["variety"], "IF")
        self.assertEqual(if_row["date"], "2022-01-04")
        self.assertAlmostEqual(if_row["open"], 4906.6)
        self.assertAlmostEqual(if_row["high"], 4930.0)
        self.assertAlmostEqual(if_row["low"], 4880.2)
        self.assertAlmostEqual(if_row["close"], 4910.2)
        self.assertAlmostEqual(if_row["settle"], 4906.8)
        self.assertAlmostEqual(if_row["prev_settle"], 4900.0)
        self.assertEqual(if_row["volume"], 89876)
        self.assertEqual(if_row["open_interest"], 112084)
        self.assertEqual(if_row["oi_change"], 25714)
        self.assertEqual(if_row["is_main"], 0)


# ------------------------------------------------------------------ 指数解析
class TestIndexParsing(unittest.TestCase):
    def test_parse_tencent(self):
        rows = index_data.parse_tencent(TENCENT_JSON, "sh000300")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "2022-01-04")
        self.assertAlmostEqual(rows[0][1], 4900.0)   # open
        self.assertAlmostEqual(rows[0][2], 4920.0)   # high
        self.assertAlmostEqual(rows[0][3], 4890.0)   # low
        self.assertAlmostEqual(rows[0][4], 4910.0)   # close
        self.assertAlmostEqual(rows[0][5], 123456.0)  # volume
        self.assertIsNone(rows[0][6])

    def test_parse_sina(self):
        rows = index_data.parse_sina(SINA_JSON)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[1][4], 4925.5)
        self.assertAlmostEqual(rows[1][5], 22345678.0)

    def test_parse_eastmoney(self):
        rows = index_data.parse_eastmoney(EASTMONEY_JSON)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[1][4], 4925.5)
        self.assertAlmostEqual(rows[1][6], 2.3e10)

    def test_merge_priority(self):
        t_rows = index_data.parse_tencent(TENCENT_JSON, "sh000300")
        s_rows = index_data.parse_sina(SINA_JSON)
        merged = index_data._merge({"tencent": t_rows[:1], "sina": s_rows}, "2022-01-01", "2022-12-31")
        self.assertEqual([r[0] for r in merged], ["2022-01-04", "2022-01-05"])
        self.assertEqual(merged[0][7], "tencent")   # 同日取高优先级源
        self.assertEqual(merged[1][7], "sina")      # 缺失日期由低优先级源补

    def test_fetch_history_fallback_and_shape(self):
        calls = []

        def fake_tencent(symbol, start, end):
            calls.append("tencent")
            raise http_util.FetchError("tencent down")

        def fake_sina(symbol, start, end):
            calls.append("sina")
            return index_data.parse_sina(SINA_JSON)

        def fake_eastmoney(info, start, end):
            calls.append("eastmoney")
            raise http_util.FetchError("eastmoney down")

        with mock.patch("backend.sources.index_data._fetch_tencent", side_effect=fake_tencent), \
                mock.patch("backend.sources.index_data._fetch_sina", side_effect=fake_sina), \
                mock.patch("backend.sources.index_data._fetch_eastmoney", side_effect=fake_eastmoney):
            rows, source = index_data.fetch_history_detail("000300", start="2022-01-01", end="2022-12-31")
            self.assertEqual(calls, ["tencent", "sina", "eastmoney"])
            self.assertEqual(source, "sina")
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0]), 7)
            # 契约签名：返回 7 元组且顺序一致
            self.assertEqual(index_data.fetch_history("000300", "2022-01-01", "2022-12-31"), rows)

    def test_basis_pct_uses_same_day_index(self):
        self.assertEqual(index_data.index_code_of("IM"), "000852")

    def test_narrow_window_padded_and_empty_ok(self):
        """窄区间（整段是节假日）向后垫窗口取数，返回空而不是报错。"""
        seen = {}

        def fake_tencent(symbol, start, end):
            seen["start"] = start
            return [("2022-01-04", 4900.0, 4920.0, 4890.0, 4910.0, 1e8, None)]

        with mock.patch("backend.sources.index_data._fetch_tencent", side_effect=fake_tencent), \
                mock.patch("backend.sources.index_data._fetch_sina",
                           side_effect=lambda *a, **k: []), \
                mock.patch("backend.sources.index_data._fetch_eastmoney",
                           side_effect=http_util.FetchError("down")):
            rows, source = index_data.fetch_history_detail("000300", start="2022-01-03", end="2022-01-03")
        self.assertEqual(rows, [])
        self.assertEqual(source, "none")
        # 请求窗口被向前垫过，避免"窗口内无交易日"被误判为抓取失败
        self.assertLess(seen["start"], "2022-01-03")

    def test_all_sources_failed_raises(self):
        with mock.patch("backend.sources.index_data._fetch_tencent",
                        side_effect=http_util.FetchError("t")), \
                mock.patch("backend.sources.index_data._fetch_sina",
                           side_effect=http_util.FetchError("s")), \
                mock.patch("backend.sources.index_data._fetch_eastmoney",
                           side_effect=http_util.FetchError("e")):
            with self.assertRaises(http_util.FetchError):
                index_data.fetch_history_detail("000300", start="2022-01-04", end="2022-01-05")


# ------------------------------------------------------------------ 聚合口径
class TestAggregates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fc_agg_")
        self.db_path = os.path.join(self.tmp, "market.db")
        self._patch = mock.patch.object(config, "DB_PATH", self.db_path)
        self._patch.start()
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        d1 = "2022-01-04"
        self.conn.executemany(
            "INSERT INTO fut_daily(date,variety,contract,open,high,low,close,settle,prev_settle,"
            "volume,amount,open_interest,oi_change,is_main) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            [
                (d1, "IF", "IF2201", 1, 1, 1, 4900.0, 4899.0, 4898.0, 1000, 1.0, 100, 1),
                (d1, "IF", "IF2206", 1, 1, 1, 4950.0, 4949.0, 4948.0, 2000, 2.0, 200, 2),
            ],
        )
        self.conn.executemany(
            "INSERT INTO member_rank(date,variety,contract,rank_type,rank,member,member_raw,position,change)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (d1, "IF", "IF2201", "vol", 1, MEMBER, MEMBER, 400, 0),
                (d1, "IF", "IF2201", "long", 1, MEMBER, MEMBER, 500, 0),
                (d1, "IF", "IF2201", "long", 2, OTHER, OTHER, 300, 0),
                (d1, "IF", "IF2201", "short", 1, MEMBER, MEMBER, 600, 0),
                (d1, "IF", "IF2206", "long", 1, MEMBER, MEMBER + "(代客)", 100, 0),
                (d1, "IF", "IF2206", "short", 1, MEMBER, MEMBER + "(代客)", 50, 0),
            ],
        )
        self.conn.executemany(
            "INSERT INTO index_daily(code,date,open,high,low,close,volume,amount,source)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("000300", d1, 4900.0, 4930.0, 4880.0, 4910.0, 1e8, 1.0, "test"),
                ("000016", d1, 3300.0, 3310.0, 3290.0, 3300.0, 1e8, 1.0, "test"),
                ("000905", d1, 7100.0, 7120.0, 7080.0, 7110.0, 1e8, 1.0, "test"),
                ("000852", d1, 6800.0, 6820.0, 6780.0, 6810.0, 1e8, 1.0, "test"),
            ],
        )
        self.conn.commit()

    def test_rebuild_aggregates(self):
        self._seed()
        out = sync.rebuild_aggregates(self.conn)
        self.assertEqual(out["variety_daily"], 1)
        self.assertEqual(out["member_daily"], 2)

        main = self.conn.execute(
            "SELECT contract, is_main FROM fut_daily ORDER BY contract"
        ).fetchall()
        self.assertEqual([(r["contract"], r["is_main"]) for r in main],
                         [("IF2201", 0), ("IF2206", 1)])

        md = {r["member"]: dict(r) for r in self.conn.execute("SELECT * FROM member_daily")}
        self.assertEqual(md[MEMBER]["long_pos"], 600)     # 500 + 100
        self.assertEqual(md[MEMBER]["short_pos"], 650)    # 600 + 50
        self.assertEqual(md[MEMBER]["net_pos"], -50)
        self.assertEqual(md[MEMBER]["volume"], 400)
        self.assertEqual(md[OTHER]["long_pos"], 300)
        self.assertEqual(md[OTHER]["short_pos"], 0)
        self.assertEqual(md[OTHER]["net_pos"], 300)

        vd = dict(self.conn.execute("SELECT * FROM variety_daily").fetchone())
        self.assertEqual(vd["main_contract"], "IF2206")
        self.assertAlmostEqual(vd["main_close"], 4950.0)
        self.assertEqual(vd["volume"], 3000)             # 1000 + 2000
        self.assertEqual(vd["open_interest"], 300)       # 100 + 200
        self.assertEqual(vd["top20_long"], 900)          # 500+300+100
        self.assertEqual(vd["top20_short"], 650)         # 600+50
        self.assertEqual(vd["top20_net"], 250)
        self.assertEqual(vd["citic_long"], 600)
        self.assertEqual(vd["citic_short"], 650)
        self.assertEqual(vd["citic_net"], -50)
        self.assertAlmostEqual(vd["basis"], 4950.0 - 4910.0)
        self.assertAlmostEqual(vd["basis_pct"], round(40.0 / 4910.0 * 100.0, 4))

    def test_rebuild_is_idempotent(self):
        self._seed()
        sync.rebuild_aggregates(self.conn)
        first = [tuple(r) for r in self.conn.execute("SELECT * FROM variety_daily ORDER BY date")]
        sync.rebuild_aggregates(self.conn)
        second = [tuple(r) for r in self.conn.execute("SELECT * FROM variety_daily ORDER BY date")]
        self.assertEqual(first, second)

    def test_rebuild_without_index_gives_null_basis(self):
        self._seed()
        self.conn.execute("DELETE FROM index_daily")
        self.conn.commit()
        sync.rebuild_aggregates(self.conn)
        vd = dict(self.conn.execute("SELECT * FROM variety_daily").fetchone())
        self.assertIsNone(vd["basis"])
        self.assertIsNone(vd["basis_pct"])
        self.assertEqual(vd["citic_net"], -50)


# ------------------------------------------------------------------ 同步编排
class TestSyncFlow(unittest.TestCase):
    """用 mock 数据源在临时库上跑完整 sync_all，不产生任何网络请求。"""

    D1, D2, D3, D4 = "2022-01-04", "2022-01-05", "2022-01-06", "2022-01-07"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fc_sync_")
        self.db_path = os.path.join(self.tmp, "market.db")
        self._patch = mock.patch.object(config, "DB_PATH", self.db_path)
        self._patch.start()
        self.calls = {"daily": [], "rank": [], "index": []}
        self.fail_dates = set()
        self.empty_dates = set()        # hqsj 缺失
        self.empty_rank_dates = set()   # ccpm 缺失
        self._patches = [
            mock.patch("backend.sync.cffex.fetch_daily_quotes", side_effect=self._fake_daily),
            mock.patch("backend.sync.cffex.fetch_rank", side_effect=self._fake_rank),
            mock.patch("backend.sync.index_data.fetch_history_detail",
                       side_effect=self._fake_index),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 假数据源
    def _fake_daily(self, date):
        self.calls["daily"].append(date)
        if date in self.empty_dates:
            raise http_util.Soft404(f"hqsj 缺失：{date}")
        if date in self.fail_dates:
            raise http_util.FetchError(f"模拟网络失败：{date}")
        base = 4900.0 + (dt.date.fromisoformat(date).day * 3.0)
        return [
            fut_row(date, "IF", "IF2201", base, 100),
            fut_row(date, "IF", "IF2206", base + 50, 200),
            fut_row(date, "IH", "IH2201", 3300.0, 80),
            fut_row(date, "IC", "IC2201", 7100.0, 90),
        ]

    def _fake_rank(self, date, variety):
        self.calls["rank"].append((date, variety))
        if date in self.empty_rank_dates:
            raise http_util.Soft404(f"ccpm 缺失：{date} {variety}")
        if (date, variety) in self.fail_dates:
            raise http_util.FetchError(f"模拟网络失败：{date} {variety}")
        if variety != "IF":
            return [rank_row(date, variety, f"{variety}2201", "long", 1, MEMBER, 100)]
        # 注意 D3 用带 (代客) 后缀的原始名，用于验证改名断点
        raw = MEMBER + "(代客)" if date >= self.D3 else MEMBER
        return [
            rank_row(date, "IF", "IF2201", "vol", 1, MEMBER, 400),
            rank_row(date, "IF", "IF2201", "long", 1, MEMBER, 500, raw),
            rank_row(date, "IF", "IF2201", "long", 2, OTHER, 300),
            rank_row(date, "IF", "IF2201", "short", 1, MEMBER, 600, raw),
            rank_row(date, "IF", "IF2206", "long", 1, MEMBER, 100, raw),
            rank_row(date, "IF", "IF2206", "short", 1, MEMBER, 50, raw),
        ]

    def _fake_index(self, code, start=None, end=None):
        self.calls["index"].append((code, start, end))
        closes = {"000300": 4910.0, "000016": 3300.0, "000905": 7110.0, "000852": 6810.0}
        rows = []
        for date in (self.D1, self.D2, self.D3, self.D4):
            c = closes[code] + (dt.date.fromisoformat(date).day - 4)
            rows.append((date, c - 5, c + 10, c - 10, c, 1e8, None))
        return rows, "test"

    # -- 测试
    def test_full_sync_writes_and_aggregates(self):
        self.empty_dates = {self.D4}         # D4 指数有数据但中金所无文件
        self.empty_rank_dates = {self.D4}
        progress = []
        out = sync.sync_all(start=self.D1, end=self.D4,
                            progress=lambda d, t, l: progress.append((d, t, l)))
        self.assertTrue(out["ok"], out["failed"])
        self.assertEqual(out["failed"], [])
        self.assertEqual(out["range"], {"start": self.D1, "end": self.D4})
        self.assertEqual(out["index_rows"], 16)          # 4 指数 × 4 天
        self.assertEqual(out["fut_rows"], 12)            # 3 个中金所交易日 × 4 合约
        self.assertEqual(out["rank_days"], 3)            # D4 中金所无文件

        conn = db.connect(self.db_path)
        try:
            self.assertEqual(db.table_count(conn, "index_daily"), 16)
            self.assertEqual(db.table_count(conn, "fut_daily"), 12)
            self.assertEqual(db.table_count(conn, "variety_daily"), 9)   # 3 天 × IF/IH/IC
            # IM 只有排名、没有行情，故 variety_daily 无 IM 而 member_daily 有：3 天 × 5
            self.assertEqual(db.table_count(conn, "member_daily"), 15)

            # 非交易日（index 有数据但中金所文件缺失）记为 empty，且不重抓
            status = conn.execute("SELECT status FROM sync_log WHERE date=?", (self.D4,)).fetchone()
            self.assertEqual(status["status"], "empty")

            vd = dict(conn.execute(
                "SELECT * FROM variety_daily WHERE date=? AND variety='IF'", (self.D1,)
            ).fetchone())
            self.assertEqual(vd["main_contract"], "IF2206")
            self.assertEqual(vd["open_interest"], 300)
            self.assertEqual(vd["citic_long"], 600)
            self.assertEqual(vd["citic_short"], 650)
            self.assertEqual(vd["citic_net"], -50)
            # main_close = 4900 + 4*3 + 50 = 4962；指数 close = 4910 + (4-4) = 4910
            self.assertAlmostEqual(vd["main_close"], 4962.0)
            self.assertAlmostEqual(vd["basis"], 52.0)
            self.assertAlmostEqual(vd["basis_pct"], round(52.0 / 4910.0 * 100.0, 4))

            self.assertEqual(db.kv_get(conn, "last_sync_range"), f"{self.D1}..{self.D4}")
            self.assertTrue(db.kv_get(conn, "aggregates_at"))
            self.assertEqual(db.kv_get(conn, "member_naming_break_date"), self.D3)
        finally:
            conn.close()

        # progress 单调且以 done == total 收尾
        dones = [p[0] for p in progress]
        self.assertEqual(dones, sorted(dones))
        self.assertEqual(dones[-1], progress[-1][1])
        self.assertTrue(any("重建聚合表" in p[2] for p in progress))

    def test_second_run_is_idempotent(self):
        sync.sync_all(start=self.D1, end=self.D4)
        conn = db.connect(self.db_path)
        counts_before = {t: db.table_count(conn, t) for t in
                         ("index_daily", "fut_daily", "member_rank", "member_daily", "variety_daily")}
        conn.close()
        daily_calls, rank_calls = len(self.calls["daily"]), len(self.calls["rank"])

        out2 = sync.sync_all(start=self.D1, end=self.D4)
        self.assertTrue(out2["ok"], out2["failed"])
        self.assertEqual(out2["days_fetched"], 0)
        self.assertEqual(out2["fut_rows"], 0)
        self.assertEqual(len(self.calls["daily"]), daily_calls)   # 没有多余的中金所请求
        self.assertEqual(len(self.calls["rank"]), rank_calls)

        conn = db.connect(self.db_path)
        try:
            counts_after = {t: db.table_count(conn, t) for t in counts_before}
            self.assertEqual(counts_before, counts_after)
        finally:
            conn.close()

    def test_single_day_failure_does_not_abort(self):
        self.fail_dates = {(self.D2, "IF")}
        out = sync.sync_all(start=self.D1, end=self.D3)
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["failed"]), 1)
        self.assertEqual(out["failed"][0]["date"], self.D2)
        self.assertEqual(out["failed"][0]["variety"], "IF")
        self.assertIn("error", out["failed"][0]["reason"])
        # 其余日期照常落库
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(db.latest_date(conn, "fut_daily"), self.D3)
            self.assertEqual(db.table_count(conn, "variety_daily"), 9)   # 3 天 × IF/IH/IC
            st = conn.execute("SELECT status FROM sync_log WHERE date=?", (self.D2,)).fetchone()
            self.assertEqual(st["status"], "partial")
        finally:
            conn.close()

    def test_soft404_day_recorded_as_empty_and_skipped_later(self):
        self.empty_dates = {self.D2}
        self.empty_rank_dates = {self.D2}
        out = sync.sync_all(start=self.D1, end=self.D2)
        self.assertTrue(out["ok"], out["failed"])
        conn = db.connect(self.db_path)
        try:
            st = conn.execute("SELECT status FROM sync_log WHERE date=?", (self.D2,)).fetchone()
            self.assertEqual(st["status"], "empty")
        finally:
            conn.close()
        self.calls["daily"].clear()
        out2 = sync.sync_all(start=self.D1, end=self.D2)
        self.assertEqual(out2["days_fetched"], 0)
        self.assertNotIn(self.D2, self.calls["daily"])

    def test_today_empty_is_retried(self):
        today = dt.date.today().isoformat()
        self.assertTrue(sync._skip_date(today, {today: "empty"}, today) is False)
        self.assertTrue(sync._skip_date("2022-01-04", {"2022-01-04": "empty"}, today))
        self.assertTrue(sync._skip_date("2022-01-04", {"2022-01-04": "ok"}, today))
        self.assertFalse(sync._skip_date("2022-01-04", {"2022-01-04": "failed"}, today))
        self.assertFalse(sync._skip_date("2022-01-04", {}, today))

    def test_trade_calendar_gap_fill(self):
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO fut_daily(date,variety,contract,open_interest) VALUES (?,?,?,?)",
                ("2022-01-06", "IF", "IF2201", 100),
            )
            conn.commit()
            cal = sync._trade_calendar(conn, "2022-01-03", "2022-01-07")
            # 01-03 ~ 01-05 与 01-07 用工作日补齐，周末自动排除
            self.assertEqual(cal, ["2022-01-03", "2022-01-04", "2022-01-05",
                                   "2022-01-06", "2022-01-07"])
        finally:
            conn.close()

    def test_missing_hqsj_but_rank_present_still_fetches_all_varieties(self):
        self.empty_dates = {self.D1}
        self.calls["rank"].clear()
        result = sync._fetch_day(self.D1, list(config.VARIETIES))
        # hqsj 缺失时先探一个品种；探到排名文件存在则补齐其余品种
        self.assertEqual(result["fut_rows"], [])
        self.assertEqual({r["variety"] for r in result["rank_rows"]}, set(config.VARIETIES))
        self.assertEqual(len(result["rank_rows"]), 6 + 3)   # IF 6 行 + 其余品种各 1 行

    def test_default_end_rolls_weekend_back(self):
        self.assertEqual(sync._default_end("2026-10-18"), "2026-10-16")  # 周日 -> 周五
        self.assertEqual(sync._default_end("2026-10-19"), "2026-10-19")  # 周一

    def test_norm_varieties(self):
        self.assertEqual(sync._norm_varieties(["if", " IF ", "IM"]), ["IF", "IM"])
        with self.assertRaises(ValueError):
            sync._norm_varieties(["ZZ"])

    def test_sync_index_keeps_existing_rows_outside_fetch(self):
        # 抓取只返回 D1..D2，库里已有的 D3 不能被删
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO index_daily(code,date,open,high,low,close,volume,amount,source)"
                " VALUES ('000300','2022-01-20',1,2,0.5,2.0,1,1,'old')"
            )
            conn.commit()
            with mock.patch("backend.sync.index_data.fetch_history_detail",
                            return_value=([(self.D1, 1, 2, 0.5, 1.5, 1e8, None)], "test")):
                sync.sync_index(conn, self.D1, self.D2)
            got = {r["date"] for r in conn.execute(
                "SELECT date FROM index_daily WHERE code='000300'")}
            self.assertIn(self.D1, got)
            self.assertIn("2022-01-20", got)   # 窗口外的历史数据保留
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
