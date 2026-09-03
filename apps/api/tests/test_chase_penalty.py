"""追高惩罚：拉升中的票不该因为量能/斜率自然拉满而占据榜首。"""
from __future__ import annotations

import os
import tempfile

_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db
os.environ["DEMO_MODE"] = "true"
os.environ["JWT_SECRET"] = ""

import pandas as pd

from rules.fenshi import apply_chase_penalty
from rules.params import StrategyParams


def _base(score: float = 90.0) -> dict:
    return {"score": score, "reasons": ["强势推升(站稳均价逼近高位)"], "proxy": False}


def test_no_penalty_when_position_low():
    """买在日内中低位 -> 不惩罚。"""
    out = apply_chase_penalty(
        _base(), price=10.0, day_high=11.0, day_low=9.0, vwap=10.0, p=StrategyParams()
    )
    assert out["chase_penalty"] == 0.0
    assert out["score"] == 90.0
    assert out["day_position"] == 0.5  # (10-9)/(11-9)


def test_penalty_at_day_high():
    """贴在日内最高点 -> 位置惩罚。"""
    # vwap=10.8 使乖离仅 +1.67%（< 2.5），只触发位置惩罚
    out = apply_chase_penalty(
        _base(), price=10.98, day_high=11.0, day_low=10.0, vwap=10.8, p=StrategyParams()
    )
    assert out["day_position"] >= 0.90
    assert out["vwap_deviation"] < StrategyParams().chase_dev_high
    assert out["chase_penalty"] == StrategyParams().chase_pos_penalty
    assert out["score"] == 90.0 - StrategyParams().chase_pos_penalty
    assert any("日内高位" in r for r in out["reasons"])


def test_penalty_for_large_vwap_deviation():
    """现价远离均价 -> 乖离惩罚。"""
    out = apply_chase_penalty(
        _base(), price=10.5, day_high=11.0, day_low=10.0, vwap=10.0, p=StrategyParams()
    )
    # 乖离 +5% > 2.5
    assert out["vwap_deviation"] == 5.0
    assert out["chase_penalty"] == StrategyParams().chase_dev_penalty
    assert any("乖离" in r for r in out["reasons"])


def test_both_penalties_stack():
    """同时逼近日内高位且乖离过大 -> 两项叠加。"""
    p = StrategyParams()
    out = apply_chase_penalty(
        _base(), price=10.98, day_high=11.0, day_low=10.0, vwap=10.0, p=p
    )
    assert out["chase_penalty"] == p.chase_pos_penalty + p.chase_dev_penalty
    assert out["score"] == 90.0 - p.chase_pos_penalty - p.chase_dev_penalty


def test_score_floors_at_zero():
    """惩罚不会把分数打成负数。"""
    out = apply_chase_penalty(
        {"score": 5.0, "reasons": []},
        price=10.98,
        day_high=11.0,
        day_low=10.0,
        vwap=10.0,
        p=StrategyParams(),
    )
    assert out["score"] == 0.0


def test_missing_data_is_safe():
    """缺 high/low/vwap 时不报错、不惩罚。"""
    out = apply_chase_penalty(_base(), price=10.0, day_high=0, day_low=0, vwap=None)
    assert out["chase_penalty"] == 0.0
    assert out["score"] == 90.0
    assert "day_position" not in out
    assert "vwap_deviation" not in out


def test_thresholds_are_configurable():
    """阈值可通过参数调整。"""
    p = StrategyParams(chase_pos_high=0.5, chase_pos_penalty=30.0)
    out = apply_chase_penalty(
        _base(), price=10.6, day_high=11.0, day_low=10.0, vwap=10.6, p=p
    )
    # 位置 0.6 >= 0.5（默认 0.9 时不会触发）
    assert out["chase_penalty"] == 30.0
    default_out = apply_chase_penalty(
        _base(), price=10.6, day_high=11.0, day_low=10.0, vwap=10.6, p=StrategyParams()
    )
    assert default_out["chase_penalty"] == 0.0


def test_pullback_entry_not_penalized():
    """典型回踩买点（位置 0.6、贴近均价）不受惩罚。"""
    out = apply_chase_penalty(
        _base(score=77.0),
        price=10.6,
        day_high=11.0,
        day_low=10.0,
        vwap=10.55,
        p=StrategyParams(),
    )
    assert out["chase_penalty"] == 0.0
    assert out["score"] == 77.0
