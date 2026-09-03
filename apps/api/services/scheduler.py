"""服务端定时扫描任务：交易日 10:40 / 14:20 自动扫描并加入对应策略账号的自选列表。

设计目标
--------
- 两个独立策略账号（软加权 soft / 进攻型分时 fenshi），各自拥有完全隔离的自选列表。
- 每个交易日两个时间点（默认 10:40 上午、14:20 下午）各触发一轮扫描。
- 扫描结果的全部标的自动写入对应账号的自选列表（source 标记为策略来源），供后续数据分析。

关键约束（与本项目既有约定对齐）
--------------------------------
- 本项目后端是单 worker（内存任务队列 + spawn 隔离子进程不支持多进程共享），
  因此调度器用「进程内 BackgroundScheduler」，随 uvicorn 生命周期启停，天然无多实例重复触发问题。
- 自选表 watchlist 主键 (user_id, code)，所有写操作依赖 user_ctx 的 ContextVar；
  定时任务线程没有 HTTP 请求上下文，必须用 user_scope(uid) 显式包裹。
- run_scan 内部会落 scan_snapshot / scan_quality（均 require_user_id），同样须在 user_scope 内执行。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from db import add_watch, create_user, get_user_by_username, list_watchlist
from services.trade_calendar import is_trade_date
from user_ctx import user_scope

logger = logging.getLogger("scheduler")

# 策略账号名（软加权 / 进攻型分时）——两个独立用户，各自隔离的自选列表
STRATEGY_SOFT = "soft"       # 软加权（universe_policy=soft）
STRATEGY_FENSHI = "fenshi"   # 进攻型分时（mode=fenshi + hot_only）

# 自选来源标记：与 WatchIn.source 的 Literal 一致，便于前端 by_source 统计
SOURCE_SOFT = "fenshi"       # 软加权落库 source（复用 fenshi 来源标记，前端已支持）
SOURCE_FENSHI = "fenshi"

# 扫描时点 → session 映射（精确对齐交易时段窗口：morning 09:45-11:30 / afternoon 13:30-15:00）
_HM_SESSION = {
    "10:40": "morning",
    "14:20": "afternoon",
}


@dataclass
class ScheduledScanResult:
    """一次定时扫描 + 加自选的完整结果，用于日志与告警。"""
    account: str
    fired_at: str
    trade_date: bool
    scanned: bool
    hit_count: int
    added: int
    skipped_duplicate: int
    error: str | None
    retries: int
    elapsed_ms: float
    note: str


@dataclass
class ScheduledRunStats:
    """进程内调度统计（诊断用，非持久化）。"""
    runs: list[ScheduledScanResult] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    max_keep: int = 50

    def record(self, r: ScheduledScanResult) -> None:
        with self.lock:
            self.runs.append(r)
            if len(self.runs) > self.max_keep:
                self.runs = self.runs[-self.max_keep:]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock:
            return [r.__dict__ for r in self.runs[-limit:]]


stats = ScheduledRunStats()


def ensure_strategy_user(username: str) -> int:
    """按用户名幂等创建策略账号，返回 user_id。

    账号仅作为扫描结果的隔离容器，密码随机且不可登录（内部专用）。
    """
    existing = get_user_by_username(username)
    if existing:
        return int(existing["id"])
    import secrets

    from auth import hash_password

    row = create_user(
        username=username,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        role="user",
    )
    logger.info("created strategy account %s (user_id=%s)", username, row["id"])
    return int(row["id"])


def _add_hits_to_watchlist(
    uid: int,
    hits: list[dict[str, Any]],
    source: str,
) -> tuple[int, int]:
    """把扫描命中标的加入指定账号自选。返回 (added, skipped_duplicate)。

    幂等：watchlist 主键 (user_id, code)，重复添加走 ON CONFLICT DO UPDATE，
    因此以「添加前是否已存在」判定是否重复（用于统计），实际写入均成功。
    内部自行用 user_scope 包裹，调用方无需预先设置用户上下文。
    """
    with user_scope(uid):
        existing = {str(r["code"]).zfill(6) for r in list_watchlist()}
        added = 0
        dup = 0
        for h in hits:
            code = str(h.get("code") or "").zfill(6)
            if not code:
                continue
            name = str(h.get("name") or code)
            was_present = code in existing
            add_watch(
                code,
                name,
                source=source,
                note=f"定时扫描 {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}",
                entry_price=float(h.get("price") or 0) or None,
                entry_pct=float(h.get("pct") or 0) if h.get("pct") is not None else None,
                entry_score=float(h.get("score") or 0) if h.get("score") is not None else None,
                day_position=(h.get("fenshi") or {}).get("day_position"),
                vwap_deviation=(h.get("fenshi") or {}).get("vwap_deviation"),
            )
            if was_present:
                dup += 1
            else:
                added += 1
                existing.add(code)
    return added, dup


def run_scheduled_scan(
    account: str,
    *,
    mode: str = "fenshi",
    universe_policy: str = "hot_only",
    session: str = "morning",
    max_retries: int = 2,
    retry_backoff_seconds: float = 30.0,
    top_n: int | None = None,
    source: str | None = None,
) -> ScheduledScanResult:
    """执行一轮定时扫描并把全部命中标的加入对应账号自选。

    异常与重试：扫描阶段任何异常按 max_retries 次数退避重试；重试仍失败则本次记
    error 并跳过加自选（不产生脏数据）。加自选阶段异常不重试（避免重复写入），直接记 error。
    """
    t0 = time.perf_counter()
    now = datetime.now(timezone.utc).astimezone()
    today = now.date()
    fired_at = now.isoformat()

    base = ScheduledScanResult(
        account=account,
        fired_at=fired_at,
        trade_date=is_trade_date(today),
        scanned=False,
        hit_count=0,
        added=0,
        skipped_duplicate=0,
        error=None,
        retries=0,
        elapsed_ms=0.0,
        note="",
    )

    if not base.trade_date:
        base.note = "非交易日，跳过"
        base.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info("scheduled scan [%s] skipped: not a trade date", account)
        return base

    uid = ensure_strategy_user(account)
    if source is None:
        source = SOURCE_SOFT if account == STRATEGY_SOFT else SOURCE_FENSHI

    # 扫描（带重试）。延迟导入避免循环依赖。
    from services.scan import run_scan

    result: dict[str, Any] | None = None
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with user_scope(uid):
                result = run_scan(
                    session=session,
                    mode=mode,
                    universe_policy=universe_policy,
                    top_n=top_n,
                )
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            base.retries = attempt
            logger.exception("scheduled scan [%s] attempt %d/%d failed", account, attempt + 1, max_retries + 1)
            if attempt < max_retries:
                time.sleep(retry_backoff_seconds * (attempt + 1))
            else:
                result = None

    if result is None:
        base.error = f"扫描失败（重试 {base.retries} 次后放弃）: {last_err}"
        base.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.error("scheduled scan [%s] FAILED after retries: %s", account, last_err)
        return base

    base.scanned = True
    hits = list(result.get("items") or [])
    base.hit_count = len(hits)

    # 加自选（不重试，避免重复写入；异常仅记录）
    try:
        added, dup = _add_hits_to_watchlist(uid, hits, source)
        base.added = added
        base.skipped_duplicate = dup
        base.note = f"命中 {base.hit_count}，新增自选 {added}，重复 {dup}"
    except Exception as e:  # noqa: BLE001
        base.error = f"加自选失败: {e}"
        logger.exception("scheduled scan [%s] add-watchlist failed", account)

    base.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    logger.info(
        "scheduled scan [%s] done: %s (elapsed %sms)",
        account,
        base.note or base.error,
        base.elapsed_ms,
    )
    return base


def _make_job(account: str, mode: str, universe_policy: str, session: str) -> Callable[[], None]:
    """构造一个可序列化为单一调度 job 的闭包（APScheduler 要求 callable）。"""

    def _job() -> None:
        r = run_scheduled_scan(
            account,
            mode=mode,
            universe_policy=universe_policy,
            session=session,
        )
        stats.record(r)

    _job.__name__ = f"sched_{account}_{session}"  # 便于日志区分
    return _job


_scheduler: BackgroundScheduler | None = None


def _build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(
        timezone=str(settings.scheduler_timezone),
        job_defaults={
            "coalesce": True,        # 错过的触发合并为一次，避免补跑堆积
            "max_instances": 1,      # 同一 job 不允许并发（配合单 worker）
            "misfire_grace_time": 300,  # 错过 5 分钟内仍补跑（如进程短暂卡顿）
        },
    )

    for hm, session in _HM_SESSION.items():
        hour, minute = (int(x) for x in hm.split(":"))
        # 每个策略账号 × 每个时间点 = 一个独立 job
        for account, mode, up in [
            (STRATEGY_SOFT, "fenshi", "soft"),
            (STRATEGY_FENSHI, "fenshi", "hot_only"),
        ]:
            job = _make_job(account, mode, up, session)
            sched.add_job(
                job,
                trigger=CronTrigger(
                    hour=hour,
                    minute=minute,
                    day_of_week="mon-fri",
                    timezone=str(settings.scheduler_timezone),
                ),
                id=f"{account}-{session}",
                name=f"{account}@{hm}",
                replace_existing=True,
            )
            logger.info("scheduled job registered: %s @ %s (%s/%s)", account, hm, mode, up)

    return sched


def start_scheduler() -> bool:
    """启动调度器（进程启动时调用一次，幂等）。返回是否真正启动。"""
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=false)")
        return False
    if _scheduler is not None and _scheduler.running:
        return False
    _scheduler = _build_scheduler()
    _scheduler.start()
    logger.info("scheduler started: 10:40 / 14:20 mon-fri, tz=%s", settings.scheduler_timezone)
    return True


def shutdown_scheduler() -> None:
    """停止调度器（进程退出时调用）。"""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _scheduler = None


def scheduler_status() -> dict[str, Any]:
    """调度器运行状态（供 /api/health 或诊断接口读取）。"""
    if _scheduler is None:
        return {"enabled": settings.scheduler_enabled, "running": False, "jobs": []}
    jobs = []
    for j in _scheduler.get_jobs():
        nrt = getattr(j, "next_run_time", None)
        jobs.append(
            {
                "id": j.id,
                "name": j.name,
                "next_run": nrt.isoformat() if nrt else None,
            }
        )
    return {"enabled": settings.scheduler_enabled, "running": _scheduler.running, "jobs": jobs}
