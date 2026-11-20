"""转化归因结算的回归测试：窗口、重复提交、周期口径与账单留痕。

运行：backend/.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics_engine import (  # noqa: E402
    compute_attribution,
    conversions_from_events,
    normalize_attribution_config,
    settle_conversion_attribution,
)
from app.main import (  # noqa: E402
    AttributionSettlementPayload,
    _sample_cross_period_events,
    _sample_cross_period_touchpoints,
    attribution_settlement,
)
from app.services import service_for  # noqa: E402

ANCHOR = 1698624000  # 2023-10-30 00:00 UTC，周一
WEEK = 7 * 86400


def run_scenario(model_id: int, overrides: dict | None = None, **extra):
    payload = AttributionSettlementPayload(
        model_id=model_id,
        config_id=1,
        model_overrides=overrides or {},
        **extra,
    )
    return attribution_settlement(payload)


def matrix_weights(response: dict) -> list[float]:
    row = response["cohort"]["cohort_attribution_matrix"][0]
    return [period["total_weight"] for period in row["periods"][:3]]


class SettlementRulesTest(unittest.TestCase):
    def test_window_config_is_normalized(self) -> None:
        caliber = normalize_attribution_config({
            "model": "time_decay",
            "window_days": "14",
            "period_days": "7",
            "settle_basis": "触达周",
        })
        self.assertEqual(caliber["model"], "time_decay")
        self.assertEqual(caliber["window_days"], 14.0)
        self.assertEqual(caliber["period_days"], 7)
        self.assertEqual(caliber["settle_basis"], "touch_week")

    def test_position_based_weights_use_configured_edges(self) -> None:
        touchpoints = [{"name": "a", "time": 0}, {"name": "b", "time": 10}]
        result = compute_attribution(
            touchpoints, "position_based",
            position_first_weight=0.4, position_last_weight=0.4,
        )
        self.assertEqual([round(w, 4) for w in result.weights], [0.5, 0.5])

    def test_late_touchpoints_are_invalid(self) -> None:
        result = settle_conversion_attribution(
            conversions=[{
                "conversion_id": "c1", "user_id": "u1", "conversion_time": 1000.0,
            }],
            touchpoints=[
                {"user_id": "u1", "channel": "合格", "time": 500.0},
                {"user_id": "u1", "channel": "迟到", "time": 1500.0},
            ],
            config={"model": "linear", "window_days": None},
        )
        self.assertEqual(result["totals"]["invalid_touchpoints"], 1)
        self.assertEqual([row["channel"] for row in result["conversions"]], ["合格"])
        self.assertEqual(
            result["excluded_touchpoints"][0]["reason"], "晚于转化时间",
        )

    def test_duplicate_conversion_submission_only_counts_once(self) -> None:
        conversion = {
            "conversion_id": "c1", "user_id": "u1", "conversion_time": 1000.0,
        }
        result = settle_conversion_attribution(
            conversions=[conversion, dict(conversion)],
            touchpoints=[{"user_id": "u1", "channel": "自然流量", "time": 500.0}],
            config={"model": "linear", "window_days": None},
        )
        self.assertEqual(result["totals"]["duplicate_submissions"], 1)
        self.assertEqual(result["totals"]["attributed_weight"], 1.0)
        self.assertEqual(len(result["conversions"]), 1)

    def test_duplicate_return_events_collapse_by_conversion_id(self) -> None:
        events = [
            {"user_id": "u1", "event_name": "登录", "event_time": 1000},
            {"user_id": "u1", "event_name": "登录", "event_time": 1000},
        ]
        conversions = conversions_from_events(events, "登录")
        self.assertEqual(len({item["conversion_id"] for item in conversions}), 1)

    def test_touchpoint_outside_window_is_excluded(self) -> None:
        result = settle_conversion_attribution(
            conversions=[{
                "conversion_id": "c1", "user_id": "u1", "conversion_time": 10 * 86400,
            }],
            touchpoints=[{"user_id": "u1", "channel": "旧触点", "time": 0}],
            config={"model": "linear", "window_days": 7},
        )
        self.assertEqual(result["totals"]["attributed_weight"], 0.0)
        self.assertEqual(result["unattributed"][0]["conversion_id"], "c1")


class CrossPeriodScenarioTest(unittest.TestCase):
    """同一笔转化落在第二个留存周期，两个合格触点分别在首期和第二期。"""

    def test_return_week_basis_keeps_denominator_and_splits_periods(self) -> None:
        response = run_scenario(1, {"settle_basis": "return_week"})
        cohort = response["cohort"]
        row = cohort["cohort_attribution_matrix"][0]
        self.assertEqual(row["denominator"], 2)
        self.assertEqual(cohort["denominators"], [2])
        self.assertEqual(matrix_weights(response), [1.0, 1.0, 0.0])
        self.assertEqual(row["periods"][1]["channels"], {"内容社区": 1.0, "短信召回": 0.0})

    def test_touch_week_basis_moves_weight_without_touching_denominator(self) -> None:
        response = run_scenario(1, {"settle_basis": "touch_week"})
        cohort = response["cohort"]
        self.assertEqual(cohort["denominators"], [2])
        self.assertEqual(matrix_weights(response), [2.0, 0.0, 0.0])
        self.assertEqual(
            response["reconcile"]["attribution_weight_total"],
            response["reconcile"]["cohort_weight_total"],
        )

    def test_u_shape_model_splits_cross_period_conversion(self) -> None:
        response = run_scenario(3)
        row = response["cohort"]["cohort_attribution_matrix"][0]
        self.assertEqual(row["periods"][0]["channels"], {"信息流广告": 0.5, "搜索广告": 0.5})
        self.assertEqual(row["periods"][1]["channels"], {"内容社区": 0.5, "短信召回": 0.5})

    def test_window_threshold_drops_older_touchpoints(self) -> None:
        response = run_scenario(2, {"window_days": 1})
        channels = response["settlement"]["conversions"]
        self.assertNotIn("搜索广告", {row["channel"] for row in channels})
        self.assertEqual(
            min(row["weight"] for row in channels if row["user_id"] == "u2"), 1.0,
        )

    def test_duplicate_and_late_touchpoints_are_reported(self) -> None:
        response = run_scenario(1)
        totals = response["settlement"]["totals"]
        self.assertEqual(totals["submitted_conversions"], 3)
        self.assertEqual(totals["unique_conversions"], 2)
        self.assertEqual(totals["duplicate_submissions"], 1)
        self.assertEqual(totals["invalid_touchpoints"], 1)
        self.assertEqual(
            response["settlement"]["excluded_touchpoints"][0]["channel"], "线下活动",
        )


class BillRetentionTest(unittest.TestCase):
    def test_bill_keeps_model_conditions(self) -> None:
        service = service_for("attribution_model")
        run_scenario(1, {"window_days": 5, "settle_basis": "touch_week"})
        bills = service.list_bills(1)
        bill = bills["bills"][0]
        self.assertEqual(bill["model_conditions"]["window_days"], 5.0)
        self.assertEqual(bill["model_conditions"]["settle_basis"], "touch_week")
        self.assertEqual(bill["model_conditions"]["model"], "first_touch")
        self.assertEqual(bill["overrides"]["window_days"], 5)

    def test_recompute_under_new_window_creates_new_bill(self) -> None:
        service = service_for("attribution_model")
        before = service.list_bills(1)["total"]
        run_scenario(1, {"window_days": 14})
        run_scenario(1, {"window_days": 3})
        self.assertEqual(service.list_bills(1)["total"], before + 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
