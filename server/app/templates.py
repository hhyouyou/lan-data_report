"""模板库：AI 建模 / 建看板时的脚手架。

AI 的工作从「从零生成」变成「选模板 + 填参数 + 适配业务」，
这样产出的结构、字段、布局天然一致，不会每次都换风格。

模板 = 规范的可执行形态：
  - 结构来自 standards/
  - 渲染完再跑一遍 lint，不合规的模板产出无法落地
"""
from __future__ import annotations

import re
import uuid

import yaml

from . import config, standards

TPL_DIR = config.ROOT / "templates"
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

_cache: dict[str, dict] | None = None


# ---------------------------------------------------------------------------
def load_all(force: bool = False) -> dict[str, dict]:
    global _cache
    if _cache is not None and not force:
        return _cache
    out: dict[str, dict] = {}
    if TPL_DIR.exists():
        for path in sorted(TPL_DIR.rglob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not doc or "id" not in doc or "body" not in doc:
                continue
            doc["_path"] = str(path.relative_to(config.ROOT))
            out[doc["id"]] = doc
    _cache = out
    return out


def get(tid: str) -> dict:
    t = load_all().get(tid)
    if not t:
        raise KeyError(f"模板不存在: {tid}")
    return t


def summary(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "kind", "title", "description", "tags", "variables")}


def list_templates(kind: str | None = None) -> list[dict]:
    return [summary(t) for t in load_all().values()
            if not kind or t.get("kind") == kind]


# ---------------------------------------------------------------------------
def _defaults(t: dict) -> dict:
    return {v["name"]: v["default"]
            for v in (t.get("variables") or [])
            if v.get("default") is not None}


def _substitute(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _postprocess(kind: str, doc: dict) -> dict:
    if kind == "card":
        doc.setdefault("id", "c_" + uuid.uuid4().hex[:6])
        doc.setdefault("layout", {"x": 0, "y": 0, "w": 6, "h": 4})
    elif kind == "dashboard":
        doc.setdefault("status", "draft")
        doc.setdefault("version", 1)
        for i, card in enumerate(doc.get("cards") or []):
            card.setdefault("id", f"c_{i}")
    return doc


def _lint(kind: str, doc: dict) -> dict:
    if kind == "card":
        return standards.lint("dashboard", {"title": "模板卡片", "cards": [doc]})
    return standards.lint(kind, doc)


def render(tid: str, params: dict | None = None) -> dict:
    t = get(tid)
    values = _defaults(t)
    values.update({k: v for k, v in (params or {}).items() if v is not None and v != ""})

    missing = [v["name"] for v in (t.get("variables") or [])
               if v.get("required") and not values.get(v["name"])]
    if missing:
        return {"template": summary(t), "params": values, "missing": missing,
                "doc": None, "lint": None}

    text = _substitute(t["body"], values)
    # 没被替换掉的占位符（排除 YAML 里的流式映射，如 { kind: kpi }）
    unresolved = [p for p in PLACEHOLDER.findall(text) if p in {v["name"] for v in t.get("variables") or []}]

    doc = _postprocess(t.get("kind", ""), yaml.safe_load(text))
    if not doc.get("description"):
        doc["description"] = t.get("description") or ""   # 看板简介：模板 frontmatter 即默认文案
    return {
        "template": summary(t),
        "params": values,
        "missing": [],
        "unresolved": sorted(set(unresolved)),
        "doc": doc,
        "lint": _lint(t.get("kind", ""), doc),
    }
