from __future__ import annotations

from typing import Any

import pandas as pd


def is_excluded_board(
    code: str,
    *,
    exclude_star: bool = True,
    exclude_bse: bool = True,
) -> bool:
    """科创板/北交所排除判定。

    - 科创板 688/689：20% 涨跌幅、50 万权限门槛；
    - 北交所及老三板 4/8/92 开头：30% 涨跌幅、多数账户无权限。
    涨跌幅与流动性规则与主板差异大，默认从进攻型候选中剔除。
    """
    c = str(code).strip().zfill(6)
    if exclude_star and (c.startswith("688") or c.startswith("689")):
        return True
    if exclude_bse and (c.startswith("4") or c.startswith("8") or c.startswith("92")):
        return True
    return False


def anomaly_30d_pct(daily: pd.DataFrame) -> dict[str, Any]:
    """近约 30 个交易日最大涨幅进度（相对区间最低收盘）。"""
    empty = {
        "pct_from_low": 0.0,
        "ma5": None,
        "last_close": None,
        "last_open": None,
        "bars_from_low": None,
        "days_to_regulatory_exit": None,
        "regulatory_window_end": None,
        "new_anomaly_recent": False,
        "low_date": None,
    }
    if daily is None or daily.empty or "close" not in daily.columns:
        return empty

    df = daily.dropna(subset=["close"]).tail(30).copy()
    if df.empty:
        return empty

    close = pd.to_numeric(df["close"], errors="coerce")
    low = float(close.min())
    last = float(close.iloc[-1])
    pct = (last / low - 1.0) * 100 if low > 0 else 0.0
    ma5 = float(close.tail(5).mean()) if len(df) >= 5 else float(close.mean())
    last_open = float(df["open"].iloc[-1]) if "open" in df.columns else last

    low_pos = int(close.argmin())
    bars_from_low = max(len(df) - 1 - low_pos, 0)
    days_to_exit = max(30 - bars_from_low, 0)
    last5_high = float(close.tail(5).max()) if len(df) else last
    window_high = float(close.max())
    new_anomaly = bool(window_high > 0 and last5_high >= window_high * 0.995 and bars_from_low >= 5)

    low_date = None
    if "date" in df.columns:
        try:
            low_date = str(pd.to_datetime(df["date"].iloc[low_pos]).date())
        except Exception:
            low_date = None

    window_end = None
    if low_date:
        from rules.selection import estimated_window_end

        window_end = estimated_window_end(low_date, days_to_exit)

    return {
        "pct_from_low": round(pct, 2),
        "ma5": round(ma5, 3),
        "last_close": round(last, 3),
        "last_open": round(last_open, 3),
        "bars_from_low": bars_from_low,
        "days_to_regulatory_exit": days_to_exit,
        "regulatory_window_end": window_end,
        "new_anomaly_recent": new_anomaly,
        "low_date": low_date,
    }


def risk_flags(
    anomaly_pct: float,
    *,
    price: float | None = None,
    ma5: float | None = None,
    open_price: float | None = None,
    warn: float = 180.0,
    block: float = 195.0,
    days_to_regulatory_exit: int | None = None,
    new_anomaly_recent: bool = False,
    regulatory_window_end: str | None = None,
    watch_days: int = 3,
) -> dict[str, Any]:
    level = "ok"
    messages: list[str] = []
    if anomaly_pct >= block:
        near_exit = (
            days_to_regulatory_exit is not None
            and days_to_regulatory_exit <= watch_days
            and not new_anomaly_recent
        )
        if near_exit:
            level = "watch"
            end_txt = f"，预计出监管 {regulatory_window_end}" if regulatory_window_end else ""
            messages.append(
                f"近30日从低点涨幅{anomaly_pct:.1f}%临近出监管（剩{days_to_regulatory_exit}日{end_txt}），观察而非一刀切"
            )
        else:
            level = "block"
            messages.append(f"近30日从低点涨幅{anomaly_pct:.1f}%接近/超过200%异动红线")
    elif anomaly_pct >= warn:
        level = "warn"
        extra = ""
        if days_to_regulatory_exit is not None:
            extra = f"，出监管约剩{days_to_regulatory_exit}日"
        messages.append(f"近30日从低点涨幅{anomaly_pct:.1f}%，接近异动红线{extra}")

    below_ma5 = False
    if price is not None and ma5 is not None and ma5 > 0:
        if price < ma5:
            below_ma5 = True
            messages.append("现价在五日线下方")
    auction_sell = False
    if open_price is not None and ma5 is not None and ma5 > 0 and open_price < ma5:
        auction_sell = True
        messages.append("开盘价在五日线下（趋势模式卖点提示）")

    return {
        "level": level,
        "messages": messages,
        "below_ma5": below_ma5,
        "auction_sell_hint": auction_sell,
        "anomaly_progress": round(min(anomaly_pct / 200.0 * 100, 100), 1),
        "days_to_regulatory_exit": days_to_regulatory_exit,
        "regulatory_window_end": regulatory_window_end,
    }
