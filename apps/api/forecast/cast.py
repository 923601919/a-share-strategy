"""起卦 —— 从「六个数」到「一个卦」（自 tianji `liuyao/cast.ts` 移植）。

这一步只处理卦象本身（本卦、变卦、互卦、错卦、综卦），
不涉及干支、六亲、六神（那些属于装卦，见 `dress.py`）。

三者共用的核心是 `cast_from_values`：无论是金钱卦还是预测引擎的确定性起卦，
最终都归约到「六个 6/7/8/9」。这样起卦方式就是一个纯粹的前端策略，
不影响后面的任何一环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from .constants import Trigram, hexagram_of, trigram_from_lines

YaoValue = int  # 6 | 7 | 8 | 9


@dataclass
class Yao:
    """起卦得到的原始一爻（尚未装卦）。"""

    position: int  # 1–6，自下而上
    yin_yang: str  # 'yang' | 'yin'
    value: int
    moving: bool


@dataclass
class HexagramKey:
    upper: str
    lower: str

    def as_str(self) -> str:
        return f"{self.upper}/{self.lower}"


@dataclass
class CastResult:
    method: str  # 'coin' | 'meihua' | 'manual'
    yaos: list[Yao]  # 自下而上，yaos[0] 是初爻
    original: HexagramKey
    changed: HexagramKey | None
    mutual: HexagramKey
    opposite: HexagramKey
    reversed: HexagramKey
    moving_positions: list[int]
    casting_note: str


def _is_yang(value: int) -> bool:
    return value in (7, 9)


def _is_moving(value: int) -> bool:
    return value in (6, 9)


def _trigram(flags: Sequence[bool]) -> Trigram:
    return trigram_from_lines(flags)


def _key(upper: Trigram, lower: Trigram) -> HexagramKey:
    return HexagramKey(upper.id, lower.id)


def cast_from_values(values: Sequence[int], method: str, casting_note: str) -> CastResult:
    """由六个爻值构造完整的起卦结果（`values[0]` 为初爻）。"""
    if len(values) != 6:
        raise ValueError(f"起卦需要恰好 6 个爻值，收到 {len(values)} 个")

    yaos = [
        Yao(position=i + 1, yin_yang="yang" if _is_yang(v) else "yin", value=v, moving=_is_moving(v))
        for i, v in enumerate(values)
    ]
    flags = [y.yin_yang == "yang" for y in yaos]

    lower = _trigram(flags[0:3])
    upper = _trigram(flags[3:6])
    original = _key(upper, lower)

    # 变卦：动爻反转。无动爻则无变卦
    moving_positions = [y.position for y in yaos if y.moving]
    changed: HexagramKey | None = None
    if moving_positions:
        flipped = [(not _is_yang(y.value)) if y.moving else _is_yang(y.value) for y in yaos]
        changed = _key(_trigram(flipped[3:6]), _trigram(flipped[0:3]))

    # 互卦：下卦取 2/3/4 爻，上卦取 3/4/5 爻（爻位 1-based，故索引减 1）
    mutual = _key(
        _trigram([flags[2], flags[3], flags[4]]),
        _trigram([flags[1], flags[2], flags[3]]),
    )

    # 错卦：六爻全变（阴阳相对）
    all_flipped = [not b for b in flags]
    opposite = _key(_trigram(all_flipped[3:6]), _trigram(all_flipped[0:3]))

    # 综卦：六爻倒序（原初爻变上爻）
    reversed_flags = list(reversed(flags))
    reversed_key = _key(_trigram(reversed_flags[3:6]), _trigram(reversed_flags[0:3]))

    return CastResult(
        method=method,
        yaos=yaos,
        original=original,
        changed=changed,
        mutual=mutual,
        opposite=opposite,
        reversed=reversed_key,
        moving_positions=moving_positions,
        casting_note=casting_note,
    )


_COIN_TABLE = (6, 7, 8, 9)


def cast_by_coin(random: Callable[[], float], casting_note: str) -> CastResult:
    """金钱卦（三钱法）：每爻投三枚铜钱，按「背」的数量定爻。

    0 背 → 老阴 6（动） / 1 背 → 少阳 7 / 2 背 → 少阴 8 / 3 背 → 老阳 9（动）
    概率 1/8、3/8、3/8、1/8，与传统三钱法一致。
    """
    values: list[int] = []
    for _ in range(6):
        backs = 0
        for _c in range(3):
            if random() < 0.5:
                backs += 1
        values.append(_COIN_TABLE[backs])
    return cast_from_values(values, "coin", casting_note)


def normalize_yao_value(n: int) -> int:
    """六爻数值校验：不是 6/7/8/9 就抛错，避免坏数据进入排盘。"""
    if n in (6, 7, 8, 9):
        return n
    raise ValueError(f"非法爻值 {n}，应为 6/7/8/9（老阴/少阳/少阴/老阳）")


def validate_manual_yao(values: Sequence[int]) -> list[str]:
    """手工摇卦的爻值合法性检查（供界面即时校验）。"""
    problems: list[str] = []
    if len(values) != 6:
        problems.append(f"需要 6 个爻值，当前 {len(values)} 个")
    for i, v in enumerate(values):
        if v not in (6, 7, 8, 9):
            problems.append(f"第 {i + 1} 爻的取值 {v} 非法，应为 6/7/8/9")
    return problems


def original_hexagram_name(cast: CastResult) -> str:
    return hexagram_of(cast.original.upper, cast.original.lower).name
