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


def test_non_leap_second_60_rejected(client):
    # 秒 60 只在真实闰秒（UTC 23:59:60 且日期在 IERS 闰秒表内）时合法；
    # 备料员把普通时刻误写成 60 秒必须收到 422，而不是被当作合法时间判定。
    fake_leap_inputs = [
        ("ordinary minute", "2026-09-01T08:00:60+08:00"),
        ("wrong second on leap date", "2016-12-31T23:58:60Z"),
        ("noon on leap date", "2016-12-31T12:59:60Z"),
        ("non-leap year-end", "2026-12-31T23:59:60Z"),
        ("date never announced", "2016-06-30T23:59:60Z"),
        ("midnight 60", "2026-09-01T00:00:60Z"),
    ]
    for label, opened in fake_leap_inputs:
        resp = judge(
            client,
            payload(
                opened_at=opened,
                pickup_at="2026-09-02T00:00:00Z",
            ),
        )
        assert_422_with_loc(resp, ("body", "opened_at")), label

    # 真实闰秒用偏移表达仍合法；对照非闰秒日期上的同一墙钟写法必须拒绝。
    ok_resp = judge(
        client,
        payload(
            opened_at="2017-01-01T07:59:60+08:00",  # = 2016-12-31T23:59:60Z
            pickup_at="2017-01-01T08:02:00+08:00",
        ),
    )
    assert ok_resp.status_code == 200, ok_resp.text
    bad_resp = judge(
        client,
        payload(
            opened_at="2017-01-01T07:59:60+09:00",  # 对应 UTC 22:59:60，非闰秒
            pickup_at="2017-01-01T08:02:00+09:00",
        ),
    )
    assert_422_with_loc(bad_resp, ("body", "opened_at"))


# ---------------------------------------------------- 批量判定 POST /judge/batch


def judge_batch(client, bodies):
    return client.post("/judge/batch", json={"items": bodies})


def available_body():
    # MSL3、有效暴露 60 分钟 → available。
    return payload(pickup_at=fmt(OPENED + timedelta(hours=1)))


def boundary_body():
    # MSL3、有效暴露恰好 168 分钟 → boundary_available。
    return payload(pickup_at=fmt(OPENED + timedelta(minutes=168)))


def expired_body():
    # MSL4、有效暴露 120 分钟 > 72 → expired。
    return payload(
        level="MSL4",
        pickup_at=fmt(OPENED + timedelta(hours=2)),
    )


VERDICT_BODIES = {
    "available": available_body,
    "boundary_available": boundary_body,
    "expired": expired_body,
}


def test_batch_mixed_verdicts_preserve_order_and_summary(client):
    order = ["expired", "boundary_available", "available",
             "boundary_available", "available"]
    bodies = [VERDICT_BODIES[name]() for name in order]
    resp = judge_batch(client, bodies)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 结果按输入顺序完整返回，字段与单盘响应一致。
    assert len(data["items"]) == 5
    assert [item["verdict"] for item in data["items"]] == order
    for item, body, name in zip(data["items"], bodies, order):
        single = judge(client, body)
        assert single.status_code == 200, single.text
        assert item == single.json()  # 批量结果必须与逐盘调用完全一致
        assert item["verdict"] == name

    # 三种既有结论数量汇总，便于排程员直接确认整批分布。
    assert data["summary"] == {
        "available": 2,
        "boundary_available": 2,
        "expired": 1,
    }


def test_batch_single_item_lower_bound(client):
    # 合法下限：恰好 1 项必须得到确定响应。
    resp = judge_batch(client, [available_body()])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["verdict"] == "available"
    assert data["summary"] == {
        "available": 1,
        "boundary_available": 0,
        "expired": 0,
    }


