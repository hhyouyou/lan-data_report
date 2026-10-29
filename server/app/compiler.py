"""QuerySpec → SQL 编译器。

设计约定（见 docs/01 决策一）：
  - LLM 不写 SQL，只产出 QuerySpec
  - 所有查询走这里，保证 AI 与看板数字一致
  - 权限在编译期注入
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .contracts import Filter, QuerySpec, TimeRange
from .semantic import SemanticModel, SemanticError

EXPR_REF = re.compile(r"\{(\w+)\}")

TIME_BUCKETS = {
    "day": "strftime('%Y-%m-%d', {col})",
    "week": "strftime('%Y-W%W', {col})",
    "month": "strftime('%Y-%m', {col})",
    "quarter": "strftime('%Y', {col}) || '-Q' || CAST((CAST(strftime('%m', {col}) AS INTEGER) + 2) / 3 AS TEXT)",
    "year": "strftime('%Y', {col})",
}


class CompileError(Exception):
    pass


# ---------------------------------------------------------------------------
# 时间解析：相对时间在编译时才解析（这样看板订阅每天数字会变）
# ---------------------------------------------------------------------------
def resolve_time_range(r: TimeRange, today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    if r.type == "absolute":
        if not r.start or not r.end:
            raise CompileError("absolute 时间范围需要 start 和 end")
        end_ex = date.fromisoformat(r.end) + timedelta(days=1)
        return r.start, end_ex.isoformat()

    v = (r.value or "").strip()
    if v == "today":
        return today.isoformat(), (today + timedelta(days=1)).isoformat()
    if v == "yesterday":
        y = today - timedelta(days=1)
        return y.isoformat(), today.isoformat()
    if v in ("last_7d", "近7天"):
        return (today - timedelta(days=6)).isoformat(), (today + timedelta(days=1)).isoformat()
    if v in ("last_14d",):
        return (today - timedelta(days=13)).isoformat(), (today + timedelta(days=1)).isoformat()
    if v in ("last_30d",):
        return (today - timedelta(days=29)).isoformat(), (today + timedelta(days=1)).isoformat()
    if v == "this_week":
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), (start + timedelta(days=7)).isoformat()
    if v == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start.isoformat(), (start + timedelta(days=7)).isoformat()
    if v == "this_month":
        start = today.replace(day=1)
        return start.isoformat(), _add_months(start, 1).isoformat()
    if v == "last_month":
        start = _add_months(today.replace(day=1), -1)
        return start.isoformat(), today.replace(day=1).isoformat()
    if v == "this_quarter":
        q_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        return q_start.isoformat(), _add_months(q_start, 3).isoformat()
    if v == "last_quarter":
        q_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        prev = _add_months(q_start, -3)
        return prev.isoformat(), q_start.isoformat()
    if v == "this_year":
        return date(today.year, 1, 1).isoformat(), date(today.year + 1, 1, 1).isoformat()
    if v == "last_year":
        return date(today.year - 1, 1, 1).isoformat(), date(today.year, 1, 1).isoformat()
    raise CompileError(f"不支持的相对时间: {v}")


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


# ---------------------------------------------------------------------------
# 指标聚合表达式
# ---------------------------------------------------------------------------
def agg_expr(metric: dict) -> str:
    col = metric["column"]
    conds = metric.get("filters") or []
    cond = " AND ".join(conds)
    agg = metric.get("agg", "sum")

    if agg == "count":
        return f"COUNT(CASE WHEN {cond} THEN {col} END)" if cond else f"COUNT({col})"
    if agg == "count_distinct":
        return (f"COUNT(DISTINCT CASE WHEN {cond} THEN {col} END)"
                if cond else f"COUNT(DISTINCT {col})")
    if not cond:
        return f"{agg.upper()}({col})"
    # 不写 ELSE 0：没有匹配行的分组应返回 NULL（无数据 ≠ 0）
    return f"{agg.upper()}(CASE WHEN {cond} THEN {col} END)"


def expand_base_metrics(names: list[str], model: SemanticModel) -> list[str]:
    """把请求中的指标展开成需要聚合的原子/窗口指标（去重、保持顺序）。"""
    out: list[str] = []

    def walk(n: str) -> None:
        m = model.metric(n)
        if m.get("type") == "derived":
            for ref in EXPR_REF.findall(m.get("expr", "")):
                walk(ref)
        elif n not in out:
            out.append(n)

    for n in names:
        walk(n)
    return out


def render_derived(expr: str) -> str:
    """{gmv} / {order_cnt} → CAST(t.gmv AS REAL) / NULLIF(t.order_cnt, 0)"""
    if "/" in expr:
        left, right = expr.split("/", 1)
        left = _render_operand(left)
        right = _render_operand(right)
        return f"CAST({left} AS REAL) / NULLIF({right}, 0)"
    return _render_operand(expr)


def _render_operand(s: str) -> str:
    return EXPR_REF.sub(lambda m: f"t.{m.group(1)}", s.strip())


# ---------------------------------------------------------------------------
# 主编译
# ---------------------------------------------------------------------------
def compile_spec(spec: QuerySpec, model: SemanticModel, user: dict | None = None) -> dict:
    if not spec.metrics:
        raise CompileError("metrics 不能为空")
    try:
        for m in spec.metrics:
            model.metric(m)
        for d in spec.dimensions:
            model.dimension(d)
    except SemanticError as e:
        raise CompileError(str(e)) from e

    user = user or {}
    params: list = []

    # --- 时间过滤 ---
    time_filter = next((f for f in spec.filters if f.type == "time" and f.range), None)
    grain = (time_filter.grain if time_filter and time_filter.grain else None) \
        or getattr(model, "default_grain", "day")
    if grain not in TIME_BUCKETS:
        raise CompileError(f"不支持的粒度: {grain}")

    where: list[str] = []

    # --- 维度/值过滤（合并同一维度的 IN，避免 region IN (?) AND region IN (?)） ---
    merged_in: dict[str, list] = {}
    for f in spec.filters:
        if f.type != "dimension" or f.op != "in":
            continue
        vals = list(f.values or [])
        if not vals and f.value is not None:
            vals = list(f.value) if isinstance(f.value, (list, tuple)) else [f.value]
        if vals:
            merged_in.setdefault(f.dimension, []).extend(vals)

    for f in spec.filters:
        if f.type != "dimension":
            continue
        d = model.dimension(f.dimension)
        col = d["column"]
        if f.op == "in":
            continue                      # 统一在下面合并处理
        elif f.op in ("eq", "="):
            where.append(f"{col} = ?")
            params.append(f.value)
        elif f.op in ("gt", ">"):
            where.append(f"{col} > ?")
            params.append(f.value)
        elif f.op in ("lt", "<"):
            where.append(f"{col} < ?")
            params.append(f.value)
        else:
            raise CompileError(f"不支持的过滤操作: {f.op}")

    for dim_name, vals in merged_in.items():
        col = model.dimension(dim_name)["column"]
        where.append(f"{col} IN ({', '.join('?' * len(vals))})")
        params.extend(vals)

    # --- 时间范围（在 WHERE 中，作用于时间维度） ---
    if time_filter:
        tdim = model.dimension(time_filter.dimension)
        start, end = resolve_time_range(time_filter.range)
        where.append(f"{tdim['column']} >= ? AND {tdim['column']} < ?")
        params.extend([start, end])

    # --- 行级权限 ---
    rls_sql, rls_params = _render_rls(model, user)
    if rls_sql:
        where.append(rls_sql)
        params.extend(rls_params)

    # --- SELECT 维度 ---
    dim_selects: list[str] = []
    for dname in spec.dimensions:
        d = model.dimension(dname)
        if d["type"] == "time":
            expr = TIME_BUCKETS[grain].format(col=d["column"])
        else:
            expr = d["column"]
        dim_selects.append(f"{expr} AS {dname}")

    # --- SELECT 指标（内层：原子聚合） ---
    base = expand_base_metrics(spec.metrics, model)
    base_selects = [f"{agg_expr(model.metric(m))} AS {m}" for m in base]

    from_clause = _from_clause(model)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    group_by = ""
    if dim_selects:
        group_by = " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dim_selects)))
    # 过滤掉「该分组下没有任何指标值」的行（例如城市数据只覆盖 2018–2024）
    having = ""
    if dim_selects and base_selects:
        cond = " OR ".join(f"{m} IS NOT NULL" for m in base)
        having = f" HAVING ({cond})"

    inner = f"SELECT {', '.join(dim_selects + base_selects)}{from_clause}{where_sql}{group_by}{having}"

    # --- 是否需要外层（派生指标） ---
    has_derived = any(model.metric(m).get("type") == "derived" for m in spec.metrics)
    if has_derived:
        outer_selects: list[str] = [f"t.{d}" for d in spec.dimensions]
        for m in spec.metrics:
            md = model.metric(m)
            if md.get("type") == "derived":
                outer_selects.append(f"{render_derived(md.get('expr', ''))} AS {m}")
            else:
                outer_selects.append(f"t.{m}")
        sql = f"SELECT {', '.join(outer_selects)} FROM ({inner}) t"
    else:
        sql = inner

    # --- ORDER BY ---
    order_sql = _order_clause(spec, model)
    if order_sql:
        sql += order_sql

    limit = max(1, min(int(spec.limit or 500), 5000))
    sql += f" LIMIT {limit}"

    columns = [model.column_meta(d) for d in spec.dimensions] + \
              [model.column_meta(m) for m in spec.metrics]

    return {
        "sql": sql,
        "params": params,
        "columns": columns,
        "grain": grain,
        "explain": {
            "metrics": [model.metric_definition(m) for m in spec.metrics],
            "semantic_model": f"{model.name}@{model.version}",
        },
    }


def _from_clause(model: SemanticModel) -> str:
    parts = [f"{model.fact_table} {model.fact_alias}"]
    for j in model.joins:
        parts.append(f"{j.get('type', 'left').upper()} JOIN {j['table']} {j['alias']} ON {j['on']}")
    return " FROM " + " ".join(parts)


def _order_clause(spec: QuerySpec, model: SemanticModel) -> str:
    if spec.order_by:
        bits = []
        for ob in spec.order_by:
            if ob.by not in spec.metrics and ob.by not in spec.dimensions:
                continue
            bits.append(f"{ob.by} {ob.dir.upper()}")
        return (" ORDER BY " + ", ".join(bits)) if bits else ""

    # 默认：时间维度升序；否则第一个指标降序（排名）
    if spec.dimensions:
        first = model.dimension(spec.dimensions[0])
        if first.get("type") == "time":
            return f" ORDER BY {spec.dimensions[0]} ASC"
        return f" ORDER BY {spec.metrics[0]} DESC"
    return ""


def _render_rls(model: SemanticModel, user: dict) -> tuple[str, list]:
    """把 {{ user.regions }} 之类替换成参数化条件。"""
    conds: list[str] = []
    params: list = []
    for rls in model.rls:
        rule = str(rls.get("rule", ""))
        m = re.search(r"\{\{\s*user\.(\w+)\s*\}\}", rule)
        if not m:
            continue
        var = m.group(1)
        val = user.get(var)
        if not val:
            continue
        if isinstance(val, str):
            val = [val]
        placeholders = ", ".join("?" * len(val))
        # 把 {{ user.x }} 整段替换成占位符，兼容 IN (...) 写法
        cond = re.sub(r"\{\{\s*user\.\w+\s*\}\}", placeholders, rule)
        conds.append(f"({cond})")
        params.extend(val)
    return " AND ".join(conds), params
