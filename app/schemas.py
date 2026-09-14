"""请求/响应模型与 RFC3339 秒精度时间解析。

所有入站时间字符串必须满足：
- RFC3339 完整日期时间格式：YYYY-MM-DDTHH:MM:SS
- 显式时区：结尾 Z/z 或 ±HH:MM 偏移；其中 -00:00 按 RFC3339 语义表示
  “本地偏移未知”，无法换算为确定的 UTC 时刻，必须拒绝（UTC 请用 Z/+00:00）
- 秒精度：不允许小数秒；秒值 60 仅在该时刻确为真实闰秒（IERS 公告，
  均为 UTC 23:59:60，最后一次 2016-12-31）时接受，归一化到下一分钟 :00；
  非闰秒时刻写 :60 一律拒绝
- 解析后统一换算为 UTC 再参与比较与计算；字符串本身合法但换算后越过
  datetime 可表示边界的时间同样拒绝，错误定位到对应字段
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    field_serializer,
)

# 严格的 RFC3339 秒精度格式（含时区），拒绝小数秒与裸时间。
# 秒字段额外允许 60：RFC3339 用 :60 表示闰秒，但只有下列 IERS 公告中真实
# 发生过闰秒的 UTC 日期（闰秒统一为该日 23:59:60Z）才合法；1972 年起共
# 27 次，最近一次为 2016-12-31。RFC3339 §4.3 要求接收方据已知闰秒表判定。
_LEAP_SECOND_UTC_DATES: frozenset[tuple[int, int, int]] = frozenset(
    {
        (1972, 6, 30), (1972, 12, 31),
        (1973, 12, 31),
        (1974, 12, 31),
        (1975, 12, 31),
        (1976, 12, 31),
        (1977, 12, 31),
        (1978, 12, 31),
        (1979, 12, 31),
        (1981, 6, 30),
        (1982, 6, 30),
        (1983, 6, 30),
        (1985, 6, 30),
        (1987, 12, 31),
        (1989, 12, 31),
        (1990, 12, 31),
        (1992, 6, 30),
        (1993, 6, 30),
        (1994, 6, 30),
        (1995, 12, 31),
        (1997, 6, 30),
        (1998, 12, 31),
        (2005, 12, 31),
        (2008, 12, 31),
        (2012, 6, 30),
        (2015, 6, 30),
        (2016, 12, 31),
    }
)

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
        # 闰秒无法用普通 datetime 表示：先以该分钟的第 59 秒构造，换算 UTC
        # 后再判断它是否为真实闰秒并顺延 1 秒。
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
    except (OverflowError, ValueError):
        # 字符串格式合法，但换算到 UTC 后越过了 datetime 可表示的日期边界。
        raise ValueError(
            "timestamp is out of range after conversion to UTC; conversion "
            "crosses the representable date boundary"
        ) from None

    if is_leap_second:
        # 闰秒只可能发生在 UTC 当日 23:59:60，且日期必须在 IERS 闰秒表内；
        # 非闰秒时刻写 :60 属于无效时间，必须拒绝。
        if (
            result.hour != 23
            or result.minute != 59
            or (result.year, result.month, result.day) not in _LEAP_SECOND_UTC_DATES
        ):
            raise ValueError(
                "second 60 is valid only for an actual UTC leap second "
                "(23:59:60Z on an IERS-announced date; most recent "
                "2016-12-31); this timestamp is not a real leap second"
            )
        try:
            result += timedelta(seconds=1)  # POSIX/Unix 时间戳闰秒归一化
        except (OverflowError, ValueError):
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
    include_calculation_trace: StrictBool = Field(
        default=False,
        description=(
            "可选：为 true 时响应额外携带 calculation_trace——从实际起算时刻"
            "（exposure_origin_at_utc）到 pickup_at 的 exposed/dry 计算片段"
            "（UTC 起止与秒数，按时间顺序、互不重叠、秒数之和等于 "
            "total_seconds），供复核回干区间如何影响结论；省略或为 false 时"
            "响应结构与旧版完全一致。必须是布尔值，其他类型一律 422。"
        ),
    )


class JudgeBatchRequest(BaseModel):
    """批量判定请求：1–100 个按顺序排列的现有单盘判定请求。"""

    model_config = ConfigDict(extra="forbid")

    items: list[JudgeRequest] = Field(
        min_length=1,
        max_length=100,
        description=(
            "按顺序排列的单盘判定请求（与 POST /judge 请求体同构），数量必须在 "
            "[1, 100]；任一料盘不合法则整批 422，错误 loc 在原字段路径前插入 "
            "items 与对应下标。"
        ),
    )


class CalculationTraceSegment(BaseModel):
    """一段计算片段：[start_at_utc, end_at_utc) 内要么计入有效暴露
    （exposed），要么处于回干柜暂停（dry）。"""

    kind: Literal["exposed", "dry"] = Field(
        description="exposed=计入有效暴露的时间；dry=回干暂停、不计入的时间。"
    )
    start_at_utc: datetime
    end_at_utc: datetime
    seconds: int = Field(
        description="片段时长（秒），等于 end_at_utc − start_at_utc。"
    )

    @field_serializer("start_at_utc", "end_at_utc")
    def _serialize_utc(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    calculation_trace: list[CalculationTraceSegment] | None = Field(
        default=None,
        description=(
            "仅当请求 include_calculation_trace=true 时返回：按时间顺序覆盖 "
            "[exposure_origin_at_utc, pickup_at_utc] 的计算片段，互不重叠，"
            "秒数之和等于 total_seconds（其中 dry 片段秒数之和等于 "
            "dry_seconds）。未请求时响应不携带该字段。"
        ),
    )

    @field_serializer("opened_at_utc", "pickup_at_utc", "exposure_origin_at_utc")
    def _serialize_utc(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BatchSummary(BaseModel):
    """整批三种既有结论的数量汇总。"""

    available: int = Field(description="结论为 available 的料盘数量。")
    boundary_available: int = Field(
        description="结论为 boundary_available 的料盘数量。"
    )
    expired: int = Field(description="结论为 expired 的料盘数量。")


class JudgeBatchResponse(BaseModel):
    """批量判定响应：结果按输入顺序完整返回，并汇总三种结论的数量。"""

    items: list[JudgeResponse] = Field(
        description="按输入顺序排列的完整单盘判定结果。"
    )
    summary: BatchSummary
