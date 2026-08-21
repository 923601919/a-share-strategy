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


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )


def list_watchlist() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT code, name, source, note, created_at FROM watchlist ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_watch(
    code: str,
    name: str,
    source: str = "manual",
    note: str = "",
) -> dict[str, Any]:
    code = code.strip().upper()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO watchlist(code, name, source, note, created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                source=excluded.source,
                note=excluded.note
            """,
            (code, name, source, note, now),
        )
    return {"code": code, "name": name, "source": source, "note": note, "created_at": now}


def remove_watch(code: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE code=?", (code.strip().upper(),))
        return cur.rowcount > 0


def save_scan_snapshot(payload_json: str) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scan_snapshots(created_at, payload) VALUES(?,?)",
            (now, payload_json),
        )
