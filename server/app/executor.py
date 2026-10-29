"""查询执行：连接分析库、执行 SQL、结果缓存。"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import config


class ExecError(Exception):
    pass


_cache: dict[str, tuple[float, Any]] = {}


def _source_meta(source: str | None) -> tuple[str, dict]:
    key = source or config.DEFAULT_SOURCE
    ds = config.DATA_SOURCES.get(key)
    if not ds:
        raise ExecError(f"未知数据源: {key}（可用: {', '.join(config.DATA_SOURCES)}）")
    return key, ds


def _connect(source: str | None = None) -> sqlite3.Connection:
    key, ds = _source_meta(source)
    path = ds["path"]
    if not path.exists():
        raise ExecError(f"数据源「{ds['title']}」不存在: {path}，请先运行 {ds.get('setup', '')}".rstrip())
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = None
    return conn


def execute(sql: str, params: list | None = None, use_cache: bool = True,
            source: str | None = None) -> dict:
    params = params or []
    key, _ = _source_meta(source)
    cache_key = key + "|" + sql + "|" + repr(params)

    if use_cache:
        hit = _cache.get(cache_key)
        if hit and time.time() - hit[0] < config.CACHE_TTL:
            data = dict(hit[1])
            data["cached"] = True
            return data

    t0 = time.time()
    try:
        conn = _connect(key)
        try:
            cur = conn.execute(sql, params)
            rows = [list(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise ExecError(f"SQL 执行失败: {e}\nSQL: {sql}") from e

    result = {"rows": rows, "latency_ms": int((time.time() - t0) * 1000), "cached": False}
    _cache[cache_key] = (time.time(), result)
    return result


def invalidate() -> None:
    _cache.clear()


def warehouse_stats(source: str | None = None) -> dict:
    key, ds = _source_meta(source)
    try:
        conn = _connect(key)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        return {"ok": True, "source": key, "tables": tables}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "source": key, "error": str(e), "tables": []}
