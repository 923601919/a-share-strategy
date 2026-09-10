"""服务端定时扫描（scheduler）单元测试。

覆盖：
1. 账号幂等创建（ensure_strategy_user）
2. 非交易日跳过
3. 加自选归属隔离（soft/fenshi/soft_opt 三个账号各自独立）
4. 加自选幂等 + 保留首次命中（重复标的不覆盖来源与入门价）
5. 扫描异常重试后放弃
6. verify 汇总账户：同一账户内三策略靠 source 区分来源
7. 调度计划表 SCHEDULE_PLAN：账号与时间点的对应关系、job id 唯一性
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db
os.environ["DEMO_MODE"] = "true"
os.environ["JWT_SECRET"] = ""

from db import init_db, get_user_by_username, list_watchlist, get_db  # noqa: E402
import services.scheduler as sched  # noqa: E402
from services.scheduler import (  # noqa: E402
    ensure_strategy_user,
    run_scheduled_scan,
    _add_hits_to_watchlist,
    _build_scheduler,
    STRATEGY_SOFT,
    STRATEGY_FENSHI,
    STRATEGY_SOFT_OPT,
)


def setup_function():
    init_db()
    # 清空相关表，保证测试间隔离（共享同一临时 DB 文件）
    with get_db() as conn:
        conn.execute("DELETE FROM watchlist")
        conn.execute("DELETE FROM watch_tracks")
        conn.execute("DELETE FROM watch_track_returns")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM scan_snapshots")
        conn.execute("DELETE FROM scan_quality")


def test_ensure_strategy_user_idempotent():
    a = ensure_strategy_user(STRATEGY_SOFT)
    b = ensure_strategy_user(STRATEGY_SOFT)
    assert a == b
    assert get_user_by_username(STRATEGY_SOFT)["id"] == a


def test_ensure_strategy_users_distinct():
    soft_uid = ensure_strategy_user(STRATEGY_SOFT)
    fenshi_uid = ensure_strategy_user(STRATEGY_FENSHI)
    assert soft_uid != fenshi_uid


def test_ensure_strategy_soft_opt_distinct():
    """优化软加权 soft_opt 是独立账号，与 soft / fenshi 互不相同。"""
    soft_uid = ensure_strategy_user(STRATEGY_SOFT)
    fenshi_uid = ensure_strategy_user(STRATEGY_FENSHI)
    opt_uid = ensure_strategy_user(STRATEGY_SOFT_OPT)
    assert opt_uid not in (soft_uid, fenshi_uid)
    assert opt_uid == ensure_strategy_user(STRATEGY_SOFT_OPT)  # 幂等


def test_run_scheduled_scan_soft_opt_forwards_softopt(monkeypatch):
    """soft_opt 定时扫描：universe_policy 透传 soft_opt，softopt 留空用配置默认。"""
    monkeypatch.setattr(sched, "is_trade_date", lambda d: True)
    captured: dict = {}

    def capture(**kw):
        captured.update(kw)
        return {"count": 0, "items": [], "error_code": None, "session_note": ""}

    monkeypatch.setattr("services.scan.run_scan", capture)
    r = run_scheduled_scan(STRATEGY_SOFT_OPT, universe_policy="soft_opt", session="morning", max_retries=0)
    assert r.scanned is True
    assert captured.get("universe_policy") == "soft_opt"
    # 调度侧不传具体参数 -> 交由 run_scan 用 config.py 的 softopt_* 默认归一化
    assert captured.get("softopt") is None


def test_scheduler_registers_soft_opt_jobs():
    """_build_scheduler 应为每个策略注册对应时间点的 job，且 id 不互相覆盖。

    job id 形如 "{账号}-{universe_policy}-{HHMM}"：verify 在同一 session 下有三个
    策略 job，若 id 重复会被 replace_existing 静默覆盖成只剩一个，故这里显式校验唯一性。
    """
    s = _build_scheduler()
    try:
        ids = [j.id for j in s.get_jobs()]
    finally:
        try:
            s.shutdown(wait=False)
        except Exception:
            pass
    # 老三账户 3×2 时间点 + verify 3×1 时间点 + 15:30 收益回填 = 10
    assert len(ids) == 10, f"job 数应为 10，实际 {len(ids)}: {sorted(ids)}"
    assert len(set(ids)) == len(ids), f"job id 存在重复（发生覆盖）: {sorted(ids)}"
    assert "soft_opt-soft_opt-1040" in ids
    assert "soft_opt-soft_opt-1420" in ids
    assert "verify-soft-0945" in ids
    assert "verify-hot_only-0945" in ids
    assert "verify-soft_opt-0945" in ids


def test_skip_on_non_trade_date(monkeypatch):
    # 强制非交易日：monkeypatch 交易日判断为 False
    monkeypatch.setattr(sched, "is_trade_date", lambda d: False)
    r = run_scheduled_scan(STRATEGY_SOFT, session="morning")
    assert r.trade_date is False
    assert r.scanned is False
    assert r.note == "非交易日，跳过"


def test_add_hits_isolated_per_account():
    soft_uid = ensure_strategy_user(STRATEGY_SOFT)
    fenshi_uid = ensure_strategy_user(STRATEGY_FENSHI)
    hits = [{"code": "600001", "name": "测试A", "price": 10.0, "pct": 3.0, "score": 80.0}]

    from user_ctx import user_scope

    with user_scope(soft_uid):
        added, dup = _add_hits_to_watchlist(soft_uid, hits, source="fenshi")
    assert added == 1 and dup == 0

    # soft 账号自选里有 600001
    with user_scope(soft_uid):
        codes = [r["code"] for r in list_watchlist()]
    assert "600001" in codes

    # fenshi 账号自选为空（隔离）
    with user_scope(fenshi_uid):
        assert list_watchlist() == []


def test_add_hits_idempotent_duplicate():
    uid = ensure_strategy_user(STRATEGY_SOFT)
    hits = [{"code": "600001", "name": "测试A", "price": 10.0, "pct": 3.0, "score": 80.0}]

    from user_ctx import user_scope

    with user_scope(uid):
        added1, _ = _add_hits_to_watchlist(uid, hits, source="fenshi")
        added2, dup2 = _add_hits_to_watchlist(uid, hits, source="fenshi")
    assert added1 == 1
    assert added2 == 0
    assert dup2 == 1


def test_run_scheduled_scan_scan_failure_retries_then_errors(monkeypatch):
    """扫描抛异常 -> 按 max_retries 重试，最终记 error 且不产生自选。"""
    monkeypatch.setattr(sched, "is_trade_date", lambda d: True)
    calls = {"n": 0}

    def boom(**kwargs):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr("services.scan.run_scan", boom)
    r = run_scheduled_scan(STRATEGY_SOFT, session="morning", max_retries=2, retry_backoff_seconds=0)
    assert r.scanned is False
    assert r.error is not None
    assert calls["n"] == 3  # 1 次初始 + 2 次重试
    assert r.retries == 2


def test_scheduled_jobs_are_serialized(monkeypatch):
    """两个账号的 job 同秒触发时必须串行执行。

    背景：并发时 soft(universe_policy=soft) 需要全市场快照（spawn 子进程抓 ~5500 只），
    与另一轮扫描争抢网络/内存会让 akshare 返回空 DataFrame（不抛异常）→ no_quotes → 命中 0。
    """
    events: list[tuple[str, str, float]] = []

    def fake_run(account, **kw):
        events.append(("start", account, time.time()))
        time.sleep(0.2)
        events.append(("end", account, time.time()))
        return sched.ScheduledScanResult(
            account=account, fired_at="", trade_date=True, scanned=True,
            hit_count=1, added=1, skipped_duplicate=0, error=None,
            retries=0, elapsed_ms=200.0, note="ok",
        )

    monkeypatch.setattr(sched, "run_scheduled_scan", fake_run)

    j1 = sched._make_job(STRATEGY_SOFT, "fenshi", "soft", "afternoon")
    j2 = sched._make_job(STRATEGY_FENSHI, "fenshi", "hot_only", "afternoon")

    t1 = threading.Thread(target=j1)
    t2 = threading.Thread(target=j2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # 还原区间并检查无重叠
    intervals: list[tuple[float, float, str]] = []
    starts: dict[str, float] = {}
    for kind, acct, ts in events:
        if kind == "start":
            starts[acct] = ts
        else:
            intervals.append((starts.pop(acct), ts, acct))
    intervals.sort()
    assert len(intervals) == 2, f"两轮都应执行完: {events}"
    for i in range(len(intervals) - 1):
        assert intervals[i][1] <= intervals[i + 1][0], f"扫描区间重叠，未串行: {intervals}"


def test_no_quotes_retries_then_succeeds(monkeypatch):
    """全市场快照瞬时返回空(error_code=no_quotes)时退避重试，恢复后正常入自选。"""
    monkeypatch.setattr(sched, "is_trade_date", lambda d: True)
    monkeypatch.setattr(sched, "_NO_QUOTES_BACKOFF", 0.0)

    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "count": 0,
                "items": [],
                "error_code": "no_quotes",
                "session_note": "无真实行情数据，返回空结果（未使用演示数据）",
            }
        return {
            "count": 1,
            "items": [{"code": "600009", "name": "C", "price": 1.0, "pct": 1.0, "score": 70.0}],
        }

    monkeypatch.setattr("services.scan.run_scan", flaky)

    r = run_scheduled_scan(STRATEGY_SOFT, session="afternoon", max_retries=0)
    assert calls["n"] == 2, "首次 no_quotes 应触发一次重试"
    assert r.hit_count == 1
    assert r.added == 1

    from user_ctx import user_scope

    uid = ensure_strategy_user(STRATEGY_SOFT)
    with user_scope(uid):
        codes = {x["code"] for x in list_watchlist()}
    assert "600009" in codes


def test_run_scheduled_scan_success_adds_watchlist(monkeypatch):
    """扫描成功 -> 命中全部加入对应账号自选。"""
    monkeypatch.setattr(sched, "is_trade_date", lambda d: True)

    fake_payload = {
        "count": 2,
        "items": [
            {"code": "600001", "name": "A", "price": 10.0, "pct": 3.0, "score": 88.0, "fenshi": {"day_position": 0.7}},
            {"code": "600002", "name": "B", "price": 20.0, "pct": 4.0, "score": 76.0, "fenshi": {}},
        ],
    }
    monkeypatch.setattr("services.scan.run_scan", lambda **kw: fake_payload)

    r = run_scheduled_scan(STRATEGY_FENSHI, session="afternoon", max_retries=0)
    assert r.scanned is True
    assert r.hit_count == 2
    assert r.added == 2

    from user_ctx import user_scope

    uid = ensure_strategy_user(STRATEGY_FENSHI)
    with user_scope(uid):
        codes = {x["code"] for x in list_watchlist()}
    assert codes == {"600001", "600002"}


# --------------------------------------------------------------------------
# verify 汇总账户：同一账户内跑三策略，靠 source 区分来源（2026-09-10 新增）
# --------------------------------------------------------------------------


def test_schedule_plan_timepoints_isolated():
    """计划表：老三账户仅 10:40/14:20；verify 仅 09:45（互不影响）。"""
    from services.scheduler import SCHEDULE_PLAN, STRATEGY_VERIFY

    times_by_account: dict[str, set[str]] = {}
    for account, _mode, _up, _source, times in SCHEDULE_PLAN:
        times_by_account.setdefault(account, set()).update(times)

    assert times_by_account[STRATEGY_SOFT] == {"10:40", "14:20"}
    assert times_by_account[STRATEGY_FENSHI] == {"10:40", "14:20"}
    assert times_by_account[STRATEGY_SOFT_OPT] == {"10:40", "14:20"}
    assert times_by_account[STRATEGY_VERIFY] == {"09:45"}, "verify 只应跑 9:45"


def test_schedule_plan_verify_sources_distinct_and_legacy_unchanged():
    """verify 三策略 source 互不相同；老三账户 source 必须仍为 fenshi 不受影响。"""
    from services.scheduler import SCHEDULE_PLAN, STRATEGY_VERIFY

    verify_sources = sorted(src for acct, _m, _u, src, _t in SCHEDULE_PLAN if acct == STRATEGY_VERIFY)
    assert verify_sources == ["fenshi", "soft", "soft_opt"], f"verify 来源需三值区分，实际 {verify_sources}"

    # 老三账户靠 user_id 隔离自选，source 改变会影响止损止盈取值链路，必须保持不变
    for acct in (STRATEGY_SOFT, STRATEGY_FENSHI, STRATEGY_SOFT_OPT):
        srcs = {src for a, _m, _u, src, _t in SCHEDULE_PLAN if a == acct}
        assert srcs == {"fenshi"}, f"{acct} 的 source 应保持 fenshi，实际 {srcs}"


def test_verify_three_strategies_write_distinct_sources():
    """同一 verify 自选列表里，三策略各自的 source 应同时并存且可分辨。"""
    from services.scheduler import STRATEGY_VERIFY

    uid = ensure_strategy_user(STRATEGY_VERIFY)
    from user_ctx import user_scope

    _add_hits_to_watchlist(uid, [{"code": "600001", "name": "A", "price": 10.0}], sched.SOURCE_SOFT)
    _add_hits_to_watchlist(uid, [{"code": "600002", "name": "B", "price": 20.0}], sched.SOURCE_FENSHI)
    _add_hits_to_watchlist(uid, [{"code": "600003", "name": "C", "price": 30.0}], sched.SOURCE_SOFT_OPT)

    with user_scope(uid):
        rows = {str(r["code"]).zfill(6): r["source"] for r in list_watchlist()}

    assert len(rows) == 3, f"应有 3 条自选，实际 {rows}"
    assert rows["600001"] == "soft"
    assert rows["600002"] == "fenshi"
    assert rows["600003"] == "soft_opt"


def test_add_hits_keeps_first_source_and_price_no_overwrite():
    """同一标的被多策略依次命中时，保留首次的 source 与入门价，不静默覆盖。

    这是 verify 能否区分来源的前提：add_watch 走 ON CONFLICT DO UPDATE，
    若不跳过已存在标的，后跑策略会把先前来源连同入门价一起改写。
    """
    from services.scheduler import STRATEGY_VERIFY

    uid = ensure_strategy_user(STRATEGY_VERIFY)
    from user_ctx import user_scope

    hits_first = [{"code": "600001", "name": "A", "price": 10.0, "pct": 3.0, "score": 80.0}]
    hits_later = [{"code": "600001", "name": "A", "price": 99.0, "pct": 9.0, "score": 20.0}]

    added1, dup1 = _add_hits_to_watchlist(uid, hits_first, sched.SOURCE_SOFT)
    added2, dup2 = _add_hits_to_watchlist(uid, hits_later, sched.SOURCE_FENSHI)
    assert added1 == 1 and dup1 == 0
    assert added2 == 0 and dup2 == 1

    with user_scope(uid):
        row = [r for r in list_watchlist() if str(r["code"]).zfill(6) == "600001"][0]

    assert row["source"] == "soft", f"来源被后续策略覆盖了: {row['source']}"
    assert row["entry_price"] == 10.0, f"入门价被后续策略覆盖了: {row['entry_price']}"
