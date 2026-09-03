"""交易日历接入 track：长假下自然日近似会误判 T+3 补全时机。"""
from __future__ import annotations

import os
import tempfile

_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db
os.environ["DEMO_MODE"] = "true"
os.environ["JWT_SECRET"] = ""

import services.track as track
from services.track import _needs_t3_refresh


def test_needs_t3_refresh_uses_trade_days_not_calendar_days(monkeypatch):
    """长假：入池 5 个自然日但只有 1 个交易日 -> 不应触发 T+3 补全。"""
    # 模拟交易日历：10-01 入池，10-02..10-07 全是国庆假（只有 10-08 一个交易日）
    monkeypatch.setattr(track, "trade_days_between", lambda start, end: 1)
    partial = [{"day_offset": 0, "trade_date": "2026-10-01"}]
    assert _needs_t3_refresh("2026-10-01", partial, today="2026-10-06") is False


def test_needs_t3_refresh_after_enough_trade_days(monkeypatch):
    """过了 3 个交易日 -> 触发补全。"""
    monkeypatch.setattr(track, "trade_days_between", lambda start, end: 3)
    partial = [{"day_offset": 0, "trade_date": "2026-10-01"}]
    assert _needs_t3_refresh("2026-10-01", partial, today="2026-10-12") is True


def test_needs_t3_refresh_no_trade_days_yet(monkeypatch):
    """入池当天 / 次日：交易日数为 0 -> 不触发。"""
    monkeypatch.setattr(track, "trade_days_between", lambda start, end: 0)
    partial = [{"day_offset": 0, "trade_date": "2026-10-01"}]
    assert _needs_t3_refresh("2026-10-01", partial, today="2026-10-02") is False
