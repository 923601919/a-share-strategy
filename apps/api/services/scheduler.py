"""服务端定时扫描任务：交易日 10:40 / 14:20 自动扫描并加入对应策略账号的自选列表。

设计目标
--------
- 三个独立策略账号（软加权 soft / 进攻型分时 fenshi / 优化软加权 soft_opt），各自拥有完全隔离的自选列表。
- 每个交易日两个时间点（默认 10:40 上午、14:20 下午）各触发一轮扫描。
- 扫描结果的全部标的自动写入对应账号的自选列表（source 标记为策略来源），供后续数据分析。
- soft_opt 在软加权基础上叠加「拉升量能必须 > 开盘量能」硬过滤（参数取 config.py 的 softopt_* 默认值）。

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

# 策略账号名（软加权 / 进攻型分时 / 优化软加权）——三个独立用户，各自隔离的自选列表
STRATEGY_SOFT = "soft"           # 软加权（universe_policy=soft）
STRATEGY_FENSHI = "fenshi"      # 进攻型分时（mode=fenshi + hot_only）
STRATEGY_SOFT_OPT = "soft_opt"  # 优化软加权（universe_policy=soft_opt，拉升量能>开盘量能硬过滤）
STRATEGY_VERIFY = "verify"      # 汇总验证账户：同一账户内跑上述三策略，靠 source 区分来源

# 自选来源标记：与 WatchIn.source 的 Literal 一致，便于前端 by_source 统计。
# 老三账户（soft/fenshi/soft_opt）沿用 "fenshi" —— 它们靠 user_id 隔离自选，
# source 不参与区分，保持既有行为以免改变止损止盈取值链路。
# verify 账户才使用三值区分：同一自选页里要能看出每条来自哪个策略。
SOURCE_FENSHI = "fenshi"        # 进攻型分时（老三账户沿用；verify 下表示进攻型分时命中）
SOURCE_SOFT = "soft"            # 软加权，仅 verify 账户使用
SOURCE_SOFT_OPT = "soft_opt"    # 优化软加权，仅 verify 账户使用

# 全局串行锁：同一时刻只允许一轮定时扫描在跑。
# 原因（2026-09-04 实测）：两个账号的 job 会被 APScheduler 在同一秒并发启动，
# 而 soft(universe_policy="soft") 必须拉全市场快照（spawn 子进程抓 ~5500 只），
# 与并发的另一轮扫描争抢网络/内存，子进程内 akshare 会返回**空 DataFrame 且不抛异常**，
# 最终 soft 拿到 error_code=no_quotes → 命中 0；fenshi 走板块成分实时报价(几十只)不受影响。
# 串行后 soft 恢复稳定命中（实测 30 命中）。
_scheduled_scan_lock = threading.Lock()
_SCHEDULED_LOCK_WAIT = 300.0   # 等另一轮扫描让位的最长秒数（超过则本轮跳过并告警）

# 数据源瞬时失败（no_quotes）重试：全市场快照偶发返回空，退避后重试可自愈
_NO_QUOTES_RETRIES = 2
_NO_QUOTES_BACKOFF = 20.0

# 扫描时点 → session 映射（精确对齐交易时段窗口：morning 09:45-11:30 / afternoon 13:30-15:00）
# 09:45 是 morning 窗口的起点且为闭区间（实测 in_session_bucket("09:45") == "morning"），放行。
_HM_SESSION = {
    "09:45": "morning",
    "10:40": "morning",
    "14:20": "afternoon",
}

# 定时扫描计划表：(账号, mode, universe_policy, source, 时间点列表)
#
# 之所以用显式列表而不是「账号列表 × 时间点列表」的笛卡尔积：不同账号需要的时间点不同。
#   - 老三账户：各自独立自选，10:40 / 14:20 生产在用 → source 一律沿用 fenshi
#   - verify：单一汇总账户跑三策略，仅 9:45 → source 必须三值区分，页面才可分辨
# 顺序即意向上的执行先后（实际由 APScheduler 同秒触发 + 全局串行锁排队决定）。
SCHEDULE_PLAN: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    # ---- 老三账户：保持既有行为完全不变 ----
    (STRATEGY_SOFT, "fenshi", "soft", SOURCE_FENSHI, ("10:40", "14:20")),
    (STRATEGY_FENSHI, "fenshi", "hot_only", SOURCE_FENSHI, ("10:40", "14:20")),
    (STRATEGY_SOFT_OPT, "fenshi", "soft_opt", SOURCE_FENSHI, ("10:40", "14:20")),
    # ---- verify 汇总账户：9:45 三个策略依次跑，靠 source 在同一自选页区分来源 ----
    (STRATEGY_VERIFY, "fenshi", "soft", SOURCE_SOFT, ("09:45",)),
    (STRATEGY_VERIFY, "fenshi", "hot_only", SOURCE_FENSHI, ("09:45",)),
    (STRATEGY_VERIFY, "fenshi", "soft_opt", SOURCE_SOFT_OPT, ("09:45",)),
]


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

    **保留首次命中**：watchlist 主键 (user_id, code)，而 add_watch 是
    ON CONFLICT DO UPDATE —— 若重复写入，后一轮扫描会静默覆盖先前的
    source / entry_price / entry_score。这在单策略账号（如 soft）里无碍，
    但在 verify 汇总账号里会致命：三个策略串行跑，后跑的会把前两个的来源
    全部改成自己的，自选页就无法区分来源了。
    因此这里改为「已存在则跳过」，每条自选的来源 = 首次命中它的那个策略，
    且入门价/分数保持首次快照，后续收益统计口径稳定。

    副作用：同一标的被多策略同时命中时，只记录首次来源，「多策略共振」信号
    不在 watchlist 表内体现（如需共振分析，应另建关联表，不应靠覆盖主键实现）。

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
            if code in existing:
                dup += 1
                continue
            name = str(h.get("name") or code)
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
    softopt: dict[str, Any] | None = None,
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
        # 兜底一律沿用 fenshi：老三账户（soft/fenshi/soft_opt）靠 user_id 隔离自选，
        # 来源不参与区分，必须保持既有行为以免改变止损止盈取值链路。
        # verify 汇总账户的三个策略 job 由 SCHEDULE_PLAN 显式传入各自 source，
        # 不会走到这个分支（见 _make_job 的 source 参数）。
        source = SOURCE_FENSHI

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
                    softopt=softopt,
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

    # 数据源瞬时失败自愈：全市场快照偶发返回空（error_code=no_quotes，akshare 不抛异常），
    # 退避后重跑一次即可恢复；否则整轮白跑、账号自选为空且难以归因。
    extra = 0
    while not hits and result.get("error_code") == "no_quotes" and extra < _NO_QUOTES_RETRIES:
        extra += 1
        logger.warning(
            "scheduled scan [%s] got no_quotes, retry %d/%d after %.0fs (note=%s)",
            account,
            extra,
            _NO_QUOTES_RETRIES,
            _NO_QUOTES_BACKOFF,
            (result.get("session_note") or "")[:160],
        )
        time.sleep(_NO_QUOTES_BACKOFF)
        try:
            with user_scope(uid):
                result = run_scan(
                    session=session,
                    mode=mode,
                    universe_policy=universe_policy,
                    top_n=top_n,
                    softopt=softopt,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("scheduled scan [%s] no_quotes retry %d failed", account, extra)
            break
        hits = list(result.get("items") or [])

    base.hit_count = len(hits)
    if not hits:
        # 0 命中时把 session_note 打进日志，便于事后直接归因（时段拦截 / 无行情 / 策略收紧）
        logger.warning(
            "scheduled scan [%s] 0 hits: error_code=%s note=%s",
            account,
            result.get("error_code"),
            (result.get("session_note") or "")[:300],
        )

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


def _make_job(
    account: str,
    mode: str,
    universe_policy: str,
    session: str,
    source: str | None = None,
) -> Callable[[], None]:
    """构造一个可序列化为单一调度 job 的闭包（APScheduler 要求 callable）。"""

    def _job() -> None:
        # 串行化：避免与同时点触发的另一账号扫描并发（并发会让依赖全市场快照的一方拿到空数据）
        acquired = _scheduled_scan_lock.acquire(timeout=_SCHEDULED_LOCK_WAIT)
        if not acquired:
            logger.error(
                "scheduled scan [%s] SKIPPED: another scan still running after %.0fs",
                account,
                _SCHEDULED_LOCK_WAIT,
            )
            return
        try:
            r = run_scheduled_scan(
                account,
                mode=mode,
                universe_policy=universe_policy,
                session=session,
                source=source,
            )
            stats.record(r)
        finally:
            _scheduled_scan_lock.release()

    # job 名需含 universe_policy：verify 账户在同一 session 下有三个策略 job，
    # 否则日志里无法区分是哪一个在跑。
    _job.__name__ = f"sched_{account}_{universe_policy}_{session}"  # 便于日志区分
    return _job


# 收盘后收益回填串行锁：避免与同进程其它重算/扫描任务并发争抢网络/子进程
_backfill_lock = threading.Lock()


def _make_backfill_job() -> Callable[[], None]:
    """构造 15:30 收盘后收益回填 job（每个交易日触发一次）。"""

    def _job() -> None:
        if not is_trade_date(datetime.now().date()):
            logger.info("returns backfill skipped: not a trade date")
            return
        if not _backfill_lock.acquire(blocking=False):
            logger.warning("returns backfill SKIPPED: previous backfill still running")
            return
        try:
            # 延迟导入避免循环依赖
            from services.track import backfill_all_watchlist_returns

            result = backfill_all_watchlist_returns()
            logger.info("returns backfill done: %s", result)
        except Exception:  # noqa: BLE001
            logger.exception("returns backfill FAILED")
        finally:
            _backfill_lock.release()

    _job.__name__ = "returns_backfill"
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

    # 按计划表注册（而非笛卡尔积）：账号 × 策略 × 该账号自己的时间点
    for account, mode, up, source, times in SCHEDULE_PLAN:
        for hm in times:
            session = _HM_SESSION[hm]
            hour, minute = (int(x) for x in hm.split(":"))
            job = _make_job(account, mode, up, session, source)
            sched.add_job(
                job,
                trigger=CronTrigger(
                    hour=hour,
                    minute=minute,
                    day_of_week="mon-fri",
                    timezone=str(settings.scheduler_timezone),
                ),
                # id 必须带上策略维度：verify 在同一 session 下注册三个 job，
                # 若沿用 "{account}-{session}" 会同 id，被 replace_existing 互相覆盖，
                # 最终只剩最后一个策略生效。
                id=f"{account}-{up}-{hm.replace(':', '')}",
                name=f"{account}@{hm}/{up}",
                replace_existing=True,
            )
            logger.info(
                "scheduled job registered: %s @ %s (%s/%s, source=%s)",
                account, hm, mode, up, source,
            )

    # 收盘后 15:30 自动回填收益：绕过缓存把当日最新日线补齐落库，无需用户手点。
    backfill_job = _make_backfill_job()
    sched.add_job(
        backfill_job,
        trigger=CronTrigger(
            hour=15,
            minute=30,
            day_of_week="mon-fri",
            timezone=str(settings.scheduler_timezone),
        ),
        id="returns-backfill",
        name="returns@15:30",
        replace_existing=True,
    )
    logger.info("scheduled job registered: returns-backfill @ 15:30 mon-fri")

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