def test_batch_one_hundred_items_upper_bound(client):
    # 合法上限：恰好 100 项必须全部完成判定，顺序不重排。
    bodies = [expired_body()] + [available_body()] * 99
    resp = judge_batch(client, bodies)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["items"]) == 100
    assert data["items"][0]["verdict"] == "expired"
    assert all(item["verdict"] == "available" for item in data["items"][1:])
    assert data["summary"] == {
        "available": 99,
        "boundary_available": 0,
        "expired": 1,
    }
    # 每项的 opened/pickup 与输入下标对应，证明没有串行错位。
    assert data["items"][0]["limit_minutes"] == 72
    assert data["items"][1]["limit_minutes"] == 168


def test_batch_reuses_single_rebake_and_dry_interval_rules(client):
    # 烘烤重置与烘烤后回干扣除在批量链路中与单盘完全一致。
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
    single = judge(client, body)
    assert single.status_code == 200, single.text

    resp = judge_batch(client, [available_body(), body, expired_body()])
    assert resp.status_code == 200, resp.text
    middle = resp.json()["items"][1]
    assert middle == single.json()
    assert middle["reset_applied"] is True
    assert middle["effective_exposure_minutes"] == 90
    assert middle["verdict"] == "available"


def test_batch_empty_items_rejected_at_items(client):
    # 空批次无意义：422 且 loc 直接定位 items。
    resp = judge_batch(client, [])
    assert_422_with_loc(resp, ("body", "items"))
    assert resp.json()["detail"][0]["type"] == "too_short"
    assert "items" not in resp.json()  # 不产生判定结果


def test_batch_missing_items_rejected_at_items(client):
    resp = client.post("/judge/batch", json={})
    assert_422_with_loc(resp, ("body", "items"))
    assert resp.json()["detail"][0]["type"] == "missing"


def test_batch_items_must_be_list(client):
    resp = client.post("/judge/batch", json={"items": "nope"})
    assert_422_with_loc(resp, ("body", "items"))
    assert resp.json()["detail"][0]["type"] == "list_type"


def test_batch_101_items_rejected_at_items(client):
    # 超过一百项：422 且 loc 定位 items，防止单次计算量失控。
    resp = judge_batch(client, [available_body()] * 101)
    assert_422_with_loc(resp, ("body", "items"))
    detail = resp.json()["detail"][0]
    assert detail["type"] == "too_long"
    assert detail["ctx"]["max_length"] == 100
    assert detail["ctx"]["actual_length"] == 101
    assert "items" not in resp.json()  # 不产生部分判定


def test_batch_business_error_nested_with_items_and_index(client):
    # 下标 1 的料盘 pickup 早于 opened：loc 在原字段路径前加 items 与下标。
    bodies = [
        available_body(),
        payload(pickup_at=fmt(OPENED - timedelta(seconds=1))),
        expired_body(),
    ]
    resp = judge_batch(client, bodies)
    assert_422_with_loc(resp, ("body", "items", 1, "pickup_at"))
    assert resp.json()["detail"][0]["type"] == "value_error.time_inverted"
    assert "items" not in resp.json()  # 任一料盘非法 → 整批无判定结果


def test_batch_nested_dry_interval_error_includes_index(client):
    # 错误定位深入到具体料盘的具体回干区间字段。
    bodies = [
        available_body(),
        available_body(),
        payload(
            dry_intervals=[
                {
                    "start": fmt(OPENED + timedelta(minutes=30)),
                    "end": fmt(OPENED + timedelta(minutes=10)),
                }
            ]
        ),
    ]
    resp = judge_batch(client, bodies)
    assert_422_with_loc(
        resp, ("body", "items", 2, "dry_intervals", 0, "end")
    )


def test_batch_rebake_conflict_nested_with_index(client):
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
    resp = judge_batch(client, [available_body(), body])
    assert_422_with_loc(resp, ("body", "items", 1, "rebake_completed_at"))
    assert (
        resp.json()["detail"][0]["type"]
        == "value_error.rebake_dry_interval_conflict"
    )


