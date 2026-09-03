"""扫描硬化回归：板块排除、日线缓存、策略参数化。不依赖外网。"""
from __future__ import annotations

import pandas as pd

from config import settings
from providers import market as mkt
from rules.fenshi import score_offensive_fenshi
from rules.params import StrategyParams
from rules.risk import is_excluded_board
from services.scan import _filter_spot


def _spot_df(codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [c.zfill(6) for c in codes],
            "name": [f"股{i}" for i in range(len(codes))],
            "amount": [3e8] * len(codes),
            "pct": [3.0] * len(codes),
        }
    )


# ---------- 板块排除 ----------

def test_is_excluded_board():
    assert is_excluded_board("688001") is True
    assert is_excluded_board("689009") is True
    assert is_excluded_board("830799") is True
    assert is_excluded_board("430047") is True
    assert is_excluded_board("920001") is True
    assert is_excluded_board("600000") is False
    assert is_excluded_board("000001") is False
    assert is_excluded_board("002212") is False
    assert is_excluded_board("300750") is False
    # 显式关闭则不排除
    assert is_excluded_board("688001", exclude_star=False) is False
    assert is_excluded_board("830799", exclude_bse=False) is False


def test_filter_spot_excludes_star_and_bse():
    codes = ["600000", "000001", "002212", "300750", "688001", "689009", "830799", "430047", "920001"]
    out = _filter_spot(_spot_df(codes), 1.0, 2.0, None, 50)
    kept = set(out["code"])
    assert kept == {"600000", "000001", "002212", "300750"}


def test_filter_spot_board_exclusion_toggleable(monkeypatch):
    monkeypatch.setattr(settings, "exclude_star_market", False)
    monkeypatch.setattr(settings, "exclude_bse", False)
    out = _filter_spot(_spot_df(["600000", "688001", "830799"]), 1.0, 2.0, None, 50)
    assert set(out["code"]) == {"600000", "688001", "830799"}


# ---------- 日线缓存 ----------

def _fake_daily(code: str, limit: int = 40) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-08-30"] * 3,
            "close": [10.0, 10.5, 11.0],
            "open": [9.9, 10.2, 10.8],
            "amount": [1e8] * 3,
            "volume": [1e5] * 3,
        }
    )


def test_daily_cache_hits(monkeypatch):
    calls = {"n": 0}

    def _counting(code: str, limit: int = 40):
        calls["n"] += 1
        return _fake_daily(code, limit)

    monkeypatch.setattr(mkt.raw, "fetch_daily", _counting)
    monkeypatch.setattr(settings, "daily_cache_enabled", True)
    monkeypatch.setattr(settings, "demo_mode", False)

    d1 = mkt.fetch_daily("601111", limit=40)
    d2 = mkt.fetch_daily("601111", limit=40)
    assert calls["n"] == 1
    pd.testing.assert_frame_equal(d1, d2)

    # 不同 limit 是不同 key，需重新拉取
    mkt.fetch_daily("601111", limit=30)
    assert calls["n"] == 2


def test_daily_cache_disabled(monkeypatch):
    calls = {"n": 0}

    def _counting(code: str, limit: int = 40):
        calls["n"] += 1
        return _fake_daily(code, limit)

    monkeypatch.setattr(mkt.raw, "fetch_daily", _counting)
    monkeypatch.setattr(settings, "daily_cache_enabled", False)
    monkeypatch.setattr(settings, "demo_mode", False)

    mkt.fetch_daily("601398", limit=40)
    mkt.fetch_daily("601398", limit=40)
    assert calls["n"] == 2


def test_daily_cache_ttl_after_close():
    """收盘后/周末 TTL 应覆盖到下一交易日开盘，而不是盘中短 TTL。"""
    from datetime import datetime as _dt

    # 周三 15:30 收盘后 → 次日（周四）09:15
    now = _dt(2026, 9, 2, 15, 30)
    assert mkt._seconds_until_next_open(now) == (_dt(2026, 9, 3, 9, 15) - now).total_seconds()
    # 周五 16:00 收盘后 → 下周一 09:15
    now = _dt(2026, 9, 4, 16, 0)
    assert mkt._seconds_until_next_open(now) == (_dt(2026, 9, 7, 9, 15) - now).total_seconds()
    # 周三 08:30 盘前 → 当日 09:15
    now = _dt(2026, 9, 2, 8, 30)
    assert mkt._seconds_until_next_open(now) == 45 * 60


# ---------- 策略参数化 ----------

def test_strategy_params_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "vol_hot", 2.5)
    p = StrategyParams.from_settings()
    assert p.vol_hot == 2.5
    # 未改动的字段保持默认
    assert p.score_pullback_reattack == 40.0


def test_params_change_scores():
    minute = pd.DataFrame(
        {
            "close": [10.0] * 20 + [9.95] * 8 + [10.05 + i * 0.01 for i in range(12)],
            "volume": [800] * 28 + [2500] * 12,
        }
    )
    base = score_offensive_fenshi(minute, p=StrategyParams())
    # 该样例斜率≈0.69% 落在温和档(+10)：清零斜率分后总分应下降
    no_slope = score_offensive_fenshi(
        minute, p=StrategyParams(slope_hot_score=0.0, slope_mild_score=0.0)
    )
    assert base["score"] > no_slope["score"]
    assert base["score"] - no_slope["score"] == 10.0


def test_default_params_match_legacy_behavior():
    """默认参数应与历史硬编码行为一致（关键阈值快照）。"""
    p = StrategyParams()
    assert p.fenshi_pullback_band == 0.008
    assert p.fenshi_tail_bars == 6
    assert p.vol_hot == 1.8 and p.vol_mild == 1.3
    assert p.slope_window == 8 and p.slope_hot == 1.0
    assert p.strong_push_min_slope == 0.8
    assert p.false_push_penalty == 25.0
    assert p.ld_ma5_near == 1.5 and p.ld_ma5_near_score == 32.0
    assert p.fenshi_score_cap == 100.0
