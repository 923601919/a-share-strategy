"""流派档案（Profile）与版本常量。

── 为什么把整份档案原样搬过来（而不是只留预测用得到的两项）────────────

`profile_hash` 是对**整份档案**做 canonical JSON 后取 sha256 的前 16 位。
只留 `seedSalt` + `flatThresholdPct` 会让哈希变掉，于是：

  · 本项目产出的预测无法与 tianji 的历史记录对账（同一目标日、同一卦，
    哈希却不同，看起来像两个不同的口径）；
  · `profile_hash` 失去「这次计算用的是哪套口径」的含义，退化成随机串。

所以档案整体保留（含紫微/八字那些本功能用不到的字段），
并在单测里用 tianji dump 出来的哈希值钉住：`0da2843edf6479f1`。

── 档案是「一等公民」

它不是一个散落在各处的全局开关集合，而是一个显式参数，贯穿
历法 → 引擎 → 规则 → 预测种子。`profile_hash` 把「用的是哪套口径」
压缩成一个短字符串，历史预测因此可以归因。
"""

from __future__ import annotations

import hashlib
import json

# 与 tianji version.ts 对齐。改动起卦/装卦算法或 PRNG → 必 bump ENGINE_VERSION；
# 改动 RuleConfig 权重或阈值 → 必 bump RULESET_VERSION。bump 之后新记录自然
# 走新版本维度（预测 id 含版本号），历史记录不重算、不覆盖。
ENGINE_VERSION = "0.1.2"
RULESET_VERSION = "0.1.0"

# 档案版本
PROFILE_VERSION = "1.0.0"

# 大盘基准标的：上证指数
MARKET_INDEX_SYMBOL = "000001.SH"
MARKET_INDEX_LABEL = "上证指数"
MARKET_INDEX_CODE = "000001"  # 行情接口用的 6 位代码

# 默认种子盐值：与 tianji 的 standard-cn 档案同值。
# 沿用它的唯一目的是让两边的同一目标日产出同一个卦，从而可以对账。
# 改这个值 = 换一套档案，profile_hash 会一起变——这是正确行为，不是 bug。
DEFAULT_SEED_SALT = "yz-standard-v1"

# 涨跌判定的平盘阈值（**百分点**：0.05 表示 0.05%）。
# ⚠ 单位务必看清：它换算成小数比例是 0.0005。传 0.0005 当百分点用会把
# 几乎所有真实平盘算成涨跌，样本量凭空膨胀。
DEFAULT_FLAT_THRESHOLD_PCT = 0.05

# 起卦时点（收盘时刻，申时）。与 tianji 的盘后作业一致。
CAST_HOUR = 15
CAST_MINUTE = 0

# 排盘地点：上海。起卦要用它算真太阳时与时辰。
#
# ⚠ 本项目**不做真太阳时修正**：断卦只消费日柱干支、月柱（月建）、旬空、
#    月建五行，时辰不参与判决（见 dress.py / rules.py 的说明）。起卦固定在
#    15:00（申时），永远碰不到晚子时（23:00）边界，而中国 1991 年后无夏令时。
#    这条结论由单测钉住：把 hour_branch 换成任意值，判决必须逐字段不变。
JOB_PLACE = {"longitude": 121.47, "latitude": 31.23, "timezone": "Asia/Shanghai"}


def build_profile(seed_salt: str = DEFAULT_SEED_SALT) -> dict:
    """构造档案（字段与 tianji `standard-cn` 逐项一致）。

    含紫微/八字字段是刻意的：它们进 hash，删掉就换了口径。
    """
    return {
        "id": "standard-cn" if seed_salt == DEFAULT_SEED_SALT else "custom",
        "name": "通行（中国大陆）" if seed_salt == DEFAULT_SEED_SALT else "自定义盐值",
        "version": PROFILE_VERSION,
        "seedSalt": seed_salt,
        "solarTime": "true",
        "lateZiHour": "nextDay",
        "lateZiHourTimeGan": "followDayPillar",
        "leapMonth": "asNextMonth",
        "ziweiLateZiHour": "currentLunarDay",
        "starPlacement": "zhongzhou-v1",
        "brightnessTable": "standard-v1",
        "brightnessSchool": "table",
        "yearLimit": "xu-sui-v1",
        "yongShenMethod": "fu-yi",
        "geJuMethod": "tou-gan",
        "flatThresholdPct": DEFAULT_FLAT_THRESHOLD_PCT,
        "adjust": "qfq",
    }


def canonicalize(value) -> str:
    """稳定序列化：递归按键名排序（对应 tianji config/profile.ts）。

    不能直接用 `json.dumps` 的默认行为——对象键顺序不同会产出不同 hash，
    同一份档案就会算出两个 hash，缓存与归因全部失效。
    """
    if value is None or not isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(v) for v in value) + "]"
    entries = list(value.items())
    entries.sort(key=lambda kv: kv[0])
    return "{" + ",".join(
        f"{json.dumps(k, ensure_ascii=False)}:{canonicalize(v)}" for k, v in entries
    ) + "}"


def hash_profile(profile: dict) -> str:
    """档案短 hash（16 位十六进制）。"""
    return hashlib.sha256(canonicalize(profile).encode("utf-8")).hexdigest()[:16]
