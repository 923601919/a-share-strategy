from __future__ import annotations

from typing import Any

import pandas as pd


def compute_vwap_series(minute: pd.DataFrame) -> pd.Series:
    if minute.empty:
        return pd.Series(dtype=float)
    df = minute.copy()
    if "amount" in df.columns and df["amount"].sum() > 0:
        # amount 元 / (volume手*100) ≈ 价；东财分钟成交额多为元、成交量为手
        vol_shares = df["volume"].clip(lower=0) * 100
        # 避免除零：用典型价近似
        typical = df["close"]
        cum_amt = (typical * vol_shares).cumsum()
        cum_vol = vol_shares.cumsum().replace(0, pd.NA)
        vwap = cum_amt / cum_vol
        return vwap.ffill().astype(float)
    # fallback: 累计均价近似
    return df["close"].expanding().mean()


def score_offensive_fenshi(minute: pd.DataFrame, lookback: int = 20) -> dict[str, Any]:
    """进攻型分时打分：站上均价、近段斜率、量能放大。"""
    if minute is None or len(minute) < 10 or "close" not in minute.columns:
        return {
            "score": 0.0,
            "above_vwap": False,
            "slope": 0.0,
            "vol_expand": 0.0,
            "reasons": ["分时数据不足"],
        }

    df = minute.dropna(subset=["close"]).reset_index(drop=True)
    vwap = compute_vwap_series(df)
    last = float(df["close"].iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if len(vwap) else last
    above = last >= last_vwap * 0.998

    lb = min(lookback, len(df) - 1)
    window = df["close"].iloc[-lb:]
    # 相对涨幅作为斜率代理
    slope = float((window.iloc[-1] / window.iloc[0] - 1.0) * 100) if window.iloc[0] else 0.0

    vol = df["volume"] if "volume" in df.columns else pd.Series([1.0] * len(df))
    recent = float(vol.iloc[-lb:].mean() or 0)
    prev = float(vol.iloc[max(0, -2 * lb) : -lb].mean() or 1)
    vol_expand = recent / prev if prev > 0 else 1.0

    score = 0.0
    reasons: list[str] = []
    if above:
        score += 35
        reasons.append("现价站上分时均价")
    else:
        reasons.append("现价未站稳均价")

    if slope >= 1.5:
        score += 35
        reasons.append(f"近{lb}分钟上攻约{slope:.2f}%")
    elif slope >= 0.5:
        score += 18
        reasons.append(f"近段温和上攻{slope:.2f}%")
    else:
        reasons.append(f"近段斜率偏弱{slope:.2f}%")

    if vol_expand >= 1.8:
        score += 30
        reasons.append(f"量能放大{vol_expand:.2f}x")
    elif vol_expand >= 1.2:
        score += 15
        reasons.append(f"量能略增{vol_expand:.2f}x")
    else:
        reasons.append(f"量能未明显放大({vol_expand:.2f}x)")

    return {
        "score": round(min(score, 100.0), 1),
        "above_vwap": above,
        "slope": round(slope, 3),
        "vol_expand": round(vol_expand, 3),
        "vwap": round(last_vwap, 3),
        "last": round(last, 3),
        "reasons": reasons,
    }


def in_session_bucket(now_hm: str | None = None) -> str:
    """返回 morning / afternoon / other。"""
    from datetime import datetime

    if now_hm is None:
        now = datetime.now()
        hm = now.hour * 100 + now.minute
    else:
        parts = now_hm.split(":")
        hm = int(parts[0]) * 100 + int(parts[1])
    if 945 <= hm <= 1100:
        return "morning"
    if 1330 <= hm <= 1430:
        return "afternoon"
    return "other"
