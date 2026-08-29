from services.track import is_past_t3, _needs_t3_refresh


def test_is_past_t3_after_window():
    returns = [
        {"day_offset": 0, "trade_date": "2026-08-20"},
        {"day_offset": 3, "trade_date": "2026-08-25"},
    ]
    assert is_past_t3(returns, today="2026-08-26") is True
    assert is_past_t3(returns, today="2026-08-25") is False


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
    assert _needs_t3_refresh(entry, full, today="2026-08-25") is False
