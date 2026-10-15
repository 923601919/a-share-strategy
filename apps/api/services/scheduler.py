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
from datetime import datetime, timedelta, timezone
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
            from services.track import backfill_all_watchlist_returns, backfill_candidate_returns

            result = backfill_all_watchlist_returns()
            logger.info("returns backfill done: %s", result)

            # 候选收益与自选收益是两件事，但共用同一次收盘后窗口（都要拉日线）。
            # 刻意分成两个 try：候选表坏了不该让自选收益回填跟着失败（反之亦然）。
            try:
                cand = backfill_candidate_returns()
                logger.info("candidate returns backfill done: %s", cand)
            except Exception:  # noqa: BLE001
                logger.exception("candidate returns backfill FAILED")
        except Exception:  # noqa: BLE001
            logger.exception("returns backfill FAILED")
        finally:
            _backfill_lock.release()

    _job.__name__ = "returns_backfill"
    return _job


# 大盘预判作业串行锁：独立于扫描锁。
# 它与扫描共享的只是网络（akshare）与 SQLite，不需要排在扫描后面——
# 反而要独立：若共用一把锁，15:45 撞上某轮扫描变慢，预判会被静默跳过，
# 而当天的记录就永久缺了一条（漏跑看得出来，但代价是样本少一天）。
_forecast_lock = threading.Lock()

# 基准指数同步锁：同样独立。
# 与预判作业互不阻塞——两者取的是不同的指数端点，且各自带一段回看窗口，
# 被跳过一轮不会留下永久空洞（下一轮自然补上）。
_bench_sync_lock = threading.Lock()


def _benchmark_symbols(raw: str | None = None) -> list[str]:
    """解析基准指数列表（逗号分隔，容忍空格与空项）。

    `raw` 可显式传入以便单测——否则「配置写错 → 列表为空 → 作业静默什么都不做」
    这条路径没法被钉住。
    """
    if raw is None:
        raw = str(getattr(settings, "benchmark_sync_symbols", "") or "")
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def _make_bench_sync_job() -> Callable[[], None]:
    """构造基准指数日线同步 job（收盘后，每个交易日一次）。

    为什么单独一个 job，而不是并进 `market_forecast`：
      · 预判只同步 `market_forecast_symbol` 一个标的。把基准塞进去，
        会让「关掉预判」连带把基准一起关掉——而基准是**分析**的依赖，不是预判的。
      · 重复同步 `000001.SH` 是**有意**的（预判作业也同步它）：两边都是 upsert，
        重复的代价只是一次多余请求，换来的是「基准始终新鲜」不依赖预判是否开启。
    """
    def _job() -> None:
        if not getattr(settings, "benchmark_sync_enabled", True):
            logger.info("benchmark sync skipped: disabled by config")
            return
        if not is_trade_date(datetime.now().date()):
            logger.info("benchmark sync skipped: not a trade date")
            return
        if not _bench_sync_lock.acquire(blocking=False):
            logger.warning("benchmark sync SKIPPED: previous run still in progress")
            return
        try:
            # 延迟导入：forecast.service 会拉起 db，放在模块顶层会加剧循环依赖
            from forecast.service import sync_index_daily

            today = datetime.now().date()
            lookback = int(getattr(settings, "benchmark_sync_days", 90))
            for sym in _benchmark_symbols():
                try:
                    res = sync_index_daily(sym, today - timedelta(days=lookback), today)
                    logger.info(
                        "benchmark sync %s: written=%s last=%s",
                        sym, res.get("written"), res.get("last_date"),
                    )
                except Exception as e:  # noqa: BLE001
                    # 单个基准失败不影响其余：一个取不到不该拖垮全部
                    logger.warning("benchmark sync FAILED %s: %s", sym, e)
        except Exception:  # noqa: BLE001
            logger.exception("benchmark sync FAILED")
        finally:
            _bench_sync_lock.release()

    _job.__name__ = "benchmark_sync"
    return _job


def _make_forecast_job() -> Callable[[], None]:
    """构造 15:45 大盘预判作业（结算 + 为下一个交易日起卦）。

    时点选择依据（实测，2026-09-17）：15:00 收盘后新浪**当日**指数日线仍未发布，
    所以作业本身不依赖它——当日用实时行情先结算（`source='sina_rt'`），
    次日官方日线到位后再 upsert 修正。15:45 这个时点给的是"盘后数据基本稳定"，
    而不是"当日日线一定拿得到"——后者在本机不成立。
    """

    def _job() -> None:
        if not getattr(settings, "market_forecast_enabled", True):
            logger.info("market forecast skipped: disabled by config")
            return
        if not is_trade_date(datetime.now().date()):
            logger.info("market forecast skipped: not a trade date")
            return
        if not _forecast_lock.acquire(blocking=False):
            logger.warning("market forecast SKIPPED: previous forecast run still in progress")
            return
        try:
            from forecast.service import run_daily_forecast

            result = run_daily_forecast()
            logger.info(
                "market forecast done: status=%s target=%s created=%s",
                result.get("status"),
                result.get("target_date"),
                result.get("created"),
            )
            if result.get("status") == "failed":
                # 0 产出 = 失败（对应 tianji 的退出码 3）。这条必须响亮：
                # 静默少一条记录比报错危险得多。
                logger.error("market forecast FAILED (0 output): %s", result.get("notes"))
        except Exception:  # noqa: BLE001
            logger.exception("market forecast FAILED")
        finally:
            _forecast_lock.release()

    _job.__name__ = "market_forecast"
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

    # 15:45 大盘预判：结算到期预测 + 为下一个交易日起卦。
    # 刻意排在 15:30 收益回填之后：两者都打行情网，错开可避免在同一分钟内
    # 争抢 akshare 的连接（预判失败会直接少一天样本，不能靠重试弥补）。
    if getattr(settings, "market_forecast_enabled", True):
        sched.add_job(
            _make_forecast_job(),
            trigger=CronTrigger(
                hour=15,
                minute=45,
                day_of_week="mon-fri",
                timezone=str(settings.scheduler_timezone),
            ),
            id="market-forecast",
            name="forecast@15:45",
            replace_existing=True,
        )
        logger.info("scheduled job registered: market-forecast @ 15:45 mon-fri")
    else:
        logger.info("market-forecast job NOT registered (MARKET_FORECAST_ENABLED=false)")

    # 15:50 基准指数同步：让分析口径的 000001/000300/000905/000852 保持新鲜。
    # 排在预判之后：预判是「少一天样本就补不回来」的作业，优先级更高；
    # 而基准自带回看窗口，晚一轮/漏一轮都能自动补上。
    if getattr(settings, "benchmark_sync_enabled", True):
        sched.add_job(
            _make_bench_sync_job(),
            trigger=CronTrigger(
                hour=15,
                minute=50,
                day_of_week="mon-fri",
                timezone=str(settings.scheduler_timezone),
            ),
            id="benchmark-sync",
            name="bench@15:50",
            replace_existing=True,
        )
        logger.info(
            "scheduled job registered: benchmark-sync @ 15:50 mon-fri (%s)",
            ",".join(_benchmark_symbols()) or "(空)",
        )
    else:
        logger.info("benchmark-sync job NOT registered (BENCHMARK_SYNC_ENABLED=false)")

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
