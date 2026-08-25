from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings
from db import add_watch, init_db, list_watchlist, list_watch_tracks, remove_watch
from ssl_fix import apply_ssl_fix

# 必须在首次请求东财前生效
_SSL_MODE = apply_ssl_fix(insecure=not settings.ssl_verify)

from providers import akshare_client as mkt
from services.review import get_review, review_history, run_daily_review
from services.scan import run_scan, watchlist_quotes
from services.track import enrich_watch_item, refresh_track_returns, watchlist_stats

app = FastAPI(title="A-Share Strategy API", version="0.1.0")
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


class ScanIn(BaseModel):
    min_amount_yi: float | None = Field(default=None, description="成交额下限（亿）")
    min_pct: float | None = None
    max_pct: float | None = Field(default=None, description="当前涨幅上限%，严格小于该值")
    session: Literal["auto", "morning", "afternoon", "any"] = "auto"
    top_n: int | None = None
    board_top_n: int = 15


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "demo_mode": settings.demo_mode,
        "ssl_verify": settings.ssl_verify,
        "ssl_mode": _SSL_MODE,
    }


@app.post("/api/scan")
def scan(body: ScanIn | None = None):
    body = body or ScanIn()
    try:
        return run_scan(
            min_amount_yi=body.min_amount_yi,
            min_pct=body.min_pct,
            max_pct=body.max_pct,
            session=body.session,
            top_n=body.top_n,
            board_top_n=body.board_top_n,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/watchlist")
def get_watchlist(
    with_quotes: bool = Query(default=False),
    refresh_returns: bool = Query(default=False),
):
    """默认快速返回库内自选；行情/收益刷新按需开启。"""
    items = list_watchlist()
    quotes: dict[str, dict] = {}
    if with_quotes and items:
        try:
            quotes = {q["code"]: q for q in watchlist_quotes([i["code"] for i in items])}
        except Exception:
            quotes = {}

    out = []
    for it in items:
        q = quotes.get(str(it["code"]).zfill(6), {}) or quotes.get(it["code"], {})
        try:
            out.append(enrich_watch_item(it, q, force_refresh=refresh_returns))
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
    return {"items": out, "stats": watchlist_stats()}


@app.get("/api/watchlist/stats")
def get_watchlist_stats():
    return watchlist_stats()


@app.get("/api/watchlist/history")
def get_watchlist_history(limit: int = Query(default=100, le=500)):
    tracks = list_watch_tracks(active_only=False, limit=limit)
    rows = []
    for tr in tracks:
        rets = refresh_track_returns(tr, persist=True)
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
        spot = mkt.get_spot_df()
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

    row = add_watch(
        code,
        name,
        body.source,
        body.note,
        entry_price=entry_price,
        entry_pct=entry_pct,
        entry_score=body.entry_score,
    )
    return row


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
