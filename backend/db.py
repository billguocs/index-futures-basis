# -*- coding: utf-8 -*-
"""SQLite 存储层：表结构、连接管理与通用读写工具。

设计原则
--------
* 只依赖标准库 ``sqlite3``（可选使用 pandas 做便捷读取）。
* 表结构覆盖"原始数据 -> 聚合特征 -> 派生指标"三层，派生指标不落库、实时计算。
* 所有写入走 ``upsert_many``，天然支持增量同步与断点续抓。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Sequence

import config

SCHEMA = """
-- 指数日线（沪深300 / 上证50 / 中证500 / 中证1000）
CREATE TABLE IF NOT EXISTS index_daily (
    code     TEXT NOT NULL,           -- 000300
    date     TEXT NOT NULL,           -- YYYY-MM-DD
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    amount   REAL,
    source   TEXT,
    PRIMARY KEY (code, date)
);

-- 中金所股指期货日行情（逐合约）
CREATE TABLE IF NOT EXISTS fut_daily (
    date          TEXT NOT NULL,
    variety       TEXT NOT NULL,      -- IF/IH/IC/IM
    contract      TEXT NOT NULL,      -- IF2611
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    settle        REAL,
    prev_settle   REAL,
    volume        INTEGER,
    amount        REAL,
    open_interest INTEGER,
    oi_change     INTEGER,
    is_main       INTEGER DEFAULT 0,   -- 当日该品种持仓量最大的合约
    PRIMARY KEY (date, contract)
);
CREATE INDEX IF NOT EXISTS idx_fut_daily_variety ON fut_daily(variety, date);

-- 中金所成交持仓排名（逐合约、逐榜单、逐名次）
CREATE TABLE IF NOT EXISTS member_rank (
    date      TEXT NOT NULL,
    variety   TEXT NOT NULL,
    contract  TEXT NOT NULL,
    rank_type TEXT NOT NULL,          -- vol | long | short
    rank      INTEGER NOT NULL,       -- 1..20
    member    TEXT NOT NULL,          -- 归一化会员名（去掉 (代客)/(自营) 后缀）
    member_raw TEXT NOT NULL,         -- 原始会员名
    position  INTEGER NOT NULL,       -- 成交量 / 持买单量 / 持卖单量
    change    INTEGER,                -- 比上一交易日增减
    PRIMARY KEY (date, contract, rank_type, rank)
);
CREATE INDEX IF NOT EXISTS idx_member_rank_member ON member_rank(member, variety, date);
CREATE INDEX IF NOT EXISTS idx_member_rank_date ON member_rank(date, variety);

-- 逐会员逐品种日度汇总（由 member_rank 聚合而来）
CREATE TABLE IF NOT EXISTS member_daily (
    date       TEXT NOT NULL,
    variety    TEXT NOT NULL,
    member     TEXT NOT NULL,
    long_pos   INTEGER,               -- 各合约持买单量之和
    short_pos  INTEGER,               -- 各合约持卖单量之和
    volume     INTEGER,               -- 各合约成交量之和（同一会员去重后累加）
    net_pos    INTEGER,               -- long_pos - short_pos
    PRIMARY KEY (date, variety, member)
);
CREATE INDEX IF NOT EXISTS idx_member_daily_member ON member_daily(member, variety, date);

-- 逐品种日度汇总（含主力合约、全市场持仓、前20合计）
CREATE TABLE IF NOT EXISTS variety_daily (
    date          TEXT NOT NULL,
    variety       TEXT NOT NULL,
    main_contract TEXT,
    main_close    REAL,
    volume        INTEGER,
    open_interest INTEGER,            -- 该品种全部合约持仓量之和
    top20_long    INTEGER,            -- 前20名持买单量合计
    top20_short   INTEGER,            -- 前20名持卖单量合计
    top20_net     INTEGER,
    citic_long    INTEGER,
    citic_short   INTEGER,
    citic_net     INTEGER,
    basis         REAL,               -- 主力合约收盘 - 现货指数收盘
    basis_pct     REAL,
    PRIMARY KEY (date, variety)
);

-- 同步日志
CREATE TABLE IF NOT EXISTS sync_log (
    date       TEXT PRIMARY KEY,      -- 交易日
    fetched_at TEXT,
    status     TEXT,                  -- ok / partial / failed
    note       TEXT
);

-- 键值元数据
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """打开（必要时创建）数据库，并确保表结构存在。"""
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(path: str | Path | None = None):
    """with 语法的连接管理，异常回滚，退出提交。"""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_many(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    pk_cols: Sequence[str],
) -> int:
    """批量 UPSERT：按主键冲突则更新非主键列。返回处理行数。"""
    rows = list(rows)
    if not rows:
        return 0
    cols = ",".join(columns)
    qs = ",".join("?" * len(columns))
    update_cols = [c for c in columns if c not in pk_cols]
    if update_cols:
        sets = ",".join(f"{c}=excluded.{c}" for c in update_cols)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({qs}) ON CONFLICT({','.join(pk_cols)}) DO UPDATE SET {sets}"
    else:
        sql = f"INSERT INTO {table} ({cols}) VALUES ({qs}) ON CONFLICT({','.join(pk_cols)}) DO NOTHING"
    conn.executemany(sql, rows)
    return len(rows)


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def latest_date(conn: sqlite3.Connection, table: str, variety: str | None = None) -> str | None:
    if variety:
        row = conn.execute(f"SELECT MAX(date) FROM {table} WHERE variety=?", (variety,)).fetchone()
    else:
        row = conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()
    return row[0] if row and row[0] else None


def min_date(conn: sqlite3.Connection, table: str, variety: str | None = None) -> str | None:
    if variety:
        row = conn.execute(f"SELECT MIN(date) FROM {table} WHERE variety=?", (variety,)).fetchone()
    else:
        row = conn.execute(f"SELECT MIN(date) FROM {table}").fetchone()
    return row[0] if row and row[0] else None


def existing_dates(conn: sqlite3.Connection, table: str, variety: str | None = None) -> set[str]:
    if variety:
        cur = conn.execute(f"SELECT DISTINCT date FROM {table} WHERE variety=?", (variety,))
    else:
        cur = conn.execute(f"SELECT DISTINCT date FROM {table}")
    return {r[0] for r in cur}


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def data_stats(conn: sqlite3.Connection) -> dict:
    """给 /api/meta 用的数据概览。"""
    def rng(table: str):
        row = conn.execute(f"SELECT MIN(date), MAX(date), COUNT(*) FROM {table}").fetchone()
        return {"start": row[0], "end": row[1], "rows": row[2]}

    stats = {
        "index_daily": rng("index_daily"),
        "fut_daily": rng("fut_daily"),
        "member_rank": rng("member_rank"),
        "member_daily": rng("member_daily"),
        "variety_daily": rng("variety_daily"),
    }
    stats["by_variety"] = {}
    for v in config.VARIETIES:
        row = conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(*) FROM variety_daily WHERE variety=?", (v,)
        ).fetchone()
        stats["by_variety"][v] = {"start": row[0], "end": row[1], "rows": row[2]}
    stats["last_sync"] = kv_get(conn, "last_sync_at")
    return stats
