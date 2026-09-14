"""请求/响应模型与 RFC3339 秒精度时间解析。

所有入站时间字符串必须满足：
- RFC3339 完整日期时间格式：YYYY-MM-DDTHH:MM:SS
- 显式时区：结尾 Z/z 或 ±HH:MM 偏移；其中 -00:00 按 RFC3339 语义表示
  “本地偏移未知”，无法换算为确定的 UTC 时刻，必须拒绝（UTC 请用 Z/+00:00）
- 秒精度：不允许小数秒；秒值 60 表示闰秒，按 RFC3339 接受并归一化到 UTC
- 解析后统一换算为 UTC 再参与比较与计算；字符串本身合法但换算后越过
  datetime 可表示边界的时间同样拒绝，错误定位到对应字段
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_serializer

# 严格的 RFC3339 秒精度格式（含时区），拒绝小数秒与裸时间。
# 秒字段额外允许 60：RFC3339 用 :60 表示闰秒。
_RFC3339_SECONDS_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"[Tt](?:[01]\d|2[0-3]):[0-5]\d:(?:[0-5]\d|60)"
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

    year, month, day = int(value[0:4]), int(value[5:7]), int(value[8:10])
    hour, minute, second = int(value[11:13]), int(value[14:16]), int(value[17:19])
    tz_text = value[19:]

    if tz_text in ("Z", "z"):
        offset = timedelta(0)
    else:
        sign = 1 if tz_text[0] == "+" else -1
        offset_hours, offset_minutes = int(tz_text[1:3]), int(tz_text[4:6])
        if sign < 0 and offset_hours == 0 and offset_minutes == 0:
            # RFC3339：-00:00 表示“本地偏移未知”，不得当作 +00:00（UTC）处理。
            raise ValueError(
                "offset -00:00 denotes an unknown local offset per RFC3339; the "
                "actual UTC offset cannot be determined, use Z or +00:00 for UTC "
                "or supply a concrete non-zero offset"
            )
        offset = sign * timedelta(hours=offset_hours, minutes=offset_minutes)

    is_leap_second = second == 60
    if is_leap_second:
        # 闰秒无法用普通 datetime 表示：先以该分钟的第 59 秒构造，换算后
        # 再顺延 1 秒（POSIX/Unix 时间戳对闰秒的标准归一化方式）。
        second = 59

    try:
        parsed = datetime(
            year, month, day, hour, minute, second,
            tzinfo=timezone(offset),
        )
    except ValueError:
        raise ValueError("invalid calendar date or time of day") from None

    try:
        result = parsed.astimezone(timezone.utc)
        if is_leap_second:
            result += timedelta(seconds=1)
    except (OverflowError, ValueError):
        # 字符串格式合法，但换算到 UTC（含闰秒归一化）后越过了 datetime
        # 可表示的日期边界。
        raise ValueError(
            "timestamp is out of range after conversion to UTC; conversion "
            "crosses the representable date boundary"
        ) from None
    return result


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
    rebake_completed_at: Rfc3339Seconds | None = Field(
        default=None,
        description=(
            "可选：已开封料盘一次合格烘烤的完成时刻（带时区 RFC3339 秒精度）。"
            "提供后有效暴露从该时刻重新累计，且只扣除该时刻之后的回干区间；"
            "必须严格位于 opened_at 与 pickup_at 之间，且不得落入任何回干区间"
            "或与其端点重合。省略时沿用从 opened_at 起算的旧计算。"
        ),
    )
    dry_intervals: list[DryInterval] = Field(default_factory=list)


class JudgeResponse(BaseModel):
    verdict: Verdict
    limit_minutes: int
    effective_exposure_minutes: int
    remaining_minutes: int
    exceeded_minutes: int
    opened_at_utc: datetime
    pickup_at_utc: datetime
    exposure_origin_at_utc: datetime = Field(
        description=(
            "有效暴露的起算时刻（UTC）：传入 rebake_completed_at 时为该时刻，"
            "否则等于 opened_at_utc。"
        )
    )
    reset_applied: bool = Field(
        description="本次判定是否因 rebake_completed_at 而重新累计暴露。"
    )
    total_seconds: int
    dry_seconds: int
    effective_seconds: int

    @field_serializer("opened_at_utc", "pickup_at_utc", "exposure_origin_at_utc")
    def _serialize_utc(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
