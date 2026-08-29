from __future__ import annotations

import logging
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings
from db import add_watch, get_track_returns, init_db, list_watchlist, list_watch_tracks, remove_watch
from ssl_fix import apply_ssl_fix

# 必须在首次请求东财前生效
_SSL_MODE = apply_ssl_fix(insecure=not settings.ssl_verify)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

from providers import market as mkt
from services.jobs import job_store
from services.review import get_review, review_history, run_daily_review
from services.scan import ScanCancelled, run_scan, watchlist_quotes
from services.sim import (
    evaluate_orders,
    open_position_from_watch,
    reset_sim,
    sell_position,
    sim_overview,
)
from services.track import (
    enrich_watch_item,
    expire_past_t3_watchlist,
    refresh_track_returns,
    watchlist_stats,
)

app = FastAPI(title="A-Share Strategy API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


class WatchIn(BaseModel):
    code: str
    name: str = ""
    source: Literal["manual", "fenshi", "longtou"] = "manual"
    note: str = ""
    entry_price: float | None = None
    entry_pct: float | None = None
    entry_score: float | None = None
    minute_confirmed: bool = True


class ScanIn(BaseModel):
    min_amount_yi: float | None = Field(default=None, description="成交额下限（亿）")
    min_pct: float | None = None
    max_pct: float | None = Field(default=None, description="当前涨幅上限%")
    session: Literal["auto", "morning", "afternoon", "any"] = "auto"
    top_n: int | None = None
    board_top_n: int = 15
    mode: Literal["fenshi", "leader_dip"] = "fenshi"


@app.get("/api/health")
def health():
    sources = mkt.source_health()
    return {
        "ok": True,
        "demo_mode": settings.demo_mode,
        "ssl_verify": settings.ssl_verify,
        "ssl_mode": _SSL_MODE,
        "strategy_version": settings.strategy_version,
        "scan_use_isolated": settings.scan_use_isolated,
        "sources": sources,
    }


@app.post("/api/scan")
def scan(body: ScanIn | None = None):
    """同步扫描（兼容旧客户端）；前端请优先用 /api/scan/jobs。"""
    body = body or ScanIn()
    try:
        return run_scan(
            min_amount_yi=body.min_amount_yi,
            min_pct=body.min_pct,
            max_pct=body.max_pct,
            session=body.session,
            top_n=body.top_n,
            board_top_n=body.board_top_n,
            mode=body.mode,
        )
    except ScanCancelled:
        raise HTTPException(status_code=499, detail="cancelled") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/scan/jobs")
def start_scan_job(body: ScanIn | None = None):
    """异步扫描：立即返回 job_id，前端轮询进度。"""
    body = body or ScanIn()
    params = body.model_dump()
    job = job_store.create("scan", params)

    def _run() -> None:
        def on_progress(stage: str, progress: float, message: str) -> None:
            job_store.update(
                job.id,
                stage=stage,
                progress=progress,
                message=message,
                status="running",
            )

        try:
            result = run_scan(
                min_amount_yi=body.min_amount_yi,
                min_pct=body.min_pct,
                max_pct=body.max_pct,
                session=body.session,
                top_n=body.top_n,
                board_top_n=body.board_top_n,
                mode=body.mode,
                on_progress=on_progress,
                should_cancel=lambda: job_store.is_cancelled(job.id),
            )
            job_store.update(
                job.id,
                status="done",
                stage="done",
                progress=1.0,
                message=f"完成，命中 {result.get('count', 0)} 只",
                result=result,
                timings=result.get("timings") or {},
                error_code=result.get("error_code"),
            )
        except ScanCancelled:
            job_store.update(
                job.id,
                status="cancelled",
                stage="cancelled",
                progress=1.0,
                message="已取消",
                error_code="cancelled",
            )

    job_store.run_in_background(job.id, _run)
    return job.to_public(include_result=False)


@app.get("/api/scan/jobs/{job_id}")
def get_scan_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.to_public(include_result=True)


@app.post("/api/scan/jobs/{job_id}/cancel")
def cancel_scan_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    ok = job_store.request_cancel(job_id)
    job = job_store.get(job_id)
    return {"ok": ok, **(job.to_public(include_result=False) if job else {})}


@app.get("/api/watchlist")
def get_watchlist(
    with_quotes: bool = Query(default=False),
    refresh_returns: bool = Query(default=False),
    with_risk: bool = Query(default=False, description="是否拉日线算异动（较慢）"),
):
    """默认快速返回库内自选；行情/收益刷新按需开启。超过 T+3 的条目会自动归档并移出。"""
    expired = expire_past_t3_watchlist(
        fetch_quotes=with_quotes or refresh_returns,
        force_refresh=refresh_returns,
    )
    items = list_watchlist()
    quotes: dict[str, dict] = {}
    if with_quotes and items:
        try:
            quotes = {
                q["code"]: q
                for q in watchlist_quotes(
                    [i["code"] for i in items],
                    include_risk=with_risk,
                )
            }
        except Exception:
            quotes = {}

    out = []
    if refresh_returns and items:
        # 收益刷新并行，避免串行日线
        from concurrent.futures import ThreadPoolExecutor

        def _one(it: dict) -> dict:
            q = quotes.get(str(it["code"]).zfill(6), {}) or quotes.get(it["code"], {})
            try:
                return enrich_watch_item(it, q, force_refresh=True)
            except Exception:
                return {
                    **it,
                    "quote": q,
                    "returns": [],
                    "track": {
                        "entry_price": it.get("entry_price"),
                        "entry_pct": it.get("entry_pct"),
                        "entry_score": it.get("entry_score"),
                    },
                }

        with ThreadPoolExecutor(max_workers=min(6, max(len(items), 1))) as pool:
            out = list(pool.map(_one, items))
    else:
        for it in items:
            q = quotes.get(str(it["code"]).zfill(6), {}) or quotes.get(it["code"], {})
            try:
                out.append(enrich_watch_item(it, q, force_refresh=False))
            except Exception:
                out.append(
                    {
                        **it,
                        "quote": q,
                        "returns": [],
                        "track": {
                            "entry_price": it.get("entry_price"),
                            "entry_pct": it.get("entry_pct"),
                            "entry_score": it.get("entry_score"),
                        },
                    }
                )
    return {"items": out, "stats": watchlist_stats(), "expired": expired}


@app.get("/api/watchlist/stats")
def get_watchlist_stats():
    return watchlist_stats()


@app.get("/api/watchlist/history")
def get_watchlist_history(limit: int = Query(default=100, le=500)):
    tracks = list_watch_tracks(active_only=False, limit=limit)
    rows = []
    for tr in tracks:
        rets = get_track_returns(int(tr["id"]))
        if not rets:
            rets = refresh_track_returns(tr, persist=True, force=False)
        ret_map = {r["day_offset"]: r for r in rets}
        rows.append(
            {
                **tr,
                "returns": rets,
                "t3_return_pct": ret_map.get(3, {}).get("return_pct"),
            }
        )
    return {"items": rows, "stats": watchlist_stats()}


@app.post("/api/watchlist")
def post_watchlist(body: WatchIn):
    if not body.code.strip():
        raise HTTPException(400, "code required")
    code = body.code.strip().zfill(6)
    name = body.name or code
    entry_price = body.entry_price
    entry_pct = body.entry_pct

    # 入池价强制用实时单票报价，避免扫描页演示价/脏价（常见差约一倍）写入跟踪
    try:
        q = (mkt.fetch_realtime_quotes([code]) or {}).get(code) or {}
    except Exception:
        q = {}
    live_price = float(q.get("price") or 0)
    live_pct = q.get("pct")
    if live_price > 0:
        entry_price = live_price
        if live_pct is not None:
            entry_pct = float(live_pct)
        if q.get("name"):
            name = str(q["name"])
    elif entry_price is None or entry_pct is None:
        try:
            spot = mkt.get_spot_df_or_empty()
            spot["code"] = spot["code"].astype(str).str.zfill(6)
            row = spot[spot["code"] == code]
            if not row.empty:
                r = row.iloc[0]
                if entry_price is None:
                    entry_price = float(r.get("price") or 0)
                if entry_pct is None:
                    entry_pct = float(r.get("pct") or 0)
                if not body.name and r.get("name"):
                    name = str(r["name"])
        except Exception:
            pass

    row = add_watch(
        code,
        name,
        body.source,
        body.note,
        entry_price=entry_price,
        entry_pct=entry_pct,
        entry_score=body.entry_score,
    )

    sim_result: dict = {"ok": False, "skipped": True, "reason": "无有效入池价，未开仓"}
    if entry_price and float(entry_price) > 0 and body.minute_confirmed:
        try:
            sim_result = open_position_from_watch(
                code=code,
                name=name,
                price=float(entry_price),
                entry_score=body.entry_score,
                note=body.note or "",
                source=body.source,
            )
        except Exception as e:
            sim_result = {"ok": False, "skipped": True, "reason": f"模拟开仓失败: {e}"}
    elif entry_price and float(entry_price) > 0:
        sim_result = {"ok": False, "skipped": True, "reason": "分时未确认(代理分)，未自动开仓"}

    return {**row, "sim": sim_result}


@app.delete("/api/watchlist/{code}")
def delete_watchlist(code: str):
    ok = remove_watch(code)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class ReviewIn(BaseModel):
    trade_date: str | None = Field(default=None, description="交易日 YYYY-MM-DD，默认今天")
    persist: bool = True


@app.post("/api/review/run")
def review_run(body: ReviewIn | None = None):
    body = body or ReviewIn()
    try:
        return run_daily_review(trade_date=body.trade_date, persist=body.persist)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/review/latest")
def review_latest(trade_date: str | None = Query(default=None)):
    data = get_review(trade_date)
    if not data:
        raise HTTPException(404, "暂无复盘，请先点击生成复盘")
    return data


@app.get("/api/review/history")
def review_history_api(limit: int = Query(default=20, le=60)):
    return {"items": review_history(limit=limit)}


# ---------- 模拟盘 ----------


@app.get("/api/sim")
def get_sim():
    try:
        # 打开页面时顺带检查止盈止损是否已触发
        evaluate_orders()
        return sim_overview()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/sim/evaluate")
def sim_evaluate():
    try:
        filled = evaluate_orders()
        return {"evaluate": filled, **sim_overview()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class SimSellIn(BaseModel):
    position_id: int
    price: float | None = None


@app.post("/api/sim/sell")
def sim_sell(body: SimSellIn):
    try:
        price = body.price
        if price is None or price <= 0:
            from db import get_sim_position

            pos = get_sim_position(body.position_id)
            if not pos:
                raise HTTPException(404, "持仓不存在")
            q = (mkt.fetch_realtime_quotes([pos["code"]]) or {}).get(str(pos["code"]).zfill(6)) or {}
            price = float(q.get("price") or pos.get("cost_price") or 0)
        res = sell_position(body.position_id, price=float(price), reason="manual")
        if not res.get("ok"):
            raise HTTPException(400, res.get("reason") or "卖出失败")
        return {**res, "overview": sim_overview()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class SimResetIn(BaseModel):
    initial_capital: float | None = Field(default=None, description="重置初始资金，默认10万")


@app.post("/api/sim/reset")
def sim_reset(body: SimResetIn | None = None):
    body = body or SimResetIn()
    try:
        return reset_sim(body.initial_capital)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
