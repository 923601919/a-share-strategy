"""候选池策略：配额 / 软加权（无外网）。"""
from __future__ import annotations

import pandas as pd

from services.scan import _apply_quota_top, _build_candidate_rows


def _spot_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_apply_quota_reserves_non_hot_slots():
    results = [
        {"code": f"60000{i}", "score": 90 - i, "pct": 3.0, "in_hot_board": True}
        for i in range(10)
    ] + [
        {"code": f"00000{i}", "score": 80 - i, "pct": 2.5, "in_hot_board": False}
        for i in range(5)
    ]
    out = _apply_quota_top(results, top_n=10, satellite_pct=0.25)
    assert len(out) == 10
    cold = [x for x in out if not x["in_hot_board"]]
    assert len(cold) == 3  # round(10*0.25)=3
    assert all(not x["in_hot_board"] for x in cold)


def test_build_soft_ignores_universe_filter():
    spot = _spot_df(
        [
            {"code": "600001", "name": "热门A", "pct": 3.0, "amount": 2e8, "volume_ratio": 1.5},
            {"code": "000001", "name": "冷门B", "pct": 3.2, "amount": 2e8, "volume_ratio": 1.2},
            {"code": "300001", "name": "冷门C", "pct": 2.5, "amount": 1.5e8, "volume_ratio": 1.1},
        ]
    )
    rows, note = _build_candidate_rows(
        spot,
        universe_policy="soft",
        universe_codes={"600001"},
        min_amount_yi=1.0,
        min_pct=2.0,
        max_pct=6.0,
        max_pct_inclusive=False,
    )
    codes = {str(r["code"]).zfill(6) for r in rows}
    assert "600001" in codes
    assert "000001" in codes
    assert "软加权" in note


def test_build_quota_includes_satellite():
    spot = _spot_df(
        [
            {"code": "600001", "name": "热门A", "pct": 4.0, "amount": 3e8, "volume_ratio": 2.0},
            {"code": "600002", "name": "热门B", "pct": 3.5, "amount": 2.5e8, "volume_ratio": 1.8},
            {"code": "000010", "name": "卫星X", "pct": 3.0, "amount": 2e8, "volume_ratio": 1.4},
            {"code": "000011", "name": "卫星Y", "pct": 2.8, "amount": 1.8e8, "volume_ratio": 1.3},
        ]
    )
    rows, note = _build_candidate_rows(
        spot,
        universe_policy="quota",
        universe_codes={"600001", "600002"},
        min_amount_yi=1.0,
        min_pct=2.0,
        max_pct=6.0,
        max_pct_inclusive=False,
    )
    codes = {str(r["code"]).zfill(6) for r in rows}
    assert "600001" in codes
    assert "000010" in codes or "000011" in codes
    assert "配额" in note
