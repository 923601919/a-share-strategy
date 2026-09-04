"""A+B+C 修复的聚焦单元测试（不依赖网络/真实 DB）。

验证：
- compute_short_term_returns: 入池日 K 线缺失返回空（不再伪造 T+0=0）；no_cache 参数透传。
- _merge_track_returns: 并集保护已有偏移（新值优先，保留旧 T+2/T+3）。
"""
import sys
import types
import pandas as pd

# 让 services.track 在导入时不必连接真实 DB/config 的副作用最小化：
# 直接导入模块，config/db/providers 已在 .venv 内可解析。
import services.track as track_mod

# ---- 构造假日线 ----
def _fake_daily(prices_by_date):
    rows = [{"date": d, "close": p, "open": p, "high": p, "low": p, "volume": 1} for d, p in prices_by_date]
    return pd.DataFrame(rows)


def test_compute_returns_normal():
    df = _fake_daily([
        ("2026-09-01", 10.0),
        ("2026-09-02", 11.0),
        ("2026-09-03", 9.9),
        ("2026-09-04", 12.0),
    ])
    # 记录 no_cache 是否真的传到 fetch_daily
    seen = {}
    def _fake_fetch(code, limit=40, *, no_cache=False):
        seen["no_cache"] = no_cache
        return df
    track_mod.mkt.fetch_daily = _fake_fetch

    rows = track_mod.compute_short_term_returns(
        code="000001", entry_price=10.0, entry_date="2026-09-01", max_days=3, no_cache=True
    )
    assert seen.get("no_cache") is True, "no_cache 未透传到 fetch_daily"
    offsets = [r["day_offset"] for r in rows]
    assert offsets == [0, 1, 2, 3], f"应算全 T0..T3，实际 {offsets}"
    # T+1: 11/10-1 = 10%
    assert abs(rows[1]["return_pct"] - 10.0) < 1e-6, rows[1]
    print("[OK] compute_returns_normal: T0..T3 正确，no_cache 透传")


def test_compute_returns_entry_missing_returns_empty():
    # 日线里没有 >= entry_date 的 bar（入池当日 K 线未入库）
    df = _fake_daily([("2026-08-30", 10.0), ("2026-08-31", 10.5)])
    track_mod.mkt.fetch_daily = lambda code, limit=40, *, no_cache=False: df

    rows = track_mod.compute_short_term_returns(
        code="000001", entry_price=10.0, entry_date="2026-09-01", max_days=3, no_cache=True
    )
    assert rows == [], f"入池日缺失应返回空，实际 {rows}"
    print("[OK] compute_returns_entry_missing: 返回空（不再伪造 T+0=0）")


def test_merge_protects_existing():
    existing = [
        {"day_offset": 0, "trade_date": "2026-09-01", "close_price": 10.0, "return_pct": 0.0},
        {"day_offset": 1, "trade_date": "2026-09-02", "close_price": 11.0, "return_pct": 10.0},
        {"day_offset": 2, "trade_date": "2026-09-03", "close_price": 9.9, "return_pct": -1.0},
        {"day_offset": 3, "trade_date": "2026-09-04", "close_price": 12.0, "return_pct": 20.0},
    ]
    # 模拟一次「算不全」的刷新：网络/缓存滞后只拿到 T0/T1（且 T0 是更旧的占位）
    new = [
        {"day_offset": 0, "trade_date": "2026-09-01", "close_price": 10.0, "return_pct": 0.0},
        {"day_offset": 1, "trade_date": "2026-09-02", "close_price": 11.0, "return_pct": 10.0},
    ]
    merged = track_mod._merge_track_returns(existing, new)
    offsets = [r["day_offset"] for r in merged]
    assert offsets == [0, 1, 2, 3], f"并集应保留 T2/T3，实际 {offsets}"
    # 旧的 T2/T3 必须保留（未被残缺刷新冲掉）
    by_off = {r["day_offset"]: r for r in merged}
    assert by_off[2]["return_pct"] == -1.0, "T2 被错误覆盖"
    assert by_off[3]["return_pct"] == 20.0, "T3 被错误覆盖"
    print("[OK] merge_protects_existing: 残缺刷新的并集保护住了 T2/T3")


def test_merge_full_recompute_overrides():
    existing = [
        {"day_offset": 0, "trade_date": "2026-09-01", "close_price": 10.0, "return_pct": 0.0},
        {"day_offset": 3, "trade_date": "2026-09-04", "close_price": 12.0, "return_pct": 20.0, "recorded_at": "stale"},
    ]
    new = [
        {"day_offset": 0, "trade_date": "2026-09-01", "close_price": 10.0, "return_pct": 0.0},
        {"day_offset": 1, "trade_date": "2026-09-02", "close_price": 11.0, "return_pct": 10.0},
        {"day_offset": 2, "trade_date": "2026-09-03", "close_price": 9.9, "return_pct": -1.0},
        {"day_offset": 3, "trade_date": "2026-09-04", "close_price": 12.5, "return_pct": 25.0},
    ]
    merged = track_mod._merge_track_returns(existing, new)
    by_off = {r["day_offset"]: r for r in merged}
    assert by_off[3]["return_pct"] == 25.0, "完整重算的 T3 应覆盖旧值"
    assert by_off[1]["return_pct"] == 10.0
    print("[OK] merge_full_recompute_overrides: 完整重算覆盖旧值")


if __name__ == "__main__":
    test_compute_returns_normal()
    test_compute_returns_entry_missing_returns_empty()
    test_merge_protects_existing()
    test_merge_full_recompute_overrides()
    print("\nALL TESTS PASSED")
