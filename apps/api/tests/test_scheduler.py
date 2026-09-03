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
