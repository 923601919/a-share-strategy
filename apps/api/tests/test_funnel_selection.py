"""选股漏斗增量：情绪温度计、板块生命周期、龙头分层、炸板/逐波、铁律、监管日历。"""
from __future__ import annotations

import pandas as pd

from rules.fenshi import detect_wave_volume, detect_zhaban
from rules.risk import anomaly_30d_pct, risk_flags
from rules.selection import (
    apply_selection_adjustments,
    board_rank_top_codes,
    finalize_trapped_and_dip,
    limit_up_stats,
    sector_lifecycle_map,
    trapped_share_ratio,
)
from rules.sentiment import classify_sentiment
from services.review import _build_orders_for_watch


def test_sentiment_ice_needs_full_triad():
    ice = classify_sentiment(zt_count=20, zhaban_rate=0.5, promotion_rate=0.1)
    assert ice["ice"] is True
    assert ice["phase"] == "ice"

    # 缺字段不误判冰点
    partial = classify_sentiment(zt_count=20)
    assert partial["ice"] is False


def test_sentiment_ice_by_down_count():
    out = classify_sentiment(n_down=4000, n_up=500)
    assert out["ice"] is True


def test_sentiment_euphoria():
    out = classify_sentiment(zt_count=90, max_lianban=6, promotion_rate=0.5, zhaban_rate=0.2)
    assert out["euphoria"] is True
    assert out["phase"] == "euphoria"


def test_sector_life_insufficient_history_is_neutral():
    life = sector_lifecycle_map(
        ["机器人"],
        [("2026-09-03", {"机器人"})],
        today="2026-09-03",
        min_history_dates=5,
    )
    assert life["机器人"]["coefficient"] == 1.0
    assert life["机器人"]["history_ready"] is False


def test_sector_life_first_day_and_persistent():
    hist = [
        ("2026-09-03", {"机器人"}),
        ("2026-09-02", {"机器人"}),
        ("2026-09-01", {"机器人"}),
        ("2026-08-31", {"AI眼镜"}),
        ("2026-08-30", {"煤炭"}),
    ]
    life = sector_lifecycle_map(["机器人", "一日游"], hist, today="2026-09-03", min_history_dates=5)
    assert life["机器人"]["coefficient"] == 1.2
    assert life["机器人"]["consecutive"] == 3
    assert life["一日游"]["first_day"] is True
    assert life["一日游"]["coefficient"] == 0.6


def test_limit_up_stats_leader_candidate():
    dates = pd.date_range("2026-08-01", periods=12, freq="B")
    close = [10.0]
    for _ in range(11):
        close.append(round(close[-1] * 1.101, 2))
    daily = pd.DataFrame({"date": dates, "close": close[-12:]})
    st = limit_up_stats(daily, "600000", lookback=10)
    assert st["zt_count"] >= 2
    assert st["leader_candidate"] is True


def test_board_rank_top3():
    tags = {
        "600001": ["电力"],
        "600002": ["电力"],
        "600003": ["电力"],
        "600004": ["电力"],
    }
    pct = {"600001": 5.0, "600002": 4.0, "600003": 3.0, "600004": 1.0}
    top = board_rank_top_codes(pct, tags, top_n=3)
    assert top == {"600001", "600002", "600003"}


def test_follower_penalty_on_persistent_board():
    adj = apply_selection_adjustments(
        score=60,
        reasons=[],
        code="600004",
        name="补涨杂毛",
        pct=4.0,
        daily=None,
        tags=["电力"],
        mode="fenshi",
        score_cap=100,
        life={"coefficient": 1.0, "first_day": False, "consecutive": 3, "name": "电力", "note": "连续3日"},
        in_board_top=False,
        is_ths_leader=False,
        confirmed_dip=False,
        hot_bonus_base=8.0,
        follower_penalty=10.0,
        first_day_attack_penalty=0.0,
        leader_layer_enabled=True,
        trapped_enabled=False,
    )
    assert any("杂毛" in r for r in adj["reasons"])
    assert adj["score"] == 58.0  # +8 热门 -10 杂毛


def test_trapped_share_ratio_above_price():
    daily = pd.DataFrame(
        {
            "close": [12, 12, 12, 8, 8],
            "high": [12.2, 12.2, 12.2, 8.2, 8.2],
            "low": [11.8, 11.8, 11.8, 7.8, 7.8],
            "amount": [1e8, 1e8, 1e8, 1e8, 1e8],
        }
    )
    ratio = trapped_share_ratio(daily, price=10.0)
    assert ratio is not None
    assert ratio > 0.4


