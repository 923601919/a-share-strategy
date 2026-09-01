from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import pandas as pd

SessionFilter = Literal["auto", "morning", "afternoon", "any"]
DayVolLevel = Literal["block", "warn", "healthy", "ok", "skip"]


def _hm(now_hm: str | None = None) -> int:
    if now_hm is None:
        now = datetime.now()
        return now.hour * 100 + now.minute
    parts = now_hm.split(":")
    return int(parts[0]) * 100 + int(parts[1])


def _session_elapsed_minutes(hm: int) -> int:
    """已过交易分钟数（上午 120 + 下午 120 = 240）。非交易时段夹到边界。"""
    if hm < 930:
        return 0
    if hm <= 1130:
        return (hm // 100 - 9) * 60 + (hm % 100) - 30
    if hm < 1300:
        return 120
    if hm <= 1500:
        return 120 + (hm // 100 - 13) * 60 + (hm % 100)
    return 240


def _metric_sum(df: pd.DataFrame) -> tuple[float, str]:
    """优先成交额，否则成交量。返回 (值, 字段名)。"""
    if df is None or df.empty:
        return 0.0, "amount"
    if "amount" in df.columns:
        amt = float(pd.to_numeric(df["amount"], errors="coerce").fillna(0).sum())
        if amt > 0:
            return amt, "amount"
    if "volume" in df.columns:
        vol = float(pd.to_numeric(df["volume"], errors="coerce").fillna(0).sum())
        return vol, "volume"
    return 0.0, "amount"


def _prev_session_metric(daily: pd.DataFrame, field: str) -> float:
    """上一完整交易日的 amount/volume。末行若为今日则用前一行。"""
    if daily is None or daily.empty or field not in daily.columns:
        return 0.0
    df = daily.dropna(subset=[field]).copy()
    if df.empty:
        return 0.0
    idx = -1
    if "date" in df.columns:
        last_dt = pd.to_datetime(df["date"].iloc[-1], errors="coerce")
        if pd.notna(last_dt) and last_dt.date() == datetime.now().date() and len(df) >= 2:
            idx = -2
    val = float(pd.to_numeric(df[field].iloc[idx], errors="coerce") or 0)
    return val if val > 0 else 0.0


def day_volume_health(
    minute: pd.DataFrame | None,
    daily: pd.DataFrame | None,
    *,
    now_hm: str | None = None,
    block_by_1000: float = 1.0,
    block_by_1130: float = 2.0,
    warn_by_1130: float = 1.2,
) -> dict[str, Any]:
    """
    今累计成交 / 昨全日（优先成交额）。
    对齐作者细节：早盘超昨全日、午盘约 2× 视为日内出货。
    """
    empty = {
        "level": "skip",
        "ratio": None,
        "metric": "amount",
        "today_cum": 0.0,
        "prev_day": 0.0,
        "message": "日量数据不足",
    }
    if minute is None or minute.empty or daily is None or daily.empty:
        return empty

    today_cum, metric = _metric_sum(minute)
    prev = _prev_session_metric(daily, metric)
    if metric == "amount" and prev <= 0 and "volume" in daily.columns and "volume" in minute.columns:
        # 日线无额时回退量比
        today_cum = float(pd.to_numeric(minute["volume"], errors="coerce").fillna(0).sum())
        metric = "volume"
        prev = _prev_session_metric(daily, "volume")

    if today_cum <= 0 or prev <= 0:
        return {**empty, "today_cum": today_cum, "prev_day": prev, "metric": metric}

    ratio = today_cum / prev
    hm = _hm(now_hm)
    level: DayVolLevel = "ok"
    message = f"今/昨{metric}比 {ratio:.2f}x"

    if hm <= 1000 and ratio >= block_by_1000:
        level = "block"
        message = f"早盘今/昨{metric}已达 {ratio:.2f}x（≥{block_by_1000:g}），疑似日内出货"
    elif hm <= 1130 and ratio >= block_by_1130:
        level = "block"
        message = f"午前今/昨{metric}已达 {ratio:.2f}x（≥{block_by_1130:g}），疑似日内出货"
    elif hm <= 1130 and ratio >= warn_by_1130:
        level = "warn"
        message = f"午前今/昨{metric}偏快 {ratio:.2f}x（≥{warn_by_1130:g}）"
    else:
        elapsed = max(_session_elapsed_minutes(hm), 1)
        expected_frac = elapsed / 240.0
        pace = ratio / expected_frac if expected_frac > 0 else ratio
        if 0.4 <= pace <= 1.5:
            level = "healthy"
            message = f"日量健康(轻微放量) 今/昨{metric} {ratio:.2f}x · 进度归一 {pace:.2f}"
        else:
            message = f"今/昨{metric}比 {ratio:.2f}x · 进度归一 {pace:.2f}"

    return {
        "level": level,
        "ratio": round(ratio, 3),
        "metric": metric,
        "today_cum": round(today_cum, 2),
        "prev_day": round(prev, 2),
        "message": message,
    }


def detect_false_push(
    minute: pd.DataFrame | None,
    *,
    day_vol_ratio: float | None = None,
    day_vol_level: str | None = None,
    lookback: int = 30,
) -> dict[str, Any]:
    """冲高后跌破均价 + 日量偏大 → 假进攻。"""
    out = {"false_push": False, "near_high_then_below": False}
    if minute is None or len(minute) < 15 or "close" not in minute.columns:
        return out

    df = minute.dropna(subset=["close"]).reset_index(drop=True)
    vwap = compute_vwap_series(df)
    n = len(df)
    lb = min(lookback, n)
    c = df["close"].iloc[-lb:].astype(float)
    last = float(c.iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if len(vwap) else last
    window_high = float(c.max())
    touched_high = (c >= window_high * 0.98).any()
    below_vwap = last < last_vwap * 0.998
    near_high_then_below = bool(touched_high and below_vwap)

    vol_hot = bool(
        day_vol_level in ("warn", "block")
        or (day_vol_ratio is not None and day_vol_ratio >= 1.0)
    )
    false_push = bool(near_high_then_below and vol_hot)
    return {
        "false_push": false_push,
        "near_high_then_below": near_high_then_below,
        "below_vwap": below_vwap,
    }


def apply_day_vol_and_false_push(
    fenshi: dict[str, Any],
    day_vol: dict[str, Any],
    false_push: dict[str, Any],
) -> dict[str, Any]:
    """对进攻型打分结果施加日量健康加分 / warn 与假推升降权（block 由调用方剔除）。"""
    out = dict(fenshi)
    reasons = list(out.get("reasons") or [])
    score = float(out.get("score") or 0)
    level = str(day_vol.get("level") or "skip")
    ratio = day_vol.get("ratio")

    out["day_vol_ratio"] = ratio
    out["day_vol_level"] = level

    if level == "healthy":
        score = min(100.0, score + 5)
        reasons.append(str(day_vol.get("message") or "日量健康(轻微放量)"))
    elif level == "warn":
        score = max(0.0, score - 15)
        reasons.append(str(day_vol.get("message") or "日量偏快(降权)"))

    if false_push.get("false_push"):
        out["strong_push"] = False
        out["pullback"] = False
        out["reattack"] = False
        score = max(0.0, score - 25)
        reasons.append("冲高跌破均价·疑似假进攻")

    out["score"] = round(min(score, 100.0), 1)
    out["reasons"] = reasons
    out["false_push"] = bool(false_push.get("false_push"))
    return out


def in_session_bucket(now_hm: str | None = None) -> str:
    """返回 morning / afternoon / other。"""
    hm = _hm(now_hm)
    if 945 <= hm <= 1100:
        return "morning"
    if 1330 <= hm <= 1430:
        return "afternoon"
    return "other"


def in_attack_window(now_hm: str | None = None) -> bool:
    """核心买点窗口 10:15-10:40（作者 CDJS 案例区间）。"""
    hm = _hm(now_hm)
    return 1015 <= hm <= 1040


def session_allowed(session: SessionFilter, *, demo_mode: bool = False) -> tuple[bool, str]:
    """时段硬约束。auto 仅在重点窗口内扫描。"""
    if demo_mode or session == "any":
        return True, "演示/不限时段"

    bucket = in_session_bucket()
    if session == "morning":
        if bucket == "morning":
            extra = "·核心买点10:15-10:40" if in_attack_window() else ""
            return True, f"上午窗口(09:45-11:00){extra}"
        return False, "当前不在上午扫描窗口(09:45-11:00)"

    if session == "afternoon":
        if bucket == "afternoon":
            return True, "下午窗口(13:30-14:30)"
        return False, "当前不在下午扫描窗口(13:30-14:30)"

    # auto
    if bucket == "morning":
        if in_attack_window():
            return True, "自动·核心买点窗口(10:15-10:40)"
        return True, "自动·上午重点窗口(09:45-11:00)"
    if bucket == "afternoon":
        return True, "自动·下午重点窗口(13:30-14:30)"
    return False, "自动模式：非重点时段(09:45-11:00 / 13:30-14:30)，已跳过扫描"


def compute_vwap_series(minute: pd.DataFrame) -> pd.Series:
    if minute.empty:
        return pd.Series(dtype=float)
    df = minute.copy()
    if "amount" in df.columns and df["amount"].sum() > 0:
        vol_shares = df["volume"].clip(lower=0) * 100
        typical = df["close"]
        cum_amt = (typical * vol_shares).cumsum()
        cum_vol = vol_shares.cumsum().replace(0, pd.NA)
        vwap = cum_amt / cum_vol
        return vwap.ffill().astype(float)
    return df["close"].expanding().mean()


def _detect_pullback_reattack(
    closes: pd.Series,
    vwap: pd.Series,
    volume: pd.Series,
    *,
    lookback: int = 30,
) -> dict[str, Any]:
    """
    回踩均价后再攻：
    1) 前段贴近/略破均价（回踩）
    2) 末段重新站上均价并上行
    3) 末段放量
    """
    n = len(closes)
    lb = min(lookback, n - 1)
    if lb < 12:
        return {
            "pullback": False,
            "reattack": False,
            "vol_breakout": 1.0,
            "slope": 0.0,
            "above_vwap": False,
        }

    c = closes.iloc[-lb:].astype(float)
    v = vwap.iloc[-lb:].astype(float)
    vol = volume.iloc[-lb:].astype(float).fillna(0)

    above_now = float(c.iloc[-1]) >= float(v.iloc[-1]) * 0.998

    # 回踩区：除最后 6 根外的前段
    hist_c = c.iloc[:-6]
    hist_v = v.iloc[:-6]
    if len(hist_c) < 5:
        pullback = False
    else:
        ratio = hist_c / hist_v.replace(0, pd.NA)
        # 贴近均价（±0.8%）或短暂跌破
        near = ((ratio >= 0.992) & (ratio <= 1.008)).any()
        below = (ratio < 0.998).any()
        pullback = bool(near or below)

    # 再攻：末 6 根整体抬升且站上均价
    tail = c.iloc[-6:]
    tail_v = v.iloc[-6:]
    reattack = bool(
        above_now
        and float(tail.iloc[-1]) > float(tail.iloc[0])
        and (tail >= tail_v * 0.998).sum() >= 4
    )

    recent_vol = float(vol.iloc[-5:].mean() or 0)
    prior_vol = float(vol.iloc[-15:-5].mean() or 0) if len(vol) >= 15 else float(vol.iloc[:-5].mean() or 1)
    vol_breakout = recent_vol / prior_vol if prior_vol > 0 else 1.0

    slope = float((c.iloc[-1] / c.iloc[-8] - 1.0) * 100) if float(c.iloc[-8]) else 0.0

    return {
        "pullback": pullback,
        "reattack": reattack,
        "vol_breakout": vol_breakout,
        "slope": slope,
        "above_vwap": above_now,
    }


def _detect_strong_push(
    closes: pd.Series,
    vwap: pd.Series,
    volume: pd.Series,
    *,
    lookback: int = 30,
) -> dict[str, Any]:
    """强势推升：全天站稳均价 + 斜率大 + 放量 + 逼近日内高位（作者新集能源类）。"""
    n = len(closes)
    lb = min(lookback, n - 1)
    if lb < 12:
        return {
            "strong_push": False,
            "slope": 0.0,
            "vol_breakout": 1.0,
            "above_vwap": False,
            "near_high": False,
        }

    c = closes.iloc[-lb:].astype(float)
    v = vwap.iloc[-lb:].astype(float)
    vol = volume.iloc[-lb:].astype(float).fillna(0)

    above_ratio = float((c >= v * 0.998).sum()) / max(len(c), 1)
    slope = float((c.iloc[-1] / c.iloc[-8] - 1.0) * 100) if float(c.iloc[-8]) else 0.0
    recent_vol = float(vol.iloc[-5:].mean() or 0)
    prior_vol = float(vol.iloc[-15:-5].mean() or 0) if len(vol) >= 15 else float(vol.iloc[:-5].mean() or 1)
    vol_breakout = recent_vol / prior_vol if prior_vol > 0 else 1.0
    near_high = float(c.iloc[-1]) >= float(c.max()) * 0.98
    above_now = float(c.iloc[-1]) >= float(v.iloc[-1]) * 0.998

    strong_push = bool(
        above_ratio >= 0.75 and slope >= 0.8 and vol_breakout >= 1.3 and near_high and above_now
    )

    return {
        "strong_push": strong_push,
        "slope": slope,
        "vol_breakout": vol_breakout,
        "above_vwap": above_now,
        "near_high": near_high,
        "above_ratio": round(above_ratio, 3),
    }


def score_leader_dip(
    minute: pd.DataFrame | None,
    *,
    price: float,
    pct: float,
    ma5: float | None,
    open_price: float,
    lookback: int = 30,
) -> dict[str, Any]:
    """龙头低吸：水下/平盘附近 + 贴近 MA5 + 分时企稳。"""
    score = 0.0
    reasons: list[str] = []

    if -2.0 <= pct <= 0.5:
        score += 28
        reasons.append(f"水下/平盘企稳({pct:.2f}%)")
    elif pct <= 1.5:
        score += 18
        reasons.append(f"涨幅温和({pct:.2f}%)")
    else:
        reasons.append(f"涨幅偏高({pct:.2f}%)")

    if ma5 and ma5 > 0 and price > 0:
        dist = abs(price / ma5 - 1.0) * 100
        if dist <= 1.5:
            score += 32
            reasons.append(f"贴近MA5({dist:.2f}%)")
        elif dist <= 3.0:
            score += 18
            reasons.append(f"接近MA5({dist:.2f}%)")
        else:
            reasons.append(f"偏离MA5({dist:.2f}%)")
    else:
        reasons.append("MA5不可用")

    if open_price > 0 and price >= open_price * 0.995:
        score += 12
        reasons.append("现价不低于开盘")
    elif open_price > 0 and price >= open_price * 0.98:
        score += 6
        reasons.append("小幅低开回升")

    strong_push = False
    above_vwap = None
    slope = None
    vol_expand = None
    vwap_val = None

    if minute is not None and len(minute) >= 15 and "close" in minute.columns:
        df = minute.dropna(subset=["close"]).reset_index(drop=True)
        vwap = compute_vwap_series(df)
        vol = df["volume"] if "volume" in df.columns else pd.Series([1.0] * len(df))
        push = _detect_strong_push(df["close"], vwap, vol, lookback=lookback)
        pat = _detect_pullback_reattack(df["close"], vwap, vol, lookback=lookback)
        last = float(df["close"].iloc[-1])
        last_vwap = float(vwap.iloc[-1]) if len(vwap) else last
        vwap_val = round(last_vwap, 3)
        above_vwap = bool(last >= last_vwap * 0.998)
        slope = round(float(max(pat["slope"], push["slope"])), 3)
        vol_expand = round(float(max(pat["vol_breakout"], push["vol_breakout"])), 3)
        strong_push = bool(push["strong_push"])

        if above_vwap:
            score += 15
            reasons.append("分时站稳均价")
        elif last >= last_vwap * 0.992:
            score += 8
            reasons.append("分时贴近均价")
        else:
            reasons.append("分时仍偏弱")

        tail = df["close"].iloc[-8:].astype(float)
        if len(tail) >= 6 and float(tail.iloc[-1]) >= float(tail.iloc[0]):
            score += 10
            reasons.append("近段止跌回升")
    else:
        reasons.append("分时数据不足，仅盘口评估")

    if in_attack_window():
        score = min(100.0, score + 5)
        reasons.insert(0, "核心买点窗口(10:15-10:40)")

    return {
        "score": round(min(score, 100.0), 1),
        "above_vwap": above_vwap,
        "pullback": None,
        "reattack": None,
        "strong_push": strong_push,
        "slope": slope,
        "vol_expand": vol_expand,
        "vwap": vwap_val,
        "last": round(price, 3),
        "reasons": reasons,
        "proxy": minute is None or len(minute) < 15,
    }


def score_offensive_fenshi(minute: pd.DataFrame, lookback: int = 30) -> dict[str, Any]:
    """进攻型分时：回踩均价 + 放量再攻（B）。"""
    if minute is None or len(minute) < 15 or "close" not in minute.columns:
        return {
            "score": 0.0,
            "above_vwap": False,
            "pullback": False,
            "reattack": False,
            "slope": 0.0,
            "vol_expand": 0.0,
            "reasons": ["分时数据不足"],
        }

    df = minute.dropna(subset=["close"]).reset_index(drop=True)
    vwap = compute_vwap_series(df)
    vol = df["volume"] if "volume" in df.columns else pd.Series([1.0] * len(df))

    pat = _detect_pullback_reattack(df["close"], vwap, vol, lookback=lookback)
    push = _detect_strong_push(df["close"], vwap, vol, lookback=lookback)
    last = float(df["close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if len(vwap) else last

    score = 0.0
    reasons: list[str] = []

    if push["strong_push"]:
        score += 38
        reasons.append("强势推升(站稳均价逼近高位)")
    elif pat["pullback"] and pat["reattack"]:
        score += 40
        reasons.append("回踩均价后重新上攻")
    elif pat["pullback"]:
        score += 18
        reasons.append("出现回踩均价")
    elif pat["reattack"]:
        score += 22
        reasons.append("站上均价上攻")
    else:
        reasons.append("未形成回踩再攻形态")

    if pat["above_vwap"] or push["above_vwap"]:
        score += 15
        reasons.append("现价站稳均价")
    else:
        reasons.append("现价未站稳均价")

    vb = float(max(pat["vol_breakout"], push["vol_breakout"]))
    if vb >= 1.8:
        score += 25
        reasons.append(f"再攻放量{vb:.2f}x")
    elif vb >= 1.3:
        score += 12
        reasons.append(f"量能略增{vb:.2f}x")
    else:
        reasons.append(f"再攻量能偏弱({vb:.2f}x)")

    slope = float(max(pat["slope"], push["slope"]))
    if slope >= 1.0:
        score += 20
        reasons.append(f"再攻斜率{slope:.2f}%")
    elif slope >= 0.4:
        score += 10
        reasons.append(f"再攻温和{slope:.2f}%")
    else:
        reasons.append(f"再攻斜率偏弱{slope:.2f}%")

    if in_attack_window():
        score = min(100.0, score + 5)
        reasons.insert(0, "核心买点窗口(10:15-10:40)")

    return {
        "score": round(min(score, 100.0), 1),
        "above_vwap": pat["above_vwap"] or push["above_vwap"],
        "pullback": pat["pullback"],
        "reattack": pat["reattack"],
        "strong_push": push["strong_push"],
        "slope": round(slope, 3),
        "vol_expand": round(vb, 3),
        "vwap": round(last_vwap, 3),
        "last": round(last, 3),
        "reasons": reasons,
    }
