from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import settings
from db import add_watch, init_db, list_watchlist, remove_watch
from ssl_fix import apply_ssl_fix

# 必须在首次请求东财前生效
_SSL_MODE = apply_ssl_fix(insecure=not settings.ssl_verify)

from services.scan import run_scan, watchlist_quotes

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


class ScanIn(BaseModel):
    min_amount_yi: float | None = Field(default=None, description="成交额下限（亿）")
    min_pct: float | None = None
    max_pct: float | None = Field(default=None, description="涨幅上限%，超过不考虑")
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
def get_watchlist(with_quotes: bool = Query(default=True)):
    items = list_watchlist()
    if with_quotes and items:
        quotes = {q["code"]: q for q in watchlist_quotes([i["code"] for i in items])}
        for it in items:
            q = quotes.get(it["code"], {})
            it["quote"] = q
    return {"items": items}


@app.post("/api/watchlist")
def post_watchlist(body: WatchIn):
    if not body.code.strip():
        raise HTTPException(400, "code required")
    row = add_watch(body.code, body.name or body.code, body.source, body.note)
    return row


@app.delete("/api/watchlist/{code}")
def delete_watchlist(code: str):
    ok = remove_watch(code)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}
