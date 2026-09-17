"""统计：准确率报告与显著性检验（自 tianji `stats/` 移植）。

这个模块存在的唯一理由是：**准确率是个比例，小样本下波动极大**。
直接报一个「准确率 55%」而不给置信区间和显著性检验，等于欺骗用户。

全部使用**精确二项检验**而非正态近似：正态近似在 p 接近 0.5、n 中等时表现
尚可，但它在边缘处会给出偏乐观的结论，而我们的样本恰恰经常落在边缘区。

── 两条必须一起给的参照 ────────────────────────────────────────────

1. `always_up` 基线：A 股指数长期本身有上行倾向，「永远猜涨」往往就有
   52%–55%。拿 p=0.5 的二项检验说「显著」是**用错了零假设**——
   它证明了涨的天数多，没证明预测有信息。
2. 偏置匹配置换检验（`permutation_test`）：保持模型的涨跌比例不变、与结果
   随机重排，看命中率能到多高。观测值明显超出这个零分布才算有信息量。
"""

from __future__ import annotations

import math

from .prng import Prng
from .seed import derive_prng
from .util import js_round

MIN_SAMPLE_FOR_PERCENT = 120
DEFAULT_ITERATIONS = 10_000
# 定点置换的随机种子。固定它，报告才能被任何人复现。
DEFAULT_PERMUTATION_SEED = 20260912


# ================================================================ 二项分布


def _log_gamma(x: float) -> float:
    """Lanczos 近似的 log Γ(x)，用于精确计算组合数对数。"""
    g = 7
    c = (
        0.99999999999980993, 676.5203681218851, -1259.1392167224028,
        771.32342877765313, -176.61502916214059, 12.507343278686905,
        -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7,
    )
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _log_gamma(1 - x)
    z = x - 1
    a = c[0]
    t = z + g + 0.5
    for i in range(1, g + 2):
        a += c[i] / (z + i)
    return 0.5 * math.log(2 * math.pi) + (z + 0.5) * math.log(t) - t + math.log(a)


def log_choose(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return _log_gamma(n + 1) - _log_gamma(k + 1) - _log_gamma(n - k + 1)


def binomial_tail_p_value(n: int, k: int, p: float = 0.5) -> float:
    """单尾二项检验：P(X >= k)，X ~ B(n, p)。「这么高的准确率是碰巧吗」。"""
    if not isinstance(n, int) or n <= 0:
        return 1.0
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    log_p = math.log(p)
    log_q = math.log(1 - p)
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(log_choose(n, i) + i * log_p + (n - i) * log_q)
    return min(1.0, max(0.0, total))


def significance_threshold(n: int, p: float = 0.5, alpha: float = 0.05) -> float:
    """达到 95% 统计显著（单尾）所需的最小命中数，返回对应的准确率门槛。

    这解释了为什么「准确率 55%」在小样本下毫无意义：
        n=60  → 需要 61.7%
        n=120 → 需要 58.3%
        n=250 → 需要 55.2%
    """
    if n <= 0:
        return 1.0
    for k in range(math.ceil(n * p), n + 1):
        if binomial_tail_p_value(n, k, p) < alpha:
            return k / n
    return 1.0


def wilson_interval(hits: int, n: int, z: float = 1.96) -> dict:
    """Wilson 置信区间（95%）。

    选 Wilson 而非 Wald 的理由：命中数接近 0 或 n 时，Wald 区间会越出 [0,1]
    并给出宽度为 0 的荒谬结果，而本功能在早期恰恰经常样本极少。
    """
    if n <= 0:
        return {"low": 0.0, "high": 1.0}
    phat = hits / n
    denom = 1 + (z * z) / n
    center = (phat + (z * z) / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat)) / n + (z * z) / (4 * n * n))) / denom
    return {
        "low": max(0.0, center - margin),
        "high": min(1.0, center + margin),
    }


def sample_sufficient(n: int) -> bool:
    """样本量是否足以展示百分比（tianji module-design §7.5 硬约束）。"""
    return n >= MIN_SAMPLE_FOR_PERCENT


# ================================================================ 准确率报告


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _round4(x: float) -> float:
    return js_round(x, 4)


