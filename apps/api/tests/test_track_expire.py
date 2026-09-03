import services.track as track
from services.track import is_past_t3, _needs_t3_refresh, _t3_close_settled


def test_is_past_t3_after_window():
    returns = [
        {"day_offset": 0, "trade_date": "2026-08-20"},
        {"day_offset": 3, "trade_date": "2026-08-25"},
    ]
    assert is_past_t3(returns, today="2026-08-26") is True
    assert is_past_t3(returns, today="2026-08-25", now_hm="10:00") is False


def test_is_past_t3_on_t3_day_after_close():
    """T+3 当日 15:00 收盘后收盘价已落定，即可归档，无需等到下一日。"""
    returns = [
        {"day_offset": 0, "trade_date": "2026-08-20"},
        {"day_offset": 3, "trade_date": "2026-08-25"},
    ]
    assert is_past_t3(returns, today="2026-08-25", now_hm="09:30") is False
    assert is_past_t3(returns, today="2026-08-25", now_hm="14:59") is False
    assert is_past_t3(returns, today="2026-08-25", now_hm="15:00") is True
    assert is_past_t3(returns, today="2026-08-25", now_hm="15:01") is True
    assert is_past_t3(returns, today="2026-08-25", now_hm="1430") is False


def test_is_past_t3_next_day_regardless_of_time():
    """T+3 次日：全天可归档，不依赖时钟。"""
    returns = [
        {"day_offset": 0, "trade_date": "2026-08-20"},
        {"day_offset": 3, "trade_date": "2026-08-25"},
    ]
    assert is_past_t3(returns, today="2026-08-26", now_hm="09:00") is True
    assert is_past_t3(returns, today="2026-08-26") is True


def test_is_past_t3_without_t3():
    returns = [{"day_offset": 0, "trade_date": "2026-08-20"}]
    assert is_past_t3(returns, today="2026-08-30") is False


def test_needs_t3_refresh():
    entry = "2026-08-20"
    partial = [{"day_offset": 0, "trade_date": "2026-08-20"}]
    assert _needs_t3_refresh(entry, partial, today="2026-08-25") is True
    full = partial + [
        {"day_offset": 1, "trade_date": "2026-08-21"},
        {"day_offset": 2, "trade_date": "2026-08-22"},
        {"day_offset": 3, "trade_date": "2026-08-25"},
    ]
    assert _needs_t3_refresh(entry, full, today="2026-08-25", now_hm="10:00") is False


def test_needs_t3_refresh_t3_day_after_close_without_row(monkeypatch):
    """T+3 当日 15:00 后、库内尚无 T+3 行：仍应触发补全（收盘价可拉到）。"""
    monkeypatch.setattr(track, "trade_days_between", lambda start, end: 3)
    partial = [{"day_offset": 0, "trade_date": "2026-08-25"}]
    assert _needs_t3_refresh("2026-08-25", partial, today="2026-08-25", now_hm="15:30") is True
    # 15:00 前：当日日线未定，同样允许补全尝试（数据源可能已有盘中行，归档判定不受影响）
    assert _needs_t3_refresh("2026-08-25", partial, today="2026-08-25", now_hm="14:00") is True


def test_t3_close_settled_by_recorded_time():
    """T+3 收盘价可信性：落库时间须晚于其 trade_date 当日 15:00。"""
    settled = [
        {"day_offset": 0, "trade_date": "2026-09-01"},
        {"day_offset": 3, "trade_date": "2026-09-03", "recorded_at": "2026-09-03T15:05:00+08:00"},
    ]
    assert _t3_close_settled(settled) is True

    # 盘中 14:37 落库：可能拿到的是实时价而非最终收盘价
    stale = [
        {"day_offset": 0, "trade_date": "2026-09-01"},
        {"day_offset": 3, "trade_date": "2026-09-03", "recorded_at": "2026-09-03T14:37:21+08:00"},
    ]
    assert _t3_close_settled(stale) is False

    # 次日早晨落库：历史 K 线已定，可信
    next_morning = [
        {"day_offset": 0, "trade_date": "2026-09-01"},
        {"day_offset": 3, "trade_date": "2026-09-03", "recorded_at": "2026-09-04T09:10:00+08:00"},
    ]
    assert _t3_close_settled(next_morning) is True


def test_t3_close_settled_missing_or_bad_recorded_at():
    """无法判断时保守返回 False（交由强刷路径确认）。"""
    assert _t3_close_settled([{"day_offset": 3, "trade_date": "2026-09-03"}]) is False
    assert _t3_close_settled([{"day_offset": 3, "trade_date": "2026-09-03", "recorded_at": "garbage"}]) is False
    assert _t3_close_settled([{"day_offset": 3, "trade_date": "", "recorded_at": "2026-09-03T16:00:00+08:00"}]) is False
    assert _t3_close_settled([{"day_offset": 0, "trade_date": "2026-09-03"}]) is False
