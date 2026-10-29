"""FastAPI 入口。

P0 刻意保持简单：
  - 单进程、内存会话
  - SQLite 存在两个文件里（分析库 / 元数据库）
  - 没有登录（内部工具 v0.1，Phase A 后段再加）
"""
from __future__ import annotations

import base64
import uuid
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (builder, config, datasets, describe, engine, executor, knowledge,
               llm, sources, standards, store, templates, viz, workflow)
from .compiler import CompileError, compile_spec
from .contracts import Filter, QuerySpec, TimeRange
from .executor import ExecError
from .semantic import SemanticError, load_model, load_model_file

app = FastAPI(title="Lan Data Report", version="0.2.0")


@app.middleware("http")
async def no_store_frontend(request, call_next):
    """内部工具：HTML/JS/CSS 一律不缓存，避免改动后用户还看到旧前端。"""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

_model = None
_active_name = config.DEFAULT_MODEL
_sessions: dict[str, dict] = {}


def _find_model_path(name: str):
    for p in (config.SEMANTIC_DIR / f"{name}.yaml",
              config.SEMANTIC_DIR / "drafts" / f"{name}.yaml"):
        if p.exists():
            return p
    raise SemanticError(f"语义模型不存在: {name}")


def get_model():
    global _model
    if _model is None:
        _model = load_model_file(_find_model_path(_active_name))
    return _model


def activate_model(name: str):
    global _model, _active_name
    model = load_model_file(_find_model_path(name))
    _model = model
    _active_name = name
    return model


_model_cache: dict[str, object] = {}


def model_by_name(name: str | None):
    """按名字取模型（带缓存），供看板绑定自己的语义模型。"""
    if not name or name == _active_name:
        return get_model()
    if name in _model_cache:
        return _model_cache[name]
    try:
        m = load_model_file(_find_model_path(name))
    except SemanticError:
        return get_model()
    _model_cache[name] = m
    return m


def _apply_published(kind: str, ref: str, snapshot: dict) -> None:
    """审批通过后，把快照写入线上版本。"""
    if kind == "dashboard":
        store.save_dashboard({**snapshot, "id": ref, "status": "published"})


workflow.set_apply(_apply_published)


def _user(x_user_regions: str | None) -> dict:
    if not x_user_regions:
        return {}
    return {"regions": [r.strip() for r in x_user_regions.split(",") if r.strip()]}


def _latest_time(model) -> str | None:
    """模型时间维度的最新值，用于「默认取最新年份」。"""
    td = model.time_dimension()
    if not td:
        return None
    try:
        res = executor.execute(
            f"SELECT MAX({td['column']}) FROM {model.fact_table} {model.fact_alias}",
            use_cache=True, source=getattr(model, "source", None),
        )
        return res["rows"][0][0]
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- schemas
class AskIn(BaseModel):
    question: str
    session_id: str | None = None


class RunSpecIn(BaseModel):
    spec: dict
    model: str | None = None


class PinIn(BaseModel):
    dashboard_id: str | None = None
    title: str | None = None


class DashboardIn(BaseModel):
    title: str
    description: str = ""


class DashboardPatch(BaseModel):
    doc: dict


class DescribeIn(BaseModel):
    apply: bool = False
    text: str | None = None
    polish: bool = True          # 配置了 LLM 时是否润色
    source: str | None = None    # 前端回传预览来源：rule / llm，用于记录 provenance


class LintIn(BaseModel):
    kind: str
    doc: dict


class CommentIn(BaseModel):
    comment: str = ""


class RenderIn(BaseModel):
    params: dict = {}


# ---------------------------------------------------------------- core
REL_PERIOD_LABELS = {
    "today": "今天", "yesterday": "昨天", "last_7d": "近7天", "last_14d": "近14天",
    "last_30d": "近30天", "this_week": "本周", "last_week": "上周",
    "this_month": "本月", "last_month": "上月",
    "this_quarter": "本季度", "last_quarter": "上季度",
    "this_year": "今年", "last_year": "去年",
}


