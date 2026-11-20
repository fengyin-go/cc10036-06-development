"""多维分析查询与指标平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.config import settings
from app.routers import ROUTERS
from app.store import store
from app.services import service_for

app = FastAPI(title="多维分析查询与指标平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


# ============ 跨模块分析链（按5条主要数据流设计） ============


# ---- 数据流1: 指标定义 → 语义层映射 → 多维立方构建 → 切片查询 ----
@app.post("/api/analytics/metric-pipeline")
def metric_pipeline(metric_code: str = "DAU", sample_data: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """完整指标体系流水线：指标口径解析→语义层映射→立方预计算→切片查询执行。"""
    metric_svc = service_for("metric_def")
    semantic_svc = service_for("semantic_layer")
    cube_svc = service_for("cube_builder")
    slice_svc = service_for("slice_query")

    if any(s is None for s in [metric_svc, semantic_svc, cube_svc, slice_svc]):
        return {"ok": False, "message": "依赖服务未就绪"}

    # Step 1: 指标口径解析
    caliber = metric_svc.resolve_metric_caliber(metric_code)
    if caliber is None:
        return {"ok": False, "message": f"指标 {metric_code} 未定义"}

    # Step 2: 语义层映射
    mapping = semantic_svc.map_metric_to_physical(metric_code)

    # Step 3: 立方预计算（使用已上线立方）
    online_cubes = cube_svc.get_online_cubes()
    cube_results: list[dict[str, Any]] = []
    if sample_data is None:
        sample_data = _sample_behavior_data()
    for cube in online_cubes[:2]:
        precompute = cube_svc.precompute_cube(int(cube["id"]), sample_data)
        cube_results.append(precompute)

    # Step 4: 切片查询
    slice_result = None
    published_templates = [
        t for t in store.rows("slice_query") if t.get("status") == "已发布"
    ]
    if published_templates:
        slice_result = slice_svc.execute_slice_query(int(published_templates[0]["id"]), sample_data)

    return {
        "ok": True,
        "pipeline": "指标定义→语义层→立方构建→切片查询",
        "metric_caliber": caliber,
        "physical_mapping": mapping,
        "cube_precompute": cube_results[:2],
        "slice_query": slice_result,
    }


# ---- 数据流2: 分群规则 → 同期群分析 → 归因模型 → 结果缓存 ----
@app.post("/api/analytics/segment-pipeline")
def segment_pipeline(
    rule_id: int = 1, events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """分群分析流水线：分群计算→同期群保留率→归因权重→缓存存储。"""
    segment_svc = service_for("segment_rule")
    cohort_svc = service_for("cohort_analysis")
    attribution_svc = service_for("attribution_model")
    cache_svc = service_for("result_cache")

    if any(s is None for s in [segment_svc, cohort_svc, attribution_svc, cache_svc]):
        return {"ok": False, "message": "依赖服务未就绪"}

    if events is None:
        events = _sample_user_events()

    # Step 1: 分群计算
    segment_result = segment_svc.compute_segment(rule_id, events)

    # Step 2: 同期群分析
    cohort_result = cohort_svc.run_cohort_analysis(1, events)

    # Step 3: 归因模型
    active_models = attribution_svc.get_active_models()
    attribution_results: list[dict[str, Any]] = []
    for model in active_models[:2]:
        result = attribution_svc.compute_attribution_weights(int(model["id"]), _sample_touchpoints())
        attribution_results.append(result)

    # Step 4: 缓存结果
    cache_key = f"segment_pipeline_{rule_id}"
    _ = cache_svc.get_or_compute(
        "segment_pipeline", {"rule_id": rule_id},
        compute_fn=lambda: {"segment": segment_result.get("matched_count"), "cohort": cohort_result},
        ttl_seconds=600,
    )

    return {
        "ok": True,
        "pipeline": "分群圈选→同期群分析→归因模型→结果缓存",
        "segment": segment_result,
        "cohort": cohort_result,
        "attribution": attribution_results,
        "cached": True,
    }


# ---- 数据流2b: 归因窗口 → 转化归因结算 → 同期群留存渠道贡献 ----
class AttributionSettlementPayload(BaseModel):
    model_id: int = 1
    config_id: int = 1
    events: list[dict[str, Any]] = Field(default_factory=list)
    touchpoints: list[dict[str, Any]] = Field(default_factory=list)
    model_overrides: dict[str, Any] = Field(default_factory=dict)
    max_periods: int = 8


@app.post("/api/analytics/attribution-settlement")
def attribution_settlement(payload: AttributionSettlementPayload) -> dict[str, Any]:
    """按归因窗口结算转化归因，并交给同期群口径输出渠道贡献。

    入参不传 events 时使用内置跨期样例：同一笔转化落在第二个留存周期，
    两个合格触点分别在首期和第二期，用来核对渠道归属周期与矩阵分母。
    """
    settlement_svc = service_for("attribution_model")
    cohort_svc = service_for("cohort_analysis")
    if any(s is None for s in [settlement_svc, cohort_svc]):
        return {"ok": False, "message": "依赖服务未就绪"}

    events = payload.events or _sample_cross_period_events()
    touchpoints = payload.touchpoints or _sample_cross_period_touchpoints()
    overrides = dict(payload.model_overrides)

    settlement = settlement_svc.settle_conversions(
        payload.model_id, _conversions_from_events(events), touchpoints, overrides,
    )
    if not settlement.get("ok"):
        return settlement

    analysis = cohort_svc.analyze_with_attribution(
        payload.config_id, events, payload.model_id, touchpoints, overrides,
        payload.max_periods,
    )
    if not analysis.get("ok"):
        return analysis

    return {
        "ok": True,
        "pipeline": "归因窗口→转化归因结算→同期群留存渠道贡献",
        "settlement": settlement,
        "cohort": analysis,
        "reconcile": {
            "attribution_weight_total": settlement["totals"]["attributed_weight"],
            "cohort_weight_total": round(
                sum(
                    period["total_weight"]
                    for row in analysis["cohort_attribution_matrix"]
                    for period in row["periods"]
                ), 6,
            ),
            "denominators": analysis["denominators"],
            "denominator_rule": analysis["periods"]["denominator"],
            "settlement_basis": analysis["periods"]["settlement_basis"],
            "model_conditions": {
                "model_id": payload.model_id,
                "model": analysis["attribution_caliber"]["model"],
                "window_days": analysis["attribution_caliber"]["window_days"],
                "period_days": analysis["attribution_caliber"]["period_days"],
                "overrides": overrides,
            },
        },
    }


# ---- 数据流3: 看板布局 → 即席导出 → 报表订阅 → 查询加速 ----
@app.post("/api/analytics/dashboard-pipeline")
def dashboard_pipeline(
    dashboard_id: int = 1, query_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """看板导出流水线：看板布局→即席导出→报表订阅→查询加速。"""
    dashboard_svc = service_for("dashboard_layout")
    export_svc = service_for("adhoc_export")
    sub_svc = service_for("report_subscription")
    accelerator_svc = service_for("query_accelerator")

    if any(s is None for s in [dashboard_svc, export_svc, sub_svc, accelerator_svc]):
        return {"ok": False, "message": "依赖服务未就绪"}

    if query_data is None:
        query_data = _sample_behavior_data()

    # Step 1: 看板布局解析
    layout = dashboard_svc.parse_layout(dashboard_id)

    # Step 2: 即席导出
    export_result = export_svc.submit_export(1, query_data)

    # Step 3: 报表订阅
    dash_name = layout.get("dashboard_name", "")
    subscriptions = sub_svc.get_subscriptions_for_dashboard(dash_name)

    # Step 4: 查询加速
    cache_hit = accelerator_svc.compute_cache_hit_rate()

    return {
        "ok": True,
        "pipeline": "看板布局→即席导出→报表订阅→查询加速",
        "layout": layout,
        "export": export_result,
        "subscription_count": len(subscriptions),
        "accelerator_hit_rate": cache_hit.get("hit_rate_pct"),
    }


# ---- 数据流4: 异常检测 → 实验分析 → AB指标 ----
@app.post("/api/analytics/monitor-pipeline")
def monitor_pipeline(
    metric_values: list[float] | None = None,
) -> dict[str, Any]:
    """监控实验流水线：异常检测→实验分析→AB置信区间。"""
    anomaly_svc = service_for("anomaly_detect")
    ab_svc = service_for("ab_analysis")

    if any(s is None for s in [anomaly_svc, ab_svc]):
        return {"ok": False, "message": "依赖服务未就绪"}

    if metric_values is None:
        metric_values = _sample_metric_timeseries()

    # Step 1: 异常检测
    anomaly_result = anomaly_svc.detect(1, metric_values)

    # Step 2: 实验分析（模拟对照组/实验组数据）
    half = len(metric_values) // 2
    control = metric_values[:half]
    treatment = [v * 1.05 for v in metric_values[half:]]  # +5%效果
    ab_result = ab_svc.analyze(1, control, treatment)

    # Step 3: 实验期间异常检测联动
    exp_anomalies = ab_svc.check_anomalies_during_experiment(2, metric_values)

    return {
        "ok": True,
        "pipeline": "异常检测→实验分析→AB指标",
        "anomaly_detection": anomaly_result,
        "ab_analysis": ab_result,
        "experiment_anomalies": exp_anomalies,
    }


# ---- 数据流5: 数据血缘 → 数据范围 → 查询调度 → 下钻路径 ----
@app.post("/api/analytics/governance-pipeline")
def governance_pipeline(
    node_id: str = "1", metric_values: list[float] | None = None,
) -> dict[str, Any]:
    """数据治理流水线：血缘图→数据范围→调度拓扑→下钻分析。"""
    lineage_svc = service_for("data_lineage")
    scope_svc = service_for("olap_scope")
    scheduler_svc = service_for("query_scheduler")
    drill_svc = service_for("drill_path")

    if any(s is None for s in [lineage_svc, scope_svc, scheduler_svc, drill_svc]):
        return {"ok": False, "message": "依赖服务未就绪"}

    # Step 1: 血缘下游影响分析
    impact = lineage_svc.compute_downstream(node_id)

    # Step 2: 数据范围检查
    scope_check = scope_svc.check_access("分析师A", "cube_user_behavior")

    # Step 3: 调度拓扑排序
    topo = scheduler_svc.topological_order()

    # Step 4: 下钻路径
    if metric_values is None:
        metric_values = _sample_metric_timeseries()
    sample_data = [{"region": "华东", "city": "上海", "store": f"门店{i}", "revenue": v} for i, v in enumerate(metric_values[:10])]
    drill_result = drill_svc.execute_drill(1, sample_data)

    return {
        "ok": True,
        "pipeline": "数据血缘→数据范围→查询调度→下钻路径",
        "lineage_impact": impact,
        "scope_check": scope_check,
        "topological_order": topo,
        "drill_result": drill_result,
    }


# ----  天平 ----
@app.get("/api/analytics/lineage/{node_id}/upstream")
def lineage_upstream(node_id: str) -> dict[str, Any]:
    """递归计算血缘上游链（图结构计算）。"""
    svc = service_for("data_lineage")
    if svc is None:
        return {"ok": False, "message": "血缘服务未就绪"}
    return svc.compute_upstream(node_id)


@app.get("/api/analytics/lineage/{node_id}/impact")
def lineage_impact(node_id: str) -> dict[str, Any]:
    """变更影响分析。"""
    svc = service_for("data_lineage")
    if svc is None:
        return {"ok": False, "message": "血缘服务未就绪"}
    return svc.impact_analysis(node_id)


@app.get("/api/analytics/cache/stats")
def cache_stats() -> dict[str, Any]:
    """缓存周期指标。"""
    svc = service_for("result_cache")
    if svc is None:
        return {"ok": False, "message": "缓存服务未就绪"}
    return svc.cache_stats()


@app.get("/api/analytics/scheduler/topology")
def scheduler_topology() -> dict[str, Any]:
    """调度依赖拓扑排序。"""
    svc = service_for("query_scheduler")
    if svc is None:
        return {"ok": False, "message": "调度服务未就绪"}
    return svc.topological_order()


@app.get("/api/analytics/attribution/caliber/{model_id}")
def attribution_caliber(model_id: int) -> dict[str, Any]:
    """查看归因模型收敛后的结算口径（窗口、模型、周期、结算基数）。"""
    svc = service_for("attribution_model")
    if svc is None:
        return {"ok": False, "message": "归因服务未就绪"}
    caliber = svc.model_caliber(model_id)
    if caliber is None:
        return {"ok": False, "message": f"归因模型 {model_id} 不存在"}
    return {"ok": True, "model_id": model_id, "caliber": caliber}


@app.get("/api/analytics/attribution/bills")
def attribution_bills(limit: int = 50) -> dict[str, Any]:
    """历次转化归因结算账单（含模型条件，供按周期对账）。"""
    svc = service_for("attribution_model")
    if svc is None:
        return {"ok": False, "message": "归因服务未就绪"}
    return svc.list_bills(limit)


@app.get("/api/analytics/attribution/bills/{bill_id}")
def attribution_bill(bill_id: str) -> dict[str, Any]:
    """单张结算账单。"""
    svc = service_for("attribution_model")
    if svc is None:
        return {"ok": False, "message": "归因服务未就绪"}
    bill = svc.get_bill(bill_id)
    if bill is None:
        return {"ok": False, "message": f"账单 {bill_id} 不存在"}
    return {"ok": True, "bill": bill}


# ---- 辅助数据生成函数 ----
def _sample_behavior_data() -> list[dict[str, Any]]:
    return [
        {"user_id": "u1", "event_name": "页面访问", "event_time": 1698768000 + i * 3600,
         "city": "北京" if i % 3 == 0 else "上海", "device": "ios" if i % 2 == 0 else "android",
         "amount": 100 + i * 10} for i in range(50)
    ]


def _sample_user_events() -> list[dict[str, Any]]:
    import random
    random.seed(42)
    events: list[dict[str, Any]] = []
    for uid in range(200):
        for day in range(30):
            if random.random() > 0.7:
                events.append({
                    "user_id": f"u{uid}",
                    "event_name": "注册" if day == 0 else "登录",
                    "event_time": 1698768000 + day * 86400 + random.randint(0, 86400),
                })
    return events


def _sample_touchpoints() -> list[dict[str, Any]]:
    return [
        {"name": "搜索广告", "time": 1698768000},
        {"name": "信息流广告", "time": 1698796800},
        {"name": "邮件营销", "time": 1698832800},
        {"name": "社交媒体", "time": 1698868800},
        {"name": "直接访问", "time": 1698883200},
    ]


# 跨期样例：用户 u1 在首期注册并触达「内容社区」，第二期再次被「短信召回」触达，
# 随后在第二期完成「登录」转化。转化落在第二个留存周期，两个合格触点分属首期与第二期。
_CROSS_PERIOD_ANCHOR = 1698624000  # 2023-10-30 00:00:00 UTC，周一，周对齐起点
_CROSS_PERIOD_PERIOD = 7 * 86400


def _sample_cross_period_events() -> list[dict[str, Any]]:
    return [
        # u1 的同期群初始事件：首期注册
        {"user_id": "u1", "event_name": "注册", "event_time": _CROSS_PERIOD_ANCHOR + 3600},
        # u1 的转化：第二期回访（登录）
        {"user_id": "u1", "event_name": "登录", "event_time": _CROSS_PERIOD_ANCHOR + _CROSS_PERIOD_PERIOD + 7200},
        # 同一回访重复提交：结算时只记一次权重
        {"user_id": "u1", "event_name": "登录", "event_time": _CROSS_PERIOD_ANCHOR + _CROSS_PERIOD_PERIOD + 7200},
        # u2 首期注册，首期内转化，作为对照
        {"user_id": "u2", "event_name": "注册", "event_time": _CROSS_PERIOD_ANCHOR + 7200},
        {"user_id": "u2", "event_name": "登录", "event_time": _CROSS_PERIOD_ANCHOR + 2 * 86400},
    ]


def _sample_cross_period_touchpoints() -> list[dict[str, Any]]:
    return [
        # 首期合格触点：内容社区
        {"user_id": "u1", "channel": "内容社区", "time": _CROSS_PERIOD_ANCHOR + 1800},
        # 第二期合格触点：短信召回（紧随转化之前）
        {"user_id": "u1", "channel": "短信召回", "time": _CROSS_PERIOD_ANCHOR + _CROSS_PERIOD_PERIOD + 3600},
        # 晚于转化的触点：无效，不能参与归因
        {"user_id": "u1", "channel": "线下活动", "time": _CROSS_PERIOD_ANCHOR + _CROSS_PERIOD_PERIOD + 86400},
        # u2 的两个合格触点
        {"user_id": "u2", "channel": "搜索广告", "time": _CROSS_PERIOD_ANCHOR + 5400},
        {"user_id": "u2", "channel": "信息流广告", "time": _CROSS_PERIOD_ANCHOR + 86400},
    ]


def _conversions_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.analytics_engine import conversions_from_events
    return conversions_from_events(events, "登录")


def _sample_metric_timeseries() -> list[float]:
    import random, math
    random.seed(42)
    base = 100.0
    values: list[float] = []
    for i in range(100):
        trend = base + i * 0.5 + 10 * math.sin(i / 10)
        noise = random.gauss(0, 5)
        if i in (20, 45, 78):
            noise += random.choice([-30, 40])  # 异常点
        values.append(round(max(0, trend + noise), 2))
    return values


# 资源路由含 /api/{module}，必须放在 health / overview 之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in ROUTERS:
    app.include_router(module.router)