"""历法桥接（干支 ↔ 六爻）与「时辰不参与判决」这条边界。

★ 本文件的核心是 `test_hour_branch_does_not_reach_verdict` ★

移植时实测到：tianji 做了真太阳时修正，本项目**没有**做，于是 8 个黄金向量里
有 2 个日期的 `hour_branch` 与 tianji 不同（15:00 被修正推回未时=7，
我们用钟表时辰申时=8），而**其余全部字段逐字节一致**——包括方向、强度、
六爻装卦与整条依据链。

这说明 `hour_branch` 根本没参与判决。既然没参与，就不迁那条修正链路：
迁一条不参与计算的修正，只会让人以为它算了什么。
（`calendar_ctx.py` 的文件头把这句话的准确范围写清楚了。）

本测试把这条结论钉死：**任意替换 hour_branch，判决必须一字不变。**
将来若引入时辰相关规则，这个测试会失败——那正是应当发生的事，
因为届时的正确做法是先把它换成真太阳时口径并 bump ENGINE_VERSION。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from forecast.calendar_ctx import build_divination_context  # noqa: E402
from forecast.constants import DI_ZHI  # noqa: E402
from forecast.engine import DivineRequest, divine  # noqa: E402
from forecast.profile import (  # noqa: E402
    CAST_HOUR,
    CAST_MINUTE,
    ENGINE_VERSION,
    RULESET_VERSION,
    build_profile,
)

GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "forecast_golden.json").read_text("utf-8")
)
PROFILE = build_profile()


def _divine_with(ctx, symbol="000001.SH", target="2026-09-18", as_of="2026-09-17T15:00:00+08:00"):
    return divine(
        DivineRequest(
            symbol=symbol,
            target_date=target,
            as_of=as_of,
            method="liuyao",
            mode="deterministic",
            profile=PROFILE,
            context=ctx,
            engine_version=ENGINE_VERSION,
            ruleset_version=RULESET_VERSION,
            profile_hash=GOLDEN["profileHash"],
            now="2026-09-17T17:00:00+08:00",
        )
    )


def test_hour_branch_does_not_reach_verdict():
    """把 hour_branch 换成 12 个地支里任意一个，判决必须完全不变。"""
    base_ctx = build_divination_context("2026-09-17", hour=CAST_HOUR, minute=CAST_MINUTE)
    base = _divine_with(base_ctx)

    for branch in range(12):
        if branch == base_ctx.hour_branch:
            continue
        mutated = dataclasses.replace(base_ctx, hour_branch=branch)
        got = _divine_with(mutated)
        assert got.direction == base.direction
        assert got.strength == base.strength
        assert got.id == base.id
        # 整份 basis 逐字段相等——reasoning / dressed_yaos / yong_shen 全在内
        assert got.basis == base.basis, f"hour_branch={branch}（{DI_ZHI[branch]}）时判决发生了变化"


def test_hour_branch_is_the_clock_hour_not_solar_time():
    """`hour_branch` 的语义是**钟表时辰**（申时=8），不是真太阳时。

    这条单独立一个断言而不是只写在注释里：将来若有人"顺手"把真太阳时修正补上，
    这里会失败并提示他——先读 `calendar_ctx.py` 文件头的范围说明再审慎决定。
    """
    ctx = build_divination_context("2026-09-17", hour=15, minute=0)
    assert ctx.hour_branch == DI_ZHI.index("申") == 8


def test_hour_does_not_change_pillars_outside_late_zi():
    """除晚子时外，起卦小时的改变不影响任何四柱。"""
    a = build_divination_context("2026-09-17", hour=9, minute=30)
    b = build_divination_context("2026-09-17", hour=15, minute=0)
    assert a.day_pillar == b.day_pillar
    assert a.month_pillar == b.month_pillar
    assert a.year_pillar == b.year_pillar
    assert a.void_branches == b.void_branches
    assert a.month_element == b.month_element


def test_late_zi_hour_switches_day_pillar():
    """23:00 之后日柱必须按档案口径归次日（standard-cn: nextDay）。

    这是**真实被消费**的输入：日柱变 → 旬空变 → 六神与旺衰变 → 判决会变。
    与 hour_branch 形成对照，说明"不迁真太阳时"不是偷懒，而是范围界定。
    """
    next_day = build_divination_context("2026-09-17", hour=23, minute=30, late_zi_hour="nextDay")
    cur_day = build_divination_context("2026-09-17", hour=23, minute=30, late_zi_hour="currentDay")
    assert next_day.day_pillar != cur_day.day_pillar

    p_next = _divine_with(next_day)
    p_cur = _divine_with(cur_day)
    # 日柱不同 → 判决不必相同；但两者都必须产出合法结论（不是抛错）
    assert p_next.direction in ("up", "down")
    assert p_cur.direction in ("up", "down")


def test_void_branches_are_two_and_distinct():
    """旬空恒为两个地支且互不相同——它直接决定用神是否"落空"。"""
    for case in GOLDEN["cases"]:
        ctx = build_divination_context(case["asOfDate"], hour=CAST_HOUR, minute=CAST_MINUTE)
        assert len(ctx.void_branches) == 2
        assert ctx.void_branches[0] != ctx.void_branches[1]
        assert all(z in DI_ZHI for z in ctx.void_branches)


def test_pillars_match_golden_per_case():
    """历法结果逐案对账（与 tianji 的 lunar-typescript 同源）。"""
    for case in GOLDEN["cases"]:
        ctx = build_divination_context(case["asOfDate"], hour=CAST_HOUR, minute=CAST_MINUTE)
        want = case["context"]
        got_day = f"{ctx.day_pillar.gan}{ctx.day_pillar.zhi}"
        assert got_day == f"{want['dayPillar']['gan']}{want['dayPillar']['zhi']}"
        got_month = f"{ctx.month_pillar.gan}{ctx.month_pillar.zhi}"
        assert got_month == f"{want['monthPillar']['gan']}{want['monthPillar']['zhi']}"
        got_year = f"{ctx.year_pillar.gan}{ctx.year_pillar.zhi}"
        assert got_year == f"{want['yearPillar']['gan']}{want['yearPillar']['zhi']}"


def test_month_pillar_follows_solar_term_not_calendar_month():
    """月柱以**节气**为界，不是农历月也不是公历月。

    取立秋日附近（2026 年立秋约 8 月 7 日）前后各一天，月柱必须不同——
    说明用的是 `getMonthInGanZhiExact` 而不是 `getMonthInGanZhi`。
    用错方法的结果是"看起来完全正常"的盘：月建错了，旺衰就全错，
    而整条依据链依然自洽。
    """
    # 2026 立秋：8 月 7 日。前后各取一天，避开交节当日的时刻歧义。
    before = build_divination_context("2026-08-05", hour=CAST_HOUR)
    after = build_divination_context("2026-08-10", hour=CAST_HOUR)
    assert before.month_pillar.zhi != after.month_pillar.zhi, (
        "立秋前后月建应换支；若相等，说明月柱没走节气口径"
    )
    assert before.month_element != after.month_element


def test_year_pillar_follows_lichun():
    """年柱以**立春**为界（不是元旦、也不是正月初一）。"""
    # 2026 立春约 2 月 4 日
    before = build_divination_context("2026-01-20", hour=CAST_HOUR)
    after = build_divination_context("2026-02-20", hour=CAST_HOUR)
    assert before.year_pillar.zhi != after.year_pillar.zhi


def test_context_rejects_malformed_date():
    """坏日期必须抛错，不能静默退回某一天。"""
    with pytest.raises(Exception):
        build_divination_context("2026-13-45", hour=CAST_HOUR)
