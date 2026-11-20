"""归因模型领域服务：触点权重分配、窗口结算、多模型比较。"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import (
    compute_attribution,
    normalize_attribution_config,
    settle_conversion_attribution,
)


def _parse_window_days(value: Any) -> float | None:
    """把「30天」「7d」「natural」之类的窗口配置收敛成天数。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in ("不限", "all", "无", "none", "natural"):
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    days = float(match.group(1))
    if "小时" in text or text.endswith("h"):
        days = days / 24
    elif "周" in text or text.endswith("w"):
        days = days * 7
    return max(0.0, days)


class AttributionModelService(BaseModuleService):
    MODULE = "attribution_model"
    ENTITY = "归因配置"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["模型名称", "归因对象", "归因窗口", "触点权重", "衰减方式", "首次归因", "末次归因"]
    REQUIRED_FIELDS = ["模型名称", "归因对象", "归因窗口"]
    STATUS_ORDER = ["草稿", "计算中", "已生效", "已过期"]
    ACTION_RULES = {"开始计算": "计算中", "生效模型": "已生效", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = ["标记过期"]
    EXTRA_FIELDS = ["结算基数", "归因窗口天数", "留存周期天数"]

    def __init__(self) -> None:
        # 结算账单：每次重算都留存一份模型条件和口径，供按周期对账。
        self._bills: list[dict[str, Any]] = []
        self._bill_seq = 0

    # ---- 业务方法 ----

    def model_caliber(self, model_id: int) -> dict[str, Any] | None:
        """从归因配置解析出结算口径，供账单和留存分析复用。"""
        model = store.find(self.MODULE, model_id)
        if model is None:
            return None

        decay_method = str(model.get("衰减方式") or "linear").strip().lower()
        first_attr = str(model.get("首次归因") or "").strip()
        last_attr = str(model.get("末次归因") or "").strip()

        first_weight = 0.4
        last_weight = 0.4
        for attr, target in ((first_attr, "first"), (last_attr, "last")):
            if attr:
                try:
                    value = float(attr)
                except ValueError:
                    continue
                if value > 1:
                    value = value / 100
                if target == "first":
                    first_weight = value
                else:
                    last_weight = value

        if "decay" in decay_method or ("衰减" in decay_method and "无衰减" not in decay_method):
            model_type = "time_decay"
        elif first_attr and last_attr:
            first_positive = first_weight > 0
            last_positive = last_weight > 0
            if first_positive and last_positive:
                model_type = "position_based"
                if first_weight >= 1.0 and last_weight <= 0.0:
                    model_type = "first_touch"
                elif last_weight >= 1.0 and first_weight <= 0.0:
                    model_type = "last_touch"
            elif last_weight >= 1.0:
                model_type = "last_touch"
            elif first_weight >= 1.0:
                model_type = "first_touch"
            else:
                model_type = "linear"
        elif first_attr and first_weight >= 1.0:
            model_type = "first_touch"
        elif last_attr and last_weight >= 1.0:
            model_type = "last_touch"
        else:
            model_type = "linear"

        basis_text = str(model.get("结算基数", model.get("复盘口径", ""))).lower()
        settle_basis = "touch_week" if "触" in basis_text or "touch" in basis_text else "return_week"

        window_value = model.get("归因窗口天数", model.get("归因窗口"))
        return normalize_attribution_config({
            "model": model_type,
            "window_days": _parse_window_days(window_value),
            "position_first_weight": first_weight,
            "position_last_weight": last_weight,
            "period_days": model.get("留存周期天数") or 7,
            "settle_basis": settle_basis,
        })

    def compute_attribution_weights(
        self, model_id: int, touchpoints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """计算归因权重分配。"""
        caliber = self.model_caliber(model_id)
        if caliber is None:
            return {"ok": False, "message": f"归因模型 {model_id} 不存在"}

        model = store.find(self.MODULE, model_id) or {}
        result = compute_attribution(
            touchpoints,
            caliber["model"],
            caliber["decay_half_life_days"],
            position_first_weight=caliber["position_first_weight"],
            position_last_weight=caliber["position_last_weight"],
        )

        self.run_action(model_id, "生效模型")

        return {
            "ok": True,
            "model_name": str(model.get("模型名称", "")),
            "model_type": caliber["model"],
            "attribution_window": model.get("归因窗口"),
            "caliber": caliber,
            "touchpoints": result.touchpoints,
            "weights": [round(w, 4) for w in result.weights],
            "top_channel": result.touchpoints[result.weights.index(max(result.weights))] if result.weights else "",
            "top_weight": round(max(result.weights), 4) if result.weights else 0,
        }

    def settle_conversions(
        self,
        model_id: int,
        conversions: list[dict[str, Any]],
        touchpoints: list[dict[str, Any]] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按模型口径（可被本次入参覆盖）重算转化归因数据，并保留模型条件。"""
        caliber = self.model_caliber(model_id)
        if caliber is None:
            return {"ok": False, "message": f"归因模型 {model_id} 不存在"}

        effective = {**caliber, **(overrides or {})}
        normalized = normalize_attribution_config(effective)
        result = settle_conversion_attribution(conversions, touchpoints, normalized)
        model_row = store.find(self.MODULE, model_id) or {}
        result["model"] = {
            "model_id": model_id,
            "model_name": str(model_row.get("模型名称", "")),
            "attribution_window": model_row.get("归因窗口"),
        }
        result["overrides"] = overrides or {}

        self._bill_seq += 1
        bill = {
            "bill_id": f"ATTR-{self._bill_seq:04d}",
            "model_id": model_id,
            "model_name": str(model_row.get("模型名称", "")),
            "model_conditions": {
                "model": normalized["model"],
                "window_days": normalized["window_days"],
                "decay_half_life_days": normalized["decay_half_life_days"],
                "position_first_weight": normalized["position_first_weight"],
                "position_last_weight": normalized["position_last_weight"],
                "period_days": normalized["period_days"],
                "settle_basis": normalized["settle_basis"],
            },
            "overrides": overrides or {},
            "totals": result["totals"],
            "period_summary": result["period_summary"],
        }
        self._bills.append(bill)
        result["bill"] = bill
        result["bill_id"] = bill["bill_id"]
        return result

    def list_bills(self, limit: int = 50) -> dict[str, Any]:
        """查看历次结算账单，每条都带当次使用的模型条件。"""
        bills = self._bills[-max(1, limit):]
        return {"ok": True, "total": len(self._bills), "bills": list(reversed(bills))}

    def get_bill(self, bill_id: str) -> dict[str, Any] | None:
        for bill in self._bills:
            if bill["bill_id"] == bill_id:
                return bill
        return None

    def compare_models(
        self, model_ids: list[int], touchpoints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """比较多个归因模型的权重分配差异。"""
        comparisons: list[dict[str, Any]] = []
        for mid in model_ids:
            r = self.compute_attribution_weights(mid, touchpoints)
            if r.get("ok"):
                comparisons.append({
                    "model_name": r.get("model_name"),
                    "model_type": r.get("model_type"),
                    "weights": r.get("weights"),
                })
        # 计算各模型权重差异
        diff_metrics: dict[str, Any] = {}
        if len(comparisons) >= 2:
            diffs: list[float] = []
            for i in range(len(comparisons[0]["weights"])):
                values = [c["weights"][i] for c in comparisons if c.get("weights")]
                diffs.append(max(values) - min(values))
            diff_metrics = {
                "max_difference": round(max(diffs), 4) if diffs else 0,
                "avg_difference": round(sum(diffs) / len(diffs), 4) if diffs else 0,
            }

        return {
            "ok": True,
            "comparisons": comparisons,
            "touchpoint_count": len(touchpoints),
            "cross_model_diff": diff_metrics,
        }

    def get_active_models(self) -> list[dict[str, Any]]:
        """获取已生效的归因模型（供分群分析消费）。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "已生效"
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "归因模型数据集"
        base["active_models"] = sum(1 for row in store.rows(self.MODULE) if row.get("status") == "已生效")
        return base
