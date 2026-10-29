"""规则引擎：自然语言 → QuerySpec。

定位：
  1) 没有配置 LLM 时的兜底通道（保证系统可用）
  2) 评测基线（LLM 至少要打得过规则）
  3) 给 LLM 提供候选集（指标 / 维度 / 维度值）

注意：这里只做「规划」，不生成 SQL。
"""
from __future__ import annotations

import re
from datetime import date

from .contracts import Filter, OrderBy, QuerySpec, TimeRange
from .semantic import SemanticModel

REL_TIME = [
    ("上个月", "last_month"), ("上月", "last_month"),
    ("这个月", "this_month"), ("本月", "this_month"),
    ("上个星期", "last_week"), ("上个周", "last_week"), ("上周", "last_week"),
    ("这个星期", "this_week"), ("本周", "this_week"), ("这周", "this_week"),
    ("最近7天", "last_7d"), ("近7天", "last_7d"), ("近一周", "last_7d"), ("最近一周", "last_7d"),
    ("最近30天", "last_30d"), ("近30天", "last_30d"), ("近一个月", "last_30d"),
    ("昨天", "yesterday"), ("今天", "today"),
    ("上个季度", "last_quarter"), ("上季度", "last_quarter"), ("本季度", "this_quarter"),
    ("今年", "this_year"), ("去年", "last_year"),
]

TREND_WORDS = ["趋势", "走势", "变化", "每天", "每日", "按天", "按月", "每月", "曲线"]
RANK_WORDS = ["排名", "排行", "最高", "最多", "最大", "top", "前"]
COMPARE_WORDS = ["对比", "相比", "环比", "同比"]
SHARE_WORDS = ["占比", "比例", "构成"]
REFUSE_WORDS = ["竞品", "手机号", "身份证", "密码", "别的公司"]


class PlanError(Exception):
    pass


