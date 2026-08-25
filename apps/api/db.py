import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from config import settings


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_watchlist(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
    for col, typ in [
        ("entry_price", "REAL"),
        ("entry_pct", "REAL"),
        ("entry_score", "REAL"),
        ("track_id", "INTEGER"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE watchlist ADD COLUMN {col} {typ}")


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                entry_price REAL,
                entry_pct REAL,
                entry_score REAL,
                track_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS scan_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watch_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                note TEXT DEFAULT '',
                entry_price REAL,
                entry_pct REAL,
                entry_score REAL,
                created_at TEXT NOT NULL,
                removed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS watch_track_returns (
                track_id INTEGER NOT NULL,
                day_offset INTEGER NOT NULL,
                trade_date TEXT NOT NULL,
                close_price REAL,
                return_pct REAL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (track_id, day_offset),
                FOREIGN KEY (track_id) REFERENCES watch_tracks(id)
            );

            CREATE TABLE IF NOT EXISTS daily_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        _migrate_watchlist(conn)


def list_watchlist() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT code, name, source, note, created_at,
                   entry_price, entry_pct, entry_score, track_id
            FROM watchlist ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def create_watch_track(
    *,
    code: str,
    name: str,
    source: str,
    note: str,
    entry_price: float | None,
    entry_pct: float | None,
    entry_score: float | None,
    created_at: str | None = None,
) -> int:
    now = created_at or datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO watch_tracks(code, name, source, note, entry_price, entry_pct, entry_score, created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (code, name, source, note, entry_price, entry_pct, entry_score, now),
        )
        return int(cur.lastrowid)


def add_watch(
    code: str,
    name: str,
    source: str = "manual",
    note: str = "",
    *,
    entry_price: float | None = None,
    entry_pct: float | None = None,
    entry_score: float | None = None,
) -> dict[str, Any]:
    code = code.strip().zfill(6)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    track_id = create_watch_track(
        code=code,
        name=name,
        source=source,
        note=note,
        entry_price=entry_price,
        entry_pct=entry_pct,
        entry_score=entry_score,
        created_at=now,
    )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO watchlist(code, name, source, note, created_at, entry_price, entry_pct, entry_score, track_id)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                source=excluded.source,
                note=excluded.note,
                created_at=excluded.created_at,
                entry_price=excluded.entry_price,
                entry_pct=excluded.entry_pct,
                entry_score=excluded.entry_score,
                track_id=excluded.track_id
            """,
            (code, name, source, note, now, entry_price, entry_pct, entry_score, track_id),
        )
    return {
        "code": code,
        "name": name,
        "source": source,
        "note": note,
        "created_at": now,
        "entry_price": entry_price,
        "entry_pct": entry_pct,
        "entry_score": entry_score,
        "track_id": track_id,
    }


def remove_watch(code: str) -> bool:
    code = code.strip().zfill(6)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT track_id FROM watchlist WHERE code=?", (code,)).fetchone()
        if row and row["track_id"]:
            conn.execute(
                "UPDATE watch_tracks SET removed_at=? WHERE id=?",
                (now, row["track_id"]),
            )
        cur = conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
        return cur.rowcount > 0


def list_watch_tracks(*, active_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    sql = """
        SELECT id, code, name, source, note, entry_price, entry_pct, entry_score, created_at, removed_at
        FROM watch_tracks
    """
    if active_only:
        sql += " WHERE removed_at IS NULL"
    sql += " ORDER BY created_at DESC LIMIT ?"
    with get_db() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def upsert_track_returns(track_id: int, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO watch_track_returns(track_id, day_offset, trade_date, close_price, return_pct, recorded_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(track_id, day_offset) DO UPDATE SET
                    trade_date=excluded.trade_date,
                    close_price=excluded.close_price,
                    return_pct=excluded.return_pct,
                    recorded_at=excluded.recorded_at
                """,
                (
                    track_id,
                    int(r["day_offset"]),
                    str(r["trade_date"]),
                    float(r.get("close_price") or 0),
                    float(r.get("return_pct") or 0),
                    now,
                ),
            )


def get_track_returns(track_id: int) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT day_offset, trade_date, close_price, return_pct, recorded_at
            FROM watch_track_returns WHERE track_id=? ORDER BY day_offset
            """,
            (track_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_scan_snapshot(payload_json: str) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scan_snapshots(created_at, payload) VALUES(?,?)",
            (now, payload_json),
        )


def latest_scan_snapshot() -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, created_at, payload FROM scan_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def save_daily_review(trade_date: str, payload_json: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO daily_reviews(trade_date, created_at, payload)
            VALUES(?,?,?)
            ON CONFLICT(trade_date) DO UPDATE SET
                created_at=excluded.created_at,
                payload=excluded.payload
            """,
            (trade_date, now, payload_json),
        )
        row = conn.execute(
            "SELECT id, trade_date, created_at FROM daily_reviews WHERE trade_date=?",
            (trade_date,),
        ).fetchone()
    return dict(row) if row else {"trade_date": trade_date, "created_at": now}


def get_daily_review(trade_date: str | None = None) -> dict[str, Any] | None:
    with get_db() as conn:
        if trade_date:
            row = conn.execute(
                "SELECT id, trade_date, created_at, payload FROM daily_reviews WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, trade_date, created_at, payload FROM daily_reviews ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
    return dict(row) if row else None


def list_daily_reviews(limit: int = 30) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, trade_date, created_at
            FROM daily_reviews ORDER BY trade_date DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
