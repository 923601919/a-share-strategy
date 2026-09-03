"""分数有效性验证（score-effectiveness）测试。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))

# 先隔离 DB 再导入应用模块（与 test_auth_isolation 相同套路）
_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db
os.environ["DEMO_MODE"] = "true"

from services.stats import aggregate_score_effectiveness, score_effectiveness  # noqa: E402


def _track(tid, *, score=None, source="fenshi", created="2026-08-01T10:00:00", exit_ret=None, day_position=None):
    return {
        "id": tid,
        "code": f"60{tid:04d}",
        "name": f"股{tid}",
        "source": source,
        "entry_score": score,
        "created_at": created,
        "exit_return_pct": exit_ret,
        "day_position": day_position,
    }


def _rets(*vals):
    return [
        {"day_offset": i, "trade_date": "2026-08-01", "return_pct": v}
        for i, v in enumerate(vals)
    ]


def test_bucket_assignment_and_win_rate():
    tracks = [
        _track(1, score=85, created="2026-08-01T10:00:00"),
        _track(2, score=75, created="2026-08-01T10:05:00"),
        _track(3, score=60, created="2026-08-01T10:10:00"),
        _track(4, score=None, source="manual", created="2026-08-01T10:15:00"),
    ]
    returns_map = {
        1: _rets(0.0, 2.0, 3.0, 5.0),
        2: _rets(0.0, -1.0, -2.0, -3.0),
        3: _rets(0.0, 1.5, 1.0, 1.0),
        4: _rets(0.0, -0.5, -1.0, -2.0),
    }
    out = aggregate_score_effectiveness(tracks, returns_map, today="2026-08-10")

    assert out["summary"]["total_tracks"] == 4
    assert out["summary"]["tracks_with_score"] == 3
    assert out["summary"]["tracks_without_score"] == 1
    assert out["summary"]["with_t3"] == 4

    buckets = {b["bucket"]: b for b in out["buckets"]}
    assert set(buckets) == {"score>=70", "score50-69", "score<50", "no_score"}

    hi = buckets["score>=70"]
    assert hi["tracks"] == 2
    t3 = next(d for d in hi["days"] if d["day"] == "T+3")
    # 5.0 与 -3.0：胜率 50%，均值 1.0
    assert t3["count"] == 2
    assert t3["win_rate"] == 50.0
    assert t3["avg_return"] == 1.0
    assert t3["avg_win"] == 5.0
    assert t3["avg_loss"] == -3.0

    no = buckets["no_score"]
    assert no["tracks"] == 1
    t3_no = next(d for d in no["days"] if d["day"] == "T+3")
    assert t3_no["win_rate"] == 0.0


def test_days_window_filters_old_tracks():
    tracks = [
        _track(1, score=80, created="2026-07-01T10:00:00"),
        _track(2, score=80, created="2026-08-20T10:00:00"),
    ]
    returns_map = {1: _rets(0, 1, 2, 3), 2: _rets(0, -1, -2, -3)}
    out = aggregate_score_effectiveness(tracks, returns_map, days=30, today="2026-09-01")
    assert out["summary"]["total_tracks"] == 1
    assert out["days_window"] == 30


def test_position_bucket_assignment():
    """日内位置分桶：追高区（>=0.9）与低位区分开，且收益可对比。"""
    tracks = [
        _track(1, score=80, day_position=0.95),   # 追高
        _track(2, score=80, day_position=0.50),   # 中位
        _track(3, score=80, day_position=0.20),   # 低位
        _track(4, score=80, day_position=None),   # 无位置（手工/旧数据）
    ]
    returns_map = {
        1: _rets(0, -2, -3, -4),   # 追高 T+3 亏损
        2: _rets(0, 1, 2, 2),
        3: _rets(0, 1, 1, 1),
        4: _rets(0, 0.5, 0.5, 0.5),
    }
    out = aggregate_score_effectiveness(tracks, returns_map, today="2026-08-10")

    pos = {b["bucket"]: b for b in out["position_buckets"]}
    assert set(pos) == {"<0.3", "0.3-0.6", "0.6-0.9", ">=0.9", "unknown"}
    assert pos[">=0.9"]["tracks"] == 1
    assert pos["0.3-0.6"]["tracks"] == 1
    assert pos["<0.3"]["tracks"] == 1
    assert pos["unknown"]["tracks"] == 1
    assert pos["0.6-0.9"]["tracks"] == 0

    # 追高区 T+3 亏损
    hi_t3 = next(d for d in pos[">=0.9"]["days"] if d["day"] == "T+3")
    assert hi_t3["avg_return"] == -4.0
    assert hi_t3["win_rate"] == 0.0


def test_monthly_and_exit_stats():
    tracks = [
        _track(1, score=80, created="2026-07-05T10:00:00", exit_ret=4.0),
        _track(2, score=65, created="2026-08-05T10:00:00", exit_ret=-2.0),
    ]
    returns_map = {1: _rets(0, 1, 2, 3), 2: _rets(0, -1, -1, -1)}
    out = aggregate_score_effectiveness(tracks, returns_map, today="2026-09-01")

    months = {m["month"]: m for m in out["monthly"]}
    assert months["2026-07"]["tracks"] == 1
    assert months["2026-07"]["t3"]["avg_return"] == 3.0
    assert months["2026-08"]["t3"]["win_rate"] == 0.0

    assert out["exits"]["count"] == 2
    assert out["exits"]["win_rate"] == 50.0
    assert out["exits"]["avg_return"] == 1.0

    # 交叉矩阵：score>=70 × fenshi 有 T+3 样本
    assert any(m["bucket"] == "score>=70" and m["source"] == "fenshi" for m in out["bucket_by_source_t3"])


def test_db_backed_score_effectiveness():
    from config import settings  # noqa: E402
    from db import create_watch_track, init_db, upsert_track_returns  # noqa: E402
    from user_ctx import user_scope  # noqa: E402

    settings.db_path = Path(_db)
    init_db()

    with user_scope(1):
        t1 = create_watch_track(
            code="600001", name="甲", source="fenshi", note="",
            entry_price=10.0, entry_pct=3.0, entry_score=85.0,
            created_at="2026-08-01T10:00:00",
        )
        t2 = create_watch_track(
            code="600002", name="乙", source="manual", note="",
            entry_price=8.0, entry_pct=-1.0, entry_score=None,
            created_at="2026-08-01T10:05:00",
        )
        upsert_track_returns(t1, _rets(0.0, 2.0, 2.5, 3.0))
        upsert_track_returns(t2, _rets(0.0, -1.0, -1.5, -2.0))

        out = score_effectiveness()
        assert out["summary"]["total_tracks"] == 2
        assert out["summary"]["tracks_with_score"] == 1
        buckets = {b["bucket"]: b for b in out["buckets"]}
        assert buckets["score>=70"]["tracks"] == 1
        assert buckets["no_score"]["tracks"] == 1
        hi_t1 = next(d for d in buckets["score>=70"]["days"] if d["day"] == "T+1")
        assert hi_t1["avg_return"] == 2.0

    # 其他用户隔离：user 2 无数据
    with user_scope(2):
        out2 = score_effectiveness()
        assert out2["summary"]["total_tracks"] == 0
