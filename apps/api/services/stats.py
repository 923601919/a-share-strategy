"""分数有效性验证：入池 entry_score 分桶 × T+N 收益/胜率。

只读本地库（watch_tracks + watch_track_returns），不触发任何行情网络请求。
核心问题：打分高的票，T+1/T+3 真的比打分低的票更容易赚钱吗？
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from db import get_track_returns_map, list_watch_tracks

# 与 services.track.watchlist_stats 保持一致的桶命名
BUCKET_KEYS = ("score>=70", "score50-69", "score<50", "no_score")
BUCKET_LABELS = {
    "score>=70": "70 分以上",
    "score50-69": "50-69 分",
    "score<50": "50 分以下（含代理分）",
    "no_score": "无分数（手工入池）",
}
SOURCE_LABELS = {
    "fenshi": "进攻型分时",
    "longtou": "龙头低吸",
    "manual": "手工",
}
# 日内位置分桶（追高参数校准的核心维度：买在高位的票 T+N 是否更差）
POSITION_KEYS = ("<0.3", "0.3-0.6", "0.6-0.9", ">=0.9", "unknown")
POSITION_LABELS = {
    "<0.3": "日内低位 (<30%)",
    "0.3-0.6": "日内中低位 (30-60%)",
    "0.6-0.9": "日内中高位 (60-90%)",
    ">=0.9": "日内高位 (≥90%，追高区)",
    "unknown": "无位置记录",
}
MAX_DAY_OFFSET = 3
DEFAULT_MIN_SAMPLES = 5


def _position_bucket_of(pos: Any) -> str:
    if pos is None:
        return "unknown"
    p = float(pos)
    if p < 0.3:
        return "<0.3"
    if p < 0.6:
        return "0.3-0.6"
    if p < 0.9:
        return "0.6-0.9"
    return ">=0.9"


def _bucket_of(score: Any) -> str:
    if score is None:
        return "no_score"
    s = float(score)
    if s >= 70:
        return "score>=70"
    if s >= 50:
        return "score50-69"
    return "score<50"


def _ret_at(returns: list[dict[str, Any]], day: int) -> float | None:
    for r in returns:
        if int(r.get("day_offset", -1)) == day:
            v = r.get("return_pct")
            if v is None:
                return None
            return float(v)
    return None


def _stats(vals: list[float]) -> dict[str, Any]:
    """单组收益统计。空组返回 count=0，其余字段 None。"""
    if not vals:
        return {
            "count": 0,
            "win_rate": None,
            "avg_return": None,
            "median_return": None,
            "avg_win": None,
            "avg_loss": None,
            "best": None,
            "worst": None,
        }
    wins = [x for x in vals if x > 0]
    losses = [x for x in vals if x < 0]
    n = len(vals)
    return {
        "count": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_return": round(sum(vals) / n, 2),
        "median_return": round(statistics.median(vals), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "best": round(max(vals), 2),
        "worst": round(min(vals), 2),
    }


def _day_rows(tracks: list[dict[str, Any]], returns_map: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """按 day_offset 0..MAX_DAY_OFFSET 输出每组统计行。"""
    rows: list[dict[str, Any]] = []
    for day in range(MAX_DAY_OFFSET + 1):
        vals = []
        for tr in tracks:
            rets = returns_map.get(int(tr["id"])) or []
            v = _ret_at(rets, day)
            if v is not None:
                vals.append(v)
        rows.append({"day": f"T+{day}", **_stats(vals)})
    return rows


def aggregate_score_effectiveness(
    tracks: list[dict[str, Any]],
    returns_map: dict[int, list[dict[str, Any]]],
    *,
    days: int | None = None,
    today: str | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, Any]:
    """
    纯聚合（不碰 DB/网络），便于测试。

    tracks: watch_tracks 行（需含 id/entry_score/source/created_at）
    returns_map: track_id -> [{day_offset, return_pct, ...}]
    days: 只统计最近 N 天入池的记录（None = 全部）
    """
    today_iso = (today or datetime.now().date().isoformat())[:10]

    if days is not None:
        cutoff = (
            datetime.fromisoformat(today_iso).date() - timedelta(days=days)
        ).isoformat()
        tracks = [tr for tr in tracks if str(tr.get("created_at") or "")[:10] >= cutoff]

    tracks = [tr for tr in tracks if tr.get("id")]
    with_score = [tr for tr in tracks if tr.get("entry_score") is not None]
    with_t3 = [
        tr
        for tr in tracks
        if _ret_at(returns_map.get(int(tr["id"])) or [], 3) is not None
    ]

    # ---- 分数分桶 ----
    buckets: list[dict[str, Any]] = []
    for key in BUCKET_KEYS:
        group = [tr for tr in tracks if _bucket_of(tr.get("entry_score")) == key]
        buckets.append(
            {
                "bucket": key,
                "label": BUCKET_LABELS[key],
                "tracks": len(group),
                "sufficient": len(group) >= min_samples,
                "days": _day_rows(group, returns_map),
            }
        )

    # ---- 按来源 ----
    by_source: list[dict[str, Any]] = []
    sources = sorted({str(tr.get("source") or "manual") for tr in tracks})
    for src in sources:
        group = [tr for tr in tracks if str(tr.get("source") or "manual") == src]
        by_source.append(
            {
                "source": src,
                "label": SOURCE_LABELS.get(src, src),
                "tracks": len(group),
                "sufficient": len(group) >= min_samples,
                "days": _day_rows(group, returns_map),
            }
        )

    # ---- 分数 × 来源（T+3 交叉验证）----
    matrix: list[dict[str, Any]] = []
    for key in BUCKET_KEYS:
        for src in sources:
            group = [
                tr
                for tr in tracks
                if _bucket_of(tr.get("entry_score")) == key
                and str(tr.get("source") or "manual") == src
            ]
            vals = [
                v
                for tr in group
                if (v := _ret_at(returns_map.get(int(tr["id"])) or [], 3)) is not None
            ]
            s = _stats(vals)
            if s["count"]:
                matrix.append(
                    {"bucket": key, "source": src, "label": SOURCE_LABELS.get(src, src), **s}
                )

    # ---- 按入池月份（策略是否随时间衰减/失效）----
    monthly: list[dict[str, Any]] = []
    months = sorted({str(tr.get("created_at") or "")[:7] for tr in tracks if str(tr.get("created_at") or "")[:7]})
    for month in months:
        group = [
            tr for tr in tracks if str(tr.get("created_at") or "")[:7] == month
        ]
        t1_vals = [
            v
            for tr in group
            if (v := _ret_at(returns_map.get(int(tr["id"])) or [], 1)) is not None
        ]
        t3_vals = [
            v
            for tr in group
            if (v := _ret_at(returns_map.get(int(tr["id"])) or [], 3)) is not None
        ]
        monthly.append(
            {
                "month": month,
                "tracks": len(group),
                "t1": _stats(t1_vals),
                "t3": _stats(t3_vals),
            }
        )

    # ---- 按日内位置分桶（追高参数校准）----
    position_buckets: list[dict[str, Any]] = []
    for key in POSITION_KEYS:
        group = [tr for tr in tracks if _position_bucket_of(tr.get("day_position")) == key]
        position_buckets.append(
            {
                "bucket": key,
                "label": POSITION_LABELS[key],
                "tracks": len(group),
                "sufficient": len(group) >= min_samples,
                "days": _day_rows(group, returns_map),
            }
        )

    # ---- 实际退出收益（exit_return_pct，含人工/异动离场）----
    exit_vals = [
        float(tr["exit_return_pct"])
        for tr in tracks
        if tr.get("exit_return_pct") is not None
    ]

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "days_window": days,
        "min_samples": min_samples,
        "summary": {
            "total_tracks": len(tracks),
            "tracks_with_score": len(with_score),
            "tracks_without_score": len(tracks) - len(with_score),
            "with_t3": len(with_t3),
        },
        "buckets": buckets,
        "by_source": by_source,
        "bucket_by_source_t3": matrix,
        "monthly": monthly,
        "position_buckets": position_buckets,
        "exits": _stats(exit_vals),
    }


def score_effectiveness(*, days: int | None = None) -> dict[str, Any]:
    """读本地库并聚合（当前用户）。"""
    tracks = list_watch_tracks(active_only=False, limit=5000)
    returns_map = get_track_returns_map([int(tr["id"]) for tr in tracks if tr.get("id")])
    return aggregate_score_effectiveness(tracks, returns_map, days=days)
