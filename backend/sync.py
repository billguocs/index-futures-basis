# -*- coding: utf-8 -*-
"""数据同步编排：指数日线 + 中金所日行情/持仓排名落库，并重建聚合表。

设计要点
--------
* **幂等**：以 ``sync_log`` 的日期级状态为准——``ok`` 的日期直接跳过；``empty``
  （确认非交易日 / 中金所文件缺失）只在"今天之前"的日期跳过，今天的 ``empty``
  允许下次重试；所有写入都是 UPSERT，随时可安全重跑。
* **容错**：单日 / 单品种失败只记录进 ``failed``，绝不中断整体；交易日历不硬编码，
  由指数日线日期与中金所已落库日期求并集，缺口用工作日补齐。
* **并发**：网络请求在线程池跑（``config.REQUEST_THREADS``），SQLite 写入集中在
  主线程，避免 WAL 写锁竞争；抓取按"整日原子"处理，便于断点续抓。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Sequence

if __package__ in (None, ""):  # 支持 `python backend\sync.py` 直接运行
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from backend import db, http_util
from backend.sources import cffex, index_data

# ---------------------------------------------------------------- 列定义
INDEX_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount", "source"]
FUT_COLUMNS = [
    "date", "variety", "contract", "open", "high", "low", "close", "settle",
    "prev_settle", "volume", "amount", "open_interest", "oi_change", "is_main",
]
RANK_COLUMNS = [
    "date", "variety", "contract", "rank_type", "rank", "member", "member_raw",
    "position", "change",
]

OK_STATUSES = {"ok", "empty"}   # 可跳过（empty 仅限历史日期）

ProgressFn = Callable[[int, int, str], None] | None


# ---------------------------------------------------------------- 小工具
def _emit(progress: ProgressFn, done: int, total: int, label: str) -> None:
    if progress is None:
        return
    try:
        progress(int(done), int(total), str(label))
    except Exception:  # noqa: BLE001 - 进度回调异常不能影响同步
        pass


def _norm_varieties(varieties: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for v in varieties or config.VARIETIES:
        v = str(v).strip().upper()
        if v and v not in out:
            out.append(v)
    unknown = [v for v in out if v not in config.VARIETIES]
    if unknown:
        raise ValueError(f"未知品种：{unknown}（可选 {config.VARIETIES}）")
    return out


def _default_end(today: str | None = None) -> str:
    """默认结束日：今天（周末回退到上周五）。"""
    d = dt.date.fromisoformat(today) if today else dt.date.today()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def _weekdays(start: str, end: str) -> list[str]:
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    out: list[str] = []
    while a <= b:
        if a.weekday() < 5:
            out.append(a.isoformat())
        a += dt.timedelta(days=1)
    return out


def _load_log(conn) -> dict[str, str]:
    return {r[0]: (r[1] or "") for r in conn.execute("SELECT date, status FROM sync_log")}


def _skip_date(date: str, log: dict[str, str], today: str) -> bool:
    status = log.get(date)
    if status is None:
        return False
    if status == "ok":
        return True
    if status == "empty":
        return date < today      # 今天的数据可能还没发布，留着重试
    return False                  # partial / failed 需要重抓


def _trade_calendar(conn, start: str, end: str) -> list[str]:
    """交易日历 = 指数日线日期 ∪ 中金所已落库日期，缺口用工作日补齐。"""
    known: set[str] = set()
    for table in ("index_daily", "fut_daily", "member_rank"):
        try:
            known |= db.existing_dates(conn, table)
        except Exception:  # noqa: BLE001 - 表可能尚未建立
            continue
    window = sorted(d for d in known if start <= d <= end)
    if not window:
        return _weekdays(start, end)
    filled = set(window)
    first, last = window[0], window[-1]
    if first > start:
        filled |= set(_weekdays(start, first))
    if last < end:
        filled |= set(_weekdays(last, end))
    return sorted(d for d in filled if start <= d <= end)


# ---------------------------------------------------------------- 指数
def sync_index(conn, start: str, end: str, progress: ProgressFn = None) -> dict:
    """抓取 4 个指数日线并与库中已有数据合并后 UPSERT。"""
    result = {"rows": 0, "by_code": {}, "failed": [], "last_date": None}
    codes = list(config.INDEX_CODES)
    for i, code in enumerate(codes, 1):
        _emit(progress, i - 1, len(codes), f"指数 {code}")
        try:
            rows, source = index_data.fetch_history_detail(code, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - 单指数失败不阻断
            result["failed"].append(
                {"date": end, "variety": "INDEX", "reason": f"error:{code}:{exc!r}"}
            )
            continue
        merged: dict[str, tuple] = {}
        for r in conn.execute("SELECT * FROM index_daily WHERE code=?", (code,)):
            if start <= r["date"] <= end:
                merged[r["date"]] = (
                    r["date"], r["open"], r["high"], r["low"], r["close"],
                    r["volume"], r["amount"], r["source"],
                )
        for row in rows:  # 抓到的数据优先覆盖旧值
            merged[row[0]] = (row[0], row[1], row[2], row[3], row[4], row[5], row[6], source)
        payload = [(code,) + merged[d] for d in sorted(merged)]
        db.upsert_many(conn, "index_daily", INDEX_COLUMNS, payload, ["code", "date"])
        conn.commit()
        result["by_code"][code] = len(payload)
        result["rows"] += len(payload)
    result["last_date"] = db.latest_date(conn, "index_daily")
    _emit(progress, len(codes), len(codes), "指数完成")
    return result


# ---------------------------------------------------------------- 单日抓取（线程池内执行，只联网不写库）
def _fetch_day(date: str, varieties: Sequence[str]) -> dict:
    out = {
        "date": date,
        "fut_rows": [],
        "rank_rows": [],
        "missing": [],
        "errors": [],
    }
    fut_missing = False
    try:
        out["fut_rows"] = cffex.fetch_daily_quotes(date)
    except http_util.Soft404 as exc:
        fut_missing = True
        out["missing"].append({"date": date, "variety": "ALL", "reason": f"missing:hqsj:{exc}"})
    except Exception as exc:  # noqa: BLE001
        out["errors"].append({"date": date, "variety": "ALL", "reason": f"error:hqsj:{exc!r}"})

    # hqsj 缺失时先只探一个品种：真的非交易日就能省下 3~4 个请求
    targets = list(varieties[:1]) if fut_missing else list(varieties)
    for variety in targets:
        try:
            rows = cffex.fetch_rank(date, variety)
        except http_util.Soft404 as exc:
            out["missing"].append({"date": date, "variety": variety, "reason": f"missing:ccpm:{exc}"})
            continue
        except Exception as exc:  # noqa: BLE001
            out["errors"].append({"date": date, "variety": variety, "reason": f"error:ccpm:{exc!r}"})
            continue
        out["rank_rows"].extend(rows)
    if fut_missing and out["rank_rows"]:
        # 极少数情况下 hqsj 缺文件但排名文件存在，补齐其余品种
        for variety in varieties[1:]:
            try:
                out["rank_rows"].extend(cffex.fetch_rank(date, variety))
            except http_util.Soft404:
                continue
            except Exception as exc:  # noqa: BLE001
                out["errors"].append({"date": date, "variety": variety, "reason": f"error:ccpm:{exc!r}"})
    elif fut_missing and not out["rank_rows"] and not out["errors"]:
        # 整个交易日的中金所文件都不存在（法定节假日）：合并成一条，避免缺失清单噪音
        out["missing"] = [{
            "date": date, "variety": "ALL",
            "reason": "missing:holiday:中金所当日无任何文件（非交易日）",
        }]
    return out


def _write_day(conn, result: dict) -> str:
    """把单日抓取结果写库并记 sync_log，返回状态字符串。"""
    date = result["date"]
    fut_rows = result.get("fut_rows") or []
    rank_rows = result.get("rank_rows") or []
    errors = result.get("errors") or []
    missing = result.get("missing") or []

    fut_payload = [
        (r["date"], r["variety"], r["contract"], r["open"], r["high"], r["low"],
         r["close"], r["settle"], r["prev_settle"], r["volume"], r["amount"],
         r["open_interest"], r["oi_change"], r.get("is_main", 0))
        for r in fut_rows
    ]
    rank_payload = [
        (r["date"], r["variety"], r["contract"], r["rank_type"], r["rank"],
         r["member"], r["member_raw"], r["position"], r["change"])
        for r in rank_rows
    ]
    if fut_payload:
        db.upsert_many(conn, "fut_daily", FUT_COLUMNS, fut_payload, ["date", "contract"])
    if rank_payload:
        db.upsert_many(conn, "member_rank", RANK_COLUMNS, rank_payload,
                       ["date", "contract", "rank_type", "rank"])

    has_data = bool(fut_payload or rank_payload)
    if errors:
        status = "partial" if has_data else "failed"
    elif has_data:
        status = "ok"
    else:
        status = "empty"
    note_bits = [f"fut={len(fut_payload)}", f"rank={len(rank_payload)}"]
    if missing:
        note_bits.append("missing=" + ",".join(sorted({m["variety"] for m in missing})))
    if errors:
        note_bits.append("errors=" + ",".join(sorted({e["variety"] for e in errors})))
    conn.execute(
        "INSERT INTO sync_log(date, fetched_at, status, note) VALUES (?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET fetched_at=excluded.fetched_at, "
        "status=excluded.status, note=excluded.note",
        (date, dt.datetime.now().isoformat(timespec="seconds"), status, " ".join(note_bits)),
    )
    conn.commit()
    return status


# ---------------------------------------------------------------- 聚合表
def rebuild_aggregates(conn=None) -> dict:
    """纯重算聚合表（``is_main`` / ``member_daily`` / ``variety_daily``），幂等。"""
    own = conn is None
    conn = conn or db.connect()
    try:
        conn.execute("DELETE FROM variety_daily")
        conn.execute("DELETE FROM member_daily")
        conn.execute("UPDATE fut_daily SET is_main = 0")
        conn.execute(
            """
            WITH best AS (
                SELECT date, variety, MAX(open_interest) AS moi
                FROM fut_daily
                WHERE open_interest IS NOT NULL
                GROUP BY date, variety
            ),
            pick AS (
                SELECT f.date AS date, MIN(f.contract) AS contract
                FROM fut_daily f
                JOIN best b
                  ON b.date = f.date AND b.variety = f.variety AND b.moi = f.open_interest
                GROUP BY f.date, f.variety
            )
            UPDATE fut_daily SET is_main = 1
            WHERE (date, contract) IN (SELECT date, contract FROM pick)
            """
        )
        conn.execute(
            """
            INSERT INTO member_daily (date, variety, member, long_pos, short_pos, volume, net_pos)
            SELECT date, variety, member,
                   COALESCE(SUM(CASE WHEN rank_type='long'  THEN position END), 0),
                   COALESCE(SUM(CASE WHEN rank_type='short' THEN position END), 0),
                   COALESCE(SUM(CASE WHEN rank_type='vol'   THEN position END), 0),
                   COALESCE(SUM(CASE WHEN rank_type='long'  THEN position END), 0)
                 - COALESCE(SUM(CASE WHEN rank_type='short' THEN position END), 0)
            FROM member_rank
            GROUP BY date, variety, member
            """
        )
        case_sql = "CASE m.variety " + " ".join(
            "WHEN '%s' THEN '%s'" % (v, config.VARIETY_INFO[v]["index_code"])
            for v in config.VARIETIES
        ) + " END"
        conn.execute(
            f"""
            WITH agg AS (
                SELECT date, variety, SUM(volume) AS volume_sum, SUM(open_interest) AS oi_sum
                FROM fut_daily
                GROUP BY date, variety
            ),
            top AS (
                SELECT date, variety,
                       COALESCE(SUM(CASE WHEN rank_type='long'  THEN position END), 0) AS tl,
                       COALESCE(SUM(CASE WHEN rank_type='short' THEN position END), 0) AS ts
                FROM member_rank
                WHERE rank BETWEEN 1 AND 20
                GROUP BY date, variety
            ),
            citic AS (
                SELECT date, variety,
                       SUM(CASE WHEN rank_type='long'  THEN position END) AS cl,
                       SUM(CASE WHEN rank_type='short' THEN position END) AS cs
                FROM member_rank
                WHERE member = ?
                GROUP BY date, variety
            )
            INSERT INTO variety_daily (
                date, variety, main_contract, main_close, volume, open_interest,
                top20_long, top20_short, top20_net,
                citic_long, citic_short, citic_net, basis, basis_pct
            )
            SELECT m.date, m.variety, m.contract, COALESCE(m.close, m.settle),
                   agg.volume_sum, agg.oi_sum,
                   COALESCE(top.tl, 0), COALESCE(top.ts, 0),
                   COALESCE(top.tl, 0) - COALESCE(top.ts, 0),
                   citic.cl, citic.cs,
                   CASE WHEN citic.cl IS NULL AND citic.cs IS NULL THEN NULL
                        ELSE COALESCE(citic.cl, 0) - COALESCE(citic.cs, 0) END,
                   (COALESCE(m.close, m.settle) - i.close),
                   CASE WHEN i.close IS NULL OR i.close = 0 THEN NULL
                        ELSE ROUND((COALESCE(m.close, m.settle) - i.close) / i.close * 100.0, 4) END
            FROM fut_daily m
            JOIN agg ON agg.date = m.date AND agg.variety = m.variety
            LEFT JOIN top   ON top.date = m.date AND top.variety = m.variety
            LEFT JOIN citic ON citic.date = m.date AND citic.variety = m.variety
            LEFT JOIN index_daily i ON i.date = m.date AND i.code = {case_sql}
            WHERE m.is_main = 1
            """,
            (config.DEFAULT_MEMBER,),
        )
        conn.commit()
        out = {
            "main_days": int(
                conn.execute("SELECT COUNT(*) FROM fut_daily WHERE is_main=1").fetchone()[0]
            ),
            "member_daily": db.table_count(conn, "member_daily"),
            "variety_daily": db.table_count(conn, "variety_daily"),
        }
        return out
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 主同步
def sync_all(
    start: str | None = None,
    end: str | None = None,
    varieties: Sequence[str] | None = None,
    progress: ProgressFn = None,
) -> dict:
    """全量/增量同步。

    参数
    ----
    start : 起始日（默认 ``config.SYNC_START``）
    end   : 结束日（默认最新交易日）
    varieties : 品种列表（默认 IF/IH/IC/IM）
    progress : ``progress(done, total, label)`` 回调，供 /api/sync/status 使用
    """
    t0 = time.time()
    start = cffex.norm_date(start) if start else config.SYNC_START
    varieties = _norm_varieties(varieties)
    today = dt.date.today().isoformat()
    requested_end = cffex.norm_date(end) if end else None
    desired_end = requested_end or _default_end(today)
    if start > desired_end:
        raise ValueError(f"起始日 {start} 晚于结束日 {desired_end}")

    conn = db.connect()
    try:
        _emit(progress, 0, 1, "同步指数日线")
        idx = sync_index(conn, start, desired_end)
        failed: list[dict] = list(idx["failed"])

        # 结束日：未显式指定时收敛到指数最新交易日（今天可能是节假日/未发布）
        index_last = idx.get("last_date")
        eff_end = desired_end
        if not requested_end and index_last and index_last < eff_end and index_last >= start:
            eff_end = index_last
        if eff_end < start:
            eff_end = desired_end

        calendar = _trade_calendar(conn, start, eff_end)
        log = _load_log(conn)
        pending = [d for d in calendar if not _skip_date(d, log, today)]
        skipped = len(calendar) - len(pending)

        total = len(pending) + 1  # +1 = 聚合重建
        fut_written = 0
        rank_written = 0
        rank_days = 0
        status_counts = {"ok": 0, "partial": 0, "empty": 0, "failed": 0}
        done = 0
        _emit(progress, 0, total, f"待抓取 {len(pending)} 个交易日（跳过 {skipped}）")

        if pending:
            with ThreadPoolExecutor(max_workers=max(1, int(config.REQUEST_THREADS))) as pool:
                futures = {pool.submit(_fetch_day, d, varieties): d for d in pending}
                for fut in as_completed(futures):
                    day = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001 - 兜底，单日不中断
                        result = {
                            "date": day, "fut_rows": [], "rank_rows": [],
                            "missing": [],
                            "errors": [{"date": day, "variety": "ALL",
                                        "reason": f"error:worker:{exc!r}"}],
                        }
                    status = _write_day(conn, result)
                    status_counts[status] = status_counts.get(status, 0) + 1
                    fut_written += len(result.get("fut_rows") or [])
                    rank_written += len(result.get("rank_rows") or [])
                    if result.get("rank_rows"):
                        rank_days += 1
                    failed.extend(result.get("errors") or [])
                    done += 1
                    _emit(progress, done, total, f"{day} {status}")

        _emit(progress, total - 1, total, "重建聚合表")
        agg = rebuild_aggregates(conn)
        now_iso = dt.datetime.now().isoformat(timespec="seconds")
        db.kv_set(conn, "last_sync_at", now_iso)
        db.kv_set(conn, "last_sync_range", f"{start}..{eff_end}")
        db.kv_set(conn, "aggregates_at", now_iso)
        db.kv_set(conn, "variety_daily_rows", str(agg["variety_daily"]))
        db.kv_set(conn, "member_daily_rows", str(agg["member_daily"]))
        db.kv_set(conn, "last_sync_days", str(len(calendar)))
        db.kv_set(conn, "last_sync_status", "ok" if not failed else f"partial:{len(failed)}")
        break_row = conn.execute(
            "SELECT MIN(date) FROM member_rank "
            "WHERE instr(member_raw,'(')>0 OR instr(member_raw,'（')>0"
        ).fetchone()
        if break_row and break_row[0]:
            db.kv_set(conn, "member_naming_break_date", break_row[0])
        conn.commit()
        _emit(progress, total, total, "同步完成")

        elapsed = round(time.time() - t0, 2)
        report = {
            "ok": not failed,
            "index_rows": idx["rows"],
            "fut_rows": fut_written,
            "rank_days": rank_days,
            "variety_rows": agg["variety_daily"],
            "failed": failed,
            "range": {"start": start, "end": eff_end},
            "elapsed": elapsed,
            "days_total": len(calendar),
            "days_skipped": skipped,
            "days_fetched": len(pending),
            "status_counts": status_counts,
            "member_rows": rank_written,
            "member_daily_rows": agg["member_daily"],
        }
        return report
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    import json
    import sys

    _start = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] not in ("-", "") else None
    _end = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] not in ("-", "") else None
    _vars = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else None

    def _print_progress(done_, total_, label_):
        print(f"[{done_}/{total_}] {label_}", flush=True)

    _out = sync_all(start=_start, end=_end, varieties=_vars, progress=_print_progress)
    print(json.dumps(_out, ensure_ascii=False, indent=2))
