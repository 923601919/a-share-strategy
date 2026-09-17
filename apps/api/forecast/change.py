"""涨跌方向判定 —— 预测与验证之间的唯一接口（自 tianji `market/change.ts` 移植）。

「预测对了没有」这个问题的全部答案，都取决于这个文件里的两个决定：

    ① 涨跌幅用哪两个价格算；
    ② 「平盘」的边界画在哪里、算不算命中。

两条都必须事先写死并对外可见，否则准确率就成了可以事后调整的东西。

── ① 价格口径（本项目：指数）────────────────────────────────────

优先使用数据源给的 `prev_close`（交易所公布的除权后前收盘价），
真实值缺失时退化为「上一根 K 线收盘价」（`basis='naive'`）。

⚠ **指数没有除权**，所以 `naive` 对指数恒等于交易所口径，可以直接用。
tianji 对**个股**会把 `naive` 的样本拦下保持待结算（除权日的方向可能反），
那条规则不适用于本功能——本项目只预测指数。若将来扩展到个股，
`judge_outcome` 必须先补上除权事件，而不是把 `naive` 当成能用：
除权日拿未复权的前收盘价当基准，会把上涨记成下跌，而**结果上完全看不出来**。

── ② 平盘口径（单位务必看清）────────────────────────────────────

`flat_threshold_pct` 的后缀是 `pct`，单位是**百分点**：
0.05 表示 0.05%（换算成比例是 0.0005）。不要传 0.0005 当百分点用——
那表示 0.0005%，会把几乎所有真实平盘都算成涨跌，样本量凭空膨胀；
反过来把 0.05 当比例用则是 5% 的平盘带，同样错得离谱。

命中判定是**对称**的：预测方向与实际方向任一方为 flat，该次预测都不计入
二元统计（返回 None）。若只排除「实际平盘」而把「预测平盘」计入分母，
模型只要偶尔输出 flat 就会被无端扣分；反过来也会诱导调参去猜平盘——
两种偏差都不可接受。
"""

from __future__ import annotations

from dataclasses import dataclass

from .profile import DEFAULT_FLAT_THRESHOLD_PCT
from .util import js_round

DIRECTIONS = ("up", "down", "flat")


@dataclass
class DailyBar:
    """一根日线。`prev_close` 为 None 表示数据源没给前收盘价。"""

    date: str
    close: float
    prev_close: float | None = None


@dataclass
class ChangeResult:
    symbol: str
    date: str
    base_date: str
    base_close: float
    close: float
    change: float
    change_ratio: float  # 小数比例（0.0005 = 0.05%）
    direction: str
    basis: str  # 'prevClose' | 'naive'
    flat_threshold_pct: float


def direction_of(change_ratio: float, flat_threshold_pct: float = DEFAULT_FLAT_THRESHOLD_PCT) -> str:
    """由涨跌幅判定方向。

    `change_ratio` 是**小数比例**，`flat_threshold_pct` 是**百分点**，
    两者数值差 100 倍，函数内部做换算。

    边界包含：恰好等于阈值的涨跌幅判为 up/down，不算平盘——
    这样「平盘区间」是一个开区间，语义上更自然，也便于测试。
    """
    if change_ratio != change_ratio or change_ratio in (float("inf"), float("-inf")):
        raise ValueError(f"涨跌幅非有限数：{change_ratio}")
    threshold = flat_threshold_pct / 100
    if abs(change_ratio) < threshold:
        return "flat"
    return "up" if change_ratio > 0 else "down"


