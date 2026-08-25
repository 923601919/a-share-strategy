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


def _http_get_text(url: str, *, encoding: str = "utf-8", headers: dict[str, str] | None = None) -> str:
    import urllib.request

    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        **(headers or {}),
    }
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read()
    return raw.decode(encoding, errors="ignore")


def fetch_realtime_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """单票/批量实时报价（新浪优先，腾讯兜底）。用于入池价与自选现价，避免全市场快照失败落到演示价。"""
    uniq: list[str] = []
    seen: set[str] = set()
    for c in codes:
        code = _normalize_code(c)
        if not code or code in seen:
            continue
        seen.add(code)
        uniq.append(code)
    if not uniq:
        return {}

    out: dict[str, dict[str, Any]] = {}
    # 新浪批量（每批最多约 80，稳妥）
    for i in range(0, len(uniq), 80):
        chunk = uniq[i : i + 80]
        syms = ",".join(_sina_symbol(c) for c in chunk)
        try:
            text = _http_get_text(
                f"https://hq.sinajs.cn/list={syms}",
                encoding="gbk",
                headers={"Referer": "https://finance.sina.com.cn"},
            )
            for line in text.splitlines():
                if "hq_str_" not in line or '="' not in line:
                    continue
                left, right = line.split('="', 1)
                payload = right.rstrip('";')
                if not payload:
                    continue
                sym = left.split("hq_str_")[-1].strip()
                code = _normalize_code(sym)
                parts = payload.split(",")
                if len(parts) < 10:
                    continue
                price = _safe_float(parts[3])
                pre = _safe_float(parts[2])
                open_p = _safe_float(parts[1])
                if price <= 0 and pre > 0:
                    price = pre
                pct = round((price / pre - 1.0) * 100, 2) if pre > 0 and price > 0 else 0.0
                out[code] = {
                    "code": code,
                    "name": parts[0],
                    "price": price,
                    "pct": pct,
                    "open": open_p,
                    "pre_close": pre,
                    "high": _safe_float(parts[4]),
                    "low": _safe_float(parts[5]),
                    "volume": _safe_float(parts[8]),
                    "amount": _safe_float(parts[9]),
                    "source": "sina_rt",
                }
        except Exception:
            pass

    missing = [c for c in uniq if c not in out or float(out[c].get("price") or 0) <= 0]
    if missing:
        for i in range(0, len(missing), 60):
            chunk = missing[i : i + 60]
            syms = ",".join(_tx_symbol(c) for c in chunk)
            try:
                text = _http_get_text(f"https://qt.gtimg.cn/q={syms}", encoding="gbk")
                for part in text.split(";"):
                    part = part.strip()
                    if not part or '="' not in part:
                        continue
                    payload = part.split('="', 1)[1].rstrip('"')
                    if not payload:
                        continue
                    fields = payload.split("~")
                    if len(fields) < 6:
                        continue
                    code = _normalize_code(fields[2] if len(fields) > 2 else "")
                    if not code:
                        continue
                    price = _safe_float(fields[3])
                    pre = _safe_float(fields[4])
                    open_p = _safe_float(fields[5])
                    if price <= 0 and pre > 0:
                        price = pre
                    pct = _safe_float(fields[32]) if len(fields) > 32 else 0.0
                    if abs(pct) < 1e-9 and pre > 0 and price > 0:
                        pct = round((price / pre - 1.0) * 100, 2)
                    amount = _safe_float(fields[37]) * 10000 if len(fields) > 37 else 0.0
                    out[code] = {
                        "code": code,
                        "name": fields[1],
                        "price": price,
                        "pct": pct,
                        "open": open_p,
                        "pre_close": pre,
                        "high": _safe_float(fields[33]) if len(fields) > 33 else 0.0,
                        "low": _safe_float(fields[34]) if len(fields) > 34 else 0.0,
                        "volume": _safe_float(fields[6]) * 100 if len(fields) > 6 else 0.0,
                        "amount": amount,
                        "source": "tencent_rt",
                    }
            except Exception:
                pass
    return out


def fetch_spot() -> pd.DataFrame:
    """全市场快照：本机东财常断连，优先新浪，失败再东财。"""
    global _LAST_SPOT_SOURCE
    import akshare as ak

    errors: list[str] = []

    # 1) 新浪（更稳）
    try:
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
        if len(df) > 100:
            _LAST_SPOT_SOURCE = "sina"
            return df
        errors.append(f"sina_rows={len(df)}")
    except Exception as e:
        errors.append(f"sina:{e}")

    # 2) 东财兜底
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
    except Exception as e:
        errors.append(f"em:{e}")

    raise RuntimeError("; ".join(errors) or "spot fetch failed")


