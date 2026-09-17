"""引擎护栏：时间边界 / live 与 replay 的区分 / 确定性 / 手工摇卦。

这个文件测的全是「出错时不会报错、只会让准确率虚高」的那类问题——
它们无法通过看盘面发现，所以只能靠用例钉住。

最核心的一条是**未来函数拦截**：起卦时点必须早于目标交易日。
允许此情形，回测准确率会虚高到毫无意义，而且完全看不出来。
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


def _req(
    *,
    symbol: str = "000001.SH",
    target_date: str = "2026-09-18",
    as_of_date: str = "2026-09-17",
    now: str | None = None,
    origin: str = "live",
    mode: str = "deterministic",
    manual_yao: list[int] | None = None,
    context_hour: int = CAST_HOUR,
):
    return DivineRequest(
        symbol=symbol,
        target_date=target_date,
        as_of=f"{as_of_date}T15:00:00+08:00",
        method="liuyao",
        mode=mode,
        profile=PROFILE,
        context=build_divination_context(as_of_date, hour=context_hour, minute=CAST_MINUTE),
        engine_version=ENGINE_VERSION,
        ruleset_version=RULESET_VERSION,
        profile_hash=GOLDEN["profileHash"],
        now=now or f"{as_of_date}T17:00:00+08:00",
        origin=origin,
        manual_yao=manual_yao,
    )


# ─────────────────────── ① 时间边界（未来函数拦截） ───────────────────────


@pytest.mark.parametrize("bad_target", ["2026-09-17", "2026-09-16", "2026-08-01"])
def test_as_of_not_before_target_is_rejected(bad_target):
    """起卦时点 >= 目标交易日 → 必须抛错。

    这是整个功能的诚信基础。允许它，就等于允许"用 T 日的信息预测 T 日"，
    而结果会表现为一条漂亮的历史准确率。
    """
    with pytest.raises(ValueError) as e:
        divine(_req(target_date=bad_target))
    assert "未来函数" in str(e.value)


def test_as_of_same_day_as_target_rejected_even_by_one_second():
    """同日也算越界：盘中起卦已经能看到当日方向了。"""
    with pytest.raises(ValueError):
        divine(_req(target_date="2026-09-17", as_of_date="2026-09-17"))


def test_live_requires_generation_before_target():
    """声明 live 就必须真的"活"：记录时刻必须早于目标交易日。

    它挡不住存心伪造（调用方可以把 now 填成过去），但挡住了最常见的疏忽——
    **回补历史时忘记把 origin 改成 replay**，于是凭空多出一批看起来完全合法的
    "实时预测"，然后被计入准确率。
    """
    with pytest.raises(ValueError) as e:
        divine(_req(target_date="2026-09-17", as_of_date="2026-09-16", now="2026-09-17T17:00:00+08:00"))
    assert "replay" in str(e.value)


def test_replay_allows_generating_after_target():
    """显式声明 replay 时允许事后生成——但统计层会把它排除在外。

    这不是"放松检查"，而是把这件事从**沉默的疏忽**变成**显式的声明**。
    """
    pred = divine(
        _req(
            target_date="2026-09-17",
            as_of_date="2026-09-16",
            now="2026-09-17T17:00:00+08:00",
            origin="replay",
        )
    )
    assert pred.origin == "replay"
    assert pred.direction in ("up", "down")


def test_origin_enters_prediction_id():
    """同一目标日的 live 与 replay 记录必须是**两条**，各有各的 id。

    若两者算成同一个 id，后写的会撞上前者的冻结约束而被拒绝——
    那比现在更糟：你会失去原本想要的那条回补记录，而且看起来像"功能坏了"。
    """
    live = divine(_req(origin="live"))
    replay = divine(_req(origin="replay"))
    assert live.id != replay.id
    assert live.basis == replay.basis, "origin 不应影响卦象本身，只影响身份"


def test_version_enters_prediction_id():
    """引擎版本或规则集版本变化 → id 变化。

    否则升级规则后新算出的结论会撞上旧 id 被冻结拒绝，
    而"为什么这天的预测没更新"这个问题将无从回答。
    """
    base = divine(_req())
    newer = divine(dataclasses.replace(_req(), engine_version="0.1.3"))
    assert base.id != newer.id


# ─────────────────────────── ② 确定性 ───────────────────────────


def test_same_request_gives_identical_prediction():
    a = divine(_req())
    b = divine(_req())
    assert a.id == b.id
    assert a.direction == b.direction
    assert a.strength == b.strength
    assert a.basis == b.basis


def test_now_does_not_affect_the_hexagram():
    """`now`（记录生成时刻）只进 generated_at / locked_at，**不参与起卦**。

    这条是在防一个很隐蔽的错：若哪天有人"顺手"把 now 掺进种子，
    同一个目标日会因为跑作业的时刻不同而摇出不同的卦——
    用户反复重跑就能刷出满意的卦，这个功能立刻失去全部意义。
    """
    a = divine(_req(now="2026-09-17T16:00:00+08:00"))
    b = divine(_req(now="2026-09-17T23:59:59+08:00"))
    assert a.basis == b.basis
    assert a.id == b.id
    assert a.direction == b.direction
    assert a.strength == b.strength
    assert a.generated_at != b.generated_at


def test_different_salt_changes_the_hexagram():
    """换盐值 = 换一套档案，卦必须不同。

    反之若盐值不影响结果，说明它没进种子——那种情况下
    profile_hash 会变而卦不变，两者不一致且没人会发现。
    """
    other_profile = build_profile("another-salt")
    req = dataclasses.replace(_req(), profile=other_profile)
    assert divine(req).basis["seed"]["material"] != divine(_req()).basis["seed"]["material"]


def test_different_symbol_or_target_changes_the_hexagram():
    base = divine(_req())
    assert divine(_req(symbol="SZ399006")).basis["seed"]["digest"] != base.basis["seed"]["digest"]
    assert divine(_req(target_date="2026-09-21")).basis["seed"]["digest"] != base.basis["seed"]["digest"]


def test_engine_has_no_hidden_time_or_io_dependency():
    """引擎模块不得引入 db / 网络 / 时钟相关模块。

    这是一条**架构级**的护栏：引擎必须能在没有数据库、没有网络的环境里单独跑。
    一旦有人为了图方便在 engine 里直接落库，这个断言会失败。
    """
    import forecast.engine as engine

    src = Path(engine.__file__).read_text("utf-8")
    for forbidden in ("import akshare", "from db import", "import db", "requests.", "time.time(", "datetime.now("):
        assert forbidden not in src, f"引擎里出现了不该有的依赖：{forbidden}"


# ─────────────────────────── ③ 手工摇卦 ───────────────────────────


def test_manual_mode_requires_six_values():
    with pytest.raises(ValueError) as e:
        divine(_req(mode="manual", manual_yao=[7, 8, 9]))
    assert "6 个爻值" in str(e.value)


def test_manual_mode_rejects_illegal_value():
    with pytest.raises(ValueError) as e:
        divine(_req(mode="manual", manual_yao=[7, 8, 9, 6, 5, 7]))
    assert "6/7/8/9" in str(e.value)


def test_manual_mode_material_is_traceable():
    """手工摇卦的种子材料里必须留下那六个数——否则事后无法复核这卦是怎么来的。"""
    pred = divine(_req(mode="manual", manual_yao=[7, 8, 9, 6, 7, 8]))
    assert pred.basis["seed"]["material"] == "manual|000001.SH|2026-09-18|789678"
    assert pred.basis["seed"]["method"] == "manual"
    assert pred.basis["seed"]["yao_values"] == [7, 8, 9, 6, 7, 8]


def test_manual_mode_still_enforces_time_boundary():
    """手工摇卦不能成为绕过未来函数检查的后门。"""
    with pytest.raises(ValueError):
        divine(_req(mode="manual", manual_yao=[7, 8, 9, 6, 7, 8], target_date="2026-09-17"))


def test_manual_mode_moving_yao_from_six_and_nine():
    """6（老阴）与 9（老阳）为动爻，7/8 不动。变卦只在有动爻时存在。"""
    pred = divine(_req(mode="manual", manual_yao=[9, 7, 7, 8, 8, 6]))
    assert pred.basis["moving_yao"] == [1, 6]
    assert pred.basis["changed_hexagram"] is not None

    still = divine(_req(mode="manual", manual_yao=[7, 7, 7, 8, 8, 8]))
    assert still.basis["moving_yao"] == []
    assert still.basis["changed_hexagram"] is None


# ─────────────────────────── ④ 记录字段完备性 ───────────────────────────


def test_basis_contains_everything_needed_to_recheck():
    """依据（basis）必须自带复核所需的全部东西：种子材料 + digest + 历法 + 装卦 + 依据链。

    少任何一项，事后就无法在不重跑历史代码的前提下解释这条预测。
    """
    pred = divine(_req())
    b = pred.basis
    assert set(b["seed"]) == {"material", "digest", "yao_values", "method"}
    assert set(b["context"]) == {
        "solar_date", "year_pillar", "month_pillar", "day_pillar", "void_branches", "month_element",
    }
    assert len(b["dressed_yaos"]) == 6
    assert b["reasoning"]
    for key in ("method", "hexagram", "changed_hexagram", "moving_yao", "palace",
                "generation", "shi_position", "ying_position", "yong_shen"):
        assert key in b


def test_prediction_metadata_is_frozen_at_creation():
    """locked_at 与 generated_at 同值：记录一旦产出即冻结，不存在"稍后补写"。"""
    pred = divine(_req())
    assert pred.locked_at == pred.generated_at
    assert pred.engine_version == ENGINE_VERSION
    assert pred.ruleset_version == RULESET_VERSION
