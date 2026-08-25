from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from config import settings
from db import (
    get_daily_review,
    latest_scan_snapshot,
    list_daily_reviews,
    list_watchlist,
    save_daily_review,
)
from providers import akshare_client as mkt
from rules.fenshi import score_offensive_fenshi
from rules.risk import anomaly_30d_pct, risk_flags
from services.track import enrich_watch_item


def _today() -> str:
    return datetime.now().date().isoformat()


def _build_orders_for_watch(item: dict[str, Any], quote: dict[str, Any], daily_info: dict[str, Any]) -> list[dict[str, Any]]:
    """基于进攻型分时 + 五日线/异动红线，生成次日条件单。"""
    code = str(item["code"]).zfill(6)
    name = str(item.get("name") or code)
    entry = float(item.get("entry_price") or quote.get("price") or 0)
    price = float(quote.get("price") or entry or 0)
    ma5 = daily_info.get("ma5")
    anom = float(daily_info.get("pct_from_low") or 0)
    fenshi = quote.get("fenshi") or {}
    orders: list[dict[str, Any]] = []

    # ---- 卖：保护与兑现 ----
    if ma5 and ma5 > 0:
        orders.append(
            {
                "side": "sell",
                "priority": 1,
                "code": code,
                "name": name,
                "title": "竞价/开盘跌破五日线卖出",
                "trigger": f"次日开盘价 < MA5({ma5:.2f})",
                "action": "开盘后尽快卖出或竞价卖出",
                "price_hint": round(ma5 * 0.998, 3),
                "window": "09:15-09:30 / 09:30-09:45",
                "reason": "趋势模式：开盘站不上五日线则走",
            }
        )
        orders.append(
            {
                "side": "sell",
                "priority": 2,
                "code": code,
                "name": name,
                "title": "盘中跌破五日线减仓",
                "trigger": f"现价跌破 MA5({ma5:.2f}) 并站不稳",
                "action": "减仓或清仓",
                "price_hint": round(ma5, 3),
                "window": "全天",
                "reason": "短线防守线失效",
            }
        )

    if entry > 0:
        stop = entry * 0.97
        orders.append(
            {
                "side": "sell",
                "priority": 3,
                "code": code,
                "name": name,
                "title": "入池价回撤止损",
                "trigger": f"现价 <= {stop:.2f}（入池价{entry:.2f} 回撤约3%）",
                "action": "止损卖出",
                "price_hint": round(stop, 3),
                "window": "全天",
                "reason": "自选跟踪短线风险控制",
            }
        )

    if anom >= settings.anomaly_warn_pct:
        orders.append(
            {
                "side": "sell",
                "priority": 1,
                "code": code,
                "name": name,
                "title": "异动红线附近减仓",
                "trigger": f"近30日从低点涨幅已达 {anom:.1f}%（警戒{settings.anomaly_warn_pct}%）",
                "action": "优先减仓，卡不住就走",
                "price_hint": price,
                "window": "全天",
                "reason": "龙头异动红线纪律",
            }
        )

    # ---- 买：回踩再攻 / 承接 ----
    if anom < settings.anomaly_block_pct and price > 0:
        buy_zone = None
        if ma5 and ma5 > 0:
            buy_zone = round(ma5 * 1.002, 3)
        vwap = fenshi.get("vwap")
        if vwap:
            buy_zone = round(float(vwap), 3) if buy_zone is None else round(min(buy_zone, float(vwap)), 3)

        if buy_zone:
            orders.append(
                {
                    "side": "buy",
                    "priority": 2,
                    "code": code,
                    "name": name,
                    "title": "回踩均价/五日线承接买入",
                    "trigger": f"10:00-10:40 回踩至 {buy_zone:.2f} 附近后放量再攻",
                    "action": "分批买入，确认站上均价再加",
                    "price_hint": buy_zone,
                    "window": "10:00-10:40",
                    "reason": "进攻型分时：回踩再攻买点",
                }
            )

        if fenshi.get("pullback") and fenshi.get("reattack"):
            orders.append(
                {
                    "side": "buy",
                    "priority": 1,
                    "code": code,
                    "name": name,
                    "title": "已确认回踩再攻，次日高开低吸",
                    "trigger": f"开盘不破昨日收盘/MA5，回踩不破 {buy_zone or price:.2f}",
                    "action": "低吸承接",
                    "price_hint": buy_zone or price,
                    "window": "09:45-10:40",
                    "reason": "当日分时已走出攻击形态，次日沿趋势低吸",
                }
            )

    orders.sort(key=lambda x: (0 if x["side"] == "sell" else 1, x.get("priority") or 9))
    return orders


