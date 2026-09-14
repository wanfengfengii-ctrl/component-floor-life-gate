"""判定 API 的验收测试。

默认通过 FastAPI TestClient 在进程内测试；设置 VERIFY_BASE_URL 环境变量时，
改为对运行中的服务发起真实 HTTP 请求（docker compose 的 verify 服务使用）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

BASE_URL = os.environ.get("VERIFY_BASE_URL")


@pytest.fixture(scope="session")
def client():
    if BASE_URL:
        with httpx.Client(base_url=BASE_URL, timeout=10.0) as http_client:
            yield http_client
    else:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as test_client:
            yield test_client


def fmt(dt: datetime) -> str:
    """格式化为 RFC3339 秒精度字符串（UTC 用 Z，其余保留偏移）。"""
    dt = dt.astimezone(dt.tzinfo or timezone.utc)
    if dt.utcoffset() == timedelta(0):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.isoformat(timespec="seconds")


UTC = timezone.utc
CST = timezone(timedelta(hours=8))  # +08:00

OPENED = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


def payload(**overrides):
    base = {
        "level": "MSL3",
        "opened_at": fmt(OPENED),
        "pickup_at": fmt(OPENED + timedelta(hours=1)),
        "dry_intervals": [],
    }
    base.update(overrides)
    return base


def judge(client, body):
    return client.post("/judge", json=body)


# ---------------------------------------------------------------- 合法请求


def test_available_with_dry_interval_and_mixed_timezones(client):
    # opened 00:00Z，pickup 02:30Z → 总 9000 秒；回干 30 分钟（用 +08:00 表达）。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2, minutes=30)),
        dry_intervals=[
            {
                "start": "2026-09-01T08:30:00+08:00",  # = 00:30Z
                "end": "2026-09-01T09:00:00+08:00",    # = 01:00Z
            }
        ],
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["verdict"] == "available"
    assert data["limit_minutes"] == 168
    assert data["total_seconds"] == 9000
    assert data["dry_seconds"] == 1800
    assert data["effective_seconds"] == 7200
    assert data["effective_exposure_minutes"] == 120
    assert data["remaining_minutes"] == 48
    assert data["exceeded_minutes"] == 0
    assert data["opened_at_utc"] == "2026-09-01T00:00:00Z"
    assert data["pickup_at_utc"] == "2026-09-01T02:30:00Z"


def test_opened_at_in_non_utc_timezone_converted(client):
    # 2026-09-01T08:00:00+08:00 即 2026-09-01T00:00:00Z。
    body = payload(
        opened_at="2026-09-01T08:00:00+08:00",
        pickup_at="2026-09-01T10:48:00+08:00",  # 168 分钟整
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["opened_at_utc"] == "2026-09-01T00:00:00Z"
    assert data["effective_exposure_minutes"] == 168
    assert data["verdict"] == "boundary_available"
    assert data["remaining_minutes"] == 0
    assert data["exceeded_minutes"] == 0


def test_boundary_available_exactly_at_limit(client):
    body = payload(pickup_at=fmt(OPENED + timedelta(minutes=168)))
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["verdict"] == "boundary_available"
    assert data["effective_exposure_minutes"] == 168
    assert data["remaining_minutes"] == 0
    assert data["exceeded_minutes"] == 0


def test_expired_when_over_limit(client):
    body = payload(
        level="MSL4",
        pickup_at=fmt(OPENED + timedelta(hours=2)),  # 120 分钟 > 72
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["verdict"] == "expired"
    assert data["limit_minutes"] == 72
    assert data["remaining_minutes"] == 0
    assert data["exceeded_minutes"] == 48


def test_effective_minutes_rounded_down(client):
    # 119 秒有效暴露 → 向下取整为 1 分钟。
    body = payload(
        level="MSL4",
        pickup_at=fmt(OPENED + timedelta(seconds=119)),
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["effective_seconds"] == 119
    assert data["effective_exposure_minutes"] == 1
    assert data["verdict"] == "available"
    assert data["remaining_minutes"] == 71


def test_msl2_long_limit(client):
    body = payload(
        level="MSL2",
        pickup_at=fmt(OPENED + timedelta(days=10)),  # 14400 分钟
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["verdict"] == "available"
    assert data["limit_minutes"] == 525600
    assert data["remaining_minutes"] == 525600 - 14400


def test_opened_equals_pickup_is_zero_exposure(client):
    body = payload(pickup_at=fmt(OPENED))
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_seconds"] == 0
    assert data["effective_exposure_minutes"] == 0
    assert data["verdict"] == "available"
    assert data["remaining_minutes"] == 168


def test_dry_intervals_may_touch_outer_bounds(client):
    # 回干区间允许贴着 opened_at / pickup_at。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=4)),
        dry_intervals=[
            {"start": fmt(OPENED), "end": fmt(OPENED + timedelta(hours=1))},
            {
                "start": fmt(OPENED + timedelta(hours=3)),
                "end": fmt(OPENED + timedelta(hours=4)),
            },
        ],
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_seconds"] == 7200
    assert data["effective_exposure_minutes"] == 120
    assert data["verdict"] == "available"


def test_unsorted_dry_intervals_accepted(client):
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=5)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(hours=3)),
                "end": fmt(OPENED + timedelta(hours=4)),
            },
            {
                "start": fmt(OPENED + timedelta(hours=1)),
                "end": fmt(OPENED + timedelta(hours=2)),
            },
        ],
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dry_seconds"] == 7200
    assert data["effective_exposure_minutes"] == 180


def test_dry_intervals_default_to_empty(client):
    body = payload()
    del body["dry_intervals"]
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_seconds"] == 0


# ------------------------------------------------ 合格烘烤后重新累计（rebake）


def test_rebake_resets_exposure_and_makes_reel_available(client):
    # 不烘烤：opened → pickup 共 200 分钟 > 168，必然 expired。
    expired_body = payload(pickup_at=fmt(OPENED + timedelta(minutes=200)))
    expired_resp = judge(client, expired_body)
    assert expired_resp.status_code == 200, expired_resp.text
    assert expired_resp.json()["verdict"] == "expired"

    # 在 190 分钟处完成合格烘烤，重新累计 10 分钟 → available。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(minutes=200)),
        rebake_completed_at=fmt(OPENED + timedelta(minutes=190)),
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reset_applied"] is True
    assert data["exposure_origin_at_utc"] == fmt(OPENED + timedelta(minutes=190))
    assert data["opened_at_utc"] == "2026-09-01T00:00:00Z"
    assert data["pickup_at_utc"] == fmt(OPENED + timedelta(minutes=200))
    assert data["total_seconds"] == 600
    assert data["dry_seconds"] == 0
    assert data["effective_seconds"] == 600
    assert data["effective_exposure_minutes"] == 10
    assert data["verdict"] == "available"
    assert data["remaining_minutes"] == 158
    assert data["exceeded_minutes"] == 0


def test_rebake_boundary_available_after_reset(client):
    # 重新累计恰好 168 分钟 → boundary_available。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=10)),
        rebake_completed_at=fmt(OPENED + timedelta(hours=10) - timedelta(minutes=168)),
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reset_applied"] is True
    assert data["effective_exposure_minutes"] == 168
    assert data["verdict"] == "boundary_available"
    assert data["remaining_minutes"] == 0
    assert data["exceeded_minutes"] == 0


def test_dry_interval_after_rebake_still_deducted(client):
    # opened 00:00，rebake 02:00，pickup 04:00 → 重累计 120 分钟；
    # 02:30-03:00（烘烤后）30 分钟回干必须扣除；00:30-01:00（烘烤前）忽略。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=4)),
        rebake_completed_at=fmt(OPENED + timedelta(hours=2)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(hours=1)),
            },
            {
                "start": fmt(OPENED + timedelta(hours=2, minutes=30)),
                "end": fmt(OPENED + timedelta(hours=3)),
            },
        ],
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reset_applied"] is True
    assert data["exposure_origin_at_utc"] == fmt(OPENED + timedelta(hours=2))
    assert data["total_seconds"] == 7200
    assert data["dry_seconds"] == 1800
    assert data["effective_seconds"] == 5400
    assert data["effective_exposure_minutes"] == 90
    assert data["verdict"] == "available"
    assert data["remaining_minutes"] == 78


def test_rebake_timestamp_accepted_in_non_utc_timezone(client):
    # rebake 用 +08:00 表达：2026-09-01T10:00:00+08:00 = 02:00Z。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=4)),
        rebake_completed_at="2026-09-01T10:00:00+08:00",
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reset_applied"] is True
    assert data["exposure_origin_at_utc"] == "2026-09-01T02:00:00Z"
    assert data["total_seconds"] == 7200
    assert data["effective_exposure_minutes"] == 120


def test_rebake_inside_dry_interval_rejected(client):
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=4)),
        rebake_completed_at=fmt(OPENED + timedelta(minutes=45)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(hours=1)),
            }
        ],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "rebake_completed_at"))
    assert (
        resp.json()["detail"][0]["type"]
        == "value_error.rebake_dry_interval_conflict"
    )


def test_rebake_coinciding_with_dry_interval_endpoints_rejected(client):
    # 与区间 start 重合。
    body_start = payload(
        pickup_at=fmt(OPENED + timedelta(hours=4)),
        rebake_completed_at=fmt(OPENED + timedelta(minutes=30)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(hours=1)),
            }
        ],
    )
    resp_start = judge(client, body_start)
    assert_422_with_loc(resp_start, ("body", "rebake_completed_at"))

    # 与区间 end 重合。
    body_end = payload(
        pickup_at=fmt(OPENED + timedelta(hours=4)),
        rebake_completed_at=fmt(OPENED + timedelta(hours=1)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(hours=1)),
            }
        ],
    )
    resp_end = judge(client, body_end)
    assert_422_with_loc(resp_end, ("body", "rebake_completed_at"))


def test_rebake_equal_to_opened_or_pickup_rejected(client):
    # 必须严格位于 (opened_at, pickup_at)：与任一端点相等都拒绝。
    resp_opened = judge(
        client,
        payload(
            pickup_at=fmt(OPENED + timedelta(hours=2)),
            rebake_completed_at=fmt(OPENED),
        ),
    )
    assert_422_with_loc(resp_opened, ("body", "rebake_completed_at"))
    assert resp_opened.json()["detail"][0]["type"] == "value_error.rebake_out_of_range"

    resp_pickup = judge(
        client,
        payload(
            pickup_at=fmt(OPENED + timedelta(hours=2)),
            rebake_completed_at=fmt(OPENED + timedelta(hours=2)),
        ),
    )
    assert_422_with_loc(resp_pickup, ("body", "rebake_completed_at"))
    assert resp_pickup.json()["detail"][0]["type"] == "value_error.rebake_out_of_range"


def test_rebake_outside_window_rejected(client):
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2)),
        rebake_completed_at=fmt(OPENED + timedelta(hours=3)),
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "rebake_completed_at"))
    assert resp.json()["detail"][0]["type"] == "value_error.rebake_out_of_range"


def test_rebake_requires_rfc3339_seconds_with_timezone(client):
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2)),
        rebake_completed_at="2026-09-01T01:00:00",  # 裸时间
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "rebake_completed_at"))


def test_rebake_conflict_reports_field_alongside_other_violations(client):
    # rebake 落在零长区间上，既要报区间零长，也要报 rebake 冲突，整单 422。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2)),
        rebake_completed_at=fmt(OPENED + timedelta(hours=1)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(hours=1)),
                "end": fmt(OPENED + timedelta(hours=1)),
            }
        ],
    )
    resp = judge(client, body)
    assert resp.status_code == 422
    locs = [tuple(item["loc"]) for item in resp.json()["detail"]]
    assert ("body", "rebake_completed_at") in locs
    assert ("body", "dry_intervals", 0, "end") in locs


def test_omitting_rebake_keeps_legacy_calculation_and_response(client):
    # 旧请求（不带 rebake_completed_at）计算结果不变，新增字段给出兼容值。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2, minutes=30)),
        dry_intervals=[
            {
                "start": "2026-09-01T08:30:00+08:00",  # = 00:30Z
                "end": "2026-09-01T09:00:00+08:00",    # = 01:00Z
            }
        ],
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reset_applied"] is False
    assert data["exposure_origin_at_utc"] == data["opened_at_utc"]
    assert data["total_seconds"] == 9000
    assert data["dry_seconds"] == 1800
    assert data["effective_seconds"] == 7200
    assert data["effective_exposure_minutes"] == 120
    assert data["verdict"] == "available"


def test_explicit_null_rebake_matches_omitted(client):
    body = payload(rebake_completed_at=None)
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reset_applied"] is False
    assert data["exposure_origin_at_utc"] == data["opened_at_utc"]


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------- 非法请求


def assert_422_with_loc(resp, expected_loc):
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail, "detail must be a non-empty list"
    for item in detail:
        assert set(item) >= {"loc", "msg", "type"}
        assert item["loc"][0] == "body"
    locs = [tuple(item["loc"]) for item in detail]
    assert expected_loc in locs, f"{expected_loc} not in {locs}"
    # 整单拒绝：不得携带任何判定结果。
    assert "verdict" not in resp.json()


def test_unknown_level_rejected(client):
    resp = judge(client, payload(level="MSL1"))
    assert_422_with_loc(resp, ("body", "level"))


def test_opened_after_pickup_rejected(client):
    resp = judge(client, payload(pickup_at=fmt(OPENED - timedelta(seconds=1))))
    assert_422_with_loc(resp, ("body", "pickup_at"))
    detail = resp.json()["detail"]
    assert detail[0]["type"] == "value_error.time_inverted"


def test_naive_datetime_rejected(client):
    resp = judge(client, payload(opened_at="2026-09-01T00:00:00"))
    assert_422_with_loc(resp, ("body", "opened_at"))


def test_fractional_seconds_rejected(client):
    resp = judge(client, payload(pickup_at="2026-09-01T01:00:00.500Z"))
    assert_422_with_loc(resp, ("body", "pickup_at"))


def test_negative_zero_offset_rejected(client):
    # RFC3339：-00:00 表示“本地偏移未知”，无法确定实际 UTC 时刻，必须拒绝，
    # 不得当作标准时区 +00:00 参与换算。
    resp = judge(client, payload(opened_at="2026-09-01T00:00:00-00:00"))
    assert_422_with_loc(resp, ("body", "opened_at"))

    # +00:00 是确定的 UTC，仍然合法。
    ok_resp = judge(client, payload(opened_at="2026-09-01T00:00:00+00:00"))
    assert ok_resp.status_code == 200, ok_resp.text


def test_timestamp_crossing_datetime_boundary_rejected_with_field_loc(client):
    # 字符串格式合法，但换算为 UTC 后越过 datetime 可表示的下边界
    # （0001-01-01T00:00:00+08:00 = 公元前一年 16:00Z）：必须 422 且 loc 精确到
    # 时间字段，而不是服务异常。
    resp = judge(
        client,
        payload(
            opened_at="0001-01-01T00:00:00+08:00",
            pickup_at="0001-01-01T01:00:00+08:00",
        ),
    )
    assert_422_with_loc(resp, ("body", "opened_at"))

    # 越过上边界（9999-12-31T23:59:59-08:00 换算到次日）同样 422。
    resp_upper = judge(
        client,
        payload(
            opened_at="9999-12-31T23:59:59-08:00",
            pickup_at="9999-12-31T23:59:59-01:00",
        ),
    )
    assert_422_with_loc(resp_upper, ("body", "opened_at"))

    # 定位必须能深入到回干区间内的具体时间字段。
    resp_nested = judge(
        client,
        payload(
            opened_at="0001-01-01T08:00:00Z",
            pickup_at="0001-01-01T09:00:00Z",
            dry_intervals=[
                {
                    "start": "0001-01-01T00:00:00+08:00",
                    "end": "0001-01-01T08:30:00Z",
                }
            ],
        ),
    )
    assert_422_with_loc(resp_nested, ("body", "dry_intervals", 0, "start"))


def test_timestamp_near_boundary_that_stays_in_range_accepted(client):
    # 对照：换算后仍在可表示范围内的边界时刻必须接受，不能误杀。
    body = payload(
        opened_at="0001-01-01T00:00:00-08:00",  # = 0001-01-01T08:00:00Z
        pickup_at="0001-01-01T09:00:00Z",
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_seconds"] == 3600


def test_date_only_rejected(client):
    resp = judge(client, payload(opened_at="2026-09-01"))
    assert_422_with_loc(resp, ("body", "opened_at"))


def test_invalid_calendar_date_rejected(client):
    resp = judge(client, payload(opened_at="2026-02-30T00:00:00Z"))
    assert_422_with_loc(resp, ("body", "opened_at"))


def test_zero_length_interval_rejected(client):
    body = payload(
        dry_intervals=[{"start": fmt(OPENED), "end": fmt(OPENED)}],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 0, "end"))


def test_inverted_interval_rejected(client):
    body = payload(
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(minutes=10)),
            }
        ],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 0, "end"))


def test_interval_starting_before_opened_rejected(client):
    body = payload(
        dry_intervals=[
            {
                "start": fmt(OPENED - timedelta(minutes=5)),
                "end": fmt(OPENED + timedelta(minutes=5)),
            }
        ],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 0, "start"))


def test_interval_ending_after_pickup_rejected(client):
    body = payload(
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(hours=2)),
            }
        ],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 0, "end"))


def test_overlapping_intervals_rejected(client):
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=3)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=10)),
                "end": fmt(OPENED + timedelta(minutes=40)),
            },
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(minutes=50)),
            },
        ],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 1, "start"))
    assert resp.json()["detail"][0]["type"] == "value_error.dry_intervals_overlap"


def test_touching_intervals_rejected(client):
    # 端点相接（前一段 end == 后一段 start）同样非法。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=3)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=10)),
                "end": fmt(OPENED + timedelta(minutes=40)),
            },
            {
                "start": fmt(OPENED + timedelta(minutes=40)),
                "end": fmt(OPENED + timedelta(minutes=50)),
            },
        ],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 1, "start"))


def test_overlapping_unsorted_intervals_rejected(client):
    # 乱序提交的重叠区间也必须被识别。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=3)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(minutes=50)),
            },
            {
                "start": fmt(OPENED + timedelta(minutes=10)),
                "end": fmt(OPENED + timedelta(minutes=40)),
            },
        ],
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 0, "start"))


def test_multiple_violations_reported_together(client):
    body = payload(
        pickup_at=fmt(OPENED - timedelta(hours=1)),  # 时间倒置
        dry_intervals=[{"start": fmt(OPENED), "end": fmt(OPENED)}],  # 零长区间
    )
    resp = judge(client, body)
    assert resp.status_code == 422
    types = {item["type"] for item in resp.json()["detail"]}
    assert "value_error.time_inverted" in types
    assert "value_error.dry_interval_not_positive" in types


def test_extra_field_rejected(client):
    body = payload(comment="extra")
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "comment"))


def test_missing_field_rejected(client):
    body = payload()
    del body["level"]
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "level"))


def test_interval_with_string_offset_z_and_lowercase(client):
    # 小写 z 同样属于 RFC3339。
    body = payload(
        pickup_at="2026-09-01T01:00:00z",
        dry_intervals=[{"start": "2026-09-01T00:10:00Z", "end": "2026-09-01T00:20:00Z"}],
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_seconds"] == 600


def test_leap_second_accepted_and_normalized_to_utc(client):
    # RFC3339 用 :60 表示闰秒：2016-12-31T23:59:60Z 归一化为 2017-01-01T00:00:00Z。
    body = payload(
        opened_at="2016-12-31T23:59:60Z",
        pickup_at="2017-01-01T00:02:00Z",
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["opened_at_utc"] == "2017-01-01T00:00:00Z"
    assert data["pickup_at_utc"] == "2017-01-01T00:02:00Z"
    assert data["total_seconds"] == 120
    assert data["verdict"] == "available"

    # 闰秒配非零偏移同样接受：07:59:60+08:00 即 23:59:60Z。
    body_offset = payload(
        opened_at="2017-01-01T07:59:60+08:00",
        pickup_at="2017-01-01T08:02:00+08:00",
    )
    resp_offset = judge(client, body_offset)
    assert resp_offset.status_code == 200, resp_offset.text
    assert resp_offset.json()["opened_at_utc"] == "2017-01-01T00:00:00Z"
