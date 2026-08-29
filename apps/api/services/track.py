from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from config import settings
from db import (
    create_watch_track,
    get_track_returns,
    list_watchlist,
    list_watch_tracks,
    remove_watch,
    upsert_track_returns,
)
from providers import akshare_client as mkt


def _parse_entry_date(created_at: str) -> str:
    """ISO 时间 -> YYYY-MM-DD（本地日历日）。"""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def is_past_t3(returns: list[dict[str, Any]], *, today: str | None = None) -> bool:
    """当前日历日是否已超过 T+3 交易日（T+3 收盘日之后）。"""
    today = (today or _today_iso())[:10]
    t3 = next((r for r in returns if int(r.get("day_offset", -1)) == 3), None)
    if not t3:
        return False
    trade_date = str(t3.get("trade_date") or "")[:10]
    return bool(trade_date) and today > trade_date


def _needs_t3_refresh(entry_date: str, returns: list[dict[str, Any]], *, today: str | None = None) -> bool:
    """入池已久但尚无 T+3 落库时，尝试拉日线补全。"""
    if is_past_t3(returns, today=today):
        return False
    if any(int(r.get("day_offset", -1)) == 3 for r in returns):
        return False
    try:
        elapsed = (datetime.fromisoformat(today or _today_iso()).date() - datetime.fromisoformat(entry_date).date()).days
    except Exception:
        elapsed = 0
    return elapsed >= 4


def _daily_bars(code: str, limit: int = 40) -> pd.DataFrame:
    df = mkt.fetch_daily(code, limit=limit)
    if df is None or df.empty or "date" not in df.columns:
        return pd.DataFrame()
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return work.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def compute_short_term_returns(
    *,
    code: str,
    entry_price: float,
    entry_date: str,
    max_days: int = 3,
) -> list[dict[str, Any]]:
    """计算 T+0..T+N 相对入池价的涨跌幅（按交易日收盘）。"""
    if entry_price <= 0:
        return []

    daily = _daily_bars(code, limit=40)
    rows: list[dict[str, Any]] = []

    start_idx: int | None = None
    if not daily.empty:
        for i, d in enumerate(daily["date"].tolist()):
            if d >= entry_date:
                start_idx = i
                break

    if start_idx is None:
        # 入池当日 K 线尚未入库：T+0 记为入池价
        rows.append(
            {
                "day_offset": 0,
                "trade_date": entry_date,
                "close_price": round(entry_price, 3),
                "return_pct": 0.0,
            }
        )
        return rows

    for offset in range(max_days + 1):
        idx = start_idx + offset
        if idx >= len(daily):
            break
        row = daily.iloc[idx]
        close = float(row.get("close") or 0)
        trade_date = str(row.get("date") or "")
        if close <= 0 or not trade_date:
            continue
        ret = (close / entry_price - 1.0) * 100
        rows.append(
            {
                "day_offset": offset,
                "trade_date": trade_date,
                "close_price": round(close, 3),
                "return_pct": round(ret, 2),
            }
        )
    return rows


def _build_completion_snapshot(
    item: dict[str, Any],
    returns: list[dict[str, Any]],
    *,
    quote: dict[str, Any] | None = None,
    reason: str = "auto_t3",
) -> dict[str, Any]:
    ret_map = {int(r["day_offset"]): r for r in returns}
    entry_price = float(item.get("entry_price") or 0)
    exit_price = None
    if quote and float(quote.get("price") or 0) > 0:
        exit_price = float(quote["price"])
    elif returns:
        exit_price = float(returns[-1].get("close_price") or 0) or None
    exit_return_pct = None
    if exit_price and entry_price > 0:
        exit_return_pct = round((exit_price / entry_price - 1.0) * 100, 2)
    return {
        "reason": reason,
        "completed_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "code": item.get("code"),
        "name": item.get("name"),
        "source": item.get("source"),
        "note": item.get("note"),
        "entry_price": entry_price,
        "entry_pct": item.get("entry_pct"),
        "entry_score": item.get("entry_score"),
        "entry_date": _parse_entry_date(str(item.get("created_at") or "")),
        "created_at": item.get("created_at"),
        "returns": returns,
        "t0": ret_map.get(0),
        "t1": ret_map.get(1),
        "t2": ret_map.get(2),
        "t3": ret_map.get(3),
        "t3_return_pct": ret_map.get(3, {}).get("return_pct"),
        "exit_price": exit_price,
        "exit_return_pct": exit_return_pct,
        "quote": quote or {},
    }


