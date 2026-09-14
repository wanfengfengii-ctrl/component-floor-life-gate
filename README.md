# MSL Floor-Life Judge（湿敏元件上线判定 API）

贴片线临时领用已开封湿敏元件时，本服务把墙钟经过时间扣除回干柜内的暂停时段，
判定料盘能否上线。纯后端 API：Python 3.12 + FastAPI + Pydantic，pytest 验收。

## 判定规则

- 等级与允许有效暴露分钟（固定）：`MSL2=525600`、`MSL3=168`、`MSL4=72`。
- 所有时间必须是**带时区的 RFC3339 秒精度**时间
  （`2026-09-01T08:00:00+08:00` 或 `2026-09-01T00:00:00Z`；拒绝裸时间与小数秒），
  一律先换算为 UTC 再比较。秒值 60 仅在真实闰秒时刻接受（依据 IERS 闰秒表，
  均为 UTC 23:59:60，如 `2016-12-31T23:59:60Z`，归一化到下一分钟 `:00`，
  POSIX/Unix 时间戳语义）；非闰秒时刻写 `:60` 一律 422。`-00:00` 在 RFC3339
  中表示“本地偏移未知”，无法确定实际 UTC 时刻，整单 422（表达 UTC 请用 `Z`
  或 `+00:00`）；时间字符串本身合法但换算为 UTC 后越过可表示日期边界的，
  同样 422，`loc` 精确定位到对应时间字段（含数组下标）。
- `opened_at` 不得晚于 `pickup_at`（相等合法，暴露为 0）。
- 每个回干区间须 `start < end`，且完整落在 `[opened_at, pickup_at]` 内
  （允许贴着两端边界）。
- 回干区间两两不得相交，也不得端点相接（前一段 `end` 必须严格小于后一段 `start`）。
- 可选的 `rebake_completed_at` 表示该已开封料盘**一次合格烘烤的完成时刻**，
  适用于领取曾完成合格烘烤的料盘：
  - 必须严格位于 `(opened_at, pickup_at)` 内，且不能落在任何回干区间内或与其
    端点重合，否则整单 422，`loc` 精确定位到 `rebake_completed_at`；
  - 提供后有效暴露从该时刻**重新累计**，并继续扣除该时刻**之后**的回干区间
    （该时刻之前的区间不再影响结论）；
  - 省略（或为 `null`）时从 `opened_at` 起算，计算与旧版完全一致。
- `有效暴露分钟 = floor((总秒数 − 回干秒数合计) / 60)`；重新累计时总秒数为
  `pickup_at − 起算时刻`，回干秒数只统计起算时刻之后的区间。
- 结论唯一：小于限额 `available`，等于限额 `boundary_available`，大于限额 `expired`；
  同时返回非负的 `remaining_minutes`（剩余）或 `exceeded_minutes`（超出）。
- 任一等级未知、时间倒置或区间非法：整单返回 **422**，不给出部分判定；
  错误体为 `{"detail": [{"loc": ["body", ...], "msg": ..., "type": ...}]}`，
  `loc` 可定位到具体字段（含数组下标）。
- 批量接口 `POST /judge/batch` 接收 **1–100 个按顺序排列**的单盘请求，
  完全复用上述时间解析、烘烤重置、回干校验与暴露计算；任一料盘非法则
  整批 422 且不产出任何判定，错误 `loc` 在原字段路径前插入 `items` 与
  对应下标（如 `["body", "items", 3, "dry_intervals", 0, "end"]`）；
  `items` 缺失、为空或超过 100 项同样 422，定位 `["body", "items"]`。

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

`dry_intervals` 可省略或为空数组。可选字段 `rebake_completed_at` 为带时区
RFC3339 秒精度时间；提供后暴露从烘烤完成时刻重新累计。

```json
{
  "level": "MSL3",
  "opened_at": "2026-09-01T08:00:00+08:00",
  "pickup_at": "2026-09-01T12:00:00+08:00",
  "rebake_completed_at": "2026-09-01T10:00:00+08:00",
  "dry_intervals": [
    {"start": "2026-09-01T00:30:00Z", "end": "2026-09-01T01:00:00Z"},
    {"start": "2026-09-01T02:30:00Z", "end": "2026-09-01T03:00:00Z"}
  ]
}
```