def _analyze_one_code(code: str, name: str, price: float, entry_price: float | None = None) -> dict[str, Any]:
    daily_info = {"pct_from_low": 0.0, "ma5": None, "last_close": price}
    fenshi: dict[str, Any] = {}
    risk: dict[str, Any] = {"level": "ok", "messages": []}

    try:
        daily = None if settings.demo_mode else mkt.fetch_daily(code, limit=40)
        if daily is not None:
            daily_info = anomaly_30d_pct(daily)
            risk = risk_flags(
                daily_info["pct_from_low"],
                price=price,
                ma5=daily_info.get("ma5"),
                open_price=daily_info.get("last_open"),
                warn=settings.anomaly_warn_pct,
                block=settings.anomaly_block_pct,
            )
    except Exception as e:
        risk = {"level": "ok", "messages": [f"日线拉取失败: {e}"]}

    try:
        if settings.demo_mode:
            minute = mkt.demo_minute(code)
        else:
            minute = mkt.fetch_minute(code)
        if minute is not None and len(minute) >= 15:
            fenshi = score_offensive_fenshi(minute)
    except Exception as e:
        fenshi = {"score": 0, "reasons": [f"分时失败: {e}"]}

    ret_pct = None
    if entry_price and entry_price > 0 and price > 0:
        ret_pct = round((price / entry_price - 1.0) * 100, 2)

    return {
        "code": code,
        "name": name,
        "price": price,
        "entry_price": entry_price,
        "day_return_pct": ret_pct,
        "daily": daily_info,
        "risk": risk,
        "fenshi": {
            "score": fenshi.get("score"),
            "pullback": fenshi.get("pullback"),
            "reattack": fenshi.get("reattack"),
            "above_vwap": fenshi.get("above_vwap"),
            "vwap": fenshi.get("vwap"),
            "reasons": fenshi.get("reasons") or [],
        },
    }


