"""历法 → 六爻 的桥接（自 tianji `calendar/` + `service/context.ts` 移植）。

放在这一层而不是六爻引擎里，是为了守住那条纪律：
**六爻引擎不认识历法结果对象**。它只接受一个扁平的 `DivinationContext`，
由这里负责翻译。于是六爻引擎可以在完全没有历法库的环境里被单独测试。

── 关于历法库的选择与口径 ──────────────────────────────────────────

tianji 的历法层唯一外部依赖是 `lunar-typescript`（6tail）。本项目用同作者的
`lunar-python`，三处最关键的口径方法同名同义：

    年柱（立春为界）   getYearInGanZhiExact()
    月柱（节气为界）   getMonthInGanZhiExact()
    日柱               getDayInGanZhiExact()（晚子时归次日，standard-cn 口径）

★ 不迁真太阳时修正（以及这句话准确的范围）★

断卦只消费日柱干支、月柱（月建）、旬空、月建五行——**不使用时辰**
（见 `dress.py` 的 `_base_flags` 与 `rules.py` 的 `evaluate`）。

实测证据：对 8 个黄金向量做逐字段比对，`hour_branch` 在 2 个日期上与 tianji
不同（真太阳时修正把 15:00 推回未时，我们用的是钟表时辰），而**其余全部字段
逐字节一致**，包含方向、强度、六爻装卦与整条 reasoning。
单测 `test_forecast_calendar.py::test_hour_branch_does_not_reach_verdict`
把这条钉住：任意替换 `hour_branch`，判决必须一字不变。

所以这里不迁修正链路**不是简化**：一条不参与计算的修正，迁过来只会让人
以为它算了什么。`DivinationContext.hour_branch` 在语义上是「钟表时辰」，
将来若要引入时辰相关规则，必须先把它换成真太阳时口径，并 bump ENGINE_VERSION。
"""

from __future__ import annotations

from lunar_python import Solar

from .constants import (
    DI_ZHI,
    ZHI_ELEMENT,
    hour_branch_of,
    sexagenary_of,
    void_branches_of,
)
from .dress import DivinationContext, Pillar


def _to_pillar(text: str) -> Pillar:
    """把「甲子」形式的干支文本解析为 Pillar。解析失败抛错，不静默返回坏数据。"""
    if len(text) != 2:
        raise ValueError(f"非法干支文本: {text}")
    gan, zhi = text[0], text[1]
    return Pillar(gan=gan, zhi=zhi, sexagenary=sexagenary_of(gan, zhi))


def build_divination_context(
    date_iso: str,
    hour: int = 15,
    minute: int = 0,
    late_zi_hour: str = "nextDay",
) -> DivinationContext:
    """由公历日期 + 起卦时刻算出装卦所需的历法要素。

    @param date_iso  起卦当天的公历日期（`YYYY-MM-DD`）
    @param hour      起卦小时（默认 15 = 收盘时刻）
    @param late_zi_hour 晚子时日柱归属：'nextDay'（standard-cn）| 'currentDay'
    """
    year, month, day = (int(x) for x in date_iso.split("-"))
    solar = Solar.fromYmdHms(year, month, day, hour, minute, 0)
    lunar = solar.getLunar()

    # 日柱：两个流派对应库里两个方法
    day_text = (
        lunar.getDayInGanZhiExact() if late_zi_hour == "nextDay" else lunar.getDayInGanZhiExact2()
    )
    year_pillar = _to_pillar(lunar.getYearInGanZhiExact())
    month_pillar = _to_pillar(lunar.getMonthInGanZhiExact())
    day_pillar = _to_pillar(day_text)

    return DivinationContext(
        solar_date=date_iso,
        day_pillar=day_pillar,
        month_pillar=month_pillar,
        year_pillar=year_pillar,
        hour_branch=hour_branch_of(hour, minute),
        void_branches=void_branches_of(day_pillar.sexagenary),
        month_element=ZHI_ELEMENT[month_pillar.zhi],
        day_element=ZHI_ELEMENT[day_pillar.zhi],
    )


def branch_index(zhi: str) -> int:
    return DI_ZHI.index(zhi)
