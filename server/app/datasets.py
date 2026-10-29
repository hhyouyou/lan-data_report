"""数据接入与数据集管理。

P1 目标：让平台能「接数据」，并且用规则引擎把脏数据变成可问数的语义模型草稿。

流程：
  上传 Excel/CSV → 清洗列名/推断类型 → 落地到仓库 SQLite
    → 分析字段 → 生成语义模型草稿（YAML）→ 人工确认 → 激活
"""
from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime

import pandas as pd
import yaml
from sqlalchemy import create_engine

from . import config, standards
from .semantic import validate

SAFE = re.compile(r"[^a-z0-9_]+")
VALID_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

# 中文表头 → 英文字段名的启发式映射（LLM 未接入时的基线）
CN_HINTS = [
    ("成交额", "amt"), ("销售额", "amt"), ("金额", "amt"), ("营收", "amt"), ("价格", "price"),
    ("数量", "cnt"), ("件数", "cnt"), ("销量", "qty"),
    ("订单", "order"), ("用户", "user"), ("客户", "customer"),
    ("商品", "product"), ("产品", "product"),
    ("日期", "date"), ("时间", "time"), ("月份", "month"),
    ("名称", "name"), ("省份", "province"), ("城市", "city"),
    ("地区", "region"), ("区域", "region"), ("大区", "region"),
    ("渠道", "channel"), ("状态", "status"), ("类型", "type"), ("编号", "id"),
    ("退款", "refund"), ("成本", "cost"), ("利润", "profit"),
    ("是否", "is"), ("新客", "new_user"), ("老客", "old_user"),
    ("门店", "store"), ("部门", "dept"), ("员工", "employee"),
]

RATE_HINTS = ("rate", "ratio", "pct", "percent", "率")
AMOUNT_HINTS = ("amt", "amount", "price", "revenue", "gmv", "cost", "profit", "金额", "额")
COUNT_HINTS = ("cnt", "count", "qty", "num", "数量", "件数")

META_TABLE = "_ldr_column_meta"
_BOOL_VALUES = {"是", "否", "true", "false", "y", "n", "yes", "no", "1", "0"}


