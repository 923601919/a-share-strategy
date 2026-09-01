from services.sim import is_t1_sellable, t1_block_reason


def test_t1_same_day_blocked():
    assert is_t1_sellable(opened_at="2026-08-31T10:00:00+08:00", as_of="2026-08-31") is False
    reason = t1_block_reason(opened_at="2026-08-31T10:00:00+08:00", as_of="2026-08-31")
    assert reason and "T+1" in reason


def test_t1_next_day_ok():
    assert is_t1_sellable(opened_at="2026-08-30T14:00:00+08:00", as_of="2026-08-31") is True
    assert t1_block_reason(opened_at="2026-08-30T14:00:00+08:00", as_of="2026-08-31") is None
