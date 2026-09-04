"""服务端定时扫描（scheduler）单元测试。

覆盖：
1. 账号幂等创建（ensure_strategy_user）
2. 非交易日跳过
3. 加自选归属隔离（soft/fenshi 两个账号各自独立）
4. 加自选幂等（重复命中不重复计 new）
5. 扫描异常重试后放弃
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
    STRATEGY_SOFT,
    STRATEGY_FENSHI,
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
