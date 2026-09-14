"""湿敏元件上线判定 API。"""

from __future__ import annotations

from fastapi import FastAPI

from .logic import (
    compute_batch_judgement,
    compute_judgement,
    enforce_business_rules,
)
from .schemas import JudgeBatchRequest, JudgeBatchResponse, JudgeRequest, JudgeResponse

app = FastAPI(
    title="MSL Floor-Life Judge",
    version="1.2.0",
    description=(
        "判定已开封湿敏元件在扣除回干柜暂停时段后是否仍可上线。"
        "所有时间按 UTC 比较；非法请求整单返回 422。\n\n"
        "- `POST /judge`：单盘判定。\n"
        "- `POST /judge/batch`：一次提交 1–100 个按顺序排列的单盘请求，"
        "复用同一套时间解析与判定规则，按输入顺序返回完整结果并汇总三种"
        "结论数量；任一料盘非法则整批 422，错误 loc 前缀为 items 与下标。\n\n"
        "可选字段 `rebake_completed_at` 表示该已开封料盘一次合格烘烤的完成时刻；"
        "提供后有效暴露从该时刻重新累计，并继续扣除该时刻之后的回干区间，"
        "响应通过 `reset_applied=true` 与 `exposure_origin_at_utc` 供调用方复核。"
        "省略时从 opened_at 起算，响应保持兼容（`reset_applied=false`，"
        "`exposure_origin_at_utc` 等于 `opened_at_utc`）。\n\n"
        "可选布尔开关 `include_calculation_trace=true` 时，响应额外携带 "
        "`calculation_trace`：从实际起算时刻到领用时刻按时间顺序排列的 "
        "exposed/dry 计算片段（UTC 起止与秒数），片段互不重叠且秒数之和等于 "
        "`total_seconds`，供复核回干区间如何影响结论；省略或为 false 时响应"
        "结构不变。批量接口的每个料盘可各自开启，仅为开启的条目生成片段。"
    ),
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/judge", response_model=JudgeResponse, response_model_exclude_none=True)
def judge(payload: JudgeRequest) -> JudgeResponse:
    """判定料盘能否上线。

    - 不传 `rebake_completed_at`：有效暴露从 `opened_at` 起算。
    - 传入 `rebake_completed_at`：从烘烤完成时刻重新累计有效暴露，
      并继续扣除该时刻之后的回干区间；该时刻必须严格位于
      `(opened_at, pickup_at)` 且不与任何回干区间相交或共端点，
      冲突时以 422 返回，`loc` 精确定位到 `rebake_completed_at`。
    - 传入 `include_calculation_trace=true`：响应附加按时间顺序的
      exposed/dry 计算片段；不传或为 false 时响应结构保持原样。
    """
    enforce_business_rules(payload)
    return compute_judgement(payload)


@app.post(
    "/judge/batch",
    response_model=JudgeBatchResponse,
    response_model_exclude_none=True,
)
def judge_batch(payload: JudgeBatchRequest) -> JudgeBatchResponse:
    """批量判定 1–100 个料盘。

    - 时间解析、烘烤重置、回干校验与暴露计算完全复用单盘链路；
    - 任一料盘不合法则整批 422 且不产出任何判定结果，错误 ``loc`` 在原字段
      路径前插入 ``items`` 与下标（如
      ``("body", "items", 3, "dry_intervals", 0, "end")``）；
    - ``items`` 为空或超过 100 项同样 422，定位到 ``items``；
    - 全部合法时结果按输入顺序完整返回，并在 ``summary`` 中汇总
      ``available`` / ``boundary_available`` / ``expired`` 三种结论的数量；
    - 每个料盘可各自传 ``include_calculation_trace=true``，仅为开启的条目
      附加计算片段，其余条目响应结构不变。
    """
    return compute_batch_judgement(payload)
