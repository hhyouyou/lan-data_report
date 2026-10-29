"""数据开发：用自然语言与 AI 对话式地构建 / 修改数据模型。

没有 LLM 时走规则引擎（基线）；接入 LLM 后，这里换成「LLM 产出操作 → 本地执行」。

设计原则：
  - AI 不直接改文件，只产出「模型操作结果」，必须过校验 + Lint
  - 每一步都能看到模型变化，可回退（会话里保留历史快照）
  - 生成的模型仍然是标准 semantic YAML，和手工写的没有区别
"""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone

import yaml

from . import datasets, standards
from .semantic import validate

_sessions: dict[str, dict] = {}

EXPR_OK = re.compile(r"^[0-9{}.+\-*/()a-zA-Z_]+$")


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _lint(doc: dict) -> dict:
    return standards.lint("semantic", doc)


def _yaml(doc: dict) -> str:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _public(session: dict) -> dict:
    doc = session.get("doc") or {}
    return {
        "id": session["id"],
        "table": session.get("table"),
        "messages": session["messages"],
        "doc": doc or None,
        "yaml": _yaml(doc) if doc else "",
        "lint": _lint(doc) if doc else None,
        "stats": {
            "metrics": len(doc.get("metrics") or []),
            "dimensions": len(doc.get("dimensions") or []),
        } if doc else {"metrics": 0, "dimensions": 0},
    }


def create_session(table: str | None = None) -> dict:
    sid = "dev_" + uuid.uuid4().hex[:10]
    session = {"id": sid, "table": table, "doc": None,
               "messages": [], "created_at": _now(), "history": []}
    if table:
        draft = datasets.suggest_model(table)
        session["doc"] = draft["doc"]
        session["table"] = table
        session["messages"].append({
            "role": "assistant",
            "text": _welcome(draft),
            "at": _now(),
        })
        session["history"].append(copy.deepcopy(draft["doc"]))
    _sessions[sid] = session
    return _public(session)


def get_session(sid: str) -> dict:
    if sid not in _sessions:
        raise KeyError(f"会话不存在: {sid}")
    return _sessions[sid]


def say(sid: str, text: str) -> dict:
    session = get_session(sid)
    text = (text or "").strip()
    if not text:
        return _public(session)

    session["messages"].append({"role": "user", "text": text, "at": _now()})

    # 还没有模型 → 先尝试从表创建
    if session.get("doc") is None:
        table = _find_table(text) or session.get("table")
        if not table:
            session["messages"].append({"role": "assistant", "text":
                "还不知道基于哪张表建模。可以说「基于 t_sales 创建模型」，或先在数据页导入一张表。",
                "at": _now()})
            return _public(session)
        session["table"] = table
        draft = datasets.suggest_model(table)
        session["doc"] = draft["doc"]
        session["history"].append(copy.deepcopy(draft["doc"]))
        session["messages"].append({"role": "assistant", "text": _welcome(draft), "at": _now()})
        return _public(session)

    doc = session["doc"]
    try:
        new_doc, reply, changed = _apply(text, doc, session.get("table"), session)
    except Exception as e:  # noqa: BLE001
        new_doc, reply, changed = doc, f"操作失败：{e}", False

    if changed:
        session["history"].append(copy.deepcopy(new_doc))
        session["doc"] = new_doc

    session["messages"].append({"role": "assistant", "text": reply, "at": _now()})
    return _public(session)


def save_session(sid: str) -> dict:
    session = get_session(sid)
    doc = session.get("doc")
    if not doc:
        raise ValueError("还没有模型可以保存")
    result = datasets.save_draft(doc["name"], _yaml(doc))
    return {"saved": result["saved"], "name": doc["name"], "lint": result["lint"]}


# ---------------------------------------------------------------------------
# 意图解析
# ---------------------------------------------------------------------------
def _find_table(text: str) -> str | None:
    tables = {t["name"] for t in datasets.list_tables()}
    for t in sorted(tables, key=len, reverse=True):
        if t in text:
            return t
    return None


