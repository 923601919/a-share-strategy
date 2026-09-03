import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from config import settings
from user_ctx import require_user_id


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                used_by INTEGER,
                used_at TEXT,
                note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                entry_price REAL,
                entry_pct REAL,
                entry_score REAL,
                track_id INTEGER,
                user_id INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, code)
            );

            CREATE TABLE IF NOT EXISTS scan_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS scan_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                universe_policy TEXT NOT NULL,
                candidates INTEGER,
                scored INTEGER,
                fenshi_ok INTEGER,
                proxy_count INTEGER,
                timed_out INTEGER,
                total_ms REAL,
                market_env_level TEXT,
                market_pct REAL,
                spot_source TEXT,
                strategy_version TEXT,
                top_avg_day_position REAL,
                user_id INTEGER NOT NULL DEFAULT 1
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
                removed_at TEXT,
                day_position REAL,
                vwap_deviation REAL,
                user_id INTEGER NOT NULL DEFAULT 1
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
                trade_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                user_id INTEGER NOT NULL DEFAULT 1,
                UNIQUE (user_id, trade_date)
            );

            CREATE TABLE IF NOT EXISTS sim_account (
                user_id INTEGER PRIMARY KEY,
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
                note TEXT DEFAULT '',
                user_id INTEGER NOT NULL DEFAULT 1
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
                user_id INTEGER NOT NULL DEFAULT 1,
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
                meta TEXT DEFAULT '',
                user_id INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS trade_calendar (
                year INTEGER PRIMARY KEY,
                dates TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _migrate_watchlist(conn)
        _migrate_watch_tracks(conn)
        _migrate_sim_positions(conn)
        _migrate_multi_user(conn)


def _migrate_watch_tracks(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(watch_tracks)").fetchall()}
    for col, typ in [
        ("exit_price", "REAL"),
        ("exit_return_pct", "REAL"),
        ("completion_reason", "TEXT"),
        ("completion_snapshot", "TEXT"),
        ("day_position", "REAL"),
        ("vwap_deviation", "REAL"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE watch_tracks ADD COLUMN {col} {typ}")


def _migrate_sim_positions(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sim_positions)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE sim_positions ADD COLUMN source TEXT DEFAULT 'manual'")


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_multi_user(conn: sqlite3.Connection) -> None:
    """为旧库补 user_id，并重建主键/唯一约束。"""
    # users / invites 可能由旧库缺失
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            disabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            created_by INTEGER,
            created_at TEXT NOT NULL,
            used_by INTEGER,
            used_at TEXT,
            note TEXT DEFAULT ''
        );
        """
    )

    def _add_user_id(table: str) -> None:
        cols = _table_cols(conn, table)
        if "user_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")

    for t in (
        "watchlist",
        "watch_tracks",
        "scan_snapshots",
        "daily_reviews",
        "sim_positions",
        "sim_orders",
        "sim_trades",
    ):
        try:
            _add_user_id(t)
        except sqlite3.Error:
            pass

    # watchlist: 旧主键仅 code → (user_id, code)
    wl_cols = _table_cols(conn, "watchlist")
    pk = conn.execute("PRAGMA table_info(watchlist)").fetchall()
    pk_names = [r[1] for r in pk if r[5]]  # pk ordinal > 0
    if pk_names == ["code"] or (len(pk_names) == 1 and pk_names[0] == "code"):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist_new (
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                entry_price REAL,
                entry_pct REAL,
                entry_score REAL,
                track_id INTEGER,
                user_id INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, code)
            );
            INSERT OR IGNORE INTO watchlist_new(
                code, name, source, note, created_at, entry_price, entry_pct, entry_score, track_id, user_id
            )
            SELECT code, name, source, note, created_at, entry_price, entry_pct, entry_score, track_id,
                   COALESCE(user_id, 1)
            FROM watchlist;
            DROP TABLE watchlist;
            ALTER TABLE watchlist_new RENAME TO watchlist;
            """
        )

    # daily_reviews: UNIQUE(trade_date) → UNIQUE(user_id, trade_date)
    dr_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_reviews'"
    ).fetchone()
    if dr_sql and dr_sql[0] and "UNIQUE (user_id, trade_date)" not in dr_sql[0].replace("\n", " "):
        if "trade_date TEXT NOT NULL UNIQUE" in (dr_sql[0] or "") or (
            "user_id" in _table_cols(conn, "daily_reviews")
            and "UNIQUE (user_id, trade_date)" not in (dr_sql[0] or "")
        ):
            # 仅当仍是旧 UNIQUE(trade_date) 时重建
            if "UNIQUE" in (dr_sql[0] or "") and "user_id, trade_date" not in (dr_sql[0] or ""):
                conn.executescript(
                    """
                    CREATE TABLE daily_reviews_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        user_id INTEGER NOT NULL DEFAULT 1,
                        UNIQUE (user_id, trade_date)
                    );
                    INSERT OR IGNORE INTO daily_reviews_new(id, trade_date, created_at, payload, user_id)
                    SELECT id, trade_date, created_at, payload, COALESCE(user_id, 1) FROM daily_reviews;
                    DROP TABLE daily_reviews;
                    ALTER TABLE daily_reviews_new RENAME TO daily_reviews;
                    """
                )

    # sim_account: id=1 → user_id PK
    sa_cols = _table_cols(conn, "sim_account") if _table_exists(conn, "sim_account") else set()
    if sa_cols and "user_id" not in sa_cols and "id" in sa_cols:
        conn.executescript(
            """
            CREATE TABLE sim_account_new (
                user_id INTEGER PRIMARY KEY,
                cash REAL NOT NULL,
                initial_capital REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO sim_account_new(user_id, cash, initial_capital, updated_at)
            SELECT id, cash, initial_capital, updated_at FROM sim_account;
            DROP TABLE sim_account;
            ALTER TABLE sim_account_new RENAME TO sim_account;
            """
        )
    elif not _table_exists(conn, "sim_account"):
        conn.execute(
            """
            CREATE TABLE sim_account (
                user_id INTEGER PRIMARY KEY,
                cash REAL NOT NULL,
                initial_capital REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def get_trade_calendar(year: int) -> list[str] | None:
    """读取缓存的全年交易日（YYYY-MM-DD 列表）。无缓存返回 None。"""
    with get_db() as conn:
        if not _table_exists(conn, "trade_calendar"):
            return None
        row = conn.execute("SELECT dates FROM trade_calendar WHERE year=?", (year,)).fetchone()
    if not row:
        return None
    try:
        return [str(d) for d in json.loads(row[0])]
    except Exception:
        return None


def save_trade_calendar(year: int, dates: list[str]) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trade_calendar(year, dates, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(year) DO UPDATE SET dates=excluded.dates, updated_at=excluded.updated_at",
            (year, json.dumps(sorted(dates)), now),
        )


def _ensure_sim_account(conn: sqlite3.Connection, user_id: int | None = None) -> None:
    uid = int(user_id if user_id is not None else require_user_id())
    row = conn.execute("SELECT user_id FROM sim_account WHERE user_id=?", (uid,)).fetchone()
    if row:
        return
    now = datetime.now(timezone.utc).astimezone().isoformat()
    capital = float(settings.sim_initial_capital)
    conn.execute(
        "INSERT INTO sim_account(user_id, cash, initial_capital, updated_at) VALUES(?,?,?,?)",
        (uid, capital, capital, now),
    )


# ---------- 用户 / 邀请 ----------


def count_users() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    return int(row["c"] if row else 0)


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, disabled, created_at FROM users WHERE id=?",
            (int(user_id),),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, disabled, created_at FROM users WHERE username=?",
            (username.strip(),),
        ).fetchone()
    return dict(row) if row else None


def create_user(*, username: str, password_hash: str, role: str = "user") -> dict[str, Any]:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO users(username, password_hash, role, disabled, created_at)
            VALUES(?,?,?,0,?)
            """,
            (username.strip(), password_hash, role, now),
        )
        uid = int(cur.lastrowid)
        _ensure_sim_account(conn, uid)
        row = conn.execute(
            "SELECT id, username, password_hash, role, disabled, created_at FROM users WHERE id=?",
            (uid,),
        ).fetchone()
    return dict(row)


def ensure_local_user() -> dict[str, Any]:
    """鉴权关闭时使用的本地默认用户（id 尽量为 1，承接旧数据）。"""
    existing = get_user_by_username("__local__")
    if existing:
        return existing
    by_id = get_user_by_id(1)
    if by_id:
        return by_id
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        # 若库空，显式插入 id=1
        conn.execute(
            """
            INSERT INTO users(id, username, password_hash, role, disabled, created_at)
            VALUES(1, '__local__', '', 'admin', 0, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (now,),
        )
        row = conn.execute(
            "SELECT id, username, password_hash, role, disabled, created_at FROM users WHERE id=1"
        ).fetchone()
        if not row:
            cur = conn.execute(
                """
                INSERT INTO users(username, password_hash, role, disabled, created_at)
                VALUES('__local__', '', 'admin', 0, ?)
                """,
                (now,),
            )
            uid = int(cur.lastrowid)
            row = conn.execute(
                "SELECT id, username, password_hash, role, disabled, created_at FROM users WHERE id=?",
                (uid,),
            ).fetchone()
        _ensure_sim_account(conn, int(row["id"]))
    return dict(row)


def create_invite(*, code: str, created_by: int, note: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO invite_codes(code, created_by, created_at, note)
            VALUES(?,?,?,?)
            """,
            (code, int(created_by), now, note or ""),
        )
        row = conn.execute(
            "SELECT code, created_by, created_at, used_by, used_at, note FROM invite_codes WHERE code=?",
            (code,),
        ).fetchone()
    return dict(row)


def get_invite(code: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT code, created_by, created_at, used_by, used_at, note FROM invite_codes WHERE code=?",
            (code.strip().upper(),),
        ).fetchone()
    return dict(row) if row else None


def verify_invite_code(code: str) -> dict[str, Any] | None:
    row = get_invite(code)
    if not row or row.get("used_by"):
        return None
    return row


def consume_invite(code: str, *, user_id: int) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE invite_codes SET used_by=?, used_at=?
            WHERE code=? AND used_by IS NULL
            """,
            (int(user_id), now, code.strip().upper()),
        )
        if cur.rowcount < 1:
            raise ValueError("invite already used or missing")


def list_invites(limit: int = 50) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT code, created_by, created_at, used_by, used_at, note
            FROM invite_codes ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_watchlist() -> list[dict[str, Any]]:
    uid = require_user_id()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT code, name, source, note, created_at,
                   entry_price, entry_pct, entry_score, track_id
            FROM watchlist WHERE user_id=? ORDER BY created_at DESC
            """,
            (uid,),
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
    day_position: float | None = None,
    vwap_deviation: float | None = None,
) -> int:
    uid = require_user_id()
    now = created_at or datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO watch_tracks(
                code, name, source, note, entry_price, entry_pct, entry_score,
                created_at, day_position, vwap_deviation, user_id
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (code, name, source, note, entry_price, entry_pct, entry_score, now, day_position, vwap_deviation, uid),
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
    day_position: float | None = None,
    vwap_deviation: float | None = None,
) -> dict[str, Any]:
    uid = require_user_id()
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
        day_position=day_position,
        vwap_deviation=vwap_deviation,
    )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO watchlist(
                code, name, source, note, created_at, entry_price, entry_pct, entry_score, track_id, user_id
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id, code) DO UPDATE SET
                name=excluded.name,
                source=excluded.source,
                note=excluded.note,
                created_at=excluded.created_at,
                entry_price=excluded.entry_price,
                entry_pct=excluded.entry_pct,
                entry_score=excluded.entry_score,
                track_id=excluded.track_id
            """,
            (code, name, source, note, now, entry_price, entry_pct, entry_score, track_id, uid),
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
        "day_position": day_position,
        "vwap_deviation": vwap_deviation,
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
    uid = require_user_id()
    code = code.strip().zfill(6)
    with get_db() as conn:
        row = conn.execute(
            "SELECT track_id FROM watchlist WHERE user_id=? AND code=?",
            (uid, code),
        ).fetchone()
        if row and row["track_id"]:
            complete_watch_track(
                int(row["track_id"]),
                reason=reason,
                exit_price=exit_price,
                exit_return_pct=exit_return_pct,
                snapshot=snapshot,
            )
        cur = conn.execute("DELETE FROM watchlist WHERE user_id=? AND code=?", (uid, code))
        return cur.rowcount > 0


def list_watch_tracks(*, active_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    uid = require_user_id()
    sql = """
        SELECT id, code, name, source, note, entry_price, entry_pct, entry_score,
               created_at, removed_at, exit_price, exit_return_pct,
               completion_reason, completion_snapshot, day_position, vwap_deviation
        FROM watch_tracks
        WHERE user_id=?
    """
    if active_only:
        sql += " AND removed_at IS NULL"
    sql += " ORDER BY created_at DESC LIMIT ?"
    with get_db() as conn:
        rows = conn.execute(sql, (uid, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        snap = d.get("completion_snapshot")
        if isinstance(snap, str) and snap:
            try:
                d["completion_snapshot"] = json.loads(snap)
            except Exception:
                pass
        out.append(d)
    return out


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


def get_track_returns_map(track_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """批量读取收益，避免履历页 N+1 开连接。"""
    if not track_ids:
        return {}
    uniq = sorted({int(i) for i in track_ids if i})
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in uniq}
    placeholders = ",".join("?" for _ in uniq)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT track_id, day_offset, trade_date, close_price, return_pct, recorded_at
            FROM watch_track_returns
            WHERE track_id IN ({placeholders})
            ORDER BY track_id, day_offset
            """,
            uniq,
        ).fetchall()
    for r in rows:
        tid = int(r["track_id"])
        out.setdefault(tid, []).append(
            {
                "day_offset": r["day_offset"],
                "trade_date": r["trade_date"],
                "close_price": r["close_price"],
                "return_pct": r["return_pct"],
                "recorded_at": r["recorded_at"],
            }
        )
    return out