def _period_info(spec: QuerySpec) -> dict | None:
    """从生效的时间过滤里提炼一个可展示的周期标签（如「2024年」「2000–2020年」）。"""
    for f in spec.filters:
        if f.type != "time" or not f.range:
            continue
        r = f.range
        if r.type == "absolute" and r.start and r.end:
            y1, y2 = r.start[:4], r.end[:4]
            return {"label": f"{y1}年" if y1 == y2 else f"{y1}–{y2}年",
                    "start": r.start, "end": r.end}
        if r.type == "relative" and r.value:
            return {"label": REL_PERIOD_LABELS.get(r.value, r.value), "relative": r.value}
    return None


def run_spec(spec_doc: dict, user: dict, model=None) -> dict:
    model = model or get_model()
    spec = QuerySpec.model_validate(spec_doc)

    # 年度模型：未指定时间且不按时间分组时，默认取最新年份（看板卡片也受益）
    td = model.time_dimension()
    has_time_filter = any(f.type == "time" for f in spec.filters)
    if td and not has_time_filter and td["name"] not in spec.dimensions \
            and getattr(model, "default_time_range", "last_30d") == "latest":
        latest = _latest_time(model)
        if latest:
            y = str(latest)[:4]
            spec.filters.append(Filter(
                type="time", dimension=td["name"],
                range=TimeRange(type="absolute", start=f"{y}-01-01", end=f"{y}-12-31"),
                grain=getattr(model, "default_grain", "day"),
            ))

    compiled = compile_spec(spec, model, user)
    res = executor.execute(compiled["sql"], compiled["params"],
                           source=getattr(model, "source", None))
    kind = viz.infer(spec, model, len(res["rows"]))
    return {
        "spec": spec.model_dump(),
        "columns": compiled["columns"],
        "result": {"rows": res["rows"]},
        "visualization": {"kind": kind},
        "period": _period_info(spec),
        "explain": {
            "metrics": compiled["explain"]["metrics"],
            "sql": compiled["sql"],
            "params": compiled["params"],
            "semantic_model": compiled["explain"]["semantic_model"],
        },
        "latency_ms": res["latency_ms"],
        "cached": res["cached"],
    }


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    source_stats = [executor.warehouse_stats(key) for key in config.DATA_SOURCES]
    ok = all(s["ok"] for s in source_stats)
    try:
        model = get_model()
        return {
            "ok": ok,
            "warehouse": executor.warehouse_stats(),      # 兼容旧字段：默认真实库
            "sources": source_stats,
            "semantic_model": {"name": model.name, "version": model.version,
                               "metrics": len(model.metrics),
                               "dimensions": len(model.dimensions)},
            "llm_configured": bool(config.LLM_API_KEY),
        }
    except SemanticError as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/sources")
def list_sources():
    """数据源清单（文件 / 表 / 行数 / 绑定模型 / 数据性质），供「数据源」页面展示。"""
    return sources.list_sources()


@app.get("/api/meta")
def meta():
    model = get_model()
    return {
        "model": {"name": model.name, "title": model.title, "version": model.version,
                  "data_nature": model.data_nature, "data_note": model.data_note,
                  "source": model.source},
        "metrics": [
            {"name": m["name"], "title": m.get("title", m["name"]),
             "type": m.get("type", "atomic"), "format": m.get("format")}
            for m in model.metrics.values()
        ],
        "dimensions": [
            {"name": d["name"], "title": d.get("title", d["name"]),
             "type": d.get("type", "string"),
             "dictionary": d.get("dictionary")}
            for d in model.dimensions.values()
        ],
        "examples": [
            "上个月 GMV 是多少",
            "9月每天的 GMV 趋势",
            "上个月 GMV 最高的 10 个品类",
            "各个渠道的订单量排名",
            "华东地区 9 月的新客订单量",
            "退款率是多少",
            "各品类的 GMV 占比",
        ],
    }