def _apply(text: str, doc: dict, table: str | None, session: dict):
    t = text.strip()
    cols = _column_index(table) if table else {}

    # 1. 检查
    if re.search(r"(检查|校验|合规|有没有问题|lint)", t, re.IGNORECASE):
        return doc, _check_reply(doc), False

    # 2. 查看现状
    if re.search(r"(看看|列出|有哪些|现状|总结一下|现在有什么|描述)", t) and not re.search(r"(加|新增|改|删|设置)", t):
        return doc, _summary(doc), False

    # 3. 回退
    if re.search(r"(撤销|回退|退回|上一步)", t):
        if len(session["history"]) >= 2:
            session["history"].pop()
            return copy.deepcopy(session["history"][-1]), "已回退到上一步。", True
        return doc, "已经是最初版本，无法回退。", False

    # 4. 加同义词
    m = re.search(r"(?:给|为)\s*[「\"]?(.+?)[」\"]?\s*(?:加|添加|设置)(?:个|一个)?(?:别名|同义词)\s*[「\"]?(.+?)[」\"]?\s*$", t)
    if m:
        return _add_synonym(doc, m.group(1), m.group(2))

    # 5. 改名
    m = re.search(r"(?:把|将)\s*[「\"]?(.+?)[」\"]?\s*(?:改名为|改成|重命名为|更名为)\s*[「\"]?(.+?)[」\"]?\s*$", t)
    if m:
        return _rename(doc, m.group(1), m.group(2))

    # 6. 新增指标
    m = re.search(r"(?:新增|添加|加)(?:一个|个)?\s*[「\"]?(.+?)[」\"]?\s*(?:指标|度量|metric)", t)
    if m:
        return _add_metric(doc, m.group(1), t, cols)

    # 7. 新增维度
    m = re.search(r"(?:新增|添加|加)(?:一个|个)?\s*[「\"]?(.+?)[」\"]?\s*(?:维度|dimension)", t)
    if m:
        return _add_dimension(doc, m.group(1), cols)

    # 8. 删除
    m = re.search(r"(?:删除|删掉|去掉|移除)\s*[「\"]?(.+?)[」\"]?\s*(?:指标|维度)?\s*$", t)
    if m:
        return _remove(doc, m.group(1))

    # 9. 改名模型（没有「把」的情况）
    m = re.search(r"(?:模型|数据集)(?:改名|命名为|重命名为|叫)\s*[「\"]?(.+?)[」\"]?\s*$", t)
    if m:
        doc["title"] = m.group(1)
        return doc, f"模型标题已改为「{doc['title']}」。", True

    return doc, _help(), False


# ---------------------------------------------------------------------------
# 操作实现
# ---------------------------------------------------------------------------
def _column_index(table: str | None) -> dict[str, dict]:
    if not table:
        return {}
    try:
        detail = datasets.table_detail(table, sample_limit=1)
    except KeyError:
        return {}
    idx: dict[str, dict] = {}
    for c in detail["columns"]:
        idx[c["name"].lower()] = c
        idx[str(c.get("source", "")).lower()] = c
    return idx


def _find_col(cols: dict, token: str) -> dict | None:
    token = token.strip().lower()
    if token in cols:
        return cols[token]
    for k, v in cols.items():
        if token and token in k:
            return v
    return None


def _next_name(doc: dict, prefix: str) -> str:
    used = {m["name"] for m in doc.get("metrics") or []}
    i = 1
    while f"{prefix}{i}" in used:
        i += 1
    return f"{prefix}{i}"


