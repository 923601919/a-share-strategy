"""折分前候选分落库：消融回测的前置设施。

被追高惩罚挡在 top-N 外的票在旧口径里没有任何痕迹（scan_snapshots 只存入选后的
items），于是「不罚它们会怎样」永远问不出来。这组用例钉住三件事：
落库内容完整、落选者与被剔除者都在、以及这条私有通路不会漏进接口响应。
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["JWT_SECRET"] = ""

from config import settings  # noqa: E402
from db import (  # noqa: E402
    get_db,
    init_db,
    list_scan_candidates,
    save_scan_candidates,
)
from services.scan import _candidate_row, _reject_row  # noqa: E402
from user_ctx import user_scope  # noqa: E402


def _fresh_db(monkeypatch) -> Path:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(settings, "db_path", Path(path))
    init_db()
    return Path(path)


def _row(**kw):
    base = {
        "mode": "fenshi",
        "universe_policy": "soft",
        "strategy_version": "test",
        "spot_source": "sina",
        "market_env_level": "normal",
        "top_n": 10,
        "code": "600001",
        "name": "样本A",
        "pct": 4.2,
        "amount": 3e8,
        "score": 71.0,
        "score_pre_chase": 86.0,
        "chase_penalty": 15.0,
        "day_position": 0.93,
        "vwap_deviation": 2.7,
        "selected": True,
    }
    base.update(kw)
    return base


# ---------- 落库 / 读取 ----------

def test_save_and_list_roundtrip(monkeypatch):
    _fresh_db(monkeypatch)
    with user_scope(1):
        assert save_scan_candidates([_row()]) == 1
        rows = list_scan_candidates(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "600001"
    assert r["mode"] == "fenshi"
    assert r["universe_policy"] == "soft"
    # 消融的对照口径必须原样存活：折分前分、扣分、以及据以触发惩罚的日内位置
    assert r["score"] == 71.0
    assert r["score_pre_chase"] == 86.0
    assert r["chase_penalty"] == 15.0
    assert r["day_position"] == 0.93
    assert r["vwap_deviation"] == 2.7
    assert r["selected"] == 1
    assert r["reject_reason"] is None


def test_selected_flag_defaults_false(monkeypatch):
    """落选者要被记下来，且必须能与入选者区分。"""
    _fresh_db(monkeypatch)
    with user_scope(1):
        save_scan_candidates(
            [
                _row(code="600001", selected=True, score=90.0),
                _row(code="600002", selected=False, score=70.0),
                _row(code="600003", selected=False, score=69.0, chase_penalty=0.0),
            ]
        )
        rows = list_scan_candidates(limit=10)
    picked = [r["code"] for r in rows if r["selected"]]
    assert picked == ["600001"]
    assert {r["code"] for r in rows} == {"600001", "600002", "600003"}


def test_nullable_fields_stay_null(monkeypatch):
    """追高惩罚没跑（代理分/非进攻型）时，折分前后不存在——留 NULL 而不是编一个等值。"""
    _fresh_db(monkeypatch)
    with user_scope(1):
        save_scan_candidates([_row(score_pre_chase=None, chase_penalty=None, day_position=None)])
        rows = list_scan_candidates(limit=10)
    assert rows[0]["score_pre_chase"] is None
    assert rows[0]["day_position"] is None
    assert rows[0]["score"] == 71.0


def test_empty_input_is_noop(monkeypatch):
    _fresh_db(monkeypatch)
    with user_scope(1):
        assert save_scan_candidates([]) == 0
        assert list_scan_candidates(limit=10) == []


# ---------- 用户隔离 ----------

def test_candidates_are_user_scoped(monkeypatch):
    _fresh_db(monkeypatch)
    with user_scope(1):
        save_scan_candidates([_row(code="600001")])
    with user_scope(2):
        assert list_scan_candidates(limit=10) == []
        save_scan_candidates([_row(code="000002")])
        mine = list_scan_candidates(limit=10)
    assert [r["code"] for r in mine] == ["000002"]
    with user_scope(1):
        assert [r["code"] for r in list_scan_candidates(limit=10)] == ["600001"]


# ---------- 保留窗口 ----------

def test_retention_drops_only_expired_rows(monkeypatch):
    _fresh_db(monkeypatch)
    monkeypatch.setattr(settings, "scan_candidate_keep_days", 30)
    now = datetime.now(timezone.utc).astimezone()
    with user_scope(1):
        save_scan_candidates([_row(code="600001"), _row(code="600002"), _row(code="600003")])
        with get_db() as conn:
            conn.execute(
                "UPDATE scan_candidates SET created_at=? WHERE code IN ('600001','600002')",
                ((now - timedelta(days=90)).isoformat(),),
            )
            conn.execute(
                "UPDATE scan_candidates SET created_at=? WHERE code='600003'",
                ((now - timedelta(days=1)).isoformat(),),
            )
        # 下一次扫描触发裁剪
        save_scan_candidates([_row(code="600004")])
        rows = list_scan_candidates(limit=10)
    assert {r["code"] for r in rows} == {"600003", "600004"}


def test_retention_does_not_touch_other_users(monkeypatch):
    _fresh_db(monkeypatch)
    monkeypatch.setattr(settings, "scan_candidate_keep_days", 1)
    old = (datetime.now(timezone.utc).astimezone() - timedelta(days=90)).isoformat()
    with user_scope(2):
        save_scan_candidates([_row(code="000002")])
        with get_db() as conn:
            conn.execute("UPDATE scan_candidates SET created_at=?", (old,))
    with user_scope(1):
        save_scan_candidates([_row(code="600001")])
    with user_scope(2):
        assert [r["code"] for r in list_scan_candidates(limit=10)] == ["000002"]


# ---------- 投影函数 ----------

def test_reject_row_marks_reason_and_carries_identity():
    row = {"code": "600812", "name": "华北制药", "pct": 5.2, "amount": 8.5e8}
    out = _reject_row(row, "day_vol_block")
    assert out["code"] == "600812"
    assert out["reject_reason"] == "day_vol_block"
    assert out["_rejected"] == "day_vol_block"
    # 被剔除的票没走到打分，不该有分
    assert "score" not in out


def test_reject_row_tolerates_missing_fields():
    out = _reject_row({}, "enrich_timeout")
    assert out["code"] == "000000"
    assert out["name"] == ""
    assert out["pct"] is None
    assert out["amount"] is None


def test_candidate_row_reads_pre_chase_and_position():
    fenshi = {
        "score": 62.0,
        "chase_penalty": 12.0,
        "day_position": 0.81,
        "vwap_deviation": -0.4,
    }
    out = _candidate_row({"code": "600001", "name": "A", "pct": 3.0, "amount": 2e8}, fenshi, 74.0)
    assert out["score"] == 62.0
    assert out["score_pre_chase"] == 74.0
    assert out["chase_penalty"] == 12.0
    assert out["day_position"] == 0.81
    assert out["vwap_deviation"] == -0.4
    # 折分前后要自洽：最终分 = 折分前分 - 扣分
    assert out["score_pre_chase"] - out["chase_penalty"] == out["score"]


def test_candidate_row_without_penalty_step():
    """惩罚步骤没跑时折分前分留 None，不与最终分混为一谈。"""
    out = _candidate_row({"code": "600001"}, {"score": 55.0}, None)
    assert out["score_pre_chase"] is None
    assert out["chase_penalty"] is None


# ---------- 端到端：真跑一轮扫描 ----------

def test_run_scan_persists_candidates_and_hides_private_key(monkeypatch):
    _fresh_db(monkeypatch)
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "scan_candidate_log", True)

    from services.scan import run_scan

    with user_scope(1):
        payload = run_scan(session="any", mode="fenshi", universe_policy="soft")
        rows = list_scan_candidates(limit=200)

    assert payload["error_code"] is None
    assert payload["count"] > 0, payload["session_note"]

    # 私有键不得漏进接口响应（前端拿到的载荷不该因为内部要做消融而变形）
    for item in payload["items"]:
        assert "_cand" not in item
        assert "_rejected" not in item

    # 每个候选都留下了痕迹，且入选数与 payload 一致
    logged = {r["code"] for r in rows}
    assert logged, "候选表为空：消融的对照面没落下来"
    for item in payload["items"]:
        assert item["code"] in logged
    assert sum(1 for r in rows if r["selected"]) == payload["count"]

    # 至少有一条走完追高惩罚链路（demo 分时足够长 → 非代理分 → 会施加惩罚）
    assert any(r["score_pre_chase"] is not None for r in rows)
    for r in rows:
        assert r["mode"] == "fenshi"
        assert r["universe_policy"] == "soft"
        assert r["top_n"] == payload["params"]["top_n"]
        assert r["strategy_version"] == payload["params"]["strategy_version"]


def test_run_scan_candidate_log_can_be_disabled(monkeypatch):
    _fresh_db(monkeypatch)
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "scan_candidate_log", False)

    from services.scan import run_scan

    with user_scope(1):
        payload = run_scan(session="any", mode="fenshi", universe_policy="soft")
        rows = list_scan_candidates(limit=200)
    assert payload["count"] > 0
    assert rows == []
