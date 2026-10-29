"""图表推断：规则优先，LLM 的 viz_hint 只作参考。"""
from __future__ import annotations

from .contracts import QuerySpec
from .semantic import SemanticModel


def infer(spec: QuerySpec, model: SemanticModel, row_count: int) -> str:
    if spec.viz_hint in {"bar", "bar_h", "line", "pie", "kpi", "table", "map", "scatter"}:
        if spec.viz_hint == "pie" and row_count > 12:
            return "bar"
        return spec.viz_hint

    if not spec.dimensions:
        return "kpi" if row_count <= 1 else "table"

    dims = spec.dimensions
    if len(dims) == 1:
        d = model.dimension(dims[0])
        if d.get("type") == "time":
            return "line"
        if row_count <= 8:
            return "bar"
        return "bar_h"

    if len(dims) == 2:
        # 双维度（时间 × 类别）需要 pivot，v0.1 先以表格呈现
        return "table"

    return "table"