def test_dip_confirm_bonus():
    adj = apply_selection_adjustments(
        score=40,
        reasons=[],
        code="600001",
        name="龙头",
        pct=-0.5,
        daily=None,
        tags=["电力"],
        mode="leader_dip",
        score_cap=100,
        life=None,
        in_board_top=False,
        is_ths_leader=False,
        confirmed_dip=True,
        hot_bonus_base=8.0,
        leader_layer_enabled=False,
        trapped_enabled=False,
        dip_confirm_bonus=8.0,
    )
    adj = finalize_trapped_and_dip(adj, price=10.0, daily=None)
    assert adj["score"] == 56.0  # +8 热门 +8 企稳
    assert "企稳确认" in adj["tags"]


def test_wave_volume_expanding():
    from rules.fenshi import _local_extrema

    vals = [1.0, 1.1, 1.2, 2.0, 1.3, 1.2, 1.1, 1.0, 1.2, 1.5, 2.4, 1.6, 1.4]
    peaks, troughs = _local_extrema(vals, order=2)
    assert peaks
    assert troughs

    closes = (
        [10 + i * 0.08 for i in range(8)]
        + [10.56 - i * 0.07 for i in range(7)]
        + [10.07 + i * 0.1 for i in range(10)]
        + [11.0] * 4
    )
    vols = [800] * 8 + [400] * 7 + [2000] * 10 + [900] * 4
    minute = pd.DataFrame({"close": closes, "volume": vols})
    wave = detect_wave_volume(minute)
    assert "healthy" in wave and "divergence" in wave
    assert wave["n_up"] >= 0


def test_zhaban_benign_reseal():
    pre = 10.0
    limit = round(pre * 1.1, 2)
    closes = [10.2] * 10 + [limit] * 6 + [limit - 0.15] * 4 + [limit] * 8
    highs = [c + 0.01 for c in closes]
    vols = [800] * 10 + [2000] * 6 + [700] * 4 + [1800] * 8
    minute = pd.DataFrame({"close": closes, "high": highs, "volume": vols})
    zb = detect_zhaban(minute, pre_close=pre, code="600000", now_hm="11:00")
    assert zb["touched"] is True
    assert zb["opened"] is True
    assert zb["resealed"] is True
    assert zb["quality"] == "benign"


def test_regulatory_watch_not_block():
    # 低点在 28 根之前，接近出窗口且近 5 日未创新高
    closes = [10.0] + [19.5] * 24 + [19.0] * 5
    daily = pd.DataFrame(
        {
            "date": pd.date_range("2026-07-20", periods=30, freq="B"),
            "close": closes,
            "open": closes,
        }
    )
    anom = anomaly_30d_pct(daily)
    assert anom["pct_from_low"] >= 80
    # 构造接近 200% 且临近出监管
    closes2 = [10.0] + [29.8] * 26 + [29.5] * 3
    daily2 = pd.DataFrame(
        {
            "date": pd.date_range("2026-07-20", periods=30, freq="B"),
            "close": closes2,
            "open": closes2,
        }
    )
    anom2 = anomaly_30d_pct(daily2)
    flags = risk_flags(
        anom2["pct_from_low"],
        warn=180,
        block=195,
        days_to_regulatory_exit=2,
        new_anomaly_recent=False,
        regulatory_window_end="2026-09-05",
        watch_days=3,
    )
    assert flags["level"] == "watch"
    blocked = risk_flags(196, warn=180, block=195, days_to_regulatory_exit=10, new_anomaly_recent=True)
    assert blocked["level"] == "block"


def test_iron_rule_orders_for_big_gain():
    orders = _build_orders_for_watch(
        {"code": "600000", "name": "测试", "entry_price": 10.0, "source": "fenshi"},
        {"price": 10.8, "pct": 8.0, "pre_close": 10.0, "fenshi": {}},
        {"ma5": 10.2, "pct_from_low": 40.0},
    )
    titles = [o["title"] for o in orders]
    assert any("铁律·竞价弱" in t for t in titles)
    assert any("卖一半" in t for t in titles)
    assert any("不封板清仓" in t for t in titles)
    assert all(o.get("playbook") == "limit_up_next_day" for o in orders if "铁律" in o["title"])
