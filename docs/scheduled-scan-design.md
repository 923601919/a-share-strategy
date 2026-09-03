# 服务端定时扫描方案设计

> 针对「软加权」与「进攻型分时」两个策略，在每个交易日 10:40 / 14:20 自动扫描并把全部命中标的加入对应自选列表。

## 1. 目标与范围

- **触发**：每个 A 股交易日 10:40（上午）、14:20（下午）各一次。
- **对象**：两个独立策略账号，各自拥有完全隔离的自选列表：
  - `soft` —— 软加权（`mode=fenshi` + `universe_policy=soft`）
  - `fenshi` —— 进攻型分时（`mode=fenshi` + `universe_policy=hot_only`）
- **动作**：扫描结果（`items`）中的**全部**标的写入对应账号自选列表，供后续 T+N 收益分析。

## 2. 调度配置

采用 **APScheduler 进程内 BackgroundScheduler**（版本 3.11.3，随 akshare 已在环境，正式依赖已加入 `requirements.txt`）。

| 配置项 | 值 | 说明 |
| ------ | -- | ---- |
| 调度器 | `BackgroundScheduler` | 随 uvicorn 生命周期启停，不阻塞主进程 |
| 触发器 | `CronTrigger(day_of_week="mon-fri")` | 周一到周五触发，交易日判断在任务内二次校验 |
| 时间点 | 10:40、14:20 | 见下方 job 注册表 |
| 时区 | `Asia/Shanghai`（`SCHEDULER_TIMEZONE`） | A 股时间 |
| `coalesce` | `True` | 错过的多次触发合并为一次，避免补跑堆积 |
| `max_instances` | `1` | 同一 job 禁止并发（配合后端单 worker） |
| `misfire_grace_time` | `300` 秒 | 错过 5 分钟内仍补跑（如进程短暂卡顿） |
| 开关 | `SCHEDULER_ENABLED=true` | 默认开启 |

**Job 注册表**（`_build_scheduler`，2 账号 × 2 时间点 = 4 个 job）：

| job id | 账号 | 时间 | mode / universe_policy | session |
| ------ | ---- | ---- | ---------------------- | ------- |
| `soft-morning` | soft | 10:40 | fenshi / soft | morning |
| `fenshi-morning` | fenshi | 10:40 | fenshi / hot_only | morning |
| `soft-afternoon` | soft | 14:20 | fenshi / soft | afternoon |
| `fenshi-afternoon` | fenshi | 14:20 | fenshi / hot_only | afternoon |

> 时间点落在既有时段窗口内：10:40 ∈ 上午窗口 09:45–11:30，14:20 ∈ 下午窗口 13:30–15:00，因此 `session` 用固定值即可被 `session_allowed` 放行。

## 3. 任务执行逻辑

```
触发(job) ──▶ 判断是否交易日 ──非交易日──▶ 跳过并记录
                    │交易日
                    ▼
           ensure_strategy_user(账号) 幂等建账号
                    ▼
           扫描 run_scan(session, mode, universe_policy)  ← 失败则退避重试(默认2次)
                    ▼ 成功
           命中 items 全部 add_watch 加入该账号自选
                    ▼
           记录 ScheduledScanResult 到进程内 stats
```

关键点：

- **用户上下文**：`run_scan` 内部会落 `scan_snapshot` / `scan_quality`，`add_watch` / `list_watchlist` 都调用 `require_user_id()`（依赖 ContextVar）。定时任务线程无 HTTP 请求上下文，因此全程用 `user_scope(uid)` 显式包裹。
- **账号隔离**：`watchlist` 主键 `(user_id, code)`，soft / fenshi 各写入自己的 `user_id`，互不可见。
- **自选来源**：`source` 标记为 `fenshi`（复用既有枚举，前端 `by_source` 统计已支持），`note` 记录触发时间便于追溯。
- **入池字段**：`entry_price` / `entry_pct` / `entry_score` 取扫描结果现价/涨幅/得分，`day_position` / `vwap_deviation` 取自结果 `fenshi` 子结构，供 T+N 分析对齐。

## 4. 扫描 → 加自选流程

1. `run_scan` 返回 payload：`items`（每项含 `code`/`name`/`price`/`pct`/`score`/`fenshi.day_position` 等）。
2. `_add_hits_to_watchlist(uid, hits, source)`：
   - 先取该账号已有自选代码集（`list_watchlist`）。
   - 逐个 `add_watch`；`watchlist` 的 `ON CONFLICT(user_id, code) DO UPDATE` 保证幂等。
   - 统计 `added`（新增）与 `skipped_duplicate`（已存在）。
3. 返回 `ScheduledScanResult`，写入进程内 `stats`（诊断用，保留最近 50 条）。

## 5. 异常与重试处理

| 场景 | 处理 |
| ---- | ---- |
| 非交易日 | 直接跳过（`is_trade_date` 用 akshare 交易日历，失败回退工作日近似） |
| 扫描抛异常 | 按 `max_retries`（默认 2 次）**指数退避**重试（30s × 尝试次数）；仍失败则本次记 `error`，**不产生任何自选脏数据** |
| 加自选抛异常 | **不重试**（避免重复写入），仅记 `error`；已写入的部分保留（幂等，下次触发会识别为重复） |
| 进程重启 | 内存 job 丢失；`startup` 重新注册；错过的触发由 `misfire_grace_time` + `coalesce` 决定是否补跑 |
| 多实例/多进程 | 后端强制单 worker（既有约定），进程内调度器天然单实例，无重复触发风险 |
| 快照裁剪 | `save_scan_snapshot` 只保留最近 30 条，但自选数据持久化在 `watchlist`/`watch_tracks`，不受影响 |

## 6. 代码实现清单

| 文件 | 改动 |
| ---- | ---- |
| `apps/api/services/scheduler.py` | **新增**：调度器核心（账号创建、扫描、加自选、重试、状态） |
| `apps/api/config.py` | 新增 `scheduler_enabled`、`scheduler_timezone` 两个配置 |
| `apps/api/main.py` | `startup` 启动调度器、`shutdown` 停止；`/api/health` 暴露 `scheduler` 状态 |
| `apps/api/requirements.txt` | 新增 `apscheduler>=3.10.0` |
| `.env.example` | 新增 `SCHEDULER_ENABLED`、`SCHEDULER_TIMEZONE` 说明 |
| `apps/api/tests/test_scheduler.py` | **新增** 7 个单测（幂等、隔离、非交易日、重试、成功加自选） |

## 7. 验证结果

- 单测：`tests/test_scheduler.py` **7 passed**；完整套件 **97 passed**。
- Job 注册：4 个 job，下次触发 2026-09-04（周五）10:40 / 14:20，时区 +08:00 正确。
- 端到端（demo 模式）：扫描命中 3 只 → 全部加入 soft 账号自选，链路通。