def list_archived_watch_tracks(*, limit: int = 200) -> list[dict[str, Any]]:
    """仅已移除/归档的跟踪记录（履历页）。"""
    uid = require_user_id()
    sql = """
        SELECT id, code, name, source, note, entry_price, entry_pct, entry_score,
               created_at, removed_at, exit_price, exit_return_pct,
               completion_reason, completion_snapshot
        FROM watch_tracks
        WHERE user_id=? AND removed_at IS NOT NULL
        ORDER BY removed_at DESC, id DESC
        LIMIT ?
    """
    with get_db() as conn:
        rows = conn.execute(sql, (uid, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        snap = d.get("completion_snapshot")
        if isinstance(snap, str) and snap:
            try:
                d["completion_snapshot"] = json.loads(snap)
            except Exception:
                pass
        out.append(d)
    return out


def save_scan_snapshot(payload_json: str) -> None:
    uid = require_user_id()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    keep = max(5, int(settings.scan_snapshot_keep))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scan_snapshots(created_at, payload, user_id) VALUES(?,?,?)",
            (now, payload_json, uid),
        )
        conn.execute(
            """
            DELETE FROM scan_snapshots WHERE user_id=? AND id NOT IN (
                SELECT id FROM scan_snapshots WHERE user_id=? ORDER BY id DESC LIMIT ?
            )
            """,
            (uid, uid, keep),
        )


def latest_scan_snapshot() -> dict[str, Any] | None:
    uid = require_user_id()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, created_at, payload FROM scan_snapshots
            WHERE user_id=? ORDER BY id DESC LIMIT 1
            """,
            (uid,),
        ).fetchone()
    return dict(row) if row else None


def save_scan_quality(q: dict[str, Any]) -> None:
    """落一条扫描质量摘要（结构化、可长期积累，不受快照裁剪影响）。"""
    uid = require_user_id()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scan_quality(
                created_at, mode, universe_policy, candidates, scored, fenshi_ok,
                proxy_count, timed_out, total_ms, market_env_level, market_pct,
                spot_source, strategy_version, top_avg_day_position, user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now,
                q.get("mode") or "",
                q.get("universe_policy") or "",
                q.get("candidates"),
                q.get("scored"),
                q.get("fenshi_ok"),
                q.get("proxy_count"),
                q.get("timed_out"),
                q.get("total_ms"),
                q.get("market_env_level"),
                q.get("market_pct"),
                q.get("spot_source"),
                q.get("strategy_version"),
                q.get("top_avg_day_position"),
                uid,
            ),
        )


def list_scan_quality(limit: int = 200, *, mode: str | None = None) -> list[dict[str, Any]]:
    uid = require_user_id()
    sql = "SELECT * FROM scan_quality WHERE user_id=?"
    args: list[Any] = [uid]
    if mode:
        sql += " AND mode=?"
        args.append(mode)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def save_daily_review(trade_date: str, payload_json: str) -> dict[str, Any]:
    uid = require_user_id()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO daily_reviews(trade_date, created_at, payload, user_id)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id, trade_date) DO UPDATE SET
                created_at=excluded.created_at,
                payload=excluded.payload
            """,
            (trade_date, now, payload_json, uid),
        )
        row = conn.execute(
            "SELECT id, trade_date, created_at FROM daily_reviews WHERE user_id=? AND trade_date=?",
            (uid, trade_date),
        ).fetchone()
    return dict(row) if row else {"trade_date": trade_date, "created_at": now}


