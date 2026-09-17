"""统计诚信：与 tianji 逐数值对齐 + 报告结构的强制项。

为什么值得单独立一个测试文件
----------------------------
这个模块是「验证正确率」这件事的**最终出口**。它出错的表现不是崩溃，
而是给出一个看起来合理、实际上无意义的百分比。所以两件事都要钉：

1. **数值**：wilson / p 值 / 显著性门槛 / 置换零分布，与 tianji dump 出来的
   参考值逐个相等（容差 1e-12）。fixture 由 tianji 自身模块产出
   （`dump-stats.mjs`），不是在本项目里重算的。
2. **结构**：报告里必须永远带着基线、置信区间、p 值、门槛、样本充足性标记。
   不存在"只给一个百分比"的形态——那是这个功能最容易骗人的地方。

★ 一处**声明过的**文案分叉 ★
tianji 的显著档 note 结尾是「（见回测评估报告）」——它把置换检验挂在一个独立的
回测评估模块里。本项目没有那个模块（置换检验的结果就在同一份报告的
`permutation` 字段里），照抄会让界面指向不存在的页面。
测试里把这一处替换**显式写出来**，并断言"恰好只有这一处"：
其它任何字面漂移都会让用例失败。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from forecast import stats as st  # noqa: E402

GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "forecast_stats_golden.json").read_text("utf-8")
)

# 声明过的唯一一处文案分叉：tianji 指向独立模块，本项目指向本报告的字段。
DECLARED_NOTE_SUBSTITUTION = ("（见回测评估报告）", "（见 permutation 字段）")


# ─────────────────────────── ① 基础量 ───────────────────────────


def test_min_sample_constant():
    assert st.MIN_SAMPLE_FOR_PERCENT == GOLDEN["minSampleForPercent"] == 120


@pytest.mark.parametrize("row", GOLDEN["sampleSufficient"], ids=lambda r: f"n={r['n']}")
def test_sample_sufficient(row):
    assert st.sample_sufficient(row["n"]) is row["value"]


@pytest.mark.parametrize("row", GOLDEN["logChoose"], ids=lambda r: f"C({r['n']},{r['k']})")
def test_log_choose(row):
    """logChoose 是二项检验的底座：它错了，p 值会整体偏移而不报错。"""
    assert st.log_choose(row["n"], row["k"]) == pytest.approx(row["value"], abs=1e-9)


@pytest.mark.parametrize("row", GOLDEN["binomial"], ids=lambda r: f"n={r['n']}")
def test_significance_threshold(row):
    """门槛随样本量变化：n=60 要 61.7%，n=120 要 58.3%，n=250 要 55.2%。

    这条不是"更严格"或"更宽松"，而是"样本越多、越小的优势也能被确认"。
    把它写死成某个常数会让报告在早期样本上假装显著、在后期样本上假装没优势。
    """
    assert st.significance_threshold(row["n"]) == pytest.approx(row["threshold"], abs=1e-12)


@pytest.mark.parametrize("row", GOLDEN["binomial"], ids=lambda r: f"n={r['n']}")
def test_wilson_interval(row):
    for w in row["wilson"]:
        ci = st.wilson_interval(w["hits"], row["n"])
        assert ci["low"] == pytest.approx(w["low"], abs=1e-12)
        assert ci["high"] == pytest.approx(w["high"], abs=1e-12)


@pytest.mark.parametrize("row", GOLDEN["binomial"], ids=lambda r: f"n={r['n']}")
def test_binomial_tail_p_value(row):
    for pv in row["pValues"]:
        got = st.binomial_tail_p_value(row["n"], pv["hits"])
        assert got == pytest.approx(pv["p"], abs=1e-12)


def test_threshold_anchor_values():
    """三个锚点写死在用例里，任何人改门槛都会立刻看到这三个数变化。

    ⚠ 这里 250 档是 **55.6%**，不是 55.2%。移植笔记里曾把它记成 55.2%，
    对账时被黄金向量纠正——记数不能凭印象，只能以 fixture 为准。
    """
    assert round(st.significance_threshold(60) * 100, 1) == 61.7
    assert round(st.significance_threshold(120) * 100, 1) == 58.3
    assert round(st.significance_threshold(250) * 100, 1) == 55.6
    # 门槛必须随样本量单调不增：样本越多，越小的优势也能被确认
    thresholds = [st.significance_threshold(n) for n in (60, 120, 250, 500, 1000)]
    assert thresholds == sorted(thresholds, reverse=True)


def test_wilson_interval_is_conservative_at_edges():
    """极端命中数下置信区间不能越界（Wilson 的存在意义就是修正 Wald 的越界）。"""
    for hits, n in [(0, 30), (30, 30), (1, 5), (0, 5)]:
        ci = st.wilson_interval(hits, n)
        assert 0.0 <= ci["low"] <= ci["high"] <= 1.0


# ─────────────────────────── ② 报告 ───────────────────────────


@pytest.mark.parametrize("entry", GOLDEN["reports"], ids=lambda e: e["name"])
def test_accuracy_report_matches(entry):
    outcomes = [
        {"direction": o["direction"], "actual_direction": o["actualDirection"]}
        for o in entry["outcomes"]
    ]
    got = st.build_accuracy_report("000001.SH", outcomes, 0)
    x = entry["report"]
    assert got["n"] == x["n"]
    assert got["hits"] == x["hits"]
    assert got["rate"] == pytest.approx(x["rate"], abs=1e-12)
    assert got["ci"]["low"] == pytest.approx(x["ci"]["low"], abs=1e-12)
    assert got["ci"]["high"] == pytest.approx(x["ci"]["high"], abs=1e-12)
    assert got["p_value"] == pytest.approx(x["pValue"], abs=1e-12)
    assert got["baselines"]["random"] == x["baselines"]["random"]
    assert got["baselines"]["always_up"] == pytest.approx(x["baselines"]["alwaysUp"], abs=1e-12)
    assert got["baselines"]["always_down"] == pytest.approx(x["baselines"]["alwaysDown"], abs=1e-12)
    assert got["significant"] is x["significant"]
    assert got["threshold"] == pytest.approx(x["threshold"], abs=1e-12)
    assert got["flat_count"] == x["flatCount"]
    assert got["sample_sufficient"] is x["sampleSufficient"]

    want_note = x["note"].replace(*DECLARED_NOTE_SUBSTITUTION)
    assert got["note"] == want_note


def test_report_never_exposes_a_bare_percentage():
    """报告的字段集合是**固定的**：不可能出现"只有百分比"的形态。

    这个断言防的是将来有人为了做个小卡片而加一个 `{"rate": 0.58}` 的便捷接口。
    一旦存在那种接口，界面上就一定会有地方只显示它。
    """
    got = st.build_accuracy_report("000001.SH", [
        {"direction": "up", "actual_direction": "up"},
        {"direction": "down", "actual_direction": "down"},
    ], 0)
    assert set(got) >= {
        "symbol", "window_days", "n", "hits", "rate", "ci", "p_value",
        "baselines", "significant", "threshold", "flat_count", "sample_sufficient", "note",
    }


def test_flat_on_either_side_is_excluded_from_denominator():
    """平盘口径必须**对称**：任一方为平盘都不计入。

    只排除"实际平盘"而把"预测平盘"计入分母，会让模型因偶尔输出 flat 被无端扣分；
    反过来只排除"预测平盘"则诱导调参去猜平盘。两种偏差都不可接受。
    """
    # 预测平盘 × 实际涨：剔除
    r1 = st.build_accuracy_report("X", [{"direction": "flat", "actual_direction": "up"}], 0)
    assert r1["n"] == 0 and r1["flat_count"] == 1
    # 预测涨 × 实际平盘：同样剔除
    r2 = st.build_accuracy_report("X", [{"direction": "up", "actual_direction": "flat"}], 0)
    assert r2["n"] == 0 and r2["flat_count"] == 1
    # 只有双方都是涨/跌才进分母
    r3 = st.build_accuracy_report("X", [{"direction": "up", "actual_direction": "up"}], 0)
    assert r3["n"] == 1 and r3["hits"] == 1 and r3["flat_count"] == 0


def test_always_up_baseline_is_always_reported():
    """「永远猜涨」基线是必须项：A 股指数长期有上行倾向，
    一个只会猜涨的模型稳定能拿到 52%~55%。报告里不给出这个数，
    用户就无法判断 55% 到底是本事还是偏置。"""
    outcomes = [{"direction": "up", "actual_direction": "up"} for _ in range(7)]
    outcomes += [{"direction": "down", "actual_direction": "down"} for _ in range(3)]
    got = st.build_accuracy_report("X", outcomes, 0)
    assert got["baselines"]["always_up"] == pytest.approx(0.7, abs=1e-12)
    assert got["baselines"]["always_down"] == pytest.approx(0.3, abs=1e-12)


def test_sample_size_is_carried_into_the_note():
    """note 里必须出现样本量——一个不带 n 的百分比没法判断可信度。"""
    small = st.build_accuracy_report(
        "X", [{"direction": "up", "actual_direction": "up"}] * 10, 0
    )
    assert "10 个交易日" in small["note"]
    assert str(st.MIN_SAMPLE_FOR_PERCENT) in small["note"]


# ─────────────────────────── ③ 置换检验 ───────────────────────────


@pytest.mark.parametrize("entry", GOLDEN["permutations"], ids=lambda e: e["name"])
def test_permutation_test_matches(entry):
    """零分布逐项对齐：均值 / 标准差 / P95 / p 值 / 超出量。

    置换检验是唯一能回答「是不是只靠偏置」的工具。它与 tianji 的零分布必须
    逐个相等，否则"跑赢了基线"这句话就建立在两套不同的随机数上。
    """
    got = st.permutation_test(entry["predicted"], entry["actual"], 2000, 20260912)
    want = entry["result"]
    assert got["iterations"] == want["iterations"]
    assert got["observed_hits"] == want["observedHits"]
    for py_key, ts_key in [
        ("observed_rate", "observedRate"), ("null_mean", "nullMean"),
        ("null_std", "nullStd"), ("null_p95", "nullP95"),
        ("p_value", "pValue"), ("edge", "edge"),
    ]:
        assert got[py_key] == pytest.approx(want[ts_key], abs=1e-9), f"{ts_key} 不一致"


def test_permutation_covers_both_significant_and_not():
    """fixture 里必须同时存在显著与不显著两档——否则这个检验的判别力没被验证。"""
    ps = [e["result"]["pValue"] for e in GOLDEN["permutations"]]
    assert any(p < 0.05 for p in ps), "缺少显著样本"
    assert any(p >= 0.05 for p in ps), "缺少不显著样本"


def test_permutation_p_value_never_zero():
    """p 值用 (count+1)/(iterations+1)，因此**最小值为 1/(N+1)，永不为 0**。

    报 p = 0 是在宣称一个蒙特卡洛方法算不出来的结论：随机重排永远无法排除
    "更极端的情况存在"。

    构造要点：预测与实际**都必须同时含 up 与 down**，且观测命中数取到置换分布
    的上限（即两者完全一致）。此时 2000 次随机重排几乎不可能再抽到恒等排列，
    count_ge = 0 → p 取到下限。
    （若两边都是清一色 'up'，重排根本不改变任何东西，p 恒为 1 —— 那是退化情形，
    不是这里要测的东西。这是本用例第一版踩的坑。）
    """
    predicted = ["up"] * 20 + ["down"] * 10
    actual = ["up"] * 20 + ["down"] * 10
    got = st.permutation_test(predicted, actual, 500, 20260912)
    assert got["observed_hits"] == 30
    # 返回值经 js_round(..., 6)：1/501 = 0.00199600798… → 0.001996
    assert got["p_value"] == round(1 / 501, 6)
    assert got["p_value"] > 0


def test_permutation_degenerate_case_reports_p_one():
    """两边清一色时 p = 1 —— 这是**正确**结果，不是 bug。

    重排一个全 up 的序列不会产生任何变化，观测命中数必然等于零分布的每一个值。
    把它记成"显著"就等于把"没有信息"说成"有信息"。
    """
    got = st.permutation_test(["up"] * 30, ["up"] * 30, 500, 20260912)
    assert got["p_value"] == 1.0
    assert got["null_std"] == 0.0
    assert got["edge"] == 0.0


def test_permutation_is_reproducible():
    a = st.permutation_test(["up", "down"] * 30, ["up"] * 40 + ["down"] * 20, 500, 1)
    b = st.permutation_test(["up", "down"] * 30, ["up"] * 40 + ["down"] * 20, 500, 1)
    assert a == b


def test_permutation_rejects_flat():
    """平盘必须由调用方事先剔除。若放进来当 up/down 参与配对，零分布会被污染，
    而报告会照常给出一串数字。"""
    with pytest.raises(ValueError) as e:
        st.permutation_test(["up", "flat"], ["up", "down"], 200, 1)
    assert "平盘" in str(e.value)


def test_permutation_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        st.permutation_test(["up", "down"], ["up"], 200, 1)


def test_permutation_requires_min_iterations():
    with pytest.raises(ValueError):
        st.permutation_test(["up"], ["up"], 50, 1)


def test_permutation_empty_input_is_safe():
    got = st.permutation_test([], [], 200, 1)
    assert got["p_value"] == 1.0
    assert got["observed_hits"] == 0


# ─────────────────────────── ④ 多重比较校正 ───────────────────────────


def test_benjamini_hochberg_matches_golden():
    bh = GOLDEN["benjaminiHochberg"]
    assert st.benjamini_hochberg(bh["pValues"], bh["alpha"]) == bh["survive"]


def test_benjamini_hochberg_kills_the_lucky_groups():
    """按 64 卦分组时，即使卦象毫无信息量也必然有几组"特别准"——
    那不是卦灵，是在做 64 次检验。校正必须把它们按下去。"""
    p_values = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    survive = st.benjamini_hochberg(p_values, 0.05)
    assert survive[:2] == [True, True]
    assert not any(survive[2:])
    # 未校正时前 5 个都"显著"——校正的意义就在这里
    assert sum(1 for p in p_values if p < 0.05) == 5


def test_benjamini_hochberg_empty():
    assert st.benjamini_hochberg([], 0.05) == []
