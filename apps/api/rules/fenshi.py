from __future__ import annotations

from typing import Any, Literal

import pandas as pd

SessionFilter = Literal["auto", "morning", "afternoon", "any"]


def _hm(now_hm: str | None = None) -> int:
    from datetime import datetime

    if now_hm is None:
        now = datetime.now()
        return now.hour * 100 + now.minute
    parts = now_hm.split(":")
    return int(parts[0]) * 100 + int(parts[1])


def in_session_bucket(now_hm: str | None = None) -> str:
    """返回 morning / afternoon / other。"""
    hm = _hm(now_hm)
    if 945 <= hm <= 1100:
        return "morning"
    if 1330 <= hm <= 1430:
        return "afternoon"
    return "other"


def in_attack_window(now_hm: str | None = None) -> bool:
    """核心买点窗口 10:00-10:40（进攻型分时常用）。"""
    hm = _hm(now_hm)
    return 1000 <= hm <= 1040


def session_allowed(session: SessionFilter, *, demo_mode: bool = False) -> tuple[bool, str]:
    """时段硬约束。auto 仅在重点窗口内扫描。"""
    if demo_mode or session == "any":
        return True, "演示/不限时段"

    bucket = in_session_bucket()
    if session == "morning":
        if bucket == "morning":
            extra = "·核心买点10:00-10:40" if in_attack_window() else ""
            return True, f"上午窗口(09:45-11:00){extra}"
        return False, "当前不在上午扫描窗口(09:45-11:00)"

    if session == "afternoon":
        if bucket == "afternoon":
            return True, "下午窗口(13:30-14:30)"
        return False, "当前不在下午扫描窗口(13:30-14:30)"

    # auto
    if bucket == "morning":
        if in_attack_window():
            return True, "自动·核心买点窗口(10:00-10:40)"
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
    last = float(df["close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if len(vwap) else last

    score = 0.0
    reasons: list[str] = []

    if pat["pullback"] and pat["reattack"]:
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

    if pat["above_vwap"]:
        score += 15
        reasons.append("现价站稳均价")
    else:
        reasons.append("现价未站稳均价")

    vb = float(pat["vol_breakout"])
    if vb >= 1.8:
        score += 25
        reasons.append(f"再攻放量{vb:.2f}x")
    elif vb >= 1.3:
        score += 12
        reasons.append(f"量能略增{vb:.2f}x")
    else:
        reasons.append(f"再攻量能偏弱({vb:.2f}x)")

    slope = float(pat["slope"])
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
        reasons.insert(0, "核心买点窗口(10:00-10:40)")

    return {
        "score": round(min(score, 100.0), 1),
        "above_vwap": pat["above_vwap"],
        "pullback": pat["pullback"],
        "reattack": pat["reattack"],
        "slope": round(slope, 3),
        "vol_expand": round(vb, 3),
        "vwap": round(last_vwap, 3),
        "last": round(last, 3),
        "reasons": reasons,
    }