上例：起算（烘烤完成）02:00Z → pickup 04:00Z，总 7200 秒；02:30–03:00Z
的回干在烘烤之后，继续扣除 1800 秒；00:30–01:00Z 的区间在烘烤之前，不影响结论。

响应 `200`（不传 `rebake_completed_at`，兼容旧客户端；新增字段为默认值）：

```json
{
  "verdict": "available",
  "limit_minutes": 168,
  "effective_exposure_minutes": 120,
  "remaining_minutes": 48,
  "exceeded_minutes": 0,
  "opened_at_utc": "2026-09-01T00:00:00Z",
  "pickup_at_utc": "2026-09-01T02:30:00Z",
  "exposure_origin_at_utc": "2026-09-01T00:00:00Z",
  "reset_applied": false,
  "total_seconds": 9000,
  "dry_seconds": 1800,
  "effective_seconds": 7200
}
```

响应 `200`（传入 `rebake_completed_at`；`exposure_origin_at_utc` 即烘烤完成
时刻 UTC，`reset_applied=true`）：

```json
{
  "verdict": "available",
  "limit_minutes": 168,
  "effective_exposure_minutes": 90,
  "remaining_minutes": 78,
  "exceeded_minutes": 0,
  "opened_at_utc": "2026-09-01T00:00:00Z",
  "pickup_at_utc": "2026-09-01T04:00:00Z",
  "exposure_origin_at_utc": "2026-09-01T02:00:00Z",
  "reset_applied": true,
  "total_seconds": 7200,
  "dry_seconds": 1800,
  "effective_seconds": 5400
}
```

响应 `422`（示例：区间端点相接；烘烤时刻与回干冲突时 `loc` 为
`["body", "rebake_completed_at"]`，`type` 为
`value_error.rebake_dry_interval_conflict`）：

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

### `POST /judge/batch`

供生产排程员一次评估同批料盘，减少逐盘往返。请求体为
`{"items": [ ... ]}`，`items` 为 **1–100 个按顺序排列**的 `POST /judge`
同构请求（字段、时间格式、默认值规则完全一致）：

```json
{
  "items": [
    {
      "level": "MSL3",
      "opened_at": "2026-09-01T00:00:00Z",
      "pickup_at": "2026-09-01T01:00:00Z"
    },
    {
      "level": "MSL3",
      "opened_at": "2026-09-01T00:00:00Z",
      "pickup_at": "2026-09-01T02:48:00Z"
    },
    {
      "level": "MSL4",
      "opened_at": "2026-09-01T00:00:00Z",
      "pickup_at": "2026-09-01T02:00:00Z"
    }
  ]
}
```

响应 `200`：`items` 为按**输入顺序**排列的完整单盘结果（与逐盘调用
`POST /judge` 的响应体逐字段一致），`summary` 汇总三种既有结论的数量：

```json
{
  "items": [
    {"verdict": "available", "...": "..."},
    {"verdict": "boundary_available", "...": "..."},
    {"verdict": "expired", "...": "..."}
  ],
  "summary": {
    "available": 1,
    "boundary_available": 1,
    "expired": 1
  }
}
```

非法请求整批返回 **422**，不产出任何判定结果（响应无 `items`）：

- 任一料盘不合法（模型错误或业务规则错误）：错误 `loc` 在单盘字段路径前
  插入 `items` 与下标，例如时间倒置为
  `["body", "items", 1, "pickup_at"]`，下标 2 的第 1 个回干区间零长为
  `["body", "items", 2, "dry_intervals", 0, "end"]`；同一批多个料盘违规时
  错误一次性全部返回；
- `items` 缺失、不是数组、为空（`too_short`）或超过 100 项
  （`too_long`）：`loc` 为 `["body", "items"]`；
- 顶层多余字段同样 422（如 `["body", "note"]`）。

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
  main.py      # FastAPI 入口：/judge、/judge/batch、/healthz
  schemas.py   # 请求/响应模型（含批量模型），RFC3339 秒精度时间解析（归一化为 UTC）
  logic.py     # 业务规则校验（422 收集，支持批量 loc 前缀）与暴露时长判定
tests/
  test_api.py  # 62 条验收测试：单盘合法/边界/过期/烘烤重置/各类 422 + 批量顺序汇总/嵌套定位/数量边界
Dockerfile
docker-compose.yml   # api（常驻）+ verify（一次性验收，profile=verify）
requirements.txt
```
