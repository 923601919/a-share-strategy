"""确定性起卦的种子（自 tianji `forecast/seed.ts` 移植）。

★ 这是整个预测功能里最需要守住的约束 ★

种子材料**只能**由事前可知的信息构成：标的代码 + 目标交易日 + 流派盐值。

绝不能把任何行情数据（收盘价、成交量、当日涨跌幅）放进来。一旦放进来，
就等于用 T 日的信息去预测 T 日的结果——这叫未来函数，回测准确率会虚高到
毫无意义，而且**看盘面完全看不出来**。

同理，同一组输入必须永远得到同一个卦：这既是回测可复现的前提，
也是产品诚信的底线（不能让用户反复起卦直到摇出满意的卦）。
"""

from __future__ import annotations

import hashlib

from .prng import Prng

# 未来函数红线的守卫：种子材料里出现这些字段名即视为违规（单测会断言）
FORBIDDEN_IN_MATERIAL: tuple[str, ...] = (
    "close", "open", "high", "low", "volume", "amount", "pct", "change",
)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def build_seed_material(symbol: str, target_date: str, seed_salt: str) -> str:
    """构造种子材料原文。它会被写入预测记录，作为可复核的证据。"""
    return f"{symbol.strip().upper()}|{target_date}|{seed_salt}"


def derive_prng(material: str, domain: str = "cast") -> tuple[str, Prng]:
    """由种子材料派生确定性 PRNG。

    用 domain separation（附加用途标记）区分不同用途的随机流，
    这样将来若要「同一标的产生一个主卦和一个参考卦」，两者不会相关。
    """
    seeded = f"{material}#{domain}"
    return sha256_hex(material), Prng(sha256_bytes(seeded))