def change_at(
    bars: list[DailyBar],
    index: int,
    flat_threshold_pct: float = DEFAULT_FLAT_THRESHOLD_PCT,
) -> ChangeResult | None:
    """计算第 `index` 根 K 线相对其前一交易日的涨跌。

    `index` 必须 ≥ 1：判定「某日涨跌」需要前一日作为基准。
    首根 K 线返回 None 而不是抛错（正常情形，不应中断整批计算）。
    """
    if not isinstance(index, int) or index < 1 or index >= len(bars):
        return None

    cur = bars[index]
    prev_bar = bars[index - 1]

    if cur.prev_close is not None and cur.prev_close > 0:
        base_close = cur.prev_close
        basis = "prevClose"
    else:
        base_close = prev_bar.close
        basis = "naive"

    if not base_close > 0:
        raise ValueError(f"基准价非正 @ {cur.date}（{base_close}），无法计算涨跌幅")

    change = cur.close - base_close
    # 保留 8 位小数，消除浮点噪声但保留足够精度。这是**小数比例**。
    change_ratio = js_round(change / base_close, 8)

    return ChangeResult(
        symbol="",
        date=cur.date,
        base_date=prev_bar.date,
        base_close=base_close,
        close=cur.close,
        change=js_round(change, 6),
        change_ratio=change_ratio,
        direction=direction_of(change_ratio, flat_threshold_pct),
        basis=basis,
        flat_threshold_pct=flat_threshold_pct,
    )


def index_by_date(bars: list[DailyBar]) -> dict[str, int]:
    """建立日期 → 下标索引。结算要按目标日反复查询，线性扫描在几千根 K 线上
    会变成明显的开销。"""
    out: dict[str, int] = {}
    for i, bar in enumerate(bars):
        out.setdefault(bar.date, i)
    return out


@dataclass
class OutcomeJudgement:
    symbol: str
    target_date: str
    base_date: str
    base_close: float
    close: float
    actual_change: float
    actual_direction: str
    basis: str
    flat_threshold_pct: float


def judge_outcome(
    symbol: str,
    target_date: str,
    bars: list[DailyBar],
    index: dict[str, int] | None = None,
    flat_threshold_pct: float = DEFAULT_FLAT_THRESHOLD_PCT,
) -> OutcomeJudgement | None:
    """判定某条预测的实际结果。

    目标日无数据时返回 None —— **不要把它当作「未命中」**。
    停牌、数据缺失都会导致 None，把它们计入分母会低估准确率。
    """
    idx = index if index is not None else index_by_date(bars)
    i = idx.get(target_date)
    if i is None:
        return None
    change = change_at(bars, i, flat_threshold_pct)
    if change is None:
        return None
    return OutcomeJudgement(
        symbol=symbol,
        target_date=target_date,
        base_date=change.base_date,
        base_close=change.base_close,
        close=change.close,
        actual_change=change.change_ratio,
        actual_direction=change.direction,
        basis=change.basis,
        flat_threshold_pct=flat_threshold_pct,
    )


def hit_of(predicted: str, actual: str) -> bool | None:
    """命中判定。

    返回 None 表示「本次不计入二元统计」，而非「未命中」。
    调用方必须区分这两种情况——这正是准确率有没有意义的分水岭。
    """
    if predicted == "flat" or actual == "flat":
        return None
    return predicted == actual


def judge_close_pair(
    base_close: float,
    close: float,
    flat_threshold_pct: float = DEFAULT_FLAT_THRESHOLD_PCT,
) -> tuple[float, str]:
    """由「基准价 + 收盘价」直接判定涨跌幅与方向（返回 `(比例, 方向)`）。

    用途只有一个：**当日收盘后、官方日线尚未发布时**，用指数实时行情
    （现价 = 当日收盘、昨收 = 前收盘）先把当天结算掉，而不是让样本多躺一天。
    次日官方日线到位后，结算记录会被 upsert 修正（`source` 字段能看出来
    用的是哪一个），因为账面上的实际方向本就该跟着数据源走。
    """
    if not base_close > 0:
        raise ValueError(f"基准价非正（{base_close}），无法计算涨跌幅")
    ratio = js_round((close - base_close) / base_close, 8)
    return ratio, direction_of(ratio, flat_threshold_pct)
