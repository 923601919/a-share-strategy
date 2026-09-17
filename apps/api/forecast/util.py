"""数值工具。

★ 为什么需要 `js_round` 而不是 Python 的 `round` ★

JavaScript 的 `Math.round(x)` 等价于 `floor(x + 0.5)`（平局向上取整到 +∞），
而 `Number.prototype.toFixed(d)` 在 V8 里按十进制做四舍五入；
Python 的 `round()` / `format()` 用的是**银行家舍入**（平局取偶）。

本模块的数值要与 tianji 的黄金向量逐位对齐（strength、score 展示、统计量），
平局时的方向差别会让「同一卦同一强度」出现最后一位不同——
那正好会让对账变成一件说不清的事。所以统一走这里。
"""

from __future__ import annotations

import math


def js_round(x: float, digits: int = 0) -> float:
    """对应 JS `Math.round`，保留 `digits` 位小数。"""
    m = 10 ** digits
    return math.floor(x * m + 0.5) / m


def js_fixed(x: float, digits: int) -> str:
    """对应 JS `Number.toFixed(digits)`。"""
    return f"{js_round(x, digits):.{digits}f}"


def signif_round(x: float, digits: int) -> float:
    """按小数位四舍五入（对应 tianji 里成对出现的 `Math.round(x * 10^n) / 10^n`）。"""
    return js_round(x, digits)
