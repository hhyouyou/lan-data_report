"""看板简介文案生成：根据卡片结构 + 实际数据自动写简介。

看板卡片本身就是结构化知识：指标、维度、时间范围、图表类型、KPI 数值、
排行首项。这里逐卡跑一次 QuerySpec（复用 /api/run-spec 同一个网关），
把这些事实拼成 2–3 句中文简介：

    覆盖 12 项指标（…），涉及 13 个设区市，时间跨度 1978–2024。
    共 13 张卡片：4 张指标卡、7 张图表、1 张地图（可下钻县市）、1 张散点图。
    关键指标：社零总额 44,922.13 亿元…；苏州在「各市社会消费品零售总额」中最高（8,915.12 亿元）。

配置了 LLM（LLM_BASE_URL / LLM_API_KEY）时，可以再走 polish() 把这段摘要
改写成更自然的分析口吻；没配置就只用规则摘要，功能不依赖 LLM。

设计约束：
- 单卡查询失败不影响整段文案（跳过该卡），保证「随时能生成」；
- 不写死任何业务指标名：指标名来自列标签，排行/周期来自查询结果；
- 生成结果是纯文本，交给审批流决定是否发布（应用时看板退回 draft）。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

CARD_KIND_LABEL = {
    "kpi": "指标卡",
    "line": "趋势图",
    "bar": "柱状图",
    "bar_h": "条形图",
    "pie": "饼图",
    "table": "数据表",
    "map": "地图",
    "scatter": "散点图",
}

# 指标标题里的「各市 / 全市 / 县域」只是口径前缀，写进简介太啰嗦
_PREFIXES = ("县市/市区", "各市", "全市", "县域", "全省")
_UNIT_RE = re.compile(r"[（(]([^（）()]{1,14})[）)]")


# ---------------------------------------------------------------- 格式化
def _fmt_num(v, fmt: str | None = None) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if fmt == "percent":
        return f"{x * 100:.2f}%"
    if fmt == "integer":
        return f"{x:,.0f}"
    if abs(x) >= 100:
        s = f"{x:,.2f}"
    elif abs(x) >= 1:
        s = f"{x:,.3f}"
    else:
        s = f"{x:.3f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _unit_text(value: str, unit: str) -> str:
    if not unit:
        return value
    if unit.startswith(("%", "‰")):
        return f"{value}{unit}"
    return f"{value} {unit}"


def _split_title(title: str) -> tuple[str, str]:
    """「社零总额（亿元）」→ ("社零总额", "亿元")；括号里是说明（含数字/「按」）则不算单位。"""
    name = _UNIT_RE.sub("", title or "").strip(" ｜|")
    for raw in reversed(_UNIT_RE.findall(title or "")):
        unit = raw.strip()
        if unit and not any(ch.isdigit() for ch in unit) and "按" not in unit:
            return name, unit
    return name, ""


def _clean_metric_label(label: str) -> str:
    label = (label or "").strip()
    for p in _PREFIXES:
        if label.startswith(p) and len(label) > len(p):
            return label[len(p):]
    return label


# ---------------------------------------------------------------- 采集
def collect(doc: dict, model, user: dict, run_spec) -> list[dict]:
    """逐卡取数：返回 [{title, kind, columns, rows, period, viz}]，失败的卡直接跳过。"""
    cards = []
    for card in doc.get("cards") or []:
        spec = card.get("spec")
        if not spec:
            continue
        try:
            data = run_spec(spec, user, model=model)
        except Exception:  # noqa: BLE001 —— 单卡失败不影响整段文案
            continue
        cards.append({
            "title": card.get("title", ""),
            "kind": (data.get("visualization") or {}).get("kind")
                    or (card.get("visualization") or {}).get("kind") or "table",
            "columns": data.get("columns") or [],
            "rows": (data.get("result") or {}).get("rows") or [],
            "period": data.get("period"),
            "viz": card.get("visualization") or {},
        })
    return cards


def _metric_labels(cards: list[dict], model) -> list[str]:
    out: list[str] = []
    for c in cards:
        for col in c["columns"]:
            name = col.get("name")
            if name in model.metrics:
                label = _clean_metric_label(col.get("label") or model.metrics[name].get("title") or name)
                if label and label not in out:
                    out.append(label)
    return out


def _region_scope(cards: list[dict], model) -> str:
    city_dict = set((model.dimensions.get("region") or {}).get("dictionary") or [])
    vals: list[str] = []
    for c in cards:
        for i, col in enumerate(c["columns"]):
            if col.get("name") not in ("region", "parent_region"):
                continue
            for r in c["rows"]:
                v = r[i] if i < len(r) else None
                if isinstance(v, str) and v and v != "江苏省":
                    vals.append(v)
    uniq = list(dict.fromkeys(vals))
    if not uniq:
        return ""
    cities = [v for v in uniq if v in city_dict]
    if cities and len(cities) >= 0.6 * len(uniq):
        return f"{len(cities)} 个设区市"
    county = [v for v in uniq
              if v not in city_dict and v.endswith(("县", "区", "市"))]
    if county and len(county) >= 0.6 * len(uniq):
        return f"{len(county)} 个县市/市区"
    return ""


def _time_span(cards: list[dict]) -> str:
    years: list[int] = []
    relative: list[str] = []
    for c in cards:
        p = c.get("period") or {}
        for key in ("start", "end"):
            if p.get(key):
                y = str(p[key])[:4]
                if y.isdigit():
                    years.append(int(y))
        if p.get("relative"):
            relative.append(p.get("label") or p["relative"])
        for i, col in enumerate(c["columns"]):
            if col.get("type") != "time":
                continue
            for r in c["rows"]:
                y = str(r[i])[:4] if i < len(r) and r[i] is not None else ""
                if y.isdigit():
                    years.append(int(y))
    if relative:                       # 相对周期（近30天）优先于日期列的年份
        return relative[0]
    if years:
        lo, hi = min(years), max(years)
        return f"{lo}–{hi} 年" if lo != hi else f"{hi} 年"
    return ""


def _kpi_highlights(cards: list[dict]) -> list[str]:
    out = []
    for c in cards:
        if c["kind"] != "kpi" or not c["rows"]:
            continue
        cols = c["columns"]
        idx = max((i for i, col in enumerate(cols) if col.get("type") == "number"),
                  default=len(cols) - 1)
        name, unit = _split_title(c["title"])
        val = _fmt_num(c["rows"][0][idx], cols[idx].get("format"))
        out.append(f"{name} {_unit_text(val, unit)}")
        if len(out) >= 3:
            break
    return out


def _leader(cards: list[dict]) -> str:
    """找一个降序排行卡片，给出「谁最高」；优先条形图（TOP 排行）。"""
    skip = ("分布", "占比", "结构", "趋势", "构成", "平均")
    order = {"bar_h": 0, "bar": 1, "pie": 2}
    cands = [c for c in cards
             if c["kind"] in order and len(c["rows"]) >= 2
             and not any(k in c["title"] for k in skip)]
    cands.sort(key=lambda c: order[c["kind"]])
    for c in cands:
        cols = c["columns"]
        if not cols or cols[0].get("type") == "number":
            continue
        idx = next((i for i, col in enumerate(cols)
                    if col.get("type") == "number"), None)
        if idx is None:
            continue
        try:
            if float(c["rows"][0][idx]) < float(c["rows"][1][idx]):
                continue                      # 不是降序，跳过
        except (TypeError, ValueError):
            continue
        title, unit = _split_title(c["title"])
        val = _fmt_num(c["rows"][0][idx], cols[idx].get("format"))
        return f"{c['rows'][0][0]}在「{title}」中最高（{_unit_text(val, unit)}）"
    return ""


# ---------------------------------------------------------------- 主入口
SYSTEM_PROMPT = (
    "你是资深数据分析师，负责给数据看板写简介。"
    "要求：只使用给定的事实和数字，不编造；结论先行，语气自然、有分析感；"
    "2–3 句话，120–200 字；不要标题、不要分点、不要 markdown、不要引号。"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_llm(text: str) -> str:
    """去掉模型爱加的前缀、引号、markdown 符号，压成一段。"""
    s = (text or "").strip().strip('"“”\'')
    for p in ("看板简介：", "看板简介:", "简介：", "简介:"):
        if s.startswith(p):
            s = s[len(p):].strip()
            break
    s = re.sub(r"\s*\n+\s*", "", s)
    s = re.sub(r"[#*`]+", "", s)
    return s[:300].strip()


def build(doc: dict, model, user: dict, run_spec) -> dict:
    """返回 {"text": 规则摘要, "facts": 结构化事实}。"""
    cards = collect(doc, model, user, run_spec)
    if not cards:
        n = len(doc.get("cards") or [])
        facts = {"model": model.title, "metrics": [], "scope": "", "span": "",
                 "cards": n, "kinds": {}, "kpis": [], "leader": "", "drill": False}
        return {"text": f"共 {n} 张卡片，展示 {model.title} 数据。", "facts": facts}

    # 1）范围：指标 + 地区 + 时间
    metrics = _metric_labels(cards, model)
    scope = _region_scope(cards, model)
    span = _time_span(cards)
    bits = []
    if metrics:
        if len(metrics) <= 4:
            bits.append("覆盖 " + "、".join(metrics) + f" 共 {len(metrics)} 项指标")
        else:
            bits.append("覆盖 " + "、".join(metrics[:3]) + f" 等 {len(metrics)} 项指标")
    if scope:
        bits.append(f"涉及 {scope}")
    if span:
        if "–" in span:
            bits.append(f"时间跨度 {span}")
        elif span.endswith("年"):
            bits.append(f"数据为 {span}")
        else:
            bits.append(f"数据范围为{span}")
    s1 = ("，".join(bits) + "。") if bits else ""

    # 2）结构：卡片构成
    kinds = Counter(c["kind"] for c in cards)
    kpi_n = kinds.pop("kpi", 0)
    map_n = kinds.pop("map", 0)
    scatter_n = kinds.pop("scatter", 0)
    chart_n = sum(kinds.values())
    drill = any((c.get("viz") or {}).get("drill_metrics") or (c.get("viz") or {}).get("drill_metric")
                for c in cards if c["kind"] == "map")
    parts = []
    if kpi_n:
        parts.append(f"{kpi_n} 张指标卡")
    if chart_n:
        parts.append(f"{chart_n} 张图表")
    if map_n:
        parts.append(f"{map_n} 张地图" + ("（可下钻县市）" if drill else ""))
    if scatter_n:
        parts.append(f"{scatter_n} 张散点图")
    s2 = f"共 {len(cards)} 张卡片：" + "、".join(parts) + "。" if parts else ""

    # 3）亮点：KPI 数值 + 排行首项
    highlights = _kpi_highlights(cards)
    leader = _leader(cards)
    tail = []
    if highlights:
        tail.append("关键指标：" + "、".join(highlights))
    if leader:
        tail.append(leader)
    s3 = "；".join(tail) + "。" if tail else ""

    facts = {
        "model": model.title,
        "metrics": metrics,
        "scope": scope,
        "span": span,
        "cards": len(cards),
        "kinds": {"kpi": kpi_n, "chart": chart_n, "map": map_n, "scatter": scatter_n},
        "kpis": highlights,
        "leader": leader,
        "drill": drill,
    }
    return {"text": "".join(x for x in (s1, s2, s3) if x), "facts": facts}


def generate(doc: dict, model, user: dict, run_spec) -> str:
    """根据看板卡片生成 2–3 句简介（规则引擎）。"""
    return build(doc, model, user, run_spec)["text"]


def meta(facts: dict, source: str = "rule") -> dict:
    """给简介条渲染 chips 用的结构化摘要，随 doc.description_meta 一起存。"""
    return {
        "metrics": len(facts.get("metrics") or []),
        "scope": facts.get("scope") or "",
        "span": facts.get("span") or "",
        "cards": facts.get("cards") or 0,
        "source": source,
        "generated_at": _now_iso(),
    }


def polish(text: str, facts: dict, title: str, chat) -> str:
    """把规则摘要交给 LLM 改写成更自然的分析口吻。

    chat 由调用方注入（main 传 llm.chat，测试可以传假函数），
    失败由调用方兜底回退到 text。
    """
    f = facts or {}
    kinds = f.get("kinds") or {}
    user = (
        f"看板标题：{title}\n"
        f"数据摘要（事实）：{text}\n\n"
        "结构化事实：\n"
        f"- 数据源：{f.get('model') or '—'}\n"
        f"- 指标：{'、'.join((f.get('metrics') or [])[:8]) or '—'}\n"
        f"- 覆盖：{f.get('scope') or '—'}\n"
        f"- 时间：{f.get('span') or '—'}\n"
        f"- 卡片：{f.get('cards') or 0} 张（指标卡 {kinds.get('kpi', 0)}、"
        f"图表 {kinds.get('chart', 0)}、地图 {kinds.get('map', 0)}、"
        f"散点 {kinds.get('scatter', 0)}）\n"
        f"- 关键数字：{'、'.join(f.get('kpis') or []) or '—'}\n"
        f"- 排名最高：{f.get('leader') or '—'}\n\n"
        "请据此写一段看板简介。"
    )
    out = chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ])
    return _clean_llm(out)
