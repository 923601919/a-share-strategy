from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta
from typing import Any, Callable, TypeVar

import pandas as pd

from config import settings
from providers import akshare_client as raw
from providers.isolated import call_isolated

logger = logging.getLogger("market")

_T = TypeVar("_T")

# 简易 TTL 缓存（进程内）
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()

# 同 key 单飞：并发 miss 只打一次源站
_inflight: dict[str, Future] = {}
_inflight_lock = threading.Lock()

# 数据源健康状态
_source_health: dict[str, dict[str, Any]] = {}
_health_lock = threading.Lock()
_LAST_SPOT_SOURCE_OVERRIDE: str | None = None


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        expires, value = item
        if time.time() > expires:
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: Any, ttl: float) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)


def _single_flight(key: str, fn: Callable[[], _T], *, wait_timeout: float = 120.0) -> _T:
    """同一 key 仅一个 in-flight 请求，其余等待同一结果。"""
    with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None:
            fut = existing
            owner = False
        else:
            fut = Future()
            _inflight[key] = fut
            owner = True

    if not owner:
        return fut.result(timeout=wait_timeout)

    try:
        value = fn()
        fut.set_result(value)
        return value
    except Exception as e:
        fut.set_exception(e)
        raise
    finally:
        with _inflight_lock:
            if _inflight.get(key) is fut:
                _inflight.pop(key, None)


def _mark(source: str, *, ok: bool, detail: str = "", ms: float | None = None) -> None:
    with _health_lock:
        prev = _source_health.get(source) or {}
        _source_health[source] = {
            "ok": ok,
            "detail": detail[:200],
            "last_ms": ms,
            "last_ok_at": time.time() if ok else prev.get("last_ok_at"),
            "last_fail_at": time.time() if not ok else prev.get("last_fail_at"),
            "ok_count": int(prev.get("ok_count") or 0) + (1 if ok else 0),
            "fail_count": int(prev.get("fail_count") or 0) + (0 if ok else 1),
        }


def source_health() -> dict[str, dict[str, Any]]:
    with _health_lock:
        return {k: dict(v) for k, v in _source_health.items()}


def last_spot_source() -> str:
    if _LAST_SPOT_SOURCE_OVERRIDE:
        return _LAST_SPOT_SOURCE_OVERRIDE
    return raw.last_spot_source()


def demo_mode() -> bool:
    return bool(settings.demo_mode)


def fetch_realtime_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    t0 = time.perf_counter()
    try:
        out = raw.fetch_realtime_quotes(codes)
        _mark("realtime_quotes", ok=True, detail=f"n={len(out)}", ms=(time.perf_counter() - t0) * 1000)
        return out
    except Exception as e:
        _mark("realtime_quotes", ok=False, detail=str(e), ms=(time.perf_counter() - t0) * 1000)
        raise


def quotes_to_spot_df(quotes: dict[str, dict[str, Any]]) -> pd.DataFrame:
    return raw.quotes_to_spot_df(quotes)


def get_spot_df_or_empty(*, use_isolated: bool = True, ttl: float = 45.0) -> pd.DataFrame:
    """全市场快照：优先缓存；危险路径走子进程隔离。"""
    global _LAST_SPOT_SOURCE_OVERRIDE
    if settings.demo_mode:
        return raw.demo_spot()

    cached = _cache_get("spot_df")
    if cached is not None:
        return cached["df"] if isinstance(cached, dict) else cached

    def _load() -> pd.DataFrame:
        global _LAST_SPOT_SOURCE_OVERRIDE
        t0 = time.perf_counter()
        try:
            if use_isolated:
                bundle = call_isolated(
                    "get_spot_df_or_empty_bundle", timeout=settings.spot_isolated_timeout
                )
                df = bundle.get("df") if isinstance(bundle, dict) else bundle
                src = (bundle or {}).get("source") if isinstance(bundle, dict) else "isolated"
                _LAST_SPOT_SOURCE_OVERRIDE = src
            else:
                df = raw.get_spot_df_or_empty()
                _LAST_SPOT_SOURCE_OVERRIDE = None
            if df is None:
                df = raw.empty_spot_df()
            _cache_set("spot_df", {"df": df, "source": last_spot_source()}, ttl)
            _mark(
                "spot",
                ok=not getattr(df, "empty", True),
                detail=last_spot_source(),
                ms=(time.perf_counter() - t0) * 1000,
            )
            return df
        except Exception as e:
            logger.warning("spot fetch failed: %s", e)
            _LAST_SPOT_SOURCE_OVERRIDE = f"error:{e}"
            _mark("spot", ok=False, detail=str(e), ms=(time.perf_counter() - t0) * 1000)
            return raw.empty_spot_df()

    # 外层等待须大于子进程超时，否则单飞会在子进程被杀之前就放弃
    return _single_flight("spot_df", _load, wait_timeout=settings.spot_isolated_timeout + 30)


