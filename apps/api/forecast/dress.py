"""装卦 —— 把卦象变成可以断的盘（自 tianji `liuyao/dress.ts` 移植）。

链条严格串行，每一步的输出是下一步的输入：

    卦象(已起好) → 定卦宫 → 纳甲 → 装六亲 → 配六神 → 定世应 → 标旬空/旺衰

纯函数：不读数据库、不发网络、不依赖 UI、不依赖历法库。
所有外部信息通过 `DivinationContext` 一次性注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .cast import CastResult, HexagramKey, Yao
from .constants import (
    DI_ZHI,
    GENERATES,
    OVERCOMES,
    ZHI_ELEMENT,
    deity_of,
    hexagram_of,
    is_chong,
    is_liu_he,
    najia_of,
    palace_element_of,
    relation_of,
    vigor_of,
)


@dataclass(frozen=True)
class Pillar:
    gan: str
    zhi: str
    sexagenary: int


@dataclass(frozen=True)
class DivinationContext:
    """装卦所需的历法要素。

    刻意不在这里引用历法结果对象 —— 六爻引擎只接受这个扁平上下文，
    由上层（`calendar_ctx`）负责从历法库翻译过来。这样六爻引擎就与历法库
    完全解耦，可以在没有历法库的环境里被单独测试。

    ⚠ `hour_branch` 目前**不参与断卦**：断卦只消费日柱干支、月柱（月建）、
    旬空与月建五行（见 `rules.py`）。保留该字段是为了让上下文完整，
    也让将来若要引入时辰相关的规则时不必改结构。
    """

    solar_date: str
    day_pillar: Pillar
    month_pillar: Pillar
    year_pillar: Pillar
    hour_branch: int
    void_branches: tuple[str, str]
    month_element: str
    day_element: str


@dataclass
class ChangedYao:
    yin_yang: str
    gan: str
    zhi: str
    element: str
    relation: str  # 六亲仍以**本卦卦宫**为「我」推得（传统做法）


@dataclass
class DressedYao:
    position: int
    yin_yang: str
    value: int
    moving: bool
    gan: str
    zhi: str
    element: str
    relation: str
    deity: str
    is_shi: bool
    is_ying: bool
    is_void: bool
    vigor: str
    flags: list[str] = field(default_factory=list)
    changed: ChangedYao | None = None


@dataclass
class DressedCore:
    key: HexagramKey
    meta: object  # HexagramMeta
    relation_base: str
    own_palace_element: str
    yaos: list[DressedYao]


@dataclass
class DressedHexagram:
    original: object  # HexagramMeta
    changed: object | None  # HexagramMeta
    changed_core: DressedCore | None
    core: DressedCore


def dress_hexagram(cast: CastResult, ctx: DivinationContext) -> DressedHexagram:
    """装一卦（含变卦）。"""
    original_meta = hexagram_of(cast.original.upper, cast.original.lower)
    # 「我」恒为本卦卦宫五行
    relation_base = palace_element_of(original_meta)

    core = _dress_core(cast.original, cast, ctx, relation_base, True)

    changed_meta = None
    changed_core = None
    if cast.changed is not None:
        changed_meta = hexagram_of(cast.changed.upper, cast.changed.lower)
        # 变卦的六亲仍以本卦卦宫为「我」，且不再向下递归（变卦不再有变卦）
        changed_core = _dress_core(cast.changed, cast, ctx, relation_base, False)

    return DressedHexagram(
        original=original_meta,
        changed=changed_meta,
        changed_core=changed_core,
        core=core,
    )


def _dress_core(
    key: HexagramKey,
    cast: CastResult,
    ctx: DivinationContext,
    relation_base: str,
    with_changed: bool,
) -> DressedCore:
    meta = hexagram_of(key.upper, key.lower)
    yaos: list[DressedYao] = []

    for position in range(1, 7):
        source: Yao = cast.yaos[position - 1]
        # 1–3 爻取下卦纳甲，4–6 爻取上卦纳甲
        trigram = key.lower if position <= 3 else key.upper
        gan, zhi = najia_of(trigram, position)
        element = ZHI_ELEMENT[zhi]
        relation = relation_of(relation_base, element)
        deity = deity_of(ctx.day_pillar.gan, position)
        is_void = zhi in ctx.void_branches
        vigor = vigor_of(ctx.month_element, element)
        flags = _base_flags(zhi, ctx, is_void)

        changed: ChangedYao | None = None
        if with_changed and source.moving and cast.changed is not None:
            c_trigram = cast.changed.lower if position <= 3 else cast.changed.upper
            c_gan, c_zhi = najia_of(c_trigram, position)
            c_element = ZHI_ELEMENT[c_zhi]
            changed = ChangedYao(
                # 动爻必反化：老阳变阴、老阴变阳
                yin_yang="yin" if source.yin_yang == "yang" else "yang",
                gan=c_gan,
                zhi=c_zhi,
                element=c_element,
                relation=relation_of(relation_base, c_element),
            )

            # 回头生克：变爻对动爻本身的作用，是断卦的关键信号
            if GENERATES[c_element] == element:
                flags.append("回头生")
            if OVERCOMES[c_element] == element:
                flags.append("回头克")
            if OVERCOMES[element] == c_element:
                flags.append("化泄")
            if is_chong(DI_ZHI.index(c_zhi), DI_ZHI.index(zhi)):
                flags.append("化冲")
            if is_liu_he(DI_ZHI.index(c_zhi), DI_ZHI.index(zhi)):
                flags.append("化合")

        yaos.append(
            DressedYao(
                position=position,
                yin_yang=source.yin_yang,
                value=source.value,
                moving=source.moving,
                gan=gan,
                zhi=zhi,
                element=element,
                relation=relation,
                deity=deity,
                is_shi=position == meta.shi_position,
                is_ying=position == meta.ying_position,
                is_void=is_void,
                vigor=vigor,
                flags=flags,
                changed=changed,
            )
        )

    return DressedCore(
        key=key,
        meta=meta,
        relation_base=relation_base,
        own_palace_element=palace_element_of(meta),
        yaos=yaos,
    )


def _base_flags(zhi: str, ctx: DivinationContext, is_void: bool) -> list[str]:
    """与月建、日辰、旬空相关的基础标记。"""
    flags: list[str] = []
    z = DI_ZHI.index(zhi)
    month = DI_ZHI.index(ctx.month_pillar.zhi)
    day = DI_ZHI.index(ctx.day_pillar.zhi)

    if is_void:
        flags.append("旬空")
    if z == month:
        flags.append("临月建")
    if z == day:
        flags.append("临日辰")
    # 月破比日破严重：月建为一月之提纲，被月建冲则整月无力
    if is_chong(z, month):
        flags.append("月破")
    if is_chong(z, day):
        flags.append("日破")
    if is_liu_he(z, day):
        flags.append("日辰合")
    if is_liu_he(z, month):
        flags.append("月建合")
    return flags


def shi_yao_of(core: DressedCore) -> DressedYao:
    for y in core.yaos:
        if y.is_shi:
            return y
    raise ValueError(f"卦 {core.meta.name} 未标记世爻，数据异常")


def ying_yao_of(core: DressedCore) -> DressedYao:
    for y in core.yaos:
        if y.is_ying:
            return y
    raise ValueError(f"卦 {core.meta.name} 未标记应爻，数据异常")


def moving_yaos_of(core: DressedCore) -> list[DressedYao]:
    return [y for y in core.yaos if y.moving]
