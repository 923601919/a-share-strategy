from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from config import settings
from db import (
    cancel_sim_orders_by_types,
    get_open_sim_position_by_code,
    get_sim_account,
    get_sim_position,
    get_watch_source,
    insert_sim_order,
    list_sim_orders,
    list_sim_positions,
    list_sim_trades,
    open_sim_position_tx,
    reset_sim_account,
    sell_sim_position_tx,
    update_sim_position,
)
from providers import akshare_client as mkt


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _parse_local_date(iso_or_date: str | None):
    """ISO / YYYY-MM-DD -> date（本地日历日）。"""
    from datetime import date as date_cls

    if not iso_or_date:
        return None
    s = str(iso_or_date).strip()
    try:
        if "T" in s or s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone().date() if dt.tzinfo else dt.date()
        return date_cls.fromisoformat(s[:10])
    except Exception:
        try:
            return date_cls.fromisoformat(s[:10])
        except Exception:
            return None


def is_t1_sellable(*, opened_at: str | None, as_of: str | None = None) -> bool:
    """
    A股 T+1：买入当日不可卖出。
    以本地日历日比较（研究工具近似；未接交易日历）。
    """
    open_d = _parse_local_date(opened_at)
    asof_d = _parse_local_date(as_of) if as_of else datetime.now().astimezone().date()
    if open_d is None or asof_d is None:
        return False
    return asof_d > open_d


def t1_block_reason(*, opened_at: str | None, as_of: str | None = None) -> str | None:
    if is_t1_sellable(opened_at=opened_at, as_of=as_of):
        return None
    open_d = _parse_local_date(opened_at)
    if open_d is None:
        return "无法判定开仓日，禁止卖出（T+1）"
    return f"T+1限制：{open_d.isoformat()} 开仓，次日方可卖出"


def _fee(amount: float) -> float:
    return max(settings.sim_min_commission, abs(amount) * settings.sim_commission_rate)


