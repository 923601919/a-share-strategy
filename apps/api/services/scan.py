from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import datetime
from typing import Any, Callable, Literal

from config import settings
from db import save_scan_snapshot, save_scan_quality
from providers import market as mkt
from rules.fenshi import (
    apply_chase_penalty,
    apply_day_vol_and_false_push,
    apply_zhaban,
    day_volume_health,
    detect_false_push,
    detect_zhaban,
    in_attack_window,
    score_leader_dip,
    score_offensive_fenshi,
    session_allowed,
)
from rules.risk import anomaly_30d_pct, is_excluded_board, risk_flags
from rules.selection import (
    apply_selection_adjustments,
    best_sector_life,
    board_rank_top_codes,
    finalize_trapped_and_dip,
    sector_lifecycle_map,
)
from rules.sentiment import classify_sentiment


SessionFilter = Literal["auto", "morning", "afternoon", "any"]
ScanMode = Literal["fenshi", "leader_dip"]
UniversePolicy = Literal["hot_only", "quota", "soft"]
ProgressCb = Callable[[str, float, str], None]
CancelCb = Callable[[], bool]

logger = logging.getLogger("scan")


class ScanCancelled(Exception):
    """扫描被用户取消。"""


def _is_st(name: str) -> bool:
    n = name.upper()
    return "ST" in n or "退" in name


def _filter_spot(
    df,
    min_amount_yi: float,
    min_pct: float,
    max_pct: float | None,
    limit: int,
    *,
    universe_codes: set[str] | None = None,
    max_pct_inclusive: bool = False,
):
    amount_floor = min_amount_yi * 1e8
    out = df.copy()
    out["code"] = out["code"].astype(str).str.zfill(6)
    out = out[~out["name"].astype(str).map(_is_st)]
    out = out[
        ~out["code"].map(
            lambda c: is_excluded_board(
                c, exclude_star=settings.exclude_star_market, exclude_bse=settings.exclude_bse
            )
        )
    ]
    if universe_codes:
        out = out[out["code"].isin(universe_codes)]
    out = out[out["amount"] >= amount_floor]
    out = out[out["pct"] >= min_pct]
    if max_pct is not None:
        if max_pct_inclusive:
            out = out[out["pct"] <= max_pct]
        else:
            out = out[out["pct"] < max_pct]
    sort_cols = [c for c in ("pct", "volume_ratio", "amount") if c in out.columns]
    out = out.sort_values(sort_cols, ascending=False)
    return out.head(limit)


def _minute_threshold(now: datetime | None = None) -> int:
    """早盘（开盘 30 分钟内）放宽分时最低行数，避免正常数据被误判为代理分。"""
    try:
        if now is None:
            now = datetime.now()
        # 09:30 后的分钟数
        m = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
        if 0 <= m < settings.minute_early_window_min:
            return settings.minute_min_rows_early
    except Exception:
        pass
    return settings.minute_min_rows


def _is_minute_usable(minute, *, now: datetime | None = None) -> bool:
    if minute is None:
        return False
    return len(minute) >= _minute_threshold(now)


def _short_minute_reason(minute, minute_err, *, now: datetime | None = None) -> str:
    """区分'分时数据太短'（早盘正常）和'分时拉取失败'（真降级）。"""
    if minute_err is not None:
        return f"分时拉取失败已降级: {minute_err}"
    if minute is None:
        return "分时数据为空，使用盘口代理打分"
    threshold = _minute_threshold(now)
    if len(minute) < threshold:
        return f"分时数据偏短({len(minute)}根/需{threshold}根)，早盘尚未走出形态，使用盘口代理打分"
    return "分时数据不足，使用盘口代理打分"


def _classify_from_breadth(breadth: dict[str, Any] | None) -> dict[str, Any] | None:
    if not breadth:
        return None
    return classify_sentiment(
        zt_count=breadth.get("zt_count"),
        dt_count=breadth.get("dt_count"),
        zhaban_rate=breadth.get("zhaban_rate"),
        max_lianban=breadth.get("max_lianban"),
        promotion_rate=breadth.get("promotion_rate"),
        n_up=breadth.get("n_up"),
        n_down=breadth.get("n_down"),
        zt_ice=settings.sentiment_zt_ice,
        zt_euphoria=settings.sentiment_zt_euphoria,
        lianban_euphoria=settings.sentiment_lianban_euphoria,
        promotion_ice=settings.sentiment_promotion_ice,
        zhaban_ice=settings.sentiment_zhaban_ice,
        down_ice=settings.sentiment_down_ice,
    )


def _market_env(
    *, force_index: str | None = None
) -> dict[str, Any]:
    """拉大盘指数快照，判定当日环境等级：normal / warn / block。

    warn：上证跌幅达 warn_pct -> 结果分数折减 + 前端风险横幅。
    block：上证跌幅达 block_pct -> 进攻型观望（直接空结果）。
    拉取失败静默降级为 normal（不阻塞扫描，note 里标注）。
    情绪温度计默认只作提示层；硬闸门仍由指数决定（sentiment_as_gate 可升级）。
    """
    ref = force_index or settings.market_env_ref_index
    sentiment = None
    if settings.sentiment_enabled:
        try:
            sentiment = _classify_from_breadth(mkt.fetch_market_breadth())
        except Exception:
            sentiment = None

    if not settings.market_env_enabled or settings.demo_mode:
        return {
            "level": "normal",
            "ref_index": ref,
            "pct": None,
            "note": "演示模式未接入大盘",
            "sentiment": sentiment,
        }
    try:
        snap = mkt.fetch_index_snapshot([ref])
    except Exception:
        snap = {}
    idx = snap.get(ref) or {}
    pct = idx.get("pct")
    if pct is None:
        return {
            "level": "normal",
            "ref_index": ref,
            "pct": None,
            "note": "大盘快照不可用，跳过环境闸门",
            "sentiment": sentiment,
        }
    level = "normal"
    if pct <= settings.market_env_block_pct:
        level = "block"
    elif pct <= settings.market_env_warn_pct:
        level = "warn"
    return {
        "level": level,
        "ref_index": ref,
        "ref_name": idx.get("name"),
        "pct": round(float(pct), 2),
        "note": "",
        "sentiment": sentiment,
    }


