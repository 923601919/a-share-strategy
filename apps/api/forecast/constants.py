"""预测引擎的基础常量与静态数据表（自 tianji `packages/core/src` 移植）。

移植原则（与源头一致，不要"顺手简化"）：

1. **爻位一律 1-based、自下而上**；数组顺序也是自下而上（`yaos[0]` 是初爻）。
2. **地支索引 0=子 … 11=亥**；六十甲子序号 0=甲子 … 59=癸亥。
3. 环状索引一律走 `modulo`（Python 的 `%` 对正模已恒为非负，等价）。
4. 数据表**逐项列全**，不用生成规则 —— 卦名、纳甲地支手工录入极易出错，
   而错误一旦进入排盘就会静默传播（用户看到一张自洽的错盘）。
5. 六十四卦表由**两个独立来源交叉生成**（卦名矩阵 × 京房八宫卦序），
   并做结构校验（卦名前两字必须等于上下卦自然象）。这是源头的防错机制，
   移植时整体保留：它在导入期就会炸，而不是等到某天算出一张错盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# ================================================================ 干支

TIAN_GAN: tuple[str, ...] = ("甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸")
DI_ZHI: tuple[str, ...] = (
    "子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥",
)

GAN_ELEMENT: dict[str, str] = {
    "甲": "wood", "乙": "wood",
    "丙": "fire", "丁": "fire",
    "戊": "earth", "己": "earth",
    "庚": "metal", "辛": "metal",
    "壬": "water", "癸": "water",
}

ZHI_ELEMENT: dict[str, str] = {
    "子": "water", "丑": "earth", "寅": "wood", "卯": "wood",
    "辰": "earth", "巳": "fire", "午": "fire", "未": "earth",
    "申": "metal", "酉": "metal", "戌": "earth", "亥": "water",
}

WUXING_NAME: dict[str, str] = {
    "wood": "木", "fire": "火", "earth": "土", "metal": "金", "water": "水",
}

LIUQIN_NAME: dict[str, str] = {
    "brother": "兄弟", "parent": "父母", "child": "子孙",
    "official": "官鬼", "wealth": "妻财",
}

VIGOR_NAME: dict[str, str] = {
    "wang": "旺", "xiang": "相", "xiu": "休", "qiu": "囚", "si": "死",
}

GENERATES: dict[str, str] = {
    "wood": "fire", "fire": "earth", "earth": "metal",
    "metal": "water", "water": "wood",
}

OVERCOMES: dict[str, str] = {
    "wood": "earth", "earth": "water", "water": "fire",
    "fire": "metal", "metal": "wood",
}


def modulo(n: int, m: int) -> int:
    """取模，结果恒为非负。环状索引一律用它（对应 tianji internal/assert.ts）。"""
    return n % m


def gan_index(gan: str) -> int:
    try:
        return TIAN_GAN.index(gan)
    except ValueError as exc:
        raise ValueError(f"非法天干: {gan}") from exc


def zhi_index(zhi: str) -> int:
    try:
        return DI_ZHI.index(zhi)
    except ValueError as exc:
        raise ValueError(f"非法地支: {zhi}") from exc


def gan_of(sexagenary: int) -> str:
    return TIAN_GAN[modulo(sexagenary, 10)]


def zhi_of(sexagenary: int) -> str:
    return DI_ZHI[modulo(sexagenary, 12)]


def sexagenary_of(gan: str, zhi: str) -> int:
    """由天干、地支求六十甲子序号（0–59）。干支阴阳不匹配时抛错。"""
    g = gan_index(gan)
    z = zhi_index(zhi)
    if modulo(g - z, 2) != 0:
        raise ValueError(f"干支阴阳不匹配: {gan}{zhi}")
    for n in range(60):
        if n % 10 == g and n % 12 == z:
            return n
    raise ValueError(f"无法求解六十甲子序号: {gan}{zhi}")


def void_branches_of(sexagenary: int) -> tuple[str, str]:
    """旬空：由六十甲子序号推出所在旬的两个空亡地支。"""
    xun_head = sexagenary - modulo(sexagenary, 10)
    xun_head_zhi = modulo(xun_head, 12)
    return (
        DI_ZHI[modulo(xun_head_zhi + 10, 12)],
        DI_ZHI[modulo(xun_head_zhi + 11, 12)],
    )


def hour_branch_of(hour: int, minute: int = 0) -> int:
    """由小时数（0–23）取时支索引。23 点已属次日子时。"""
    if not 0 <= hour <= 23:
        raise ValueError(f"小时越界: {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"分钟越界: {minute}")
    return modulo((hour + 1) // 2, 12)


# ================================================================ 五行生克


def relation_of(me: str, other: str) -> str:
    """六亲判定。以 `me`（卦宫五行）为「我」判断 `other` 与我的关系。

    同我=兄弟 · 生我=父母 · 我生=子孙 · 克我=官鬼 · 我克=妻财
    """
    if me == other:
        return "brother"
    if GENERATES[other] == me:
        return "parent"
    if GENERATES[me] == other:
        return "child"
    if OVERCOMES[other] == me:
        return "official"
    if OVERCOMES[me] == other:
        return "wealth"
    raise ValueError(f"六亲判定失败: me={me} other={other}")


def vigor_of(month_element: str, target: str) -> str:
    """旺衰判定。以月建（月令）五行为基准：

    当令者旺 · 令生者相 · 生令者休 · 克令者囚 · 令克者死
    """
    if target == month_element:
        return "wang"
    if GENERATES[month_element] == target:
        return "xiang"
    if GENERATES[target] == month_element:
        return "xiu"
    if OVERCOMES[target] == month_element:
        return "qiu"
    if OVERCOMES[month_element] == target:
        return "si"
    raise ValueError(f"旺衰判定失败: month={month_element} target={target}")


def is_chong(a: int, b: int) -> bool:
    """六冲：相隔六位。子午、丑未、寅申、卯酉、辰戌、巳亥。"""
    return modulo(a - b, 12) == 6


def is_liu_he(a: int, b: int) -> bool:
    """六合：子丑、寅亥、卯戌、辰酉、巳申、午未。索引之和 ≡ 1 (mod 12)。"""
    if a == b:
        return False
    return modulo(a + b, 12) == 1


# ================================================================ 八卦


@dataclass(frozen=True)
class Trigram:
    id: str
    name: str
    symbol: str
    xiantian_number: int
    lines: tuple[str, str, str]  # 自下而上：索引 0 是初爻
    element: str
    direction: str
    family: str


TRIGRAM_IDS: tuple[str, ...] = ("qian", "dui", "li", "zhen", "xun", "kan", "gen", "kun")

TRIGRAMS: tuple[Trigram, ...] = (
    Trigram("qian", "乾", "☰", 1, ("yang", "yang", "yang"), "metal", "西北", "父"),
    Trigram("dui", "兑", "☱", 2, ("yang", "yang", "yin"), "metal", "西", "少女"),
    Trigram("li", "离", "☲", 3, ("yang", "yin", "yang"), "fire", "南", "中女"),
    Trigram("zhen", "震", "☳", 4, ("yang", "yin", "yin"), "wood", "东", "长男"),
    Trigram("xun", "巽", "☴", 5, ("yin", "yang", "yang"), "wood", "东南", "长女"),
    Trigram("kan", "坎", "☵", 6, ("yin", "yang", "yin"), "water", "北", "中男"),
    Trigram("gen", "艮", "☶", 7, ("yin", "yin", "yang"), "earth", "东北", "少男"),
    Trigram("kun", "坤", "☷", 8, ("yin", "yin", "yin"), "earth", "西南", "母"),
)

TRIGRAM_BY_ID: dict[str, Trigram] = {t.id: t for t in TRIGRAMS}


def trigram_from_lines(lines: Sequence[bool]) -> Trigram:
    """由三爻（自下而上，阴阳布尔）反查单卦。"""
    want = tuple("yang" if b else "yin" for b in lines)
    for t in TRIGRAMS:
        if t.lines == want:
            return t
    raise ValueError(f"无法识别的卦画: {''.join('1' if b else '0' for b in lines)}")


# ================================================================ 六十四卦

TRIGRAM_NATURE: dict[str, str] = {
    "qian": "天", "dui": "泽", "li": "火", "zhen": "雷",
    "xun": "风", "kan": "水", "gen": "山", "kun": "地",
}

# A. 卦名矩阵。行 = 上卦，列 = 下卦（列顺序同 TRIGRAM_IDS）
NAME_TABLE: tuple[tuple[str, ...], ...] = (
    ("乾为天", "天泽履", "天火同人", "天雷无妄", "天风姤", "天水讼", "天山遁", "天地否"),
    ("泽天夬", "兑为泽", "泽火革", "泽雷随", "泽风大过", "泽水困", "泽山咸", "泽地萃"),
    ("火天大有", "火泽睽", "离为火", "火雷噬嗑", "火风鼎", "火水未济", "火山旅", "火地晋"),
    ("雷天大壮", "雷泽归妹", "雷火丰", "震为雷", "雷风恒", "雷水解", "雷山小过", "雷地豫"),
    ("风天小畜", "风泽中孚", "风火家人", "风雷益", "巽为风", "风水涣", "风山渐", "风地观"),
    ("水天需", "水泽节", "水火既济", "水雷屯", "水风井", "坎为水", "水山蹇", "水地比"),
    ("山天大畜", "山泽损", "山火贲", "山雷颐", "山风蛊", "山水蒙", "艮为山", "山地剥"),
    ("地天泰", "地泽临", "地火明夷", "地雷复", "地风升", "地水师", "地山谦", "坤为地"),
)

# B. 京房八宫卦序。索引即代数：0=八纯 1–5=一世至五世 6=游魂 7=归魂
PALACE_TABLE: dict[str, tuple[str, ...]] = {
    "qian": ("乾为天", "天风姤", "天山遁", "天地否", "风地观", "山地剥", "火地晋", "火天大有"),
    "kan": ("坎为水", "水泽节", "水雷屯", "水火既济", "泽火革", "雷火丰", "地火明夷", "地水师"),
    "gen": ("艮为山", "山火贲", "山天大畜", "山泽损", "火泽睽", "天泽履", "风泽中孚", "风山渐"),
    "zhen": ("震为雷", "雷地豫", "雷水解", "雷风恒", "地风升", "水风井", "泽风大过", "泽雷随"),
    "xun": ("巽为风", "风天小畜", "风火家人", "风雷益", "天雷无妄", "火雷噬嗑", "山雷颐", "山风蛊"),
    "li": ("离为火", "火山旅", "火风鼎", "火水未济", "山水蒙", "风水涣", "天水讼", "天火同人"),
    "kun": ("坤为地", "地雷复", "地泽临", "地天泰", "雷天大壮", "泽天夬", "水天需", "水地比"),
    "dui": ("兑为泽", "泽水困", "泽地萃", "泽山咸", "水山蹇", "地山谦", "雷山小过", "雷泽归妹"),
}

# 世爻位置（按代数）。应爻恒为「世爻顺行三位」。
SHI_POSITION: tuple[int, ...] = (6, 1, 2, 3, 4, 5, 4, 3)

PALACE_ELEMENT: dict[str, str] = {
    "qian": "metal", "dui": "metal", "li": "fire",
    "zhen": "wood", "xun": "wood", "kan": "water",
    "gen": "earth", "kun": "earth",
}

PALACE_NAME: dict[str, str] = {
    "qian": "乾宫", "dui": "兑宫", "li": "离宫", "zhen": "震宫",
    "xun": "巽宫", "kan": "坎宫", "gen": "艮宫", "kun": "坤宫",
}

GENERATION_LABELS: tuple[str, ...] = (
    "本宫卦", "一世卦", "二世卦", "三世卦", "四世卦", "五世卦", "游魂卦", "归魂卦",
)


@dataclass(frozen=True)
class HexagramMeta:
    upper: str
    lower: str
    name: str
    short_name: str
    palace: str
    generation: int
    shi_position: int
    ying_position: int


def _short_name_of(full_name: str) -> str:
    # 八纯卦形如「乾为天」
    if len(full_name) >= 3 and full_name[1] == "为":
        return full_name[:1]
    return full_name[2:]


def _build_hexagrams() -> tuple[dict[str, HexagramMeta], dict[str, HexagramMeta]]:
    """构造六十四卦表，并做三重交叉校验（源头的防错机制，整体保留）。"""
    name_to_palace: dict[str, str] = {}
    name_to_generation: dict[str, int] = {}
    for palace, names in PALACE_TABLE.items():
        for generation, name in enumerate(names):
            if name in name_to_palace:
                raise ValueError(f"六十四卦数据错误：卦名重复 —— 「{name}」同时出现在多个卦宫")
            name_to_palace[name] = palace
            name_to_generation[name] = generation

    by_key: dict[str, HexagramMeta] = {}
    by_name: dict[str, HexagramMeta] = {}
    for upper in TRIGRAM_IDS:
        for lower in TRIGRAM_IDS:
            ui, li = TRIGRAM_IDS.index(upper), TRIGRAM_IDS.index(lower)
            name = NAME_TABLE[ui][li]

            if upper == lower:
                expected = f"{TRIGRAM_BY_ID[upper].name}为{TRIGRAM_NATURE[upper]}"
                if name != expected:
                    raise ValueError(f"卦名结构错误: {upper}/{lower} 期望「{expected}」，实际「{name}」")
            else:
                expected_prefix = f"{TRIGRAM_NATURE[upper]}{TRIGRAM_NATURE[lower]}"
                if not name.startswith(expected_prefix):
                    raise ValueError(
                        f"卦名结构错误: {upper}/{lower} 期望以「{expected_prefix}」开头，实际「{name}」"
                    )

            palace = name_to_palace.get(name)
            if palace is None:
                raise ValueError(f"卦名「{name}」不在京房八宫卦序中（八宫表与卦名矩阵不一致）")
            generation = name_to_generation[name]
            shi = SHI_POSITION[generation]
            meta = HexagramMeta(
                upper=upper,
                lower=lower,
                name=name,
                short_name=_short_name_of(name),
                palace=palace,
                generation=generation,
                shi_position=shi,
                ying_position=((shi - 1 + 3) % 6) + 1,
            )
            by_key[f"{upper}/{lower}"] = meta
            if name in by_name:
                raise ValueError(f"卦名重复: {name}")
            by_name[name] = meta

    if len(by_key) != 64:
        raise ValueError(f"六十四卦构造失败：得到 {len(by_key)} 个，期望 64")
    for name in name_to_palace:
        if name not in by_name:
            raise ValueError(f"京房八宫卦序中的「{name}」在卦名矩阵中不存在")
    return by_key, by_name


HEXAGRAM_BY_KEY, HEXAGRAM_BY_NAME = _build_hexagrams()


def hexagram_of(upper: str, lower: str) -> HexagramMeta:
    meta = HEXAGRAM_BY_KEY.get(f"{upper}/{lower}")
    if meta is None:
        raise ValueError(f"未知卦: {upper}/{lower}")
    return meta


def hexagram_by_name(name: str) -> HexagramMeta:
    meta = HEXAGRAM_BY_NAME.get(name)
    if meta is None:
        raise ValueError(f"未知卦名: {name}")
    return meta


def palace_element_of(meta: HexagramMeta) -> str:
    """卦宫五行（六亲判定的「我」）。"""
    return PALACE_ELEMENT[meta.palace]


def describe_hexagram(meta: HexagramMeta) -> str:
    """卦的完整显示名，如 "地天泰（坤宫三世卦）"。"""
    return f"{meta.name}（{PALACE_NAME[meta.palace]}{GENERATION_LABELS[meta.generation]}）"


# ================================================================ 纳甲

@dataclass(frozen=True)
class NajiaEntry:
    inner_gan: str
    outer_gan: str
    inner_zhi: tuple[str, str, str]  # 初、二、三爻
    outer_zhi: tuple[str, str, str]  # 四、五、六爻


NAJIA: dict[str, NajiaEntry] = {
    "qian": NajiaEntry("甲", "壬", ("子", "寅", "辰"), ("午", "申", "戌")),
    "kun": NajiaEntry("乙", "癸", ("未", "巳", "卯"), ("丑", "亥", "酉")),
    "zhen": NajiaEntry("庚", "庚", ("子", "寅", "辰"), ("午", "申", "戌")),
    "xun": NajiaEntry("辛", "辛", ("丑", "亥", "酉"), ("未", "巳", "卯")),
    "kan": NajiaEntry("戊", "戊", ("寅", "辰", "午"), ("申", "戌", "子")),
    "li": NajiaEntry("己", "己", ("卯", "丑", "亥"), ("酉", "未", "巳")),
    "gen": NajiaEntry("丙", "丙", ("辰", "午", "申"), ("戌", "子", "寅")),
    "dui": NajiaEntry("丁", "丁", ("巳", "卯", "丑"), ("亥", "酉", "未")),
}

for _tid in TRIGRAM_IDS:
    if _tid not in NAJIA:
        raise ValueError(f"NAJIA: 数据表缺失 {_tid}")


def najia_of(trigram: str, position: int) -> tuple[str, str]:
    """取某卦某一爻（1–6）的纳甲干支。1–3 爻取内卦，4–6 爻取外卦。"""
    entry = NAJIA[trigram]
    if not 1 <= position <= 6:
        raise ValueError(f"爻位越界: {position}（应为 1–6）")
    if position <= 3:
        return entry.inner_gan, entry.inner_zhi[position - 1]
    return entry.outer_gan, entry.outer_zhi[position - 4]


# ================================================================ 六神

LIUSHEN_ORDER: tuple[str, ...] = (
    "azureDragon", "vermilionBird", "hookChen",
    "coilingSnake", "whiteTiger", "blackTortoise",
)

LIUSHEN_NAME: dict[str, str] = {
    "azureDragon": "青龙", "vermilionBird": "朱雀", "hookChen": "勾陈",
    "coilingSnake": "螣蛇", "whiteTiger": "白虎", "blackTortoise": "玄武",
}

LIUSHEN_ELEMENT: dict[str, str] = {
    "azureDragon": "wood", "vermilionBird": "fire", "hookChen": "earth",
    "coilingSnake": "earth", "whiteTiger": "metal", "blackTortoise": "water",
}

# 各日干起神的起始索引（甲0 乙0 丙1 丁1 戊2 己3 庚4 辛4 壬5 癸5）。
# 戊、己两干各占一个神，所以不能写成 floor(gan_index / 2) —— 那会把己错配成勾陈。
_START_INDEX_BY_GAN: tuple[int, ...] = (0, 0, 1, 1, 2, 3, 4, 4, 5, 5)


def deity_of(day_gan: str, position: int) -> str:
    """取某爻（1–6）的六神。"""
    gi = gan_index(day_gan)
    if not 1 <= position <= 6:
        raise ValueError(f"爻位越界: {position}")
    start = _START_INDEX_BY_GAN[gi]
    return LIUSHEN_ORDER[modulo(start + position - 1, 6)]