def _lot_shares(budget: float, price: float) -> int:
    """按手（100股）向下取整。"""
    if price <= 0 or budget <= 0:
        return 0
    raw = int(budget // price)
    return (raw // 100) * 100


def _tp_sl_for_source(
    source: str,
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
) -> tuple[float, float]:
    src = (source or "manual").strip().lower()
    default_tp = {
        "fenshi": settings.sim_take_profit_pct_fenshi,
        "longtou": settings.sim_take_profit_pct_longtou,
    }.get(src, settings.sim_take_profit_pct)
    default_sl = {
        "fenshi": settings.sim_stop_loss_pct_fenshi,
        "longtou": settings.sim_stop_loss_pct_longtou,
    }.get(src, settings.sim_stop_loss_pct)
    tp = float(take_profit_pct if take_profit_pct is not None else default_tp)
    sl = float(stop_loss_pct if stop_loss_pct is not None else default_sl)
    return tp, sl


def _position_source(pos: dict[str, Any]) -> str:
    src = str(pos.get("source") or "").strip().lower()
    if src in ("fenshi", "longtou", "manual"):
        return src
    code = str(pos.get("code") or "").zfill(6)
    from_watch = get_watch_source(code)
    return str(from_watch or "manual").strip().lower()


def _tp_sl_prices(
    cost: float,
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
    *,
    source: str = "manual",
) -> tuple[float, float, float, float]:
    tp_pct, sl_pct = _tp_sl_for_source(source, take_profit_pct, stop_loss_pct)
    tp_price = round(cost * (1 + tp_pct / 100), 3)
    sl_price = round(cost * (1 - sl_pct / 100), 3)
    return tp_pct, sl_pct, tp_price, sl_price


def design_position_size(
    *,
    price: float,
    cash: float,
    open_count: int,
    entry_score: float | None = None,
) -> dict[str, Any]:
    """按权益比例自动设计仓位：默认约 20%，高分略加仓，受最大持仓数约束。"""
    if price <= 0:
        return {"shares": 0, "budget": 0.0, "reason": "无有效价格"}

    slots_left = max(0, settings.sim_max_positions - open_count)
    if slots_left <= 0:
        return {"shares": 0, "budget": 0.0, "reason": f"已达最大持仓数 {settings.sim_max_positions}"}

    pct = settings.sim_position_pct
    score = float(entry_score or 0)
    if score >= 80:
        pct = min(0.3, pct + 0.05)
    elif score >= 65:
        pct = min(0.25, pct + 0.02)
    elif score > 0 and score < 50:
        pct = max(0.1, pct - 0.05)

    # 剩余名额均分与目标比例取较小，避免后期现金不足
    even_pct = 1.0 / max(slots_left, 1)
    use_pct = min(pct, even_pct, 0.35)
    budget = cash * use_pct
    shares = _lot_shares(budget, price)
    if shares <= 0:
        # 尝试用更小仓位买一手
        one_lot_cost = price * 100
        if cash >= one_lot_cost * 1.01:
            shares = 100
            budget = one_lot_cost
        else:
            return {
                "shares": 0,
                "budget": round(budget, 2),
                "pct": round(use_pct * 100, 2),
                "reason": "现金不足以买入1手",
            }

    return {
        "shares": shares,
        "budget": round(shares * price, 2),
        "pct": round(use_pct * 100, 2),
        "reason": f"目标仓位约{use_pct*100:.0f}%·可买{shares}股",
    }


def _tp_sl_order_payloads(pos: dict[str, Any]) -> list[dict[str, Any]]:
    """构建止盈/止损单 payload（不落库）。"""
    orders: list[dict[str, Any]] = []
    tp = float(pos.get("take_profit_price") or 0)
    sl = float(pos.get("stop_loss_price") or 0)
    tp_pct = pos.get("take_profit_pct")
    sl_pct = pos.get("stop_loss_pct")
    src = _position_source(pos)
    src_label = {"fenshi": "进攻分时", "longtou": "龙头低吸"}.get(src, "默认")
    if tp > 0:
        orders.append(
            {
                "code": pos["code"],
                "name": pos["name"],
                "side": "sell",
                "order_type": "take_profit",
                "trigger_price": tp,
                "trigger_pct": tp_pct,
                "reason": f"止盈({src_label})：相对成本涨幅达 {tp_pct}%",
            }
        )
    if sl > 0:
        orders.append(
            {
                "code": pos["code"],
                "name": pos["name"],
                "side": "sell",
                "order_type": "stop_loss",
                "trigger_price": sl,
                "trigger_pct": sl_pct,
                "reason": f"止损：相对成本跌幅达 {sl_pct}%",
            }
        )
    return orders


def _create_tp_sl_orders(pos: dict[str, Any]) -> list[dict[str, Any]]:
    return [insert_sim_order({**o, "position_id": pos["id"]}) for o in _tp_sl_order_payloads(pos)]


def open_position_from_watch(
    *,
    code: str,
    name: str,
    price: float,
    entry_score: float | None = None,
    note: str = "",
    source: str = "manual",
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
) -> dict[str, Any]:
    """加入自选时自动开仓 + 生成止盈/止损条件单。"""
    code = code.strip().zfill(6)
    existing = get_open_sim_position_by_code(code)
    if existing:
        return {
            "ok": False,
            "skipped": True,
            "reason": "已有持仓，未重复开仓",
            "position": existing,
            "orders": [o for o in list_sim_orders(status="active") if o.get("position_id") == existing["id"]],
        }

    acct = get_sim_account()
    open_pos = list_sim_positions(status="open")
    sizing = design_position_size(
        price=price,
        cash=float(acct["cash"]),
        open_count=len(open_pos),
        entry_score=entry_score,
    )
    shares = int(sizing.get("shares") or 0)
    if shares <= 0 or price <= 0:
        return {
            "ok": False,
            "skipped": True,
            "reason": sizing.get("reason") or "无法开仓",
            "sizing": sizing,
            "account": acct,
        }

    amount = shares * price
    fee = _fee(amount)
    total = amount + fee
    cash = float(acct["cash"])
    if total > cash:
        shares = _lot_shares(cash - settings.sim_min_commission, price)
        if shares <= 0:
            return {
                "ok": False,
                "skipped": True,
                "reason": "现金不足",
                "sizing": sizing,
                "account": acct,
            }
        amount = shares * price
        fee = _fee(amount)
        total = amount + fee

    tp_pct, sl_pct, tp_price, sl_price = _tp_sl_prices(
        price, take_profit_pct, stop_loss_pct, source=source
    )
    now = _now()
    pos_payload = {
        "code": code,
        "name": name,
        "shares": shares,
        "cost_price": price,
        "opened_at": now,
        "take_profit_pct": tp_pct,
        "stop_loss_pct": sl_pct,
        "take_profit_price": tp_price,
        "stop_loss_price": sl_price,
        "entry_score": entry_score,
        "note": note,
        "source": source,
    }
    trade_payload = {
        "code": code,
        "name": name,
        "side": "buy",
        "shares": shares,
        "price": price,
        "amount": round(amount, 2),
        "fee": round(fee, 2),
        "pnl": None,
        "pnl_pct": None,
        "reason": "watch_auto_open",
        "order_id": None,
        "traded_at": now,
        "meta": json.dumps({"sizing": sizing}, ensure_ascii=False),
    }
    tx = open_sim_position_tx(
        position=pos_payload,
        trade=trade_payload,
        cash_after=cash - total,
        orders=_tp_sl_order_payloads(pos_payload),
    )
    return {
        "ok": True,
        "skipped": False,
        "position": tx["position"],
        "orders": tx["orders"],
        "trade": tx["trade"],
        "sizing": sizing,
        "account": tx["account"],
    }


def redesign_orders_for_position(
    pos: dict[str, Any],
    *,
    current_price: float | None = None,
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
) -> list[dict[str, Any]]:
    """复盘时重设止盈/止损条件单（相对成本价）。"""
    cost = float(pos.get("cost_price") or 0)
    if cost <= 0:
        return []
    src = _position_source(pos)
    tp_pct, sl_pct, tp_price, sl_price = _tp_sl_prices(
        cost, take_profit_pct, stop_loss_pct, source=src
    )

    # 若已大幅浮盈，可将止损抬到成本附近（保本），止盈保持最大涨幅目标
    px = float(current_price or 0)
    if px > 0 and cost > 0:
        unreal = (px / cost - 1) * 100
        if unreal >= tp_pct * 0.6:
            # 已走大部分止盈空间：止损抬到成本+1%，锁定部分利润
            sl_price = round(max(sl_price, cost * 1.01), 3)
            sl_pct = round((sl_price / cost - 1) * 100, 2)

    update_sim_position(
        int(pos["id"]),
        take_profit_pct=tp_pct,
        stop_loss_pct=sl_pct,
        take_profit_price=tp_price,
        stop_loss_price=sl_price,
        source=src,
    )
    cancel_sim_orders_by_types(int(pos["id"]), ["take_profit", "stop_loss"])
    refreshed = get_sim_position(int(pos["id"])) or pos
    refreshed["take_profit_pct"] = tp_pct
    refreshed["stop_loss_pct"] = sl_pct
    refreshed["take_profit_price"] = tp_price
    refreshed["stop_loss_price"] = sl_price
    return _create_tp_sl_orders(refreshed)


def sell_position(
    position_id: int,
    *,
    price: float,
    reason: str = "manual",
    order_id: int | None = None,
) -> dict[str, Any]:
    pos = get_sim_position(position_id)
    if not pos or pos.get("status") != "open":
        return {"ok": False, "reason": "持仓不存在或已平仓"}
    if price <= 0:
        return {"ok": False, "reason": "无效卖出价"}

    blocked = t1_block_reason(opened_at=str(pos.get("opened_at") or ""))
    if blocked:
        return {"ok": False, "reason": blocked, "error_code": "t1_locked"}

    shares = int(pos["shares"])
    cost = float(pos["cost_price"])
    amount = shares * price
    fee = _fee(amount)
    cost_amount = shares * cost
    pnl = amount - fee - cost_amount
    pnl_pct = (price / cost - 1.0) * 100 if cost > 0 else 0.0
    now = _now()

    acct = get_sim_account()
    cash_after = float(acct["cash"]) + amount - fee
    trade_payload = {
        "code": pos["code"],
        "name": pos["name"],
        "side": "sell",
        "shares": shares,
        "price": price,
        "amount": round(amount, 2),
        "fee": round(fee, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "traded_at": now,
        "meta": json.dumps({"cost_price": cost}, ensure_ascii=False),
    }
    tx = sell_sim_position_tx(
        position_id=position_id,
        trade=trade_payload,
        cash_after=cash_after,
        order_id=order_id,
        fill_price=price if order_id else None,
    )
    return {
        "ok": True,
        "trade": tx["trade"],
        "position": tx["position"],
        "account": tx["account"],
    }


def evaluate_orders(*, quotes: dict[str, dict] | None = None) -> dict[str, Any]:
    """检查活跃止盈/止损是否触发并自动卖出。"""
    active = [o for o in list_sim_orders(status="active") if o.get("side") == "sell"]
    if not active:
        return {"checked": 0, "filled": [], "messages": []}

    codes = list({str(o["code"]).zfill(6) for o in active})
    if quotes is None:
        try:
            quotes = mkt.fetch_realtime_quotes(codes)
        except Exception:
            quotes = {}

    filled: list[dict[str, Any]] = []
    messages: list[str] = []
    # 同一持仓只成交一次（优先止损）
    handled_pos: set[int] = set()
    ordered = sorted(
        active,
        key=lambda o: 0 if o.get("order_type") == "stop_loss" else 1,
    )

    for o in ordered:
        pid = int(o.get("position_id") or 0)
        if pid in handled_pos:
            continue
        code = str(o["code"]).zfill(6)
        q = (quotes or {}).get(code) or {}
        px = float(q.get("price") or 0)
        if px <= 0:
            continue
        trigger = float(o.get("trigger_price") or 0)
        otype = str(o.get("order_type") or "")
        hit = False
        if otype == "take_profit" and trigger > 0 and px >= trigger:
            hit = True
        elif otype == "stop_loss" and trigger > 0 and px <= trigger:
            hit = True
        if not hit:
            continue
        res = sell_position(
            pid,
            price=px,
            reason=otype,
            order_id=int(o["id"]),
        )
        if res.get("ok"):
            handled_pos.add(pid)
            filled.append(res)
            trade = res.get("trade") or {}
            messages.append(
                f"{code} {otype} @ {px:.2f} 盈亏 {trade.get('pnl')} ({trade.get('pnl_pct')}%)"
            )
        elif res.get("error_code") == "t1_locked":
            messages.append(f"{code} 触发{otype}但受T+1限制，未卖出")

    return {"checked": len(active), "filled": filled, "messages": messages}


def redesign_all_open_orders(*, quotes: dict[str, dict] | None = None) -> dict[str, Any]:
    """复盘：先撮合触发单，再为剩余持仓重设止盈止损。"""
    eval_res = evaluate_orders(quotes=quotes)
    positions = list_sim_positions(status="open")
    codes = [str(p["code"]).zfill(6) for p in positions]
    if quotes is None and codes:
        try:
            quotes = mkt.fetch_realtime_quotes(codes)
        except Exception:
            quotes = {}

    redesigned: list[dict[str, Any]] = []
    for pos in positions:
        code = str(pos["code"]).zfill(6)
        px = float(((quotes or {}).get(code) or {}).get("price") or 0)
        orders = redesign_orders_for_position(pos, current_price=px or None)
        redesigned.append({"position_id": pos["id"], "code": code, "orders": orders, "price": px})

    return {
        "evaluated": eval_res,
        "redesigned": redesigned,
        "account": get_sim_account(),
        "open_positions": list_sim_positions(status="open"),
        "active_orders": list_sim_orders(status="active"),
    }


def sim_overview() -> dict[str, Any]:
    acct = get_sim_account()
    positions = list_sim_positions(status="open")
    codes = [str(p["code"]).zfill(6) for p in positions]
    quotes: dict[str, dict] = {}
    if codes:
        try:
            quotes = mkt.fetch_realtime_quotes(codes)
        except Exception:
            quotes = {}

    pos_out = []
    market_value = 0.0
    for p in positions:
        code = str(p["code"]).zfill(6)
        q = quotes.get(code) or {}
        px = float(q.get("price") or p.get("cost_price") or 0)
        shares = int(p["shares"])
        mv = px * shares
        cost = float(p["cost_price"])
        unreal = (px / cost - 1) * 100 if cost > 0 else 0.0
        unreal_pnl = (px - cost) * shares
        market_value += mv
        sellable = is_t1_sellable(opened_at=str(p.get("opened_at") or ""))
        pos_out.append(
            {
                **p,
                "quote_price": px,
                "market_value": round(mv, 2),
                "unrealized_pnl": round(unreal_pnl, 2),
                "unrealized_pct": round(unreal, 2),
                "quote_pct": q.get("pct"),
                "t1_sellable": sellable,
                "t1_lock_reason": None
                if sellable
                else t1_block_reason(opened_at=str(p.get("opened_at") or "")),
            }
        )

    cash = float(acct["cash"])
    equity = cash + market_value
    initial = float(acct["initial_capital"])
    trades = list_sim_trades(limit=500)
    sells = [t for t in trades if t.get("side") == "sell" and t.get("pnl") is not None]
    win = [t for t in sells if float(t.get("pnl") or 0) > 0]
    total_realized = sum(float(t.get("pnl") or 0) for t in sells)

    return {
        "account": {
            **acct,
            "market_value": round(market_value, 2),
            "equity": round(equity, 2),
            "total_pnl": round(equity - initial, 2),
            "total_pnl_pct": round((equity / initial - 1) * 100, 2) if initial else 0,
            "realized_pnl": round(total_realized, 2),
        },
        "positions": pos_out,
        "orders": list_sim_orders(status="active"),
        "trades": trades[:100],
        "stats": {
            "open_count": len(positions),
            "max_positions": settings.sim_max_positions,
            "trade_count": len(trades),
            "sell_count": len(sells),
            "win_count": len(win),
            "win_rate": round(len(win) / len(sells) * 100, 1) if sells else None,
            "avg_sell_pnl_pct": round(
                sum(float(t.get("pnl_pct") or 0) for t in sells) / len(sells), 2
            )
            if sells
            else None,
            "take_profit_pct": settings.sim_take_profit_pct,
            "stop_loss_pct": settings.sim_stop_loss_pct,
            "take_profit_by_source": {
                "fenshi": settings.sim_take_profit_pct_fenshi,
                "longtou": settings.sim_take_profit_pct_longtou,
                "manual": settings.sim_take_profit_pct,
            },
            "stop_loss_by_source": {
                "fenshi": settings.sim_stop_loss_pct_fenshi,
                "longtou": settings.sim_stop_loss_pct_longtou,
                "manual": settings.sim_stop_loss_pct,
            },
        },
    }


def reset_sim(initial: float | None = None) -> dict[str, Any]:
    reset_sim_account(initial)
    return sim_overview()