def run_daily_review(*, trade_date: str | None = None, persist: bool = True) -> dict[str, Any]:
    """收盘复盘：板块/自选表现 + 次日买卖条件单。"""
    trade_date = trade_date or _today()
    notes: list[str] = []
    boards: list[dict[str, Any]] = []
    watch_reviews: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []

    # 1) 板块
    try:
        boards = mkt.fetch_concept_boards_top(12)
        notes.append(f"强势行业参考 {len(boards)} 个")
    except Exception as e:
        notes.append(f"板块拉取失败: {e}")
        boards = []

    uni_sectors: list[dict[str, Any]] = []
    try:
        uni = mkt.fetch_hot_sector_universe(industry_top=5, concept_top=3)
        uni_sectors = uni.get("sectors") or []
    except Exception as e:
        notes.append(f"强势板块成分池失败: {e}")

    # 2) 自选复盘
    watch = list_watchlist()
    spot_map: dict[str, dict[str, Any]] = {}
    try:
        spot = mkt.get_spot_df()
        spot["code"] = spot["code"].astype(str).str.zfill(6)
        for _, r in spot.iterrows():
            spot_map[str(r["code"]).zfill(6)] = r.to_dict()
    except Exception as e:
        notes.append(f"行情快照失败: {e}")

    for it in watch:
        code = str(it["code"]).zfill(6)
        row = spot_map.get(code, {})
        price = float(row.get("price") or it.get("entry_price") or 0)
        analyzed = _analyze_one_code(code, str(it.get("name") or code), price, float(it.get("entry_price") or 0) or None)
        # 同步 track 收益占位
        try:
            enrich_watch_item(it, {"price": price, "pct": float(row.get("pct") or 0)}, force_refresh=False)
        except Exception:
            pass

        quote_for_order = {
            "price": price,
            "fenshi": analyzed.get("fenshi") or {},
        }
        stock_orders = _build_orders_for_watch(it, quote_for_order, analyzed.get("daily") or {})
        orders.extend(stock_orders)
        watch_reviews.append(
            {
                **analyzed,
                "entry_score": it.get("entry_score"),
                "source": it.get("source"),
                "note": it.get("note"),
                "orders_count": len(stock_orders),
            }
        )

    # 3) 最近扫描结果里高分票：补次日观察/买入条件（未在自选中的）
    scan_items: list[dict[str, Any]] = []
    snap = latest_scan_snapshot()
    if snap:
        try:
            payload = json.loads(snap["payload"])
            scan_items = list(payload.get("items") or [])[:15]
            notes.append(f"纳入最近扫描 {len(scan_items)} 只参考")
        except Exception:
            pass

    watched_codes = {str(w["code"]).zfill(6) for w in watch}
    for s in scan_items:
        code = str(s.get("code") or "").zfill(6)
        if not code or code in watched_codes:
            continue
        if float(s.get("score") or 0) < 70:
            continue
        name = str(s.get("name") or code)
        price = float(s.get("price") or 0)
        analyzed = _analyze_one_code(code, name, price, None)
        # 仅给买入观察单，不做止损（尚未持仓）
        daily = analyzed.get("daily") or {}
        ma5 = daily.get("ma5")
        buy_zone = float(ma5) if ma5 else price
        if buy_zone and analyzed.get("risk", {}).get("level") != "block":
            orders.append(
                {
                    "side": "buy",
                    "priority": 3,
                    "code": code,
                    "name": name,
                    "title": "扫描高分票观察买入",
                    "trigger": f"10:00-10:40 回踩 {buy_zone:.2f} 附近后放量再攻，涨幅仍 <6%",
                    "action": "观察后分批买，确认形态再动手",
                    "price_hint": round(buy_zone, 3),
                    "window": "10:00-10:40",
                    "reason": f"扫描得分{s.get('score')}，尚未入自选",
                }
            )

    # 4) 复盘结论
    wins = [w for w in watch_reviews if (w.get("day_return_pct") or 0) > 0]
    losses = [w for w in watch_reviews if (w.get("day_return_pct") or 0) < 0]
    reattack = [w for w in watch_reviews if (w.get("fenshi") or {}).get("pullback") and (w.get("fenshi") or {}).get("reattack")]

    summary = {
        "trade_date": trade_date,
        "watch_count": len(watch_reviews),
        "watch_up": len(wins),
        "watch_down": len(losses),
        "reattack_count": len(reattack),
        "buy_orders": sum(1 for o in orders if o["side"] == "buy"),
        "sell_orders": sum(1 for o in orders if o["side"] == "sell"),
        "top_boards": [{"name": b.get("name"), "pct": b.get("pct")} for b in boards[:8]],
        "notes": notes,
        "verdict": _verdict(watch_reviews, boards),
    }

    # 去重条件单（同 code+title）
    seen: set[str] = set()
    uniq_orders: list[dict[str, Any]] = []
    for o in sorted(orders, key=lambda x: (0 if x["side"] == "sell" else 1, x.get("priority") or 9)):
        key = f"{o['side']}:{o['code']}:{o['title']}"
        if key in seen:
            continue
        seen.add(key)
        uniq_orders.append(o)

    payload = {
        "trade_date": trade_date,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "boards": boards[:15],
        "universe_sectors": uni_sectors[:10],
        "watch_reviews": watch_reviews,
        "orders": uniq_orders,
        "next_day_checklist": [
            "09:15-09:25 看竞价：开盘能否站上五日线",
            "重点卖单优先执行（跌破五日线/异动红线）",
            "10:00-10:40 只做回踩均价后放量再攻的买单",
            "涨幅已达/超过6%的短线票次日谨慎追高",
            "无形态宁空仓",
        ],
    }

    if persist:
        meta = save_daily_review(trade_date, json.dumps(payload, ensure_ascii=False))
        payload["id"] = meta.get("id")
    return payload


def _verdict(watch_reviews: list[dict[str, Any]], boards: list[dict[str, Any]]) -> str:
    if not watch_reviews and not boards:
        return "数据不足，建议收盘后再跑一次复盘。"
    up = sum(1 for w in watch_reviews if (w.get("day_return_pct") or 0) > 0)
    down = sum(1 for w in watch_reviews if (w.get("day_return_pct") or 0) < 0)
    board_txt = "、".join(str(b.get("name")) for b in boards[:3]) if boards else "无明显主线"
    if up > down:
        return f"自选今日偏强（涨{up}/跌{down}）。主线参考：{board_txt}。次日优先执行卖出保护单，买点只做回踩再攻。"
    if down > up:
        return f"自选今日偏弱（涨{up}/跌{down}）。主线参考：{board_txt}。次日先处理破位卖单，减少新开仓。"
    return f"自选分化。主线参考：{board_txt}。次日严格按时段与条件单执行。"


def get_review(trade_date: str | None = None) -> dict[str, Any] | None:
    row = get_daily_review(trade_date)
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        payload = {}
    payload["id"] = row.get("id")
    payload["trade_date"] = row.get("trade_date")
    payload["saved_at"] = row.get("created_at")
    return payload


def review_history(limit: int = 30) -> list[dict[str, Any]]:
    return list_daily_reviews(limit=limit)
