# -*- coding: utf-8 -*-
"""HTTP 抓取工具（仅标准库 ``urllib``）。

职责
----
* 统一 UA / Referer / 超时 / 重试退避；
* 中文站点 GBK / GB2312 与 UTF-8 自动解码；
* 中金所「缺失文件」soft-404 判定（HTTP 200 + 2132 字节「网页错误」页）；
* 线程级最小请求间隔（``config.REQUEST_SLEEP``）与受限并发（``config.REQUEST_THREADS``）。

约定
----
* 所有网络异常统一收敛为 :class:`FetchError`；
* 目标不存在（soft-404 / 404 / 空 body）抛 :class:`Soft404`，语义上等于「该日无数据」，
  调用方据此跳过而不是当成失败重试。
"""
from __future__ import annotations

import random
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence

import config

# ------------------------------------------------------------------ soft-404
# 实测：中金所缺失 CSV 返回 HTTP 200 + 2132 字节的 GBK「网页错误」页面。
SOFT_404_SIZE = 2132
SOFT_404_SIZE_TOL = 600
SOFT_404_MARKERS = (
    "网页错误",
    "网页不存在",
    "页面不存在",
    "404 Not Found",
    "Not Found",
    "<!DOCTYPE html",
    "<html",
)


class FetchError(Exception):
    """抓取失败：重试耗尽或响应不可用。"""


class Soft404(FetchError):
    """目标不存在：HTTP 200 但返回错误页 / 404 / 空响应。"""


# ------------------------------------------------------------------ 限速
_thread_state = threading.local()


def _throttle() -> None:
    """同一线程内两次请求之间至少间隔 ``config.REQUEST_SLEEP`` 秒。"""
    gap = float(getattr(config, "REQUEST_SLEEP", 0.0) or 0.0)
    if gap <= 0:
        return
    last = getattr(_thread_state, "last_request", 0.0)
    wait = last + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _thread_state.last_request = time.time()


# ------------------------------------------------------------------ 请求
def build_headers(referer: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造统一请求头；``Referer`` 对新浪/腾讯等接口是必需的。"""
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return headers


def fetch_bytes(
    url: str,
    referer: str | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """GET ``url`` 返回原始字节。

    HTTP 404/410 与空响应直接抛 :class:`Soft404`；网络错误按指数退避重试
    ``config.REQUEST_RETRIES`` 次后抛 :class:`FetchError`。
    """
    timeout = float(config.REQUEST_TIMEOUT if timeout is None else timeout)
    retries = int(config.REQUEST_RETRIES if retries is None else retries)
    req_headers = build_headers(referer, headers)
    last_err: BaseException | None = None
    for attempt in range(max(1, retries + 1)):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            if not body:
                raise FetchError("响应为空 (0 bytes)")
            return body
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise Soft404(f"HTTP {exc.code} {url}") from exc
            last_err = exc
        except Soft404:
            raise
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, FetchError) as exc:
            last_err = exc
        if attempt < retries:
            delay = 0.4 * (2 ** attempt) + random.uniform(0.0, 0.3)
            time.sleep(min(delay, 8.0))
    raise FetchError(f"抓取失败（{retries + 1} 次尝试）：{url} :: {last_err!r}")


def decode_text(body: bytes, encoding: str = "utf-8") -> str:
    """按 ``encoding`` 解码，失败依次回退 utf-8 / gbk，最后 ignore 兜底。"""
    if not body:
        return ""
    candidates: list[str] = [encoding]
    for enc in ("utf-8", "gb18030", "gbk"):
        if enc.lower() not in {c.lower() for c in candidates}:
            candidates.append(enc)
    for enc in candidates:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "ignore")


def fetch_text(
    url: str,
    referer: str | None = None,
    encoding: str = "utf-8",
    timeout: float | None = None,
    retries: int | None = None,
) -> str:
    """GET 并按 ``encoding`` 解码（中金所用 ``gbk``）。"""
    return decode_text(fetch_bytes(url, referer=referer, timeout=timeout, retries=retries), encoding)


# ------------------------------------------------------------------ soft-404 判定
def is_soft_404(body: bytes | None, text: str | None = None) -> bool:
    """判定响应是否为站点「缺失文件」错误页。

    双重判据（实测二者都命中）：长度接近 2132 字节；内容含「网页错误」等标记。
    真实 CSV 一般 2.7KB–70KB 且以表头/数字开头，不会命中。
    """
    if not body:
        return True
    probe = text
    if probe is None:
        probe = body[:4096].decode("gbk", "ignore")
    head = probe.lstrip("\ufeff\r\n\t ")[:4096]
    if any(marker in head for marker in SOFT_404_MARKERS):
        return True
    # 长度判据：2132 字节附近且不像 CSV（避免把偏小的真实 CSV 误判为错误页）
    return abs(len(body) - SOFT_404_SIZE) <= SOFT_404_SIZE_TOL and not looks_like_csv(probe)


def looks_like_csv(text: str) -> bool:
    """粗判文本是否为中金所 CSV（含中文表头或逗号分隔数字行）。"""
    if not text:
        return False
    head = text.lstrip("\ufeff\r\n\t ")
    if not head:
        return False
    if head.startswith("<"):
        return False
    first_line = head.splitlines()[0]
    return ("," in first_line) and ("网页" not in first_line)


# ------------------------------------------------------------------ 并发
def make_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """按 ``config.REQUEST_THREADS`` 建线程池（对中金所保持克制）。"""
    n = int(max_workers or getattr(config, "REQUEST_THREADS", 6) or 6)
    return ThreadPoolExecutor(max_workers=max(1, n), thread_name_prefix="fetch")


def parallel_map(
    fn: Callable[[Any], Any],
    items: Sequence[Any],
    max_workers: int | None = None,
) -> list[Any]:
    """受限并发执行 ``fn(item)``，按输入顺序返回结果；单项异常记为 ``None``。"""
    items = list(items)
    if not items:
        return []
    out: list[Any] = [None] * len(items)
    with make_executor(max_workers) as pool:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for fut, idx in futures.items():
            try:
                out[idx] = fut.result()
            except Exception:  # noqa: BLE001 - 单项失败不影响整体
                out[idx] = None
    return out


def invalidate_throttle() -> None:
    """清空当前线程的限速时间戳（测试用）。"""
    _thread_state.last_request = 0.0
