from __future__ import annotations

import logging
import threading
from datetime import date, timedelta

from config import settings
from db import get_trade_calendar, save_trade_calendar

logger = logging.getLogger("trade_calendar")

_lock = threading.Lock()
# 进程内缓存: year -> 排序后的交易日列表（下载/读库失败时该年无条目，走工作日兜底）
_cache: dict[int, list[date]] = {}
_fetch_failed_years: set[int] = set()


def _weekday_dates(year: int) -> list[date]:
    """兜底：全年周一至周五视为交易日（无节假日信息，长假前后会偏松）。"""
    d = date(year, 1, 1)
    out: list[date] = []
    while d.year == year:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _fetch_year_from_akshare(year: int) -> list[date] | None:
    if settings.demo_mode:
        return None
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        dates = df[col]
        out = []
        for v in dates:
            try:
                d = v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
            except Exception:
                continue
            if d.year == year:
                out.append(d)
        return out or None
    except Exception as e:
        logger.warning("trade calendar fetch failed for %s: %s", year, e)
        return None


def _load_year(year: int) -> list[date] | None:
    cached = _cache.get(year)
    if cached is not None:
        return cached
    with _lock:
        if year in _cache:
            return _cache[year]
        dates_iso = get_trade_calendar(year)
        if not dates_iso:
            if year in _fetch_failed_years:
                return None
            fetched = _fetch_year_from_akshare(year)
            if fetched:
                try:
                    save_trade_calendar(year, [d.isoformat() for d in fetched])
                except Exception:
                    pass
                dates_iso = [d.isoformat() for d in fetched]
            else:
                _fetch_failed_years.add(year)
                return None
        parsed: list[date] = []
        for s in dates_iso:
            try:
                parsed.append(date.fromisoformat(str(s)[:10]))
            except Exception:
                continue
        if not parsed:
            return None
        parsed.sort()
        _cache[year] = parsed
        return parsed


def _calendar_dates_spanning(start: date, end: date) -> list[date] | None:
    """取覆盖 [start, end] 所需年份的日历数据；任一年失败返回 None（走兜底）。"""
    out: list[date] = []
    for year in range(start.year, end.year + 1):
        ds = _load_year(year)
        if ds is None:
            return None
        out.extend(ds)
    out.sort()
    return out


def calendar_available(start: date, end: date) -> bool:
    return _calendar_dates_spanning(start, end) is not None


def is_trade_date(d: date) -> bool:
    """是否 A 股交易日。日历不可用时回退工作日近似。"""
    ds = _calendar_dates_spanning(d, d)
    if ds is None:
        return d.weekday() < 5
    return d in set(ds)


def trade_days_between(start: date, end: date) -> int:
    """(start, end] 区间内的交易日数。日历不可用时回退工作日近似。"""
    if end <= start:
        return 0
    ds = _calendar_dates_spanning(start, end)
    if ds is None:
        days = (end - start).days
        return sum(1 for i in range(1, days + 1) if (start + timedelta(days=i)).weekday() < 5)
    s = set(ds)
    n = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur in s:
            n += 1
        cur += timedelta(days=1)
    return n


def nth_trade_date_after(d: date, n: int) -> date | None:
    """d 之后第 n 个交易日（n>=1）。日历不可用或越界（数据只到当年末）返回 None。"""
    if n < 1:
        return d
    horizon = d + timedelta(days=n * 10 + 15)
    ds = _calendar_dates_spanning(d, horizon)
    if ds is None:
        cur = d
        k = 0
        while k < n:
            cur += timedelta(days=1)
            if cur.weekday() < 5:
                k += 1
            if (cur - d).days > n * 10 + 15:
                return None
        return cur
    future = [x for x in ds if x > d]
    if len(future) < n:
        return None
    return future[n - 1]
