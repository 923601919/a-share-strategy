"""服务层与落库：冻结约束 / 结算幂等 / 待结算 / 漏跑留痕。

这个文件测的是「出错时不会报错」的另一半——**数据层**的那些：

  · 预测记录写入即冻结：SQLite 触发器必须真的拒绝 UPDATE/DELETE；
  · 结算幂等：作业重跑不会重复结算、也不会把已有结果算成新的；
  · 拿不到行情 → 留在**待结算**，不是"未命中"；
  · 漏跑必须留下缺口，而不是被悄悄补成一条看起来正常的记录；
  · replay 记录不进准确率。

⚠ 本文件全程用临时 DB，且**打桩掉所有网络调用**（行情同步与实时行情）。
   打桩不是为了让测试好过，而是为了让"哪一步真的碰了网络"这件事显式化：
   若哪天有人在结算里偷偷加了一次取行情，这里的桩不会拦住他，
   但 `run_daily_forecast` 的步骤日志会多出一段没有对应桩的输出。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

# 必须在 import db / config 之前设好：Settings 在导入期就实例化了
_fd, _DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _DB
os.environ["DEMO_MODE"] = "true"
os.environ["JWT_SECRET"] = ""
os.environ["SCHEDULER_ENABLED"] = "false"

import sqlite3  # noqa: E402

import db as dbmod  # noqa: E402
from db import (  # noqa: E402
    get_db,
    get_market_forecast_outcome,
    get_market_prediction,
    init_db,
    insert_market_prediction,
    list_market_forecast_runs,
    list_market_predictions,
    list_pending_settlement,
    upsert_market_index_daily,
)
from forecast import service as svc  # noqa: E402

SYMBOL = "000001.SH"


def setup_function():
    init_db()
    with get_db() as conn:
        # 预测表有 append-only 触发器，DELETE 会被拒 → 用 DROP TRIGGER 临时放行。
        # 这一步本身就是对触发器的反向验证：不摘掉它们，连测试都清不干净表。
        conn.execute("DROP TRIGGER IF EXISTS market_predictions_no_update")
        conn.execute("DROP TRIGGER IF EXISTS market_predictions_no_delete")
        for t in (
            "market_predictions",
            "market_index_daily",
            "market_forecast_outcomes",
            "market_forecast_runs",
        ):
            conn.execute(f"DELETE FROM {t}")
    # 触发器是 init_db() 里 CREATE TRIGGER IF NOT EXISTS 建的，再跑一次即可装回
    init_db()


def _bars(pairs):
    """pairs: [(date, close), ...] 升序。prev_close 由上一根推出（指数无除权）。"""
    rows = []
    prev = None
    for d, c in pairs:
        rows.append({"date": d, "close": c, "prev_close": prev, "source": "test"})
        prev = c
    upsert_market_index_daily(SYMBOL, rows)


def _prediction(target_date: str, origin: str = "live", direction: str = "up", as_of="2026-01-01T15:00:00+08:00"):
    """直接落一条最小可用的预测记录（不走引擎，专测数据层）。"""
    rec = {
        "id": f"{origin}-{target_date}-{direction}",
        "symbol": SYMBOL,
        "target_date": target_date,
        "as_of": as_of,
        "generated_at": "2026-01-01T17:00:00+08:00",
        "origin": origin,
        "direction": direction,
        "strength": 0.2,
        "basis_json": "{}",
        "profile_hash": "deadbeefdeadbeef",
        "engine_ver": "0.1.2",
        "ruleset_ver": "0.1.0",
        "created_at": "2026-01-01T17:00:00+08:00",
    }
    rec["content_hash"] = svc._content_hash(rec)
    insert_market_prediction(rec)
    return rec["id"]


@pytest.fixture
def no_network(monkeypatch):
    """把作业里两处会打行情网的地方打桩掉。"""
    monkeypatch.setattr(svc, "_realtime_judgement", lambda *a, **k: None)
    monkeypatch.setattr(
        svc, "sync_index_daily", lambda *a, **k: {"written": 0, "sources": [], "last_date": None}
    )


# ─────────────────────── ① 冻结（append-only） ───────────────────────


def test_update_is_rejected_by_trigger():
    """预测记录一旦写入即冻结：UPDATE 必须被 SQLite 拒绝。

    这不是洁癖。预测记录一旦可改，准确率就成了可以事后修补的东西——
    而"修补过的历史"和"真实历史"在界面上长得一模一样。
    """
    pid = _prediction("2026-03-02")
    with pytest.raises(sqlite3.IntegrityError) as e:
        with get_db() as conn:
            conn.execute("UPDATE market_predictions SET direction='down' WHERE id=?", (pid,))
    assert "append-only" in str(e.value)


def test_delete_is_rejected_by_trigger():
    pid = _prediction("2026-03-02")
    with pytest.raises(sqlite3.IntegrityError) as e:
        with get_db() as conn:
            conn.execute("DELETE FROM market_predictions WHERE id=?", (pid,))
    assert "append-only" in str(e.value)


def test_content_hash_detects_tampering():
    """摘掉触发器后手改字段 → content_hash 必须对不上。

    触发器挡得住 UPDATE/DELETE，挡不住绕过 SQLite 直接改文件。
    content_hash 就是为那种情况留的复核手段：改了字段，哈希就对不上。
    """
    pid = _prediction("2026-03-02", direction="up")
    assert svc.verify_content_hash(pid)["ok"] is True

    with get_db() as conn:
        conn.execute("DROP TRIGGER IF EXISTS market_predictions_no_update")
        conn.execute("UPDATE market_predictions SET direction='down' WHERE id=?", (pid,))
    init_db()  # 把触发器装回来

    check = svc.verify_content_hash(pid)
    assert check["found"] is True
    assert check["ok"] is False


def test_insert_is_idempotent_by_id():
    """同 id 重复写入返回 False 且不报错——作业重跑不该炸。"""
    rec = {
        "id": "same-id", "symbol": SYMBOL, "target_date": "2026-03-02",
        "as_of": "2026-03-01T15:00:00+08:00", "generated_at": "2026-03-01T17:00:00+08:00",
        "origin": "live", "direction": "up", "strength": 0.2, "basis_json": "{}",
        "profile_hash": "x", "engine_ver": "0.1.2", "ruleset_ver": "0.1.0",
        "created_at": "2026-03-01T17:00:00+08:00",
    }
    rec["content_hash"] = svc._content_hash(rec)
    assert insert_market_prediction(rec) is True
    assert insert_market_prediction(rec) is False


def test_replay_and_live_records_coexist():
    """同一目标日的 live 与 replay 各自独立存在，不互相覆盖。"""
    _prediction("2026-03-02", origin="live")
    _prediction("2026-03-02", origin="replay")
    rows = list_market_predictions(symbol=SYMBOL, limit=10)
    assert len(rows) == 2
    assert {r["origin"] for r in rows} == {"live", "replay"}


# ─────────────────────── ② 结算 ───────────────────────


def test_settlement_writes_outcome_and_is_idempotent(no_network):
    """结算一次写入结果；再跑一次不得产生"新结算"。"""
    _prediction("2026-03-03", direction="up")
    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3030.0)])

    first = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 3))
    assert first["settled"] == ["2026-03-03"]
    assert first["pending"] == []
    assert first["corrections"] == []

    o = get_market_forecast_outcome(SYMBOL, "2026-03-03")
    assert o["actual_direction"] == "up"
    assert o["basis"] == "prevClose"
    assert o["base_close"] == 3000.0 and o["close"] == 3030.0

    second = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 3))
    assert second["settled"] == []
    assert second["checked"] == 0, "已结算的样本不应再次进入检查队列"


def test_earliest_pending_target_can_actually_settle(no_network):
    """★ 回归：最早的待结算日必须能被结掉（窗口起点 off-by-one）★

    曾经的写法把行情查询窗口起点设成"最早的待结算日"。但判定某日涨跌要用它
    **前一根** bar 作基准，那根 bar 落在窗口外 → `change_at(bars, 0)` 返回 None
    → 最早的待结算日**永远结不掉**。

    这个 bug 的破坏方式是安静的：没有任何报错，样本一天天堆在待结算队列里，
    界面上只表现为"样本还少"。所以必须有一条用例专门盯住"最早那一天"。

    这里只给**目标日之前**的行情，且不额外扩大查询范围——
    完全依赖 settle_pending 自己去取基准 bar。
    """
    _prediction("2026-03-03", direction="up")
    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3030.0)])

    r = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 3))
    assert r["settled"] == ["2026-03-03"], "最早的待结算日没能结掉，基准 bar 又被漏在窗口外了"
    o = get_market_forecast_outcome(SYMBOL, "2026-03-03")
    assert o["base_close"] == 3000.0, "基准价取错了：应当是前一交易日的收盘"
    assert o["basis"] == "prevClose"


def test_multiple_pending_days_all_settle_in_one_run(no_network):
    """多日积压时，**每一条**都要结掉（含最早的），不能只结后几条。"""
    for d in ("2026-03-03", "2026-03-04", "2026-03-05"):
        _prediction(d, direction="up")
    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3030.0),
           ("2026-03-04", 3010.0), ("2026-03-05", 3060.0)])

    r = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 5), today=date(2026, 3, 5))
    assert r["settled"] == ["2026-03-03", "2026-03-04", "2026-03-05"]
    assert r["pending"] == []
    dirs = {
        d: get_market_forecast_outcome(SYMBOL, d)["actual_direction"]
        for d in ("2026-03-03", "2026-03-04", "2026-03-05")
    }
    assert dirs == {"2026-03-03": "up", "2026-03-04": "down", "2026-03-05": "up"}


def test_missing_bar_keeps_sample_pending(no_network):
    """目标日没有行情 → **保持待结算**，既不算命中也不算未命中。

    这是"没拿到"与"没有"必须能被分辨的那条线。
    把停牌/缺数据计入分母会系统性低估准确率，而且完全看不出来。
    """
    _prediction("2026-03-04", direction="up")
    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3030.0)])  # 缺 03-04

    r = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 4), today=date(2026, 3, 4))
    assert r["settled"] == []
    assert r["pending"] == ["2026-03-04"]
    assert get_market_forecast_outcome(SYMBOL, "2026-03-04") is None
    assert [p["target_date"] for p in list_pending_settlement(SYMBOL, "2026-03-04")] == ["2026-03-04"]


def test_later_run_settles_the_previously_pending_sample(no_network):
    """补结算：下一次作业拿到行情后，该样本自然被结掉。"""
    _prediction("2026-03-04", direction="up")
    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3030.0)])
    svc.settle_pending(SYMBOL, up_to=date(2026, 3, 4), today=date(2026, 3, 4))

    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3030.0), ("2026-03-04", 2990.0)])
    r = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 4), today=date(2026, 3, 5))
    assert r["settled"] == ["2026-03-04"]
    assert get_market_forecast_outcome(SYMBOL, "2026-03-04")["actual_direction"] == "down"


TARGET = "2026-03-03"


def _rt_stub(close: float, pre_close: float = 3000.0):
    """把 `_realtime_judgement` 打桩成"针对 TARGET 的实时口径"。

    真的 `_realtime_judgement` 只能代表"今天"，而测试的目标日是固定的历史日期，
    所以必须打桩。这不是为了绕开逻辑——实时口径的**落库与复核路径**才是被测对象。
    """
    from forecast.change import OutcomeJudgement, judge_close_pair

    def _f(symbol, flat):
        ratio, direction = judge_close_pair(pre_close, close, flat)
        return {
            "date": TARGET,
            "judgement": OutcomeJudgement(
                symbol=symbol, target_date=TARGET, base_date="实时前收盘",
                base_close=pre_close, close=close, actual_change=ratio,
                actual_direction=direction, basis="rtQuote", flat_threshold_pct=flat,
            ),
        }

    return _f


def test_official_bar_corrects_realtime_settlement(monkeypatch):
    """实时口径先结掉 → 次日官方日线到位后必须被**修正**，并记进 corrections。

    这条用例之前是"假装"测这个机制的：它手动把实时结果 upsert 进库，
    于是绕开了真正的路径。而真正的路径当时**根本走不到**——
    `list_pending_settlement` 只返回没有 outcome 的记录，实时结算一写 outcome，
    该样本就离开了待结算队列，"次日修正"永远不会发生。

    现在的断言是关键的那一条：**第二次作业必须自己发现那条实时结算**，
    不需要任何手工干预。旧代码在这里会得到 `corrections == []`。
    """
    _prediction(TARGET, direction="up")

    # ① 当日：官方日线还没发布，用实时价 3050（涨）先结掉
    monkeypatch.setattr(svc, "_realtime_judgement", _rt_stub(3050.0))
    first = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 3))
    assert first["settled"] == [TARGET]
    assert get_market_forecast_outcome(SYMBOL, TARGET)["source"] == "sina_rt"
    assert get_market_forecast_outcome(SYMBOL, TARGET)["actual_direction"] == "up"

    # ② 次日：官方日线到位且方向相反（2980 → 跌）
    _bars([("2026-03-02", 3000.0), (TARGET, 2980.0)])
    monkeypatch.setattr(svc, "_realtime_judgement", lambda *a, **k: None)
    second = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 4))

    assert second["settled"] == [], "这不是新结算，是一次修正"
    assert second["corrections"] == [TARGET]
    o = get_market_forecast_outcome(SYMBOL, TARGET)
    assert o["actual_direction"] == "down"
    assert o["close"] == 2980.0
    assert o["source"] == "bar"

    # ③ 第三次：已经换成官方口径，不再重复复核
    third = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 5))
    assert third["corrections"] == []
    assert third["confirmed"] == []
    assert third["checked"] == 0


def test_realtime_settlement_kept_when_official_bar_absent(no_network):
    """官方日线仍未发布 → 保留实时口径，**不清空、也不改记成未命中**。

    它是一条真结算，只是依据弱一些。把它退回去等于让样本量凭空少一天。
    """
    _prediction(TARGET, direction="up")
    from db import upsert_market_forecast_outcome

    upsert_market_forecast_outcome({
        "symbol": SYMBOL, "target_date": TARGET, "base_date": "实时前收盘",
        "base_close": 3000.0, "close": 3050.0, "actual_change": 3050.0 / 3000.0 - 1,
        "actual_direction": "up", "basis": "rtQuote", "source": "sina_rt",
        "ruleset_ver": "0.1.0", "settled_at": "2026-03-03T15:45:00+08:00",
    })
    r = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 4))
    assert r["corrections"] == [] and r["confirmed"] == []
    o = get_market_forecast_outcome(SYMBOL, TARGET)
    assert o["source"] == "sina_rt" and o["actual_direction"] == "up"


def test_matching_official_bar_is_confirmed_not_corrected(no_network):
    """官方数值与实时口径一致 → 记 confirmed，不记 corrections。

    两者混为一谈会让"修正了多少条"这个数字失去含义：
    它应当只统计**方向或数值真的变了**的那些。
    """
    _prediction(TARGET, direction="up")
    from db import upsert_market_forecast_outcome

    upsert_market_forecast_outcome({
        "symbol": SYMBOL, "target_date": TARGET, "base_date": "实时前收盘",
        "base_close": 3000.0, "close": 3030.0, "actual_change": 0.01,
        "actual_direction": "up", "basis": "rtQuote", "source": "sina_rt",
        "ruleset_ver": "0.1.0", "settled_at": "2026-03-03T15:45:00+08:00",
    })
    _bars([("2026-03-02", 3000.0), (TARGET, 3030.0)])
    r = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 4))
    assert r["corrections"] == []
    assert r["confirmed"] == [TARGET]
    assert get_market_forecast_outcome(SYMBOL, TARGET)["source"] == "bar"


def test_flat_sample_excluded_from_settlement_stats(no_network):
    """平盘照常落库（事实要留存），但命中判定返回 None，不进二元统计。"""
    _prediction("2026-03-03", direction="up")
    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3000.5)])  # +0.0167% < 0.05%
    svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 3))
    o = get_market_forecast_outcome(SYMBOL, "2026-03-03")
    assert o["actual_direction"] == "flat"
    from forecast.change import hit_of

    assert hit_of("up", o["actual_direction"]) is None


def test_replay_records_are_never_settled_into_accuracy(no_network):
    """replay 记录不参与结算统计（它不是"当时的预测"）。"""
    _prediction("2026-03-03", origin="replay")
    _bars([("2026-03-02", 3000.0), ("2026-03-03", 3030.0)])
    r = svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 3))
    assert r["checked"] == 0
    assert list_pending_settlement(SYMBOL, "2026-03-03") == []


# ─────────────────────── ③ 作业主体 ───────────────────────

# 「模拟作业跑在 2026-09-17 盘后」——必须同时冻结 `now`，只给 `today` 是不够的。
#
# run_daily_forecast 取记录时刻用 `now or now_shanghai()`，漏掉 `now` 就会落回
# **真实当前时间**。等真实日期越过目标交易日（2026-09-18）之后，引擎会以
# 「记录时刻不早于目标交易日，不能声明为 live 预测」为由拒绝起卦——这恰恰是
# 引擎**该做**的事（见 `engine.divine` 的未来函数拦截，以及记忆里的补跑口径：
# 不许把未来的作业提前跑）。于是这些用例从绿变红，不是代码坏了，是时钟走过去了。
# 所以日期与时刻必须一起钉住，用例才对时钟免疫。
_D0917 = date(2026, 9, 17)  # 周四


def _run_on(d: date, **kwargs):
    """以 d 当天盘后 15:45（Asia/Shanghai）的时刻跑一次作业。"""
    return svc.run_daily_forecast(
        today=d,
        now=datetime(d.year, d.month, d.day, 15, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
        **kwargs,
    )


def test_daily_run_creates_prediction_for_next_trade_date(no_network):
    r = _run_on(_D0917)
    assert r["trade_date"] is True
    assert r["status"] == "ok"
    assert r["target_date"] == "2026-09-18"
    assert r["created"] is not None
    row = get_market_prediction(r["created"])
    assert row["origin"] == "live"
    assert row["target_date"] == "2026-09-18"
    assert row["symbol"] == SYMBOL


def test_recorded_clock_follows_the_now_argument(no_network):
    """`now` 必须真的贯穿到记录时刻，而不是被就地读墙钟顶掉。

    这是把上面那批用例「钉住时刻」的前提：如果 `generate_prediction` 内部直接
    读墙钟（本轮修的那个 bug），传进来的 `now` 就只是个摆设，
    用例照样会随真实日期推进而变红。
    """
    r = _run_on(_D0917)
    assert r["status"] == "ok"
    row = get_market_prediction(r["created"])
    assert row["generated_at"].startswith("2026-09-17"), row["generated_at"]
    assert row["created_at"].startswith("2026-09-17"), row["created_at"]


def test_clock_later_than_target_is_refused(no_network):
    """记录时刻晚于目标交易日 → 引擎必须拒绝起卦（作业报非 ok）。

    这条是「不许把未来的作业提前跑」那口径的反面：不能靠事后把时钟报小
    来伪造一条 live 预测。上一轮那 4 个红用例就是被这条拦下的——
    现在把它写成显式断言，而不是让它以「测试过期」的形式偶然暴露。
    """
    r = svc.run_daily_forecast(
        today=_D0917,
        now=datetime(2026, 10, 15, 15, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert r["created"] is None
    assert r["status"] != "ok"
    assert any("起卦失败" in n for n in r["notes"]), r["notes"]


def test_daily_run_is_idempotent(no_network):
    """同一天重跑：不新增记录、状态仍为 ok（不是"失败"）。

    这条正是把出口判据从"本轮是否创建"改成"目标日是否有 live 记录"的理由：
    用事件式判据时，重跑会被误报成失败。
    """
    first = _run_on(_D0917)
    second = _run_on(_D0917)
    assert second["created"] is None
    assert second["status"] == "ok"
    assert first["target_date"] == second["target_date"] == "2026-09-18"
    assert len(list_market_predictions(symbol=SYMBOL, limit=10)) == 1


def test_daily_run_reports_failed_when_target_has_no_prediction(no_network, monkeypatch):
    """起卦环节坏掉 → 必须报 failed，而不是安静地记成 ok。

    判据是**状态式**的：作业跑完，目标日有没有 live 记录。
    这能揪出所有该报警的情形——起卦抛错、日历越界、库写不进去。
    """
    monkeypatch.setattr(svc, "generate_prediction", lambda *a, **k: None)
    r = _run_on(_D0917)
    assert r["status"] == "failed"
    assert r["target_date"] == "2026-09-18"
    assert any("仍无 live 预测记录" in n for n in r["notes"])


def test_daily_run_reports_failed_on_exception_in_divine(no_network, monkeypatch):
    """起卦抛异常时同样必须是 failed —— 异常被 catch 住不等于作业成功。"""
    def _boom(*a, **k):
        raise RuntimeError("模拟引擎故障")

    monkeypatch.setattr(svc, "generate_prediction", _boom)
    r = _run_on(_D0917)
    assert r["status"] == "failed"
    assert any("起卦失败" in n for n in r["notes"])


def test_holiday_run_does_nothing(no_network):
    """非交易日：什么都不做，并如实记为 holiday。

    DEMO_MODE 下交易日历走"周一至周五"兜底，所以用周六来构造。
    """
    assert date(2026, 9, 19).weekday() == 5
    r = _run_on(date(2026, 9, 19))
    assert r["status"] == "holiday"
    assert r["created"] is None
    assert list_market_predictions(symbol=SYMBOL, limit=10) == []


def test_run_is_persisted_with_notes(no_network):
    """每次运行都留痕（漏跑检测的对照表）。"""
    _run_on(_D0917)
    runs = list_market_forecast_runs(limit=10)
    assert len(runs) == 1
    assert runs[0]["run_date"] == "2026-09-17"
    assert runs[0]["status"] == "ok"


def test_sync_failure_marks_partial_but_still_predicts(no_network, monkeypatch):
    """行情同步失败不应吞掉起卦：结算的成果与起卦的成果互相独立。"""
    def _boom(*a, **k):
        raise RuntimeError("行情源不可用")

    monkeypatch.setattr(svc, "sync_index_daily", _boom)
    r = _run_on(_D0917)
    assert r["status"] == "partial"
    assert r["created"] is not None
    assert any("行情同步失败" in n for n in r["notes"])


# ─────────────────────── ④ 读取用载荷 ───────────────────────


def test_accuracy_payload_excludes_replay_and_pending(no_network):
    """准确率只吃 live 且**已结算**的样本。"""
    _prediction("2026-03-02", origin="live", direction="up")
    _prediction("2026-03-03", origin="live", direction="up")
    _prediction("2026-03-04", origin="live", direction="up")   # 无行情 → 待结算
    _prediction("2026-03-02", origin="replay", direction="down")
    _bars([("2026-03-01", 3000.0), ("2026-03-02", 3030.0), ("2026-03-03", 3030.0)])
    svc.settle_pending(SYMBOL, up_to=date(2026, 3, 4), today=date(2026, 3, 4))

    rep = svc.accuracy_payload()
    # 03-02 涨(命中) + 03-03 平(剔除) → n = 1
    assert rep["n"] == 1
    assert rep["hits"] == 1
    assert rep["flat_count"] == 1
    assert rep["pending_count"] == 1
    assert rep["symbol"] == SYMBOL
    # 报告结构必须带基线/门槛/样本充足性——不存在"只给百分比"的形态
    assert "always_up" in rep["baselines"]
    assert rep["sample_sufficient"] is False
    assert rep["disclaimer"]


def test_history_payload_carries_hit_and_pending_flag(no_network):
    _prediction("2026-03-02", origin="live", direction="up")
    _prediction("2026-03-03", origin="live", direction="down")
    _bars([("2026-03-01", 3000.0), ("2026-03-02", 3030.0)])
    svc.settle_pending(SYMBOL, up_to=date(2026, 3, 3), today=date(2026, 3, 3))

    items = {i["target_date"]: i for i in svc.history_payload()["items"]}
    assert items["2026-03-02"]["outcome"]["hit"] is True
    assert items["2026-03-03"]["outcome"] is None, "无行情的样本在列表里应显示为待结算"


def test_latest_payload_picks_upcoming_and_recent_settled(no_network):
    _prediction("2026-03-02", origin="live", direction="up")
    _bars([("2026-03-01", 3000.0), ("2026-03-02", 3030.0)])
    svc.settle_pending(SYMBOL, up_to=date(2026, 3, 2), today=date(2026, 3, 2))

    payload = svc.latest_payload()
    assert payload["symbol"] == SYMBOL
    assert payload["symbol_label"]
    assert payload["disclaimer"]
    # 目标日全部早于今天 → upcoming 为空，recent_settled 取最近一条
    assert payload["recent_settled"]["target_date"] == "2026-03-02"


def test_runs_payload_lists_gaps_honestly(no_network):
    """漏跑的交易日必须出现在缺口列表里，且**不会被伪造补跑**。"""
    _prediction("2026-09-16", origin="live")
    payload = svc.runs_payload(gap_days=10)
    assert "gap_note" in payload
    assert payload["disclaimer"]
    # 缺口按交易日列出（DEMO_MODE 下=工作日），且不含已有预测的那一天
    assert all(isinstance(g, str) for g in payload["gaps"])
    assert isinstance(payload["runs"], list)


def test_backfill_replay_marks_origin_and_is_excluded(no_network):
    """回补只走 replay：它能填满页面，但不进准确率。"""
    out = svc.backfill_replay(
        SYMBOL,
        ["2026-03-03"],
        {"2026-03-03": "2026-03-02"},
    )
    assert out["created"] == ["2026-03-03"]
    row = list_market_predictions(symbol=SYMBOL, limit=5)[0]
    assert row["origin"] == "replay"
    rep = svc.accuracy_payload()
    assert rep["n"] == 0, "replay 记录不得进入准确率"


def test_backfill_replay_requires_explicit_as_of(no_network):
    """缺 as_of 必须失败——不允许调用方"顺手挑一个起卦日"。

    让调用方为每条记录挑起卦时点，就等于允许"挑一个能摇出好卦的日子"，
    而那正是这个功能最不能做的事。
    """
    out = svc.backfill_replay(SYMBOL, ["2026-03-03"], {})
    assert out["created"] == []
    assert out["failed"] == [{"target_date": "2026-03-03", "reason": "缺少起卦日（as_of）"}]


def test_flat_threshold_is_read_in_percentage_points():
    """平盘阈值单位是**百分点**：0.05 表示 0.05%（换算比例 0.0005）。

    传 0.0005 当百分点用会把几乎所有真实平盘算成涨跌，样本量凭空膨胀——
    而报告里的数字会一并"变好"。
    """
    from forecast.change import direction_of

    assert svc.flat_threshold_pct() == 0.05
    assert direction_of(0.0001) == "flat"      # 0.01% → 平
    assert direction_of(0.0005) == "up"        # 恰在阈值上 → 计入涨（区间为开区间）
    assert direction_of(0.002) == "up"
    assert direction_of(-0.002) == "down"
