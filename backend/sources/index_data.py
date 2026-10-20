# -*- coding: utf-8 -*-
"""指数日线数据源：腾讯（翻页拼长历史）-> 新浪（1023 根）-> 东方财富（兜底）。

返回统一格式 ``[(date, open, high, low, close, volume, amount_or_None), ...]``，
按日期升序去重；``fetch_history_detail`` 额外返回数据来源名，供 ``index_daily.source`` 落库。

多源合并策略：优先级 腾讯 > 新浪 > 东财，**同一天只用高优先级源的值**，
低优先级源只补主源缺失的日期，避免不同复权口径混用。
"""
from __future__ import annotations

import datetime as dt
import json

import config
from backend import http_util

TENCENT_KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
    "{symbol},day,{start},{end},{count},qfq"
)
TENCENT_PAGE_COUNT = 640      # 契约实测：单次最多 640 根
TENCENT_MAX_PAGES = 10
TENCENT_REFERER = "https://gu.qq.com/"
SINA_REFERER = "https://finance.sina.com.cn/"
EASTMONEY_REFERER = "https://quote.eastmoney.com/"
SOURCE_ORDER = ("tencent", "sina", "eastmoney")


# ------------------------------------------------------------------ 解析
def _row(date: str, open_, high, low, close, volume, amount=None):
    try:
        o = float(open_)
        h = float(high)
        l = float(low)  # noqa: E741
        c = float(close)
        v = float(volume)
    except (TypeError, ValueError):
        return None
    return (str(date)[:10], o, h, l, c, v, (float(amount) if amount not in (None, "") else None))


def parse_tencent(text: str, symbol: str | None = None) -> list[tuple]:
    """解析腾讯 fqkline JSON；数组顺序为 ``[日期, 开, 收, 高, 低, 量]``。"""
    data = json.loads(text)
    if not isinstance(data, dict):
        return []
    node_all = data.get("data") or {}
    node = None
    if symbol and isinstance(node_all, dict):
        node = node_all.get(symbol)
    if node is None and isinstance(node_all, dict) and node_all:
        node = next(iter(node_all.values()))
    if not isinstance(node, dict):
        return []
    arr = node.get("qfqday") or node.get("day") or []
    out: list[tuple] = []
    for item in arr:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        row = _row(item[0], item[1], item[3], item[4], item[2], item[5])
        if row:
            out.append(row)
    return out


def parse_sina(text: str) -> list[tuple]:
    """解析新浪 KLineData JSON（``open/high/low/close/volume`` 为字符串）。"""
    data = json.loads(text)
    if not isinstance(data, list):
        return []
    out: list[tuple] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        row = _row(
            item.get("day"), item.get("open"), item.get("high"),
            item.get("low"), item.get("close"), item.get("volume"),
        )
        if row:
            out.append(row)
    return out


def parse_eastmoney(text: str) -> list[tuple]:
    """解析东财 kline JSON：``klines`` 每项 ``日期,开,收,高,低,量,额,...``。"""
    data = json.loads(text)
    node = (data or {}).get("data") or {}
    klines = node.get("klines") or []
    out: list[tuple] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        amount = parts[6] if len(parts) > 6 else None
        row = _row(parts[0], parts[1], parts[3], parts[4], parts[2], parts[5], amount)
        if row:
            out.append(row)
    return out


def _parse(source: str, text: str, symbol: str) -> list[tuple]:
    if source == "tencent":
        return parse_tencent(text, symbol)
    if source == "sina":
        return parse_sina(text)
    if source == "eastmoney":
        return parse_eastmoney(text)
    raise ValueError(f"未知数据源：{source}")


# ------------------------------------------------------------------ 单源抓取
def _fetch_tencent(symbol: str, start: str, end: str) -> list[tuple]:
    """腾讯：按日期窗口向前翻页，拼出长历史（每页最多 640 根）。"""
    collected: dict[str, tuple] = {}
    cur_end = dt.date.fromisoformat(end)
    start_date = dt.date.fromisoformat(start)
    for _page in range(TENCENT_MAX_PAGES):
        url = TENCENT_KLINE_URL.format(
            symbol=symbol, start=start, end=cur_end.isoformat(),
            count=TENCENT_PAGE_COUNT,
        )
        text = http_util.fetch_text(url, referer=TENCENT_REFERER, encoding="utf-8")
        rows = parse_tencent(text, symbol)
        if not rows:
            break
        for row in rows:
            collected.setdefault(row[0], row)
        first = min(row[0] for row in rows)
        last = max(row[0] for row in rows)
        if first <= start or len(rows) < TENCENT_PAGE_COUNT:
            break
        next_end = dt.date.fromisoformat(first) - dt.timedelta(days=1)
        if next_end < start_date or next_end >= cur_end:
            break
        cur_end = next_end
    return [collected[d] for d in sorted(collected)]