def get_daily_review(trade_date: str | None = None) -> dict[str, Any] | None:
    uid = require_user_id()
    with get_db() as conn:
        if trade_date:
            row = conn.execute(
                """
                SELECT id, trade_date, created_at, payload FROM daily_reviews
                WHERE user_id=? AND trade_date=?
                """,
                (uid, trade_date),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, trade_date, created_at, payload FROM daily_reviews
                WHERE user_id=? ORDER BY trade_date DESC LIMIT 1
                """,
                (uid,),
            ).fetchone()
    return dict(row) if row else None


def list_daily_reviews(limit: int = 30) -> list[dict[str, Any]]:
    uid = require_user_id()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, trade_date, created_at
            FROM daily_reviews WHERE user_id=? ORDER BY trade_date DESC LIMIT ?
            """,
            (uid, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- 模拟盘 ----------


def get_sim_account() -> dict[str, Any]:
    uid = require_user_id()
    with get_db() as conn:
        _ensure_sim_account(conn, uid)
        row = conn.execute(
            "SELECT user_id AS id, cash, initial_capital, updated_at FROM sim_account WHERE user_id=?",
            (uid,),
        ).fetchone()
    return dict(row)


def set_sim_cash(cash: float) -> dict[str, Any]:
    uid = require_user_id()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        _ensure_sim_account(conn, uid)
        conn.execute(
            "UPDATE sim_account SET cash=?, updated_at=? WHERE user_id=?",
            (float(cash), now, uid),
        )
        row = conn.execute(
            "SELECT user_id AS id, cash, initial_capital, updated_at FROM sim_account WHERE user_id=?",
            (uid,),
        ).fetchone()
    return dict(row)


def reset_sim_account(initial: float | None = None) -> dict[str, Any]:
    uid = require_user_id()
    capital = float(initial if initial is not None else settings.sim_initial_capital)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        conn.execute("DELETE FROM sim_orders WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM sim_trades WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM sim_positions WHERE user_id=?", (uid,))
        conn.execute(
            """
            INSERT INTO sim_account(user_id, cash, initial_capital, updated_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                cash=excluded.cash,
                initial_capital=excluded.initial_capital,
                updated_at=excluded.updated_at
            """,
            (uid, capital, capital, now),
        )
        row = conn.execute(
            "SELECT user_id AS id, cash, initial_capital, updated_at FROM sim_account WHERE user_id=?",
            (uid,),
        ).fetchone()
    return dict(row)


def list_sim_positions(*, status: str = "open") -> list[dict[str, Any]]:
    uid = require_user_id()
    with get_db() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM sim_positions WHERE user_id=? ORDER BY opened_at DESC",
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sim_positions WHERE user_id=? AND status=? ORDER BY opened_at DESC",
                (uid, status),
            ).fetchall()
    return [dict(r) for r in rows]


def get_sim_position(position_id: int) -> dict[str, Any] | None:
    uid = require_user_id()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sim_positions WHERE id=? AND user_id=?",
            (position_id, uid),
        ).fetchone()
    return dict(row) if row else None