def test_batch_collects_errors_from_multiple_invalid_items(client):
    # 多个料盘各自违规时错误一次性全部返回，下标互不混淆。
    bodies = [
        payload(pickup_at=fmt(OPENED - timedelta(seconds=1))),  # 0: 时间倒置
        available_body(),                                       # 1: 合法
        payload(
            dry_intervals=[
                {"start": fmt(OPENED), "end": fmt(OPENED)}      # 2: 零长区间
            ]
        ),
    ]
    resp = judge_batch(client, bodies)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    locs = [tuple(item["loc"]) for item in detail]
    assert ("body", "items", 0, "pickup_at") in locs
    assert ("body", "items", 2, "dry_intervals", 0, "end") in locs
    assert all(loc[:3] != ("body", "items", 1) for loc in locs)
    assert "items" not in resp.json()


def test_batch_schema_error_nested_with_index(client):
    # 模型层错误（未知等级 / 裸时间 / 小数秒）同样加 items 与下标前缀。
    bodies = [
        available_body(),
        payload(
            level="MSL1",
            opened_at="2026-09-01T00:00:00",  # 裸时间
        ),
        payload(pickup_at="2026-09-01T01:00:00.500Z"),  # 小数秒
    ]
    resp = judge_batch(client, bodies)
    assert resp.status_code == 422
    locs = [tuple(item["loc"]) for item in resp.json()["detail"]]
    assert ("body", "items", 1, "level") in locs
    assert ("body", "items", 1, "opened_at") in locs
    assert ("body", "items", 2, "pickup_at") in locs


def test_batch_extra_field_inside_item_nested(client):
    body = payload(comment="extra")
    resp = judge_batch(client, [available_body(), body])
    assert_422_with_loc(resp, ("body", "items", 1, "comment"))


def test_batch_extra_top_level_field_rejected(client):
    resp = client.post(
        "/judge/batch", json={"items": [available_body()], "note": 1}
    )
    assert_422_with_loc(resp, ("body", "note"))


def test_batch_zero_exposure_item_matches_single(client):
    # opened == pickup 的零暴露边界在批量中同样可用且结果与单盘一致。
    body = payload(pickup_at=fmt(OPENED))
    resp = judge_batch(client, [body])
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item == judge(client, body).json()
    assert item["verdict"] == "available"
    assert item["total_seconds"] == 0


def test_single_judge_still_available_after_batch_added(client):
    # 回归确认：新增批量接口后，单盘接口的请求、响应与错误格式保持不变。
    ok = judge(client, available_body())
    assert ok.status_code == 200
    assert ok.json()["verdict"] == "available"

    bad = judge(client, payload(level="MSL1"))
    assert_422_with_loc(bad, ("body", "level"))
    # 单盘错误路径保持原样，绝不能被加上 items 前缀。
    assert tuple(bad.json()["detail"][0]["loc"]) == ("body", "level")


# ------------------------------------ 计算片段 include_calculation_trace