def build_accuracy_report(
    symbol: str,
    outcomes: list[dict],
    window_days: int = 0,
) -> dict:
    """准确率报告。

    这个函数是「验证正确率」这件事的最终出口，因此它刻意**不允许**只输出
    一个百分比——返回值里必然包含基线、置信区间、p 值、门槛和样本充足性判断。
    接口层面就把「误导性展示」的可能性堵死。

    @param outcomes 每项形如 `{"direction": "up", "actual_direction": "down" | None}`
    """
    settled = [o for o in outcomes if o.get("actual_direction") is not None]

    up = 0
    down = 0
    flat_count = 0
    hits = 0

    for o in settled:
        actual = o["actual_direction"]
        # 命中口径必须与 change.hit_of() 完全一致：任一方为平盘都不计入。
        if actual == "flat" or o["direction"] == "flat":
            flat_count += 1
            continue
        if actual == "up":
            up += 1
        else:
            down += 1
        if o["direction"] == actual:
            hits += 1

    n = up + down
    rate = hits / n if n > 0 else 0.0
    ci = wilson_interval(hits, n)
    p_value = binomial_tail_p_value(n, hits) if n > 0 else 1.0
    threshold = significance_threshold(n) if n > 0 else 1.0
    sufficient = sample_sufficient(n)

    baselines = {
        "random": 0.5,
        "always_up": (up / n) if n > 0 else 0.0,
        "always_down": (down / n) if n > 0 else 0.0,
    }

    # 显著的要求是双重的：既要跨过门槛，也要 p < 0.05。
    # 两者在数学上等价，但门槛是给用户看的、p 值是给统计看的。
    significant = n > 0 and rate >= threshold and p_value < 0.05

    if not settled:
        note = "暂无已结算的预测记录，无法评估。"
    elif n == 0:
        note = f"已结算 {len(settled)} 条，但预测或实际任一方为平盘，无可用于二元判定的样本。"
    elif not sufficient:
        note = (
            f"样本不足（有效样本 {n} 个交易日，需满 {MIN_SAMPLE_FOR_PERCENT} 个才具备统计意义），"
            f"仅供参考。当前样本量下，需准确率超过 {_fmt_pct(threshold)} 才可认为优于随机。"
        )
    elif significant:
        # 措辞刻意保守：这里的 p 值检验的是「相对随机方向（50%）」，
        # 而 A 股指数长期本身有上行倾向，「永远猜涨」往往就能拿到 52%～55%。
        # 若把二项检验的结果直接说成「有预测能力」，等于在用一个错误的零假设下结论。
        note = (
            f"样本 {n} 个交易日，准确率 {_fmt_pct(rate)}，"
            f"相对「随机方向」基线 50.0% 显著（单尾二项检验 p = {p_value:.4f}，< 0.05）。"
            f"但同期「永远猜涨」基线为 {_fmt_pct(baselines['always_up'])}，"
            # ★ 与 tianji 的唯一一处**故意**文案分叉 ★
            # tianji 这里写的是「（见回测评估报告）」——它把置换检验挂在一个独立的
            # 回测评估模块里。本项目没有那个模块：置换检验的结果就在**同一份报告**
            # 的 `permutation` 字段里（见 service.accuracy_payload）。照抄那句会
            # 让界面指向一个不存在的页面，所以改为指向实际存在的字段名。
            # 这是**声明过的**分叉，由 tests/test_forecast_stats.py 显式钉成"恰好一处"，
            # 其它任何字面漂移都会让测试失败。
            "是否真正跑赢基线须以偏置匹配置换检验为准（见 permutation 字段）。"
        )
    else:
        note = (
            f"样本 {n} 个交易日，准确率 {_fmt_pct(rate)}，"
            f"未达到当前样本量所需的 {_fmt_pct(threshold)}（p = {p_value:.4f}，"
            f"「永远猜涨」基线 {_fmt_pct(baselines['always_up'])}）。当前结果不能说明优于随机。"
        )

    return {
        "symbol": symbol,
        "window_days": window_days,
        "n": n,
        "hits": hits,
        "rate": rate,
        "ci": {"low": _round4(ci["low"]), "high": _round4(ci["high"])},
        "p_value": _round4(p_value),
        "baselines": {
            "random": baselines["random"],
            "always_up": _round4(baselines["always_up"]),
            "always_down": _round4(baselines["always_down"]),
        },
        "significant": significant,
        "threshold": _round4(threshold),
        "flat_count": flat_count,
        "sample_sufficient": sufficient,
        "note": note,
    }