def fetch_minute(code: str, days: int = 1) -> pd.DataFrame:
    """1 分钟分时：腾讯优先，失败再东财。"""
    import akshare as ak

    symbol6 = _normalize_code(code)
    end = datetime.now()
    start = end - timedelta(days=max(days, 1) + 2)

    # 1) 腾讯
    try:
        df = ak.stock_zh_a_minute(symbol=_tx_symbol(symbol6), period="1")
        df = _normalize_minute(df, source="tx")
        if not df.empty:
            t = pd.to_datetime(df["time"], errors="coerce")
            last_day = t.max().normalize()
            return df[t >= last_day].reset_index(drop=True)
    except Exception:
        pass

    # 2) 东财
    df = ak.stock_zh_a_hist_min_em(
        symbol=symbol6,
        start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
        end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
        period="1",
        adjust="",
    )
    return _normalize_minute(df, source="em")


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
            adjust="",
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
        df = ak.stock_zh_a_daily(symbol=_sina_symbol(symbol6), adjust="")
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


def fetch_hot_sector_universe(
    *,
    industry_top: int = 5,
    concept_top: int = 3,
) -> dict[str, Any]:
    """
    强势板块成分股候选池（新浪行业/概念）。
    返回 codes、板块列表、code->板块名 标签。
    """
    import akshare as ak

    codes: set[str] = set()
    sectors: list[dict[str, Any]] = []
    code_tags: dict[str, list[str]] = {}

    for indicator, limit in (("行业", industry_top), ("概念", concept_top)):
        if limit <= 0:
            continue
        try:
            df = ak.stock_sector_spot(indicator=indicator)
        except Exception:
            continue
        if df is None or df.empty:
            continue

        colmap = {str(c): c for c in df.columns}
        name_c = next((colmap[k] for k in colmap if "板块" in k), df.columns[1])
        pct_c = next((colmap[k] for k in colmap if "涨跌幅" in k), df.columns[4])
        label_c = "label" if "label" in df.columns else df.columns[0]

        work = df.copy()
        work["_pct"] = pd.to_numeric(work[pct_c], errors="coerce").fillna(0)
        if work["_pct"].abs().max() < 1:
            work["_pct"] *= 100
        work = work.sort_values("_pct", ascending=False).head(limit)

        for _, row in work.iterrows():
            label = str(row.get(label_c, "")).strip()
            name = str(row.get(name_c, "")).strip()
            if not label or not name:
                continue
            try:
                members = ak.stock_sector_detail(sector=label)
            except Exception:
                continue
            if members is None or members.empty or "code" not in members.columns:
                continue
            member_codes = {_normalize_code(x) for x in members["code"].tolist()}
            codes |= member_codes
            for c in member_codes:
                code_tags.setdefault(c, [])
                if name not in code_tags[c]:
                    code_tags[c].append(name)
            sectors.append(
                {
                    "name": name,
                    "pct": round(float(row["_pct"]), 2),
                    "type": indicator,
                    "members": len(member_codes),
                }
            )

    return {"codes": codes, "sectors": sectors, "code_tags": code_tags}


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
    closes[-50:-10] = closes[-51] + np.cumsum(rng.normal(-0.01, 0.02, size=40))
    # 回踩后再攻
    closes[-10:] = closes[-11] + np.cumsum(rng.normal(0.05, 0.02, size=10))
    vols = rng.integers(500, 3000, size=n)
    vols[-50:-10] = (vols[-50:-10] * 0.6).astype(int)
    vols[-10:] = vols[-10:] * 4
    times = pd.date_range("2026-08-21 09:30", periods=n, freq="min")
    return pd.DataFrame({"time": times.astype(str), "close": closes, "volume": vols, "amount": vols * closes * 100})


def get_spot_df() -> pd.DataFrame:
    global _LAST_SPOT_SOURCE
    if settings.demo_mode:
        _LAST_SPOT_SOURCE = "demo"
        return demo_spot()
    try:
        return fetch_spot()
    except Exception as e:
        _LAST_SPOT_SOURCE = f"demo_fallback:{type(e).__name__}"
        return demo_spot()
