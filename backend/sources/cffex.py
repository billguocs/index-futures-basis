# -*- coding: utf-8 -*-
"""中金所（CFFEX）数据源：成交持仓排名 CSV + 日行情 CSV。

已实测的坑位（契约第 2 节）
--------------------------
* 编码 GB2312/GBK，统一 ``decode("gbk", "ignore")``；
* 缺失文件返回 **HTTP 200 + 2132 字节错误页**，必须按长度/内容判定（见 ``http_util.is_soft_404``）；
* 合约列存在**右补空格**（``"IF2401                        "``），必须 ``strip()``；
* 会员名 2024-05 之后带 ``(代客)`` 后缀，存在全/半角括号两种写法，用 ``normalize_member`` 归一化；
* 早期文件含 ``非期货公司`` 的 0 值行与 9 列残行，按 ``rank`` 必须是 1..20 的整数过滤。
"""
from __future__ import annotations

import datetime as dt
import re

import config
from backend import http_util

REFERER = "http://www.cffex.com.cn/"

# 12 列中的三个榜单：(rank_type, 会员简称列, 持仓量列, 增减列)
RANK_SECTIONS = (("vol", 3, 4, 5), ("long", 6, 7, 8), ("short", 9, 10, 11))

_CONTRACT_RE = re.compile(r"^(IF|IH|IC|IM)(\d{4})$")
_DATE_RE = re.compile(r"^\d{8}$")
_RANK_RE = re.compile(r"^\d{1,2}$")


# ------------------------------------------------------------------ 工具
def norm_date(value: str) -> str:
    """``20261020`` / ``2026-10-20`` / ``2026/10/20`` -> ``2026-10-20``。"""
    s = str(value).strip()
    if not s:
        raise ValueError("空日期")
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    if re.match(r"^\d{4}/\d{2}/\d{2}$", s):
        return s.replace("/", "-")
    raise ValueError(f"无法识别的日期：{value!r}")


def _dash(date: str) -> str:
    """``2026-10-20`` -> ``20261020``。"""
    return norm_date(date).replace("-", "")


def _int(value, default=None):
    if value is None:
        return default
    s = str(value).strip().replace(",", "").replace("\u3000", "")
    if not s or s in {"--", "-", "—", "None", "nan"}:
        return default
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    if value is None:
        return default
    s = str(value).strip().replace(",", "").replace("\u3000", "")
    if not s or s in {"--", "-", "—", "None", "nan"}:
        return default
    try:
        v = float(s)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def normalize_member(name: str) -> str:
    """去掉会员名尾部的括号后缀（``中信期货(代客)`` -> ``中信期货``）。

    兼容全角 ``（）`` 与半角 ``()``；只处理**尾部**括号，不会误删名称中间括号。
    """
    if not name:
        return ""
    s = str(name).strip().replace("\u3000", " ")
    s = re.sub(config.MEMBER_SUFFIX_PATTERN, "", s).strip()
    return s


def member_raw_has_suffix(name: str) -> bool:
    """原始会员名是否带括号后缀（用于判定披露口径切换日）。"""
    if not name:
        return False
    raw = str(name).strip().replace("\u3000", " ")
    return normalize_member(raw) != raw


# ------------------------------------------------------------------ URL
def url_rank(date: str, variety: str) -> str:
    d = _dash(date)
    return config.CFFEX_CCPM_URL.format(yyyymm=d[:6], dd=d[6:8], variety=variety.strip().upper())


def url_daily(date: str) -> str:
    d = _dash(date)
    return config.CFFEX_DAILY_CSV_URL.format(yyyymm=d[:6], dd=d[6:8], yyyymmdd=d)