def plan(question: str, model: SemanticModel, last_spec: dict | None = None,
         today: date | None = None, latest_date: str | None = None) -> dict:
    today = today or date.today()
    text = (question or "").strip()
    if not text:
        return _clarify("请描述你想看什么数据，例如「上个月各渠道的 GMV」。")

    for w in REFUSE_WORDS:
        if w in text:
            return {"kind": "refuse",
                    "reason": f"「{w}」涉及超出当前数据范围或敏感信息，无法回答。"}

    metrics, dims = _link(text, model)
    # 布尔维度（如「新客」）是过滤条件，不是分组维度
    bool_dims = [d for d in dims if d.get("type") == "boolean"]
    dims = [d for d in dims if d.get("type") != "boolean"]
    values = model.search_values(text)
    # 已被具体值限定（如「华东地区」「直播渠道」）的维度只做过滤，不做分组
    value_dims = {dim for dim, _ in values}
    dims = [d for d in dims if d["name"] not in value_dims]
    time_range = _detect_time(text, today)
    trend = any(w in text for w in TREND_WORDS)
    ranking = any(w in text for w in RANK_WORDS)
    compare = any(w in text for w in COMPARE_WORDS)
    share = any(w in text for w in SHARE_WORDS)

    # ---------- 城市语境：提到城市 / 苏南苏中苏北 / 各市 → 用城市级指标 ----------
    city_values = [v for dim, v in values if dim == "region" and v != "江苏省"]
    group_values = [v for dim, v in values if dim == "region_group"]
    city_words = ("各市", "分市", "各城市", "城市", "地级市", "市域")
    city_ctx = bool(city_values or group_values) or any(w in text for w in city_words)
    if city_ctx and metrics:
        swapped = []
        for m in metrics:
            city_variant = model.metrics.get(m["name"] + "_city")
            if city_variant and city_variant["name"] not in {x["name"] for x in swapped}:
                swapped.append(city_variant)
            elif m not in swapped:
                swapped.append(m)
        metrics = swapped

    # ---------- 多轮修正：没有指标，但有上一轮 ----------
    if not metrics and last_spec:
        return _patch(text, last_spec, model, dims, time_range, trend)

    if not metrics:
        options = [{"label": m.get("title", n), "value": n}
                   for n, m in list(model.metrics.items())[:6]
                   if m.get("type") != "window"]
        return _clarify("没听出要查哪个指标，你是指下面这些吗？", options)

    # ---------- 组装 ----------
    spec_metrics = [m["name"] for m in metrics]

    # 趋势词 → 时间维度
    spec_dims: list[str] = []
    time_dim = model.time_dimension()
    if trend and time_dim:
        spec_dims.append(time_dim["name"])

    for d in dims:
        if d["name"] not in spec_dims and d["name"] != (time_dim or {}).get("name"):
            spec_dims.append(d["name"])

    # 同一维度提到多个值（如「苏州和南京」）→ 自动按该维度分组
    from collections import Counter

    for dim_name, n in Counter(dim for dim, _ in values).items():
        if n >= 2 and dim_name not in spec_dims and dim_name in model.dimensions:
            spec_dims.append(dim_name)

    # 「各市 / 各城市」但没指定分组 → 自动按地区拆
    if city_ctx and not spec_dims and any(w in text for w in ("各市", "分市", "各城市", "各城市", "城市")):
        if "region" in model.dimensions:
            spec_dims.append("region")

    # 年度模型：跨多年的区间自动按年展示趋势
    auto_trend = False
    if (time_dim and not spec_dims and time_range is not None
            and getattr(model, "default_grain", "day") == "year"
            and _span_years(time_range) >= 2):
        spec_dims.append(time_dim["name"])
        auto_trend = True

    # 粒度
    grain = model.default_grain
    if trend and ("按月" in text or "每月" in text):
        grain = "month"
    elif trend and ("按周" in text or "每周" in text):
        grain = "week"
    elif trend and ("按年" in text or "每年" in text):
        grain = "year"

    filters: list[Filter] = []
    for d in bool_dims:
        filters.append(Filter(type="dimension", dimension=d["name"], op="eq", value=True))

    for dim_name, val in values:
        d = model.dimension(dim_name)
        if d.get("type") == "boolean":
            filters.append(Filter(type="dimension", dimension=dim_name, op="eq",
                                  value=val in ("1", "true", "是")))
        else:
            filters.append(Filter(type="dimension", dimension=dim_name, op="in", values=[val]))

    notes: list[str] = []
    if auto_trend:
        notes.append("时间跨度超过一年，按年度趋势展示")
    if time_dim:
        if time_range is None and not trend:
            mode = getattr(model, "default_time_range", "last_30d")
            if mode == "latest" and latest_date:
                y = str(latest_date)[:4]
                time_range = TimeRange(type="absolute", start=f"{y}-01-01", end=f"{y}-12-31")
                notes.append(f"未指定时间，默认取最新年份 {y}")
            elif mode == "none":
                notes.append("未指定时间范围，统计全部年份")
            else:
                time_range = TimeRange(type="relative", value="last_30d")
                notes.append("未指定时间范围，默认统计近 30 天")
        if time_range is not None:
            filters.append(Filter(type="time", dimension=time_dim["name"],
                                  range=time_range, grain=grain))

    order_by: list[OrderBy] = []
    limit = 500
    if ranking:
        m = re.search(r"(?:top|前)\s*(\d+)", text, re.IGNORECASE)
        limit = int(m.group(1)) if m else 10
        order_by = [OrderBy(by=spec_metrics[0], dir="desc")]

    viz_hint = "pie" if (share and not trend) else None

    spec = QuerySpec(
        metrics=spec_metrics,
        dimensions=spec_dims,
        filters=filters,
        order_by=order_by,
        limit=limit,
        viz_hint=viz_hint,
    )
    if compare:
        notes.append("当前版本暂不支持环比/同比对比，已返回所选区间数据")

    return {"kind": "query", "spec": spec, "notes": notes,
            "matched": {"metrics": spec_metrics,
                        "dimensions": spec_dims,
                        "values": values}}


