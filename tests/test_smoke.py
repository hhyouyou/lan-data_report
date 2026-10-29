"""端到端冒烟测试：语义层 → 编译 → 执行 → 问数 → 沉淀。

运行：python -m pytest tests -q
前提：先执行 python -m server.app.seed
"""
from __future__ import annotations

import pytest

from server.app import config, engine, executor
from server.app.compiler import compile_spec
from server.app.contracts import Filter, OrderBy, QuerySpec, TimeRange
from server.app.semantic import load_model


@pytest.fixture(scope="session")
def model():
    return load_model("ecommerce", config.SEMANTIC_DIR)


@pytest.fixture(scope="session", autouse=True)
def _require_sources():
    if not config.DEMO_DB.exists():
        pytest.skip("缺少演示数据，请先运行 python -m server.app.seed")


def run(spec, model, user=None):
    out = compile_spec(spec, model, user or {})
    res = executor.execute(out["sql"], out["params"], use_cache=False,
                           source=getattr(model, "source", None))
    return out, res


# ------------------------------------------------------------------ 编译
def test_simple_metric(model):
    spec = QuerySpec(
        metrics=["gmv"],
        filters=[Filter(type="time", dimension="order_date",
                        range=TimeRange(type="absolute", start="2026-09-01", end="2026-09-30"))],
    )
    out, res = run(spec, model)
    assert "SUM(" in out["sql"]
    assert len(res["rows"]) == 1
    assert res["rows"][0][0] > 0


def test_dimension_and_ranking(model):
    spec = QuerySpec(
        metrics=["gmv"],
        dimensions=["category_l1"],
        order_by=[OrderBy(by="gmv", dir="desc")],
        limit=3,
        filters=[Filter(type="time", dimension="order_date",
                        range=TimeRange(type="relative", value="this_year"))],
    )
    out, res = run(spec, model)
    assert "GROUP BY" in out["sql"]
    assert len(res["rows"]) == 3
    vals = [r[1] for r in res["rows"]]
    assert vals == sorted(vals, reverse=True)


def test_derived_metric(model):
    spec = QuerySpec(
        metrics=["aov"],
        filters=[Filter(type="time", dimension="order_date",
                        range=TimeRange(type="relative", value="last_month"))],
    )
    out, res = run(spec, model)
    assert "NULLIF" in out["sql"]
    assert res["rows"][0][0] > 0


def test_value_filter(model):
    spec = QuerySpec(
        metrics=["gmv"],
        filters=[
            Filter(type="dimension", dimension="channel", op="in", values=["直播"]),
            Filter(type="time", dimension="order_date",
                   range=TimeRange(type="relative", value="last_month")),
        ],
    )
    out, res = run(spec, model)
    assert "IN (?)" in out["sql"]
    assert res["rows"][0][0] > 0


def test_rls_filters_rows(model):
    base = QuerySpec(
        metrics=["gmv"],
        filters=[Filter(type="time", dimension="order_date",
                        range=TimeRange(type="relative", value="last_month"))],
    )
    _, all_res = run(base, model, {})
    _, east_res = run(base, model, {"regions": ["华东"]})
    assert east_res["rows"][0][0] > 0
    assert east_res["rows"][0][0] < all_res["rows"][0][0]


# ------------------------------------------------------------------ 问数
@pytest.mark.parametrize("question,expect", [
    ("上个月 GMV 是多少", {"metrics": ["gmv"]}),
    ("9月每天的 GMV 趋势", {"metrics": ["gmv"], "dimensions": ["order_date"]}),
    ("上个月 GMV 最高的 10 个品类", {"metrics": ["gmv"], "dimensions": ["category_l1"], "limit": 10}),
    ("各个渠道的订单量排名", {"metrics": ["order_cnt"], "dimensions": ["channel"]}),
])
def test_engine_plan(question, expect, model):
    plan = engine.plan(question, model)
    assert plan["kind"] == "query", f"{question} -> {plan}"
    spec = plan["spec"].model_dump()
    for k, v in expect.items():
        assert spec[k] == v, f"{question}: {k} 期望 {v}，实际 {spec[k]}"


def test_engine_clarification(model):
    plan = engine.plan("你好", model)
    assert plan["kind"] == "clarification"


def test_engine_refuse(model):
    plan = engine.plan("竞品的销量是多少", model)
    assert plan["kind"] == "refuse"


