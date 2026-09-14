"""业务规则校验与暴露时长判定。

规则（全部在 UTC 下比较）：
- opened_at 不得晚于 pickup_at；
- 每个回干区间必须 start < end，且完整落在 [opened_at, pickup_at] 内；
- 回干区间两两不得相交，也不得端点相接（前一区间的 end 必须严格小于后一区间的 start）；
- 可选的 rebake_completed_at（合格烘烤完成时刻）必须严格位于
  (opened_at, pickup_at) 内，且不得落入任何回干区间或与其端点重合；
- 未提供 rebake_completed_at 时，有效暴露从 opened_at 起算；提供后从该时刻
  重新累计，只扣除起算时刻之后的回干区间（起算时刻之前的区间不再影响结论）；
- 有效暴露分钟 = floor((总秒数 - 回干秒数合计) / 60)，其中总秒数与回干秒数
  均以实际起算时刻为基准；
- 小于限额 → available，等于限额 → boundary_available，大于限额 → expired。

可选的 include_calculation_trace=true 不改变结论，只在响应中附加
calculation_trace：从实际起算时刻到 pickup_at 按时间顺序排列的
exposed/dry 计算片段（UTC 起止与秒数），片段互不重叠且秒数之和等于
total_seconds，供复核回干区间如何影响计算。

任何违规都收集为带字段定位的错误列表，整单以 422 拒绝，不产出部分判定。

批量接口（POST /judge/batch）逐盘复用本模块的同一套校验与判定：校验阶段
给每个料盘的错误 loc 前缀 ``("items", 下标)`` 后统一收集，任一料盘违规即
整批 422；全部合法时按输入顺序计算，并汇总三种结论的数量。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Sequence

from fastapi.exceptions import RequestValidationError

from .schemas import (
    BatchSummary,
    CalculationTraceSegment,
    JudgeBatchRequest,
    JudgeBatchResponse,
    JudgeRequest,
    JudgeResponse,
)

# 各湿敏等级允许的有效暴露分钟数（固定值）。
MSL_LIMIT_MINUTES: dict[str, int] = {
    "MSL2": 525600,
    "MSL3": 168,
    "MSL4": 72,
}


def _error(loc: tuple[Any, ...], msg: str, type_: str) -> dict[str, Any]:
    return {"loc": ("body",) + loc, "msg": msg, "type": type_}


def enforce_business_rules(
    payload: JudgeRequest, loc_prefix: Sequence[Any] = ()
) -> None:
    """校验跨字段业务规则；有任何违规则抛出 RequestValidationError（→ 422）。

    ``loc_prefix`` 拼在所有错误字段路径的 ``body`` 之后、字段名之前：
    单盘接口为空（保持 ``("body", "pickup_at")`` 等旧定位不变），批量接口
    传入 ``("items", index)``，使错误精确定位到具体料盘。
    """
    errors: list[dict[str, Any]] = []

    def err(field_loc: tuple[Any, ...], msg: str, type_: str) -> dict[str, Any]:
        return _error(tuple(loc_prefix) + field_loc, msg, type_)

    opened = payload.opened_at
    pickup = payload.pickup_at
    intervals = payload.dry_intervals

    if opened > pickup:
        errors.append(
            err(
                ("pickup_at",),
                "opened_at must not be later than pickup_at",
                "value_error.time_inverted",
            )
        )

    rebake = payload.rebake_completed_at
    if rebake is not None:
        if not (opened < rebake < pickup):
            errors.append(
                err(
                    ("rebake_completed_at",),
                    "rebake_completed_at must be strictly between opened_at and "
                    "pickup_at",
                    "value_error.rebake_out_of_range",
                )
            )
        # 烘烤完成时刻不得落入任何回干区间，也不得与其端点重合。
        for index, interval in enumerate(intervals):
            if interval.start <= rebake <= interval.end:
                errors.append(
                    err(
                        ("rebake_completed_at",),
                        f"rebake_completed_at must not fall inside dry interval "
                        f"#{index} or coincide with either of its endpoints",
                        "value_error.rebake_dry_interval_conflict",
                    )
                )

    for index, interval in enumerate(intervals):
        if interval.start >= interval.end:
            errors.append(
                err(
                    ("dry_intervals", index, "end"),
                    "dry interval end must be strictly after its start",
                    "value_error.dry_interval_not_positive",
                )
            )
        if interval.start < opened:
            errors.append(
                err(
                    ("dry_intervals", index, "start"),
                    "dry interval starts before opened_at; intervals must lie "
                    "within [opened_at, pickup_at]",
                    "value_error.dry_interval_out_of_range",
                )
            )
        if interval.end > pickup:
            errors.append(
                err(
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
                err(
                    ("dry_intervals", curr_index, "start"),
                    f"dry interval #{curr_index} overlaps or touches dry interval "
                    f"#{prev_index}; intervals must be disjoint and may not share "
                    "an endpoint",
                    "value_error.dry_intervals_overlap",
                )
            )

    if errors:
        raise RequestValidationError(errors)


def _build_calculation_trace(
    payload: JudgeRequest, origin: datetime
) -> list[CalculationTraceSegment]:
    """把 [起算时刻, pickup_at] 按时间顺序切成 exposed/dry 片段。

    区间过滤与 dry_seconds 完全一致（只算起算时刻之后的回干区间），因此
    全部 dry 片段秒数之和等于 dry_seconds，所有片段秒数之和等于
    total_seconds；校验已保证区间互不重叠且不端点相接，故片段首尾相接、
    两两不重叠。零暴露（opened_at == pickup_at 且无相关区间）时为空列表。
    """
    segments: list[CalculationTraceSegment] = []

    def segment(
        kind: Literal["exposed", "dry"], start: datetime, end: datetime
    ) -> CalculationTraceSegment:
        return CalculationTraceSegment(
            kind=kind,
            start_at_utc=start,
            end_at_utc=end,
            seconds=int((end - start).total_seconds()),
        )

    cursor = origin
    relevant = sorted(
        (i for i in payload.dry_intervals if i.start >= origin),
        key=lambda i: (i.start, i.end),
    )
    for interval in relevant:
        if interval.start > cursor:
            segments.append(segment("exposed", cursor, interval.start))
        segments.append(segment("dry", interval.start, interval.end))
        cursor = interval.end
    if cursor < payload.pickup_at:
        segments.append(segment("exposed", cursor, payload.pickup_at))
    return segments


def compute_judgement(payload: JudgeRequest) -> JudgeResponse:
    """在已通过全部校验的请求上计算唯一上线结论与可复核分钟数。"""
    reset_applied = payload.rebake_completed_at is not None
    # 提供合格烘烤完成时刻后从该时刻重新累计；否则沿用 opened_at 起算。
    origin = payload.rebake_completed_at or payload.opened_at
    total_seconds = int((payload.pickup_at - origin).total_seconds())
    # 只扣除起算时刻之后的回干区间；校验已保证没有区间跨过或接触起算时刻。
    dry_seconds = sum(
        int((interval.end - interval.start).total_seconds())
        for interval in payload.dry_intervals
        if interval.start >= origin
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
        exposure_origin_at_utc=origin,
        reset_applied=reset_applied,
        total_seconds=total_seconds,
        dry_seconds=dry_seconds,
        effective_seconds=effective_seconds,
        calculation_trace=(
            _build_calculation_trace(payload, origin)
            if payload.include_calculation_trace
            else None
        ),
    )


def compute_batch_judgement(payload: JudgeBatchRequest) -> JudgeBatchResponse:
    """先整批校验（任一料盘非法即整批 422，不产出任何判定），再按输入顺序
    复用单盘计算并汇总三种结论的数量。"""
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(payload.items):
        try:
            enforce_business_rules(item, loc_prefix=("items", index))
        except RequestValidationError as exc:
            errors.extend(exc.errors())
    if errors:
        raise RequestValidationError(errors)

    results = [compute_judgement(item) for item in payload.items]
    summary = BatchSummary(
        available=sum(r.verdict == "available" for r in results),
        boundary_available=sum(r.verdict == "boundary_available" for r in results),
        expired=sum(r.verdict == "expired" for r in results),
    )
    return JudgeBatchResponse(items=results, summary=summary)
