"""同期群分析领域服务：保留率计算、归因结算消费、渠道贡献对账。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import (
    build_cohort_attribution_matrix,
    compute_cohort_retention,
    conversions_from_events,
    normalize_attribution_config,
    settle_conversion_attribution,
)


class CohortAnalysisService(BaseModuleService):
    MODULE = "cohort_analysis"
    ENTITY = "群组配置"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["群组名称", "初始事件", "回访事件", "分析周期", "群组大小", "保留计算", "显示格式"]
    REQUIRED_FIELDS = ["群组名称", "初始事件", "回访事件"]
    STATUS_ORDER = ["草稿", "分析中", "已完成", "已归档"]
    ACTION_RULES = {"开始分析": "分析中", "完成分析": "已完成", "归档群组": "已归档"}
    NEGATIVE_ACTIONS = []
    EXTRA_FIELDS = ["归因模型"]

    # ---- 业务方法 ----

    def _config(self, config_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, config_id)

    def _period_days(self, cohort_config: dict[str, Any]) -> int:
        try:
            return max(1, int(str(cohort_config.get("分析周期", "7"))))
        except ValueError:
            return 7

    def run_cohort_analysis(
        self, config_id: int, events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """执行同期群分析：基于事件数据计算保留率矩阵。"""
        cohort_config = self._config(config_id)
        if cohort_config is None:
            return {"ok": False, "message": f"同期群配置 {config_id} 不存在"}

        init_event = str(cohort_config.get("初始事件", ""))
        return_event = str(cohort_config.get("回访事件", ""))
        period_days = self._period_days(cohort_config)

        max_periods = 8
        retention_method = str(cohort_config.get("保留计算", "standard")).lower()

        if retention_method == "bounded":
            max_periods = 12  # 有界保留

        matrix = compute_cohort_retention(
            events, init_event, return_event, period_days, max_periods,
        )

        self.run_action(config_id, "完成分析")

        return {
            "ok": True,
            "config_name": str(cohort_config.get("群组名称", "")),
            "init_event": init_event,
            "return_event": return_event,
            "period_days": period_days,
            "max_periods": max_periods,
            "cohort_count": len(matrix.cohort_ids),
            "cohort_ids": matrix.cohort_ids,
            "period_labels": matrix.period_labels,
            "retention_matrix": matrix.retention_matrix,
            "caliber": {
                "period_days": period_days,
                "period_label": f"周（{period_days}天）" if period_days == 7 else f"{period_days}天",
                "denominator": "同期群初始事件用户数（去重）",
            },
            "avg_retention_week1": round(
                sum(row[0] for row in matrix.retention_matrix if len(row) > 0)
                / max(len(matrix.retention_matrix), 1), 2,
            ),
        }

    def analyze_with_attribution(
        self,
        config_id: int,
        events: list[dict[str, Any]],
        model_id: int = 1,
        touchpoints: list[dict[str, Any]] | None = None,
        overrides: dict[str, Any] | None = None,
        max_periods: int = 8,
    ) -> dict[str, Any]:
        """留存分析读取转化归因数据，按明示的周期口径计算渠道贡献。

        - 转化口径：同期群配置的「回访事件」即转化事件。
        - 归因口径：归因模型配置（可被 overrides 覆盖），账单里保留模型条件。
        - 周期口径：settle_basis=touch_week 按触点所在周期入列；
          settle_basis=return_week 按转化所在周期入列。两种口径分母相同。
        """
        cohort_config = self._config(config_id)
        if cohort_config is None:
            return {"ok": False, "message": f"同期群配置 {config_id} 不存在"}

        from app.services import service_for
        attribution_svc = service_for("attribution_model")
        if attribution_svc is None:
            return {"ok": False, "message": "归因服务不可用"}

        init_event = str(cohort_config.get("初始事件", ""))
        return_event = str(cohort_config.get("回访事件", ""))

        caliber = attribution_svc.model_caliber(model_id)
        if caliber is None:
            return {"ok": False, "message": f"归因模型 {model_id} 不存在"}
        if overrides:
            caliber = normalize_attribution_config({**caliber, **overrides})
        # 输出周期以本次结算口径为准；同期群默认周期只在口径未指定时兜底。
        period_days = caliber["period_days"] or self._period_days(cohort_config)

        # 1) 转化归因数据：同一回访重复提交只记一次权重。
        conversions = conversions_from_events(events, return_event)
        settlement = settle_conversion_attribution(conversions, touchpoints, caliber)

        # 2) 保留率矩阵沿用统一分母，渠道贡献矩阵与它按同期群行对齐。
        retention = compute_cohort_retention(
            events, init_event, return_event, period_days, max_periods,
        )
        attribution_rows = settlement.get("conversions", [])
        matrix = build_cohort_attribution_matrix(
            events, attribution_rows, init_event, caliber, max_periods,
            cohort_matrix=retention,
        )

        self.run_action(config_id, "完成分析")

        model_row = store.find("attribution_model", model_id) or {}
        return {
            "ok": True,
            "config_name": str(cohort_config.get("群组名称", "")),
            "init_event": init_event,
            "return_event": return_event,
            "periods": {
                "period_days": period_days,
                "period_labels": matrix["period_labels"],
                "settlement_basis": caliber["settle_basis"],
                "settlement_basis_label": (
                    "渠道按触达周结算" if caliber["settle_basis"] == "touch_week"
                    else "运营按回访周复盘"
                ),
                "denominator": "同期群初始事件用户数（去重）",
                "rate_formula": "周期归因权重 / 同期群分母 * 100",
                "model": caliber["model"],
                "window_days": caliber["window_days"],
                "period_unit": "周" if period_days == 7 else f"{period_days}天",
            },
            "attribution_caliber": settlement["caliber"],
            "attribution_model": {
                "model_id": model_id,
                "model_name": str(model_row.get("模型名称", "")),
                "attribution_window": model_row.get("归因窗口"),
                "overrides": overrides or {},
            },
            "attribution_overrides": overrides or {},
            "attribution_totals": settlement["totals"],
            "conversions": attribution_rows,
            "unattributed": settlement["unattributed"],
            "excluded_touchpoints": settlement.get("excluded_touchpoints", []),
            "attribution_periods": settlement["period_summary"],
            "channel_contributions": matrix["channel_contributions"],
            "cohort_ids": matrix["cohort_ids"],
            "denominators": matrix["denominators"],
            "cohort_attribution_matrix": matrix["rows"],
            "retention_matrix": retention.retention_matrix,
            "retention_period_labels": retention.period_labels,
            "unmatched_weight": matrix["unmatched_weight"],
            "outside_range_weight": matrix["outside_range_weight"],
        }

    def compare_cohort_retention(
        self, config_id_1: int, config_id_2: int,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """比较两个群组的保留率。"""
        result_1 = self.run_cohort_analysis(config_id_1, events)
        result_2 = self.run_cohort_analysis(config_id_2, events)
        if not result_1.get("ok") or not result_2.get("ok"):
            return {"ok": False, "message": "分析运行失败"}
        r1_avg = result_1.get("avg_retention_week1", 0)
        r2_avg = result_2.get("avg_retention_week1", 0)
        return {
            "ok": True,
            "config_1": result_1.get("config_name"),
            "config_2": result_2.get("config_name"),
            "retention_1_week1": r1_avg,
            "retention_2_week1": r2_avg,
            "difference": round(r2_avg - r1_avg, 2),
        }

    def get_retention_for_segment(
        self, segment_id: str, events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """按分群筛选事件后运行同期群分析（分群分析→同期群分析的跨模块消费）。"""
        from app.services import service_for
        segment_svc = service_for("segment_rule")
        if segment_svc is None:
            return {"ok": False, "message": "分群服务不可用"}
        segment = store.find("segment_rule", int(segment_id))
        if segment is None:
            return {"ok": False, "message": f"分群 {segment_id} 不存在"}
        seg_results = segment_svc.compute_segment(int(segment_id), events)
        matched_data = seg_results.get("sample", events)
        return self.run_cohort_analysis(1, matched_data)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "同期群分析数据集"
        rows = store.rows(self.MODULE)
        base["completed_analyses"] = sum(1 for row in rows if row.get("status") == "已完成")
        base["ensemble_periods"] = sorted({
            self._period_days(row) for row in rows
        })
        return base
