from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from typing import Any, Callable, TypeVar

import pandas as pd

from config import settings
from ssl_fix import apply_ssl_fix

# 被其它模块单独 import 时也保证 SSL 已处理
apply_ssl_fix(insecure=not settings.ssl_verify)

_LAST_SPOT_SOURCE = "none"
_T = TypeVar("_T")
# 板块成分并行专用池（任务内同步完成，不用于可超时丢弃的请求）
_SECTOR_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ak-sector")
_abandoned = 0
_abandoned_lock = __import__("threading").Lock()


def last_spot_source() -> str:
    return _LAST_SPOT_SOURCE


def call_with_timeout(fn: Callable[..., _T], timeout: float, *args: Any, **kwargs: Any) -> _T:
    """
    在一次性守护线程中执行并超时。
    不用共享线程池，避免超时任务占满 worker 导致后续假超时。
    超时后线程可能仍在跑，但不会堵住新请求。
    """
    import threading

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=_runner, name=f"ak-to-{getattr(fn, '__name__', 'call')}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        global _abandoned
        with _abandoned_lock:
            _abandoned += 1
            n = _abandoned
        if n % 10 == 1:
            import logging

            logging.getLogger("akshare_client").warning(
                "timed-out background calls so far: %s (last=%s)",
                n,
                getattr(fn, "__name__", "call"),
            )
        raise TimeoutError(f"{getattr(fn, '__name__', 'call')} timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _call_sector_timeout(fn: Callable[..., _T], timeout: float, *args: Any, **kwargs: Any) -> _T:
    """板块列表等短调用：仍走专用池，但超时不占用共享通用池。"""
    fut = _SECTOR_POOL.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout as e:
        raise TimeoutError(f"{getattr(fn, '__name__', 'call')} timed out after {timeout}s") from e


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
    """全市场快照：本机东财常断连/易卡死，优先新浪并强制超时。"""
    global _LAST_SPOT_SOURCE
    import akshare as ak

    errors: list[str] = []

    def _from_sina() -> pd.DataFrame:
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
        return df

    def _from_em() -> pd.DataFrame:
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
        return df

    # 1) 新浪（更稳），严格超时；超时后绝不继续等
    try:
        df = call_with_timeout(_from_sina, 18)
        if len(df) > 100:
            _LAST_SPOT_SOURCE = "sina"
            return df
        errors.append(f"sina_rows={len(df)}")
    except Exception as e:
        errors.append(f"sina:{e}")

    # 2) 东财兜底（部分环境会触发 py_mini_racer，也必须超时）
    try:
        df = call_with_timeout(_from_em, 18)
        _LAST_SPOT_SOURCE = "eastmoney"
        return df
    except Exception as e:
        errors.append(f"em:{e}")

    raise RuntimeError("; ".join(errors) or "spot fetch failed")


def fetch_minute(code: str, days: int = 1) -> pd.DataFrame:
    """1 分钟分时：腾讯优先，失败再东财；每源强制超时。"""
    import akshare as ak

    symbol6 = _normalize_code(code)
    end = datetime.now()
    start = end - timedelta(days=max(days, 1) + 2)

    def _from_tx() -> pd.DataFrame:
        df = ak.stock_zh_a_minute(symbol=_tx_symbol(symbol6), period="1")
        df = _normalize_minute(df, source="tx")
        if df.empty:
            return df
        t = pd.to_datetime(df["time"], errors="coerce")
        last_day = t.max().normalize()
        return df[t >= last_day].reset_index(drop=True)

    def _from_em() -> pd.DataFrame:
        df = ak.stock_zh_a_hist_min_em(
            symbol=symbol6,
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            period="1",
            adjust="",
        )
        return _normalize_minute(df, source="em")

    try:
        df = call_with_timeout(_from_tx, 8)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass

    try:
        return call_with_timeout(_from_em, 10)
    except Exception as e:
        raise RuntimeError(f"minute fetch failed for {symbol6}: {e}") from e


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

    def _from_em() -> pd.DataFrame:
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
        return df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    def _from_sina() -> pd.DataFrame:
        df = ak.stock_zh_a_daily(symbol=_sina_symbol(symbol6), adjust="")
        if "date" not in df.columns and "日期" in df.columns:
            df = df.rename(columns={"日期": "date"})
        return df

    try:
        df = call_with_timeout(_from_em, 10)
    except Exception:
        df = call_with_timeout(_from_sina, 10)

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
        df = call_with_timeout(ak.stock_board_industry_summary_ths, 12)
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
            df = call_with_timeout(ak.stock_sector_spot, 12, indicator="行业")
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


def _pick_sector_spot_columns(df: pd.DataFrame) -> tuple[Any, Any, Any]:
    """新浪板块表：优先按列名，避免把涨跌额当成涨跌幅。"""
    cols = list(df.columns)
    colmap = {str(c): c for c in cols}
    label_c = colmap.get("label") or cols[0]
    name_c = next((colmap[k] for k in colmap if "板块" in k or k in ("name", "板块")), None)
    if name_c is None:
        name_c = cols[1] if len(cols) > 1 else cols[0]
    # 涨跌幅通常在涨跌额之后；优先列名，其次第 6 列（index 5）
    pct_c = next((colmap[k] for k in colmap if "涨跌幅" in k or "changepercent" in k.lower()), None)
    if pct_c is None:
        pct_c = cols[5] if len(cols) > 5 else (cols[4] if len(cols) > 4 else cols[-1])
    return label_c, name_c, pct_c


def _member_codes_from_df(df: pd.DataFrame | None) -> set[str]:
    if df is None or df.empty:
        return set()
    for col in ("code", "代码", "股票代码", "证券代码", "symbol"):
        if col not in df.columns:
            continue
        vals = df[col].tolist()
        if col == "symbol":
            return {_normalize_code(x) for x in vals if x is not None}
        return {_normalize_code(x) for x in vals if x is not None}
    return set()


def _fetch_sina_sector_members(label: str) -> set[str]:
    import akshare as ak

    try:
        # 已在 _SECTOR_POOL worker 内时直接调用，避免嵌套提交死锁
        members = ak.stock_sector_detail(sector=label)
    except Exception:
        return set()
    return _member_codes_from_df(members)


def fetch_hot_sector_universe(
    *,
    industry_top: int = 5,
    concept_top: int = 3,
    sector_min_pct: float | None = None,
) -> dict[str, Any]:
    """
    强势板块成分股候选池（新浪行业/概念，失败则东财成分兜底）。
    返回 codes、板块列表、code->板块名 标签。
    """
    import akshare as ak
    from concurrent.futures import as_completed

    codes: set[str] = set()
    sectors: list[dict[str, Any]] = []
    code_tags: dict[str, list[str]] = {}
    min_pct = settings.sector_min_pct if sector_min_pct is None else sector_min_pct
    jobs: list[tuple[str, str, float, str]] = []  # label, name, pct, type

    for indicator, limit in (("行业", industry_top), ("概念", concept_top)):
        if limit <= 0:
            continue
        try:
            df = _call_sector_timeout(ak.stock_sector_spot, 15, indicator=indicator)
        except Exception:
            continue
        if df is None or df.empty:
            continue

        label_c, name_c, pct_c = _pick_sector_spot_columns(df)
        work = df.copy()
        work["_pct"] = pd.to_numeric(work[pct_c], errors="coerce").fillna(0)
        # 仅当整列都像小数涨跌幅（如 0.02）时放大；涨跌额也可能 <1，不能误乘
        pct_abs_max = float(work["_pct"].abs().max() or 0)
        if 0 < pct_abs_max <= 0.3:
            work["_pct"] *= 100
        work = work[work["_pct"] >= min_pct]
        work = work.sort_values("_pct", ascending=False).head(limit)

        for _, row in work.iterrows():
            label = str(row.get(label_c, "")).strip()
            name = str(row.get(name_c, "")).strip()
            if not label or not name:
                continue
            jobs.append((label, name, float(row["_pct"]), indicator))

    def _one(job: tuple[str, str, float, str]) -> tuple[str, str, float, str, set[str]]:
        label, name, pct, indicator = job
        member_codes = _fetch_sina_sector_members(label)
        if not member_codes:
            member_codes = _fetch_em_members_direct(name)
        return label, name, pct, indicator, member_codes

    if jobs:
        futs = {_SECTOR_POOL.submit(_one, j): j for j in jobs}
        try:
            for fut in as_completed(futs, timeout=45):
                try:
                    _label, name, pct, indicator, member_codes = fut.result()
                except Exception:
                    continue
                if not member_codes:
                    continue
                codes |= member_codes
                for c in member_codes:
                    code_tags.setdefault(c, [])
                    if name not in code_tags[c]:
                        code_tags[c].append(name)
                sectors.append(
                    {
                        "name": name,
                        "pct": round(pct, 2),
                        "type": indicator,
                        "members": len(member_codes),
                    }
                )
        except TimeoutError:
            pass

    sectors.sort(key=lambda x: x.get("pct") or 0, reverse=True)
    return {"codes": codes, "sectors": sectors, "code_tags": code_tags}


def _fetch_em_members_direct(board_name: str) -> set[str]:
    """在已占用的 worker 内直接拉东财成分（不再进线程池）。"""
    import akshare as ak

    name = (board_name or "").strip()
    if not name:
        return set()
    candidates = [name]
    for suffix in ("板块", "概念", "行业"):
        if name.endswith(suffix) and len(name) > len(suffix):
            candidates.append(name[: -len(suffix)])
    seen: set[str] = set()
    for symbol in candidates:
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        for fetcher_name in ("stock_board_industry_cons_em", "stock_board_concept_cons_em"):
            fetcher = getattr(ak, fetcher_name, None)
            if fetcher is None:
                continue
            try:
                df = fetcher(symbol=symbol)
            except Exception:
                continue
            codes = _member_codes_from_df(df)
            if codes:
                return codes
    return set()


def fetch_concept_members(board_name: str) -> set[str]:
    """成分股。东财行业/概念；失败返回空集。带超时，可被外部线程调用。"""
    try:
        return _call_sector_timeout(_fetch_em_members_direct, 15, board_name)
    except Exception:
        return set()


def demo_spot() -> pd.DataFrame:
    """仅 DEMO_MODE=true 时使用，禁止作为行情失败兜底。"""
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


def quotes_to_spot_df(quotes: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """实时报价字典 → 与 fetch_spot 同结构的 DataFrame。"""
    rows: list[dict[str, Any]] = []
    for code, q in (quotes or {}).items():
        price = _safe_float(q.get("price"))
        if price <= 0:
            continue
        rows.append(
            {
                "code": _normalize_code(code),
                "name": str(q.get("name") or code),
                "price": price,
                "pct": _safe_float(q.get("pct")),
                "amount": _safe_float(q.get("amount")),
                "turnover": 0.0,
                "volume_ratio": 0.0,
                "high": _safe_float(q.get("high")),
                "low": _safe_float(q.get("low")),
                "open": _safe_float(q.get("open")),
                "pre_close": _safe_float(q.get("pre_close")),
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "code",
                "name",
                "price",
                "pct",
                "amount",
                "turnover",
                "volume_ratio",
                "high",
                "low",
                "open",
                "pre_close",
            ]
        )
    return pd.DataFrame(rows)


def empty_spot_df() -> pd.DataFrame:
    return quotes_to_spot_df({})


# 指数代码 -> 新浪 symbol。指数不能用 _sina_symbol（会把 000001 判成深市个股）。
_INDEX_SYMBOLS: dict[str, str] = {
    "sh000001": "sh000001",  # 上证指数
    "sz399001": "sz399001",  # 深证成指
    "sz399006": "sz399006",  # 创业板指
    "sh000300": "sh000300",  # 沪深300
    "sh000905": "sh000905",  # 中证500
}


def fetch_index_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """指数实时快照（新浪 hq.sinajs.cn）。返回 {指数代码: {name, price, pct, ...}}。

    新浪指数字段：parts[0]=名称 parts[1]=今开 parts[2]=昨收 parts[3]=现价
    parts[4]=最高 parts[5]=最低。失败静默返回空 dict，由调用方降级。
    """
    syms: list[str] = []
    for c in codes:
        s = str(c).strip().lower()
        s = s if s in _INDEX_SYMBOLS else _INDEX_SYMBOLS.get(f"{s[:2]}{s[-6:].zfill(6)}", "")
        if s:
            syms.append(s)
    if not syms:
        return {}

    out: dict[str, dict[str, Any]] = {}
    try:
        text = _http_get_text(
            f"https://hq.sinajs.cn/list={','.join(syms)}",
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
            parts = payload.split(",")
            if len(parts) < 6:
                continue
            price = _safe_float(parts[3])
            pre = _safe_float(parts[2])
            if price <= 0 and pre > 0:
                price = pre
            pct = round((price / pre - 1.0) * 100, 2) if pre > 0 and price > 0 else 0.0
            out[sym] = {
                "code": sym,
                "name": parts[0],
                "price": price,
                "pct": pct,
                "open": _safe_float(parts[1]),
                "pre_close": pre,
                "high": _safe_float(parts[4]),
                "low": _safe_float(parts[5]),
                "source": "sina_index",
            }
    except Exception:
        pass
    return out


def fetch_overnight_global() -> dict[str, Any]:
    """
    隔夜外盘参考（美股主要指数），用于复盘宏观偏弱→竞价卖提示。
    """
    import akshare as ak

    indices: list[dict[str, Any]] = []
    errors: list[str] = []

    def _parse_pct(row: pd.Series, pct_cols: list[str]) -> float | None:
        for c in pct_cols:
            if c in row.index:
                v = _safe_float(row[c], default=float("nan"))
                if v == v:
                    if abs(v) < 1 and v != 0:
                        v *= 100
                    return round(v, 2)
        return None

    try:
        df = ak.index_us_stock_sina()
        if df is not None and not df.empty:
            name_c = next((c for c in df.columns if "名称" in str(c)), df.columns[0])
            pct_c = next((c for c in df.columns if "涨跌幅" in str(c)), None)
            for _, row in df.iterrows():
                name = str(row.get(name_c) or "").strip()
                if not name:
                    continue
                pct = _parse_pct(row, [pct_c] if pct_c else [])
                if pct is None:
                    continue
                indices.append({"name": name, "pct": pct, "market": "US"})
    except Exception as e:
        errors.append(f"us_sina:{e}")

    if not indices:
        try:
            df = ak.stock_us_spot_em()
            if df is not None and not df.empty:
                name_c = next((c for c in df.columns if "名称" in str(c)), df.columns[1])
                pct_c = next((c for c in df.columns if "涨跌幅" in str(c)), None)
                focus = ("道琼斯", "纳斯达克", "标普", "纳指", "道指")
                for _, row in df.iterrows():
                    name = str(row.get(name_c) or "").strip()
                    if not any(k in name for k in focus):
                        continue
                    pct = _parse_pct(row, [pct_c] if pct_c else [])
                    if pct is None:
                        continue
                    indices.append({"name": name, "pct": pct, "market": "US"})
        except Exception as e:
            errors.append(f"us_em:{e}")

    pcts = [float(i["pct"]) for i in indices if i.get("pct") is not None]
    avg_pct = round(sum(pcts) / len(pcts), 2) if pcts else None
    down_heavy = sum(1 for p in pcts if p <= settings.global_weak_index_pct)
    weak = False
    weak_reason = ""
    if pcts:
        if avg_pct is not None and avg_pct <= settings.global_weak_avg_pct:
            weak = True
            weak_reason = f"美股主要指数均涨 {avg_pct:.2f}%（偏弱阈值 {settings.global_weak_avg_pct}%）"
        elif down_heavy >= 2:
            weak = True
            weak_reason = f"{down_heavy} 个主要指数跌超 {abs(settings.global_weak_index_pct)}%"

    return {
        "indices": indices,
        "avg_pct": avg_pct,
        "weak": weak,
        "weak_reason": weak_reason,
        "errors": errors,
        "source": "sina_us" if indices and errors == [] else ("mixed" if indices else "none"),
    }


def get_spot_df() -> pd.DataFrame:
    """获取全市场快照。非 demo 模式失败时抛错，绝不回落演示票。"""
    global _LAST_SPOT_SOURCE
    if settings.demo_mode:
        _LAST_SPOT_SOURCE = "demo"
        return demo_spot()
    try:
        return fetch_spot()
    except Exception as e:
        _LAST_SPOT_SOURCE = f"error:{type(e).__name__}"
        raise RuntimeError(f"行情快照失败: {e}") from e


def get_spot_df_or_empty() -> pd.DataFrame:
    """扫描用：失败返回空表，不注入演示数据。"""
    global _LAST_SPOT_SOURCE
    if settings.demo_mode:
        _LAST_SPOT_SOURCE = "demo"
        return demo_spot()
    try:
        return fetch_spot()
    except Exception as e:
        _LAST_SPOT_SOURCE = f"error:{type(e).__name__}:{str(e)[:80]}"
        return empty_spot_df()


def get_spot_df_or_empty_bundle() -> dict[str, Any]:
    """子进程隔离用：一并返回 source。"""
    df = get_spot_df_or_empty()
    return {"df": df, "source": last_spot_source()}


def fetch_hot_sector_universe_bundle(
    *,
    industry_top: int = 5,
    concept_top: int = 3,
    sector_min_pct: float | None = None,
) -> dict[str, Any]:
    """子进程隔离用：codes 转为 list 便于序列化。"""
    uni = fetch_hot_sector_universe(
        industry_top=industry_top,
        concept_top=concept_top,
        sector_min_pct=sector_min_pct,
    )
    return {
        "codes": list(uni.get("codes") or []),
        "sectors": uni.get("sectors") or [],
        "code_tags": uni.get("code_tags") or {},
    }