def _patch(text: str, last_spec: dict, model: SemanticModel,
           dims: list[dict], time_range: TimeRange | None, trend: bool) -> dict:
    """修正轮：基于上一轮 spec 做增量修改。"""
    spec = QuerySpec.model_validate(last_spec)
    time_dim = model.time_dimension()
    changed = []

    if trend and time_dim:
        spec.dimensions = [time_dim["name"]]
        for f in spec.filters:
            if f.type == "time":
                f.grain = getattr(model, "default_grain", "day")
        spec.order_by = []
        changed.append("改为趋势")

    new_dims = [d["name"] for d in dims if d["name"] != (time_dim or {}).get("name")]
    if new_dims and not trend:
        spec.dimensions = new_dims
        spec.order_by = []
        changed.append("按 " + "、".join(new_dims) + " 拆分")

    if time_range and time_dim:
        for f in spec.filters:
            if f.type == "time":
                f.range = time_range
                changed.append("更新了时间范围")

    if not changed:
        return _clarify("没听懂这次要改什么，可以试试「按地区拆一下」「换成趋势」")

    return {"kind": "query", "spec": spec, "notes": changed,
            "matched": {"metrics": spec.metrics, "dimensions": spec.dimensions, "values": []}}


# ---------------------------------------------------------------------------
def _detect_time(text: str, today: date) -> TimeRange | None:
    # 年份区间：2000-2020 / 2000年到2020年
    m = re.search(r"((?:19|20)\d{2})\s*年?\s*(?:-|—|~|～|至|到)\s*((?:19|20)\d{2})\s*年?", text)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if y1 > y2:
            y1, y2 = y2, y1
        return TimeRange(type="absolute", start=f"{y1}-01-01", end=f"{y2}-12-31")

    # 近 N 年（支持「近十年」这类中文数字）
    m = re.search(r"近\s*([0-9]+|[一二两三四五六七八九十]+)\s*年", text)
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else _cn_number(raw)
        n = max(1, n)
        return TimeRange(type="absolute", start=f"{today.year - n + 1}-01-01",
                         end=f"{today.year}-12-31")

    for word, value in REL_TIME:
        if word in text:
            return TimeRange(type="relative", value=value)

    # 单个年份：2020年
    m = re.search(r"((?:19|20)\d{2})\s*年", text)
    if m:
        y = int(m.group(1))
        return TimeRange(type="absolute", start=f"{y}-01-01", end=f"{y}-12-31")

    m = re.search(r"(\d{1,2})\s*月", text)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            year = today.year
            if "去年" in text:
                year -= 1
            elif month > today.month:
                year -= 1          # 未来月份 → 视为去年
            start = date(year, month, 1)
            end = date(year + (month == 12), month % 12 + 1, 1)
            end = date.fromordinal(end.toordinal() - 1)
            return TimeRange(type="absolute", start=start.isoformat(), end=end.isoformat())
    return None


def _link(text: str, model: SemanticModel) -> tuple[list[dict], list[dict]]:
    """最长匹配优先的实体链接。

    避免「人均地区生产总值」里的「地区」被误识别成维度，
    也避免「人均GDP」同时命中 GDP 和人均 GDP。
    """
    candidates: list[tuple[int, int, str, dict]] = []
    for kind, items in (("metric", model.metrics.values()),
                        ("dimension", model.dimensions.values())):
        for item in items:
            aliases = [item["name"], item.get("title", "")] + list(item.get("synonyms") or [])
            for alias in aliases:
                if not alias:
                    continue
                start = text.find(alias)
                while start != -1:
                    candidates.append((len(alias), start, kind, item))
                    start = text.find(alias, start + 1)
    candidates.sort(key=lambda c: (-c[0], c[1]))

    claimed: list[tuple[int, int]] = []
    metrics: list[dict] = []
    dims: list[dict] = []

    def overlaps(s: int, e: int) -> bool:
        return any(s < ce and cs < e for cs, ce in claimed)

    for length, start, kind, item in candidates:
        end = start + length
        if overlaps(start, end):
            continue
        claimed.append((start, end))
        target = metrics if kind == "metric" else dims
        if all(x["name"] != item["name"] for x in target):
            target.append(item)
    return metrics, dims


CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int:
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = CN_DIGITS.get(left, 1) if left else 1
        ones = CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return CN_DIGITS.get(text, 1)


def _span_years(r: TimeRange | None) -> int:
    if r and r.type == "absolute" and r.start and r.end:
        try:
            return int(r.end[:4]) - int(r.start[:4]) + 1
        except ValueError:
            return 0
    return 0


def _clarify(question: str, options: list[dict] | None = None) -> dict:
    return {"kind": "clarification", "question": question, "options": options or []}
