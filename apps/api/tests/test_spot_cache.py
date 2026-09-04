"""全市场快照缓存：空结果不得写入缓存。

回归背景（2026-09-04）：`get_spot_df_or_empty` 拿到空 DataFrame 时也会写进 45s TTL 缓存，
于是后续扫描直接命中「空表」→ error_code=no_quotes → 整轮 0 命中，
且日志上只表现为「无真实行情数据」，极难归因。空快照是数据源抖动/子进程被杀的结果，
属于故障态，必须允许下一次调用重新去源站取。
"""
from __future__ import annotations

import pandas as pd
import pytest

import providers.market as mkt


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例前清空缓存/单飞状态，并禁用 demo 模式。"""
    with mkt._cache_lock:
        mkt._cache.clear()
    with mkt._inflight_lock:
        mkt._inflight.clear()
    monkeypatch.setattr(mkt.settings, "demo_mode", False)
    yield
    with mkt._cache_lock:
        mkt._cache.clear()
    with mkt._inflight_lock:
        mkt._inflight.clear()


def test_empty_snapshot_is_not_cached(monkeypatch):
    """空快照不写缓存：下一次调用必须重新取源站（重试才有意义）。"""
    calls = {"n": 0}

    def fake_isolated(fn_name, *, timeout=0.0, args=(), kwargs=None):
        calls["n"] += 1
        return {"df": mkt.raw.empty_spot_df(), "source": "fake"}

    monkeypatch.setattr(mkt, "call_isolated", fake_isolated)

    df1 = mkt.get_spot_df_or_empty(use_isolated=True, ttl=45.0)
    assert getattr(df1, "empty", True) is True
    assert calls["n"] == 1

    # 第二次调用若命中缓存就不会再打源站；空结果必须重新取
    df2 = mkt.get_spot_df_or_empty(use_isolated=True, ttl=45.0)
    assert getattr(df2, "empty", True) is True
    assert calls["n"] == 2, "空快照不应进缓存，第二次必须重新打源站"
    assert mkt._cache_get("spot_df") is None


def test_non_empty_snapshot_is_cached(monkeypatch):
    """正常非空快照仍然走缓存，避免每轮扫描都重复抓全市场。"""
    calls = {"n": 0}

    def fake_isolated(fn_name, *, timeout=0.0, args=(), kwargs=None):
        calls["n"] += 1
        return {
            "df": pd.DataFrame({"code": ["600000", "000001"], "pct": [1.0, 2.0]}),
            "source": "fake",
        }

    monkeypatch.setattr(mkt, "call_isolated", fake_isolated)

    df1 = mkt.get_spot_df_or_empty(use_isolated=True, ttl=45.0)
    assert len(df1) == 2
    assert calls["n"] == 1

    df2 = mkt.get_spot_df_or_empty(use_isolated=True, ttl=45.0)
    assert len(df2) == 2
    assert calls["n"] == 1, "非空快照应命中缓存，不重复打源站"


def test_subprocess_exception_is_not_cached(monkeypatch):
    """子进程超时被杀（抛异常）同样不写缓存。"""
    calls = {"n": 0}

    def boom(fn_name, *, timeout=0.0, args=(), kwargs=None):
        calls["n"] += 1
        raise TimeoutError(f"{fn_name} timed out after {timeout}s (subprocess killed)")

    monkeypatch.setattr(mkt, "call_isolated", boom)

    df = mkt.get_spot_df_or_empty(use_isolated=True, ttl=45.0)
    assert getattr(df, "empty", True) is True
    assert mkt._cache_get("spot_df") is None
    assert calls["n"] == 1
