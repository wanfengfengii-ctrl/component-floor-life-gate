"""请求/响应模型与 RFC3339 秒精度时间解析。

所有入站时间字符串必须满足：
- RFC3339 完整日期时间格式：YYYY-MM-DDTHH:MM:SS
- 显式时区：结尾 Z/z 或 ±HH:MM 偏移
- 秒精度：不允许小数秒
解析后统一换算为 UTC 再参与比较与计算。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_serializer

# 严格的 RFC3339 秒精度格式（含时区），拒绝小数秒与裸时间。
_RFC3339_SECONDS_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"[Tt](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)


def _parse_rfc3339_seconds(value: object) -> datetime:
    """把入站字符串解析为 UTC 时间；格式不符时抛出 ValueError（→ 422）。"""
    if not isinstance(value, str):
        raise ValueError(
            "must be an RFC3339 timestamp string with timezone and seconds "
            "precision, e.g. 2026-09-01T08:00:00+08:00 or 2026-09-01T00:00:00Z"
        )
    if not _RFC3339_SECONDS_RE.fullmatch(value):
        raise ValueError(
            "must be RFC3339 with seconds precision and explicit timezone "
            "(YYYY-MM-DDTHH:MM:SSZ or YYYY-MM-DDTHH:MM:SS±HH:MM); "
            "fractional seconds and naive datetimes are not accepted"
        )
    text = value[:10] + "T" + value[11:]
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("invalid calendar date or time of day") from None
    return parsed.astimezone(timezone.utc)


Rfc3339Seconds = Annotated[datetime, BeforeValidator(_parse_rfc3339_seconds)]

MSLLevel = Literal["MSL2", "MSL3", "MSL4"]

Verdict = Literal["available", "boundary_available", "expired"]


class DryInterval(BaseModel):
    """一段回干（干燥柜暂停）区间，[start, end)，端点均为 RFC3339 秒精度时间。"""

    model_config = ConfigDict(extra="forbid")

    start: Rfc3339Seconds
    end: Rfc3339Seconds


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: MSLLevel
    opened_at: Rfc3339Seconds
    pickup_at: Rfc3339Seconds
    dry_intervals: list[DryInterval] = Field(default_factory=list)


class JudgeResponse(BaseModel):
    verdict: Verdict
    limit_minutes: int
    effective_exposure_minutes: int
    remaining_minutes: int
    exceeded_minutes: int
    opened_at_utc: datetime
    pickup_at_utc: datetime
    total_seconds: int
    dry_seconds: int
    effective_seconds: int

    @field_serializer("opened_at_utc", "pickup_at_utc")
    def _serialize_utc(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
