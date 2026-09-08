"""优化软加权：拉升量能 vs 开盘量能（无外网）。"""
from __future__ import annotations

import pandas as pd

from rules.fenshi import apply_rally_open_volume, detect_rally_vs_open_volume


def _minute(open_vol: float, rally_vol: float, *, rally_pct: float = 2.0) -> pd.DataFrame:
    """前 10 根为开盘横盘（open_vol），后 30 根拉升（rally_vol）。"""
    closes = [10.0] * 10 + [10.0 * (1 + rally_pct / 100 * (i / 29)) for i in range(30)]
    vols = [open_vol] * 10 + [rally_vol] * 30
    return pd.DataFrame(
        {
            "close": closes,
            "volume": vols,
            "amount": [c * v * 100 for c, v in zip(closes, vols)],
        }
    )


def test_rally_amplified_volume_passes():
    """拉升段均量 > 开盘量能 -> ok=True 且 ratio 达标。"""
    out = detect_rally_vs_open_volume(
        _minute(open_vol=500, rally_vol=1500),
        open_window=5,
        vol_mode="avg",
        min_ratio=1.2,
        rally_min_pct=1.0,
    )
    assert out["ok"] is True
    assert out["ratio"] is not None and out["ratio"] >= 1.2
    assert out["rally_bars"] >= 1


def test_rally_weak_volume_fails():
    """拉升段均量 < 开盘量能 -> ok=False。"""
    out = detect_rally_vs_open_volume(
        _minute(open_vol=500, rally_vol=400),
        open_window=5,
        vol_mode="avg",
        min_ratio=1.2,
        rally_min_pct=1.0,
    )
    assert out["ok"] is False
    assert out["ratio"] is not None and out["ratio"] < 1.2


def test_no_rally_detected():
    """横盘无拉升 -> 未识别到拉升段，ok=False。"""
    minute = pd.DataFrame({"close": [10.0] * 40, "volume": [500] * 40})
    out = detect_rally_vs_open_volume(
        minute,
        open_window=5,
        vol_mode="avg",
        min_ratio=1.2,
        rally_min_pct=1.0,
    )
    assert out["ok"] is False
    assert "未识别到拉升段" in out["message"]


def test_peak_mode_uses_max_volume():
    """峰值口径：只要拉升段出现一根巨量即达标（即便均量偏低）。"""
    closes = [10.0] * 10 + [10.0 * (1 + 0.02 * (i / 29)) for i in range(30)]
    vols = [500] * 10 + [100] * 29 + [5000]  # 拉升段均量低，但末尾一根巨量
    minute = pd.DataFrame({"close": closes, "volume": vols})
    out = detect_rally_vs_open_volume(
        minute, open_window=5, vol_mode="peak", min_ratio=1.2, rally_min_pct=1.0
    )
    assert out["ok"] is True
    assert out["ratio"] is not None and out["ratio"] >= 1.2


def test_apply_hard_filter_removes_non_pass():
    rally = detect_rally_vs_open_volume(
        _minute(open_vol=500, rally_vol=400),
        open_window=5, vol_mode="avg", min_ratio=1.2, rally_min_pct=1.0,
    )
    fenshi = {"score": 50.0, "reasons": []}
    out, filtered = apply_rally_open_volume(fenshi, rally, hard_filter=True)
    assert filtered is True
    assert out is None


def test_apply_soft_filter_keeps_and_penalizes():
    rally = detect_rally_vs_open_volume(
        _minute(open_vol=500, rally_vol=400),
        open_window=5, vol_mode="avg", min_ratio=1.2, rally_min_pct=1.0,
    )
    fenshi = {"score": 50.0, "reasons": []}
    out, filtered = apply_rally_open_volume(
        fenshi, rally, hard_filter=False, penalty=15.0, bonus=6.0
    )
    assert filtered is False
    assert out["score"] == 35.0
    assert out["rally_open"]["ok"] is False


def test_apply_bonus_on_pass():
    rally = detect_rally_vs_open_volume(
        _minute(open_vol=500, rally_vol=1500),
        open_window=5, vol_mode="avg", min_ratio=1.2, rally_min_pct=1.0,
    )
    fenshi = {"score": 50.0, "reasons": []}
    out, filtered = apply_rally_open_volume(
        fenshi, rally, hard_filter=True, bonus=6.0
    )
    assert filtered is False
    assert out["score"] == 56.0
    assert out["rally_open"]["ok"] is True
    assert any("量能" in r for r in out["reasons"])