def _add_metric(doc: dict, token: str, full_text: str, cols: dict):
    title = token.strip()
    # 已存在？
    for m in doc.get("metrics") or []:
        names = [m["name"], m.get("title", "")] + list(m.get("synonyms") or [])
        if title in names:
            return doc, f"已经有指标「{m.get('title', m['name'])}」了，不用重复添加。", False

    # 公式形式
    fm = re.search(r"(?:公式|等于|算式|[=＝])\s*(.+)$", full_text)
    if fm:
        expr = _parse_formula(fm.group(1), doc)
        if not expr:
            return doc, ("没看懂公式。可以用已有指标名写四则运算，例如："
                         "「加一个客单价指标，等于成交额/订单量」。"), False
        name = _next_name(doc, "m")
        doc.setdefault("metrics", []).append({
            "name": name, "title": title, "type": "derived",
            "expr": expr, "format": _guess_format(title, expr),
        })
        return doc, f"已新增派生指标 `{name}`（{title}），表达式 `{expr}`。", True

    # 列形式
    col = _find_col(cols, title)
    if col:
        spec = datasets._metric_spec(col["name"], col.get("source", title), col.get("kind") or "number")
        if not spec:
            return doc, f"「{title}」是布尔字段，更适合做维度而不是指标。", False
        spec["title"] = title
        if spec["name"] in {m["name"] for m in doc.get("metrics") or []}:
            spec["name"] = _next_name(doc, "m")
        doc.setdefault("metrics", []).append(spec)
        return doc, (f"已新增指标 `{spec['name']}`（{title}），"
                     f"聚合方式 {spec['agg']}，格式 {spec['format']}。"), True

    return doc, (f"没找到「{title}」这一列。可以先用「看看有哪些字段」确认，"
                 "或者用公式形式：加一个 X 指标，等于 A/B。"), False


def _add_dimension(doc: dict, token: str, cols: dict):
    title = token.strip()
    for d in doc.get("dimensions") or []:
        names = [d["name"], d.get("title", "")] + list(d.get("synonyms") or [])
        if title in names:
            return doc, f"已经有维度「{d.get('title', d['name'])}」了。", False

    col = _find_col(cols, title)
    if not col:
        return doc, f"没找到「{title}」这一列，无法加为维度。", False

    kind = col.get("kind") or "string"
    dim = {"name": col["name"], "title": title, "type": "time" if kind == "time" else
           ("boolean" if kind == "boolean" else "string"),
           "column": f"f.{col['name']}", "synonyms": [title] if title != col["name"] else []}
    if dim["type"] == "time":
        dim["granularities"] = ["day", "week", "month", "quarter", "year"]
    if dim["name"] in {d["name"] for d in doc.get("dimensions") or []}:
        dim["name"] = dim["name"] + "_2"
    doc.setdefault("dimensions", []).append(dim)
    return doc, f"已新增维度 `{dim['name']}`（{title}），类型 {dim['type']}。", True


def _rename(doc: dict, left: str, right: str):
    left, right = left.strip(), right.strip()

    # 模型改名
    if "模型" in left or "模型" in right:
        doc["title"] = right
        return doc, f"模型标题已改为「{right}」。", True

    for key in ("metrics", "dimensions"):
        for item in doc.get(key) or []:
            names = [item["name"], item.get("title", "")] + list(item.get("synonyms") or [])
            if left in names:
                item["title"] = right
                syn = list(item.get("synonyms") or [])
                if left not in syn and left != right:
                    syn.append(left)
                item["synonyms"] = syn
                return doc, f"已把「{left}」改名为「{right}」（原名称保留为同义词）。", True
    return doc, f"没找到「{left}」，无法改名。", False


def _add_synonym(doc: dict, left: str, right: str):
    left, right = left.strip(), right.strip()
    for key in ("metrics", "dimensions"):
        for item in doc.get(key) or []:
            names = [item["name"], item.get("title", "")] + list(item.get("synonyms") or [])
            if left in names:
                syn = list(item.get("synonyms") or [])
                if right in syn:
                    return doc, f"「{right}」已经是指标/维度的同义词了。", False
                syn.append(right)
                item["synonyms"] = syn
                return doc, f"已给「{item.get('title', item['name'])}」加上同义词「{right}」。", True
    return doc, f"没找到「{left}」。", False


