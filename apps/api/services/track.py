from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

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
from services.trade_calendar import trade_days_between

ProgressCb = Callable[[str, float, str], None]
CancelCb = Callable[[], bool]


class WatchRefreshCancelled(Exception):
    """自选刷新被用户取消。"""


def _parse_entry_date(created_at: str) -> str:
    """ISO 时间 -> YYYY-MM-DD（本地日历日）。"""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def _hm_int(now_hm: str | None = None) -> int:
    """'HH:MM' / 'HHMM' -> 当日分钟数；None 取当前时间（与 fenshi._hm 同风格）。"""
    if now_hm is None:
        now = datetime.now()
        return now.hour * 60 + now.minute
    parts = now_hm.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    value = int(now_hm)
    return value // 100 * 60 + value % 100


def is_past_t3(
    returns: list[dict[str, Any]],
    *,
    today: str | None = None,
    now_hm: str | None = None,
) -> bool:
    """是否已到 T+3 归档时机：T+3 交易日之后，或 T+3 当日 15:00 收盘后。

    A 股 15:00 收盘（收盘集合竞价 14:57-15:00），收盘价落定后 T+3
    跟踪即完整，当日即可归档，无需等到下一日。now_hm 可注入便于测试。
    """
    today = (today or _today_iso())[:10]
    t3 = next((r for r in returns if int(r.get("day_offset", -1)) == 3), None)
    if not t3:
        return False
    trade_date = str(t3.get("trade_date") or "")[:10]
    if not trade_date:
        return False
    if today > trade_date:
        return True
    if today == trade_date:
        return _hm_int(now_hm) >= 15 * 60  # 15:00 收盘，收盘价已定
    return False


def _needs_t3_refresh(
    entry_date: str,
    returns: list[dict[str, Any]],
    *,
    today: str | None = None,
    now_hm: str | None = None,
) -> bool:
    """入池已久但尚无 T+3 落库时，尝试拉日线补全。"""
    if is_past_t3(returns, today=today, now_hm=now_hm):
        return False
    if any(int(r.get("day_offset", -1)) == 3 for r in returns):
        return False
    today_d = (today or _today_iso())[:10]
    try:
        entry = datetime.fromisoformat(entry_date).date()
        tday = datetime.fromisoformat(today_d).date()
    except Exception:
        return False
    # 用交易日数替代自然日近似：长假（国庆/春节）下 4 个自然日可能只有 1 个交易日
    return trade_days_between(entry, tday) >= 3


def _t3_close_settled(returns: list[dict[str, Any]]) -> bool:
    """T+3 收盘价是否可信：落库时间晚于其 trade_date 当日 15:00（收盘价已定）。

    盘中拉日线时数据源会给当日实时 K 线，此时落库的"收盘价"并非最终值；
    无法判断（缺 recorded_at / 解析失败）时保守返回 False，交由强刷路径确认。
    """
    t3 = next((r for r in returns if int(r.get("day_offset", -1)) == 3), None)
    if not t3:
        return False
    trade_date = str(t3.get("trade_date") or "")[:10]
    recorded = str(t3.get("recorded_at") or "")
    if not trade_date or not recorded:
        return False
    try:
        rec = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
        close_dt = datetime.fromisoformat(f"{trade_date}T15:00:00").astimezone()
    except Exception:
        return False
    try:
        return rec >= close_dt
    except TypeError:
        return False