def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS {META_TABLE}(
        table_name TEXT, column_name TEXT, source_name TEXT, kind TEXT,
        PRIMARY KEY(table_name, column_name)
    );
    """)


def _read_meta(table: str) -> dict[str, dict]:
    with _connect() as conn:
        _ensure_meta(conn)
        rows = conn.execute(
            f"SELECT column_name, source_name, kind FROM {META_TABLE} WHERE table_name=?",
            (table,),
        ).fetchall()
    return {r[0]: {"source": r[1], "kind": r[2]} for r in rows}


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    return sqlite3.connect(config.WAREHOUSE)


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def list_tables() -> list[dict]:
    with _connect() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        out = []
        for n in names:
            if n == META_TABLE:
                continue
            try:
                rows = conn.execute(f"SELECT COUNT(*) FROM {_quote(n)}").fetchone()[0]
                cols = conn.execute(f"PRAGMA table_info({_quote(n)})").fetchall()
            except sqlite3.Error:
                continue
            out.append({"name": n, "rows": rows, "columns": len(cols)})
    return out


def _require_table(name: str) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    if not row:
        raise KeyError(f"表不存在: {name}")


def table_detail(name: str, sample_limit: int = 20) -> dict:
    _require_table(name)
    with _connect() as conn:
        cols = conn.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
        cur = conn.execute(f"SELECT * FROM {_quote(name)} LIMIT {int(sample_limit)}")
        headers = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        # 空值率
        nulls = {}
        for c in cols:
            col = c[1]
            cnt = conn.execute(
                f"SELECT COUNT(*) FROM {_quote(name)} WHERE {_quote(col)} IS NULL"
            ).fetchone()[0]
            nulls[col] = cnt

    def _json(v):
        if v is None or isinstance(v, (int, float, str, bool)):
            return v
        return str(v)

    meta = _read_meta(name)
    return {
        "name": name,
        "rows": total,
        "columns": [
            {"name": c[1], "type": c[2], "notnull": bool(c[3]), "pk": bool(c[5]),
             "nulls": nulls.get(c[1], 0),
             "source": meta.get(c[1], {}).get("source", c[1]),
             "kind": meta.get(c[1], {}).get("kind")}
            for c in cols
        ],
        "sample": {"columns": headers, "rows": [[_json(v) for v in r] for r in rows]},
    }


# ---------------------------------------------------------------------------
# 清洗与导入
# ---------------------------------------------------------------------------
def sanitize_column(raw: str, index: int = 0) -> str:
    text = str(raw).strip()
    if VALID_IDENT.match(text.lower()):
        return text.lower()

    parts: list[str] = []
    rest = text
    for cn, en in CN_HINTS:
        if cn in rest:
            parts.append(en)
            rest = rest.replace(cn, "")

    slug = SAFE.sub("_", text.lower()).strip("_")
    if slug and slug[0].isdigit():
        slug = "c_" + slug
    if slug and VALID_IDENT.match(slug) and len(slug) > 1:
        return slug

    if parts:
        return "_".join(dict.fromkeys(parts))
    return f"c{index or 1}"


def _infer_kind(series: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(series):
        return "time"
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    return "string"


def import_file(filename: str, content: bytes, table_name: str | None = None) -> dict:
    lower = (filename or "").lower()
    try:
        if lower.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content))
        elif lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
        else:
            raise ValueError("只支持 .xlsx / .xls / .csv")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"文件解析失败: {e}") from e

    if df.empty:
        raise ValueError("文件里没有数据行")

    # 列名清洗 + 去重
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for i, raw in enumerate(df.columns, start=1):
        base = sanitize_column(raw, i)
        name, k = base, 2
        while name in used:
            name = f"{base}_{k}"
            k += 1
        used.add(name)
        mapping[str(raw)] = name
    df = df.rename(columns=mapping)

    # 日期列识别（字符串列里 90% 以上能解析成日期的，转 datetime）
    converted: list[str] = []
    for col in df.columns:
        if df[col].dtype == object:
            parsed = pd.to_datetime(df[col], errors="coerce", format="mixed")
            if parsed.notna().mean() > 0.9:
                df[col] = parsed
                converted.append(col)

    # 布尔列识别（是/否、true/false、y/n）
    boolean_cols: list[str] = []
    for col in df.columns:
        if df[col].dtype == object:
            vals = {str(v).strip().lower() for v in df[col].dropna().unique()[:10]}
            if vals and vals <= _BOOL_VALUES:
                true_set = {"是", "true", "y", "yes", "1"}
                df[col] = df[col].astype(str).str.strip().str.lower().isin(true_set)
                boolean_cols.append(col)

    # 表名
    if table_name:
        table = SAFE.sub("_", str(table_name).lower()).strip("_")
        if not table or not VALID_IDENT.match(table):
            table = "t_" + table if table else f"t_{datetime.now():%Y%m%d_%H%M%S}"
    else:
        table = f"t_{datetime.now():%Y%m%d_%H%M%S}"

    engine = create_engine(f"sqlite:///{config.WAREHOUSE.as_posix()}")
    try:
        df.to_sql(table, engine, if_exists="replace", index=False)
    finally:
        engine.dispose()

    # 记录「原表头 → 字段名 + 语义类型」，供详情页和草稿生成使用
    cols_meta = [
        (table, c, next((s for s, t in mapping.items() if t == c), c), _infer_kind(df[c]))
        for c in df.columns
    ]
    with _connect() as conn:
        _ensure_meta(conn)
        conn.execute(f"DELETE FROM {META_TABLE} WHERE table_name=?", (table,))
        conn.executemany(f"INSERT INTO {META_TABLE} VALUES (?,?,?,?)", cols_meta)
        conn.commit()

    return {
        "table": table,
        "rows": int(len(df)),
        "columns": [{"name": c, "source": s, "kind": k} for _, c, s, k in cols_meta],
        "renamed": {k: v for k, v in mapping.items() if k != v},
        "date_columns": converted,
        "boolean_columns": boolean_cols,
    }


# ---------------------------------------------------------------------------
# 语义模型草稿
# ---------------------------------------------------------------------------
def _top_values(name: str, col: str, limit: int = 30) -> list:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_quote(col)}, COUNT(*) c FROM {_quote(name)} "
            f"WHERE {_quote(col)} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}"
        ).fetchall()
    return [r[0] for r in rows]


def _metric_spec(col: str, title: str, kind: str) -> dict | None:
    lower = col.lower()
    if kind == "boolean":
        return None
    if any(h in lower for h in RATE_HINTS):
        return {"name": col, "title": title, "type": "atomic", "agg": "avg",
                "column": f"f.{col}", "format": "percent"}
    if any(h in lower for h in AMOUNT_HINTS):
        return {"name": col, "title": title, "type": "atomic", "agg": "sum",
                "column": f"f.{col}", "format": "currency"}
    if any(h in lower for h in COUNT_HINTS):
        return {"name": col, "title": title, "type": "atomic", "agg": "sum",
                "column": f"f.{col}", "format": "integer"}
    return {"name": col, "title": title, "type": "atomic", "agg": "sum",
            "column": f"f.{col}", "format": "number"}


def suggest_model(table: str) -> dict:
    """分析一张表，生成语义模型草稿（YAML 文本）+ 风险提示。"""
    detail = table_detail(table, sample_limit=50)
    warnings: list[str] = []

    with _connect() as conn:
        df = pd.read_sql_query(f"SELECT * FROM {_quote(table)} LIMIT 5000", conn)

    total = max(len(df), 1)
    meta = _read_meta(table)
    headers = {c["name"]: c.get("source", c["name"]) for c in detail["columns"]}
    dims: list[dict] = []
    metrics: list[dict] = []
    time_cols: list[str] = []

    for col in df.columns:
        kind = meta.get(col, {}).get("kind") or _infer_kind(df[col])
        title = headers.get(col, col)

        if kind == "time":
            time_cols.append(col)
            dims.append({
                "name": col, "title": title, "type": "time",
                "column": f"f.{col}",
                "granularities": ["day", "week", "month", "quarter", "year"],
                "synonyms": [title] if title != col else [],
            })
            continue

        if kind == "boolean":
            dims.append({"name": col, "title": title, "type": "boolean",
                         "column": f"f.{col}", "synonyms": [title] if title != col else []})
            continue

        if kind == "number":
            if col.lower().endswith("_id") or col.lower().endswith("id"):
                dims.append({"name": col, "title": title, "type": "string",
                             "column": f"f.{col}", "synonyms": [title] if title != col else []})
                warnings.append(f"「{title}」看起来是 ID，已作为维度而不是指标")
                continue
            m = _metric_spec(col, title, kind)
            if m:
                metrics.append(m)
            continue

        # 字符串：低基数做维度 + 值字典
        nunique = int(df[col].nunique(dropna=True))
        if nunique <= 30:
            values = _top_values(table, col)
            dims.append({
                "name": col, "title": title, "type": "string",
                "column": f"f.{col}",
                "synonyms": [title] if title != col else [],
                "dictionary": [str(v) for v in values if v is not None],
            })
        elif nunique / total < 0.95:
            dims.append({"name": col, "title": title, "type": "string",
                         "column": f"f.{col}", "synonyms": [title] if title != col else []})
            warnings.append(f"「{title}」取值有 {nunique} 种，建议确认是否适合做维度")
        else:
            warnings.append(f"「{title}」几乎是唯一值（{nunique} 种），已跳过")

    if not time_cols:
        warnings.append("没有识别到时间列 —— 趋势类问题将无法回答；建议补一列日期后重新生成")
    if not metrics:
        warnings.append("没有识别到数值列 —— 无法生成指标，请检查数据类型")

    draft_name = re.sub(r"[^a-z0-9_]", "_", table.lower()).strip("_") or table
    doc = {
        "version": 1,
        "version_label": "draft-1",
        "name": draft_name,
        "title": f"{table} 数据模型（草稿）",
        "description": "由导入数据自动生成，请人工确认指标口径后提交",
        "datasource": "local_sqlite",
        "fact_table": table,
        "fact_alias": "f",
        "joins": [],
        "dimensions": dims,
        "metrics": metrics,
        "synonyms": {},
        "rls": [],
    }
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
    lint = standards.lint("semantic", doc)

    return {
        "table": table,
        "name": draft_name,
        "yaml": text,
        "doc": doc,
        "warnings": warnings,
        "lint": lint,
        "stats": {"rows": detail["rows"], "dimensions": len(dims), "metrics": len(metrics)},
    }


def save_draft(name: str, yaml_text: str) -> dict:
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 解析失败: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError("YAML 根节点必须是 mapping")

    errors = validate(doc)
    if errors:
        raise ValueError("语义模型校验失败: " + "；".join(errors[:5]))
    lint = standards.lint("semantic", doc)
    if not lint["passed"]:
        msg = "；".join(f"{e['rule']}: {e['message']}" for e in lint["errors"][:5])
        raise ValueError(f"规范校验未通过: {msg}")

    drafts_dir = config.SEMANTIC_DIR / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    path = drafts_dir / f"{name}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return {"saved": str(path.relative_to(config.ROOT)), "name": name, "lint": lint}
