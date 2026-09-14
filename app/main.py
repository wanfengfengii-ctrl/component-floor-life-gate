"""湿敏元件上线判定 API。"""

from __future__ import annotations

from fastapi import FastAPI

from .logic import compute_judgement, enforce_business_rules
from .schemas import JudgeRequest, JudgeResponse

app = FastAPI(
    title="MSL Floor-Life Judge",
    version="1.1.0",
    description=(
        "判定已开封湿敏元件在扣除回干柜暂停时段后是否仍可上线。"
        "所有时间按 UTC 比较；非法请求整单返回 422。\n\n"
        "可选字段 `rebake_completed_at` 表示该已开封料盘一次合格烘烤的完成时刻；"
        "提供后有效暴露从该时刻重新累计，并继续扣除该时刻之后的回干区间，"
        "响应通过 `reset_applied=true` 与 `exposure_origin_at_utc` 供调用方复核。"
        "省略时从 opened_at 起算，响应保持兼容（`reset_applied=false`，"
        "`exposure_origin_at_utc` 等于 `opened_at_utc`）。"
    ),
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/judge", response_model=JudgeResponse)
def judge(payload: JudgeRequest) -> JudgeResponse:
    """判定料盘能否上线。

    - 不传 `rebake_completed_at`：有效暴露从 `opened_at` 起算。
    - 传入 `rebake_completed_at`：从烘烤完成时刻重新累计有效暴露，
      并继续扣除该时刻之后的回干区间；该时刻必须严格位于
      `(opened_at, pickup_at)` 且不与任何回干区间相交或共端点，
      冲突时以 422 返回，`loc` 精确定位到 `rebake_completed_at`。
    """
    enforce_business_rules(payload)
    return compute_judgement(payload)
