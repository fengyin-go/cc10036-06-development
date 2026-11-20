"""多维分析查询引擎：提供跨模块共享的聚合、归因、保留、检测和血缘计算核心。

所有计算方法均为纯函数，不依赖 Store，方便按业务模块组合调用。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 通用聚合函数
# ---------------------------------------------------------------------------

AGGREGATORS: dict[str, Callable[[list[float | int]], float]] = {
    "SUM": lambda values: sum(values),
    "AVG": lambda values: sum(values) / len(values) if values else 0.0,
    "COUNT": lambda values: float(len(values)),
    "MAX": lambda values: float(max(values)) if values else 0.0,
    "MIN": lambda values: float(min(values)) if values else 0.0,
    "DISTINCT_COUNT": lambda values: float(len(set(values))),
    "WEIGHTED_AVG": lambda values, weights=None: _weighted_avg(values, weights or []),
}


def _weighted_avg(values: list[float | int], weights: list[float | int]) -> float:
    if not values or not weights or len(values) != len(weights):
        return 0.0
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total_weight


def compute_metric(values: list[float | int], aggregator: str) -> float:
    """按聚合函数计算指标值，口径统一入口。"""
    func = AGGREGATORS.get(aggregator)
    if func is None:
        raise ValueError(f"不支持的聚合函数：{aggregator}")
    return func(values)


# ---------------------------------------------------------------------------
# 多维立方预计算
# ---------------------------------------------------------------------------

@dataclass
class DimensionCombo:
    """维度组合记录。"""
    dimensions: list[str]
    measure_values: dict[str, float]
    row_count: int = 0


def build_cube_cell(
    raw_data: list[dict[str, Any]],
    dimensions: list[str],
    measures: list[dict[str, str]],  # [{"name": "revenue", "aggregator": "SUM"}, ...]
) -> list[DimensionCombo]:
    """从原始数据构建多维立方单元格，按维度组合分组聚合度量。"""
    group_map: dict[tuple[str, ...], dict[str, list[float | int]]] = {}
    for row in raw_data:
        dim_key = tuple(str(row.get(d, "")) for d in dimensions)
        if dim_key not in group_map:
            group_map[dim_key] = {m["name"]: [] for m in measures}
        for m in measures:
            val = row.get(m["name"])
            if val is not None:
                try:
                    group_map[dim_key][m["name"]].append(float(val))
                except (ValueError, TypeError):
                    pass

    results: list[DimensionCombo] = []
    for dim_key, measure_data in group_map.items():
        measure_values: dict[str, float] = {}
        total_rows = max(len(v) for v in measure_data.values()) if measure_data else 0
        for m in measures:
            values = measure_data.get(m["name"], [])
            measure_values[m["name"]] = compute_metric(values, m["aggregator"])
        results.append(DimensionCombo(
            dimensions=list(dim_key),
            measure_values=measure_values,
            row_count=total_rows,
        ))
    return results


def generate_dimension_combinations(dimensions: list[str]) -> list[list[str]]:
    """生成所有维度组合（用于预计算Cube），含全量组合。"""
    from itertools import combinations
    combos: list[list[str]] = []
    for r in range(1, len(dimensions) + 1):
        for combo in combinations(dimensions, r):
            combos.append(list(combo))
    return combos


# ---------------------------------------------------------------------------
# 分群条件解析
# ---------------------------------------------------------------------------

@dataclass
class FilterCondition:
    field: str
    operator: str  # eq, ne, gt, lt, gte, lte, in, not_in, contains, starts_with
    value: Any


def parse_segment_conditions(condition_expr: str) -> list[FilterCondition]:
    """解析分群筛选条件表达式。格式：field:op:value;field:op:value"""
    conditions: list[FilterCondition] = []
    if not condition_expr:
        return conditions
    for part in condition_expr.split(";"):
        part = part.strip()
        if not part:
            continue
        components = part.split(":", 2)
        if len(components) >= 3:
            conditions.append(FilterCondition(
                field=components[0].strip(),
                operator=components[1].strip(),
                value=components[2].strip(),
            ))
    return conditions


def apply_filter(row: dict[str, Any], condition: FilterCondition) -> bool:
    """对单行数据应用筛选条件。"""
    cell_val = row.get(condition.field)
    op = condition.operator
    val = condition.value

    try:
        if op == "eq":
            return str(cell_val) == str(val)
        elif op == "ne":
            return str(cell_val) != str(val)
        elif op in ("gt", "gte", "lt", "lte"):
            cell_num = float(cell_val) if cell_val is not None else 0
            val_num = float(val)
            if op == "gt":
                return cell_num > val_num
            elif op == "gte":
                return cell_num >= val_num
            elif op == "lt":
                return cell_num < val_num
            elif op == "lte":
                return cell_num <= val_num
        elif op == "in":
            allowed = set(v.strip() for v in str(val).split(","))
            return str(cell_val) in allowed
        elif op == "not_in":
            denied = set(v.strip() for v in str(val).split(","))
            return str(cell_val) not in denied
        elif op == "contains":
            return str(val).casefold() in str(cell_val).casefold()
        elif op == "starts_with":
            return str(cell_val or "").startswith(str(val))
        elif op == "between":
            bounds = str(val).split(",")
            if len(bounds) == 2:
                cell_num = float(cell_val) if cell_val is not None else 0
                return float(bounds[0]) <= cell_num <= float(bounds[1])
        return False
    except (ValueError, TypeError):
        return False


def segment_population(
    population: list[dict[str, Any]],
    include_conditions: list[FilterCondition],
    exclude_conditions: list[FilterCondition],
    intersect_strategy: str = "all",  # all / any
) -> tuple[list[dict[str, Any]], int]:
    """对人群执行分群圈选，返回命中的行和命中数。"""
    results: list[dict[str, Any]] = []
    for row in population:
        if include_conditions:
            if intersect_strategy == "all":
                if not all(apply_filter(row, c) for c in include_conditions):
                    continue
            else:
                if not any(apply_filter(row, c) for c in include_conditions):
                    continue
        if exclude_conditions and any(apply_filter(row, c) for c in exclude_conditions):
            continue
        results.append(row)
    return results, len(results)


# ---------------------------------------------------------------------------
# 同期群保留率计算
# ---------------------------------------------------------------------------

@dataclass
class CohortMatrix:
    """同期群矩阵：行=同期群（按初始事件时间划分），列=周期偏移。"""
    cohort_ids: list[str]
    period_labels: list[str]
    retention_matrix: list[list[float]]  # [cohort_index][period_index] = 保留率


def compute_cohort_retention(
    events: list[dict[str, Any]],
    init_event_name: str,
    return_event_name: str,
    period_days: int = 7,
    max_periods: int = 8,
) -> CohortMatrix:
    """计算同期群保留率矩阵。

    events: 每行 {user_id, event_name, event_time(epoch_seconds)}
    返回保留率矩阵（百分比 0-100）。
    """
    # 按user分组事件
    user_events: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in events:
        uid = str(e.get("user_id", ""))
        ename = str(e.get("event_name", ""))
        etime = float(e.get("event_time", 0))
        if uid:
            user_events[uid].append((ename, etime))

    # 为每个用户找到首次初始事件时间和回访事件时间
    user_cohorts: dict[str, float] = {}  # user -> first init time
    user_return_times: dict[str, list[float]] = defaultdict(list)
    for uid, evts in user_events.items():
        first_init: float | None = None
        returns: list[float] = []
        for ename, etime in sorted(evts, key=lambda x: x[1]):
            if first_init is None and ename == init_event_name:
                first_init = etime
            elif first_init is not None and ename == return_event_name and etime > first_init:
                returns.append(etime)
        if first_init is not None:
            user_cohorts[uid] = first_init
            user_return_times[uid] = returns

    # 按周分组同期群（与归因结算共用同一周期对齐口径）
    period_seconds = period_days * 86400
    cohort_buckets: dict[int, set[str]] = defaultdict(set)  # week_number -> set of user_ids
    for uid, init_time in user_cohorts.items():
        week = _period_index(init_time, period_days)
        cohort_buckets[week].add(uid)

    sorted_weeks = sorted(cohort_buckets)
    cohort_ids = [_period_label(w, period_days) for w in sorted_weeks]
    matrix: list[list[float]] = []

    for wi, week in enumerate(sorted_weeks):
        users_in_cohort = cohort_buckets[week]
        total = len(users_in_cohort)
        if total == 0:
            matrix.append([0.0] * max_periods)
            continue
        cohort_start = _period_start(week, period_days)
        period_rates: list[float] = []
        for p in range(max_periods):
            period_end = cohort_start + (p + 1) * period_seconds
            retained = 0
            for uid in users_in_cohort:
                return_times = user_return_times.get(uid, [])
                if any(cohort_start < rt <= period_end for rt in return_times):
                    retained += 1
            rate = round(retained / total * 100, 2)
            period_rates.append(rate)
        matrix.append(period_rates)

    period_labels = _period_axis_labels(period_days, max_periods)
    return CohortMatrix(
        cohort_ids=cohort_ids,
        period_labels=period_labels,
        retention_matrix=matrix,
    )


def _week_label(epoch_seconds: float, period_days: int) -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)
    return dt.strftime("W%W-%Y")


# ---------------------------------------------------------------------------
# 归因权重分配
# ---------------------------------------------------------------------------

@dataclass
class AttributionResult:
    """归因结果：每个触点的权重分配。"""
    touchpoints: list[str]
    weights: list[float]
    model: str  # first_touch / last_touch / linear / time_decay / position_based


def compute_attribution(
    touchpoints: list[dict[str, Any]],
    model: str = "linear",
    decay_half_life_days: float = 7.0,
    *,
    position_first_weight: float = 0.4,
    position_last_weight: float = 0.4,
) -> AttributionResult:
    """计算归因权重分配。

    touchpoints: [{"name": str, "time": epoch_seconds}, ...]
    模型支持：first_touch, last_touch, linear, time_decay, position_based
    position_based 的首次/末次权重可由模型配置覆盖，中间触点均分剩余权重。
    """
    if not touchpoints:
        return AttributionResult(touchpoints=[], weights=[], model=model)

    tp_names = [tp["name"] for tp in touchpoints]
    tp_times = [float(tp.get("time", 0)) for tp in touchpoints]
    n = len(touchpoints)

    if model == "first_touch":
        weights = [1.0 if i == 0 else 0.0 for i in range(n)]
    elif model == "last_touch":
        weights = [1.0 if i == n - 1 else 0.0 for i in range(n)]
    elif model == "time_decay":
        max_time = max(tp_times) if tp_times else 0
        half_life = decay_half_life_days * 86400
        raw: list[float] = []
        for t in tp_times:
            delta = max_time - t
            raw.append(2.0 ** (-delta / half_life) if half_life > 0 else 1.0)
        raw_sum = sum(raw)
        weights = [r / raw_sum if raw_sum > 0 else 1.0 / n for r in raw]
    elif model == "position_based":
        if n == 1:
            weights = [1.0]
        else:
            first_weight = max(0.0, float(position_first_weight))
            last_weight = max(0.0, float(position_last_weight))
            # 首末权重之和不超过1，剩余部分留给中间触点。
            remaining = max(0.0, 1.0 - first_weight - last_weight)
            middle_count = n - 2
            middle_share = remaining / middle_count if middle_count > 0 else 0.0
            weights = []
            for i in range(n):
                if i == 0:
                    weights.append(first_weight)
                elif i == n - 1:
                    weights.append(last_weight)
                else:
                    weights.append(middle_share)
    else:  # linear
        weights = [1.0 / n] * n

    # 归一化
    total = sum(weights)
    if total > 0:
        weights = [w / total for w in weights]

    return AttributionResult(touchpoints=tp_names, weights=weights, model=model)


# ---------------------------------------------------------------------------
# 转化归因结算：按归因窗口关联触点与回访（转化）事件
# ---------------------------------------------------------------------------

ATTRIBUTION_MODELS = (
    "first_touch", "last_touch", "linear", "time_decay", "position_based",
)
SETTLEMENT_BASES = ("touch_week", "return_week")
DEFAULT_ATTRIBUTION_WINDOW_DAYS = 7.0


def normalize_attribution_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """收敛归因结算口径，保证结算账单里的模型条件可复现。

    支持字段：
    - model: first_touch / last_touch / linear / time_decay / position_based
    - window_days: 归因窗口阈值（天），None 表示不设上限
    - decay_half_life_days: 时间衰减半衰期（天）
    - position_first_weight / position_last_weight: U 型首末权重
    - period_days: 留存周期长度（天）
    - settle_basis: touch_week（渠道按触达周结算）/ return_week（运营按回访周复盘）
    """
    raw = dict(config or {})
    model = str(raw.get("model", "linear")).strip().lower()
    if model not in ATTRIBUTION_MODELS:
        model = "linear"

    window_raw = raw.get("window_days", DEFAULT_ATTRIBUTION_WINDOW_DAYS)
    if window_raw is None:
        window_days: float | None = None
    else:
        try:
            window_days = max(0.0, float(window_raw))
        except (TypeError, ValueError):
            window_days = DEFAULT_ATTRIBUTION_WINDOW_DAYS

    try:
        half_life = max(0.0, float(raw.get("decay_half_life_days", 7.0)))
    except (TypeError, ValueError):
        half_life = 7.0

    try:
        first_weight = min(1.0, max(0.0, float(raw.get("position_first_weight", 0.4))))
    except (TypeError, ValueError):
        first_weight = 0.4
    try:
        last_weight = min(1.0, max(0.0, float(raw.get("position_last_weight", 0.4))))
    except (TypeError, ValueError):
        last_weight = 0.4

    try:
        period_days = max(1, int(raw.get("period_days", 7)))
    except (TypeError, ValueError):
        period_days = 7
    basis = str(raw.get("settle_basis", "return_week")).strip().lower()
    if basis not in SETTLEMENT_BASES:
        basis = "touch_week" if basis in ("触达周", "touch") else "return_week"

    return {
        "model": model,
        "window_days": window_days,
        "decay_half_life_days": half_life,
        "position_first_weight": first_weight,
        "position_last_weight": last_weight,
        "period_days": period_days,
        "settle_basis": basis,
    }


# 周周期对齐到周一：epoch 0 是周四，向右平移 3 天即可让 7 天窗口从周一开始。
_PERIOD_ALIGN_DAYS = 3


def _period_offset_seconds(period_days: int) -> int:
    return _PERIOD_ALIGN_DAYS * 86400 if period_days == 7 else 0


def _period_index(epoch_seconds: float, period_days: int) -> int:
    period_seconds = max(1, period_days) * 86400
    return int((epoch_seconds + _period_offset_seconds(period_days)) // period_seconds)


def _period_start(period_index: int, period_days: int) -> float:
    period_seconds = max(1, period_days) * 86400
    return period_index * period_seconds - _period_offset_seconds(period_days)


def _period_label(period_index: int, period_days: int) -> str:
    start = _period_start(period_index, period_days)
    if period_days == 7:
        return _week_label(start, period_days)
    import datetime
    begin = datetime.datetime.fromtimestamp(start, tz=datetime.timezone.utc)
    return f"{begin:%Y%m%d}+{period_days}天"


def _period_axis_labels(period_days: int, max_periods: int) -> list[str]:
    """周期轴标签：7 天口径用「周N」，其余口径直接写明天数。"""
    if period_days == 7:
        return [f"周{i + 1}" for i in range(max_periods)]
    return [f"{period_days * (i + 1)}天" for i in range(max_periods)]


def conversions_from_events(
    events: list[dict[str, Any]], return_event_name: str,
) -> list[dict[str, Any]]:
    """把回访事件转成转化记录，供归因结算使用。

    重复提交的回访会在这里保留为多条，由结算层按 conversion_id 去重并计数，
    这样账单能明确记录「重复提交只记一次权重」。
    """
    conversions: list[dict[str, Any]] = []
    for e in events or []:
        if str(e.get("event_name", "")) != return_event_name:
            continue
        uid = str(e.get("user_id", ""))
        if not uid:
            continue
        try:
            ctime = float(e.get("event_time", 0))
        except (TypeError, ValueError):
            continue
        conversions.append({
            "conversion_id": str(e.get("conversion_id") or f"{uid}:{int(ctime)}:{return_event_name}"),
            "user_id": uid,
            "conversion_time": ctime,
            "conversion_name": return_event_name,
        })
    return conversions


def _dedupe_touchpoints(
    touchpoints: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    duplicates = 0
    for tp in touchpoints:
        uid = str(tp.get("user_id", ""))
        channel = str(tp.get("channel") or tp.get("name") or "")
        time_raw = tp.get("time", tp.get("touch_time"))
        try:
            ttime = float(time_raw)
        except (TypeError, ValueError):
            unique.append(tp)
            continue
        key = (uid, int(ttime), channel)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        normalized = dict(tp)
        normalized["user_id"] = uid
        normalized["channel"] = channel
        normalized["time"] = ttime
        unique.append(normalized)
    return unique, duplicates


def settle_conversion_attribution(
    conversions: list[dict[str, Any]],
    touchpoints: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按归因窗口把触点权重结算到每一笔转化，生成可周期对账的明细。

    结算规则：
    1. 只有 touch_time <= conversion_time 且 conversion_time - touch_time 不超过
       归因窗口的触点才算合格触点；晚于转化的触点一律无效。
    2. 同一 conversion_id（回访）重复提交只结算一次权重。
    3. settle_basis=touch_week 时，权重落在触点所在周期（渠道按触达周结算）；
       settle_basis=return_week 时，权重全部落在转化所在周期（运营按回访周复盘）。
    """
    cfg = normalize_attribution_config(config)
    period_days = cfg["period_days"]
    window = cfg["window_days"]
    window_seconds = None if window is None else window * 86400
    basis = cfg["settle_basis"]

    incoming = [dict(tp) for tp in (touchpoints or [])]
    for conv in conversions or []:
        for tp in conv.get("touchpoints") or []:
            attached = dict(tp)
            attached.setdefault("user_id", conv.get("user_id"))
            incoming.append(attached)
    unique_tps, duplicate_touchpoints = _dedupe_touchpoints(incoming)

    tp_by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tp in unique_tps:
        tp_by_user[str(tp.get("user_id", ""))].append(tp)

    seen_conversions: set[str] = set()
    rows: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    excluded_touchpoints: list[dict[str, Any]] = []
    invalid_touchpoints = 0
    duplicate_submissions = 0
    attributed_conversions = 0

    for conv in conversions or []:
        uid = str(conv.get("user_id", ""))
        try:
            ctime = float(conv.get("conversion_time", conv.get("time", 0)))
        except (TypeError, ValueError):
            continue
        conversion_id = str(
            conv.get("conversion_id") or f"{uid}:{int(ctime)}:{conv.get('conversion_name', '')}"
        )
        if conversion_id in seen_conversions:
            duplicate_submissions += 1
            continue
        seen_conversions.add(conversion_id)

        qualified: list[dict[str, Any]] = []
        for tp in tp_by_user.get(uid, []):
            ttime = tp.get("time")
            if ttime is None:
                invalid_touchpoints += 1
                excluded_touchpoints.append({
                    "conversion_id": conversion_id, "channel": tp.get("channel", ""),
                    "touch_time": None, "reason": "触点缺少时间",
                })
                continue
            ttime = float(ttime)
            if ttime > ctime:
                # 晚于转化的触点无效，不能参与归因。
                invalid_touchpoints += 1
                excluded_touchpoints.append({
                    "conversion_id": conversion_id, "channel": tp.get("channel", ""),
                    "touch_time": ttime, "reason": "晚于转化时间",
                })
                continue
            if window_seconds is not None and ctime - ttime > window_seconds:
                invalid_touchpoints += 1
                excluded_touchpoints.append({
                    "conversion_id": conversion_id, "channel": tp.get("channel", ""),
                    "touch_time": ttime, "reason": f"超出归因窗口（{window:g}天）",
                })
                continue
            qualified.append({"name": str(tp.get("channel", "")), "time": ttime})

        if not qualified:
            unattributed.append({
                "conversion_id": conversion_id,
                "user_id": uid,
                "conversion_time": ctime,
                "reason": "窗口内无合格触点",
            })
            continue

        qualified.sort(key=lambda tp: tp["time"])
        result = compute_attribution(
            qualified,
            model=cfg["model"],
            decay_half_life_days=cfg["decay_half_life_days"],
            position_first_weight=cfg["position_first_weight"],
            position_last_weight=cfg["position_last_weight"],
        )
        attributed_conversions += 1
        conversion_period = _period_index(ctime, period_days)

        for tp, weight in zip(qualified, result.weights):
            touch_period = _period_index(tp["time"], period_days)
            settlement_period = touch_period if basis == "touch_week" else conversion_period
            rows.append({
                "conversion_id": conversion_id,
                "user_id": uid,
                "conversion_time": ctime,
                "conversion_period": conversion_period,
                "conversion_period_label": _period_label(conversion_period, period_days),
                "channel": tp["name"],
                "touch_time": tp["time"],
                "touch_period": touch_period,
                "touch_period_label": _period_label(touch_period, period_days),
                "settlement_period": settlement_period,
                "settlement_period_label": _period_label(settlement_period, period_days),
                "weight": round(weight, 6),
                "model": cfg["model"],
                "window_days": window,
                "settle_basis": basis,
            })

    rows.sort(key=lambda row: (row["settlement_period"], row["conversion_id"], row["touch_time"]))

    period_buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        bucket = period_buckets.setdefault(row["settlement_period"], {
            "period_index": row["settlement_period"],
            "period_label": row["settlement_period_label"],
            "total_weight": 0.0,
            "channels": defaultdict(float),
            "conversion_ids": set(),
        })
        bucket["total_weight"] += row["weight"]
        bucket["channels"][row["channel"]] += row["weight"]
        bucket["conversion_ids"].add(row["conversion_id"])

    period_summary: list[dict[str, Any]] = []
    for period_index in sorted(period_buckets):
        bucket = period_buckets[period_index]
        total = round(bucket["total_weight"], 6)
        channels = {
            channel: round(weight, 6)
            for channel, weight in sorted(
                bucket["channels"].items(), key=lambda item: (-item[1], item[0]),
            )
        }
        period_summary.append({
            "period_index": period_index,
            "period_label": bucket["period_label"],
            "conversion_count": len(bucket["conversion_ids"]),
            "total_weight": total,
            "channels": channels,
            "channel_share": {
                channel: round(weight / total * 100, 2) if total else 0.0
                for channel, weight in channels.items()
            },
        })

    return {
        "ok": True,
        "caliber": {
            **cfg,
            "window_rule": "触点时间 <= 转化时间，且转化时间 - 触点时间 <= 归因窗口",
            "settlement_basis_label": (
                "渠道按触达周结算" if basis == "touch_week" else "运营按回访周复盘"
            ),
            "dedup_rule": "同一 conversion_id 重复提交只结算一次权重",
        },
        "conversions": rows,
        "unattributed": unattributed,
        "excluded_touchpoints": excluded_touchpoints,
        "period_summary": period_summary,
        "totals": {
            "submitted_conversions": len(conversions or []),
            "unique_conversions": len(seen_conversions),
            "attributed_conversions": attributed_conversions,
            "unattributed_conversions": len(unattributed),
            "duplicate_submissions": duplicate_submissions,
            "touchpoints": len(unique_tps),
            "duplicate_touchpoints": duplicate_touchpoints,
            "invalid_touchpoints": invalid_touchpoints,
            "attributed_weight": round(sum(row["weight"] for row in rows), 6),
        },
    }


