from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal

from config import settings
from db import save_scan_snapshot
from providers import akshare_client as mkt
from rules.fenshi import in_session_bucket, score_offensive_fenshi
from rules.risk import anomaly_30d_pct, risk_flags


SessionFilter = Literal["auto", "morning", "afternoon", "any"]


def _is_st(name: str) -> bool:
    n = name.upper()
    return "ST" in n or "退" in name


def _filter_spot(df, min_amount_yi: float, min_pct: float, max_pct: float, limit: int):
    amount_floor = min_amount_yi * 1e8
    out = df.copy()
    out = out[~out["name"].astype(str).map(_is_st)]
    out = out[out["amount"] >= amount_floor]
    out = out[out["pct"] >= min_pct]
    out = out[out["pct"] <= max_pct]
    # 新浪无量比时，用涨幅+成交额排序
    sort_cols = [c for c in ("pct", "volume_ratio", "amount") if c in out.columns]
    out = out.sort_values(sort_cols, ascending=False)
    return out.head(limit)


def _score_from_spot(row: dict[str, Any]) -> dict[str, Any]:
    """分时不可用时的兜底打分，保证仍能拉开选股差异。"""
    pct = float(row.get("pct") or 0)
    amount = float(row.get("amount") or 0)
    vr = float(row.get("volume_ratio") or 0)
    open_p = float(row.get("open") or 0)
    price = float(row.get("price") or 0)
    high = float(row.get("high") or 0)
    low = float(row.get("low") or 0)

    score = 0.0
    reasons: list[str] = ["分时源切换/失败，使用盘口代理打分"]
    if pct >= 5:
        score += 30
        reasons.append(f"强势涨幅{pct:.2f}%")
    elif pct >= 3:
        score += 20
        reasons.append(f"涨幅{pct:.2f}%")
    elif pct >= 2:
        score += 12
        reasons.append(f"温和上涨{pct:.2f}%")

    yi = amount / 1e8
    if yi >= 5:
        score += 25
        reasons.append(f"成交活跃{yi:.1f}亿")
    elif yi >= 2:
        score += 15
        reasons.append(f"成交{yi:.1f}亿")
    elif yi >= 1:
        score += 8
        reasons.append(f"成交过亿")

    if vr >= 2:
        score += 20
        reasons.append(f"量比{vr:.2f}")
    elif vr >= 1.3:
        score += 10
        reasons.append(f"量比{vr:.2f}")

    # 偏进攻：收在日内偏高位
    if high > low > 0 and price > 0:
        pos = (price - low) / (high - low)
        if pos >= 0.7:
            score += 15
            reasons.append("靠近日内高位")
        elif pos >= 0.5:
            score += 8
            reasons.append("站上日内中位")

    if open_p > 0 and price >= open_p:
        score += 10
        reasons.append("现价不低于开盘")

    return {
        "score": round(min(score, 100.0), 1),
        "above_vwap": None,
        "slope": None,
        "vol_expand": None,
        "vwap": None,
        "reasons": reasons,
        "proxy": True,
    }


def _enrich_one(row: dict[str, Any], hot_codes: set[str], hot_names: set[str]) -> dict[str, Any] | None:
    code = str(row["code"]).zfill(6)
    name = str(row["name"])
    minute = None
    daily = None
    minute_err = None

    try:
        if settings.demo_mode:
            minute = mkt.demo_minute(code)
        else:
            try:
                minute = mkt.fetch_minute(code)
            except Exception as e:
                minute_err = e
                minute = None
            try:
                daily = mkt.fetch_daily(code, limit=40)
            except Exception:
                daily = None
    except Exception as e:
        minute_err = e

    if minute is not None and len(minute) >= 10:
        fenshi = score_offensive_fenshi(minute)
        fenshi["proxy"] = False
    else:
        fenshi = _score_from_spot(row)
        if minute_err is not None:
            fenshi["reasons"] = [f"分时拉取失败已降级: {minute_err}"] + list(fenshi.get("reasons") or [])

    anom = anomaly_30d_pct(daily) if daily is not None else {
        "pct_from_low": 0.0,
        "ma5": None,
        "last_close": float(row.get("price") or 0),
        "last_open": float(row.get("open") or 0),
    }
    risk = risk_flags(
        anom["pct_from_low"],
        price=float(row.get("price") or 0),
        ma5=anom.get("ma5"),
        open_price=float(row.get("open") or 0),
        warn=settings.anomaly_warn_pct,
        block=settings.anomaly_block_pct,
    )
    if risk["level"] == "block":
        return None

    reasons = list(fenshi.get("reasons") or [])
    in_hot = code in hot_codes or name in hot_names
    if in_hot:
        reasons.insert(0, "热门板块领涨/相关")
        fenshi["score"] = min(100.0, float(fenshi.get("score") or 0) + 10)

    return {
        "code": code,
        "name": name,
        "pct": round(float(row.get("pct") or 0), 2),
        "price": round(float(row.get("price") or 0), 3),
        "amount": float(row.get("amount") or 0),
        "turnover": round(float(row.get("turnover") or 0), 2),
        "volume_ratio": round(float(row.get("volume_ratio") or 0), 2),
        "score": fenshi.get("score") or 0,
        "in_hot_board": in_hot,
        "reasons": reasons,
        "risk": {
            **risk,
            "anomaly_pct": anom["pct_from_low"],
            "ma5": anom.get("ma5"),
        },
        "fenshi": {
            "above_vwap": fenshi.get("above_vwap"),
            "slope": fenshi.get("slope"),
            "vol_expand": fenshi.get("vol_expand"),
            "vwap": fenshi.get("vwap"),
            "proxy": bool(fenshi.get("proxy")),
        },
    }


