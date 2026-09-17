"""断卦规则包（自 tianji `forecast/rules.ts` 移植）。

⚠ 这是全功能里**最不"硬"**的一块：排盘有唯一正确答案，断卦没有。
因此设计目标不是"算得准"，而是**可解释、可版本化、可回测**：

1. 每一步加减分都产出可读的 reasoning，用户能看到"为什么判涨"；
2. 权重集中在 `RuleConfig` 里，**改动规则必须提升 RULESET_VERSION**，
   否则样本外验证就失去意义；
3. 输出的是分数而非直接给方向，方向由阈值切分——这样"强度"和"方向"
   可以分别调，也便于将来接入校准。

取用神采用传统的「求财以妻财为用神」：妻财旺相 → 利于求财 → 判涨；
妻财休囚、受克、逢空破 → 不利 → 判跌。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import GENERATES, LIUQIN_NAME, OVERCOMES, WUXING_NAME
from .dress import DressedCore, DressedHexagram, DressedYao, DivinationContext
from .util import js_fixed, js_round


@dataclass(frozen=True)
class RuleConfig:
    """断卦权重与判向阈值。

    `up_threshold` / `down_threshold` 默认同为 0.5，即**不产生"中性"方向**。
    理由是产品层面的：用户点一次「预测涨跌」期待的是一个方向，
    给出「中性」等于什么都没说；而"有多确信"由 `strength` 承载。

    若某个流派确实想表达「看不清」，把两者设为 0.55 / 0.45 即可打开中间的
    中性区间（此时 flat 会拉低命中率，是诚实的代价）。
    """

    up_threshold: float = 0.5
    down_threshold: float = 0.5
    vigor_wang: float = 0.12
    vigor_xiang: float = 0.08
    vigor_qiu: float = -0.08
    vigor_si: float = -0.12
    on_month_or_day: float = 0.08
    broken: float = -0.18
    void: float = -0.12
    generated_by_moving: float = 0.06
    overcome_by_moving: float = -0.08
    hui_tou_sheng: float = 0.08
    hui_tou_ke: float = -0.12
    self_moving: float = 0.04
    no_wealth_in_hexagram: float = -0.15
    brother_moving: float = -0.1
    child_prosperous: float = 0.05


DEFAULT_RULE_CONFIG = RuleConfig()


@dataclass
class Verdict:
    direction: str  # 'up' | 'down' | 'flat'
    strength: float  # 0–1，距中性点 0.5 的偏离程度
    score: float  # 原始得分 0–1
    reasoning: list[str]
    yong_shen: dict | None


def _pick_yong_shen(wealth: list[DressedYao]) -> DressedYao:
    """取用神。若有多个妻财爻，按优先级选一：

    ① 发动之财（动则有力，最直接）
    ② 临世应之财（与求测者直接相关）
    ③ 旺相之财
    """
    for y in wealth:
        if y.moving:
            return y
    for y in wealth:
        if y.is_shi or y.is_ying:
            return y
    for y in wealth:
        if y.vigor in ("wang", "xiang"):
            return y
    return wealth[0]


def evaluate(
    dressed: DressedHexagram,
    ctx: DivinationContext,
    config: RuleConfig = DEFAULT_RULE_CONFIG,
) -> Verdict:
    """断卦。返回方向、强度、得分与完整依据链。"""
    core: DressedCore = dressed.core
    reasoning: list[str] = []
    score = 0.5

    wealth_yaos = [y for y in core.yaos if y.relation == "wealth"]

    if not wealth_yaos:
        score += config.no_wealth_in_hexagram
        reasoning.append(f"{core.meta.name}卦中无妻财爻（财不上卦），所求之财无可依托，判为不利。")
        return _finalize(score, reasoning, None, config)

    yong = _pick_yong_shen(wealth_yaos)
    reasoning.append(
        f"取用神：以妻财为用神（求财之常法）。本卦妻财凡 {len(wealth_yaos)} 爻，"
        f"取{yong.position}爻{yong.gan}{yong.zhi}（{WUXING_NAME[yong.element]}）为主用神。"
    )

    # —— ① 旺衰 ——
    if yong.vigor == "wang":
        score += config.vigor_wang
        reasoning.append("用神临月建而旺，得时得令，利于求财。")
    elif yong.vigor == "xiang":
        score += config.vigor_xiang
        reasoning.append("用神为月令所生而相，气势向旺。")
    elif yong.vigor == "qiu":
        score += config.vigor_qiu
        reasoning.append("用神克月令为囚，气势受制。")
    elif yong.vigor == "si":
        score += config.vigor_si
        reasoning.append("用神被月令所克为死，气势衰绝。")
    else:
        reasoning.append("用神休囚，力量平平。")

    # —— ② 与日月的关系 ——
    if "临月建" in yong.flags or "临日辰" in yong.flags:
        score += config.on_month_or_day
        reasoning.append("用神临月建或日辰，力量强旺。")
    if "月破" in yong.flags or "日破" in yong.flags:
        score += config.broken
        reasoning.append("用神逢月破/日破，力量大损，虽旺亦难为用。")
    if yong.is_void:
        score += config.void
        reasoning.append("用神旬空，所求之事悬而未定。")

    # —— ③ 动爻对用神的作用（兄弟劫财也在这里一并说明，避免同一个爻
    #        被拆成两条重复的依据，读起来像在凑数）——
    movers = [y for y in core.yaos if y.moving and y.position != yong.position]
    for m in movers:
        effects: list[str] = []
        if GENERATES[m.element] == yong.element:
            score += config.generated_by_moving
            effects.append("生用神，为助力")
        if OVERCOMES[m.element] == yong.element:
            score += config.overcome_by_moving
            effects.append("克用神，为阻力")
        if m.relation == "brother":
            score += config.brother_moving
            effects.append("又为兄弟劫财，不利求财")
        if effects:
            reasoning.append(
                f"{m.position}爻{m.gan}{m.zhi}（{WUXING_NAME[m.element]}·{LIUQIN_NAME[m.relation]}）"
                f"发动，{'；'.join(effects)}。"
            )

    # —— ④ 动爻自身 ——
    if yong.moving:
        score += config.self_moving
        reasoning.append("用神自身发动，主事有变动、方向将明。")
    if "回头生" in yong.flags:
        score += config.hui_tou_sheng
        reasoning.append("用神化回头生，变爻生本爻，后势转强。")
    if "回头克" in yong.flags:
        score += config.hui_tou_ke
        reasoning.append("用神化回头克，变爻克本爻，后势转弱。")

    # —— ④ 子孙旺相（子孙生妻财，为财之源）——
    child_strong = any(
        y.relation == "child" and y.vigor in ("wang", "xiang") for y in core.yaos
    )
    if child_strong:
        score += config.child_prosperous
        reasoning.append("卦中子孙旺相，子孙生妻财，为财有源。")

    return _finalize(
        score,
        reasoning,
        {
            "relation": yong.relation,
            "position": yong.position,
            "zhi": yong.zhi,
            "element": yong.element,
        },
        config,
    )


def _finalize(
    raw_score: float,
    reasoning: list[str],
    yong_shen: dict | None,
    config: RuleConfig,
) -> Verdict:
    score = min(1.0, max(0.0, raw_score))
    if score >= config.up_threshold:
        direction = "up"
    elif score <= config.down_threshold:
        direction = "down"
    else:
        direction = "flat"

    label = {"up": "偏涨", "down": "偏跌", "flat": "中性不明"}[direction]
    threshold_text = (
        f"得分 ≥ {config.up_threshold} 判涨，否则判跌"
        if config.up_threshold == config.down_threshold
        else f"判涨阈值 ≥ {config.up_threshold}，判跌阈值 ≤ {config.down_threshold}"
    )
    reasoning.append(
        f"综合得分 {js_fixed(score, 3)}（中性 0.500），判为{label}（{threshold_text}）。"
    )

    return Verdict(
        direction=direction,
        strength=min(1.0, abs(score - 0.5) * 2),
        score=score,
        reasoning=reasoning,
        yong_shen=yong_shen,
    )
