"""业务规则校验与暴露时长判定。

规则（全部在 UTC 下比较）：
- opened_at 不得晚于 pickup_at；
- 每个回干区间必须 start < end，且完整落在 [opened_at, pickup_at] 内；
- 回干区间两两不得相交，也不得端点相接（前一区间的 end 必须严格小于后一区间的 start）；
- 有效暴露分钟 = floor((总秒数 - 回干秒数合计) / 60)；
- 小于限额 → available，等于限额 → boundary_available，大于限额 → expired。

任何违规都收集为带字段定位的错误列表，整单以 422 拒绝，不产出部分判定。
"""

from __future__ import annotations

from typing import Any

from fastapi.exceptions import RequestValidationError

from .schemas import JudgeRequest, JudgeResponse

# 各湿敏等级允许的有效暴露分钟数（固定值）。
MSL_LIMIT_MINUTES: dict[str, int] = {
    "MSL2": 525600,
    "MSL3": 168,
    "MSL4": 72,
}


def _error(loc: tuple[Any, ...], msg: str, type_: str) -> dict[str, Any]:
    return {"loc": ("body",) + loc, "msg": msg, "type": type_}


def enforce_business_rules(payload: JudgeRequest) -> None:
    """校验跨字段业务规则；有任何违规则抛出 RequestValidationError（→ 422）。"""
    errors: list[dict[str, Any]] = []
    opened = payload.opened_at
    pickup = payload.pickup_at

    if opened > pickup:
        errors.append(
            _error(
                ("pickup_at",),
                "opened_at must not be later than pickup_at",
                "value_error.time_inverted",
            )
        )

    intervals = payload.dry_intervals
    for index, interval in enumerate(intervals):
        if interval.start >= interval.end:
            errors.append(
                _error(
                    ("dry_intervals", index, "end"),
                    "dry interval end must be strictly after its start",
                    "value_error.dry_interval_not_positive",
                )
            )
        if interval.start < opened:
            errors.append(
                _error(
                    ("dry_intervals", index, "start"),
                    "dry interval starts before opened_at; intervals must lie "
                    "within [opened_at, pickup_at]",
                    "value_error.dry_interval_out_of_range",
                )
            )
        if interval.end > pickup:
            errors.append(
                _error(
                    ("dry_intervals", index, "end"),
                    "dry interval ends after pickup_at; intervals must lie "
                    "within [opened_at, pickup_at]",
                    "value_error.dry_interval_out_of_range",
                )
            )

    # 按 (start, end) 排序后检查相邻区间：不得相交，也不得端点相接。
    ordered = sorted(enumerate(intervals), key=lambda pair: (pair[1].start, pair[1].end))
    for (prev_index, prev), (curr_index, curr) in zip(ordered, ordered[1:]):
        if curr.start <= prev.end:
            errors.append(
                _error(
                    ("dry_intervals", curr_index, "start"),
                    f"dry interval #{curr_index} overlaps or touches dry interval "
                    f"#{prev_index}; intervals must be disjoint and may not share "
                    "an endpoint",
                    "value_error.dry_intervals_overlap",
                )
            )

    if errors:
        raise RequestValidationError(errors)


def compute_judgement(payload: JudgeRequest) -> JudgeResponse:
    """在已通过全部校验的请求上计算唯一上线结论与可复核分钟数。"""
    total_seconds = int((payload.pickup_at - payload.opened_at).total_seconds())
    dry_seconds = sum(
        int((interval.end - interval.start).total_seconds())
        for interval in payload.dry_intervals
    )
    effective_seconds = total_seconds - dry_seconds
    effective_minutes = effective_seconds // 60
    limit = MSL_LIMIT_MINUTES[payload.level]

    if effective_minutes < limit:
        verdict = "available"
        remaining_minutes = limit - effective_minutes
        exceeded_minutes = 0
    elif effective_minutes == limit:
        verdict = "boundary_available"
        remaining_minutes = 0
        exceeded_minutes = 0
    else:
        verdict = "expired"
        remaining_minutes = 0
        exceeded_minutes = effective_minutes - limit

    return JudgeResponse(
        verdict=verdict,
        limit_minutes=limit,
        effective_exposure_minutes=effective_minutes,
        remaining_minutes=remaining_minutes,
        exceeded_minutes=exceeded_minutes,
        opened_at_utc=payload.opened_at,
        pickup_at_utc=payload.pickup_at,
        total_seconds=total_seconds,
        dry_seconds=dry_seconds,
        effective_seconds=effective_seconds,
    )
