# MSL Floor-Life Judge（湿敏元件上线判定 API）

贴片线临时领用已开封湿敏元件时，本服务把墙钟经过时间扣除回干柜内的暂停时段，
判定料盘能否上线。纯后端 API：Python 3.12 + FastAPI + Pydantic，pytest 验收。

## 判定规则

- 等级与允许有效暴露分钟（固定）：`MSL2=525600`、`MSL3=168`、`MSL4=72`。
- 所有时间必须是**带时区的 RFC3339 秒精度**时间
  （`2026-09-01T08:00:00+08:00` 或 `2026-09-01T00:00:00Z`；拒绝裸时间与小数秒），
  一律先换算为 UTC 再比较。
- `opened_at` 不得晚于 `pickup_at`（相等合法，暴露为 0）。
- 每个回干区间须 `start < end`，且完整落在 `[opened_at, pickup_at]` 内
  （允许贴着两端边界）。
- 回干区间两两不得相交，也不得端点相接（前一段 `end` 必须严格小于后一段 `start`）。
- `有效暴露分钟 = floor((总秒数 − 回干秒数合计) / 60)`。
- 结论唯一：小于限额 `available`，等于限额 `boundary_available`，大于限额 `expired`；
  同时返回非负的 `remaining_minutes`（剩余）或 `exceeded_minutes`（超出）。
- 任一等级未知、时间倒置或区间非法：整单返回 **422**，不给出部分判定；
  错误体为 `{"detail": [{"loc": ["body", ...], "msg": ..., "type": ...}]}`，
  `loc` 可定位到具体字段（含数组下标）。

## API

### `POST /judge`

请求：

```json
{
  "level": "MSL3",
  "opened_at": "2026-09-01T08:00:00+08:00",
  "pickup_at": "2026-09-01T10:30:00+08:00",
  "dry_intervals": [
    {"start": "2026-09-01T00:30:00Z", "end": "2026-09-01T01:00:00Z"}
  ]
}
```

`dry_intervals` 可省略或为空数组。

响应 `200`：

```json
{
  "verdict": "available",
  "limit_minutes": 168,
  "effective_exposure_minutes": 120,
  "remaining_minutes": 48,
  "exceeded_minutes": 0,
  "opened_at_utc": "2026-09-01T00:00:00Z",
  "pickup_at_utc": "2026-09-01T02:30:00Z",
  "total_seconds": 9000,
  "dry_seconds": 1800,
  "effective_seconds": 7200
}
```

响应 `422`（示例：区间端点相接）：

```json
{
  "detail": [
    {
      "loc": ["body", "dry_intervals", 1, "start"],
      "msg": "dry interval #1 overlaps or touches dry interval #0; ...",
      "type": "value_error.dry_intervals_overlap"
    }
  ]
}
```

### `GET /healthz`

健康检查，返回 `{"status": "ok"}`。

## 运行（Docker Compose）

仅需 Docker，无需本地 Python 环境。Compose 只常驻运行 API；宿主端口由
`API_PORT` 覆盖（默认 8000）：

```bash
API_PORT=9000 docker compose up --build api
# → http://localhost:9000/judge
```

### 一次性验收服务 `verify`

`verify` 依赖 `api` 健康后启动，对运行中的 API 发起真实 HTTP 请求跑完整
pytest 验收套件，结束后退出（退出码即测试结果）：

```bash
docker compose --profile verify up --build --exit-code-from verify
# 或
docker compose run --rm verify
```

## 本地开发

```bash
pip install -r requirements.txt
pytest                                   # 进程内 TestClient 跑全量测试
uvicorn app.main:app --port 8000         # 本地起服务
VERIFY_BASE_URL=http://127.0.0.1:8000 pytest   # 对运行中的服务做黑盒验收
```

## 项目结构

```
app/
  main.py      # FastAPI 入口：/judge、/healthz
  schemas.py   # 请求/响应模型，RFC3339 秒精度时间解析（归一化为 UTC）
  logic.py     # 业务规则校验（422 收集）与暴露时长判定
tests/
  test_api.py  # 28 条验收测试：合法/边界/过期/各类 422
Dockerfile
docker-compose.yml   # api（常驻）+ verify（一次性验收，profile=verify）
requirements.txt
```
