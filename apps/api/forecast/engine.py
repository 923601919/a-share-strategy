"""预测引擎（领域层，纯函数）—— 自 tianji `forecast/engine.ts` 移植。

输入：标的 + 目标交易日 + 起卦时点 + 流派档案 + 历法上下文
输出：一条**完整、冻结、可复核**的 Prediction

它不取行情、不落库、不统计、不生成自然语言——那些属于结算与展示层。
保持这一点，同一个引擎才能既服务于实盘展示，也服务于批量回测/回放。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cast import CastResult, cast_by_coin, cast_from_values, normalize_yao_value
from .constants import LIUQIN_NAME
from .dress import DivinationContext, DressedHexagram, dress_hexagram, shi_yao_of, ying_yao_of
from .prng import Prng
from .rules import DEFAULT_RULE_CONFIG, RuleConfig, evaluate
from .seed import build_seed_material, derive_prng, sha256_hex
from .util import js_round


@dataclass
class DivineRequest:
    symbol: str
    target_date: str  # 目标交易日（该日收盘后可结算）
    as_of: str  # 起卦时点。**必须早于 target_date 的 00:00**，否则抛错
    method: str  # 'liuyao' | 'meihua'
    mode: str  # 'deterministic' | 'manual'
    profile: dict
    context: DivinationContext
    engine_version: str
    ruleset_version: str
    profile_hash: str
    now: str  # 记录生成时刻；纯函数不读系统时间，由调用方提供
    manual_yao: list[int] | None = None
    # 生成来源，默认 'live'。声明 'live' 时引擎会额外校验：记录时刻必须早于
    # 目标交易日，否则「实时预测」在逻辑上不成立（那一刻结果已经出来了）。
    # 回补历史请显式传 'replay'——它会正常产出卦象，但被统计层排除在外。
    origin: str = "live"
    rule_config: RuleConfig = DEFAULT_RULE_CONFIG


@dataclass
class Prediction:
    id: str
    symbol: str
    target_date: str
    as_of: str
    generated_at: str
    origin: str
    direction: str
    strength: float
    basis: dict
    profile_hash: str
    engine_version: str
    ruleset_version: str
    locked_at: str


def divine(req: DivineRequest) -> Prediction:
    origin = req.origin or "live"

    # ── ① 时间边界校验：这是整个功能的诚信基础 ──────────────────
    as_of_date = req.as_of[:10]
    if as_of_date >= req.target_date:
        raise ValueError(
            f"起卦时点（{as_of_date}）不得晚于或等于目标交易日（{req.target_date}）。"
            "允许此情形将构成未来函数，回测准确率会虚高且无法察觉。"
        )

    # 声明为 live 就必须真的「活」：记录时刻须早于目标交易日。
    # 这条检查挡不住存心伪造（调用方可以把 now 填成过去），但它挡住了最常见的
    # 一种疏忽——回补历史时忘记把 origin 改成 replay，于是凭空多出一批看起来
    # 完全合法的「实时预测」。
    if origin == "live":
        recorded_on = req.now[:10]
        if recorded_on >= req.target_date:
            raise ValueError(
                f"记录时刻（{recorded_on}）不早于目标交易日（{req.target_date}），"
                '不能声明为 live 预测。回补历史请传 origin="replay"。'
            )

    # ── ② 起卦 ──
    cast = _build_cast(req)

    # ── ③ 装卦 ──
    dressed = dress_hexagram(cast.result, req.context)

    # ── ④ 断卦 ──
    verdict = evaluate(dressed, req.context, req.rule_config)

    # 补充世应信息，便于用户理解「我与市」的关系。插在总结论**之前**：
    # 最后一条应当永远是「综合得分 … 判为 …」，因为那是整条依据链的落点，
    # 被别的文字挤到中间会让结论读起来散掉。
    shi = shi_yao_of(dressed.core)
    ying = ying_yao_of(dressed.core)
    summary = verdict.reasoning[-1]
    reasoning = list(verdict.reasoning[:-1])
    reasoning.append(
        f"世爻在{shi.position}爻（{shi.gan}{shi.zhi}·{LIUQIN_NAME[shi.relation]}），"
        f"应爻在{ying.position}爻（{ying.gan}{ying.zhi}·{LIUQIN_NAME[ying.relation]}）。"
        "世为己、应为市，二者关系可作为辅助参照。"
    )
    reasoning.append(summary)

    # id 里包含 origin：同一目标日的实时记录与回补记录可以并存，不会互相覆盖。
    # 若把两者算成同一个 id，后写的会撞上前者的冻结约束而被拒绝——那反而更糟，
    # 因为你会失去原本想要的那条回补记录。
    prediction_id = sha256_hex(
        "|".join(
            [
                req.symbol,
                req.target_date,
                req.profile_hash,
                req.engine_version,
                req.ruleset_version,
                origin,
            ]
        )
    )[:16]

    basis = {
        "method": req.method,
        "hexagram": dressed.original.name,
        "changed_hexagram": dressed.changed.name if dressed.changed is not None else None,
        "moving_yao": cast.result.moving_positions,
        "palace": dressed.original.palace,
        "generation": dressed.original.generation,
        "shi_position": dressed.original.shi_position,
        "ying_position": dressed.original.ying_position,
        "yong_shen": verdict.yong_shen,
        "reasoning": reasoning,
        "seed": {
            "material": cast.material,
            "digest": cast.digest,
            "yao_values": cast.values,
            "method": cast.method_label,
        },
        "context": {
            "solar_date": req.context.solar_date,
            "year_pillar": f"{req.context.year_pillar.gan}{req.context.year_pillar.zhi}",
            "month_pillar": f"{req.context.month_pillar.gan}{req.context.month_pillar.zhi}",
            "day_pillar": f"{req.context.day_pillar.gan}{req.context.day_pillar.zhi}",
            "void_branches": list(req.context.void_branches),
            "month_element": req.context.month_element,
        },
        "dressed_yaos": [
            {
                "position": y.position,
                "yin_yang": y.yin_yang,
                "value": y.value,
                "moving": y.moving,
                "gan": y.gan,
                "zhi": y.zhi,
                "element": y.element,
                "relation": y.relation,
                "deity": y.deity,
                "is_shi": y.is_shi,
                "is_ying": y.is_ying,
                "is_void": y.is_void,
                "vigor": y.vigor,
                "flags": y.flags,
            }
            for y in dressed.core.yaos
        ],
    }

    return Prediction(
        id=prediction_id,
        symbol=req.symbol,
        target_date=req.target_date,
        as_of=req.as_of,
        generated_at=req.now,
        origin=origin,
        direction=verdict.direction,
        strength=js_round(verdict.strength, 3),
        basis=basis,
        profile_hash=req.profile_hash,
        engine_version=req.engine_version,
        ruleset_version=req.ruleset_version,
        locked_at=req.now,
    )


@dataclass
class _BuiltCast:
    result: object
    values: list[int]
    material: str
    digest: str
    method_label: str


def _build_cast(req: DivineRequest) -> _BuiltCast:
    # manual：用户自行摇卦。同样锁定 as_of，不允许事后补录
    if req.mode == "manual":
        if not req.manual_yao or len(req.manual_yao) != 6:
            raise ValueError("manual 模式需要提供 6 个爻值（6/7/8/9）")
        values = [normalize_yao_value(v) for v in req.manual_yao]
        result = cast_from_values(values, "manual", "用户手工摇卦录入")
        material = f"manual|{req.symbol}|{req.target_date}|{''.join(str(v) for v in values)}"
        return _BuiltCast(
            result=result,
            values=values,
            material=material,
            digest=sha256_hex(material),
            method_label="manual",
        )

    # deterministic：由「标的 + 目标日 + 盐值」确定性起卦
    material = build_seed_material(req.symbol, req.target_date, req.profile["seedSalt"])
    digest, prng = derive_prng(material)
    result: CastResult = cast_by_coin(prng.next_float, f"确定性起卦，种子材料：{material}")
    return _BuiltCast(
        result=result,
        values=[y.value for y in result.yaos],
        material=material,
        digest=digest,
        method_label="liuyao",
    )