@app.post("/api/ask")
def ask(body: AskIn, x_user_regions: str | None = Header(default=None)):
    model = get_model()
    user = _user(x_user_regions)
    sid = body.session_id or ("s_" + uuid.uuid4().hex[:8])
    last = _sessions.get(sid, {}).get("last_spec")

    plan = engine.plan(body.question, model, last, latest_date=_latest_time(model))
    if plan["kind"] != "query":
        return {"type": plan["kind"], **{k: v for k, v in plan.items() if k != "kind"}}

    spec = plan["spec"]
    try:
        data = run_spec(spec.model_dump(), user)
    except CompileError as e:
        raise HTTPException(400, f"查询规划失败: {e}") from e
    except ExecError as e:
        raise HTTPException(500, f"查询执行失败: {e}") from e

    _sessions[sid] = {"last_spec": spec.model_dump()}
    qid = store.save_query(
        body.question, spec.model_dump(), data["explain"]["sql"],
        data["result"], data["explain"], data["visualization"],
    )
    return {
        "type": "query",
        "query_id": qid,
        "session_id": sid,
        "notes": plan.get("notes", []),
        "matched": plan.get("matched"),
        **data,
    }


@app.post("/api/run-spec")
def run_spec_endpoint(body: RunSpecIn, x_user_regions: str | None = Header(default=None)):
    """结构化查询入口：看板刷新与 AI 共用同一个网关。"""
    try:
        return run_spec(body.spec, _user(x_user_regions), model=model_by_name(body.model))
    except CompileError as e:
        raise HTTPException(400, f"编译失败: {e}") from e
    except ExecError as e:
        raise HTTPException(500, f"执行失败: {e}") from e


@app.get("/api/queries/{qid}")
def get_query(qid: str):
    q = store.get_query(qid)
    if not q:
        raise HTTPException(404, "查询不存在")
    return q


@app.get("/api/queries/{qid}/explain")
def query_explain(qid: str):
    """「这个数怎么来的」：口径 + 血缘 + 业务含义 + 变更记录。"""
    q = store.get_query(qid)
    if not q:
        raise HTTPException(404, "查询不存在")
    out = knowledge.explain_spec(get_model(), q["spec"])
    out["sql"] = q["sql"]
    out["question"] = q["question"]
    return out


@app.get("/api/knowledge/metric/{name}")
def knowledge_metric(name: str):
    try:
        return knowledge.describe_metric(get_model(), name)
    except SemanticError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/metrics/{name}")
def metric_detail(name: str):
    """单个指标的口径说明（卡片 ⓘ 用）。"""
    model = get_model()
    try:
        info = knowledge.describe_metric(model, name)
    except SemanticError as e:
        raise HTTPException(404, str(e)) from e
    info["model"] = f"{model.name}@{model.version}"
    return info


@app.get("/api/knowledge/search")
def knowledge_search(q: str = ""):
    return {"items": knowledge.search(q)}


# ---------------------------------------------------------------- 沉淀
def _auto_title(spec_doc: dict) -> str:
    model = get_model()
    parts = [model.metric(m).get("title", m) for m in spec_doc.get("metrics", [])]
    title = "、".join(parts) or "查询结果"
    if spec_doc.get("dimensions"):
        dims = "、".join(model.dimension(d).get("title", d) for d in spec_doc["dimensions"])
        title += f" 按 {dims}"
    return title


@app.post("/api/queries/{qid}/pin")
def pin(qid: str, body: PinIn):
    q = store.get_query(qid)
    if not q:
        raise HTTPException(404, "查询不存在")
    title = body.title or _auto_title(q["spec"])
    try:
        res = store.pin_query(qid, body.dashboard_id, title, q["viz"], model=_active_name)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    # 内容变更 → 已批准的看板退回草稿，需重新审批
    workflow.mark_dirty("dashboard", res["dashboard"]["id"])
    res["dashboard"] = store.get_dashboard(res["dashboard"]["id"]) or res["dashboard"]
    return res


@app.get("/api/dashboards")
def list_dashboards():
    return {"items": store.list_dashboards()}


@app.post("/api/dashboards")
def create_dashboard(body: DashboardIn):
    return store.create_dashboard(body.title, model=_active_name,
                                  description=body.description)


@app.get("/api/dashboards/{did}")
def get_dashboard(did: str):
    doc = store.get_dashboard(did)
    if not doc:
        raise HTTPException(404, "看板不存在")
    return doc


