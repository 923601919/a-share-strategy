"""轻量规则回归：不依赖外网行情。"""
from __future__ import annotations

import pandas as pd

from rules.fenshi import score_leader_dip, score_offensive_fenshi


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
