"""知识层：回答「为什么这么算、口径什么含义、怎么解读」。

语义层（ecommerce.yaml）负责「怎么算」，知识层（knowledge.yaml）负责「为什么」。
两者合起来，才能让 AI 的解释有依据，而不是编。
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from . import config
from .contracts import QuerySpec
from .semantic import EXPR_REF, SemanticModel, SemanticError

_cache: dict | None = None


def load(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache
    path = config.SEMANTIC_DIR / "knowledge.yaml"
    if not path.exists():
        _cache = {"metrics": {}, "dimensions": {}, "glossary": []}
        return _cache
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _cache = {
        "metrics": doc.get("metrics") or {},
        "dimensions": doc.get("dimensions") or {},
        "glossary": doc.get("glossary") or [],
    }
    return _cache


# ---------------------------------------------------------------------------
# 血缘
# ---------------------------------------------------------------------------
def _resolve_table(model: SemanticModel, column: str) -> tuple[str | None, str]:
    alias, _, field = str(column).partition(".")
    if not field:                      # 没有别名，直接当字段
        return model.fact_table, str(column)
    if alias == model.fact_alias:
        return model.fact_table, field
    for j in model.joins:
        if j.get("alias") == alias:
            return j.get("table"), field
    return None, field


def metric_lineage(model: SemanticModel, name: str) -> dict:
    m = model.metric(name)
    mtype = m.get("type", "atomic")

    if mtype == "derived":
        refs = EXPR_REF.findall(m.get("expr", ""))
        tables: set[str] = set()
        for r in refs:
            t, _, _ = _collect_tables(model, r)
            tables |= t
        return {
            "metric": name,
            "type": "derived",
            "expression": m.get("expr", ""),
            "depends_on": refs,
            "tables": sorted(tables),
            "path": [name, "= " + m.get("expr", ""), "→ " + " / ".join(refs)],
        }

    if mtype == "window":
        base = m.get("base")
        tables, _, _ = _collect_tables(model, base) if base else (set(), [], [])
        return {
            "metric": name,
            "type": "window",
            "base": base,
            "window": m.get("window"),
            "depends_on": [base] if base else [],
            "tables": sorted(tables),
        }

    table, field = _resolve_table(model, m.get("column", ""))
    return {
        "metric": name,
        "type": "atomic",
        "aggregation": m.get("agg"),
        "column": m.get("column"),
        "table": table,
        "field": field,
        "filters": m.get("filters") or [],
        "path": [f"{table}.{field}" if table else str(field),
                 f"{m.get('agg')}()",
                 f"metric:{name}"],
    }


def _collect_tables(model: SemanticModel, name: str) -> tuple[set[str], list, list]:
    """递归收集指标依赖的表。"""
    tables: set[str] = set()
    chain: list[str] = []
    seen: set[str] = set()

    def walk(n: str) -> None:
        if n in seen:
            return
        seen.add(n)
        m = model.metric(n)
        chain.append(n)
        if m.get("type") == "derived":
            for r in EXPR_REF.findall(m.get("expr", "")):
                walk(r)
        elif m.get("type") == "window":
            if m.get("base"):
                walk(m["base"])
        else:
            t, _ = _resolve_table(model, m.get("column", ""))
            if t:
                tables.add(t)

    walk(name)
    return tables, chain, []


def dependents(model: SemanticModel, name: str) -> list[str]:
    """哪些指标直接或间接依赖这个指标（改口径的影响面）。"""
    out: list[str] = []
    for mname, m in model.metrics.items():
        if mname == name:
            continue
        refs = set(EXPR_REF.findall(m.get("expr", ""))) if m.get("type") == "derived" else set()
        if m.get("type") == "window" and m.get("base"):
            refs.add(m["base"])
        if name in refs:
            out.append(mname)
    # 传递依赖
    changed = True
    while changed:
        changed = False
        for mname, m in model.metrics.items():
            if mname in out:
                continue
            refs = set(EXPR_REF.findall(m.get("expr", ""))) if m.get("type") == "derived" else set()
            if m.get("type") == "window" and m.get("base"):
                refs.add(m["base"])
            if refs & set(out):
                out.append(mname)
                changed = True
    return out


# ---------------------------------------------------------------------------
# 描述
# ---------------------------------------------------------------------------
def describe_metric(model: SemanticModel, name: str) -> dict:
    model.metric(name)  # 不存在会抛 SemanticError
    kb = load()["metrics"].get(name) or {}
    return {
        "name": name,
        "title": model.metric(name).get("title", name),
        "type": model.metric(name).get("type", "atomic"),
        "definition": model.metric_definition(name),
        "lineage": metric_lineage(model, name),
        "dependents": dependents(model, name),
        "format": model.metric(name).get("format"),
        "business": kb or None,
    }


def describe_dimension(model: SemanticModel, name: str) -> dict:
    model.dimension(name)
    d = model.dimension(name)
    kb = load()["dimensions"].get(name) or {}
    table, field = _resolve_table(model, d.get("column", ""))
    return {
        "name": name,
        "title": d.get("title", name),
        "type": d.get("type", "string"),
        "column": d.get("column"),
        "table": table,
        "field": field,
        "dictionary": d.get("dictionary"),
        "business": kb or None,
    }


def explain_spec(model: SemanticModel, spec_doc: dict) -> dict:
    spec = QuerySpec.model_validate(spec_doc)
    metrics = [describe_metric(model, m) for m in spec.metrics]
    dimensions = [describe_dimension(model, d) for d in spec.dimensions]
    return {
        "spec": spec.model_dump(),
        "semantic_model": f"{model.name}@{model.version}",
        "metrics": metrics,
        "dimensions": dimensions,
        "glossary": load()["glossary"],
    }


def search(q: str) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    kb = load()
    hits: list[dict] = []
    for name, item in kb["metrics"].items():
        text = str(item.get("business_meaning", "")) + str(item.get("faq", ""))
        if q in name or q in text:
            hits.append({"kind": "metric", "name": name,
                         "meaning": item.get("business_meaning", "")})
    for term in kb["glossary"]:
        if q in term.get("term", "") or q in term.get("definition", ""):
            hits.append({"kind": "term", "name": term.get("term"), "meaning": term.get("definition")})
    return hits[:20]