@app.patch("/api/dashboards/{did}")
def patch_dashboard(did: str, body: DashboardPatch):
    doc = store.get_dashboard(did)
    if not doc:
        raise HTTPException(404, "看板不存在")
    doc.update(body.doc)
    doc["id"] = did
    doc["status"] = "draft"
    saved = store.save_dashboard(doc)
    workflow.mark_dirty("dashboard", did)
    return saved


@app.post("/api/dashboards/{did}/share")
def share(did: str):
    doc = store.get_dashboard(did)
    if not doc:
        raise HTTPException(404, "看板不存在")
    try:
        token = store.set_share(did)
    except PermissionError as e:
        raise HTTPException(400, str(e)) from e
    return {"share_token": token, "url": f"/?share={token}"}


@app.get("/api/share/{token}")
def get_shared(token: str):
    doc = store.get_by_share(token)
    if not doc:
        raise HTTPException(404, "分享不存在或已失效")
    return doc


@app.get("/api/dashboards/{did}/data")
def dashboard_data(did: str, x_user_regions: str | None = Header(default=None)):
    doc = store.get_dashboard(did)
    if not doc:
        raise HTTPException(404, "看板不存在")
    user = _user(x_user_regions)
    model = model_by_name(doc.get("model"))       # 看板绑定自己的语义模型
    out: dict[str, Any] = {}
    for card in doc.get("cards", []):
        try:
            out[card["id"]] = run_spec(card["spec"], user, model=model)
        except Exception as e:  # noqa: BLE001
            out[card["id"]] = {"error": str(e)}
    return out


@app.post("/api/dashboards/{did}/describe")
def describe_dashboard(did: str, body: DescribeIn | None = None,
                       x_user_regions: str | None = Header(default=None)):
    """根据卡片自动生成看板简介：规则摘要 +（可选）LLM 润色。

    body.apply=False → 只返回候选文案，不改看板；
    body.apply=True  → 写入 doc.description / description_meta（看板退回 draft，走审批后重新发布）。
    """
    doc = store.get_dashboard(did)
    if not doc:
        raise HTTPException(404, "看板不存在")
    body = body or DescribeIn()
    user = _user(x_user_regions)
    model = model_by_name(doc.get("model"))

    built = describe.build(doc, model, user, run_spec)
    has_llm = llm.available()
    if body.text and body.text.strip():
        text = body.text.strip()
        # 前端没回传来源时：与规则摘要一致就认为还是「自动生成」，否则算手工调整
        source = body.source or ("rule" if text == built["text"].strip() else "manual")
    else:
        text, source = built["text"], "rule"
        if body.polish and has_llm:
            try:
                text = describe.polish(built["text"], built["facts"],
                                       doc.get("title", ""), llm.chat)
                source = "llm"
            except Exception:  # noqa: BLE001 —— LLM 失败不影响生成，回退规则摘要
                text, source = built["text"], "rule"

    out: dict[str, Any] = {
        "text": text,
        "cards": len(doc.get("cards") or []),
        "source": source,
        "llm_available": has_llm,
        "meta": describe.meta(built["facts"], source),
    }
    if body.apply:
        doc["description"] = text
        doc["description_meta"] = out["meta"]
        doc["status"] = "draft"                  # 内容变更 → 重新审批
        out["dashboard"] = store.save_dashboard(doc)
        workflow.mark_dirty("dashboard", did)
    return out


# ---------------------------------------------------------------- 规范与审批
@app.get("/api/standards")
def get_standards():
    std = standards.load()
    return {"modeling": std["modeling"], "reporting": std["reporting"]}


@app.get("/api/standards/context")
def standards_context():
    """给 AI 的规范摘要（Markdown），建模 / 报表生成前注入 prompt。"""
    return {"markdown": standards.context_pack()}


@app.post("/api/standards/lint")
def run_lint(body: LintIn):
    try:
        return standards.lint(body.kind, body.doc)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/artifacts")
def list_artifacts(status: str | None = None, kind: str | None = None):
    items = workflow.list_artifacts(status, kind)
    for i in items:
        i.pop("snapshot", None)
        report = i.get("lint") or {}
        i["lint"] = {
            "passed": report.get("passed", True),
            "errors": len(report.get("errors") or []),
            "warnings": len(report.get("warnings") or []),
        }
    return {"items": items}