def get_open_sim_position_by_code(code: str) -> dict[str, Any] | None:
    uid = require_user_id()
    code = code.strip().zfill(6)
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM sim_positions
            WHERE user_id=? AND code=? AND status='open'
            ORDER BY id DESC LIMIT 1
            """,
            (uid, code),
        ).fetchone()
    return dict(row) if row else None


def get_watch_source(code: str) -> str | None:
    uid = require_user_id()
    code = code.strip().zfill(6)
    with get_db() as conn:
        row = conn.execute(
            "SELECT source FROM watchlist WHERE user_id=? AND code=?",
            (uid, code),
        ).fetchone()
    return str(row["source"]) if row else None


def insert_sim_position(payload: dict[str, Any]) -> dict[str, Any]:
    uid = require_user_id()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sim_positions(
                code, name, shares, cost_price, opened_at, status,
                take_profit_pct, stop_loss_pct, take_profit_price, stop_loss_price,
                entry_score, note, source, user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                uid,
            ),
        )
        pid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM sim_positions WHERE id=?", (pid,)).fetchone()
    return dict(row)


def update_sim_position(position_id: int, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return get_sim_position(position_id)
    allowed = {
        "code",
        "name",
        "shares",
        "cost_price",
        "opened_at",
        "closed_at",
        "status",
        "take_profit_pct",
        "stop_loss_pct",
        "take_profit_price",
        "stop_loss_price",
        "entry_score",
        "note",
        "source",
    }
    cols = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"invalid sim_positions column: {k}")
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(position_id)
    with get_db() as conn:
        conn.execute(f"UPDATE sim_positions SET {', '.join(cols)} WHERE id=?", vals)
        row = conn.execute("SELECT * FROM sim_positions WHERE id=?", (position_id,)).fetchone()
    return dict(row) if row else None


def open_sim_position_tx(
    *,
    position: dict[str, Any],
    trade: dict[str, Any],
    cash_after: float,
    orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """开仓：持仓 + 扣款 + 成交 + 条件单，单事务。"""
    uid = require_user_id()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        _ensure_sim_account(conn, uid)
        cur = conn.execute(
            """
            INSERT INTO sim_positions(
                code, name, shares, cost_price, opened_at, status,
                take_profit_pct, stop_loss_pct, take_profit_price, stop_loss_price,
                entry_score, note, source, user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                position["code"],
                position["name"],
                int(position["shares"]),
                float(position["cost_price"]),
                position["opened_at"],
                position.get("status") or "open",
                position.get("take_profit_pct"),
                position.get("stop_loss_pct"),
                position.get("take_profit_price"),
                position.get("stop_loss_price"),
                position.get("entry_score"),
                position.get("note") or "",
                position.get("source") or "manual",
                uid,
            ),
        )
        pid = int(cur.lastrowid)
        conn.execute(
            "UPDATE sim_account SET cash=?, updated_at=? WHERE user_id=?",
            (float(cash_after), now, uid),
        )
        tcur = conn.execute(
            """
            INSERT INTO sim_trades(
                code, name, side, shares, price, amount, fee, pnl, pnl_pct,
                reason, position_id, order_id, traded_at, meta, user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade["code"],
                trade["name"],
                trade["side"],
                int(trade["shares"]),
                float(trade["price"]),
                float(trade["amount"]),
                float(trade.get("fee") or 0),
                trade.get("pnl"),
                trade.get("pnl_pct"),
                trade.get("reason") or "",
                pid,
                trade.get("order_id"),
                trade.get("traded_at") or now,
                trade.get("meta") or "",
                uid,
            ),
        )
        tid = int(tcur.lastrowid)
        order_rows: list[dict[str, Any]] = []
        for o in orders or []:
            ocur = conn.execute(
                """
                INSERT INTO sim_orders(
                    position_id, code, name, side, order_type, trigger_price, trigger_pct,
                    status, created_at, updated_at, reason, user_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pid,
                    o["code"],
                    o["name"],
                    o["side"],
                    o["order_type"],
                    float(o["trigger_price"]),
                    o.get("trigger_pct"),
                    o.get("status") or "active",
                    now,
                    now,
                    o.get("reason") or "",
                    uid,
                ),
            )
            orow = conn.execute("SELECT * FROM sim_orders WHERE id=?", (int(ocur.lastrowid),)).fetchone()
            order_rows.append(dict(orow))
        pos = dict(conn.execute("SELECT * FROM sim_positions WHERE id=?", (pid,)).fetchone())
        trade_row = dict(conn.execute("SELECT * FROM sim_trades WHERE id=?", (tid,)).fetchone())
        acct = dict(
            conn.execute(
                "SELECT user_id AS id, cash, initial_capital, updated_at FROM sim_account WHERE user_id=?",
                (uid,),
            ).fetchone()
        )
    return {"position": pos, "trade": trade_row, "orders": order_rows, "account": acct}


