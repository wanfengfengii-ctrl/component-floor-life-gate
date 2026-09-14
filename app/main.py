"""湿敏元件上线判定 API。"""

from __future__ import annotations

from fastapi import FastAPI

from .logic import compute_judgement, enforce_business_rules
from .schemas import JudgeRequest, JudgeResponse

app = FastAPI(
    title="MSL Floor-Life Judge",
    version="1.0.0",
    description=(
        "判定已开封湿敏元件在扣除回干柜暂停时段后是否仍可上线。"
        "所有时间按 UTC 比较；非法请求整单返回 422。"
    ),
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/judge", response_model=JudgeResponse)
def judge(payload: JudgeRequest) -> JudgeResponse:
    enforce_business_rules(payload)
    return compute_judgement(payload)
