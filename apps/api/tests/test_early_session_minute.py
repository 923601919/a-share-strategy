"""早盘分时数据偏短时，不应被误判为代理分。"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import patch

import pandas as pd

# 必须在 import app 模块前设置环境。设临时 db + demo mode 避免污染用户数据。
_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db
os.environ["DEMO_MODE"] = "true"
os.environ["JWT_SECRET"] = ""

# 注意：不要直接给 settings.demo_mode 赋值！这是全局单例，会污染后续测试。
# 仅在模块顶层 import services.scan，不触碰 settings。

import services.scan as scan  # noqa: E402
from config import settings  # noqa: E402  仅用于读取默认值（如 minute_min_rows_early）


def _at(h: int, m: int) -> datetime:
    return datetime(2026, 9, 2, h, m, 0)


def test_minute_threshold_uses_early_window():
    assert scan._minute_threshold(_at(9, 35)) == settings.minute_min_rows_early
    assert scan._minute_threshold(_at(10, 5)) == settings.minute_min_rows
    # 边界：开盘 0 分钟和 30 分整
    assert scan._minute_threshold(_at(9, 30)) == settings.minute_min_rows_early
    # 30 分整超出早盘窗口（条件 0 <= m < 30）
    assert scan._minute_threshold(_at(10, 0)) == settings.minute_min_rows
    assert scan._minute_threshold(_at(10, 1)) == settings.minute_min_rows


def test_minute_usable_13rows_at_9_42():
    """9:42 时 13 根分时（开盘 12 分钟），早盘阈值 8 根，应判为可用。"""
    df = pd.DataFrame({"close": [10.0] * 13, "volume": [1000] * 13})
    assert scan._is_minute_usable(df, now=_at(9, 42)) is True


def test_minute_usable_3rows_at_9_33():
    """9:33 时 3 根分时（开盘 3 分钟），早盘阈值 8 根，应判为不可用。"""
    df = pd.DataFrame({"close": [10.0] * 3, "volume": [1000] * 3})
    assert scan._is_minute_usable(df, now=_at(9, 33)) is False


def test_short_minute_reason_distinguishes():
    """行数不足 vs 拉取失败，提示语应不同。"""
    df = pd.DataFrame({"close": [10.0] * 3, "volume": [1000] * 3})
    r1 = scan._short_minute_reason(df, None, now=_at(9, 33))
    assert "偏短" in r1
    assert "早盘" in r1

    r2 = scan._short_minute_reason(df, RuntimeError("proxy"), now=_at(9, 33))
    assert "失败" in r2
    assert "proxy" in r2


def test_enrich_short_minute_does_not_proxy():
    """开盘 12 分钟时 13 根分时，_enrich_one 不应走代理打分。"""
    row = {
        "code": "601668",
        "name": "测试股",
        "pct": 3.0,
        "price": 10.0,
        "amount": 5e8,
        "volume_ratio": 2.0,
        "open": 9.9,
        "high": 10.2,
        "low": 9.8,
    }
    df13 = pd.DataFrame({"close": [10.0] * 13, "volume": [1000] * 13})

    with patch.object(scan.mkt, "fetch_minute", return_value=df13), patch.object(
        scan.mkt, "fetch_daily", return_value=None
    ), patch.object(scan, "datetime") as fake_dt:
        fake_dt.now.return_value = _at(9, 42)
        out = scan._enrich_one(row, {}, mode="fenshi")

    assert out is not None
    assert out.get("fenshi", {}).get("proxy") is False
    # reasons 在 result 顶层（fenshi dict 不含 reasons 字段）
    reasons = out.get("reasons") or []
    assert not any("偏短" in r for r in reasons), reasons
    assert not any("分时源切换" in r for r in reasons), reasons