def _score_from_spot(row: dict[str, Any], *, mode: ScanMode = "fenshi") -> dict[str, Any]:
    """分时不可用时的兜底打分。"""
    pct = float(row.get("pct") or 0)
    amount = float(row.get("amount") or 0)
    vr = float(row.get("volume_ratio") or 0)
    open_p = float(row.get("open") or 0)
    price = float(row.get("price") or 0)
    high = float(row.get("high") or 0)
    low = float(row.get("low") or 0)

    score = 0.0
    reasons: list[str] = ["分时源切换/失败，使用盘口代理打分"]

    if mode == "leader_dip":
        if -2.0 <= pct <= 0.5:
            score += 25
            reasons.append(f"水下/平盘({pct:.2f}%)")
        elif pct <= 1.5:
            score += 15
            reasons.append(f"温和({pct:.2f}%)")
        if open_p > 0 and price >= open_p * 0.99:
            score += 12
            reasons.append("不低于开盘")
        if high > low > 0 and price > 0:
            pos = (price - low) / (high - low)
            if 0.35 <= pos <= 0.65:
                score += 10
                reasons.append("日内中位震荡")
    else:
        if pct >= 5:
            score += 30
            reasons.append(f"强势涨幅{pct:.2f}%")
        elif pct >= 3:
            score += 20
            reasons.append(f"涨幅{pct:.2f}%")
        elif pct >= 2:
            score += 12
            reasons.append(f"温和上涨{pct:.2f}%")

        yi = amount / 1e8
        if yi >= 5:
            score += 25
            reasons.append(f"成交活跃{yi:.1f}亿")
        elif yi >= 2:
            score += 15
            reasons.append(f"成交{yi:.1f}亿")
        elif yi >= 1:
            score += 8
            reasons.append(f"成交过亿")

        if vr >= 2:
            score += 20
            reasons.append(f"量比{vr:.2f}")
        elif vr >= 1.3:
            score += 10
            reasons.append(f"量比{vr:.2f}")

        if high > low > 0 and price > 0:
            pos = (price - low) / (high - low)
            if pos >= 0.7:
                score += 15
                reasons.append("靠近日内高位")
            elif pos >= 0.5:
                score += 8
                reasons.append("站上日内中位")

        if open_p > 0 and price >= open_p:
            score += 10
            reasons.append("现价不低于开盘")

    if in_attack_window():
        score = min(settings.proxy_score_cap, score + 5)
        reasons.insert(0, "核心买点窗口(10:15-10:40)")

    capped = min(score, settings.proxy_score_cap)
    if capped < score:
        reasons.append(f"代理分封顶{settings.proxy_score_cap}")

    return {
        "score": round(capped, 1),
        "above_vwap": None,
        "pullback": None,
        "reattack": None,
        "strong_push": None,
        "slope": None,
        "vol_expand": None,
        "vwap": None,
        "reasons": reasons,
        "proxy": True,
    }


def _rows_from_filter(df, *, min_amount_yi, min_pct, max_pct, limit, universe_codes, max_pct_inclusive):
    filtered = _filter_spot(
        df,
        min_amount_yi=min_amount_yi,
        min_pct=min_pct,
        max_pct=max_pct,
        limit=limit,
        universe_codes=universe_codes,
        max_pct_inclusive=max_pct_inclusive,
    )
    return filtered.to_dict(orient="records")


def _build_candidate_rows(
    spot,
    *,
    universe_policy: UniversePolicy,
    universe_codes: set[str],
    min_amount_yi: float,
    min_pct: float,
    max_pct: float | None,
    max_pct_inclusive: bool,
) -> tuple[list[dict[str, Any]], str]:
    """按策略组装待打分候选。返回 (rows, note)。"""
    base_limit = min(settings.max_candidates_spot, 40)
    if universe_policy == "hot_only":
        rows = _rows_from_filter(
            spot,
            min_amount_yi=min_amount_yi,
            min_pct=min_pct,
            max_pct=max_pct,
            limit=base_limit,
            universe_codes=universe_codes if universe_codes else None,
            max_pct_inclusive=max_pct_inclusive,
        )
        return rows, f"候选=强势板块硬过滤({len(rows)})"

    if universe_policy == "soft":
        soft_limit = min(max(settings.max_candidates_spot, 60), 80)
        rows = _rows_from_filter(
            spot,
            min_amount_yi=min_amount_yi,
            min_pct=min_pct,
            max_pct=max_pct,
            limit=soft_limit,
            universe_codes=None,
            max_pct_inclusive=max_pct_inclusive,
        )
        hot_n = sum(1 for r in rows if str(r.get("code") or "").zfill(6) in universe_codes)
        return rows, f"候选=全市场软加权({len(rows)}，其中热门{hot_n})"

    # quota：主池 + 卫星池
    primary_limit = min(base_limit, 32)
    sat_limit = max(8, int(round(base_limit * settings.universe_quota_satellite_pct)))
    primary = _rows_from_filter(
        spot,
        min_amount_yi=min_amount_yi,
        min_pct=min_pct,
        max_pct=max_pct,
        limit=primary_limit,
        universe_codes=universe_codes if universe_codes else None,
        max_pct_inclusive=max_pct_inclusive,
    )
    broad = _rows_from_filter(
        spot,
        min_amount_yi=min_amount_yi,
        min_pct=min_pct,
        max_pct=max_pct,
        limit=min(settings.max_candidates_spot, 80),
        universe_codes=None,
        max_pct_inclusive=max_pct_inclusive,
    )
    primary_codes = {str(r.get("code") or "").zfill(6) for r in primary}
    satellite = [
        r
        for r in broad
        if str(r.get("code") or "").zfill(6) not in primary_codes
        and str(r.get("code") or "").zfill(6) not in universe_codes
    ][:sat_limit]
    rows = primary + satellite
    return rows, f"候选=配额主池{len(primary)}+卫星{len(satellite)}"


