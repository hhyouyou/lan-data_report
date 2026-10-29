"""规范即代码：加载建模 / 报表规范，对 AI 产出做自动 Lint。

规范同时服务两类消费者：
  1. 机器：lint() 校验，error 级别直接阻断提交审批
  2. AI：context_pack() 生成规范摘要，注入 prompt

同一份 YAML，两边不漂移。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from . import config

STANDARDS_DIR = config.ROOT / "standards"

VALID_FORMATS = {"currency", "percent", "integer", "number", "duration"}

_cache: dict | None = None


@dataclass
class Issue:
    rule: str
    severity: str          # error | warn
    message: str
    target: str = ""


def load(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache
    out: dict = {}
    for key in ("modeling", "reporting"):
        path = STANDARDS_DIR / f"{key}.yaml"
        out[key] = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    _cache = out
    return _cache


# ---------------------------------------------------------------------------
# Lint 工具
# ---------------------------------------------------------------------------
def _add(issues: list[Issue], enf: dict, rule: str, message: str, target: str = "") -> None:
    sev = enf.get(rule, "off")
    if sev == "off":
        return
    issues.append(Issue(rule=rule, severity=sev, message=message, target=target))


def _strip_alias(column: str) -> str:
    return str(column).partition(".")[2] or str(column)


# ---------------------------------------------------------------------------
# 建模规范 Lint
# ---------------------------------------------------------------------------
def lint_semantic(doc: dict) -> list[Issue]:
    std = load()["modeling"]
    enf = std.get("enforcement") or {}
    naming = std.get("naming") or {}
    issues: list[Issue] = []

    metrics = doc.get("metrics") or []
    dims = doc.get("dimensions") or []
    names = {m.get("name") for m in metrics} | {d.get("name") for d in dims}

    # 表命名
    tables = [doc.get("fact_table")] + [j.get("table") for j in (doc.get("joins") or [])]
    pattern = naming.get("table_pattern")
    if pattern:
        for t in filter(None, tables):
            if not re.match(pattern, str(t)):
                _add(issues, enf, "table_naming",
                     f"表名 {t} 不符合规范 {pattern}（应为 层_域_主题_后缀）", target=str(t))

    # 指标
    mpat = naming.get("metric_pattern")
    for m in metrics:
        name = m.get("name") or ""
        if mpat and not re.match(mpat, name):
            _add(issues, enf, "metric_naming", f"指标名 {name} 不符合命名规范", target=name)
        if not m.get("title"):
            _add(issues, enf, "required_metadata", f"指标 {name} 缺少 title", target=name)
        fmt = m.get("format")
        if fmt and fmt not in VALID_FORMATS:
            _add(issues, enf, "required_metadata", f"指标 {name} 的 format 「{fmt}」非法", target=name)
        if not m.get("format"):
            _add(issues, enf, "required_metadata", f"指标 {name} 缺少 format（影响图表格式）", target=name)
        col = m.get("column")
        if col and m.get("type", "atomic") == "atomic":
            fpat = naming.get("field_pattern")
            field = _strip_alias(col)
            if fpat and not re.match(fpat, field):
                _add(issues, enf, "field_naming",
                     f"指标 {name} 的字段 {field} 不符合命名规范", target=name)

    # 维度
    dpat = naming.get("dimension_pattern")
    for d in dims:
        name = d.get("name") or ""
        if dpat and not re.match(dpat, name):
            _add(issues, enf, "dimension_naming", f"维度名 {name} 不符合命名规范", target=name)
        if not d.get("title"):
            _add(issues, enf, "required_metadata", f"维度 {name} 缺少 title", target=name)
        col = d.get("column")
        fpat = naming.get("field_pattern")
        if col and fpat:
            field = _strip_alias(col)
            if not re.match(fpat, field):
                _add(issues, enf, "field_naming",
                     f"维度 {name} 的字段 {field} 不符合命名规范", target=name)

    # 时间维度
    if not any(d.get("type") == "time" for d in dims):
        _add(issues, enf, "time_dimension", "缺少 time 类型维度，趋势类问题无法回答")

    # 同义词冲突
    seen: dict[str, str] = {}
    for item in list(metrics) + list(dims):
        name = item.get("name") or ""
        for alias in item.get("synonyms") or []:
            if alias in seen and seen[alias] != name:
                _add(issues, enf, "synonym_uniqueness",
                     f"同义词「{alias}」同时指向 {seen[alias]} 和 {name}", target=alias)
            seen.setdefault(alias, name)

    # 标准术语一致性
    for kw in std.get("keywords") or []:
        canonical = kw.get("canonical")
        term = kw.get("term")
        if canonical and canonical in names:
            continue
        if term in names:
            _add(issues, enf, "keyword_consistency",
                 f"「{term}」应使用标准名 {canonical}", target=str(term))

    return issues


# ---------------------------------------------------------------------------
# 报表规范 Lint
# ---------------------------------------------------------------------------
def lint_dashboard(doc: dict) -> list[Issue]:
    std = load()["reporting"]
    enf = std.get("enforcement") or {}
    issues: list[Issue] = []

    if not doc.get("title"):
        _add(issues, enf, "title_required", "看板标题不能为空")

    cols = ((std.get("theme") or {}).get("grid") or {}).get("cols", 12)
    allowed = set(std.get("allowed_chart_kinds") or [])
    cards = doc.get("cards") or []

    if len(cards) > std.get("max_cards", 12):
        _add(issues, enf, "max_cards",
             f"卡片数 {len(cards)} 超过上限 {std.get('max_cards')}")

    seen_ids: set[str] = set()
    for card in cards:
        cid = card.get("id") or ""
        if cid in seen_ids:
            _add(issues, enf, "layout_bounds", f"卡片 id 重复: {cid}", target=cid)
        seen_ids.add(cid)

        kind = ((card.get("visualization") or {}).get("kind"))
        if allowed and kind not in allowed:
            _add(issues, enf, "chart_type",
                 f"卡片 {cid} 图表类型「{kind}」不在允许列表 {sorted(allowed)}", target=cid)

        title = (card.get("title") or "").strip()
        if not title or re.match(r"^(图表|card|chart)\s*\d*$", title, re.IGNORECASE):
            _add(issues, enf, "card_title", f"卡片 {cid} 标题为空或使用默认名", target=cid)

        L = card.get("layout") or {}
        x, y, w, h = (L.get("x", 0), L.get("y", 0), L.get("w", 1), L.get("h", 1))
        if w < 1 or h < 1 or x < 0 or y < 0 or x + w > cols:
            _add(issues, enf, "layout_bounds",
                 f"卡片 {cid} 布局越界: x={x}, w={w}, cols={cols}", target=cid)

    # 重叠检测
    rects = []
    for card in cards:
        L = card.get("layout") or {}
        rects.append((card.get("id"), L.get("x", 0), L.get("y", 0), L.get("w", 1), L.get("h", 1)))
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            if a[1] < b[1] + b[3] and b[1] < a[1] + a[3] and \
               a[2] < b[2] + b[4] and b[2] < a[2] + a[4]:
                _add(issues, enf, "layout_overlap",
                     f"卡片 {a[0]} 与 {b[0]} 布局重叠", target=a[0])

    return issues


def lint_table(spec: dict) -> list[Issue]:
    """数据表规格（建模产出）的 lint。"""
    std = load()["modeling"]
    enf = std.get("enforcement") or {}
    naming = std.get("naming") or {}
    issues: list[Issue] = []

    table = spec.get("table") or spec.get("table_name") or ""
    pattern = naming.get("table_pattern")
    if pattern and table and not re.match(pattern, str(table)):
        _add(issues, enf, "table_naming",
             f"表名 {table} 不符合规范 {pattern}（应为 层_域_主题_后缀）", target=str(table))

    for key in (std.get("required_metadata") or {}).get("table", []):
        if not spec.get(key):
            _add(issues, enf, "required_metadata", f"表规格缺少必填项 {key}", target=str(table))

    layer = spec.get("layer")
    layers = std.get("warehouse_layers") or {}
    if layer and layer not in layers:
        _add(issues, enf, "table_naming", f"未知分层 {layer}", target=str(table))

    domain = spec.get("domain")
    domains = {d.get("key") for d in (std.get("data_domains") or [])}
    if domain and domain not in domains:
        _add(issues, enf, "table_naming", f"未知数据域 {domain}", target=str(table))

    return issues


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------
def lint(kind: str, doc: dict) -> dict:
    if kind == "semantic":
        issues = lint_semantic(doc)
    elif kind == "dashboard":
        issues = lint_dashboard(doc)
    elif kind == "table":
        issues = lint_table(doc)
    else:
        raise ValueError(f"不支持的规范类型: {kind}")

    errors = [asdict(i) for i in issues if i.severity == "error"]
    warnings = [asdict(i) for i in issues if i.severity == "warn"]
    return {
        "kind": kind,
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def context_pack() -> str:
    """给 AI 的规范摘要（Markdown）。建模/报表生成前注入 prompt。"""
    std = load()
    M = std["modeling"]
    R = std["reporting"]
    lines: list[str] = ["# 平台规范（必须遵守）", ""]

    lines.append("## 数仓分层")
    for key, layer in (M.get("warehouse_layers") or {}).items():
        lines.append(f"- **{key}**（{layer.get('title')}）：{layer.get('desc')}；"
                     f"表前缀 `{layer.get('table_prefix')}`")
    lines.append("")

    naming = M.get("naming") or {}
    lines.append("## 命名规范")
    lines.append(f"- 表名规则：`{naming.get('table_rule')}`，正则 `{naming.get('table_pattern')}`")
    lines.append(f"- 字段：`{naming.get('field_pattern')}`；指标：`{naming.get('metric_pattern')}`")
    if naming.get("table_suffix"):
        lines.append("- 表后缀：" + "，".join(f"`{k}`={v}" for k, v in naming["table_suffix"].items()))
    if naming.get("field_suffix"):
        lines.append("- 字段后缀：" + "，".join(f"`{k}`={v}" for k, v in naming["field_suffix"].items()))
    lines.append("")

    lines.append("## 数据域")
    for d in M.get("data_domains") or []:
        lines.append(f"- `{d.get('key')}` {d.get('title')}：{d.get('desc')}（{d.get('owner')}）")
    lines.append("")

    lines.append("## 标准术语")
    for kw in M.get("keywords") or []:
        lines.append(f"- {kw.get('term')} → `{kw.get('canonical')}`：{kw.get('definition')}")
    lines.append("")

    lines.append("## 图表选择")
    for item in R.get("chart_selection") or []:
        limit = f"（最多 {item['limit']} 类）" if item.get("limit") else ""
        lines.append(f"- {item.get('condition')} → **{item.get('chart')}**{limit}；{item.get('reason')}")
    lines.append("")

    lines.append("## 布局与命名")
    for rule in R.get("layout_rules") or []:
        lines.append(f"- {rule}")
    rn = R.get("naming") or {}
    lines.append(f"- 看板命名：`{rn.get('dashboard_rule')}`")
    lines.append(f"- 卡片命名：`{rn.get('card_rule')}`")
    lines.append("")

    lines.append("## 禁止事项")
    for f in R.get("forbidden") or []:
        lines.append(f"- {f}")
    lines.append("")

    lines.append("## 执行级别")
    lines.append("违反 error 级规则会被拒绝提交审批：")
    for group, enf in (("建模", M.get("enforcement") or {}), ("报表", R.get("enforcement") or {})):
        errs = [k for k, v in enf.items() if v == "error"]
        if errs:
            lines.append(f"- {group}：" + "、".join(errs))

    return "\n".join(lines)