def finalize_and_remove_watch(
    item: dict[str, Any],
    *,
    reason: str = "auto_t3",
    quote: dict[str, Any] | None = None,
    force_refresh: bool = True,
) -> dict[str, Any] | None:
    """归档 T+0~T+3 与退出快照后，从自选移除。"""
    track_id = int(item.get("track_id") or 0)
    code = str(item.get("code") or "").zfill(6)
    if not track_id or not code:
        return None

    track = {
        "id": track_id,
        "code": code,
        "entry_price": item.get("entry_price"),
        "created_at": item.get("created_at"),
    }
    returns: list[dict[str, Any]] = []
    try:
        returns = refresh_track_returns(track, persist=True, force=force_refresh)
    except Exception:
        returns = get_track_returns(track_id)
    if not returns:
        returns = get_track_returns(track_id)

    snapshot = _build_completion_snapshot(item, returns, quote=quote, reason=reason)
    ok = remove_watch(
        code,
        reason=reason,
        exit_price=snapshot.get("exit_price"),
        exit_return_pct=snapshot.get("exit_return_pct"),
        snapshot=snapshot,
    )
    if not ok:
        return None
    return {
        "code": code,
        "name": item.get("name"),
        "reason": reason,
        "t3_return_pct": snapshot.get("t3_return_pct"),
        "exit_return_pct": snapshot.get("exit_return_pct"),
        "completed_at": snapshot.get("completed_at"),
    }


