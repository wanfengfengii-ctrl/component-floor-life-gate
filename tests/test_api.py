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
