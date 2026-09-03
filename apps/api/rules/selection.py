"""选股层：板块生命周期、龙头分层、套牢盘代理、企稳确认。

与分时打分解耦——先选「能不能做 / 做哪个板块 / 做板块里的谁」，再打分时形态。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd


def limit_pct_for_code(code: str, name: str = "") -> float:
    """主板 10%、创业板/科创 20%、北交所 30%、ST 5%。"""
    c = str(code).zfill(6)
    n = (name or "").upper()
    if "ST" in n or "退" in (name or ""):
        return 5.0
    if c.startswith("688") or c.startswith("689") or c.startswith("300"):
        return 20.0
    if c.startswith("4") or c.startswith("8") or c.startswith("92"):
        return 30.0
    return 10.0


def is_limit_up(code: str, pct: float, *, name: str = "", pre_close: float | None = None, price: float | None = None) -> bool:
    lim = limit_pct_for_code(code, name)
    if pct >= lim - 0.2:
        return True
    if pre_close and pre_close > 0 and price and price > 0:
        limit_price = round(pre_close * (1 + lim / 100.0) + 1e-9, 2)
        return price + 1e-9 >= limit_price
    return False


def limit_up_stats(daily: pd.DataFrame | None, code: str, *, lookback: int = 10, name: str = "") -> dict[str, Any]:
    """近 N 日涨停次数与最大连板。日线不足时返回空特征。"""
    empty = {"zt_count": 0, "max_lianban": 0, "leader_candidate": False}
    if daily is None or daily.empty or "close" not in daily.columns:
        return empty
    df = daily.dropna(subset=["close"]).tail(lookback + 1).copy()
    if len(df) < 3:
        return empty
    lim = limit_pct_for_code(code, name)
    close = pd.to_numeric(df["close"], errors="coerce")
    pct = close.pct_change() * 100.0
    flags = (pct >= (lim - 0.2)).fillna(False).astype(bool).tolist()
    # 去掉因 pct_change 产生的首根 NaN
    if flags:
        flags[0] = False
    zt_count = int(sum(flags))
    streak = 0
    max_lb = 0
    for f in flags:
        if f:
            streak += 1
            max_lb = max(max_lb, streak)
        else:
            streak = 0
    return {
        "zt_count": zt_count,
        "max_lianban": max_lb,
        "leader_candidate": bool(zt_count >= 2 or max_lb >= 2),
    }


def trapped_share_ratio(daily: pd.DataFrame | None, price: float, *, lookback: int = 60) -> float | None:
    """日线近似 VAP：把每根 K 的成交在 [low, high] 上均匀分布，现价上方筹码占比。"""
    if daily is None or daily.empty or price <= 0 or "close" not in daily.columns:
        return None
    df = daily.dropna(subset=["close"]).tail(lookback).copy()
    if df.empty:
        return None
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce") if "high" in df.columns else close
    low = pd.to_numeric(df["low"], errors="coerce") if "low" in df.columns else close
    if "amount" in df.columns:
        w = pd.to_numeric(df["amount"], errors="coerce").fillna(0).clip(lower=0)
    elif "volume" in df.columns:
        w = pd.to_numeric(df["volume"], errors="coerce").fillna(0).clip(lower=0)
    else:
        w = pd.Series(1.0, index=df.index)
    if float(w.sum()) <= 0:
        return None

    above = 0.0
    total = 0.0
    for h, l, c, wt in zip(high.tolist(), low.tolist(), close.tolist(), w.tolist()):
        wt = float(wt or 0)
        if wt <= 0:
            continue
        hi = float(h) if h == h else float(c)
        lo = float(l) if l == l else float(c)
        if hi < lo:
            hi, lo = lo, hi
        total += wt
        if lo >= price:
            above += wt
        elif hi <= price:
            continue
        elif hi > lo:
            above += wt * (hi - price) / (hi - lo)
    if total <= 0:
        return None
    return round(above / total, 3)


def consecutive_streak(name: str, dated_names: list[tuple[str, set[str]]]) -> int:
    """dated_names 按日期从新到旧。名称连续出现的天数。"""
    streak = 0
    for _, names in dated_names:
        if name in names:
            streak += 1
        else:
            break
    return streak


def sector_lifecycle_map(
    current_names: list[str],
    history: list[tuple[str, set[str]]],
    *,
    today: str | None = None,
    min_history_dates: int = 5,
    first_day: float = 0.6,
    day2: float = 1.0,
    persistent: float = 1.2,
    up_count_by_name: dict[str, int] | None = None,
    prev_up_count_by_name: dict[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """给当前上榜板块打生命周期。history 含今日则 streak 含今日。"""
    today = today or datetime.now().date().isoformat()
    distinct_dates = {d for d, _ in history}
    history_ready = len(distinct_dates) >= min_history_dates
    # 最新在前
    ordered = sorted(history, key=lambda x: x[0], reverse=True)
    out: dict[str, dict[str, Any]] = {}
    for name in current_names:
        streak = consecutive_streak(name, ordered)
        appeared_before = any(name in names for d, names in ordered if d != today)
        first = streak <= 1 and not appeared_before
        up_now = (up_count_by_name or {}).get(name)
        up_prev = (prev_up_count_by_name or {}).get(name)
        expanding = (
            up_now is not None
            and up_prev is not None
            and up_now > up_prev
            and streak >= 2
        )
        if not history_ready:
            coeff = 1.0
            note = "板块历史不足，生命周期系数取 1.0"
        elif first:
            coeff = first_day
            note = "首日上榜"
        elif streak >= 3 or expanding:
            coeff = persistent
            note = f"主线第{streak}天" + ("·涨停家数递增" if expanding else "")
        else:
            coeff = day2
            note = f"连续{streak}日"
        out[name] = {
            "consecutive": streak,
            "coefficient": coeff,
            "first_day": first,
            "note": note,
            "history_ready": history_ready,
        }
    return out


def best_sector_life(tags: list[str], life_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not tags or not life_map:
        return None
    best = None
    for t in tags:
        info = life_map.get(t)
        if not info:
            continue
        if best is None or float(info.get("coefficient") or 0) > float(best.get("coefficient") or 0):
            best = {**info, "name": t}
    return best


def board_rank_top_codes(
    pct_by_code: dict[str, float],
    board_tags: dict[str, list[str]],
    *,
    top_n: int = 3,
) -> set[str]:
    by_board: dict[str, list[tuple[str, float]]] = {}
    for code, tags in board_tags.items():
        pct = float(pct_by_code.get(code) or 0)
        for tag in tags or []:
            by_board.setdefault(tag, []).append((code, pct))
    top: set[str] = set()
    for members in by_board.values():
        members.sort(key=lambda x: x[1], reverse=True)
        for c, _ in members[:top_n]:
            top.add(c)
    return top


def apply_selection_adjustments(
    *,
    score: float,
    reasons: list[str],
    code: str,
    name: str,
    pct: float,
    daily: pd.DataFrame | None,
    tags: list[str],
    mode: str,
    score_cap: float,
    life: dict[str, Any] | None,
    in_board_top: bool,
    is_ths_leader: bool,
    confirmed_dip: bool,
    hot_bonus_base: float,
    zt_lookback: int = 10,
    zt_min_count: int = 2,
    lianban_min: int = 2,
    zt_bonus: float = 10.0,
    rank_bonus: float = 5.0,
    ths_bonus: float = 5.0,
    follower_penalty: float = 10.0,
    first_day_attack_penalty: float = 5.0,
    trapped_ratio_warn: float = 0.40,
    trapped_penalty: float = 8.0,
    trapped_lookback: int = 60,
    dip_confirm_bonus: float = 8.0,
    leader_layer_enabled: bool = True,
    trapped_enabled: bool = True,
) -> dict[str, Any]:
    """把选股层加减分叠到已有分时分上。返回新 score/reasons + 结构化标签。"""
    reasons = list(reasons)
    tags_out: list[str] = []
    coeff = float(life.get("coefficient") or 1.0) if life else 1.0
    first_day = bool(life.get("first_day")) if life else False
    consecutive = int(life.get("consecutive") or 0) if life else 0
    if life:
        note = str(life.get("note") or "")
        if note:
            reasons.insert(0, f"板块生命周期: {life.get('name') or ''} {note}×{coeff:g}")
    if hot_bonus_base:
        bonus_coeff = coeff if mode == "fenshi" else 1.0
        bonus = round(hot_bonus_base * bonus_coeff, 1)
        if bonus:
            score = min(score_cap, score + bonus)
            reasons.append(f"热门板块加分+{bonus:g}")
    if first_day and mode == "fenshi" and first_day_attack_penalty:
        score = max(0.0, score - first_day_attack_penalty)
        reasons.append(f"首日板块进攻型降权-{first_day_attack_penalty:g}")
        tags_out.append("首日板块")
    elif consecutive >= 3:
        tags_out.append(f"主线第{consecutive}天")

    zt = {"zt_count": 0, "max_lianban": 0, "leader_candidate": False}
    if leader_layer_enabled:
        zt = limit_up_stats(daily, code, lookback=zt_lookback, name=name)
        if zt["leader_candidate"] or zt["zt_count"] >= zt_min_count or zt["max_lianban"] >= lianban_min:
            score = min(score_cap, score + zt_bonus)
            reasons.append(
                f"龙头候选(近{zt_lookback}日涨停{zt['zt_count']}次/连板{zt['max_lianban']})+{zt_bonus:g}"
            )
            tags_out.append("龙头候选")
            zt["leader_candidate"] = True
        if in_board_top:
            score = min(score_cap, score + rank_bonus)
            reasons.append(f"板块内涨幅前排+{rank_bonus:g}")
            tags_out.append("板块前3")
        if is_ths_leader:
            score = min(score_cap, score + ths_bonus)
            reasons.append(f"板块领涨股+{ths_bonus:g}")
            tags_out.append("领涨股")

        no_leader = not zt["leader_candidate"] and not in_board_top and not is_ths_leader
        if no_leader and consecutive >= 2 and mode == "fenshi" and follower_penalty:
            score = max(0.0, score - follower_penalty)
            reasons.append(f"补涨杂毛降权-{follower_penalty:g}")
            tags_out.append("杂毛降权")

    return {
        "score": round(min(max(score, 0.0), score_cap), 1),
        "reasons": reasons,
        "tags": tags_out,
        "zt": zt,
        "life": life,
        "coeff": coeff,
        "confirmed_dip": confirmed_dip,
        "dip_confirm_bonus": dip_confirm_bonus,
        "trapped_enabled": trapped_enabled,
        "trapped_lookback": trapped_lookback,
        "trapped_ratio_warn": trapped_ratio_warn,
        "trapped_penalty": trapped_penalty,
        "score_cap": score_cap,
        "mode": mode,
        "pct": pct,
    }


def finalize_trapped_and_dip(
    adj: dict[str, Any],
    *,
    price: float,
    daily: pd.DataFrame | None,
) -> dict[str, Any]:
    """套牢盘需要现价；与打分主路径拆开避免签名过长。"""
    score = float(adj.get("score") or 0)
    reasons = list(adj.get("reasons") or [])
    tags = list(adj.get("tags") or [])
    cap = float(adj.get("score_cap") or 100)
    mode = str(adj.get("mode") or "fenshi")
    lookback = int(adj.get("trapped_lookback") or 60)
    trapped = None
    if adj.get("trapped_enabled") and daily is not None:
        trapped = trapped_share_ratio(daily, price, lookback=lookback)
    adj["trapped_ratio"] = trapped
    warn = float(adj.get("trapped_ratio_warn") or 0.40)
    penalty = float(adj.get("trapped_penalty") or 8.0)
    if trapped is not None and trapped > warn:
        if mode == "fenshi":
            score = max(0.0, score - penalty)
            reasons.append(f"上方套牢盘{trapped:.0%}(不幻想涨停，降权-{penalty:g})")
        else:
            reasons.append(f"上方套牢盘{trapped:.0%}(低吸保留，注意抛压)")
        tags.append("套牢盘重")

    if adj.get("confirmed_dip") and mode == "leader_dip":
        bonus = float(adj.get("dip_confirm_bonus") or 8.0)
        score = min(cap, score + bonus)
        reasons.append(f"企稳次日已确认+{bonus:g}")
        tags.append("企稳确认")

    adj["score"] = round(min(max(score, 0.0), cap), 1)
    adj["reasons"] = reasons
    adj["tags"] = tags
    return adj


def estimated_window_end(trade_date: str | None, days_remaining: int) -> str | None:
    if not trade_date or days_remaining < 0:
        return None
    try:
        d = datetime.fromisoformat(str(trade_date)[:10]).date()
    except Exception:
        return None
    # 粗略：跳过周末
    left = int(days_remaining)
    while left > 0:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d.isoformat()