def assert_trace_consistent(data):
    """片段通用不变量：覆盖 [起算点, 领用点]、首尾相接无重叠、秒数自洽且
    总和等于 total_seconds，dry 合计等于 dry_seconds。"""
    trace = data["calculation_trace"]
    assert [seg["kind"] for seg in trace]  # 非空场景调用
    assert trace[0]["start_at_utc"] == data["exposure_origin_at_utc"]
    assert trace[-1]["end_at_utc"] == data["pickup_at_utc"]
    for prev, curr in zip(trace, trace[1:]):
        assert prev["end_at_utc"] == curr["start_at_utc"]
    for seg in trace:
        start = datetime.strptime(seg["start_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(seg["end_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
        assert seg["seconds"] == int((end - start).total_seconds()) > 0
    assert sum(seg["seconds"] for seg in trace) == data["total_seconds"]
    dry = sum(seg["seconds"] for seg in trace if seg["kind"] == "dry")
    exposed = sum(seg["seconds"] for seg in trace if seg["kind"] == "exposed")
    assert dry == data["dry_seconds"]
    assert exposed == data["effective_seconds"]
    return trace


def test_trace_single_exposed_segment_when_no_dry_intervals(client):
    # 无回干：从起算点到领用时刻只有一段 exposed。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2, minutes=30)),
        include_calculation_trace=True,
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_seconds"] == 9000
    assert data["calculation_trace"] == [
        {
            "kind": "exposed",
            "start_at_utc": "2026-09-01T00:00:00Z",
            "end_at_utc": "2026-09-01T02:30:00Z",
            "seconds": 9000,
        }
    ]
    # 结论与汇总字段不受开关影响。
    assert data["verdict"] == "available"
    assert data["effective_exposure_minutes"] == 150


def test_trace_splits_multiple_dry_intervals_in_chronological_order(client):
    # 故意乱序提交两段回干：片段必须按时间顺序完整切分。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=5)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(hours=3)),
                "end": fmt(OPENED + timedelta(hours=3, minutes=30)),
            },
            {
                "start": fmt(OPENED + timedelta(hours=1)),
                "end": fmt(OPENED + timedelta(hours=2)),
            },
        ],
        include_calculation_trace=True,
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    trace = assert_trace_consistent(data)
    assert [seg["kind"] for seg in trace] == [
        "exposed", "dry", "exposed", "dry", "exposed",
    ]
    assert [(seg["start_at_utc"], seg["end_at_utc"]) for seg in trace] == [
        ("2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z"),
        ("2026-09-01T01:00:00Z", "2026-09-01T02:00:00Z"),
        ("2026-09-01T02:00:00Z", "2026-09-01T03:00:00Z"),
        ("2026-09-01T03:00:00Z", "2026-09-01T03:30:00Z"),
        ("2026-09-01T03:30:00Z", "2026-09-01T05:00:00Z"),
    ]
    assert [seg["seconds"] for seg in trace] == [3600, 3600, 3600, 1800, 5400]


def test_trace_interval_touching_origin_starts_with_dry_segment(client):
    # 回干区间贴着起算点：首段即为 dry，不产出零长 exposed 片段。
    body = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2)),
        dry_intervals=[
            {"start": fmt(OPENED), "end": fmt(OPENED + timedelta(hours=1))}
        ],
        include_calculation_trace=True,
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    trace = assert_trace_consistent(resp.json())
    assert [seg["kind"] for seg in trace] == ["dry", "exposed"]
    assert [seg["seconds"] for seg in trace] == [3600, 3600]


def test_trace_after_rebake_explains_only_time_after_new_origin(client):
    # opened 00:00，rebake 02:00，pickup 04:00；00:30–01:00 的回干在起算点
    # 之前，不得出现在片段里；片段必须从烘烤完成时刻开始。
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
        include_calculation_trace=True,
    )
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reset_applied"] is True
    assert data["calculation_trace"] == [
        {
            "kind": "exposed",
            "start_at_utc": "2026-09-01T02:00:00Z",
            "end_at_utc": "2026-09-01T02:30:00Z",
            "seconds": 1800,
        },
        {
            "kind": "dry",
            "start_at_utc": "2026-09-01T02:30:00Z",
            "end_at_utc": "2026-09-01T03:00:00Z",
            "seconds": 1800,
        },
        {
            "kind": "exposed",
            "start_at_utc": "2026-09-01T03:00:00Z",
            "end_at_utc": "2026-09-01T04:00:00Z",
            "seconds": 3600,
        },
    ]
    assert_trace_consistent(data)