def _fetch_sina(symbol: str, start: str, end: str) -> list[tuple]:
    url = config.SINA_KLINE_URL.format(sina=symbol, n=config.SINA_KLINE_MAX)
    text = http_util.fetch_text(url, referer=SINA_REFERER, encoding="utf-8")
    return parse_sina(text)


def _fetch_eastmoney(info: dict, start: str, end: str) -> list[tuple]:
    url = config.EASTMONEY_KLINE_URL.format(secid=info["secid_em"])
    text = http_util.fetch_text(url, referer=EASTMONEY_REFERER, encoding="utf-8")
    return parse_eastmoney(text)


# ------------------------------------------------------------------ 合并
def _merge(rows_by_source: dict[str, list[tuple]], start: str, end: str) -> list[tuple]:
    merged: dict[str, tuple] = {}
    for source in SOURCE_ORDER:
        for row in rows_by_source.get(source, []):
            if row[0] < start or row[0] > end:
                continue
            merged.setdefault(row[0], row + (source,))
    return [merged[d] for d in sorted(merged)]


def _business_span(start: str, end: str) -> int:
    """区间内工作日数量（粗略覆盖度判断用）。"""
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    n = 0
    while a <= b:
        if a.weekday() < 5:
            n += 1
        a += dt.timedelta(days=1)
    return n


def _coverage_ok(rows: list[tuple], start: str, end: str, tol_bdays: int = 3) -> bool:
    """两侧都要覆盖：首行不晚于 start 附近、末行不早于 end 附近（容忍节假日空隙）。"""
    if not rows:
        return False
    first, last = rows[0][0], rows[-1][0]
    return _business_span(start, first) <= tol_bdays and _business_span(last, end) <= tol_bdays


# ------------------------------------------------------------------ 对外接口
def fetch_history_detail(
    code: str,
    start: str | None = None,
    end: str | None = None,
) -> tuple[list[tuple], str]:
    """按「腾讯 -> 新浪 -> 东财」降级抓取，返回 ``(rows, source)``。

    rows: ``[(date, open, high, low, close, volume, amount_or_None), ...]`` 升序去重。
    """
    if code not in config.INDEX_CODES:
        raise ValueError(f"未知指数代码：{code}（可选 {sorted(config.INDEX_CODES)}）")
    info = config.INDEX_CODES[code]
    symbol = info["sina"]
    start = start or config.SYNC_START
    end = end or dt.date.today().isoformat()
    # 请求窗口向前垫 90 天：当用户只查极窄区间（甚至一整天都是节假日）时，
    # 各数据源仍能返回足以判定"该区间无交易日"的历史，而不是全部报错。
    req_start = max("1990-12-19",
                   (dt.date.fromisoformat(start) - dt.timedelta(days=90)).isoformat())
    got: dict[str, list[tuple]] = {}
    errors: list[str] = []
    succeeded: list[str] = []
    for source in SOURCE_ORDER:
        try:
            if source == "tencent":
                rows = _fetch_tencent(symbol, req_start, end)
            elif source == "sina":
                rows = _fetch_sina(symbol, req_start, end)
            else:
                rows = _fetch_eastmoney(info, req_start, end)
        except Exception as exc:  # noqa: BLE001 - 降级到下一个源
            errors.append(f"{source}: {exc!r}")
            continue
        if rows:
            succeeded.append(source)
        rows = [r for r in rows if start <= r[0] <= end]
        if rows:
            got[source] = rows
        if _coverage_ok(rows, start, end):
            break
    merged = _merge(got, start, end)
    # 数据来源 = 提供行数最多的高优先级源
    source = "none"
    if got:
        source = max(SOURCE_ORDER, key=lambda s: (len(got.get(s, [])), -SOURCE_ORDER.index(s)))
    # 只有「所有源都失败」才算错误；若某个源成功返回（哪怕区间内恰好没有交易日），
    # 空结果表示该区间确实无数据，不抛异常。
    if not merged and errors and not succeeded:
        raise http_util.FetchError("指数 %s 全部数据源失败 :: %s" % (code, " | ".join(errors)))
    return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in merged], source


def fetch_history(
    code: str,
    start: str | None = None,
    end: str | None = None,
) -> list[tuple]:
    """抓取指数日线，返回 ``[(date, open, high, low, close, volume, amount_or_None), ...]``。"""
    rows, _source = fetch_history_detail(code, start=start, end=end)
    return rows


def index_code_of(variety: str) -> str:
    return config.VARIETY_INFO[variety.strip().upper()]["index_code"]


if __name__ == "__main__":  # pragma: no cover - 手工探测入口
    import sys

    for _code in sys.argv[1:] or list(config.INDEX_CODES):
        _rows, _src = fetch_history_detail(_code)
        print(f"{_code} source={_src} rows={len(_rows)} "
              f"range={_rows[0][0] if _rows else None}..{_rows[-1][0] if _rows else None}")
