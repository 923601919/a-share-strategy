"""市场情绪温度计：涨跌家数 / 涨停 / 炸板 / 连板 / 晋级率 → 0-100 温度 + 阶段。

默认只作为提示层接入 market_env；硬闸门仍由指数涨跌幅决定。
参数是经验起点，需经 stats 影子验证后再固化为闸门。
"""
from __future__ import annotations

from typing import Any


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def classify_sentiment(
    *,
    zt_count: float | int | None = None,
    dt_count: float | int | None = None,
    zhaban_rate: float | None = None,
    max_lianban: float | int | None = None,
    promotion_rate: float | None = None,
    n_up: float | int | None = None,
    n_down: float | int | None = None,
    zt_ice: int = 30,
    zt_euphoria: int = 80,
    lianban_euphoria: int = 5,
    promotion_ice: float = 0.20,
    zhaban_ice: float = 0.40,
    down_ice: int = 3500,
) -> dict[str, Any]:
    """把市场广度指标映射为温度计。缺字段时用剩余指标降级判定，不编造。"""
    zt = _num(zt_count)
    dt = _num(dt_count)
    zb = _num(zhaban_rate)
    lb = _num(max_lianban)
    promo = _num(promotion_rate)
    up = _num(n_up)
    down = _num(n_down)

    temp = 50.0
    if zt is not None:
        temp += max(-20.0, min(40.0, (zt / 120.0) * 40.0 - 10.0))
    if up is not None and down is not None and (up + down) > 0:
        ad = up / max(down, 1.0)
        temp += max(-15.0, min(15.0, (ad - 1.0) * 12.0))
    elif down is not None:
        if down >= down_ice:
            temp -= 15.0
    if lb is not None:
        temp += max(0.0, min(20.0, (lb / 8.0) * 20.0))
    if promo is not None:
        temp += max(-10.0, min(20.0, (promo - 0.25) * 40.0))
    if zb is not None:
        temp -= max(0.0, min(20.0, zb * 25.0))
    temp = round(max(0.0, min(100.0, temp)), 1)

    ice_by_breadth = zt is None and down is not None and down >= down_ice
    if zt is not None and promo is not None and zb is not None:
        ice = bool(zt < zt_ice and promo < promotion_ice and zb > zhaban_ice)
    else:
        ice = bool(ice_by_breadth)

    euphoria = bool(
        zt is not None
        and zt > zt_euphoria
        and (lb is None or lb >= lianban_euphoria)
        and (lb is not None or zt > zt_euphoria + 20)
    )
    # 连板高度是亢奋的必要信号之一；缺连板时要求涨停更高，避免误放宽

    if ice:
        phase = "ice"
        label = "退潮/冰点"
        hint = "后排系统性风险，进攻型宜收缩；龙头低吸可保留"
    elif euphoria:
        phase = "euphoria"
        label = "情绪亢奋"
        hint = "做多情绪强，可放宽竞价弱势卖出权重"
    elif zt is not None and 30 <= zt <= 80 and (promo is None or promo >= 0.3):
        phase = "recover"
        label = "情绪回暖"
        hint = "主线内做龙头，不追一日游"
    else:
        phase = "normal"
        label = "情绪中性"
        hint = ""

    return {
        "phase": phase,
        "label": label,
        "hint": hint,
        "temperature": temp,
        "metrics": {
            "zt_count": None if zt is None else int(round(zt)),
            "dt_count": None if dt is None else int(round(dt)),
            "zhaban_rate": None if zb is None else round(zb, 3),
            "max_lianban": None if lb is None else int(round(lb)),
            "promotion_rate": None if promo is None else round(promo, 3),
            "n_up": None if up is None else int(round(up)),
            "n_down": None if down is None else int(round(down)),
        },
        "ice": ice,
        "euphoria": euphoria,
    }