def test_trace_zero_exposure_returns_empty_segment_list(client):
    # opened == pickup：没有可解释的时间，片段为空且秒数之和仍为 total_seconds。
    body = payload(pickup_at=fmt(OPENED), include_calculation_trace=True)
    resp = judge(client, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_seconds"] == 0
    assert data["calculation_trace"] == []


def test_trace_omitted_or_false_switch_adds_no_fields(client):
    # 旧调用（不传开关）与显式 false：单盘与批量响应结构保持原样。
    for override in ({}, {"include_calculation_trace": False}):
        body = payload(
            pickup_at=fmt(OPENED + timedelta(hours=2)),
            dry_intervals=[
                {
                    "start": fmt(OPENED + timedelta(minutes=30)),
                    "end": fmt(OPENED + timedelta(hours=1)),
                }
            ],
            **override,
        )
        resp = judge(client, body)
        assert resp.status_code == 200, resp.text
        assert "calculation_trace" not in resp.json()

    resp = judge_batch(client, [available_body(), boundary_body()])
    assert resp.status_code == 200, resp.text
    assert all("calculation_trace" not in item for item in resp.json()["items"])


def test_trace_non_boolean_switch_rejected_at_field(client):
    # 开关不是布尔值：422 且精确定位到该字段，不产出判定结果。
    for bad_value in ("yes", "true", 1, 0, ["true"], None):
        resp = judge(client, payload(include_calculation_trace=bad_value))
        assert_422_with_loc(resp, ("body", "include_calculation_trace")), bad_value


def test_trace_with_invalid_dry_or_rebake_data_uses_existing_error_chain(client):
    # 回干数据非法：即使开了开关也走现有错误链路，不返回任何计算结果。
    body = payload(
        dry_intervals=[{"start": fmt(OPENED), "end": fmt(OPENED)}],
        include_calculation_trace=True,
    )
    resp = judge(client, body)
    assert_422_with_loc(resp, ("body", "dry_intervals", 0, "end"))
    assert "calculation_trace" not in resp.json()

    # 烘烤数据非法：同样整单 422，无计算结果。
    body_rebake = payload(
        pickup_at=fmt(OPENED + timedelta(hours=4)),
        rebake_completed_at=fmt(OPENED + timedelta(minutes=45)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(hours=1)),
            }
        ],
        include_calculation_trace=True,
    )
    resp_rebake = judge(client, body_rebake)
    assert_422_with_loc(resp_rebake, ("body", "rebake_completed_at"))
    assert "calculation_trace" not in resp_rebake.json()


def test_batch_trace_only_for_items_that_request_it(client):
    # 批量：仅为主动开启的条目生成片段，顺序、汇总与逐盘一致性保持不变。
    traced = payload(
        pickup_at=fmt(OPENED + timedelta(hours=2)),
        dry_intervals=[
            {
                "start": fmt(OPENED + timedelta(minutes=30)),
                "end": fmt(OPENED + timedelta(hours=1)),
            }
        ],
        include_calculation_trace=True,
    )
    resp = judge_batch(client, [available_body(), traced, expired_body()])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert [item["verdict"] for item in data["items"]] == [
        "available", "available", "expired",
    ]
    assert "calculation_trace" not in data["items"][0]
    assert "calculation_trace" not in data["items"][2]

    traced_item = data["items"][1]
    trace = assert_trace_consistent(traced_item)
    assert [seg["kind"] for seg in trace] == ["exposed", "dry", "exposed"]
    # 与单盘调用结果逐字段一致。
    single = judge(client, traced)
    assert single.status_code == 200, single.text
    assert traced_item == single.json()

    assert data["summary"] == {
        "available": 2,
        "boundary_available": 0,
        "expired": 1,
    }


def test_batch_trace_non_boolean_located_with_item_index(client):
    # 批量中开关类型非法：422 且 loc 带 items 与下标，整批无判定结果。
    bodies = [available_body(), payload(include_calculation_trace="yes")]
    resp = judge_batch(client, bodies)
    assert_422_with_loc(resp, ("body", "items", 1, "include_calculation_trace"))
    assert "items" not in resp.json()