@app.get("/api/dashboards/{did}/workflow")
def dashboard_workflow(did: str):
    return {"artifact": workflow.find("dashboard", did)}


@app.post("/api/dashboards/{did}/submit")
def submit_dashboard(did: str, x_user: str | None = Header(default=None)):
    doc = store.get_dashboard(did)
    if not doc:
        raise HTTPException(404, "看板不存在")
    try:
        art = workflow.submit("dashboard", did, doc.get("title", ""), doc, x_user or "me")
    except workflow.LintBlocked as e:
        raise HTTPException(400, {"message": str(e), "lint": e.report}) from e
    except workflow.WorkflowError as e:
        raise HTTPException(400, str(e)) from e
    store.set_dashboard_status(did, "submitted")
    return art


def _sync_dashboard_status(doc: dict) -> dict:
    if doc.get("kind") == "dashboard":
        store.set_dashboard_status(doc["ref"], doc["status"])
    return doc


def _require_approver(x_user: str | None) -> str:
    """审批人白名单。未配置时不做限制（本地开发）。"""
    user = x_user or "me"
    if config.APPROVERS and user not in config.APPROVERS:
        raise HTTPException(403, f"用户 {user} 没有审批权限（审批人：{', '.join(config.APPROVERS)}）")
    return user


@app.get("/api/policies")
def get_policies():
    return {
        "approval": config.APPROVAL_POLICY,
        "approvers": config.APPROVERS,
        "note": "semantic/table 走 Git MR；dashboard/card 走平台内审批",
    }


@app.post("/api/artifacts/{aid}/approve")
def approve_artifact(aid: str, body: CommentIn | None = None,
                     x_user: str | None = Header(default=None)):
    user = _require_approver(x_user)
    try:
        art = workflow.approve(aid, user, body.comment if body else "")
    except workflow.WorkflowError as e:
        raise HTTPException(400, str(e)) from e
    return _sync_dashboard_status(art)


@app.post("/api/artifacts/{aid}/reject")
def reject_artifact(aid: str, body: CommentIn | None = None,
                    x_user: str | None = Header(default=None)):
    user = _require_approver(x_user)
    try:
        art = workflow.reject(aid, user, body.comment if body else "")
    except workflow.WorkflowError as e:
        raise HTTPException(400, str(e)) from e
    return _sync_dashboard_status(art)


@app.post("/api/artifacts/{aid}/publish")
def publish_artifact(aid: str, x_user: str | None = Header(default=None)):
    user = _require_approver(x_user)
    try:
        art = workflow.publish(aid, user)
    except workflow.WorkflowError as e:
        raise HTTPException(400, str(e)) from e
    return _sync_dashboard_status(art)


# ---------------------------------------------------------------- 模板库
@app.get("/api/templates")
def list_templates(kind: str | None = None):
    return {"items": templates.list_templates(kind)}


@app.get("/api/templates/{tid}")
def get_template(tid: str):
    try:
        return templates.summary(templates.get(tid)) | {"body": templates.get(tid)["body"]}
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/templates/{tid}/render")
def render_template(tid: str, body: RenderIn | None = None):
    try:
        return templates.render(tid, body.params if body else {})
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/templates/{tid}/create-dashboard")
def create_dashboard_from_template(tid: str, body: RenderIn | None = None,
                                   x_user_regions: str | None = Header(default=None)):
    try:
        res = templates.render(tid, body.params if body else {})
    except KeyError as e:
        raise HTTPException(404, str(e)) from e

    if res["missing"]:
        raise HTTPException(400, f"缺少必填参数: {', '.join(res['missing'])}")
    if res["lint"] and not res["lint"]["passed"]:
        raise HTTPException(400, {"message": "模板产出未通过规范校验", "lint": res["lint"]})
    if res["template"]["kind"] != "dashboard":
        raise HTTPException(400, "只有看板模板可以直接创建看板")

    doc = res["doc"]
    user = _user(x_user_regions)
    dash = store.create_dashboard(doc.get("title", "未命名看板"), model=_active_name,
                                  description=doc.get("description") or "")
    cards: list[dict] = []
    for card in doc.get("cards") or []:
        spec = card.get("spec")
        if spec:
            try:
                data = run_spec(spec, user)
            except (CompileError, ExecError) as e:
                raise HTTPException(400, f"卡片「{card.get('title')}」查询失败: {e}") from e
            qid = store.save_query(f"模板创建 · {card.get('title', '')}", spec,
                                   data["explain"]["sql"], data["result"],
                                   data["explain"], data["visualization"])
            card["query"] = qid
        cards.append(card)
    dash["cards"] = cards
    dash["filters"] = doc.get("filters", [])

    # 建板即生成简介：卡片刚跑过一遍（query 缓存命中），这里几乎零成本
    try:
        built = describe.build(dash, model_by_name(_active_name), user, run_spec)
        if built["text"].strip():
            dash["description"] = built["text"]
            dash["description_meta"] = describe.meta(built["facts"], "rule")
    except Exception:  # noqa: BLE001 —— 生成失败时保留模板自带的简介
        pass
    return store.save_dashboard(dash, bump_version=False)