# ------------------------------------------------------------------ API
def test_api_ask_and_pin():
    from fastapi.testclient import TestClient

    from server.app.main import app

    client = TestClient(app)

    r = client.post("/api/ask", json={"question": "上个月 GMV 是多少"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["type"] == "query"
    assert data["result"]["rows"][0][0] > 0
    assert "SQL" not in data["explain"]["sql"]  # 只是确保是字符串
    assert "SUM" in data["explain"]["sql"]

    qid = data["query_id"]
    r = client.post(f"/api/queries/{qid}/pin", json={"title": "测试卡片"})
    assert r.status_code == 200, r.text
    dash = r.json()["dashboard"]
    assert dash["cards"]

    r = client.get(f"/api/dashboards/{dash['id']}/data")
    assert r.status_code == 200
    payload = r.json()
    assert payload[dash["cards"][0]["id"]]["result"]["rows"][0][0] > 0


# ------------------------------------------------------------------ 知识层
def test_knowledge_lineage(model):
    from server.app import knowledge

    d = knowledge.describe_metric(model, "gmv")
    assert d["lineage"]["table"] == "fact_order"
    assert d["lineage"]["field"] == "paid_amount"
    assert d["lineage"]["path"][-1] == "metric:gmv"
    assert d["business"]["owner"]

    a = knowledge.describe_metric(model, "aov")
    assert set(a["lineage"]["depends_on"]) == {"gmv", "order_cnt"}
    assert "aov" in knowledge.dependents(model, "gmv")


def test_api_explain():
    from fastapi.testclient import TestClient

    from server.app.main import app

    client = TestClient(app)
    r = client.post("/api/ask", json={"question": "退款率是多少"})
    qid = r.json()["query_id"]

    e = client.get(f"/api/queries/{qid}/explain")
    assert e.status_code == 200, e.text
    body = e.json()
    assert body["metrics"][0]["name"] == "refund_rate"
    assert body["metrics"][0]["business"]["business_meaning"]
    assert body["sql"]

    s = client.get("/api/knowledge/search", params={"q": "运费"}).json()
    assert s["items"]


# ------------------------------------------------------------------ 规范与审批
def test_lint_semantic_passes(model):
    from server.app import standards

    doc = model.doc
    report = standards.lint("semantic", doc)
    assert report["passed"], report["errors"]
    # 演示表名不符合数仓命名规范，应以 warn 提示而不是阻断
    assert any(i["rule"] == "table_naming" for i in report["warnings"])


def test_lint_dashboard_catches_violations():
    from server.app import standards

    doc = {
        "title": "测试看板",
        "cards": [{
            "id": "c1", "title": "图表1",
            "visualization": {"kind": "radar"},
            "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
        }],
    }
    report = standards.lint("dashboard", doc)
    assert not report["passed"]
    rules = {e["rule"] for e in report["errors"]}
    assert "chart_type" in rules
    assert any(i["rule"] == "card_title" for i in report["warnings"])


def test_standards_context_pack():
    from server.app import standards

    md = standards.context_pack()
    assert "数仓分层" in md
    assert "图表选择" in md
    assert "禁止事项" in md


def test_workflow_submit_approve_publish():
    from fastapi.testclient import TestClient

    from server.app.main import app

    client = TestClient(app)
    d = client.post("/api/dashboards", json={"title": "审批流测试看板"}).json()
    did = d["id"]

    # 未发布不能分享
    assert client.post(f"/api/dashboards/{did}/share").status_code == 400

    art = client.post(f"/api/dashboards/{did}/submit").json()
    assert art["status"] == "submitted"
    assert art["lint"]["passed"]

    approved = client.post(f"/api/artifacts/{art['id']}/approve", json={}).json()
    assert approved["status"] == "approved"

    # 已通过但未发布，仍不能分享
    assert client.post(f"/api/dashboards/{did}/share").status_code == 400

    published = client.post(f"/api/artifacts/{art['id']}/publish", json={}).json()
    assert published["status"] == "published"

    shared = client.post(f"/api/dashboards/{did}/share")
    assert shared.status_code == 200
    assert shared.json()["share_token"]


def test_workflow_blocks_bad_lint():
    from fastapi.testclient import TestClient

    from server.app.main import app

    client = TestClient(app)
    d = client.post("/api/dashboards", json={"title": "违规看板"}).json()
    did = d["id"]
    client.patch(f"/api/dashboards/{did}", json={"doc": {"cards": [{
        "id": "c1", "title": "图表1",
        "visualization": {"kind": "radar"},
        "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
    }]}})

    r = client.post(f"/api/dashboards/{did}/submit")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["lint"]["errors"]
    assert any(e["rule"] == "chart_type" for e in detail["lint"]["errors"])


# ------------------------------------------------------------------ 模板库
def test_templates_render_and_lint():
    from server.app import templates

    items = templates.list_templates()
    assert len(items) >= 6
    assert {t["kind"] for t in items} >= {"table", "card", "dashboard"}

    # 建模模板：表名由分层 + 域 + 主题 + 后缀拼出
    r = templates.render("dwd_fact_table", {
        "domain": "trade", "subject": "order_detail", "owner": "交易数据组",
    })
    assert r["doc"]["table"] == "dwd_trade_order_detail_di"
    assert r["lint"]["passed"], r["lint"]
    assert r["doc"]["layer"] == "dwd"

    # 卡片模板
    r2 = templates.render("trend_line", {"metric": "gmv", "metric_label": "GMV"})
    assert r2["lint"]["passed"], r2["lint"]
    assert r2["doc"]["visualization"]["kind"] == "line"

    # 看板模板
    r3 = templates.render("business_review", {"title": "2026年9月经营分析"})
    assert r3["lint"]["passed"], r3["lint"]
    assert len(r3["doc"]["cards"]) == 6


def test_template_missing_params():
    from server.app import templates

    r = templates.render("dwd_fact_table", {"subject": "order_detail"})
    assert set(r["missing"]) == {"domain", "owner"}
    assert r["doc"] is None


def test_api_create_dashboard_from_template():
    from fastapi.testclient import TestClient

    from server.app.main import app

    client = TestClient(app)
    r = client.post("/api/templates/business_review/create-dashboard",
                    json={"params": {"title": "模板生成的经营分析"}})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert len(doc["cards"]) == 6
    assert all(c.get("query") for c in doc["cards"])
    assert doc["status"] == "draft"

    # 卡片必须能真正取到数（spec 要保留在卡片里）
    data = client.get(f"/api/dashboards/{doc['id']}/data").json()
    for card in doc["cards"]:
        assert "error" not in data[card["id"]], data[card["id"]]
        assert data[card["id"]]["result"]["rows"]


# ------------------------------------------------------------------ 审批策略
def test_semantic_goes_git_not_platform():
    import pytest as _pytest

    from server.app import workflow

    with _pytest.raises(workflow.WorkflowError) as ei:
        workflow.submit("semantic", "ecommerce", "电商模型", {})
    assert "Git MR" in str(ei.value)


def test_policies_endpoint():
    from fastapi.testclient import TestClient

    from server.app.main import app

    client = TestClient(app)
    p = client.get("/api/policies").json()
    assert p["approval"]["semantic"] == "git"
    assert p["approval"]["dashboard"] == "platform"


def test_approver_whitelist(monkeypatch):
    from fastapi.testclient import TestClient

    from server.app import config as cfg
    from server.app.main import app

    monkeypatch.setattr(cfg, "APPROVERS", ["alice"])
    client = TestClient(app)

    d = client.post("/api/dashboards", json={"title": "审批权限测试"}).json()
    art = client.post(f"/api/dashboards/{d['id']}/submit").json()

    denied = client.post(f"/api/artifacts/{art['id']}/approve", json={},
                         headers={"X-User": "bob"})
    assert denied.status_code == 403

    ok = client.post(f"/api/artifacts/{art['id']}/approve", json={},
                     headers={"X-User": "alice"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"


# ------------------------------------------------------------------ 数据接入
CSV_SAMPLE = (
    "订单日期,销售区域,渠道,订单金额,订单数量,是否新客\n"
    "2026-07-01,华东,直播,12800.5,12,是\n"
    "2026-07-02,华南,搜索,9600.0,8,否\n"
    "2026-07-03,华东,推荐,15200.0,15,是\n"
    "2026-07-04,华北,直播,7300.0,6,否\n"
    "2026-07-05,华南,广告,4100.0,4,是\n"
)


def test_import_draft_activate_ask():
    import base64

    from fastapi.testclient import TestClient

    from server.app import config
    from server.app.main import activate_model, app

    client = TestClient(app)
    b64 = base64.b64encode(CSV_SAMPLE.encode("utf-8")).decode()
    r = client.post("/api/tables/import", json={
        "filename": "sales.csv", "content_base64": b64, "table_name": "t_test_sales",
    })
    assert r.status_code == 200, r.text
    assert r.json()["table"] == "t_test_sales"

    detail = client.get("/api/tables/t_test_sales").json()
    names = {c["name"] for c in detail["columns"]}
    assert {"order_date", "region", "channel", "amt_order"} <= names
    kinds = {c["name"]: c["kind"] for c in detail["columns"]}
    assert kinds["order_date"] == "time"
    assert kinds["is_new_user"] == "boolean"

    draft = client.post("/api/tables/t_test_sales/draft-model").json()
    assert draft["lint"]["passed"], draft["lint"]
    assert draft["stats"]["metrics"] >= 1
    assert any(d["name"] == "order_date" for d in draft["doc"]["dimensions"])

    saved = client.post("/api/semantic/drafts",
                        json={"name": "t_test_sales", "yaml": draft["yaml"]})
    assert saved.status_code == 200, saved.text

    draft_path = config.SEMANTIC_DIR / "drafts" / "t_test_sales.yaml"
    try:
        act = client.post("/api/semantic/models/t_test_sales/activate")
        assert act.status_code == 200, act.text

        ans = client.post("/api/ask", json={"question": "7月各渠道的订单金额"})
        assert ans.status_code == 200, ans.text
        body = ans.json()
        assert body["type"] == "query"
        assert body["result"]["rows"]
        assert "amt_order" in body["spec"]["metrics"]
    finally:
        activate_model("ecommerce")
        draft_path.unlink(missing_ok=True)
        import sqlite3

        conn = sqlite3.connect(config.WAREHOUSE)
        conn.execute('DROP TABLE IF EXISTS "t_test_sales"')
        conn.execute("DELETE FROM _ldr_column_meta WHERE table_name='t_test_sales'")
        conn.commit()
        conn.close()


# ------------------------------------------------------------------ 对话式数据开发
def test_builder_conversation():
    import base64
    import sqlite3

    from fastapi.testclient import TestClient

    from server.app import config
    from server.app.main import activate_model, app

    client = TestClient(app)
    b64 = base64.b64encode(CSV_SAMPLE.encode("utf-8")).decode()
    client.post("/api/tables/import", json={
        "filename": "b.csv", "content_base64": b64, "table_name": "t_builder_test",
    })

    try:
        s = client.post("/api/builder/sessions", json={"table": "t_builder_test"}).json()
        sid = s["id"]
        base_metrics = s["stats"]["metrics"]
        assert base_metrics >= 1

        # 看到 welcome 消息
        assert s["messages"] and s["messages"][0]["role"] == "assistant"

        # 新增派生指标
        r = client.post(f"/api/builder/sessions/{sid}/say",
                        json={"message": "加一个客单价指标，等于订单金额/订单数量"}).json()
        assert r["stats"]["metrics"] == base_metrics + 1
        derived = [m for m in r["doc"]["metrics"] if m["type"] == "derived"]
        assert derived and "{" in derived[0]["expr"]

        # 改名
        r2 = client.post(f"/api/builder/sessions/{sid}/say",
                         json={"message": "把订单金额改名为成交金额"}).json()
        assert "成交金额" in [m["title"] for m in r2["doc"]["metrics"]]

        # 加同义词
        r3 = client.post(f"/api/builder/sessions/{sid}/say",
                         json={"message": "给成交金额加个别名 销售额"}).json()
        amt = [m for m in r3["doc"]["metrics"] if m["title"] == "成交金额"][0]
        assert "销售额" in amt["synonyms"]

        # 校验
        r4 = client.post(f"/api/builder/sessions/{sid}/say",
                         json={"message": "检查一下"}).json()
        assert "校验" in r4["messages"][-1]["text"]

        # 保存并激活 → 直接用新模型问数
        saved = client.post(f"/api/builder/sessions/{sid}/save",
                            json={"activate": True}).json()
        assert saved["activated"] is True

        ans = client.post("/api/ask", json={"question": "7月各渠道的成交金额"})
        assert ans.status_code == 200, ans.text
        body = ans.json()
        assert body["type"] == "query"
        assert body["result"]["rows"]

        # 无法识别的指令给出帮助
        r5 = client.post(f"/api/builder/sessions/{sid}/say",
                         json={"message": "帮我把数据变好看一点"}).json()
        assert "没太听懂" in r5["messages"][-1]["text"]
    finally:
        activate_model("ecommerce")
        (config.SEMANTIC_DIR / "drafts" / "t_builder_test.yaml").unlink(missing_ok=True)
        conn = sqlite3.connect(config.WAREHOUSE)
        conn.execute('DROP TABLE IF EXISTS "t_builder_test"')
        conn.execute("DELETE FROM _ldr_column_meta WHERE table_name='t_builder_test'")
        conn.commit()
        conn.close()


# ------------------------------------------------------------------ 江苏统计年鉴（真实数据）
def _has_table(name: str) -> bool:
    import sqlite3

    from server.app import config

    conn = sqlite3.connect(config.WAREHOUSE)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


@pytest.fixture(scope="session")
def jsfj_model():
    if not _has_table("fact_stat"):
        pytest.skip("未运行 tools/jsfj_etl.py，跳过年鉴数据测试")
    from server.app import config
    from server.app.semantic import load_model_file

    return load_model_file(config.SEMANTIC_DIR / "jsfj.yaml")


def test_engine_longest_match(jsfj_model):
    from server.app import engine

    # 「人均地区生产总值」不能把「地区」误识别成维度
    metrics, dims = engine._link("人均地区生产总值", jsfj_model)
    assert [m["name"] for m in metrics] == ["gdp_per_capita"]
    assert dims == []

    # 「人均GDP」不能同时命中 GDP 和人均 GDP
    metrics2, _ = engine._link("人均GDP", jsfj_model)
    assert [m["name"] for m in metrics2] == ["gdp_per_capita"]


def test_jsfj_year_range_and_latest(jsfj_model):
    from server.app import engine

    p = engine.plan("2000年到2020年GDP", jsfj_model, latest_date="2024-01-01")
    spec = p["spec"]
    assert spec.dimensions == ["stat_year"]          # 跨多年自动按年展示
    ranges = [f.range for f in spec.filters if f.type == "time"]
    assert ranges and ranges[0].start == "2000-01-01" and ranges[0].end == "2020-12-31"

    p2 = engine.plan("江苏省GDP是多少", jsfj_model, latest_date="2024-01-01")
    assert p2["spec"].dimensions == []
    r2 = [f.range for f in p2["spec"].filters if f.type == "time"][0]
    assert r2.start == "2024-01-01"                  # 默认取最新年份


def test_jsfj_query_values(jsfj_model):
    from server.app import engine, executor
    from server.app.compiler import compile_spec

    def run(question):
        p = engine.plan(question, jsfj_model, latest_date="2024-01-01")
        out = compile_spec(p["spec"], jsfj_model, {})
        return executor.execute(out["sql"], out["params"], use_cache=False,
                               source=jsfj_model.source)["rows"]

    gdp_2020 = run("2020年江苏GDP")
    assert len(gdp_2020) == 1
    assert abs(gdp_2020[0][0] - 104566.56) < 0.01

    trend = run("江苏GDP趋势")
    assert len(trend) >= 50                          # 1952-2024 抽样年份
    assert trend[0][0] == "1952"

    ratio = run("城乡收入比")
    assert len(ratio) == 1
    assert 1.5 < ratio[0][0] < 3.5                   # 城乡收入比量级合理


def test_jsfj_city_queries(jsfj_model):
    from server.app import engine, executor
    from server.app.compiler import compile_spec

    def run(question):
        p = engine.plan(question, jsfj_model, latest_date="2024-01-01")
        out = compile_spec(p["spec"], jsfj_model, {})
        return executor.execute(out["sql"], out["params"], use_cache=False,
                               source=jsfj_model.source)["rows"]

    cities = run("2024年各市GDP")
    assert len(cities) == 13
    assert cities[0][0] == "苏州市" and cities[0][1] > 20000

    su = run("苏南GDP")
    assert len(su) == 1 and abs(su[0][0] - 77844.68) < 1

    pop = run("南京人口")
    assert len(pop) == 1 and abs(pop[0][0] - 957.7) < 0.1

    ur = run("各市城镇化率")
    assert len(ur) == 13
    assert all(0.5 < row[1] < 1 for row in ur)

    # 多值应合并成一个 IN，而不是 AND 两个 IN
    p = engine.plan("苏州和南京的GDP", jsfj_model, latest_date="2024-01-01")
    sql = compile_spec(p["spec"], jsfj_model, {})["sql"]
    assert sql.count("IN (") == 1


def test_metric_info_endpoint():
    from fastapi.testclient import TestClient

    from server.app.main import activate_model, app

    client = TestClient(app)
    try:
        activate_model("jsfj")
        r = client.get("/api/metrics/gdp_city")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["definition"]["definition"]
        assert body["model"].startswith("jsfj")
    finally:
        activate_model("ecommerce")


def test_jsfj_dashboard_template_lints():
    from server.app import templates

    r = templates.render("jsfj_review", {"title": "测试看板"})
    assert r["lint"]["passed"], r["lint"]
    assert len(r["doc"]["cards"]) == 11


def test_county_drill_data(jsfj_model):
    """地图下钻：按所属城市取县市数据。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["gdp_county"],
            "dimensions": ["region"],
            "filters": [{"type": "dimension", "dimension": "parent_region",
                         "op": "eq", "value": "苏州市"}],
            "order_by": [{"by": "gdp_county", "dir": "desc"}],
        }, {})
        rows = out["result"]["rows"]
        # 4 个县市 + 合并后的「苏州市区」
        assert len(rows) == 5
        assert rows[0][0] == "苏州市区"
        assert abs(rows[0][1] - 13011.36) < 0.01
        assert rows[1][0] == "昆山市"
        assert abs(rows[1][1] - 5380.17) < 0.01
        assert rows[-1][1] > 0
    finally:
        activate_model("ecommerce")


def test_urban_district_residual_matches_city_total(jsfj_model):
    """市区 = 全市 − 县市之和，必须严丝合缝。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        for city, total in (("苏州市", 26726.98), ("南通市", 12421.89), ("南京市", 18500.81)):
            out = run_spec({
                "metrics": ["gdp_county"],
                "dimensions": ["region"],
                "filters": [{"type": "dimension", "dimension": "parent_region",
                             "op": "eq", "value": city}],
            }, {})
            s = sum(r[1] for r in out["result"]["rows"])
            assert abs(s - total) < 0.05, f"{city}: {s} != {total}"
    finally:
        activate_model("ecommerce")


def test_map_viz_hint(jsfj_model):
    from server.app import viz
    from server.app.contracts import QuerySpec

    spec = QuerySpec(metrics=["gdp_city"], dimensions=["region"], viz_hint="map")
    assert viz.infer(spec, jsfj_model, 13) == "map"


def test_map_multi_metric_spec(jsfj_model):
    """地图卡一次取 4 个指标，前端切换。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["gdp_city", "gdp_per_capita_city",
                        "population_total_city", "income_all_city"],
            "dimensions": ["region"],
            "viz_hint": "map",
        }, {})
        assert out["visualization"]["kind"] == "map"
        assert len(out["columns"]) == 5
        assert len(out["result"]["rows"]) == 13
        labels = [c["label"] for c in out["columns"]]
        assert labels[0] == "地区"
        row = next(r for r in out["result"]["rows"] if r[0] == "苏州市")
        assert row[1] > 20000 and row[2] > 100000 and row[3] > 1000 and row[4] > 50000
    finally:
        activate_model("ecommerce")


def test_county_population_and_income(jsfj_model):
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["population_total_county", "income_all_county"],
            "dimensions": ["region"],
            "filters": [{"type": "dimension", "dimension": "parent_region",
                         "op": "eq", "value": "南通市"}],
        }, {})
        rows = out["result"]["rows"]
        assert len(rows) == 5                      # 市区 + 4 县市
        total_pop = sum(r[1] for r in rows)
        assert abs(total_pop - 775.0) < 0.11        # 南通全市常住人口
        assert all(r[2] and r[2] > 0 for r in rows)
    finally:
        activate_model("ecommerce")


def test_county_tiers(jsfj_model):
    """县域梯队：千亿县 22 个，梯队合计 40 个县市。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["county_count"],
            "dimensions": ["county_tier"],
            "filters": [{"type": "dimension", "dimension": "county_tier", "op": "in",
                         "value": ["千亿县", "900-1000亿", "800-900亿", "700-800亿", "700亿以下"]}],
            "order_by": [{"by": "county_count", "dir": "desc"}],
        }, {})
        rows = dict((r[0], r[1]) for r in out["result"]["rows"])
        assert rows["千亿县"] == 22
        assert sum(rows.values()) == 40
    finally:
        activate_model("ecommerce")


def test_county_aggregates(jsfj_model):
    """县域合计、千亿县数、占全省比重、人均 GDP。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["gdp_county_only", "county_count_1000",
                        "county_gdp_share", "county_gdp_per_capita"],
        }, {})
        row = out["result"]["rows"][0]
        assert abs(row[0] - 55808.00) < 0.5
        assert row[1] == 22
        assert abs(row[2] - 0.4074) < 0.002
        assert 120000 < row[3] < 220000
    finally:
        activate_model("ecommerce")


def test_scatter_viz_hint(jsfj_model):
    from server.app import viz
    from server.app.contracts import QuerySpec

    spec = QuerySpec(metrics=["gdp_per_capita_county", "income_all_county"],
                     dimensions=["region"], viz_hint="scatter")
    assert viz.infer(spec, jsfj_model, 40) == "scatter"


def test_county_dashboard_template_lints():
    from server.app import templates

    r = templates.render("jsfj_county", {"title": "江苏县域经济专题"})
    assert r["lint"]["passed"], r["lint"]
    assert len(r["doc"]["cards"]) == 9
    """卡片需要能显示「这是哪一年的数据」。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        # 无时间过滤 → 后端按最新年份补齐，并回传 label
        out = run_spec({"metrics": ["gdp_city"], "dimensions": ["region"]}, {})
        assert out["period"] is not None
        assert out["period"]["label"].endswith("年")

        # 显式区间 → 区间标签
        out2 = run_spec({
            "metrics": ["gdp"], "dimensions": ["stat_year"],
            "filters": [{"type": "time", "dimension": "stat_year",
                         "range": {"type": "absolute", "start": "2000-01-01", "end": "2020-12-31"}}],
        }, {})
        assert out2["period"]["label"] == "2000–2020年"
    finally:
        activate_model("ecommerce")


def test_scatter_multi_category_columns(jsfj_model):
    """散点卡一次带回城市 + 区域两列，前端切换着色维度。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["gdp_per_capita_county", "income_all_county"],
            "dimensions": ["region", "parent_region", "region_group"],
            "filters": [{"type": "dimension", "dimension": "county_tier", "op": "in",
                         "value": ["千亿县", "900-1000亿", "800-900亿", "700-800亿", "700亿以下"]}],
            "viz_hint": "scatter",
        }, {})
        assert out["visualization"]["kind"] == "scatter"
        assert len(out["columns"]) == 5          # 地区 / 城市 / 区域 / x / y
        assert out["columns"][1]["label"] == "所属城市"
        assert out["columns"][2]["label"] == "区域"
        assert len(out["result"]["rows"]) == 40
        row = next(r for r in out["result"]["rows"] if r[0] == "昆山市")
        assert row[1] == "苏州市" and row[2] == "苏南"
    finally:
        activate_model("ecommerce")


def test_dashboard_binds_its_model():
    """看板必须记住自己的语义模型，切换当前模型后仍能正常取数。"""
    from fastapi.testclient import TestClient

    from server.app.main import activate_model, app

    client = TestClient(app)
    activate_model("jsfj")
    try:
        doc = client.post("/api/templates/jsfj_review/create-dashboard",
                          json={"params": {"title": "模型绑定测试"}}).json()
        assert doc["model"] == "jsfj"
        assert doc["description"].strip()        # 模板 frontmatter 的简介写进看板

        activate_model("ecommerce")          # 切走当前模型
        payload = client.get(f"/api/dashboards/{doc['id']}/data").json()
        for cid, v in payload.items():
            assert "error" not in v, f"{cid}: {v.get('error')}"
            assert v["result"]["rows"], f"{cid} 没有数据"
    finally:
        activate_model("ecommerce")


def test_dashboard_intro_copy():
    """每张看板模板都带简介；创建看板时写入 doc，列表接口也带出。"""
    from fastapi.testclient import TestClient

    from server.app import templates
    from server.app.main import activate_model, app

    for tid in ("jsfj_review", "jsfj_county", "jsfj_invest", "business_review"):
        r = templates.render(tid, {"title": "简介测试"})
        assert r["doc"]["description"].strip(), tid

    client = TestClient(app)
    activate_model("jsfj")
    try:
        doc = client.post("/api/templates/jsfj_county/create-dashboard",
                          json={"params": {"title": "简介测试看板"}}).json()
        assert doc["description"].strip()
        assert "张卡片" in doc["description"]          # 建板即生成，而不是模板 frontmatter
        assert doc["description_meta"].get("cards", 0) > 0
        items = client.get("/api/dashboards").json()["items"]
        hit = next(i for i in items if i["id"] == doc["id"])
        assert hit["description"] == doc["description"]
    finally:
        activate_model("ecommerce")


def test_meta_marks_data_nature():
    """模型带数据性质标记：jsfj=real、ecommerce=demo，接口与模型列表都返回。"""
    from fastapi.testclient import TestClient

    from server.app.main import activate_model, app

    client = TestClient(app)
    activate_model("jsfj")
    try:
        meta = client.get("/api/meta").json()["model"]
        assert meta["data_nature"] == "real"
        assert meta["data_note"]
        assert meta["source"] == "jsfj"          # 查询打到真实库
        models = {i["name"]: i for i in client.get("/api/semantic/models").json()["items"]}
        assert models["jsfj"]["data_nature"] == "real"
        assert models["ecommerce"]["data_nature"] == "demo"
    finally:
        activate_model("ecommerce")
    demo = client.get("/api/meta").json()["model"]
    assert demo["data_nature"] == "demo"
    assert demo["data_note"]
    assert demo["source"] == "demo"              # 查询打到演示库


def test_sources_api_lists_both_sources():
    """数据源接口：两个物理隔离的库，表清单各归各。"""
    from fastapi.testclient import TestClient

    from server.app.main import app

    client = TestClient(app)
    data = client.get("/api/sources").json()
    by = {s["key"]: s for s in data["items"]}
    assert by["jsfj"]["nature"] == "real"
    assert by["demo"]["nature"] == "demo"
    assert by["jsfj"]["path"].endswith("warehouse.sqlite")
    assert by["demo"]["path"].endswith("demo.sqlite")
    assert by["jsfj"]["model"] == "jsfj"
    assert by["demo"]["model"] == "ecommerce"

    real_tables = {t["name"] for t in by["jsfj"]["tables"]}
    demo_tables = {t["name"] for t in by["demo"]["tables"]}
    assert "fact_stat" in real_tables
    assert "fact_order" not in real_tables          # 真实库里没有演示表
    assert demo_tables == {"fact_order", "dim_user", "dim_product"}


def test_dashboard_describe_generation():
    """根据卡片自动生成简介：建板时自动生成、preview 不改看板、apply 写入并退回 draft。"""
    from fastapi.testclient import TestClient

    from server.app.main import activate_model, app

    client = TestClient(app)
    activate_model("jsfj")
    try:
        doc = client.post("/api/templates/jsfj_invest/create-dashboard",
                          json={"params": {"title": "简介生成测试"}}).json()
        did = doc["id"]
        before = client.get(f"/api/dashboards/{did}").json()["description"]
        # 建板即生成：不是模板 frontmatter 的那段，而是基于卡片数据生成的
        assert "张卡片" in before and "覆盖" in before
        meta0 = doc.get("description_meta") or {}
        assert meta0.get("cards") == 13 and meta0.get("source") == "rule"

        prev = client.post(f"/api/dashboards/{did}/describe", json={}).json()
        text = prev["text"]
        assert "指标" in text
        assert "张卡片" in text
        assert "苏州" in text                       # 排行首项来自实际数据
        assert prev["source"] == "rule"
        assert prev["llm_available"] is False
        assert client.get(f"/api/dashboards/{did}").json()["description"] == before

        applied = client.post(f"/api/dashboards/{did}/describe",
                              json={"apply": True, "text": text}).json()
        assert applied["dashboard"]["description"] == text
        assert applied["dashboard"]["status"] == "draft"
        # 前端没传 source 时，内容与规则摘要一致 → 仍记为 rule（而不是 manual）
        assert applied["dashboard"]["description_meta"]["source"] == "rule"
        assert client.get(f"/api/dashboards/{did}").json()["description"] == text

        custom = client.post(f"/api/dashboards/{did}/describe",
                             json={"apply": True, "text": "自定义简介"}).json()
        assert custom["dashboard"]["description"] == "自定义简介"
        assert custom["dashboard"]["description_meta"]["source"] == "manual"
    finally:
        activate_model("ecommerce")


def test_dashboard_describe_llm_polish(monkeypatch):
    """配置 LLM 时用模型润色；模型失败自动回退规则摘要。"""
    from fastapi.testclient import TestClient

    from server.app import llm
    from server.app.main import activate_model, app

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat",
                        lambda messages, **kw: "AI 润色：江苏投资消费看板覆盖 13 张卡片，苏州社零领先。")
    client = TestClient(app)
    activate_model("jsfj")
    try:
        doc = client.post("/api/templates/jsfj_invest/create-dashboard",
                          json={"params": {"title": "LLM 润色测试"}}).json()
        did = doc["id"]
        res = client.post(f"/api/dashboards/{did}/describe", json={}).json()
        assert res["source"] == "llm"
        assert res["llm_available"] is True
        assert res["text"].startswith("AI 润色")

        applied = client.post(f"/api/dashboards/{did}/describe",
                              json={"apply": True, "text": res["text"], "source": "llm"}).json()
        assert applied["dashboard"]["description"] == res["text"]
        assert applied["dashboard"]["description_meta"]["source"] == "llm"

        def boom(messages, **kw):
            raise llm.LLMError("network down")

        monkeypatch.setattr(llm, "chat", boom)
        fallback = client.post(f"/api/dashboards/{did}/describe", json={}).json()
        assert fallback["source"] == "rule"
        assert "张卡片" in fallback["text"]
    finally:
        activate_model("ecommerce")


def test_invest_consumption_metrics(jsfj_model):
    """投资 / 消费 / 财政 / 用电量指标。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["retail_sales", "fiscal_revenue", "fiscal_expenditure",
                        "electricity", "fixed_investment_growth"],
        }, {})
        row = out["result"]["rows"][0]
        assert abs(row[0] - 44922.13) < 0.1        # 社零
        assert abs(row[1] - 10038.16) < 0.1        # 一般公共预算收入
        assert abs(row[2] - 15293.57) < 0.1        # 一般公共预算支出
        assert abs(row[3] - 8486.93) < 0.1         # 全社会用电量
        assert abs(row[4] - 5.9) < 0.05            # 固投增速 %

        out2 = run_spec({
            "metrics": ["retail_sales_city", "electricity_city",
                        "fiscal_self_sufficiency_city", "realestate_invest_city"],
            "dimensions": ["region"],
            "order_by": [{"by": "retail_sales_city", "dir": "desc"}],
            "limit": 3,
        }, {})
        rows = {r[0]: r for r in out2["result"]["rows"]}
        assert abs(rows["苏州市"][1] - 8915.12) < 0.1
        assert abs(rows["苏州市"][2] - 1863.27) < 0.1
        assert 0.9 < rows["苏州市"][3] < 0.96      # 财政自给率
        assert abs(rows["苏州市"][4] - 2508.62) < 0.1
    finally:
        activate_model("ecommerce")


