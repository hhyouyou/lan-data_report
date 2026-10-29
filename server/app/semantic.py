"""语义模型：加载、校验、实体检索。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

EXPR_REF = re.compile(r"\{(\w+)\}")

AGG_WHITELIST = {"sum", "count", "count_distinct", "avg", "min", "max"}


class SemanticError(Exception):
    pass


class SemanticModel:
    def __init__(self, doc: dict, path: str = ""):
        self.doc = doc
        self.path = path
        self.name: str = doc["name"]
        self.title: str = doc.get("title", doc["name"])
        self.desc: str = doc.get("description", "")
        # 数据性质：real=真实业务/统计数据，demo=合成演示数据（空=未标注）
        self.data_nature: str = doc.get("data_nature", "")
        self.data_note: str = doc.get("data_note", "")
        # 数据源：config.DATA_SOURCES 的 key（如 jsfj / demo），决定查询打到哪个库
        self.source: str = doc.get("source", "")
        self.datasource: str = doc.get("datasource", "")
        self.fact_table: str = doc["fact_table"]
        self.fact_alias: str = doc.get("fact_alias", "f")
        self.joins: list[dict] = doc.get("joins") or []
        # PyYAML 把裸写的 on: 解析成布尔 True，这里统一修正
        for j in self.joins:
            if "on" not in j and True in j:
                j["on"] = j.pop(True)
        self.dimensions: dict[str, dict] = {d["name"]: d for d in doc.get("dimensions") or []}
        self.metrics: dict[str, dict] = {m["name"]: m for m in doc.get("metrics") or []}
        self.synonyms: dict[str, str] = doc.get("synonyms") or {}
        self.rls: list[dict] = doc.get("rls") or []
        self.version: str = doc.get("version_label", "v1")
        # 年度数据：默认取最新年份 / 趋势按年
        self.default_time_range: str = doc.get("default_time_range", "last_30d")
        self.default_grain: str = doc.get("default_grain", "day")

    # ---------- 查询 ----------
    def metric(self, name: str) -> dict:
        if name not in self.metrics:
            raise SemanticError(f"未知指标: {name}")
        return self.metrics[name]

    def dimension(self, name: str) -> dict:
        if name not in self.dimensions:
            raise SemanticError(f"未知维度: {name}")
        return self.dimensions[name]

    def time_dimension(self) -> dict | None:
        for d in self.dimensions.values():
            if d.get("type") == "time":
                return d
        return None

    # ---------- 实体检索（给实体链接用） ----------
    def search_metric(self, text: str) -> list[dict]:
        hits = []
        for m in self.metrics.values():
            names = [m["name"], m.get("title", "")] + list(m.get("synonyms") or [])
            if any(n and n in text for n in names):
                hits.append(m)
        return hits

    def search_dimension(self, text: str) -> list[dict]:
        hits = []
        for d in self.dimensions.values():
            names = [d["name"], d.get("title", "")] + list(d.get("synonyms") or [])
            if any(n and n in text for n in names):
                hits.append(d)
        return hits

    def search_values(self, text: str) -> list[tuple[str, str]]:
        """维度值匹配：返回 [(dimension, value)]，支持「南京」匹配「南京市」。"""
        hits = []
        for d in self.dimensions.values():
            for v in d.get("dictionary") or []:
                sv = str(v) if v is not None else ""
                if not sv:
                    continue
                stem = sv[:-1] if sv.endswith(("市", "省")) and len(sv) > 1 else sv
                if sv in text or (len(stem) >= 2 and stem in text):
                    hits.append((d["name"], sv))
        return hits

    # ---------- 展示 ----------
    def metric_definition(self, name: str) -> dict:
        m = self.metric(name)
        if m.get("type") == "derived":
            return {"name": name, "title": m.get("title", name),
                    "definition": m.get("expr", ""), "type": "derived"}
        filters = " AND ".join(m.get("filters") or []) or "无"
        return {"name": name, "title": m.get("title", name), "type": m.get("type", "atomic"),
                "definition": f"{m.get('agg')}({m.get('column')}) [where {filters}]"}

    def column_meta(self, name: str) -> dict:
        if name in self.metrics:
            m = self.metrics[name]
            is_time = False
            return {"name": name, "label": m.get("title", name),
                    "type": "time" if is_time else "number", "format": m.get("format")}
        if name in self.dimensions:
            d = self.dimensions[name]
            return {"name": name, "label": d.get("title", name),
                    "type": "time" if d.get("type") == "time" else "string", "format": None}
        return {"name": name, "label": name, "type": "string", "format": None}


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------
def load_model(name: str, semantic_dir: Path) -> SemanticModel:
    return load_model_file(semantic_dir / f"{name}.yaml")


def load_model_file(path: Path) -> SemanticModel:
    if not path.exists():
        raise SemanticError(f"语义模型不存在: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = validate(doc)
    if errors:
        raise SemanticError("语义模型校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
    return SemanticModel(doc, str(path))


def validate(doc: dict) -> list[str]:
    """实现 docs/02 第 1.8 节的全部规则。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["根节点必须是 mapping"]
    if not doc.get("name"):
        errors.append("缺少 name")
    if not doc.get("fact_table"):
        errors.append("缺少 fact_table")

    metrics = doc.get("metrics") or []
    dims = doc.get("dimensions") or []

    for key, items in (("metric", metrics), ("dimension", dims)):
        names = [i.get("name") for i in items]
        dup = {n for n in names if n and names.count(n) > 1}
        if dup:
            errors.append(f"{key} 名称重复: {sorted(dup)}")
        for item in items:
            if not item.get("name"):
                errors.append(f"{key} 缺少 name: {item}")

    metric_names = {m.get("name") for m in metrics if m.get("name")}
    dim_names = {d.get("name") for d in dims if d.get("name")}

    deps: dict[str, set[str]] = {}
    for m in metrics:
        name = m.get("name")
        mtype = m.get("type", "atomic")
        if mtype == "atomic":
            if m.get("agg") not in AGG_WHITELIST:
                errors.append(f"[{name}] 非法或缺失 agg: {m.get('agg')}")
            if not m.get("column"):
                errors.append(f"[{name}] 原子指标缺少 column")
        elif mtype == "derived":
            expr = m.get("expr") or ""
            refs = set(EXPR_REF.findall(expr))
            deps[name] = refs
            if not refs:
                errors.append(f"[{name}] 派生指标 expr 未引用任何指标")
            for r in refs - metric_names:
                errors.append(f"[{name}] 引用不存在的指标: {r}")
            # 派生只能引用原子/窗口指标（v0.1 限制）
            for r in refs:
                if r in metric_names and _type_of(metrics, r) == "derived":
                    errors.append(f"[{name}] 不支持派生指标引用派生指标: {r}")
        elif mtype == "window":
            if m.get("base") not in metric_names:
                errors.append(f"[{name}] window 指标 base 不存在: {m.get('base')}")
            if m.get("window") not in {"mom", "yoy", "wow", "cumulative"}:
                errors.append(f"[{name}] 非法 window: {m.get('window')}")
        else:
            errors.append(f"[{name}] 未知指标类型: {mtype}")

    # 循环依赖
    color: dict[str, int] = {n: 0 for n in deps}

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = 1
        for nxt in deps.get(node, ()):
            if color.get(nxt) == 1:
                errors.append(f"循环依赖: {' -> '.join(stack + [nxt])}")
            elif color.get(nxt) == 0:
                dfs(nxt, stack + [nxt])
        color[node] = 2

    for n in list(deps):
        if color[n] == 0:
            dfs(n, [n])

    if not any(d.get("type") == "time" for d in dims):
        errors.append("缺少 time 类型维度，趋势类问题无法回答")

    for d in dims:
        if "dictionary" in d and not isinstance(d["dictionary"], list):
            errors.append(f"[{d.get('name')}] dictionary 必须是列表")

    for rls in doc.get("rls") or []:
        if rls.get("dimension") not in dim_names:
            errors.append(f"RLS 引用了不存在的维度: {rls.get('dimension')}")
        if "{{" not in str(rls.get("rule", "")):
            errors.append(f"RLS [{rls.get('dimension')}] rule 缺少 {{ user.xxx }}")

    for alias in (doc.get("synonyms") or {}):
        if alias in metric_names or alias in dim_names:
            errors.append(f"全局同义词「{alias}」与实体重名")

    return errors


def _type_of(metrics: list[dict], name: str) -> str:
    for m in metrics:
        if m.get("name") == name:
            return m.get("type", "atomic")
    return "atomic"