def _apply_quota_top(
    results: list[dict[str, Any]],
    top_n: int,
    *,
    satellite_pct: float,
) -> list[dict[str, Any]]:
    """结果层强制为非热门留名额；不足则用总分回填。"""
    if top_n <= 0 or not results:
        return []
    hot = sorted(
        [x for x in results if x.get("in_hot_board")],
        key=lambda x: (x.get("score") or 0, x.get("pct") or 0),
        reverse=True,
    )
    cold = sorted(
        [x for x in results if not x.get("in_hot_board")],
        key=lambda x: (x.get("score") or 0, x.get("pct") or 0),
        reverse=True,
    )
    n_sat = min(len(cold), max(1, math.ceil(top_n * satellite_pct - 1e-12))) if cold else 0
    n_hot = top_n - n_sat
    picked = hot[:n_hot] + cold[:n_sat]
    if len(picked) < top_n:
        seen = {str(x.get("code")) for x in picked}
        rest = sorted(
            results,
            key=lambda x: (x.get("score") or 0, x.get("pct") or 0),
            reverse=True,
        )
        for x in rest:
            if len(picked) >= top_n:
                break
            c = str(x.get("code"))
            if c not in seen:
                picked.append(x)
                seen.add(c)
    return picked[:top_n]


def _build_sel_ctx(
    *,
    hot_boards: list[dict[str, Any]],
    universe_sectors: list[dict[str, Any]],
    board_tags: dict[str, list[str]],
    rows: list[dict[str, Any]],
    spot: Any,
) -> dict[str, Any]:
    """板块生命周期 + 龙头分层上下文（失败降级为空，不阻断扫描）。"""
    today = datetime.now().date().isoformat()
    life_map: dict[str, dict[str, Any]] = {}
    try:
        from db import list_confirmed_dip_codes, list_sector_history, upsert_sector_daily

        board_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for b in list(universe_sectors) + list(hot_boards):
            name = str(b.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            board_rows.append(b)
        upsert_sector_daily(today, board_rows)
        hist_rows = list_sector_history(settings.sector_life_lookback + 3)
        by_date: dict[str, set[str]] = {}
        up_by_date: dict[str, dict[str, int]] = {}
        for r in hist_rows:
            d = str(r.get("trade_date") or "")
            nm = str(r.get("name") or "")
            if not d or not nm:
                continue
            by_date.setdefault(d, set()).add(nm)
            if r.get("up_count") is not None:
                up_by_date.setdefault(d, {})[nm] = int(r.get("up_count") or 0)
        dated = list(by_date.items())
        current_names = list(seen)
        dates_sorted = sorted(by_date.keys(), reverse=True)
        today_up = {
            str(b.get("name")): int(b.get("up_count") or 0)
            for b in hot_boards
            if b.get("name") and b.get("up_count") is not None
        }
        prev_date = None
        if dates_sorted:
            prev_date = dates_sorted[1] if dates_sorted[0] == today and len(dates_sorted) > 1 else (
                dates_sorted[0] if dates_sorted[0] != today else None
            )
        life_map = sector_lifecycle_map(
            current_names,
            dated,
            today=today,
            min_history_dates=settings.sector_life_min_history,
            first_day=settings.sector_life_first_day,
            day2=settings.sector_life_day2,
            persistent=settings.sector_life_persistent,
            up_count_by_name=today_up,
            prev_up_count_by_name=up_by_date.get(prev_date or "") or {},
        )
        for s in universe_sectors:
            info = life_map.get(str(s.get("name") or ""))
            if info:
                s["consecutive"] = info.get("consecutive")
                s["life_coeff"] = info.get("coefficient")
                s["life_note"] = info.get("note")
        confirmed = list_confirmed_dip_codes()
    except Exception:
        confirmed = set()

    pct_map: dict[str, float] = {}
    try:
        if spot is not None and not getattr(spot, "empty", True):
            for rec in spot.to_dict(orient="records"):
                pct_map[str(rec.get("code") or "").zfill(6)] = float(rec.get("pct") or 0)
    except Exception:
        pass
    for r in rows:
        pct_map[str(r.get("code") or "").zfill(6)] = float(r.get("pct") or 0)
    try:
        top3 = board_rank_top_codes(pct_map, board_tags, top_n=settings.leader_board_top_n)
    except Exception:
        top3 = set()
    leader_names = {str(b.get("leader") or "").strip() for b in hot_boards if str(b.get("leader") or "").strip()}
    return {
        "life_map": life_map,
        "board_top3": top3,
        "leader_names": leader_names,
        "confirmed_dip": confirmed,
    }


def _enrich_one(
    row: dict[str, Any],
    board_tags: dict[str, list[str]],
    *,
    mode: ScanMode = "fenshi",
    universe_policy: UniversePolicy = "hot_only",
    sel_ctx: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    code = str(row["code"]).zfill(6)
    name = str(row["name"])
    minute = None
    daily = None
    minute_err = None

    try:
        if settings.demo_mode:
            minute = mkt.demo_minute(code)
        else:
            try:
                minute = mkt.fetch_minute(code)
            except Exception as e:
                minute_err = e
                minute = None
            try:
                daily = mkt.fetch_daily(code, limit=60)
            except Exception:
                daily = None
    except Exception as e:
        minute_err = e

    anom = anomaly_30d_pct(daily) if daily is not None else {
        "pct_from_low": 0.0,
        "ma5": None,
        "last_close": float(row.get("price") or 0),
        "last_open": float(row.get("open") or 0),
    }

    if mode == "leader_dip":
        now = datetime.now()
        if _is_minute_usable(minute, now=now):
            fenshi = score_leader_dip(
                minute,
                price=float(row.get("price") or 0),
                pct=float(row.get("pct") or 0),
                ma5=anom.get("ma5"),
                open_price=float(row.get("open") or 0),
            )
            fenshi["proxy"] = False
        else:
            fenshi = _score_from_spot(row, mode="leader_dip")
            fenshi["reasons"] = [_short_minute_reason(minute, minute_err, now=now)] + list(
                fenshi.get("reasons") or []
            )
    elif _is_minute_usable(minute, now=datetime.now()):
        fenshi = score_offensive_fenshi(minute)
        fenshi["proxy"] = False
    else:
        fenshi = _score_from_spot(row, mode="fenshi")
        fenshi["reasons"] = [_short_minute_reason(minute, minute_err, now=datetime.now())] + list(
            fenshi.get("reasons") or []
        )

    # 进攻型：今/昨量硬过滤 + 假推升降权（leader_dip 不套用 block）
    if mode == "fenshi" and not fenshi.get("proxy") and _is_minute_usable(minute, now=datetime.now()):
        day_vol = day_volume_health(
            minute,
            daily,
            block_by_1000=settings.day_vol_block_by_1000,
            block_by_1130=settings.day_vol_block_by_1130,
            warn_by_1130=settings.day_vol_warn_by_1130,
        )
        if day_vol.get("level") == "block":
            return None
        false_push = detect_false_push(
            minute,
            day_vol_ratio=day_vol.get("ratio"),
            day_vol_level=str(day_vol.get("level") or ""),
        )
        fenshi = apply_day_vol_and_false_push(fenshi, day_vol, false_push)
        # 追高惩罚：拉升中的票量能/斜率自然拉满，需补上"买入位置"维度
        if settings.chase_penalty_enabled:
            fenshi = apply_chase_penalty(
                fenshi,
                price=float(row.get("price") or 0),
                day_high=float(row.get("high") or 0),
                day_low=float(row.get("low") or 0),
                vwap=fenshi.get("vwap"),
            )
        if settings.zhaban_enabled:
            pre = float(row.get("pre_close") or 0)
            if pre <= 0 and daily is not None and "close" in getattr(daily, "columns", []):
                try:
                    closes = [float(x or 0) for x in list(daily["close"])]
                    if len(closes) >= 2:
                        pre = closes[-2] if closes[-2] > 0 else closes[-1]
                    elif closes:
                        pre = closes[-1]
                except Exception:
                    pre = 0.0
            if pre > 0:
                zb = detect_zhaban(minute, pre_close=pre, code=code, name=name)
                fenshi = apply_zhaban(fenshi, zb)

    risk = risk_flags(
        anom["pct_from_low"],
        price=float(row.get("price") or 0),
        ma5=anom.get("ma5"),
        open_price=float(row.get("open") or 0),
        warn=settings.anomaly_warn_pct,
        block=settings.anomaly_block_pct,
        days_to_regulatory_exit=anom.get("days_to_regulatory_exit"),
        new_anomaly_recent=bool(anom.get("new_anomaly_recent")),
        regulatory_window_end=anom.get("regulatory_window_end"),
        watch_days=settings.regulatory_watch_days,
    )
    if risk["level"] == "block":
        return None

    reasons = list(fenshi.get("reasons") or [])
    tags = board_tags.get(code, [])
    in_hot = bool(tags)
    if tags:
        reasons.insert(0, f"强势板块: {', '.join(tags[:2])}")

    ctx = sel_ctx or {}
    life = best_sector_life(tags, ctx.get("life_map") or {}) if settings.sector_life_enabled else None
    hot_base = 0.0
    if tags:
        if universe_policy == "soft":
            hot_base = settings.soft_hot_board_bonus
        elif mode == "leader_dip":
            hot_base = settings.leader_dip_hot_board_bonus
        elif settings.sector_life_enabled:
            hot_base = settings.sector_hot_board_bonus
    adj = apply_selection_adjustments(
        score=float(fenshi.get("score") or 0),
        reasons=reasons,
        code=code,
        name=name,
        pct=float(row.get("pct") or 0),
        daily=daily,
        tags=tags,
        mode=mode,
        score_cap=settings.fenshi_score_cap,
        life=life,
        in_board_top=code in (ctx.get("board_top3") or set()),
        is_ths_leader=name in (ctx.get("leader_names") or set()),
        confirmed_dip=code in (ctx.get("confirmed_dip") or set()),
        hot_bonus_base=hot_base,
        zt_lookback=settings.leader_zt_lookback,
        zt_min_count=settings.leader_zt_min_count,
        lianban_min=settings.leader_lianban_min,
        zt_bonus=settings.leader_zt_bonus,
        rank_bonus=settings.leader_board_rank_bonus,
        ths_bonus=settings.leader_ths_bonus,
        follower_penalty=settings.follower_penalty,
        first_day_attack_penalty=settings.sector_first_day_attack_penalty,
        trapped_ratio_warn=settings.trapped_warn_ratio,
        trapped_penalty=settings.trapped_penalty,
        trapped_lookback=settings.trapped_lookback,
        dip_confirm_bonus=settings.dip_confirm_bonus,
        leader_layer_enabled=settings.leader_layer_enabled,
        trapped_enabled=settings.trapped_enabled,
    )
    adj = finalize_trapped_and_dip(adj, price=float(row.get("price") or 0), daily=daily)
    fenshi["score"] = adj["score"]
    reasons = list(adj.get("reasons") or reasons)

    # 进攻型分时：非回踩再攻/强势推升形态降权
    if (
        mode == "fenshi"
        and not fenshi.get("proxy")
        and not fenshi.get("strong_push")
        and not (fenshi.get("pullback") and fenshi.get("reattack"))
    ):
        fenshi["score"] = max(0.0, float(fenshi.get("score") or 0) - settings.no_pattern_penalty)
        reasons.append("未确认回踩再攻(降权)")

    return {
        "code": code,
        "name": name,
        "pct": round(float(row.get("pct") or 0), 2),
        "price": round(float(row.get("price") or 0), 3),
        "amount": float(row.get("amount") or 0),
        "turnover": round(float(row.get("turnover") or 0), 2),
        "volume_ratio": round(float(row.get("volume_ratio") or 0), 2),
        "score": fenshi.get("score") or 0,
        "in_hot_board": in_hot,
        "board_tags": tags,
        "reasons": reasons,
        "selection": {
            "tags": adj.get("tags") or [],
            "zt": adj.get("zt") or {},
            "trapped_ratio": adj.get("trapped_ratio"),
            "life": adj.get("life"),
            "coeff": adj.get("coeff"),
        },
        "risk": {
            **risk,
            "anomaly_pct": anom["pct_from_low"],
            "ma5": anom.get("ma5"),
            "days_to_regulatory_exit": anom.get("days_to_regulatory_exit"),
            "regulatory_window_end": anom.get("regulatory_window_end"),
        },
        "fenshi": {
            "above_vwap": fenshi.get("above_vwap"),
            "pullback": fenshi.get("pullback"),
            "reattack": fenshi.get("reattack"),
            "strong_push": fenshi.get("strong_push"),
            "slope": fenshi.get("slope"),
            "vol_expand": fenshi.get("vol_expand"),
            "vwap": fenshi.get("vwap"),
            "proxy": bool(fenshi.get("proxy")),
            "day_vol_ratio": fenshi.get("day_vol_ratio"),
            "day_vol_level": fenshi.get("day_vol_level"),
            "false_push": fenshi.get("false_push"),
            "day_position": fenshi.get("day_position"),
            "vwap_deviation": fenshi.get("vwap_deviation"),
            "chase_penalty": fenshi.get("chase_penalty"),
            "wave_volume": fenshi.get("wave_volume"),
            "zhaban": fenshi.get("zhaban"),
        },
    }


def _empty_scan_payload(
    *,
    session_note: str,
    spot_source: str,
    min_amount_yi: float,
    min_pct: float,
    max_pct: float | None,
    session: SessionFilter,
    top_n: int,
    mode: ScanMode,
    hot_boards: list[dict[str, Any]],
    universe_sectors: list[dict[str, Any]],
    universe_size: int = 0,
    market_env: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "session_note": session_note,
        "data_source": {
            "spot": spot_source,
            "minute": "skipped",
            "candidates": 0,
            "scored": 0,
            "fenshi_ok": 0,
            "reattack_ok": 0,
            "strong_push_ok": 0,
            "universe_size": universe_size,
        },
        "hot_boards": hot_boards,
        "universe_sectors": universe_sectors,
        "params": {
            "min_amount_yi": min_amount_yi,
            "min_pct": min_pct,
            "max_pct": max_pct,
            "session": session,
            "top_n": top_n,
            "mode": mode,
            "demo_mode": settings.demo_mode,
            "strategy_version": settings.strategy_version,
        },
        "count": 0,
        "items": [],
        "timings": {},
        "error_code": None,
        "market_env": market_env,
    }


def run_scan(
    *,
    min_amount_yi: float | None = None,
    min_pct: float | None = None,
    max_pct: float | None = None,
    session: SessionFilter = "auto",
    top_n: int | None = None,
    board_top_n: int = 15,
    mode: ScanMode = "fenshi",
    universe_policy: UniversePolicy = "hot_only",
    on_progress: ProgressCb | None = None,
    should_cancel: CancelCb | None = None,
) -> dict[str, Any]:
    timings: dict[str, float] = {}
    t_all = time.perf_counter()

    def progress(stage: str, pct: float, message: str) -> None:
        logger.info("scan stage=%s progress=%.0f%% %s", stage, pct * 100, message)
        if on_progress:
            try:
                on_progress(stage, pct, message)
            except Exception:
                pass

    def check_cancel() -> None:
        if should_cancel and should_cancel():
            raise ScanCancelled("cancelled")

    def mark(name: str, t0: float) -> None:
        timings[name] = round((time.perf_counter() - t0) * 1000, 1)

    if mode == "leader_dip":
        min_pct = min_pct if min_pct is not None else settings.leader_dip_min_pct
        max_pct = max_pct if max_pct is not None else settings.leader_dip_max_pct
    else:
        min_pct = min_pct if min_pct is not None else settings.min_pct
        max_pct = max_pct if max_pct is not None else settings.max_pct
    min_amount_yi = min_amount_yi if min_amount_yi is not None else settings.min_amount_yi
    top_n = top_n if top_n is not None else settings.top_n_result
    max_pct_inclusive = mode == "leader_dip"

    progress("session", 0.05, "检查交易时段")
    allowed, session_note = session_allowed(session, demo_mode=settings.demo_mode)
    if not allowed:
        payload = _empty_scan_payload(
            session_note=session_note,
            spot_source="skipped",
            min_amount_yi=min_amount_yi,
            min_pct=min_pct,
            max_pct=max_pct,
            session=session,
            top_n=top_n,
            mode=mode,
            hot_boards=[],
            universe_sectors=[],
        )
        payload["error_code"] = "session_blocked"
        payload["timings"] = timings
        return payload

    mode_label = "龙头低吸" if mode == "leader_dip" else "进攻型分时"
    if universe_policy == "quota":
        mode_label += "·配额测试"
    elif universe_policy == "soft":
        mode_label += "·软加权测试"
    session_note = f"{mode_label} · {session_note}"

    # 大盘环境闸门：指数暴跌日整体降级/观望；情绪默认提示级
    market_env = _market_env()
    sent = market_env.get("sentiment") or {}
    ice = bool(sent.get("ice"))
    euphoria = bool(sent.get("euphoria"))
    if sent.get("label"):
        temp = sent.get("temperature")
        extra = f"{temp:g}" if isinstance(temp, (int, float)) else ""
        session_note += f" · 情绪{sent.get('label')}" + (f" {extra}" if extra else "")
        if sent.get("hint"):
            session_note += f"（{sent.get('hint')}）"

    block_scan = market_env.get("level") == "block"
    if settings.sentiment_as_gate and ice and mode == "fenshi":
        block_scan = True
        session_note += " · 情绪冰点硬闸门，进攻型暂停"

    if block_scan:
        if market_env.get("level") == "block" and market_env.get("pct") is not None:
            session_note += (
                f" · 大盘环境观望（{market_env.get('ref_name') or market_env.get('ref_index')}"
                f" {market_env.get('pct'):+.2f}% ≤ {settings.market_env_block_pct}%，进攻型暂停）"
            )
        payload = _empty_scan_payload(
            session_note=session_note,
            spot_source="skipped",
            min_amount_yi=min_amount_yi,
            min_pct=min_pct,
            max_pct=max_pct,
            session=session,
            top_n=top_n,
            mode=mode,
            hot_boards=[],
            universe_sectors=[],
        )
        payload["error_code"] = "market_blocked"
        payload["market_env"] = market_env
        payload["timings"] = timings
        return payload
    elif market_env.get("level") == "warn":
        skip_haircut = bool(settings.sentiment_soft_adjust and euphoria)
        session_note += (
            f" · 大盘偏弱（{market_env.get('ref_name') or market_env.get('ref_index')}"
            f" {market_env.get('pct'):+.2f}%"
            + (
                "，情绪亢奋未折分"
                if skip_haircut
                else f"，分数已折减至 {settings.market_env_score_factor:g}"
            )
            + "）"
        )
    elif market_env.get("pct") is not None:
        session_note += (
            f" · 大盘（{market_env.get('ref_name') or market_env.get('ref_index')}"
            f" {market_env.get('pct'):+.2f}%）"
        )
        if settings.sentiment_soft_adjust and ice and mode == "fenshi":
            session_note += f" · 情绪冰点提示级折分×{settings.market_env_score_factor:g}"

    hot_boards: list[dict[str, Any]] = []
    universe_sectors: list[dict[str, Any]] = []
    universe_codes: set[str] = set()
    board_tags: dict[str, list[str]] = {}
    spot = None
    spot_source = "none"
    spot_empty = True

    try:
        check_cancel()
        if settings.demo_mode:
            hot_boards = [{"name": "演示板块", "pct": 3.2, "up_count": 12, "leader": "演示"}]
            universe_codes = {"600812", "002212", "003000"}
            universe_sectors = [{"name": "演示板块", "pct": 3.2, "type": "演示", "members": 3}]
            board_tags = {"600812": ["演示板块"], "002212": ["演示板块"]}
            spot = mkt.get_spot_df_or_empty(use_isolated=False)
            spot_source = mkt.last_spot_source()
            spot_empty = spot is None or getattr(spot, "empty", True)
        else:
            progress("boards", 0.12, "拉取强势板块参考")
            t0 = time.perf_counter()
            try:
                hot_boards = mkt.fetch_concept_boards_top(board_top_n)
                hot_boards = [b for b in hot_boards if abs(float(b.get("pct") or 0)) < 30]
            except Exception as e:
                hot_boards = []
                session_note += f" · 板块参考拉取失败: {str(e)[:60]}"
            mark("boards_ms", t0)

            check_cancel()
            progress("universe", 0.25, "构建强势板块成分池")
            t0 = time.perf_counter()
            try:
                uni = mkt.fetch_hot_sector_universe(
                    industry_top=5,
                    concept_top=3,
                    sector_min_pct=settings.sector_min_pct,
                    use_isolated=settings.sector_universe_use_isolated,
                    ttl=settings.universe_cache_ttl,
                )
                universe_sectors = uni.get("sectors") or []
                universe_codes = set(uni.get("codes") or [])
                board_tags = dict(uni.get("code_tags") or {})
                mark("universe_ms", t0)
                if not universe_codes:
                    if universe_policy == "hot_only":
                        session_note += " · 板块成分池为空，跳过扫描（不扫全市场）"
                        payload = _empty_scan_payload(
                            session_note=session_note,
                            spot_source=spot_source,
                            min_amount_yi=min_amount_yi,
                            min_pct=min_pct,
                            max_pct=max_pct,
                            session=session,
                            top_n=top_n,
                            mode=mode,
                            hot_boards=hot_boards[:board_top_n],
                            universe_sectors=universe_sectors[:12],
                        )
                        payload["error_code"] = "empty_universe"
                        payload["timings"] = timings
                        return payload
                    session_note += " · 板块成分池为空，测试策略继续用全市场候选"
            except Exception as e:
                mark("universe_ms", t0)
                if universe_policy == "hot_only":
                    session_note += f" · 板块成分池失败，跳过扫描: {str(e)[:60]}"
                    payload = _empty_scan_payload(
                        session_note=session_note,
                        spot_source=spot_source,
                        min_amount_yi=min_amount_yi,
                        min_pct=min_pct,
                        max_pct=max_pct,
                        session=session,
                        top_n=top_n,
                        mode=mode,
                        hot_boards=hot_boards[:board_top_n],
                        universe_sectors=universe_sectors[:12],
                    )
                    payload["error_code"] = "universe_failed"
                    payload["timings"] = timings
                    return payload
                session_note += f" · 板块成分池失败，测试策略降级全市场: {str(e)[:60]}"

            check_cancel()
            # soft/quota 需要全市场快照；hot_only 优先成分实时报价
            need_full_spot = universe_policy in ("soft", "quota")
            if universe_codes and not need_full_spot:
                progress("quotes", 0.45, f"拉取成分实时报价({len(universe_codes)}只)")
                t0 = time.perf_counter()
                try:
                    rt = mkt.fetch_realtime_quotes(list(universe_codes))
                    spot = mkt.quotes_to_spot_df(rt)
                    spot_source = "sina_rt_universe"
                    spot_empty = spot is None or getattr(spot, "empty", True)
                    if not spot_empty:
                        session_note += f" · 用板块成分实时报价({len(spot)}只)"
                except Exception as e:
                    session_note += f" · 板块实时报价失败: {str(e)[:60]}"
                mark("quotes_ms", t0)

            if spot_empty or need_full_spot:
                check_cancel()
                progress(
                    "spot",
                    0.55,
                    "拉取全市场快照" if need_full_spot else "实时报价为空，尝试全市场快照(子进程)",
                )
                t0 = time.perf_counter()
                try:
                    spot = mkt.get_spot_df_or_empty(
                        use_isolated=settings.scan_use_isolated,
                        ttl=settings.spot_cache_ttl,
                    )
                    spot_source = mkt.last_spot_source()
                    spot_empty = spot is None or getattr(spot, "empty", True)
                    if need_full_spot and not spot_empty:
                        session_note += f" · 全市场快照({len(spot)}只)"
                except Exception as e:
                    session_note += f" · 全市场快照失败: {str(e)[:60]}"
                    spot_empty = True
                mark("spot_ms", t0)

            if spot_empty:
                session_note += " · 无真实行情数据，返回空结果（未使用演示数据）"
                payload = _empty_scan_payload(
                    session_note=session_note,
                    spot_source=spot_source,
                    min_amount_yi=min_amount_yi,
                    min_pct=min_pct,
                    max_pct=max_pct,
                    session=session,
                    top_n=top_n,
                    mode=mode,
                    hot_boards=hot_boards[:board_top_n],
                    universe_sectors=universe_sectors[:12],
                    universe_size=len(universe_codes),
                )
                payload["error_code"] = "no_quotes"
                payload["timings"] = timings
                return payload

        check_cancel()
        progress("filter", 0.62, "预筛候选")
        rows, cand_note = _build_candidate_rows(
            spot,
            universe_policy=universe_policy,
            universe_codes=universe_codes,
            min_amount_yi=min_amount_yi,
            min_pct=min_pct,
            max_pct=max_pct,
            max_pct_inclusive=max_pct_inclusive,
        )
        session_note += f" · {cand_note}"

        if universe_policy == "hot_only" and universe_codes and not rows:
            session_note += " · 强势板块成分内无满足条件的标的"

        if rows and not settings.demo_mode and spot_source != "sina_rt_universe":
            try:
                rt = mkt.fetch_realtime_quotes([str(r.get("code") or "") for r in rows])
                for r in rows:
                    code = str(r.get("code") or "").zfill(6)
                    q = rt.get(code) or {}
                    if float(q.get("price") or 0) <= 0:
                        continue
                    r["price"] = float(q["price"])
                    r["pct"] = float(q.get("pct") if q.get("pct") is not None else r.get("pct") or 0)
                    r["open"] = float(q.get("open") or r.get("open") or 0)
                    if q.get("name"):
                        r["name"] = q["name"]
                    for k in ("high", "low", "pre_close", "amount"):
                        if float(q.get(k) or 0) > 0:
                            r[k] = float(q[k])
            except Exception:
                pass

        # soft/quota：用成分标签标记热门（全市场票也可能命中）
        if universe_policy in ("soft", "quota") and universe_codes:
            for r in rows:
                code = str(r.get("code") or "").zfill(6)
                if code in universe_codes and code not in board_tags:
                    board_tags[code] = board_tags.get(code) or ["强势板块"]

        sel_ctx = _build_sel_ctx(
            hot_boards=hot_boards,
            universe_sectors=universe_sectors,
            board_tags=board_tags,
            rows=rows,
            spot=spot,
        )

        results: list[dict[str, Any]] = []
        timed_out = 0
        total = max(len(rows), 1)
        done_n = 0

        progress("enrich", 0.7, f"分时打分 0/{len(rows)}")
        t0 = time.perf_counter()
        workers = 4 if not settings.demo_mode else 2
        pool = ThreadPoolExecutor(max_workers=workers)
        futs = [
            pool.submit(
                _enrich_one,
                r,
                board_tags,
                mode=mode,
                universe_policy=universe_policy,
                sel_ctx=sel_ctx,
            )
            for r in rows
        ]
        enrich_budget = max(float(settings.enrich_timeout_seconds), 1.0)
        try:
            for fut in as_completed(futs, timeout=enrich_budget):
                check_cancel()
                try:
                    item = fut.result()
                except Exception:
                    timed_out += 1
                    item = None
                done_n += 1
                if item is not None:
                    results.append(item)
                if done_n % 3 == 0 or done_n == total:
                    progress(
                        "enrich",
                        0.7 + 0.25 * (done_n / total),
                        f"分时打分 {done_n}/{len(rows)}",
                    )
        except FutureTimeoutError:
            # 整体预算耗尽：未完成的候选计超时并跳过，不再无限等待
            pending = [f for f in futs if not f.done()]
            for f in pending:
                f.cancel()
            timed_out += len(pending)
            done_n = len(rows)
            progress("enrich", 0.95, f"分时打分超时预算{enrich_budget:g}s，跳过 {len(pending)} 只")
        finally:
            # 不等待残留线程：慢请求在后台自行结束，不阻塞本轮扫描
            pool.shutdown(wait=False, cancel_futures=True)
        mark("enrich_ms", t0)

        if timed_out:
            session_note += f" · {timed_out}只分时超时已跳过"

        # 大盘偏弱 / 情绪冰点：对进攻型分数整体折减（龙头低吸不做折减）
        apply_factor = mode == "fenshi" and (
            (market_env.get("level") == "warn" and not (settings.sentiment_soft_adjust and euphoria))
            or (
                settings.sentiment_soft_adjust
                and ice
                and market_env.get("level") == "normal"
            )
        )
        if apply_factor:
            factor = float(settings.market_env_score_factor)
            for x in results:
                x["score"] = round(float(x.get("score") or 0) * factor, 1)

        results.sort(key=lambda x: (x.get("score") or 0, x.get("pct") or 0), reverse=True)
        if universe_policy == "quota":
            results = _apply_quota_top(
                results,
                top_n,
                satellite_pct=settings.universe_quota_satellite_pct,
            )
            session_note += (
                f" · 结果配额非主线约{int(round(100 * settings.universe_quota_satellite_pct))}%"
            )
        else:
            results = results[:top_n]
        scored = sum(1 for x in results if (x.get("score") or 0) > 0)
        fenshi_ok = sum(1 for x in results if not (x.get("fenshi") or {}).get("proxy"))
        reattack_ok = sum(
            1
            for x in results
            if (x.get("fenshi") or {}).get("pullback") and (x.get("fenshi") or {}).get("reattack")
        )
        strong_push_ok = sum(1 for x in results if (x.get("fenshi") or {}).get("strong_push"))

        if universe_policy == "hot_only" and universe_codes:
            session_note += f" · 候选池=强势板块成分({len(universe_codes)}只)"
        elif universe_codes:
            session_note += f" · 热门成分参考({len(universe_codes)}只)"

        pct_hint = f"≤{max_pct}%" if max_pct_inclusive else f"<{max_pct}%"
        timings["total_ms"] = round((time.perf_counter() - t_all) * 1000, 1)
        payload = {
            "session_note": session_note + f" · 涨幅{pct_hint}",
            "data_source": {
                "spot": spot_source,
                "minute": "tencent_fallback" if spot_source != "demo" else "demo",
                "candidates": len(rows),
                "scored": scored,
                "fenshi_ok": fenshi_ok,
                "reattack_ok": reattack_ok,
                "strong_push_ok": strong_push_ok,
                "universe_size": len(universe_codes),
            },
            "hot_boards": hot_boards[:board_top_n],
            "universe_sectors": universe_sectors[:12],
            "params": {
                "min_amount_yi": min_amount_yi,
                "min_pct": min_pct,
                "max_pct": max_pct,
                "session": session,
                "top_n": top_n,
                "mode": mode,
                "universe_policy": universe_policy,
                "demo_mode": settings.demo_mode,
                "strategy_version": settings.strategy_version,
            },
            "count": len(results),
            "items": results,
            "timings": timings,
            "error_code": None,
            "market_env": market_env,
        }
        progress("done", 1.0, f"完成，命中 {len(results)} 只")
        try:
            save_scan_snapshot(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
        # 扫描质量摘要落库（结构化、长期积累，供调参/数据源健康对比）
        try:
            proxy_count = sum(1 for x in results if (x.get("fenshi") or {}).get("proxy"))
            positions = [
                float((x.get("fenshi") or {}).get("day_position"))
                for x in results
                if (x.get("fenshi") or {}).get("day_position") is not None
            ]
            save_scan_quality(
                {
                    "mode": mode,
                    "universe_policy": universe_policy,
                    "candidates": len(rows),
                    "scored": scored,
                    "fenshi_ok": fenshi_ok,
                    "proxy_count": proxy_count,
                    "timed_out": timed_out,
                    "total_ms": timings.get("total_ms"),
                    "market_env_level": market_env.get("level"),
                    "market_pct": market_env.get("pct"),
                    "spot_source": spot_source,
                    "strategy_version": settings.strategy_version,
                    "top_avg_day_position": (
                        round(sum(positions) / len(positions), 3) if positions else None
                    ),
                }
            )
        except Exception:
            pass
        return payload
    except ScanCancelled:
        timings["total_ms"] = round((time.perf_counter() - t_all) * 1000, 1)
        payload = _empty_scan_payload(
            session_note=session_note + " · 已取消",
            spot_source=spot_source,
            min_amount_yi=min_amount_yi,
            min_pct=min_pct,
            max_pct=max_pct,
            session=session,
            top_n=top_n,
            mode=mode,
            hot_boards=hot_boards[:board_top_n],
            universe_sectors=universe_sectors[:12],
            universe_size=len(universe_codes),
        )
        payload["error_code"] = "cancelled"
        payload["timings"] = timings
        raise



def watchlist_quotes(
    codes: list[str],
    *,
    include_risk: bool = False,
) -> list[dict[str, Any]]:
    """批量现价。默认只拉实时盘口；include_risk 才会逐票拉日线算异动（较慢）。"""
    if not codes:
        return []
    rt: dict[str, dict] = {}
    try:
        rt = mkt.fetch_realtime_quotes(codes)
    except Exception:
        rt = {}

    spot_map: dict[str, dict] = {}
    missing = [c.zfill(6) for c in codes if float((rt.get(c.zfill(6)) or {}).get("price") or 0) <= 0]
    if missing:
        try:
            spot = mkt.get_spot_df_or_empty(use_isolated=False)
            if spot is not None and not getattr(spot, "empty", True):
                spot = spot.copy()
                spot["code"] = spot["code"].astype(str).str.zfill(6)
                for code in missing:
                    row = spot[spot["code"] == code]
                    if not row.empty:
                        r = row.iloc[0]
                        spot_map[code] = {
                            "name": str(r.get("name") or ""),
                            "price": float(r.get("price") or 0),
                            "pct": float(r.get("pct") or 0),
                            "open": float(r.get("open") or 0),
                        }
        except Exception:
            pass

    bases: list[dict[str, Any]] = []
    for code in codes:
        code = code.zfill(6)
        q = rt.get(code) or spot_map.get(code) or {}
        bases.append(
            {
                "code": code,
                "name": str(q.get("name") or ""),
                "price": float(q.get("price") or 0),
                "pct": float(q.get("pct") or 0),
                "open": float(q.get("open") or 0),
                "risk": {"level": "ok", "messages": [], "anomaly_progress": 0, "anomaly_pct": None},
            }
        )

    if not include_risk or settings.demo_mode:
        if settings.demo_mode:
            for b in bases:
                b["risk"] = {
                    "level": "ok",
                    "messages": [],
                    "anomaly_progress": 20,
                    "anomaly_pct": 40.0,
                    "ma5": b["price"] * 0.98 if b["price"] else None,
                }
        return bases

    def _risk_one(base: dict[str, Any]) -> dict[str, Any]:
        try:
            daily = mkt.fetch_daily(base["code"], limit=60)
            anom = anomaly_30d_pct(daily)
            risk = risk_flags(
                anom["pct_from_low"],
                price=base["price"],
                ma5=anom.get("ma5"),
                open_price=base["open"] or anom.get("last_open"),
                warn=settings.anomaly_warn_pct,
                block=settings.anomaly_block_pct,
                days_to_regulatory_exit=anom.get("days_to_regulatory_exit"),
                new_anomaly_recent=bool(anom.get("new_anomaly_recent")),
                regulatory_window_end=anom.get("regulatory_window_end"),
                watch_days=settings.regulatory_watch_days,
            )
            base["risk"] = {**risk, "anomaly_pct": anom["pct_from_low"], "ma5": anom.get("ma5")}
        except Exception as e:
            base["risk"] = {
                "level": "ok",
                "messages": [str(e)],
                "anomaly_progress": 0,
                "anomaly_pct": 0,
            }
        return base

    with ThreadPoolExecutor(max_workers=min(6, max(len(bases), 1))) as pool:
        return list(pool.map(_risk_one, bases))
