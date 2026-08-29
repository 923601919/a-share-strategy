import json
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

            CREATE TABLE IF NOT EXISTS sim_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL,
                initial_capital REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sim_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                shares INTEGER NOT NULL,
                cost_price REAL NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                take_profit_pct REAL,
                stop_loss_pct REAL,
                take_profit_price REAL,
                stop_loss_price REAL,
                entry_score REAL,
                note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS sim_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                trigger_price REAL NOT NULL,
                trigger_pct REAL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                filled_at TEXT,
                filled_price REAL,
                reason TEXT DEFAULT '',
                FOREIGN KEY (position_id) REFERENCES sim_positions(id)
            );

            CREATE TABLE IF NOT EXISTS sim_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                side TEXT NOT NULL,
                shares INTEGER NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                pnl REAL,
                pnl_pct REAL,
                reason TEXT DEFAULT '',
                position_id INTEGER,
                order_id INTEGER,
                traded_at TEXT NOT NULL,
                meta TEXT DEFAULT ''
            );
            """
        )
        _migrate_watchlist(conn)
        _migrate_watch_tracks(conn)
        _migrate_sim_positions(conn)
        _ensure_sim_account(conn)


def _migrate_watch_tracks(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(watch_tracks)").fetchall()}
    for col, typ in [
        ("exit_price", "REAL"),
        ("exit_return_pct", "REAL"),
        ("completion_reason", "TEXT"),
        ("completion_snapshot", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE watch_tracks ADD COLUMN {col} {typ}")


def _migrate_sim_positions(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sim_positions)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE sim_positions ADD COLUMN source TEXT DEFAULT 'manual'")


def _ensure_sim_account(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT id FROM sim_account WHERE id=1").fetchone()
    if row:
        return
    now = datetime.now(timezone.utc).astimezone().isoformat()
    capital = float(settings.sim_initial_capital)
    conn.execute(
        "INSERT INTO sim_account(id, cash, initial_capital, updated_at) VALUES(1,?,?,?)",
        (capital, capital, now),
    )


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


def complete_watch_track(
    track_id: int,
    *,
    reason: str,
    exit_price: float | None = None,
    exit_return_pct: float | None = None,
    snapshot: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    snap_json = json.dumps(snapshot, ensure_ascii=False) if snapshot else None
    with get_db() as conn:
        conn.execute(
            """
            UPDATE watch_tracks SET
                removed_at=?,
                exit_price=?,
                exit_return_pct=?,
                completion_reason=?,
                completion_snapshot=?
            WHERE id=?
            """,
            (now, exit_price, exit_return_pct, reason, snap_json, track_id),
        )


def remove_watch(
    code: str,
    *,
    reason: str = "manual",
    exit_price: float | None = None,
    exit_return_pct: float | None = None,
    snapshot: dict[str, Any] | None = None,
) -> bool:
    code = code.strip().zfill(6)
    with get_db() as conn:
        row = conn.execute("SELECT track_id FROM watchlist WHERE code=?", (code,)).fetchone()
        if row and row["track_id"]:
            complete_watch_track(
                int(row["track_id"]),
                reason=reason,
                exit_price=exit_price,
                exit_return_pct=exit_return_pct,
                snapshot=snapshot,
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


# ---------- 模拟盘 ----------


def get_sim_account() -> dict[str, Any]:
    with get_db() as conn:
        _ensure_sim_account(conn)
        row = conn.execute(
            "SELECT id, cash, initial_capital, updated_at FROM sim_account WHERE id=1"
        ).fetchone()
    return dict(row)


def set_sim_cash(cash: float) -> dict[str, Any]:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        _ensure_sim_account(conn)
        conn.execute(
            "UPDATE sim_account SET cash=?, updated_at=? WHERE id=1",
            (float(cash), now),
        )
        row = conn.execute(
            "SELECT id, cash, initial_capital, updated_at FROM sim_account WHERE id=1"
        ).fetchone()
    return dict(row)


def reset_sim_account(initial: float | None = None) -> dict[str, Any]:
    capital = float(initial if initial is not None else settings.sim_initial_capital)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute("DELETE FROM sim_orders")
        conn.execute("DELETE FROM sim_trades")
        conn.execute("DELETE FROM sim_positions")
        conn.execute(
            """
            INSERT INTO sim_account(id, cash, initial_capital, updated_at) VALUES(1,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                cash=excluded.cash,
                initial_capital=excluded.initial_capital,
                updated_at=excluded.updated_at
            """,
            (capital, capital, now),
        )
        row = conn.execute(
            "SELECT id, cash, initial_capital, updated_at FROM sim_account WHERE id=1"
        ).fetchone()
    return dict(row)


def list_sim_positions(*, status: str = "open") -> list[dict[str, Any]]:
    with get_db() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM sim_positions ORDER BY opened_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sim_positions WHERE status=? ORDER BY opened_at DESC",
                (status,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_sim_position(position_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sim_positions WHERE id=?", (position_id,)).fetchone()
    return dict(row) if row else None


def get_open_sim_position_by_code(code: str) -> dict[str, Any] | None:
    code = code.strip().zfill(6)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sim_positions WHERE code=? AND status='open' ORDER BY id DESC LIMIT 1",
            (code,),
        ).fetchone()
    return dict(row) if row else None


def get_watch_source(code: str) -> str | None:
    code = code.strip().zfill(6)
    with get_db() as conn:
        row = conn.execute("SELECT source FROM watchlist WHERE code=?", (code,)).fetchone()
    return str(row["source"]) if row else None


def insert_sim_position(payload: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sim_positions(
                code, name, shares, cost_price, opened_at, status,
                take_profit_pct, stop_loss_pct, take_profit_price, stop_loss_price,
                entry_score, note, source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload["code"],
                payload["name"],
                int(payload["shares"]),
                float(payload["cost_price"]),
                payload["opened_at"],
                payload.get("status") or "open",
                payload.get("take_profit_pct"),
                payload.get("stop_loss_pct"),
                payload.get("take_profit_price"),
                payload.get("stop_loss_price"),
                payload.get("entry_score"),
                payload.get("note") or "",
                payload.get("source") or "manual",
            ),
        )
        pid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM sim_positions WHERE id=?", (pid,)).fetchone()
    return dict(row)


def update_sim_position(position_id: int, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return get_sim_position(position_id)
    cols = []
    vals: list[Any] = []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(position_id)
    with get_db() as conn:
        conn.execute(f"UPDATE sim_positions SET {', '.join(cols)} WHERE id=?", vals)
        row = conn.execute("SELECT * FROM sim_positions WHERE id=?", (position_id,)).fetchone()
    return dict(row) if row else None


def insert_sim_order(payload: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sim_orders(
                position_id, code, name, side, order_type, trigger_price, trigger_pct,
                status, created_at, updated_at, reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.get("position_id"),
                payload["code"],
                payload["name"],
                payload["side"],
                payload["order_type"],
                float(payload["trigger_price"]),
                payload.get("trigger_pct"),
                payload.get("status") or "active",
                now,
                now,
                payload.get("reason") or "",
            ),
        )
        oid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM sim_orders WHERE id=?", (oid,)).fetchone()
    return dict(row)


def list_sim_orders(*, status: str = "active", limit: int = 200) -> list[dict[str, Any]]:
    with get_db() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM sim_orders ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sim_orders WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def cancel_sim_orders_for_position(position_id: int, *, keep_types: list[str] | None = None) -> int:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        if keep_types:
            placeholders = ",".join("?" * len(keep_types))
            cur = conn.execute(
                f"""
                UPDATE sim_orders SET status='cancelled', updated_at=?
                WHERE position_id=? AND status='active' AND order_type NOT IN ({placeholders})
                """,
                (now, position_id, *keep_types),
            )
        else:
            cur = conn.execute(
                """
                UPDATE sim_orders SET status='cancelled', updated_at=?
                WHERE position_id=? AND status='active'
                """,
                (now, position_id),
            )
        return int(cur.rowcount)


def cancel_sim_orders_by_types(position_id: int, order_types: list[str]) -> int:
    if not order_types:
        return 0
    now = datetime.now(timezone.utc).astimezone().isoformat()
    placeholders = ",".join("?" * len(order_types))
    with get_db() as conn:
        cur = conn.execute(
            f"""
            UPDATE sim_orders SET status='cancelled', updated_at=?
            WHERE position_id=? AND status='active' AND order_type IN ({placeholders})
            """,
            (now, position_id, *order_types),
        )
        return int(cur.rowcount)


def mark_sim_order_filled(order_id: int, filled_price: float) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sim_orders
            SET status='filled', filled_at=?, filled_price=?, updated_at=?
            WHERE id=?
            """,
            (now, float(filled_price), now, order_id),
        )
        row = conn.execute("SELECT * FROM sim_orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def insert_sim_trade(payload: dict[str, Any]) -> dict[str, Any]:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sim_trades(
                code, name, side, shares, price, amount, fee, pnl, pnl_pct,
                reason, position_id, order_id, traded_at, meta
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload["code"],
                payload["name"],
                payload["side"],
                int(payload["shares"]),
                float(payload["price"]),
                float(payload["amount"]),
                float(payload.get("fee") or 0),
                payload.get("pnl"),
                payload.get("pnl_pct"),
                payload.get("reason") or "",
                payload.get("position_id"),
                payload.get("order_id"),
                payload["traded_at"],
                payload.get("meta") or "",
            ),
        )
        tid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM sim_trades WHERE id=?", (tid,)).fetchone()
    return dict(row)


def list_sim_trades(limit: int = 200) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sim_trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