def _daily_bars(code: str, limit: int = 40, *, no_cache: bool = False) -> pd.DataFrame:
    df = mkt.fetch_daily(code, limit=limit, no_cache=no_cache)
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
    no_cache: bool = False,
) -> list[dict[str, Any]]:
    """计算 T+0..T+N 相对入池价的涨跌幅（按交易日收盘）。

    no_cache=True 时绕过进程日线缓存，用于收益重算 / 收盘回填，确保读到最新日线。
    入池当日 K 线尚未入库（start_idx 找不到）时返回空列表 —— 表示「暂无法计算」，
    由上层决定占位或重试，**不再伪造 T+0=0**（伪造占位会被误判为已完成并覆盖真实数据）。
    """
    if entry_price <= 0:
        return []

    daily = _daily_bars(code, limit=40, no_cache=no_cache)
    rows: list[dict[str, Any]] = []

    start_idx: int | None = None
    if not daily.empty:
        for i, d in enumerate(daily["date"].tolist()):
            if d >= entry_date:
                start_idx = i
                break

    if start_idx is None:
        # 入池当日 K 线尚未入库 / 日线缺失：无法计算，返回空（不伪造占位）
        return []

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


def _merge_track_returns(
    existing: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """收益并集：新值优先覆盖同偏移，保留旧数据中「新计算未覆盖」的偏移（如 T+2/T+3）。

    防止一次「算不全」（如日线缓存滞后、数据未全）的刷新把之前已落库的正确
    T+1/T+2/T+3 冲掉，导致 track 被错误冻结在残缺状态。
    """
    new_map = {int(r["day_offset"]): r for r in new}
    merged: list[dict[str, Any]] = []
    for r in existing:
        off = int(r["day_offset"])
        if off not in new_map:
            merged.append(r)
    merged.extend(new)
    merged.sort(key=lambda r: int(r["day_offset"]))
    return merged


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
    """
    超过 T+3 的自选：归档并移出。
    默认只读本地已落库收益（进页不打行情）；force_refresh=True 时才补全日线。
    """
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
        returns = get_track_returns(track_id)

        # 仅在用户点「刷新收益」时才打日线；进页轻量加载绝不联网
        if force_refresh:
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

        # 轻量路径不打网络：T+3 行若在其 trade_date 收盘前落库，"收盘价"可能非最终值，
        # 跳过本次归档，留给强刷路径（刷新收益）拉到收盘价确认后再归档
        if not force_refresh and not _t3_close_settled(returns):
            continue

        code = str(item["code"]).zfill(6)
        quote = quotes.get(code)
        row = finalize_and_remove_watch(
            item,
            reason="auto_t3",
            quote=quote,
            force_refresh=force_refresh,
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

    # force 刷新（用户点「刷新收益」/ 收盘后回填）必须用最新日线、绕过进程缓存，
    # 否则收盘后读到的仍是盘前/盘中快照，T+1~T+3 永远算不全。
    try:
        rows = compute_short_term_returns(
            code=code,
            entry_price=entry_price,
            entry_date=entry_date,
            max_days=3,
            no_cache=force,
        )
    except Exception:
        # 网络失败：保留已有真实收益，绝不伪造占位覆盖（伪造占位会被误判为「已完成」）
        rows = []

    if not rows:
        # 算不出来（入池当日 K 线未入库 / 网络异常）：返回已有真实数据，不落库、不覆盖。
        # 返回空时上层（enrich_watch_item）会临时给页面一个展示用占位，但不写库。
        return get_track_returns(track_id)

    # 算出来的是真实数据，但与库里已有收益做并集（新值优先），避免一次「算不全」的
    # 刷新把之前已落库的正确 T+1/T+2/T+3 冲掉。
    existing = get_track_returns(track_id)
    merged = _merge_track_returns(existing, rows)
    if persist and merged:
        upsert_track_returns(track_id, merged)
    return merged


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

    # 当日刚入池、尚无收益行：给页面一个展示用占位 T+0=0（仅内存，不落库，
    # 避免伪造占位把真实 track 永久冻结在 T+0=0）。真实数据由「刷新收益」/收盘回填补齐。
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


def run_watchlist_refresh(
    *,
    with_quotes: bool = True,
    with_risk: bool = True,
    on_progress: ProgressCb | None = None,
    should_cancel: CancelCb | None = None,
) -> dict[str, Any]:
    """
    刷新自选收益/现价/异动（可取消、带进度）。
    每只票只拉一次日线；最后用本地收益做 T+3 归档。
    """

    def prog(stage: str, progress: float, message: str) -> None:
        if should_cancel and should_cancel():
            raise WatchRefreshCancelled()
        if on_progress:
            on_progress(stage, progress, message)

    prog("start", 0.02, "读取自选")
    items = list_watchlist()
    total = len(items)
    if total == 0:
        prog("done", 1.0, "无自选")
        return {"items": [], "stats": watchlist_stats(), "expired": []}

    # 1) 逐只刷新 T+0..T+3（进度主体）
    for i, it in enumerate(items):
        code = str(it.get("code") or "").zfill(6)
        prog("returns", 0.05 + 0.55 * (i / total), f"刷新收益 {i + 1}/{total} · {code}")
        track_id = it.get("track_id")
        if not track_id:
            continue
        track = {
            "id": track_id,
            "code": code,
            "entry_price": it.get("entry_price"),
            "created_at": it.get("created_at"),
        }
        try:
            refresh_track_returns(track, persist=True, force=True)
        except Exception:
            pass

    # 2) 用已落库收益归档超过 T+3（不再重复打日线）
    prog("expire", 0.65, "归档超过 T+3 的自选")
    expired = expire_past_t3_watchlist(fetch_quotes=False, force_refresh=False)
    items = list_watchlist()

    # 3) 现价 + 异动
    quotes: dict[str, dict] = {}
    if with_quotes and items:
        prog("quotes", 0.75, f"拉取现价{'/异动' if with_risk else ''}（{len(items)} 只）")
        try:
            from services.scan import watchlist_quotes

            quotes = {
                q["code"]: q
                for q in watchlist_quotes([i["code"] for i in items], include_risk=with_risk)
            }
        except Exception:
            quotes = {}

    # 4) 组装结果（读库，不再联网）
    prog("assemble", 0.92, "汇总结果")
    out = []
    for it in items:
        if should_cancel and should_cancel():
            raise WatchRefreshCancelled()
        q = quotes.get(str(it["code"]).zfill(6), {}) or quotes.get(it["code"], {})
        try:
            out.append(enrich_watch_item(it, q, force_refresh=False))
        except Exception:
            out.append(
                {
                    **it,
                    "quote": q,
                    "returns": [],
                    "track": {
                        "entry_price": it.get("entry_price"),
                        "entry_pct": it.get("entry_pct"),
                        "entry_score": it.get("entry_score"),
                    },
                }
            )

    prog("done", 1.0, f"完成 · 自选 {len(out)} 只" + (f" · 归档 {len(expired)}" if expired else ""))
    return {"items": out, "stats": watchlist_stats(), "expired": expired}


def backfill_all_watchlist_returns(*, on_progress: ProgressCb | None = None) -> dict[str, Any]:
    """收盘后回填：遍历所有有自选的用户，强制重算每只票的 T+0..T+3 收益（绕过缓存）。

    供定时任务在「每个交易日 15:30」调用，无需用户手点即可把收盘后的最新日线补齐落库，
    根治「即使收盘了仍刷不出来」的问题。单 worker 串行执行，避免与扫描任务争抢网络/子进程。
    """
    from db import list_user_ids_with_active_watchlist
    from user_ctx import user_scope

    total = 0
    refreshed = 0
    failed = 0
    for uid in list_user_ids_with_active_watchlist():
        try:
            with user_scope(uid):
                items = list_watchlist()
        except Exception:
            continue
        for it in items:
            track_id = it.get("track_id")
            if not track_id:
                continue
            track = {
                "id": track_id,
                "code": str(it.get("code") or "").zfill(6),
                "entry_price": it.get("entry_price"),
                "created_at": it.get("created_at"),
            }
            try:
                rows = refresh_track_returns(track, persist=True, force=True)
                total += 1
                if rows:
                    refreshed += 1
            except Exception:
                failed += 1
            if on_progress:
                on_progress("backfill", total, f"回填收益 {total} · 用户 {uid}")
    return {"total": total, "refreshed": refreshed, "failed": failed}
