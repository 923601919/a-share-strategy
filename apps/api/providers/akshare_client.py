from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from config import settings
from ssl_fix import apply_ssl_fix

# 被其它模块单独 import 时也保证 SSL 已处理
apply_ssl_fix(insecure=not settings.ssl_verify)

_LAST_SPOT_SOURCE = "none"


def last_spot_source() -> str:
    return _LAST_SPOT_SOURCE


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_code(raw: Any) -> str:
    s = str(raw).strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            s = s[len(p) :]
            break
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits.zfill(6)[-6:]


def _tx_symbol(code: str) -> str:
    c = _normalize_code(code)
    if c.startswith(("5", "6", "9")):
        return f"sh{c}"
    if c.startswith(("4", "8")) or c.startswith("92"):
        return f"bj{c}"
    return f"sz{c}"


def _sina_symbol(code: str) -> str:
    return _tx_symbol(code)


def fetch_spot() -> pd.DataFrame:
    """全市场快照：东财优先，失败则新浪。"""
    global _LAST_SPOT_SOURCE
    import akshare as ak

    # 1) 东财
    try:
        df = ak.stock_zh_a_spot_em()
        rename = {
            "代码": "code",
            "名称": "name",
            "最新价": "price",
            "涨跌幅": "pct",
            "成交额": "amount",
            "换手率": "turnover",
            "量比": "volume_ratio",
            "最高": "high",
            "最低": "low",
            "今开": "open",
            "昨收": "pre_close",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        df["code"] = df["code"].map(_normalize_code)
        keep = [c for c in rename.values() if c in df.columns]
        df = df[keep].copy()
        for col in ("price", "pct", "amount", "turnover", "volume_ratio", "high", "low", "open", "pre_close"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        if "volume_ratio" not in df.columns:
            df["volume_ratio"] = 0.0
        if "turnover" not in df.columns:
            df["turnover"] = 0.0
        _LAST_SPOT_SOURCE = "eastmoney"
        return df
    except Exception:
        pass

    # 2) 新浪
    df = ak.stock_zh_a_spot()
    rename = {
        "代码": "code",
        "名称": "name",
        "最新价": "price",
        "涨跌幅": "pct",
        "成交额": "amount",
        "成交量": "volume",
        "最高": "high",
        "最低": "low",
        "今开": "open",
        "昨收": "pre_close",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    df["code"] = df["code"].map(_normalize_code)
    keep = [c for c in ("code", "name", "price", "pct", "amount", "high", "low", "open", "pre_close") if c in df.columns]
    df = df[keep].copy()
    for col in ("price", "pct", "amount", "high", "low", "open", "pre_close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["turnover"] = 0.0
    df["volume_ratio"] = 0.0
    _LAST_SPOT_SOURCE = "sina"
    return df


def fetch_minute(code: str, days: int = 1) -> pd.DataFrame:
    """1 分钟分时：东财优先，失败则腾讯。"""
    import akshare as ak

    symbol6 = _normalize_code(code)
    end = datetime.now()
    start = end - timedelta(days=max(days, 1) + 2)

    # 1) 东财
    try:
        df = ak.stock_zh_a_hist_min_em(
            symbol=symbol6,
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            period="1",
            adjust="",
        )
        return _normalize_minute(df, source="em")
    except Exception:
        pass

    # 2) 腾讯（常返回近几日，截取最近交易日）
    df = ak.stock_zh_a_minute(symbol=_tx_symbol(symbol6), period="1")
    df = _normalize_minute(df, source="tx")
    if df.empty:
        return df
    # day 列已映射为 time
    t = pd.to_datetime(df["time"], errors="coerce")
    last_day = t.max().normalize()
    df = df[t >= last_day].reset_index(drop=True)
    return df


def _normalize_minute(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    rename = {
        "时间": "time",
        "day": "time",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "volume": "volume",
        "amount": "amount",
    }
    out = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    for col in ("close", "open", "high", "low", "volume", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "volume" in out.columns:
        out["volume"] = out["volume"].fillna(0)
        # 腾讯 volume 多为股；东财多为手。打分用相对放大，单位不影响
    if "amount" in out.columns:
        out["amount"] = out["amount"].fillna(0)
    out.attrs["source"] = source
    return out


def fetch_daily(code: str, limit: int = 40) -> pd.DataFrame:
    import akshare as ak

    symbol6 = _normalize_code(code)
    end = datetime.now()
    start = end - timedelta(days=limit * 2 + 10)

    try:
        df = ak.stock_zh_a_hist(
            symbol=symbol6,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
        rename = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    except Exception:
        df = ak.stock_zh_a_daily(symbol=_sina_symbol(symbol6), adjust="qfq")
        # 已有英文字段
        if "date" not in df.columns and "日期" in df.columns:
            df = df.rename(columns={"日期": "date"})

    for col in ("open", "close", "high", "low", "volume", "amount", "pct"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.tail(limit).reset_index(drop=True)


def fetch_concept_boards_top(n: int = 20) -> list[dict[str, Any]]:
    """板块涨幅前列。东财 push2 在部分网络不可用，优先同花顺/新浪。"""
    import akshare as ak

    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        df = ak.stock_board_industry_summary_ths()
        cols = list(df.columns)
        name_c = cols[1] if len(cols) > 1 else None
        pct_c = cols[2] if len(cols) > 2 else None
        up_c = cols[6] if len(cols) > 6 else None
        leader_c = cols[9] if len(cols) > 9 else None
        for _, r in df.iterrows():
            rows.append(
                {
                    "name": str(r.get(name_c, "")),
                    "pct": _safe_float(r.get(pct_c)),
                    "up_count": int(_safe_float(r.get(up_c))),
                    "leader": str(r.get(leader_c, "")),
                }
            )
    except Exception as e:
        errors.append(f"ths_industry:{e}")

    if len(rows) < n:
        try:
            df = ak.stock_sector_spot(indicator="行业")
            colmap = {str(c): c for c in df.columns}
            name_c = next((colmap[k] for k in colmap if "板块" in k), df.columns[1])
            pct_c = next((colmap[k] for k in colmap if "涨跌幅" in k), df.columns[4])
            for _, r in df.iterrows():
                pct = _safe_float(r.get(pct_c))
                if abs(pct) < 1 and pct != 0:
                    pct *= 100
                rows.append(
                    {
                        "name": str(r.get(name_c, "")),
                        "pct": pct,
                        "up_count": 0,
                        "leader": "",
                    }
                )
        except Exception as e:
            errors.append(f"sina_sector:{e}")

    if not rows:
        raise RuntimeError("; ".join(errors) or "no board source")

    rows.sort(key=lambda x: x.get("pct") or 0, reverse=True)
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for r in rows:
        name = r["name"]
        if not name or name in seen:
            continue
        seen.add(name)
        uniq.append(r)
        if len(uniq) >= n:
            break
    return uniq


def fetch_concept_members(board_name: str) -> set[str]:
    """成分股。东财 push2 失败时返回空集。"""
    import akshare as ak

    for fetcher_name in ("stock_board_industry_cons_em", "stock_board_concept_cons_em"):
        fetcher = getattr(ak, fetcher_name, None)
        if fetcher is None:
            continue
        try:
            df = fetcher(symbol=board_name)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        col = "代码" if "代码" in df.columns else None
        if not col:
            continue
        return {_normalize_code(x) for x in df[col].tolist()}
    return set()


def demo_spot() -> pd.DataFrame:
    """无网/演示数据。"""
    return pd.DataFrame(
        [
            {
                "code": "600812",
                "name": "华北制药",
                "price": 9.0,
                "pct": 5.2,
                "amount": 8.5e8,
                "turnover": 6.1,
                "volume_ratio": 2.3,
                "high": 9.2,
                "low": 8.6,
                "open": 8.7,
                "pre_close": 8.55,
            },
            {
                "code": "002212",
                "name": "天融信",
                "price": 12.4,
                "pct": 4.1,
                "amount": 3.2e8,
                "turnover": 4.5,
                "volume_ratio": 1.8,
                "high": 12.6,
                "low": 11.9,
                "open": 12.0,
                "pre_close": 11.91,
            },
            {
                "code": "003000",
                "name": "劲嘉股份",
                "price": 8.8,
                "pct": 3.5,
                "amount": 2.1e8,
                "turnover": 3.2,
                "volume_ratio": 1.5,
                "high": 8.9,
                "low": 8.5,
                "open": 8.55,
                "pre_close": 8.5,
            },
        ]
    )


def demo_minute(code: str) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(abs(hash(code)) % (2**32))
    n = 120
    base = 10.0
    closes = base + np.cumsum(rng.normal(0.01, 0.03, size=n))
    closes[-40:] = closes[-41] + np.cumsum(rng.normal(0.04, 0.02, size=40))
    vols = rng.integers(500, 3000, size=n)
    vols[-40:] = vols[-40:] * 3
    times = pd.date_range("2026-08-21 09:30", periods=n, freq="min")
    return pd.DataFrame({"time": times.astype(str), "close": closes, "volume": vols, "amount": vols * closes * 100})


def get_spot_df() -> pd.DataFrame:
    global _LAST_SPOT_SOURCE
    if settings.demo_mode:
        _LAST_SPOT_SOURCE = "demo"
        return demo_spot()
    try:
        return fetch_spot()
    except Exception:
        _LAST_SPOT_SOURCE = "demo_fallback"
        return demo_spot()
