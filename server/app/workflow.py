"""审批发布工作流。

所有 AI 产出（看板 / 建模 / 指标）都走同一条流水线：

    draft ──submit──▶ submitted ──approve──▶ approved ──publish──▶ published
      ▲                   │                                        │
      └──────rejected─────┘◀───────────────────────────────────────┘
                                     （编辑后回到 draft，重新审批）

规则：
  - 提交前自动跑 lint，有 error 直接拒绝
  - 只有 published 才对最终用户可见
  - 每次状态变化都留时间线，可审计
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable

from . import config, standards

STATE_MACHINE: dict[str, dict[str, str]] = {
    "draft": {"submit": "submitted"},
    "submitted": {"approve": "approved", "reject": "rejected"},
    "rejected": {"submit": "submitted"},
    "approved": {"publish": "published", "reject": "rejected"},
    "published": {"archive": "archived"},
    "archived": {},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    kind TEXT,
    ref TEXT,
    title TEXT,
    status TEXT,
    version INTEGER,
    snapshot TEXT,
    lint TEXT,
    comment TEXT,
    created_by TEXT,
    reviewed_by TEXT,
    created_at TEXT,
    updated_at TEXT,
    timeline TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifact_ref ON artifacts(kind, ref);
CREATE INDEX IF NOT EXISTS idx_artifact_status ON artifacts(status);
"""

_apply_fn: Callable[[str, str, dict], None] | None = None


class WorkflowError(Exception):
    pass


class LintBlocked(WorkflowError):
    def __init__(self, report: dict):
        self.report = report
        msg = "；".join(f"{e['rule']}: {e['message']}" for e in report["errors"][:5])
        super().__init__(f"规范校验未通过（{len(report['errors'])} 项错误）：{msg}")


def set_apply(fn: Callable[[str, str, dict], None]) -> None:
    global _apply_fn
    _apply_fn = fn


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    config.META.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.META, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def _row_to_dict(row) -> dict:
    keys = ["id", "kind", "ref", "title", "status", "version", "snapshot", "lint",
            "comment", "created_by", "reviewed_by", "created_at", "updated_at", "timeline"]
    d = dict(zip(keys, row))
    for k in ("snapshot", "lint", "timeline"):
        d[k] = json.loads(d[k]) if d[k] else ([] if k == "timeline" else {})
    return d


def get(aid: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    return _row_to_dict(row) if row else None


def find(kind: str, ref: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE kind=? AND ref=? ORDER BY updated_at DESC LIMIT 1",
            (kind, ref),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_artifacts(status: str | None = None, kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM artifacts"
    conds, params = [], []
    if status:
        conds.append("status=?")
        params.append(status)
    if kind:
        conds.append("kind=?")
        params.append(kind)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY updated_at DESC LIMIT 200"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 状态迁移
# ---------------------------------------------------------------------------
def _assert_transition(status: str, action: str) -> str:
    nxt = (STATE_MACHINE.get(status) or {}).get(action)
    if not nxt:
        raise WorkflowError(f"当前状态「{status}」不允许执行「{action}」")
    return nxt


def submit(kind: str, ref: str, title: str, snapshot: dict, by: str = "") -> dict:
    policy = config.APPROVAL_POLICY.get(kind, "platform")
    if policy != "platform":
        raise WorkflowError(
            f"{kind} 类产出按约定走 Git MR（有 diff、有评审、有历史），不在平台内提交审批"
        )
    report = standards.lint(kind, snapshot)
    if not report["passed"]:
        raise LintBlocked(report)

    now = _now()
    existing = find(kind, ref)
    if existing and existing["status"] == "submitted":
        raise WorkflowError("该产出已在审批中，请等待审批结果")

    event = {"at": now, "action": "submit", "by": by, "comment": ""}
    if existing:
        version = existing["version"] + 1
        timeline = existing["timeline"] + [event]
        with _conn() as conn:
            conn.execute(
                "UPDATE artifacts SET title=?, status='submitted', version=?, snapshot=?, "
                "lint=?, comment='', created_by=?, reviewed_by=NULL, updated_at=?, timeline=? "
                "WHERE id=?",
                (title, version, json.dumps(snapshot, ensure_ascii=False),
                 json.dumps(report, ensure_ascii=False), by, now,
                 json.dumps(timeline, ensure_ascii=False), existing["id"]),
            )
        return get(existing["id"])  # type: ignore[return-value]

    aid = "art_" + uuid.uuid4().hex[:10]
    artifact = {
        "id": aid, "kind": kind, "ref": ref, "title": title,
        "status": "submitted", "version": 1,
        "snapshot": json.dumps(snapshot, ensure_ascii=False),
        "lint": json.dumps(report, ensure_ascii=False),
        "comment": "", "created_by": by, "reviewed_by": None,
        "created_at": now, "updated_at": now,
        "timeline": json.dumps([event], ensure_ascii=False),
    }
    with _conn() as conn:
        conn.execute(
            "INSERT INTO artifacts VALUES (:id,:kind,:ref,:title,:status,:version,"
            ":snapshot,:lint,:comment,:created_by,:reviewed_by,:created_at,:updated_at,:timeline)",
            artifact,
        )
    return get(aid)  # type: ignore[return-value]


def _transition(aid: str, action: str, by: str, comment: str = "") -> dict:
    art = get(aid)
    if not art:
        raise WorkflowError(f"产出不存在: {aid}")
    nxt = _assert_transition(art["status"], action)
    now = _now()
    timeline = art["timeline"] + [{"at": now, "action": action, "by": by, "comment": comment}]
    reviewed = by if action in ("approve", "reject") else art.get("reviewed_by")
    with _conn() as conn:
        conn.execute(
            "UPDATE artifacts SET status=?, comment=?, reviewed_by=?, updated_at=?, timeline=? WHERE id=?",
            (nxt, comment or art.get("comment", ""), reviewed, now,
             json.dumps(timeline, ensure_ascii=False), aid),
        )
    updated = get(aid)  # type: ignore[assignment]

    if action == "publish" and _apply_fn:
        _apply_fn(updated["kind"], updated["ref"], updated["snapshot"])
    return updated


def approve(aid: str, by: str = "", comment: str = "") -> dict:
    return _transition(aid, "approve", by, comment)


def reject(aid: str, by: str = "", comment: str = "") -> dict:
    return _transition(aid, "reject", by, comment)


def publish(aid: str, by: str = "") -> dict:
    return _transition(aid, "publish", by)


def archive(aid: str, by: str = "") -> dict:
    return _transition(aid, "archive", by)


def mark_dirty(kind: str, ref: str, by: str = "") -> dict | None:
    """产出被编辑：已发布/已批准的退回 draft，需要重新审批。"""
    art = find(kind, ref)
    if not art:
        return None
    if art["status"] in ("draft", "submitted", "rejected"):
        return art
    now = _now()
    timeline = art["timeline"] + [{"at": now, "action": "edit", "by": by,
                                   "comment": "产出被修改，退回草稿"}]
    with _conn() as conn:
        conn.execute(
            "UPDATE artifacts SET status='draft', reviewed_by=NULL, updated_at=?, timeline=? WHERE id=?",
            (now, json.dumps(timeline, ensure_ascii=False), art["id"]),
        )
    return get(art["id"])