def test_invest_dashboard_template_lints():
    from server.app import templates

    r = templates.render("jsfj_invest", {"title": "江苏投资与消费专题"})
    assert r["lint"]["passed"], r["lint"]
    assert len(r["doc"]["cards"]) == 13


def test_electricity_intensity_and_scatter(jsfj_model):
    """单位 GDP 电耗 + 散点卡片：苏州 0.070 度/元 vs 南京 0.043 度/元。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["electricity_intensity_city"],
            "dimensions": ["region"],
        }, {})
        rows = {r[0]: r[1] for r in out["result"]["rows"]}
        assert abs(rows["苏州市"] - 0.0697) < 0.0005
        assert abs(rows["南京市"] - 0.0434) < 0.0005

        out2 = run_spec({
            "metrics": ["gdp_city", "electricity_city"],
            "dimensions": ["region", "region_group"],
            "limit": 13,
            "viz_hint": "scatter",
        }, {})
        assert len(out2["columns"]) == 4
        assert len(out2["result"]["rows"]) == 13
        assert out2["visualization"]["kind"] == "scatter"
    finally:
        activate_model("ecommerce")


def test_history_backfill(jsfj_model):
    """跨年鉴版本回溯：2019 分市数据 + 1978 起的省级财政。"""
    from server.app.main import activate_model, run_spec

    activate_model("jsfj")
    try:
        out = run_spec({
            "metrics": ["retail_sales_city"],
            "dimensions": ["region"],
            "filters": [{"type": "time", "dimension": "stat_year",
                         "range": {"type": "absolute", "start": "2019-01-01",
                                   "end": "2019-12-31"}}],
        }, {})
        rows = out["result"]["rows"]
        assert len(rows) == 13
        assert abs(sum(r[1] for r in rows) - 37672.52) < 0.1

        out2 = run_spec({
            "metrics": ["fiscal_revenue"],
            "dimensions": ["stat_year"],
            "filters": [{"type": "time", "dimension": "stat_year",
                         "range": {"type": "absolute", "start": "1978-01-01",
                                   "end": "2024-12-31"}}],
            "order_by": [{"by": "stat_year", "dir": "asc"}],
        }, {})
        years = out2["result"]["rows"]
        assert years[0][0] == "1978" and abs(years[0][1] - 61.09) < 0.01
        assert years[-1][0] == "2024" and abs(years[-1][1] - 10038.16) < 0.1
    finally:
        activate_model("ecommerce")