def sell_sim_position_tx(
    *,
    position_id: int,
    trade: dict[str, Any],
    cash_after: float,
    order_id: int | None = None,
    fill_price: float | None = None,
) -> dict[str, Any]:
    """平仓：回款 + 关仓 + 取消条件单 + 成交，单事务。"""
    uid = require_user_id()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        owned = conn.execute(
            "SELECT id FROM sim_positions WHERE id=? AND user_id=?",
            (position_id, uid),
        ).fetchone()
        if not owned:
            raise ValueError("position not found")
        _ensure_sim_account(conn, uid)
        conn.execute(
            "UPDATE sim_account SET cash=?, updated_at=? WHERE user_id=?",
            (float(cash_after), now, uid),
        )
        conn.execute(
            "UPDATE sim_positions SET status=?, closed_at=? WHERE id=? AND user_id=?",
            ("closed", now, position_id, uid),
        )
        conn.execute(
            """
            UPDATE sim_orders SET status='cancelled', updated_at=?
            WHERE position_id=? AND user_id=? AND status='active'
            """,
            (now, position_id, uid),
        )
        if order_id is not None and fill_price is not None:
            conn.execute(
                """
                UPDATE sim_orders SET status='filled', filled_price=?, filled_at=?, updated_at=?
                WHERE id=? AND user_id=?
                """,
                (float(fill_price), now, now, order_id, uid),
            )
        tcur = conn.execute(
            """
            INSERT INTO sim_trades(
                code, name, side, shares, price, amount, fee, pnl, pnl_pct,
                reason, position_id, order_id, traded_at, meta, user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade["code"],
                trade["name"],
                trade["side"],
                int(trade["shares"]),
                float(trade["price"]),
                float(trade["amount"]),
                float(trade.get("fee") or 0),
                trade.get("pnl"),
                trade.get("pnl_pct"),
                trade.get("reason") or "",
                position_id,
                order_id,
                trade.get("traded_at") or now,
                trade.get("meta") or "",
                uid,
            ),
        )
        tid = int(tcur.lastrowid)
        pos = dict(conn.execute("SELECT * FROM sim_positions WHERE id=?", (position_id,)).fetchone())
        trade_row = dict(conn.execute("SELECT * FROM sim_trades WHERE id=?", (tid,)).fetchone())
        acct = dict(
            conn.execute(
                "SELECT user_id AS id, cash, initial_capital, updated_at FROM sim_account WHERE user_id=?",
                (uid,),
            ).fetchone()
        )
    return {"position": pos, "trade": trade_row, "account": acct}


def insert_sim_order(payload: dict[str, Any]) -> dict[str, Any]:
    uid = require_user_id()
    now = datetime.now(timezone.utc).astimezone().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sim_orders(
                position_id, code, name, side, order_type, trigger_price, trigger_pct,
                status, created_at, updated_at, reason, user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
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
                uid,
            ),
        )
        oid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM sim_orders WHERE id=?", (oid,)).fetchone()
    return dict(row)


def list_sim_orders(*, status: str = "active", limit: int = 200) -> list[dict[str, Any]]:
    uid = require_user_id()
    with get_db() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM sim_orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (uid, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sim_orders WHERE user_id=? AND status=? ORDER BY id DESC LIMIT ?",
                (uid, status, limit),
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
    uid = require_user_id()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sim_trades(
                code, name, side, shares, price, amount, fee, pnl, pnl_pct,
                reason, position_id, order_id, traded_at, meta, user_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                uid,
            ),
        )
        tid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM sim_trades WHERE id=?", (tid,)).fetchone()
    return dict(row)


def list_sim_trades(limit: int = 200) -> list[dict[str, Any]]:
    uid = require_user_id()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sim_trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [dict(r) for r in rows]
