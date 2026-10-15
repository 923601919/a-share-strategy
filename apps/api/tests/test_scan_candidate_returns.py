"""候选收益回填：消融回测的另一半。

scan_candidates 记了「当时看到了什么」，但没记「那票后来涨没涨」——没这一半，
反事实重排只能回答「谁进谁出」，答不了唯一要紧的问题：**换进来的票赚不赚**。

这组用例重点钉三件容易静默出错的事：
1. 基准日必须精确命中（不能退而取「第一个 ≥ base_date 的 bar」，那会整体后移一天）；
2. 当天的 bar 不得当终值用（只认严格早于 today 的交易日）；
3. 回填是跨用户的（作业拿不到用户上下文，误加 user_scope 会静默漏掉其它账号）。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

os.environ["JWT_SECRET"] = ""

from config import settings  # noqa: E402
from db import (  # noqa: E402
    get_db,
    init_db,
    list_pending_candidate_returns,
    purge_orphan_candidate_returns,
    save_scan_candidates,
    upsert_candidate_returns,
)
from services import track  # noqa: E402
from user_ctx import user_scope  # noqa: E402

BASE = "2026-10-08"  # 周四
SESSIONS = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]
CLOSES = [10.0, 11.0, 10.5, 12.0, 9.0]


def _fresh_db(monkeypatch) -> Path:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(settings, "db_path", Path(path))
    init_db()
    return Path(path)


def _bars(sessions=None, closes=None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": list(sessions if sessions is not None else SESSIONS),
            "close": list(closes if closes is not None else CLOSES),
        }
    )


@pytest.fixture
def fake_daily(monkeypatch):
    """替换日线来源，并记录被拉取的 (code, limit) —— 用来验证「同组只拉一次」。"""
    calls: list[tuple[str, int]] = []

    def _fake(code: str, limit: int = 60, *, no_cache: bool = False):
        calls.append((code, limit))
        return _bars()

    monkeypatch.setattr(track, "_daily_bars", _fake)
    return calls


def _add_candidate(*, code="600001", when="2026-10-08T10:40:00+08:00", selected=False, uid=1):
    with user_scope(uid):
        save_scan_candidates(
            [
                {
                    "mode": "fenshi",
                    "universe_policy": "soft",
                    "code": code,
                    "name": code,
                    "score": 50.0,
                    "selected": selected,
                }
            ]
        )
    # 必须按 id 定位刚插入的那一行：同一个 code 会被多次插入（多轮扫描），
    # 按 code 更新会把先前几轮的入池日一并改掉，测的就不是原来那件事了。
    with get_db() as conn:
        cid = int(
            conn.execute(
                "SELECT MAX(id) AS i FROM scan_candidates WHERE code=?", (code,)
            ).fetchone()["i"]
        )
        conn.execute("UPDATE scan_candidates SET created_at=? WHERE id=?", (when, cid))
    return cid


# ---------- compute_candidate_returns 的口径 ----------

def test_offset_zero_is_exactly_zero(fake_daily):
    """offset 0 = 基准自己（同一个值相除）。这不是巧合，是口径自检点。"""
    rows, why = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-15")
    assert why is None
    assert rows[0]["day_offset"] == 0
    assert rows[0]["return_pct"] == 0.0
    assert rows[0]["trade_date"] == BASE
    assert rows[0]["base_close"] == 10.0


def test_returns_are_close_to_close_over_sessions(fake_daily):
    rows, why = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-15")
    assert why is None
    assert [(r["day_offset"], r["trade_date"]) for r in rows] == list(zip(range(4), SESSIONS))
    # 11/10-1 = +10%；10.5/10-1 = +5%；12/10-1 = +20%
    assert [r["return_pct"] for r in rows] == [0.0, 10.0, 5.0, 20.0]


def test_base_date_must_match_exactly(fake_daily, monkeypatch):
    """基准日 bar 缺失时必须跳过，**不能**退而用「第一个 ≥ base_date 的 bar」。

    退让会把基准整体后移一天，算出来的是另一个东西，而且看上去完全正常。
    """
    monkeypatch.setattr(
        track,
        "_daily_bars",
        lambda code, limit=60, *, no_cache=False: _bars(SESSIONS[1:], CLOSES[1:]),
    )
    rows, why = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-15")
    assert rows == []
    assert why == "no_base_bar"


def test_today_bar_is_not_used_as_final(fake_daily):
    """今天的 bar 还没定稿：today=基准日 时，基准日 bar 被排除 → 跳过而不是当作终值。"""
    rows, why = track.compute_candidate_returns(code="600001", base_date=BASE, today=BASE)
    assert rows == []
    assert why == "no_base_bar"


def test_partial_then_settled_as_days_pass(fake_daily):
    """每过一个交易日多算一个 offset —— 结清不是一次性事件。"""
    _, w1 = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-12")
    r1, _ = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-12")
    assert w1 is None
    assert [r["day_offset"] for r in r1] == [0, 1]
    r2, _ = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-15")
    assert [r["day_offset"] for r in r2] == [0, 1, 2, 3]


def test_no_daily_is_reported_separately(monkeypatch):
    """「没拿到日线」与「基准日没有 bar」是两回事，不能合并成一个计数。"""
    monkeypatch.setattr(track, "_daily_bars", lambda code, limit=60, *, no_cache=False: pd.DataFrame())
    rows, why = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-15")
    assert rows == []
    assert why == "no_daily"


def test_bad_base_close_is_rejected(monkeypatch):
    monkeypatch.setattr(
        track, "_daily_bars", lambda code, limit=60, *, no_cache=False: _bars(closes=[0.0, 1, 2, 3, 4])
    )
    rows, why = track.compute_candidate_returns(code="600001", base_date=BASE, today="2026-10-15")
    assert rows == []
    assert why == "bad_base"


# ---------- backfill_candidate_returns 的行为 ----------

def test_backfill_writes_rows_and_groups_same_code_day(fake_daily, monkeypatch):
    """同代码同基准日的多轮候选（09:45/10:40）共用一次日线和一组收益行。"""
    _fresh_db(monkeypatch)
    id1 = _add_candidate(code="600001", when="2026-10-08T09:45:00+08:00", selected=True)
    id2 = _add_candidate(code="600001", when="2026-10-08T10:40:00+08:00")

    s = track.backfill_candidate_returns(today="2026-10-15")

    assert s["pending"] == 2
    assert s["codes"] == 1 and s["groups"] == 1
    assert len(fake_daily) == 1, f"日线被拉了 {len(fake_daily)} 次，应只拉一次"
    assert s["written"] == 2
    assert s["settled"] == 2
    with get_db() as conn:
        for cid in (id1, id2):
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM scan_candidate_returns WHERE candidate_id=?", (cid,)
            ).fetchone()["n"]
            assert n == 4


def test_backfill_is_idempotent_and_heals(monkeypatch, fake_daily):
    """同一天跑两次结果一致；跨日重跑把新 offset 补上（作业漏跑一轮能自愈）。"""
    _fresh_db(monkeypatch)
    cid = _add_candidate(code="600001")

    first = track.backfill_candidate_returns(today="2026-10-13")
    assert first["partial"] == 1 and first["settled"] == 0
    with get_db() as conn:
        n1 = conn.execute(
            "SELECT COUNT(*) AS n FROM scan_candidate_returns WHERE candidate_id=?", (cid,)
        ).fetchone()["n"]

    again = track.backfill_candidate_returns(today="2026-10-13")
    with get_db() as conn:
        n2 = conn.execute(
            "SELECT COUNT(*) AS n FROM scan_candidate_returns WHERE candidate_id=?", (cid,)
        ).fetchone()["n"]
    # 同一天重跑：offset 集合没变（PK 挡住重复行），但仍在待办里（还没结清）
    assert again["pending"] == 1
    assert (n1, n2) == (3, 3)

    done = track.backfill_candidate_returns(today="2026-10-15")
    assert done["settled"] == 1

    # 结清后不再进待办队列（否则每天都会把全历史重拉一遍）
    after = track.backfill_candidate_returns(today="2026-10-15")
    assert after["pending"] == 0
    assert after["written"] == 0


def test_pending_ignores_settled_previously(monkeypatch, fake_daily):
    """"结清"的判据是 offset 行落在**已收盘交易日**上，不是「有 offset 行」。"""
    _fresh_db(monkeypatch)
    cid = _add_candidate(code="600001")
    # 手写一条落在今天的 offset 行：当天 bar 未定稿 → 不算结清
    upsert_candidate_returns(
        cid,
        [
            {
                "day_offset": 3,
                "trade_date": "2026-10-15",
                "base_date": BASE,
                "base_close": 10.0,
                "close_price": 9.0,
                "return_pct": -10.0,
            }
        ],
    )
    pending = list_pending_candidate_returns(today="2026-10-15", lookback_days=30, offset=3)
    assert len(pending) == 1
    # 同一行若落在更早的交易日上，就算结清
    upsert_candidate_returns(
        cid,
        [
            {
                "day_offset": 3,
                "trade_date": "2026-10-14",
                "base_date": BASE,
                "base_close": 10.0,
                "close_price": 9.0,
                "return_pct": -10.0,
            }
        ],
    )
    assert list_pending_candidate_returns(today="2026-10-15", lookback_days=30, offset=3) == []


def test_base_date_parsed_from_created_at(monkeypatch, fake_daily):
    """SQLite 侧的时间戳解析要被钉住：带 +08:00 偏移的 ISO 串必须给出正确本地日。"""
    _fresh_db(monkeypatch)
    _add_candidate(code="600001", when="2026-10-08T09:45:00+08:00")
    pending = list_pending_candidate_returns(today="2026-10-15", lookback_days=30, offset=3)
    assert [p["base_date"] for p in pending] == ["2026-10-08"]


def test_backfill_is_cross_user(fake_daily, monkeypatch):
    """作业没有用户上下文；若误以为要 user_scope，其它账号会被静默漏掉。"""
    _fresh_db(monkeypatch)
    for uid in (3, 4, 7, 8):
        _add_candidate(code=f"60000{uid}", when="2026-10-08T10:40:00+08:00", uid=uid)
    s = track.backfill_candidate_returns(today="2026-10-15")
    assert s["pending"] == 4
    assert s["written"] == 4


def test_skipped_reasons_are_counted_apart(fake_daily, monkeypatch):
    _fresh_db(monkeypatch)
    _add_candidate(code="600001")
    monkeypatch.setattr(
        track, "_daily_bars", lambda code, limit=60, *, no_cache=False: _bars(SESSIONS[1:], CLOSES[1:])
    )
    s = track.backfill_candidate_returns(today="2026-10-15")
    assert s["skipped_no_base_bar"] == 1
    assert s["written"] == 0
    assert s["failed"] == 0


def test_one_bad_code_does_not_abort_the_run(fake_daily, monkeypatch):
    _fresh_db(monkeypatch)
    _add_candidate(code="600001")
    _add_candidate(code="600002")

    def _flaky(code, limit=60, *, no_cache=False):
        if code == "600001":
            raise RuntimeError("boom")
        return _bars()

    monkeypatch.setattr(track, "_daily_bars", _flaky)
    s = track.backfill_candidate_returns(today="2026-10-15")
    assert s["failed"] == 1
    assert s["written"] == 1, "一只票失败不该让整轮回填停下"


def test_truncation_is_reported(fake_daily, monkeypatch):
    _fresh_db(monkeypatch)
    for i in range(3):
        _add_candidate(code=f"60000{i}")
    monkeypatch.setattr(settings, "scan_candidate_return_max_codes", 2)
    s = track.backfill_candidate_returns(today="2026-10-15")
    assert s["truncated"] is True
    assert s["processed_codes"] == 3, "被截断的规模要能看出来（总数）"
    assert len(fake_daily) == 2, "截断后只处理 2 只"


def test_same_code_multiple_base_dates_fetches_once(monkeypatch):
    """同一代码的不同入池日共用同一条日线序列：按代码拉一次，两个基准日各算一遍。

    按 (代码, 基准日) 去拉会把同一条序列重复拉 4~5 次 —— 一个候选要跨 4 个交易日
    才补齐 offset，网络开销会翻几倍。
    """
    _fresh_db(monkeypatch)
    calls: list[str] = []

    def _fake(code, limit=60, *, no_cache=False):
        calls.append(code)
        return _bars()

    monkeypatch.setattr(track, "_daily_bars", _fake)
    a = _add_candidate(code="600001", when="2026-10-08T10:40:00+08:00")
    b = _add_candidate(code="600001", when="2026-10-12T10:40:00+08:00")

    s = track.backfill_candidate_returns(today="2026-10-15")

    assert calls == ["600001"], f"应只拉一次日线，实际 {calls}"
    assert s["codes"] == 1 and s["groups"] == 2
    with get_db() as conn:
        for cid, base in ((a, "2026-10-08"), (b, "2026-10-12")):
            rows = conn.execute(
                "SELECT trade_date, return_pct FROM scan_candidate_returns"
                " WHERE candidate_id=? ORDER BY day_offset",
                (cid,),
            ).fetchall()
            assert [r["trade_date"] for r in rows][0] == base
            assert rows[0]["return_pct"] == 0.0


def test_today_candidates_are_deferred_without_fetching(monkeypatch, fake_daily):
    """今天入池的候选今天必然算不出来（今天的 bar 没定稿）→ 连拉都不该拉。"""
    _fresh_db(monkeypatch)
    _add_candidate(code="600001", when="2026-10-15T10:40:00+08:00")
    s = track.backfill_candidate_returns(today="2026-10-15")
    assert s["deferred_today"] == 1
    assert fake_daily == [], "已知算不出来还去拉日线，是每天白烧一次网络"
    assert s["written"] == 0
    # 但它们仍留在待办里（第二天就能算出 offset 0）
    assert s["pending"] == 1


def test_disabled_switch_short_circuits(fake_daily, monkeypatch):
    _fresh_db(monkeypatch)
    _add_candidate(code="600001")
    monkeypatch.setattr(settings, "scan_candidate_returns", False)
    s = track.backfill_candidate_returns(today="2026-10-15")
    assert s.get("disabled") is True
    assert fake_daily == []


def test_orphan_returns_are_purged(monkeypatch, fake_daily):
    """候选被保留窗口裁掉后，收益行不能变成永远没人读的孤儿。"""
    _fresh_db(monkeypatch)
    cid = _add_candidate(code="600001")
    track.backfill_candidate_returns(today="2026-10-15")
    with get_db() as conn:
        conn.execute("DELETE FROM scan_candidates WHERE id=?", (cid,))
        assert conn.execute("SELECT COUNT(*) AS n FROM scan_candidate_returns").fetchone()["n"] == 4
    assert purge_orphan_candidate_returns() == 4
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM scan_candidate_returns").fetchone()["n"] == 0


def test_backfill_does_not_touch_old_candidates(monkeypatch, fake_daily):
    """回看窗口之外的老候选不再重拉（否则每天的网络开销随时间无界增长）。"""
    _fresh_db(monkeypatch)
    old = (datetime.now(timezone.utc).astimezone() - timedelta(days=60)).isoformat()
    _add_candidate(code="600001", when=old)
    s = track.backfill_candidate_returns(today="2026-10-15")
    assert s["pending"] == 0