def fetch_hot_sector_universe(
    *,
    industry_top: int = 5,
    concept_top: int = 3,
    sector_min_pct: float | None = None,
    use_isolated: bool = True,
    ttl: float = 90.0,
) -> dict[str, Any]:
    key = f"universe:{industry_top}:{concept_top}:{sector_min_pct}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    def _load() -> dict[str, Any]:
        t0 = time.perf_counter()
        kwargs = {
            "industry_top": industry_top,
            "concept_top": concept_top,
            "sector_min_pct": sector_min_pct,
        }
        try:
            if use_isolated and settings.sector_universe_use_isolated and not settings.demo_mode:
                try:
                    uni = call_isolated("fetch_hot_sector_universe_bundle", timeout=90, kwargs=kwargs)
                except Exception as iso_err:
                    logger.warning("isolated sector universe failed, fallback in-process: %s", iso_err)
                    uni = raw.fetch_hot_sector_universe(**kwargs)
            else:
                uni = raw.fetch_hot_sector_universe(**kwargs)
            if not uni.get("codes"):
                logger.warning("primary sector universe empty, trying board+em fallback")
                uni = _fallback_sector_universe(
                    industry_top=industry_top,
                    concept_top=concept_top,
                    sector_min_pct=sector_min_pct,
                )
            uni = uni or {"codes": [], "sectors": [], "code_tags": {}}
            if isinstance(uni.get("codes"), list):
                uni = {**uni, "codes": set(uni["codes"])}
            elif not isinstance(uni.get("codes"), set):
                uni = {**uni, "codes": set(uni.get("codes") or [])}
            n = len(uni.get("codes") or [])
            if n > 0:
                _cache_set(key, uni, ttl)
            _mark("sector_universe", ok=n > 0, detail=f"codes={n}", ms=(time.perf_counter() - t0) * 1000)
            return uni
        except Exception as e:
            logger.warning("sector universe failed: %s", e)
            _mark("sector_universe", ok=False, detail=str(e), ms=(time.perf_counter() - t0) * 1000)
            raise

    return _single_flight(key, _load, wait_timeout=ttl + 60)


def _fallback_sector_universe(
    *,
    industry_top: int = 5,
    concept_top: int = 3,
    sector_min_pct: float | None = None,
) -> dict[str, Any]:
    """新浪板块失败时：用同花顺行业榜 + 东财成分兜底。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    min_pct = settings.sector_min_pct if sector_min_pct is None else sector_min_pct
    codes: set[str] = set()
    sectors: list[dict[str, Any]] = []
    code_tags: dict[str, list[str]] = {}

    try:
        boards = raw.fetch_concept_boards_top(max(industry_top + concept_top, 12))
    except Exception as e:
        logger.warning("fallback boards failed: %s", e)
        return {"codes": set(), "sectors": [], "code_tags": {}}

    picked = [b for b in boards if float(b.get("pct") or 0) >= min_pct]
    picked = picked[: industry_top + concept_top]

    def _one(b: dict[str, Any]) -> tuple[str, float, set[str]]:
        name = str(b.get("name") or "").strip()
        if not name:
            return "", 0.0, set()
        members = raw.fetch_concept_members(name)
        return name, float(b.get("pct") or 0), members

    with ThreadPoolExecutor(max_workers=min(6, max(len(picked), 1)), thread_name_prefix="uni-fb") as pool:
        futs = [pool.submit(_one, b) for b in picked]
        try:
            for fut in as_completed(futs, timeout=50):
                try:
                    name, pct, members = fut.result()
                except Exception:
                    continue
                if not name or not members:
                    continue
                codes |= members
                for c in members:
                    code_tags.setdefault(c, [])
                    if name not in code_tags[c]:
                        code_tags[c].append(name)
                sectors.append(
                    {
                        "name": name,
                        "pct": round(pct, 2),
                        "type": "fallback",
                        "members": len(members),
                    }
                )
        except TimeoutError:
            logger.warning("fallback sector universe timed out with partial results")

    logger.info("fallback sector universe: %s boards, %s codes", len(sectors), len(codes))
    return {"codes": codes, "sectors": sectors, "code_tags": code_tags}


def fetch_concept_boards_top(n: int = 20, ttl: float = 90.0) -> list[dict[str, Any]]:
    key = f"boards:{n}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    def _load() -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        try:
            rows = raw.fetch_concept_boards_top(n)
            _cache_set(key, rows, ttl)
            _mark("boards", ok=True, detail=f"n={len(rows)}", ms=(time.perf_counter() - t0) * 1000)
            return rows
        except Exception as e:
            _mark("boards", ok=False, detail=str(e), ms=(time.perf_counter() - t0) * 1000)
            raise

    return _single_flight(key, _load, wait_timeout=ttl + 30)


def fetch_minute(code: str, days: int = 1) -> pd.DataFrame:
    t0 = time.perf_counter()
    try:
        df = raw.fetch_minute(code, days=days)
        _mark("minute", ok=True, detail=code, ms=(time.perf_counter() - t0) * 1000)
        return df
    except Exception as e:
        _mark("minute", ok=False, detail=f"{code}:{e}", ms=(time.perf_counter() - t0) * 1000)
        raise


def _seconds_until_next_open(now: datetime) -> float:
    """距离下一个交易日 09:15（日线数据重新可能变化的时刻）的秒数。"""
    target = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:  # 周六/周日
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 60.0)


def _daily_cache_ttl() -> float:
    """日线缓存 TTL：盘中短 TTL（最后一根K会变）；盘前/收盘后/周末缓存到下一交易日开盘前。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return _seconds_until_next_open(now)
    hm = now.hour * 100 + now.minute
    if 915 <= hm < 1505:
        return settings.daily_cache_intraday_ttl
    return _seconds_until_next_open(now)


