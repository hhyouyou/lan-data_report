"""核心契约：QuerySpec。LLM 的输出、编译器的输入、看板的持久化对象，都是它。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TimeRange(BaseModel):
    type: Literal["relative", "absolute"] = "relative"
    value: str | None = None          # last_month / last_7d ...
    start: str | None = None          # YYYY-MM-DD（含）
    end: str | None = None            # YYYY-MM-DD（含）


class Filter(BaseModel):
    type: Literal["dimension", "time"] = "dimension"
    dimension: str
    op: str = "eq"                    # eq | in | gt | lt
    value: Any = None
    values: list[Any] | None = None
    range: TimeRange | None = None
    grain: str | None = None


class Compare(BaseModel):
    mode: Literal["none", "previous_period", "same_period_last_year"] = "none"


class OrderBy(BaseModel):
    by: str
    dir: Literal["asc", "desc"] = "desc"


class QuerySpec(BaseModel):
    version: int = 1
    metrics: list[str]
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    compare: Compare = Field(default_factory=Compare)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int = 500
    viz_hint: str | None = None


class Column(BaseModel):
    name: str
    label: str
    type: str = "number"
    format: str | None = None


class QueryResult(BaseModel):
    columns: list[Column]
    rows: list[list[Any]]


class Explain(BaseModel):
    metrics: list[dict] = Field(default_factory=list)
    sql: str = ""
    semantic_model: str = ""
