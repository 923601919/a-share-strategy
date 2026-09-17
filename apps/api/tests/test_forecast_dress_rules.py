"""装卦 + 断卦黄金向量：8 个用例**逐字段**与 tianji 对账。

fixture 由 tianji 自身的引擎 dump（`dump.mjs`），因此这是一次真正的跨实现对账，
而不是"我自己算的自己看"。逐字段清单：

    id / direction / strength / yaoValues / material / digest
    hexagram / changedHexagram / movingYao / yongShen
    reasoning（整条中文依据链，逐条字面相等）
    dressedYao（6 爻 × gan/zhi/element/relation/deity/isShi/isYing/isVoid/vigor/flags）

另外钉住两条**结构性**约定（它们不是数值，靠数值比对抓不到）：

1. 依据链的**最后一条必须是综合得分那句** —— 它是整条链的落点。
2. 世应那条必须插在总结论**之前** —— 否则结论会被挤到中间，读起来是散的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from forecast.calendar_ctx import build_divination_context  # noqa: E402
from forecast.constants import LIUQIN_NAME  # noqa: E402
from forecast.engine import DivineRequest, divine  # noqa: E402
from forecast.profile import (  # noqa: E402
    CAST_HOUR,
    CAST_MINUTE,
    ENGINE_VERSION,
    RULESET_VERSION,
    build_profile,
    hash_profile,
)

GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "forecast_golden.json").read_text("utf-8")
)
PROFILE = build_profile()

# Python 字段名 → tianji 字段名（dump 里是 camelCase）
YAO_FIELDS = [
    ("position", "position"),
    ("gan", "gan"),
    ("zhi", "zhi"),
    ("element", "element"),
    ("relation", "relation"),
    ("deity", "deity"),
    ("is_shi", "isShi"),
    ("is_ying", "isYing"),
    ("is_void", "isVoid"),
    ("vigor", "vigor"),
    ("flags", "flags"),
]


def _case_id(c: dict) -> str:
    return f"{c['asOfDate']}->{c['targetDate']}"


def _divine(case: dict):
    ctx = build_divination_context(case["asOfDate"], hour=CAST_HOUR, minute=CAST_MINUTE)
    pred = divine(
        DivineRequest(
            symbol=case["symbol"],
            target_date=case["targetDate"],
            as_of=f"{case['asOfDate']}T15:00:00+08:00",
            method="liuyao",
            mode="deterministic",
            profile=PROFILE,
            context=ctx,
            engine_version=ENGINE_VERSION,
            ruleset_version=RULESET_VERSION,
            profile_hash=GOLDEN["profileHash"],
            now=f"{case['asOfDate']}T17:00:00+08:00",
        )
    )
    return pred, ctx


def test_profile_hash_matches_tianji():
    """profile_hash 是**整份档案**的指纹（含本功能用不到的紫微/八字字段）。

    删掉任何一个用不到的字段都会改变它，于是两边对同一日期会算出两个看起来
    都合法的口径——这正是把整份档案原样搬过来的原因。
    """
    assert hash_profile(PROFILE) == GOLDEN["profileHash"]
    assert GOLDEN["profileHash"] == "0da2843edf6479f1"
    assert GOLDEN["engineVersion"] == ENGINE_VERSION
    assert GOLDEN["rulesetVersion"] == RULESET_VERSION


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_cast_matches(case):
    """起卦层：爻值 / 种子材料 / digest / 卦名。"""
    pred, _ = _divine(case)
    b = pred.basis
    assert b["seed"]["yao_values"] == case["yaoValues"]
    assert b["seed"]["material"] == case["material"]
    assert b["seed"]["digest"] == case["digest"]
    assert b["hexagram"] == case["hexagram"]
    assert b["changed_hexagram"] == case["changedHexagram"]
    assert b["moving_yao"] == case["movingYao"]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_verdict_matches(case):
    """断卦层：方向 / 强度 / 用神 / id。"""
    pred, _ = _divine(case)
    assert pred.direction == case["direction"]
    assert pred.strength == case["strength"]
    assert pred.id == case["id"]
    assert pred.basis["yong_shen"] == case["yongShen"]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_reasoning_matches_verbatim(case):
    """依据链逐条字面相等——包括中文标点。

    这条链是"用户凭什么相信这个方向"的全部答案，它必须可被外部复核。
    放宽成"包含关键词"就等于放弃了这项保证。
    """
    pred, _ = _divine(case)
    assert pred.basis["reasoning"] == case["reasoning"]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_dressed_yaos_match(case):
    """装卦层：6 爻 × 11 字段逐字段相等。"""
    pred, _ = _divine(case)
    yaos = pred.basis["dressed_yaos"]
    assert len(yaos) == 6
    for py_y, ts_y in zip(yaos, case["dressedYao"]):
        for py_key, ts_key in YAO_FIELDS:
            assert py_y[py_key] == ts_y[ts_key], (
                f"第 {py_y['position']} 爻的 {ts_key} 不一致：{py_y[py_key]!r} vs {ts_y[ts_key]!r}"
            )


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_context_pillars_match(case):
    """历法：日柱/月柱/年柱/旬空/月建五行（此项不含 hour_branch，理由见 calendar 测试）。"""
    _, ctx = _divine(case)
    want = case["context"]
    assert {"gan": ctx.day_pillar.gan, "zhi": ctx.day_pillar.zhi,
            "sexagenary": ctx.day_pillar.sexagenary} == want["dayPillar"]
    assert {"gan": ctx.month_pillar.gan, "zhi": ctx.month_pillar.zhi,
            "sexagenary": ctx.month_pillar.sexagenary} == want["monthPillar"]
    assert {"gan": ctx.year_pillar.gan, "zhi": ctx.year_pillar.zhi,
            "sexagenary": ctx.year_pillar.sexagenary} == want["yearPillar"]
    assert list(ctx.void_branches) == want["voidBranches"]
    assert ctx.month_element == want["monthElement"]


# ─────────────────────────── 结构性约定 ───────────────────────────


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_summary_is_the_last_reasoning_line(case):
    """最后一条必须是以「综合得分」起头的总结论。

    依据链的阅读方式是从上到下、结论收口。若被别的文字挤到中间，
    整条链就散了——而且这种错误在任何数值比对里都看不出来。
    """
    pred, _ = _divine(case)
    assert pred.basis["reasoning"][-1].startswith("综合得分")


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_shi_ying_line_sits_before_summary(case):
    """世应那条插在总结论之前，且世/应位置互不相同。"""
    pred, ctx = _divine(case)
    reasoning = pred.basis["reasoning"]
    assert len(reasoning) >= 3
    assert reasoning[-2].startswith("世爻在")
    assert "应爻在" in reasoning[-2]
    assert "世为己、应为市" in reasoning[-2]
    assert pred.basis["shi_position"] != pred.basis["ying_position"]


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=_case_id)
def test_first_reasoning_line_explains_the_yong_shen_choice(case):
    """第一条必须交代用神的取法——否则后面所有加减分都失去参照：
    读者不知道加减的是"谁"的分。

    有**两个**合法开头（这是对账时被用例逼出来的，不是设计时想到的）：

      · 有妻财 → 「取用神：…」 + 爻位/干支/五行
      · 财不上卦 → 「…卦中无妻财爻（财不上卦）…」

    八条黄金向量里有 **3 条**属于后者（雷山小过 / 天泽履 / 风泽中孚）。
    最初把这个用例写成"必须 startswith('取用神：')"，三条立刻红了——
    而这正是用例该干的事：它证明了这个分支在真实样本里占 37.5%，
    不是一条几乎走不到的兜底代码。
    """
    pred, _ = _divine(case)
    first = pred.basis["reasoning"][0]
    ys = pred.basis["yong_shen"]

    if ys is None:
        assert "无妻财爻（财不上卦）" in first
        assert pred.basis["hexagram"] in first
    else:
        assert first.startswith("取用神：")
        assert LIUQIN_NAME[ys["relation"]] in first
        assert f"{ys['position']}爻" in first
        assert ys["zhi"] in first


def test_no_wealth_branch_is_a_documented_systematic_down_tilt():
    """★ 财不上卦 → 得分固定 0.35 → 一律判跌 ★

    规则里 `no_wealth_in_hexagram = -0.15`，于是 0.5 - 0.15 = 0.35 < 0.5 → down。
    这条分支在黄金向量的 8 个样本里命中 3 个（37.5%），也就是说：
    **模型的预测输出本身不是 50/50 的**，约有三分之一的日子会因为"财不上卦"
    被固定判成跌。

    这件事必须被写下来，因为它正是报告里那些基线存在的理由：
      · 若哪天「永远猜跌」的准确率特别高，多半是这条分支在起作用，
        而不是卦象有信息量；
      · 偏置匹配置换检验（保持涨跌偏好、随机重排）就是为识别这种情况设计的——
        它天然把这 37.5% 的固定偏置包含进零分布。

    若将来调整这个权重或这条分支，本用例会失败，提示重新审视统计口径。
    """
    no_wealth = []
    for case in GOLDEN["cases"]:
        pred, _ = _divine(case)
        if pred.basis["yong_shen"] is None:
            no_wealth.append(pred)

    assert len(no_wealth) == 3, "样本里财不上卦的条数变了，下面的口径要重新核对"
    for pred in no_wealth:
        assert pred.direction == "down"
        assert pred.strength == 0.3
        assert pred.basis["reasoning"][0].endswith("判为不利。")
        assert "综合得分 0.350" in pred.basis["reasoning"][-1]


def test_direction_is_only_up_or_down():
    """六爻只给方向，不给平盘。平盘是**结算口径**里的概念，不是预测输出。

    若允许引擎输出 flat，模型就多了一条"稳赚不亏"的退路：
    猜不中就报平盘，而平盘样本本来就不进分母。
    """
    for case in GOLDEN["cases"]:
        pred, _ = _divine(case)
        assert pred.direction in ("up", "down")


def test_strength_bounded_and_derived_from_basis():
    """强度落在 [0,1]，且与用神得分保持同一口径（不是另算一份）。"""
    from forecast.service import strength_of_score

    for case in GOLDEN["cases"]:
        pred, _ = _divine(case)
        assert 0.0 <= pred.strength <= 1.0
    # 已知锚点：0.5 分 → 强度 0；两端 → 1
    assert strength_of_score(0.5) == 0.0
    assert strength_of_score(0.0) == 1.0
    assert strength_of_score(1.0) == 1.0