def fetch_daily(code: str, limit: int = 40) -> pd.DataFrame:
    """个股日线：按日缓存。盘中只有最后一根K会变，用短 TTL；收盘后缓存到次日开盘前。

    30 日均线/异动等基于日线前段计算，缓存不影响结果，可砍掉约一半扫描请求量。
    """
    key = f"daily:{code}:{limit}"
    use_cache = settings.daily_cache_enabled and not settings.demo_mode
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    t0 = time.perf_counter()

    def _load() -> pd.DataFrame:
        return raw.fetch_daily(code, limit=limit)

    try:
        if use_cache:
            df = _single_flight(key, _load)
            _cache_set(key, df, _daily_cache_ttl())
        else:
            df = _load()
        _mark("daily", ok=True, detail=code, ms=(time.perf_counter() - t0) * 1000)
        return df
    except Exception as e:
        _mark("daily", ok=False, detail=f"{code}:{e}", ms=(time.perf_counter() - t0) * 1000)
        raise


def demo_minute(code: str) -> pd.DataFrame:
    return raw.demo_minute(code)


def fetch_index_snapshot(
    codes: list[str] | None = None,
    *,
    ttl: float = 30.0,
) -> dict[str, dict[str, Any]]:
    """大盘指数实时快照（带缓存）。默认上证+深成+创业板+沪深300，失败静默返回空。"""
    codes = codes or ["sh000001", "sz399001", "sz399006", "sh000300"]
    key = f"index:{','.join(codes)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    t0 = time.perf_counter()
    try:
        out = raw.fetch_index_quotes(codes)
        if out:
            _cache_set(key, out, ttl)
        _mark("index", ok=bool(out), detail=f"n={len(out)}", ms=(time.perf_counter() - t0) * 1000)
        return out
    except Exception as e:
        _mark("index", ok=False, detail=str(e), ms=(time.perf_counter() - t0) * 1000)
        return {}


def fetch_overnight_global() -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        out = raw.fetch_overnight_global()
        _mark("global", ok=True, detail=str(out.get("weak")), ms=(time.perf_counter() - t0) * 1000)
        return out
    except Exception as e:
        _mark("global", ok=False, detail=str(e), ms=(time.perf_counter() - t0) * 1000)
        raise


def fetch_market_breadth(*, ttl: float = 60.0) -> dict[str, Any]:
    """市场广度（涨跌家数/涨停/炸板/连板/晋级），失败返回空指标。"""
    if settings.demo_mode:
        return {
            "n_up": 2100,
            "n_down": 1800,
            "zt_count": 45,
            "dt_count": 8,
            "zhaban_rate": 0.22,
            "max_lianban": 4,
            "promotion_rate": 0.35,
            "as_of": datetime.now().strftime("%Y%m%d"),
            "errors": [],
            "demo": True,
        }
    cached = _cache_get("market_breadth")
    if cached is not None:
        return cached

    def _load() -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            out = raw.fetch_market_breadth()
            if out:
                _cache_set("market_breadth", out, ttl)
            _mark(
                "breadth",
                ok=bool(out.get("zt_count") is not None or out.get("n_up") is not None),
                detail=f"zt={out.get('zt_count')}",
                ms=(time.perf_counter() - t0) * 1000,
            )
            return out
        except Exception as e:
            _mark("breadth", ok=False, detail=str(e), ms=(time.perf_counter() - t0) * 1000)
            return {
                "n_up": None,
                "n_down": None,
                "zt_count": None,
                "dt_count": None,
                "zhaban_rate": None,
                "max_lianban": None,
                "promotion_rate": None,
                "errors": [str(e)],
            }

    return _single_flight("market_breadth", _load, wait_timeout=ttl + 30)
