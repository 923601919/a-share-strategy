"""大盘环境闸门：指数暴跌日整体降级/观望，逆势拉升分时不该照常推荐。"""
from __future__ import annotations

import os
import tempfile

_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db
os.environ["DEMO_MODE"] = "true"
os.environ["JWT_SECRET"] = ""

from services import scan
from services.scan import _market_env


def _patch_index(pct):
    """monkeypatch mkt.fetch_index_snapshot 返回指定涨跌幅。"""
    scan.mkt.fetch_index_snapshot = lambda codes=None: {
        "sh000001": {"name": "上证指数", "pct": pct, "price": 3000.0}
    }
    return scan


def _reset():
    scan.settings.demo_mode = False
    scan.settings.market_env_enabled = True


def test_normal_market():
    _reset()
    _patch_index(1.2)
    env = _market_env()
    assert env["level"] == "normal"
    assert env["pct"] == 1.2


def test_warn_market():
    _reset()
    _patch_index(-1.8)  # 低于 warn_pct(-1.5)，高于 block_pct(-2.5)
    env = _market_env()
    assert env["level"] == "warn"


def test_block_market():
    _reset()
    _patch_index(-3.0)  # 低于 block_pct(-2.5)
    env = _market_env()
    assert env["level"] == "block"


def test_index_unavailable_falls_back_to_normal():
    _reset()
    scan.mkt.fetch_index_snapshot = lambda codes=None: {}
    env = _market_env()
    assert env["level"] == "normal"
    assert env["pct"] is None


def test_index_fetch_exception_falls_back_to_normal():
    _reset()

    def _boom(codes=None):
        raise RuntimeError("network")

    scan.mkt.fetch_index_snapshot = _boom
    env = _market_env()
    assert env["level"] == "normal"


def test_disabled_returns_normal():
    _reset()
    scan.settings.market_env_enabled = False
    scan.settings.demo_mode = False
    env = _market_env()
    assert env["level"] == "normal"
