"""元数据存储：查询记录、看板、分享。SQLite 单文件，零运维。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    id TEXT PRIMARY KEY,
    question TEXT,
    spec TEXT,
    sql TEXT,
    result TEXT,
    explain TEXT,
    viz TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS dashboards (
    id TEXT PRIMARY KEY,
    title TEXT,
    doc TEXT,
    status TEXT,
    version INTEGER,
    share_token TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_dash_share ON dashboards(share_token);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    config.META.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.META, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------- queries
def save_query(question: str, spec: dict, sql: str, result: dict,
               explain: dict, viz: dict) -> str:
    qid = "q_" + uuid.uuid4().hex[:10]
    with _conn() as conn:
        conn.execute(
            "INSERT INTO queries VALUES (?,?,?,?,?,?,?,?)",
            (qid, question, json.dumps(spec, ensure_ascii=False), sql,
             json.dumps(result, ensure_ascii=False), json.dumps(explain, ensure_ascii=False),
             json.dumps(viz, ensure_ascii=False), _now()),
        )
    return qid


def get_query(qid: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM queries WHERE id=?", (qid,)).fetchone()
    if not row:
        return None
    keys = ["id", "question", "spec", "sql", "result", "explain", "viz", "created_at"]
    d = dict(zip(keys, row))
    for k in ("spec", "result", "explain", "viz"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


# ---------------------------------------------------------------- dashboards
def create_dashboard(title: str, cards: list | None = None,
                     model: str | None = None, description: str = "",
                     description_meta: dict | None = None) -> dict:
    did = "dash_" + uuid.uuid4().hex[:10]
    doc = {
        "id": did,
        "title": title,
        "description": description,
        "description_meta": description_meta or {},
        "status": "draft",
        "version": 1,
        "model": model,
        "grid": {"cols": 12, "rowHeight": 72},
        "filters": [],
        "cards": cards or [],
    }
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO dashboards VALUES (?,?,?,?,?,?,?,?)",
            (did, title, json.dumps(doc, ensure_ascii=False), "draft", 1, None, now, now),
        )
    return doc


def list_dashboards() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id,title,doc,status,version,created_at,updated_at"
            " FROM dashboards ORDER BY updated_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        doc = json.loads(r[2]) if r[2] else {}
        out.append({
            "id": r[0], "title": r[1],
            "description": doc.get("description") or "",
            "status": r[3], "version": r[4],
            "created_at": r[5], "updated_at": r[6],
        })
    return out


def get_dashboard(did: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT id,title,doc,status,version,share_token FROM dashboards WHERE id=?",
                           (did,)).fetchone()
    if not row:
        return None
    doc = json.loads(row[2])
    doc["share_token"] = row[5]
    return doc


def save_dashboard(doc: dict, bump_version: bool = True) -> dict:
    did = doc["id"]
    with _conn() as conn:
        row = conn.execute("SELECT version, status FROM dashboards WHERE id=?", (did,)).fetchone()
        if not row:
            raise KeyError(f"看板不存在: {did}")
        version = (row[0] + 1) if bump_version else row[0]
        doc["version"] = version
        conn.execute(
            "UPDATE dashboards SET title=?, doc=?, status=?, version=?, updated_at=? WHERE id=?",
            (doc.get("title", ""), json.dumps(doc, ensure_ascii=False),
             doc.get("status", "draft"), version, _now(), did),
        )
    return doc


def add_card(did: str, card: dict) -> dict:
    doc = get_dashboard(did)
    if not doc:
        raise KeyError(f"看板不存在: {did}")
    doc.setdefault("cards", []).append(card)
    doc["status"] = "draft"          # 内容变更 → 需要重新审批
    return save_dashboard(doc)


def set_dashboard_status(did: str, status: str) -> dict:
    doc = get_dashboard(did)
    if not doc:
        raise KeyError(f"看板不存在: {did}")
    doc["status"] = status
    return save_dashboard(doc, bump_version=False)


def set_share(did: str) -> str:
    """只有已发布（published）的看板才能生成分享链接。"""
    with _conn() as conn:
        row = conn.execute("SELECT status FROM dashboards WHERE id=?", (did,)).fetchone()
        if not row:
            raise KeyError(f"看板不存在: {did}")
        if row[0] != "published":
            raise PermissionError(f"看板状态为 {row[0]}，只有已发布的看板才能分享")
        token = uuid.uuid4().hex[:16]
        conn.execute("UPDATE dashboards SET share_token=?, updated_at=? WHERE id=?",
                     (token, _now(), did))
    return token


def get_by_share(token: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT doc FROM dashboards WHERE share_token=?", (token,)).fetchone()
    return json.loads(row[0]) if row else None


def pin_query(qid: str, dashboard_id: str | None, title: str,
              viz: dict, model: str | None = None) -> dict:
    q = get_query(qid)
    if not q:
        raise KeyError(f"查询不存在: {qid}")

    if dashboard_id:
        doc = get_dashboard(dashboard_id)
        if not doc:
            raise KeyError(f"看板不存在: {dashboard_id}")
    else:
        doc = create_dashboard(title="我的看板", model=model)
        dashboard_id = doc["id"]

    cards = doc.get("cards", [])
    # 简单的自动布局：一行两列
    idx = len(cards)
    card = {
        "id": "c_" + uuid.uuid4().hex[:8],
        "type": "chart",
        "title": title,
        "query": qid,
        "spec": q["spec"],
        "visualization": viz,
        "layout": {"x": (idx % 2) * 6, "y": (idx // 2) * 4, "w": 6, "h": 4},
    }
    doc = add_card(dashboard_id, card)
    return {"dashboard": doc, "card": card}
