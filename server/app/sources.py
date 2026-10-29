"""数据源清单：每个源 = 一个 SQLite 文件 + 绑定的语义模型。

平台里有两类源：
    jsfj  → data/warehouse.sqlite  真实：江苏统计年鉴（fact_stat）
    demo  → data/demo.sqlite       演示：合成电商（fact_order / dim_user / dim_product）

这个模块只读地盘点每个源：文件是否存在、大小、更新时间、有哪些表、各表行数/字段数，
供「数据源」页面展示，也供健康检查用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


def _rel_path(path: Path) -> str:
    try:
        return path.relative_to(config.ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _table_infos(path: Path) -> list[dict]:
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_%' ESCAPE '\\'"
        ).fetchall()]
        # 事实表在前、维表在后，便于阅读
        names.sort(key=lambda n: (0 if n.startswith("fact") else 1, n))
        out = []
        for name in names:
            try:
                rows = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                cols = len(conn.execute(f'PRAGMA table_info("{name}")').fetchall())
            except sqlite3.Error:
                rows, cols = None, None
            out.append({"name": name, "rows": rows, "columns": cols})
        return out
    finally:
        conn.close()


def list_sources() -> dict:
    items = []
    for key, ds in config.DATA_SOURCES.items():
        path: Path = ds["path"]
        exists = path.exists()
        stat = path.stat() if exists else None
        items.append({
            "key": key,
            "title": ds["title"],
            "nature": ds["nature"],
            "description": ds["description"],
            "model": ds["model"],
            "path": _rel_path(path),
            "exists": exists,
            "size_kb": round(stat.st_size / 1024, 1) if stat else 0,
            "updated_at": (datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                           .astimezone().isoformat(timespec="seconds") if stat else None),
            "setup": ds.get("setup", ""),
            "tables": _table_infos(path) if exists else [],
        })
    return {"items": items}