def cohort_assignments(
    events: list[dict[str, Any]],
    init_event_name: str,
    period_days: int = 7,
) -> dict[str, dict[str, Any]]:
    """按用户首次初始事件划分同期群，返回用户 → 群组周期。"""
    first_init: dict[str, float] = {}
    for e in events or []:
        if str(e.get("event_name", "")) != init_event_name:
            continue
        uid = str(e.get("user_id", ""))
        if not uid:
            continue
        try:
            etime = float(e.get("event_time", 0))
        except (TypeError, ValueError):
            continue
        if uid not in first_init or etime < first_init[uid]:
            first_init[uid] = etime
    return {
        uid: {
            "cohort_time": t,
            "cohort_period": _period_index(t, period_days),
            "cohort_id": _period_label(_period_index(t, period_days), period_days),
        }
        for uid, t in first_init.items()
    }


def build_cohort_attribution_matrix(
    events: list[dict[str, Any]],
    attribution_rows: list[dict[str, Any]],
    init_event_name: str,
    config: dict[str, Any] | None = None,
    max_periods: int = 8,
    cohort_matrix: CohortMatrix | None = None,
) -> dict[str, Any]:
    """留存分析读取转化归因明细，按明示的周期口径计算渠道贡献矩阵。

    分母固定为同期群初始事件用户数（去重），与结算口径无关：
    - touch_week：权重按触点所在周期入列（渠道按触达周结算）
    - return_week：权重按转化所在周期入列（运营按回访周复盘）
    这样同一笔跨期转化只改变列归属，不改变任何周期的分母。
    传入 cohort_matrix 时，群组行与保留率矩阵的 cohort_ids 一一对齐。
    """
    cfg = normalize_attribution_config(config)
    period_days = cfg["period_days"]
    period_seconds = period_days * 86400
    period_labels = _period_axis_labels(period_days, max_periods)
    assignments = cohort_assignments(events, init_event_name, period_days)
    if not assignments:
        return {
            "ok": True,
            "caliber": {**cfg, "denominator": "同期群初始事件用户数（去重）"},
            "cohort_ids": [],
            "period_labels": period_labels,
            "denominators": [],
            "rows": [],
            "channel_contributions": [],
            "unmatched_weight": 0.0,
            "outside_range_weight": 0.0,
        }

    if cohort_matrix is not None and cohort_matrix.cohort_ids:
        cohort_ids = list(cohort_matrix.cohort_ids)
        cohort_starts = [
            _period_start(cohort_period, period_days)
            for cohort_period in sorted({item["cohort_period"] for item in assignments.values()})
        ]
    else:
        starts = sorted({item["cohort_period"] for item in assignments.values()})
        cohort_starts = [_period_start(start, period_days) for start in starts]
        cohort_ids = [_period_label(start, period_days) for start in cohort_starts]

    rows: list[dict[str, Any]] = []
    unmatched_weight = 0.0
    outside_range_weight = 0.0
    channel_totals: dict[str, float] = defaultdict(float)
    assigned_users: set[str] = set()

    for cohort_index, cohort_start in enumerate(cohort_starts):
        cohort_end = cohort_start + period_seconds
        cohort_users = {
            uid for uid, item in assignments.items()
            if cohort_start <= item["cohort_time"] < cohort_end
        }
        assigned_users |= cohort_users
        denominator = len(cohort_users)
        # 相对周期 = 结算周期 - 同期群起始周期；保留率矩阵的 cohort_start 用
        # 「首个有回访的周期」做基准，两者对齐后首期即第1列。
        base_index = _period_index(cohort_start, period_days)
        weights_by_period: dict[int, float] = defaultdict(float)
        channels_by_period: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))

        for row in attribution_rows:
            uid = str(row.get("user_id", ""))
            if uid not in cohort_users:
                continue
            offset = int(row.get("settlement_period", 0)) - base_index
            if offset < 0 or offset >= max_periods:
                outside_range_weight += float(row.get("weight", 0) or 0)
                continue
            weight = float(row.get("weight", 0) or 0)
            weights_by_period[offset] += weight
            channels_by_period[offset][str(row.get("channel", ""))] += weight
            channel_totals[str(row.get("channel", ""))] += weight

        periods: list[dict[str, Any]] = []
        for offset in range(max_periods):
            weight = round(weights_by_period.get(offset, 0.0), 6)
            channels = {
                channel: round(value, 6)
                for channel, value in sorted(
                    channels_by_period.get(offset, {}).items(),
                    key=lambda item: (-item[1], item[0]),
                )
            }
            periods.append({
                "period_label": period_labels[offset],
                "period_offset": offset,
                "total_weight": weight,
                "conversion_rate_pct": round(weight / denominator * 100, 2) if denominator else 0.0,
                "channels": channels,
                "channel_rates_pct": {
                    channel: round(value / denominator * 100, 2) if denominator else 0.0
                    for channel, value in channels.items()
                },
            })

        rows.append({
            "cohort_id": cohort_ids[cohort_index] if cohort_index < len(cohort_ids) else _period_label(
                _period_index(cohort_start, period_days), period_days,
            ),
            "cohort_index": cohort_index,
            "denominator": denominator,
            "periods": periods,
        })

    for row in attribution_rows:
        if str(row.get("user_id", "")) not in assigned_users:
            unmatched_weight += float(row.get("weight", 0) or 0)

    total_weight = sum(row["weight"] for row in attribution_rows)
    channel_contributions = [
        {
            "channel": channel,
            "total_weight": round(weight, 6),
            "share_pct": round(weight / total_weight * 100, 2) if total_weight else 0.0,
        }
        for channel, weight in sorted(channel_totals.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "ok": True,
        "caliber": {
            **cfg,
            "denominator": "同期群初始事件用户数（去重）",
            "rate_formula": "周期归因权重 / 同期群分母 * 100",
            "settlement_basis_label": (
                "渠道按触达周结算" if cfg["settle_basis"] == "touch_week"
                else "运营按回访周复盘"
            ),
        },
        "cohort_ids": [row["cohort_id"] for row in rows],
        "period_labels": period_labels,
        "denominators": [row["denominator"] for row in rows],
        "rows": rows,
        "channel_contributions": channel_contributions,
        "unmatched_weight": round(unmatched_weight, 6),
        "outside_range_weight": round(outside_range_weight, 6),
    }


# ---------------------------------------------------------------------------
# 血缘图结构计算
# ---------------------------------------------------------------------------

@dataclass
class LineageNode:
    node_id: str
    node_name: str
    node_type: str  # table / metric / column / report
    upstream_ids: list[str]
    downstream_ids: list[str]


@dataclass
class LineageGraph:
    nodes: dict[str, LineageNode] = field(default_factory=dict)

    def add_node(self, node: LineageNode) -> None:
        self.nodes[node.node_id] = node

    def upstream_chain(self, node_id: str, max_depth: int = 10) -> list[list[str]]:
        """获取上游链路（从当前节点到根源的所有路径）。"""
        if node_id not in self.nodes:
            return []
        chains: list[list[str]] = []
        stack: list[tuple[str, list[str]]] = [(node_id, [node_id])]
        while stack:
            current, path = stack.pop()
            if len(path) > max_depth:
                chains.append(list(path))
                continue
            node = self.nodes.get(current)
            if node and node.upstream_ids:
                upstream_found = False
                for up_id in node.upstream_ids:
                    if up_id not in path:
                        stack.append((up_id, path + [up_id]))
                        upstream_found = True
                if not upstream_found:
                    chains.append(list(path))
            else:
                chains.append(list(path))
        return chains

    def downstream_chain(self, node_id: str, max_depth: int = 10) -> list[list[str]]:
        """获取下游影响链。"""
        if node_id not in self.nodes:
            return []
        chains: list[list[str]] = []
        stack: list[tuple[str, list[str]]] = [(node_id, [node_id])]
        while stack:
            current, path = stack.pop()
            if len(path) > max_depth:
                chains.append(list(path))
                continue
            node = self.nodes.get(current)
            if node and node.downstream_ids:
                ds_found = False
                for ds_id in node.downstream_ids:
                    if ds_id not in path:
                        stack.append((ds_id, path + [ds_id]))
                        ds_found = True
                if not ds_found:
                    chains.append(list(path))
            else:
                chains.append(list(path))
        return chains

    def impact_analysis(self, node_id: str) -> list[str]:
        """计算某个节点变更影响的所有下游节点。"""
        visited: set[str] = set()
        queue: deque[str] = deque([node_id])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            node = self.nodes.get(current)
            if node:
                for ds in node.downstream_ids:
                    if ds not in visited:
                        queue.append(ds)
        visited.discard(node_id)
        return sorted(visited)

    def to_adjacency(self) -> dict[str, Any]:
        """导出邻接表格式。"""
        adj: dict[str, list[str]] = {}
        for nid, node in self.nodes.items():
            adj[nid] = list(node.upstream_ids)
        return {"nodes": list(self.nodes), "adjacency": adj}


# ---------------------------------------------------------------------------
# 查询缓存生命周期
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    key: str
    result: Any
    row_count: int
    byte_size: int
    created_at: float
    ttl_seconds: int
    hit_count: int = 0
    last_hit_at: float | None = None


class CacheManager:
    """按TTL和LRU管理查询结果缓存生命周期。"""

    def __init__(self, max_entries: int = 500, max_memory_bytes: int = 200 * 1024 * 1024):
        self._entries: dict[str, CacheEntry] = {}
        self._access_order: list[str] = []  # LRU: 最近访问在末尾
        self.max_entries = max_entries
        self.max_memory = max_memory_bytes

    def compute_key(self, query_desc: str, params: dict[str, Any]) -> str:
        raw = json.dumps({"query": query_desc, "params": params}, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.time() - entry.created_at > entry.ttl_seconds:
            self._evict(key)
            return None
        entry.hit_count += 1
        entry.last_hit_at = time.time()
        self._touch(key)
        return entry.result

    def put(
        self, key: str, result: Any, row_count: int = 0,
        ttl_seconds: int = 300, byte_size: int | None = None,
    ) -> None:
        if byte_size is None:
            byte_size = len(json.dumps(result, default=str).encode()) if result is not None else 0
        entry = CacheEntry(
            key=key, result=result, row_count=row_count,
            byte_size=byte_size, created_at=time.time(), ttl_seconds=ttl_seconds,
        )
        self._entries[key] = entry
        self._touch(key)
        self._enforce_limits()

    def invalidate(self, key: str) -> bool:
        return self._evict(key)

    def stats(self) -> dict[str, Any]:
        total_hits = sum(e.hit_count for e in self._entries.values())
        active = sum(1 for e in self._entries.values()
                     if time.time() - e.created_at <= e.ttl_seconds)
        total_mem = sum(e.byte_size for e in self._entries.values())
        return {
            "total": len(self._entries),
            "active": active,
            "expired": len(self._entries) - active,
            "total_hits": total_hits,
            "memory_bytes": total_mem,
        }

    def _touch(self, key: str) -> None:
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)

    def _evict(self, key: str) -> bool:
        if key in self._entries:
            del self._entries[key]
        if key in self._access_order:
            self._access_order.remove(key)
        return True

    def _enforce_limits(self) -> None:
        while len(self._entries) > self.max_entries and self._access_order:
            lru_key = self._access_order[0]
            self._evict(lru_key)
        total_mem = sum(e.byte_size for e in self._entries.values())
        while total_mem > self.max_memory and self._access_order:
            lru_key = self._access_order[0]
            self._evict(lru_key)
            total_mem = sum(e.byte_size for e in self._entries.values())


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------

def detect_anomalies_zscore(
    values: list[float], threshold: float = 3.0
) -> list[dict[str, Any]]:
    """Z-Score 异常检测。"""
    n = len(values)
    if n < 3:
        return []
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(variance) if variance > 0 else 1e-10
    anomalies: list[dict[str, Any]] = []
    for i, v in enumerate(values):
        z = abs(v - mean) / std
        if z > threshold:
            anomalies.append({"index": i, "value": v, "z_score": round(z, 3), "method": "zscore"})
    return anomalies


def detect_anomalies_iqr(values: list[float], multiplier: float = 1.5) -> list[dict[str, Any]]:
    """IQR 异常检测。"""
    n = len(values)
    if n < 4:
        return []
    sorted_vals = sorted(values)
    q1_idx = n // 4
    q3_idx = (3 * n) // 4
    q1 = sorted_vals[q1_idx]
    q3 = sorted_vals[q3_idx]
    iqr = q3 - q1
    if iqr == 0:
        return []
    lower = q1 - multiplier * iqr
    upper = q3 + multiplier * iqr
    anomalies: list[dict[str, Any]] = []
    for i, v in enumerate(values):
        if v < lower or v > upper:
            anomalies.append({"index": i, "value": v, "lower": lower, "upper": upper, "method": "iqr"})
    return anomalies


def detect_anomalies_moving_avg(
    values: list[float], window: int = 7, deviation_ratio: float = 0.3,
) -> list[dict[str, Any]]:
    """基于移动平均偏差的异常检测。"""
    n = len(values)
    if n < window:
        return []
    anomalies: list[dict[str, Any]] = []
    for i in range(window, n):
        window_vals = values[i - window:i]
        avg = sum(window_vals) / window
        if avg == 0:
            continue
        deviation = abs(values[i] - avg) / abs(avg)
        if deviation > deviation_ratio:
            anomalies.append({
                "index": i, "value": values[i], "moving_avg": round(avg, 3),
                "deviation_pct": round(deviation * 100, 2), "method": "moving_avg",
            })
    return anomalies


# ---------------------------------------------------------------------------
# A/B 实验统计
# ---------------------------------------------------------------------------

def compute_ab_sample_size(
    baseline_rate: float, min_detectable_effect: float,
    alpha: float = 0.05, power: float = 0.8,
) -> int:
    """计算A/B实验最小样本量。"""
    z_alpha = 1.96  # 双尾 alpha=0.05
    z_beta = 0.84   # power=0.8
    p = baseline_rate
    delta = min_detectable_effect
    n = (2 * (z_alpha + z_beta) ** 2 * p * (1 - p)) / (delta ** 2)
    return max(1, math.ceil(n))


def compute_confidence_interval(
    values_a: list[float], values_b: list[float], confidence: float = 0.95,
) -> dict[str, Any]:
    """计算两组数据的置信区间和均值差异。"""
    na, nb = len(values_a), len(values_b)
    if na < 2 or nb < 2:
        return {"error": "样本量不足"}

    mean_a = sum(values_a) / na
    mean_b = sum(values_b) / nb
    diff = mean_b - mean_a

    var_a = sum((v - mean_a) ** 2 for v in values_a) / (na - 1)
    var_b = sum((v - mean_b) ** 2 for v in values_b) / (nb - 1)

    se = math.sqrt(var_a / na + var_b / nb)
    if se == 0:
        return {"mean_a": mean_a, "mean_b": mean_b, "diff": diff, "significant": False, "reason": "零方差"}

    z_value = 1.96 if confidence == 0.95 else 2.576  # 简化

    ci_lower = diff - z_value * se
    ci_upper = diff + z_value * se
    significant = (ci_lower > 0 or ci_upper < 0)

    return {
        "mean_a": round(mean_a, 4),
        "mean_b": round(mean_b, 4),
        "diff": round(diff, 4),
        "diff_pct": round(diff / (abs(mean_a) if mean_a != 0 else 1) * 100, 2),
        "ci_lower": round(ci_lower, 4),
        "ci_upper": round(ci_upper, 4),
        "significant": significant,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 下钻路径计算
# ---------------------------------------------------------------------------

def compute_drill_levels(
    data: list[dict[str, Any]],
    drill_hierarchy: list[str],  # e.g. ["region", "city", "store"]
    measures: list[str],
    aggregator: str = "SUM",
) -> list[dict[str, Any]]:
    """按层级分组计算各层度量值。"""
    results: list[dict[str, Any]] = []
    for level_idx, dim in enumerate(drill_hierarchy):
        group_map: dict[str, dict[str, list[float]]] = {}
        for row in data:
            key = str(row.get(dim, ""))
            if key not in group_map:
                group_map[key] = {m: [] for m in measures}
            for m in measures:
                try:
                    group_map[key][m].append(float(row.get(m, 0)))
                except (ValueError, TypeError):
                    pass
        level_data: dict[str, Any] = {
            "level": level_idx,
            "dimension": dim,
            "groups": [],
        }
        for key, mdata in group_map.items():
            entry = {"key": key}
            for m in measures:
                values = mdata.get(m, [])
                entry[m] = round(compute_metric(values, aggregator), 2)
            level_data["groups"].append(entry)
        results.append(level_data)
    return results


# ---------------------------------------------------------------------------
# 查询加速预计算策略
# ---------------------------------------------------------------------------

def recommend_materialization(
    query_patterns: list[dict[str, Any]],  # [{query_id, frequency, avg_latency_ms, row_count}]
) -> list[dict[str, Any]]:
    """根据查询模式推荐物化策略。"""
    recommendations: list[dict[str, Any]] = []
    for q in query_patterns:
        freq = int(q.get("frequency", 0))
        latency = float(q.get("avg_latency_ms", 0))
        rows = int(q.get("row_count", 0))

        score = freq * max(1, latency / 100) * max(1, rows / 1000)
        if score > 50:
            method = "物化视图" if rows > 10000 else "预计算" if rows > 1000 else "结果缓存"
            strategy = {
                "query_id": q.get("query_id", ""),
                "score": round(score, 2),
                "recommended_method": method,
                "priority": "高" if score > 500 else "中" if score > 200 else "低",
            }
            recommendations.append(strategy)
    return sorted(recommendations, key=lambda x: float(x["score"]), reverse=True)


# ---------------------------------------------------------------------------
# 导出任务调度器
# ---------------------------------------------------------------------------

class ExportScheduler:
    """异步导出任务调度器（基于优先级队列）。"""

    def __init__(self, max_concurrent: int = 3):
        self._queue: list[dict[str, Any]] = []
        self._running: set[str] = set()
        self.max_concurrent = max_concurrent

    def enqueue(
        self, task_id: str, priority: int = 0,
        sql: str = "", format: str = "csv",
    ) -> str:
        self._queue.append({
            "task_id": task_id,
            "priority": priority,
            "sql": sql,
            "format": format,
            "enqueued_at": time.time(),
        })
        self._queue.sort(key=lambda x: (-x["priority"], x["enqueued_at"]))
        return task_id

    def dequeue(self) -> dict[str, Any] | None:
        if not self._queue or len(self._running) >= self.max_concurrent:
            return None
        task = self._queue.pop(0)
        self._running.add(task["task_id"])
        return task

    def complete(self, task_id: str) -> None:
        self._running.discard(task_id)

    def queue_size(self) -> int:
        return len(self._queue)

    def running_count(self) -> int:
        return len(self._running)