def run_scan(
    *,
    min_amount_yi: float | None = None,
    min_pct: float | None = None,
    max_pct: float | None = None,
    session: SessionFilter = "auto",
    top_n: int | None = None,
    board_top_n: int = 15,
) -> dict[str, Any]:
    min_amount_yi = min_amount_yi if min_amount_yi is not None else settings.min_amount_yi
    min_pct = min_pct if min_pct is not None else settings.min_pct
    max_pct = max_pct if max_pct is not None else settings.max_pct
    top_n = top_n if top_n is not None else settings.top_n_result

    bucket = in_session_bucket()
    if session == "auto" and bucket == "other" and not settings.demo_mode:
        session_note = "当前非重点扫描时段(09:45-11:00 / 13:30-14:30)，仍执行全量弱过滤"
    else:
        session_note = f"时段桶={bucket}, filter={session}"

    spot = mkt.get_spot_df()
    spot_source = mkt.last_spot_source()
    filtered = _filter_spot(
        spot,
        min_amount_yi=min_amount_yi,
        min_pct=min_pct,
        max_pct=max_pct,
        limit=settings.max_candidates_spot,
    )

    hot_boards: list[dict[str, Any]] = []
    hot_codes: set[str] = set()
    hot_names: set[str] = set()
    if not settings.demo_mode:
        try:
            hot_boards = mkt.fetch_concept_boards_top(board_top_n)
            # 用领涨股名称做热门加权（成分股接口依赖东财常失败）
            for b in hot_boards[:8]:
                leader = str(b.get("leader") or "").strip()
                if leader and leader not in ("--", "nan", "None"):
                    hot_names.add(leader)
            if hot_names:
                name_map = spot.set_index(spot["name"].astype(str))["code"].astype(str)
                for n in list(hot_names):
                    if n in name_map.index:
                        val = name_map.loc[n]
                        if hasattr(val, "iloc"):
                            hot_codes.add(str(val.iloc[0]).zfill(6))
                        else:
                            hot_codes.add(str(val).zfill(6))
        except Exception as e:
            msg = str(e)
            short = f"板块拉取失败: {msg[:120]}"
            hot_boards = [{"name": short, "pct": 0}]
    else:
        hot_boards = [{"name": "演示板块", "pct": 3.2, "up_count": 12, "leader": "演示"}]
        hot_codes = {"600812", "002212"}

    rows = filtered.to_dict(orient="records")
    results: list[dict[str, Any]] = []

    workers = 6 if not settings.demo_mode else 2
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_enrich_one, r, hot_codes, hot_names) for r in rows]
        for fut in as_completed(futs):
            item = fut.result()
            if item is not None:
                results.append(item)

    results.sort(key=lambda x: (x.get("score") or 0, x.get("pct") or 0), reverse=True)
    results = results[:top_n]
    scored = sum(1 for x in results if (x.get("score") or 0) > 0)
    fenshi_ok = sum(1 for x in results if not (x.get("fenshi") or {}).get("proxy"))

    payload = {
        "session_note": session_note,
        "data_source": {
            "spot": spot_source,
            "minute": "tencent_fallback" if spot_source != "demo" else "demo",
            "candidates": len(rows),
            "scored": scored,
            "fenshi_ok": fenshi_ok,
        },
        "hot_boards": hot_boards[:board_top_n],
        "params": {
            "min_amount_yi": min_amount_yi,
            "min_pct": min_pct,
            "max_pct": max_pct,
            "session": session,
            "top_n": top_n,
            "demo_mode": settings.demo_mode,
        },
        "count": len(results),
        "items": results,
    }
    try:
        save_scan_snapshot(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass
    return payload


def watchlist_quotes(codes: list[str]) -> list[dict[str, Any]]:
    if not codes:
        return []
    spot = mkt.get_spot_df()
    spot["code"] = spot["code"].astype(str).str.zfill(6)
    out = []
    for code in codes:
        code = code.zfill(6)
        row = spot[spot["code"] == code]
        base = {
            "code": code,
            "name": "",
            "price": 0,
            "pct": 0,
            "open": 0,
        }
        if not row.empty:
            r = row.iloc[0]
            base.update(
                {
                    "name": str(r["name"]),
                    "price": float(r["price"]),
                    "pct": float(r["pct"]),
                    "open": float(r.get("open") or 0),
                }
            )
        try:
            daily = None if settings.demo_mode else mkt.fetch_daily(code, limit=40)
            if settings.demo_mode:
                anom = {"pct_from_low": 40.0, "ma5": base["price"] * 0.98, "last_close": base["price"], "last_open": base["open"]}
            else:
                anom = anomaly_30d_pct(daily)
            risk = risk_flags(
                anom["pct_from_low"],
                price=base["price"],
                ma5=anom.get("ma5"),
                open_price=base["open"] or anom.get("last_open"),
                warn=settings.anomaly_warn_pct,
                block=settings.anomaly_block_pct,
            )
            base["risk"] = {**risk, "anomaly_pct": anom["pct_from_low"], "ma5": anom.get("ma5")}
        except Exception as e:
            base["risk"] = {"level": "ok", "messages": [str(e)], "anomaly_progress": 0, "anomaly_pct": 0}
        out.append(base)
    return out