def _remove(doc: dict, token: str):
    token = token.strip()
    for key, label in (("metrics", "指标"), ("dimensions", "维度")):
        items = doc.get(key) or []
        for i, item in enumerate(items):
            names = [item["name"], item.get("title", "")] + list(item.get("synonyms") or [])
            if token in names:
                items.pop(i)
                return doc, f"已删除{label}「{item.get('title', item['name'])}」。", True
    return doc, f"没找到「{token}」。", False


# ---------------------------------------------------------------------------
def _parse_formula(text: str, doc: dict) -> str | None:
    out = text
    refs: list[str] = []
    aliases: list[tuple[str, str]] = []
    for m in doc.get("metrics") or []:
        for alias in [m["name"], m.get("title", "")] + list(m.get("synonyms") or []):
            if alias:
                aliases.append((alias, m["name"]))
    for alias, name in sorted(aliases, key=lambda x: -len(x[0])):
        if alias in out:
            out = out.replace(alias, "{" + name + "}")
            refs.append(name)

    out = (out.replace("×", "*").replace("÷", "/")
              .replace("（", "(").replace("）", ")")
              .replace("＝", "=").replace(" ", "").strip())
    out = out.lstrip("=").strip()
    if not refs or not out:
        return None
    if not EXPR_OK.fullmatch(out):
        return None
    return out


def _guess_format(title: str, expr: str) -> str:
    if re.search(r"(率|rate|占比|比例)", title, re.IGNORECASE):
        return "percent"
    if re.search(r"(金额|额|amt|price)", title, re.IGNORECASE):
        return "currency"
    return "number"


# ---------------------------------------------------------------------------
# 回复文案
# ---------------------------------------------------------------------------
def _welcome(draft: dict) -> str:
    s = draft["stats"]
    lines = [f"已基于表 `{draft['table']}` 生成初稿：{s['metrics']} 个指标、{s['dimensions']} 个维度。"]
    if draft.get("warnings"):
        lines.append("提示：" + "；".join(draft["warnings"]))
    lines.append("可以继续说：「加一个客单价指标，等于订单金额/订单数量」「把订单金额改名为成交金额」「检查一下」。")
    return "\n".join(lines)


def _summary(doc: dict) -> str:
    ms = [f"{m.get('title', m['name'])}（{m['name']}）" for m in doc.get("metrics") or []]
    ds = [f"{d.get('title', d['name'])}（{d['name']}）" for d in doc.get("dimensions") or []]
    return (f"当前模型「{doc.get('title', '')}」\n"
            f"指标 {len(ms)} 个：{'、'.join(ms) or '无'}\n"
            f"维度 {len(ds)} 个：{'、'.join(ds) or '无'}")


def _check_reply(doc: dict) -> str:
    errors = validate(doc)
    lint = _lint(doc)
    parts = ["校验结果："]
    if not errors and lint["passed"]:
        parts.append("✓ 结构校验与规范校验都通过，可以保存。")
    else:
        parts.extend(f"✗ {e}" for e in errors)
        parts.extend(f"✗ [{i['rule']}] {i['message']}" for i in lint["errors"])
    if lint["warnings"]:
        parts.append("提示：" + "；".join(i["message"] for i in lint["warnings"]))
    return "\n".join(parts)


def _help() -> str:
    return ("没太听懂。可以试试这些说法：\n"
            "· 加一个客单价指标，等于订单金额/订单数量\n"
            "· 加一个渠道维度\n"
            "· 把订单金额改名为成交金额\n"
            "· 给成交金额加个别名 销售额\n"
            "· 看看现在有哪些指标\n"
            "· 检查一下\n"
            "· 撤销")