# ------------------------------------------------------------------ 解析
def parse_rank_csv(text: str, variety: str | None = None, date: str | None = None) -> list[dict]:
    """解析成交持仓排名 CSV，返回 ``member_rank`` 表行（dict）。

    每个数据行拆成 3 条记录（vol / long / short）；表头行、``非期货公司`` 残行、
    名次非 1..20 的行全部跳过。
    """
    rows: list[dict] = []
    if not text:
        return rows
    wanted = variety.strip().upper() if variety else None
    for raw_line in text.splitlines():
        line = raw_line.strip("\ufeff\r\n\t ")
        if not line or line.startswith("交易日") or line.startswith(",,,"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 12:
            continue  # 9 列残行（非期货公司等）
        day_field = fields[0]
        if not _DATE_RE.match(day_field):
            continue
        if not _RANK_RE.match(fields[2]):
            continue
        rank = int(fields[2])
        if not 1 <= rank <= 20:
            continue
        contract = fields[1].strip()
        if not contract:
            continue
        this_variety = contract[:2].upper()
        if this_variety not in config.VARIETIES:
            continue
        if wanted and this_variety != wanted:
            continue
        trade_date = f"{day_field[0:4]}-{day_field[4:6]}-{day_field[6:8]}"
        for rank_type, name_idx, pos_idx, chg_idx in RANK_SECTIONS:
            member_raw = fields[name_idx]
            if not member_raw:
                continue
            if member_raw.startswith("非期货公司") or member_raw.startswith("非期货"):
                continue  # 早期文件的占位 0 值行，不是真实会员
            position = _int(fields[pos_idx], default=None)
            if position is None:
                continue
            rows.append(
                {
                    "date": trade_date,
                    "variety": this_variety,
                    "contract": contract,
                    "rank_type": rank_type,
                    "rank": rank,
                    "member": normalize_member(member_raw),
                    "member_raw": member_raw,
                    "position": position,
                    "change": _int(fields[chg_idx], default=None),
                }
            )
    return rows


def parse_daily_csv(text: str, date: str | None = None) -> list[dict]:
    """解析中金所日行情 CSV，只保留 IF/IH/IC/IM 股指期货合约行。

    列：``合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,
    今收盘,今结算,前结算,涨跌1,涨跌2,Delta``。
    """
    rows: list[dict] = []
    if not text:
        return rows
    trade_date = norm_date(date) if date else None
    for raw_line in text.splitlines():
        line = raw_line.strip("\ufeff\r\n\t ")
        if not line:
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 11:
            continue
        code = fields[0].strip()
        m = _CONTRACT_RE.match(code)
        if not m:
            continue
        variety = m.group(1)
        rows.append(
            {
                "date": trade_date,
                "variety": variety,
                "contract": code,
                "open": _float(fields[1]),
                "high": _float(fields[2]),
                "low": _float(fields[3]),
                "volume": _int(fields[4]),
                "amount": _float(fields[5]),
                "open_interest": _int(fields[6]),
                "oi_change": _int(fields[7]),
                "close": _float(fields[8]),
                "settle": _float(fields[9]),
                "prev_settle": _float(fields[10]),
                "is_main": 0,
            }
        )
    return rows


# ------------------------------------------------------------------ 抓取
def fetch_rank(date: str, variety: str) -> list[dict]:
    """抓取单品种单日持仓排名；文件不存在抛 :class:`http_util.Soft404`。"""
    trade_date = norm_date(date)
    url = url_rank(trade_date, variety)
    body = http_util.fetch_bytes(url, referer=REFERER)
    text = http_util.decode_text(body, "gbk")
    if http_util.is_soft_404(body, text):
        raise http_util.Soft404(f"ccpm 缺失：{trade_date} {variety}（HTTP 200 错误页）")
    rows = parse_rank_csv(text, variety=variety, date=trade_date)
    if not rows:
        raise http_util.Soft404(f"ccpm 无有效数据行：{trade_date} {variety}")
    return rows


def fetch_rank_day(date: str, varieties: list[str] | None = None) -> dict[str, list[dict]]:
    """抓取某交易日全部品种持仓排名，返回 ``{variety: rows}``。

    缺失品种（如 IM 上市前）静默跳过；网络类错误直接抛出，由调用方记为失败。
    """
    trade_date = norm_date(date)
    result: dict[str, list[dict]] = {}
    for variety in (varieties or config.VARIETIES):
        variety = variety.strip().upper()
        try:
            result[variety] = fetch_rank(trade_date, variety)
        except http_util.Soft404:
            continue
    return result


def fetch_daily_quotes(date: str) -> list[dict]:
    """抓取某交易日全品种日行情（一次请求覆盖 IF/IH/IC/IM）。"""
    trade_date = norm_date(date)
    url = url_daily(trade_date)
    body = http_util.fetch_bytes(url, referer=REFERER)
    text = http_util.decode_text(body, "gbk")
    if http_util.is_soft_404(body, text):
        raise http_util.Soft404(f"hqsj 缺失：{trade_date}（HTTP 200 错误页）")
    rows = parse_daily_csv(text, date=trade_date)
    if not rows:
        raise http_util.Soft404(f"hqsj 无股指期货行：{trade_date}")
    return rows


def is_trading_day_file(date: str) -> bool:
    """探测某天是否中金所交易日（有 hqsj 文件即可）。"""
    try:
        fetch_daily_quotes(date)
        return True
    except http_util.Soft404:
        return False


if __name__ == "__main__":  # pragma: no cover - 手工探测入口
    import json
    import sys

    d = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    out = {
        "daily_rows": len(fetch_daily_quotes(d)),
        "rank_days": {v: len(rows) for v, rows in fetch_rank_day(d).items()},
        "normalize": {k: normalize_member(k) for k in ("中信期货(代客)", "中信期货（自营）", "海通期货")},
        "break_date_raw": member_raw_has_suffix("中信期货(代客)"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