def expire_past_t3_watchlist(
    *,
    fetch_quotes: bool = False,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """超过 T+3 的自选：补全收益、写入归档快照并移出列表。"""
    items = list_watchlist()
    if not items:
        return []

    quotes: dict[str, dict[str, Any]] = {}
    if fetch_quotes:
        try:
            from services.scan import watchlist_quotes

            for q in watchlist_quotes([i["code"] for i in items], include_risk=True):
                quotes[str(q["code"]).zfill(6)] = q
        except Exception:
            quotes = {}

    today = _today_iso()
    expired: list[dict[str, Any]] = []
    for item in items:
        track_id = int(item.get("track_id") or 0)
        if not track_id:
            continue
        entry_date = _parse_entry_date(str(item.get("created_at") or ""))
        returns = get_track_returns(track_id)

        if _needs_t3_refresh(entry_date, returns, today=today) or force_refresh:
            track = {
                "id": track_id,
                "code": item["code"],
                "entry_price": item.get("entry_price"),
                "created_at": item.get("created_at"),
            }
            try:
                refreshed = refresh_track_returns(track, persist=True, force=True)
                if refreshed:
                    returns = refreshed
            except Exception:
                pass

        if not is_past_t3(returns, today=today):
            continue

        code = str(item["code"]).zfill(6)
        quote = quotes.get(code)
        row = finalize_and_remove_watch(
            item,
            reason="auto_t3",
            quote=quote,
            force_refresh=True,
        )
        if row:
            expired.append(row)
    return expired


def refresh_track_returns(
    track: dict[str, Any],
    *,
    persist: bool = True,
    force: bool = False,
) -> list[dict[str, Any]]:
    track_id = int(track.get("id") or track.get("track_id") or 0)
    if not track_id:
        return []

    if not force:
        cached = get_track_returns(track_id)
        if cached:
            return cached
        # 非强制刷新：无缓存时不打网络，交给页面占位
        return []

    code = str(track["code"]).zfill(6)
    entry_price = float(track.get("entry_price") or 0)
    entry_date = _parse_entry_date(str(track.get("created_at") or ""))

    if entry_price <= 0:
        return []

    if settings.demo_mode:
        demo = []
        for d, pct in enumerate([0.0, 1.2, -0.5, 2.8]):
            demo.append(
                {
                    "day_offset": d,
                    "trade_date": entry_date,
                    "close_price": round(entry_price * (1 + pct / 100), 3),
                    "return_pct": pct,
                }
            )
        if persist:
            upsert_track_returns(track_id, demo)
        return demo

    try:
        rows = compute_short_term_returns(
            code=code,
            entry_price=entry_price,
            entry_date=entry_date,
            max_days=3,
        )
    except Exception:
        # 网络失败时至少给出当日入池占位
        rows = [
            {
                "day_offset": 0,
                "trade_date": entry_date,
                "close_price": round(entry_price, 3),
                "return_pct": 0.0,
            }
        ]

    if persist and rows:
        upsert_track_returns(track_id, rows)
    return rows


def _track_payload(item: dict[str, Any], returns: list[dict[str, Any]]) -> dict[str, Any]:
    latest = returns[-1] if returns else None
    ret_map = {r["day_offset"]: r for r in returns}
    return {
        "entry_price": item.get("entry_price"),
        "entry_pct": item.get("entry_pct"),
        "entry_score": item.get("entry_score"),
        "t0": ret_map.get(0),
        "t1": ret_map.get(1),
        "t2": ret_map.get(2),
        "t3": ret_map.get(3),
        "latest_return_pct": latest.get("return_pct") if latest else None,
        "latest_day_offset": latest.get("day_offset") if latest else None,
    }


def enrich_watch_item(
    item: dict[str, Any],
    quote: dict[str, Any] | None = None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """给自选条目附上短线收益与最新跟踪。默认读库，不阻塞网络。"""
    track_id = item.get("track_id")
    if not track_id:
        return {
            **item,
            "quote": quote or {},
            "returns": [],
            "track": _track_payload(item, []),
        }

    track = {
        "id": track_id,
        "code": item["code"],
        "entry_price": item.get("entry_price"),
        "created_at": item.get("created_at"),
    }
    try:
        returns = refresh_track_returns(track, persist=True, force=force_refresh)
    except Exception:
        returns = get_track_returns(int(track_id))

    # 当日刚入池、尚无收益行：本地占位 T+0=0，保证页面立刻有数
    if not returns and item.get("entry_price"):
        entry_date = _parse_entry_date(str(item.get("created_at") or ""))
        returns = [
            {
                "day_offset": 0,
                "trade_date": entry_date,
                "close_price": float(item["entry_price"]),
                "return_pct": 0.0,
            }
        ]
        try:
            upsert_track_returns(int(track_id), returns)
        except Exception:
            pass

    return {
        **item,
        "quote": quote or {},
        "returns": returns,
        "track": _track_payload(item, returns),
    }


def watchlist_stats(*, min_score: float | None = None) -> dict[str, Any]:
    """汇总历史入池记录的成功率（仅用已落库收益，不触发网络）。"""
    tracks = list_watch_tracks(active_only=False, limit=500)
    if not tracks:
        return {
            "total": 0,
            "with_t3": 0,
            "win_rate_t3": 0.0,
            "avg_return_t3": 0.0,
            "by_source": {},
            "by_score_bucket": {},
        }

    t3_returns: list[float] = []
    wins = 0
    by_source: dict[str, list[float]] = {}
    by_bucket: dict[str, list[float]] = {}

    for tr in tracks:
        if min_score is not None and float(tr.get("entry_score") or 0) < min_score:
            continue
        rets = get_track_returns(int(tr["id"]))
        t3 = next((r for r in rets if r["day_offset"] == 3), None)
        if t3 is None:
            continue
        v = float(t3["return_pct"])
        t3_returns.append(v)
        if v > 0:
            wins += 1

        src = str(tr.get("source") or "manual")
        by_source.setdefault(src, []).append(v)

        score = float(tr.get("entry_score") or 0)
        if score >= 70:
            bucket = "score>=70"
        elif score >= 50:
            bucket = "score50-69"
        else:
            bucket = "score<50"
        by_bucket.setdefault(bucket, []).append(v)

    def _agg(vals: list[float]) -> dict[str, Any]:
        if not vals:
            return {"count": 0, "win_rate": 0.0, "avg_return": 0.0}
        return {
            "count": len(vals),
            "win_rate": round(sum(1 for x in vals if x > 0) / len(vals) * 100, 1),
            "avg_return": round(sum(vals) / len(vals), 2),
        }

    n = len(t3_returns)
    return {
        "total": len(tracks),
        "with_t3": n,
        "win_rate_t3": round(wins / n * 100, 1) if n else 0.0,
        "avg_return_t3": round(sum(t3_returns) / n, 2) if n else 0.0,
        "by_source": {k: _agg(v) for k, v in by_source.items()},
        "by_score_bucket": {k: _agg(v) for k, v in by_bucket.items()},
    }
