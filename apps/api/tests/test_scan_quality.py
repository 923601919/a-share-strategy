"""扫描质量自监控：每次扫描落一条结构化摘要，长期积累供调参/数据源健康对比。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["DEMO_MODE"] = "true"
os.environ["JWT_SECRET"] = ""

from db import init_db, list_scan_quality, save_scan_quality  # noqa: E402
from user_ctx import user_scope  # noqa: E402


def _fresh_db():
    """每个测试独立临时 DB，避免跨测试数据残留。"""
    from config import settings

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    settings.db_path = Path(path)
    init_db()
    return path


def test_save_and_list_scan_quality():
    _fresh_db()
    with user_scope(1):
        save_scan_quality(
            {
                "mode": "fenshi",
                "universe_policy": "soft",
                "candidates": 80,
                "scored": 30,
                "fenshi_ok": 22,
                "proxy_count": 8,
                "timed_out": 2,
                "total_ms": 15200.0,
                "market_env_level": "warn",
                "market_pct": -1.8,
                "spot_source": "sina",
                "strategy_version": "2026.09.02-macro",
                "top_avg_day_position": 0.72,
            }
        )
        rows = list_scan_quality(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["mode"] == "fenshi"
    assert r["proxy_count"] == 8
    assert r["market_env_level"] == "warn"
    assert r["top_avg_day_position"] == 0.72


def test_list_scan_quality_filters_by_mode():
    _fresh_db()
    with user_scope(1):
        save_scan_quality({"mode": "fenshi", "universe_policy": "hot_only"})
        save_scan_quality({"mode": "leader_dip", "universe_policy": "hot_only"})
        fenshi = list_scan_quality(limit=10, mode="fenshi")
        all_rows = list_scan_quality(limit=10)
    assert len(fenshi) == 1
    assert fenshi[0]["mode"] == "fenshi"
    assert len(all_rows) == 2


def test_scan_quality_optional_fields_nullable():
    _fresh_db()
    with user_scope(1):
        save_scan_quality({"mode": "fenshi", "universe_policy": "hot_only"})
        rows = list_scan_quality(limit=10)
    assert len(rows) == 1
    assert rows[0]["proxy_count"] is None
    assert rows[0]["top_avg_day_position"] is None