# ================================================================ 置换检验


def _shuffle_in_place(arr: list, prng: Prng) -> None:
    """Fisher-Yates，用确定性 PRNG，保证同种子同结果。"""
    for i in range(len(arr) - 1, 0, -1):
        j = prng.next_below(i + 1)
        arr[i], arr[j] = arr[j], arr[i]


def permutation_test(
    predicted: list[str],
    actual: list[str],
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_PERMUTATION_SEED,
) -> dict:
    """偏置匹配置换检验。

    零假设：「预测与结果相互独立，但模型保持自己的涨跌偏好」。
    保持模型的涨跌比例不变、与结果随机重排，看命中率能到多高。
    这样得到的零分布天然包含偏置，观测值必须明显超出它才算有信息量。

    p 值用 `(count + 1) / (iterations + 1)` 而非 `count / iterations`：
    后者会给出 p = 0，而蒙特卡洛方法永远无法排除"更极端的情况存在"，
    报 0 是在宣称一个算不出来的结论。
    """
    n = len(predicted)
    if n != len(actual):
        raise ValueError(f"预测序列长度 {n} 与实际序列长度 {len(actual)} 不一致")
    if not isinstance(iterations, int) or iterations < 100:
        raise ValueError(f"置换迭代次数至少 100，收到 {iterations}")
    for label, series in (("预测", predicted), ("实际", actual)):
        for d in series:
            if d == "flat":
                raise ValueError(
                    f"{label}序列含平盘。平盘必须在进入检验前剔除——"
                    "把它当作 up/down 参与配对会污染零分布。"
                )

    if n == 0:
        return {
            "iterations": iterations, "observed_hits": 0, "observed_rate": 0.0,
            "null_mean": 0.0, "null_std": 0.0, "null_p95": 0.0, "p_value": 1.0, "edge": 0.0,
        }

    observed_hits = sum(1 for a, b in zip(predicted, actual) if a == b)
    _, prng = derive_prng(f"permutation|{seed}|{n}", "backtest")

    work = list(actual)
    null_hits: list[int] = [0] * iterations
    total = 0
    total_sq = 0
    count_ge = 0
    for it in range(iterations):
        _shuffle_in_place(work, prng)
        hits = sum(1 for a, b in zip(predicted, work) if a == b)
        null_hits[it] = hits
        total += hits
        total_sq += hits * hits
        if hits >= observed_hits:
            count_ge += 1

    null_mean = total / iterations
    variance = max(0.0, total_sq / iterations - null_mean * null_mean)
    ordered = sorted(null_hits)
    p95_index = min(iterations - 1, int(iterations * 0.95))

    return {
        "iterations": iterations,
        "observed_hits": observed_hits,
        "observed_rate": js_round(observed_hits / n, 6),
        "null_mean": js_round(null_mean / n, 6),
        "null_std": js_round(math.sqrt(variance) / n, 6),
        "null_p95": js_round(ordered[p95_index] / n, 6),
        "p_value": js_round((count_ge + 1) / (iterations + 1), 6),
        "edge": js_round((observed_hits - null_mean) / n, 6),
    }


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg 多重比较校正（控制 FDR）。

    按卦分组统计时，即使卦象毫无信息量，也几乎必然有几组"特别准"——
    这不是卦灵，是在做多次检验。校正前看起来像发现了规律，校正后才是真相。
    """
    m = len(p_values)
    survive = [False] * m
    if m == 0:
        return survive
    order = sorted(range(m), key=lambda i: p_values[i])
    last = -1
    for r, idx in enumerate(order):
        if p_values[idx] <= ((r + 1) / m) * alpha:
            last = r
    for r in range(last + 1):
        survive[order[r]] = True
    return survive
