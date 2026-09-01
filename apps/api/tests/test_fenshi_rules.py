"""轻量规则回归：不依赖外网行情。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from rules.fenshi import (
    apply_day_vol_and_false_push,
    day_volume_health,
    detect_false_push,
    score_leader_dip,
    score_offensive_fenshi,
)


def _sample_minute(*, push: bool = False) -> pd.DataFrame:
    n = 40
    if push:
        closes = [10 + i * 0.02 for i in range(n)]
        vols = [1000] * (n - 5) + [3000] * 5
    else:
        closes = [10.0] * 20 + [9.95] * 8 + [10.05 + i * 0.01 for i in range(12)]
        vols = [800] * 28 + [2500] * 12
    return pd.DataFrame(
        {
            "time": [f"09:{30 + i // 60:02d}:{i % 60:02d}" for i in range(n)],
            "close": closes,
            "volume": vols,
            "amount": [c * v * 100 for c, v in zip(closes, vols)],
        }
    )


def _daily_prev(*, amount: float = 1e8, volume: float = 1e5) -> pd.DataFrame:
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return pd.DataFrame(
        {
            "date": [yesterday],
            "close": [10.0],
            "open": [9.8],
            "amount": [amount],
            "volume": [volume],
        }
    )


def test_offensive_fenshi_scores():
    out = score_offensive_fenshi(_sample_minute(push=False))
    assert out["score"] > 0
    assert "reasons" in out


def test_strong_push_path():
    out = score_offensive_fenshi(_sample_minute(push=True))
    assert out["score"] > 0


def test_leader_dip_near_ma5():
    out = score_leader_dip(
        _sample_minute(),
        price=10.0,
        pct=-0.5,
        ma5=10.05,
        open_price=10.1,
    )
    assert out["score"] >= 20


def test_day_vol_block_by_1000():
    # 今累计额 = 昨全日 → 10:00 前 block
    prev_amt = 1e8
    minute = pd.DataFrame(
        {
            "close": [10.0] * 20,
            "volume": [1000] * 20,
            "amount": [prev_amt / 20] * 20,
        }
    )
    daily = _daily_prev(amount=prev_amt)
    out = day_volume_health(minute, daily, now_hm="09:50")
    assert out["level"] == "block"
    assert out["ratio"] is not None and out["ratio"] >= 1.0


def test_day_vol_block_by_1130_two_x():
    prev_amt = 1e8
    minute = pd.DataFrame(
        {
            "close": [10.0] * 30,
            "volume": [1000] * 30,
            "amount": [prev_amt * 2.05 / 30] * 30,
        }
    )
    daily = _daily_prev(amount=prev_amt)
    out = day_volume_health(minute, daily, now_hm="11:20")
    assert out["level"] == "block"
    assert out["ratio"] is not None and out["ratio"] >= 2.0


def test_day_vol_healthy_mild():
    # 11:30 时约半日进度，今量 ≈ 0.45×昨 → 进度归一约 0.9 → healthy
    prev_amt = 1e8
    minute = pd.DataFrame(
        {
            "close": [10.0] * 40,
            "volume": [500] * 40,
            "amount": [prev_amt * 0.45 / 40] * 40,
        }
    )
    daily = _daily_prev(amount=prev_amt)
    out = day_volume_health(minute, daily, now_hm="11:30")
    assert out["level"] == "healthy"
    assert out["ratio"] is not None


def test_false_push_clears_confirmed_flags():
    # 冲高后跌破均价 + 日量比 ≥1
    n = 40
    closes = [10.0 + i * 0.05 for i in range(25)] + [10.8 - i * 0.04 for i in range(15)]
    vols = [2000] * n
    minute = pd.DataFrame(
        {
            "close": closes,
            "volume": vols,
            "amount": [c * v * 100 for c, v in zip(closes, vols)],
        }
    )
    scored = score_offensive_fenshi(minute)
    day_vol = {"level": "warn", "ratio": 1.5, "message": "午前偏快"}
    fp = detect_false_push(minute, day_vol_ratio=1.5, day_vol_level="warn")
    assert fp["false_push"] is True
    applied = apply_day_vol_and_false_push(scored, day_vol, fp)
    assert applied["strong_push"] is False
    assert applied["pullback"] is False
    assert applied["reattack"] is False
    assert applied["false_push"] is True
    assert any("假进攻" in r for r in applied["reasons"])