# ---------------------------------------------------------------- 数据接入
class ImportIn(BaseModel):
    filename: str
    content_base64: str
    table_name: str | None = None


class SaveDraftIn(BaseModel):
    name: str
    yaml: str


@app.get("/api/tables")
def list_tables():
    return {"items": datasets.list_tables()}


@app.get("/api/tables/{name}")
def get_table(name: str):
    try:
        return datasets.table_detail(name)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/tables/import")
def import_table(body: ImportIn):
    try:
        raw = base64.b64decode(body.content_base64)
        return datasets.import_file(body.filename, raw, body.table_name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/tables/{name}/draft-model")
def draft_model(name: str):
    try:
        return datasets.suggest_model(name)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/semantic/drafts")
def save_semantic_draft(body: SaveDraftIn):
    try:
        return datasets.save_draft(body.name, body.yaml)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _model_nature(path) -> str:
    """读模型文件头部的 data_nature（real / demo），失败就当未标注。"""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str(doc.get("data_nature", "") or "")
    except Exception:  # noqa: BLE001
        return ""


@app.get("/api/semantic/models")
def list_models():
    items = [
        {"name": p.stem, "kind": "model", "active": p.stem == _active_name,
         "data_nature": _model_nature(p)}
        for p in sorted(config.SEMANTIC_DIR.glob("*.yaml"))
        if p.name != "knowledge.yaml"
    ]
    drafts_dir = config.SEMANTIC_DIR / "drafts"
    if drafts_dir.exists():
        items += [
            {"name": p.stem, "kind": "draft", "active": p.stem == _active_name,
             "data_nature": _model_nature(p)}
            for p in sorted(drafts_dir.glob("*.yaml"))
        ]
    return {"items": items, "active": _active_name}


@app.post("/api/semantic/models/{name}/activate")
def activate(name: str):
    try:
        model = activate_model(name)
    except SemanticError as e:
        raise HTTPException(404, str(e)) from e
    return {"active": name, "title": model.title,
            "metrics": len(model.metrics), "dimensions": len(model.dimensions)}


# ---------------------------------------------------------------- 数据开发（对话式）
class BuilderStartIn(BaseModel):
    table: str | None = None


class SayIn(BaseModel):
    message: str


class BuilderSaveIn(BaseModel):
    activate: bool = False


@app.post("/api/builder/sessions")
def builder_start(body: BuilderStartIn | None = None):
    try:
        return builder.create_session(body.table if body else None)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/api/builder/sessions/{sid}")
def builder_get(sid: str):
    try:
        return builder._public(builder.get_session(sid))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/builder/sessions/{sid}/say")
def builder_say(sid: str, body: SayIn):
    try:
        return builder.say(sid, body.message)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/builder/sessions/{sid}/save")
def builder_save(sid: str, body: BuilderSaveIn):
    try:
        res = builder.save_session(sid)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.activate:
        try:
            activate_model(res["name"])
        except SemanticError as e:
            raise HTTPException(400, str(e)) from e
        res["activated"] = True
    return res


# ---------------------------------------------------------------- 静态资源
if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")
