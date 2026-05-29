"""SQLite layer — schema, upsert, queries."""

import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from config import DB_PATH, TABLE_PAGE_SIZE

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA cache_size=-65536")   # 64 MB page cache
        con.execute("PRAGMA mmap_size=536870912")  # 512 MB memory-mapped I/O
        con.execute("PRAGMA temp_store=MEMORY")
        _local.conn = con
    return _local.conn


# ── Schema ─────────────────────────────────────────────────────────────────────
_DDL_TABLES = """
CREATE TABLE IF NOT EXISTS log_files (
    path            TEXT PRIMARY KEY,
    server          TEXT NOT NULL,
    top_folder      TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL,
    folder          TEXT NOT NULL,
    sn              TEXT NOT NULL DEFAULT '',
    station         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'UNKNOWN',
    date            TEXT,
    ts              TEXT,
    modified        REAL NOT NULL,
    size            INTEGER NOT NULL DEFAULT 0,
    content_indexed INTEGER NOT NULL DEFAULT 0,
    content_cached  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS log_fts (
    path    TEXT NOT NULL,
    sn      TEXT,
    station TEXT,
    status  TEXT,
    body    TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS log_fts_idx USING fts5(
    path    UNINDEXED,
    sn,
    station,
    status,
    body,
    content='log_fts',
    tokenize='porter unicode61'
);
"""

_DDL_SCAN_LOG = """
CREATE TABLE IF NOT EXISTS scan_log (
    server          TEXT PRIMARY KEY,
    last_scanned_at REAL NOT NULL
);
"""

_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_server_top     ON log_files(server, top_folder);
CREATE INDEX IF NOT EXISTS idx_content_idx    ON log_files(content_indexed);
CREATE INDEX IF NOT EXISTS idx_content_cached ON log_files(content_cached);
CREATE INDEX IF NOT EXISTS idx_modified       ON log_files(modified DESC);
CREATE INDEX IF NOT EXISTS idx_server_top_mod ON log_files(server, top_folder, modified DESC);
CREATE INDEX IF NOT EXISTS idx_server_top_fld ON log_files(server, top_folder, folder);
"""


def _run_ddl(con: sqlite3.Connection, ddl: str) -> None:
    for stmt in ddl.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)


def _migrate(con: sqlite3.Connection) -> None:
    existing = {row[1] for row in con.execute("PRAGMA table_info(log_files)")}
    if "top_folder" not in existing:
        con.execute("ALTER TABLE log_files ADD COLUMN top_folder TEXT NOT NULL DEFAULT ''")
    if "content_cached" not in existing:
        con.execute("ALTER TABLE log_files ADD COLUMN content_cached INTEGER NOT NULL DEFAULT 0")
    con.commit()


def init_db() -> None:
    con = _conn()
    _run_ddl(con, _DDL_TABLES)
    _migrate(con)
    _run_ddl(con, _DDL_INDEXES)
    _run_ddl(con, _DDL_SCAN_LOG)
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ── Upsert ─────────────────────────────────────────────────────────────────────
def upsert_files(files: list[dict], server_name: str, top_folder: str) -> None:
    if not files:
        return
    con = _conn()
    rows = []
    for f in files:
        d = f.get("date")
        t = f.get("ts")
        rows.append((
            f["path_str"],
            server_name,
            top_folder,
            f["name"],
            f["folder"],
            f.get("sn", ""),
            f.get("station", ""),
            f.get("status", "UNKNOWN"),
            d.isoformat() if isinstance(d, date) else (str(d) if d else None),
            t.isoformat(sep=" ", timespec="seconds") if isinstance(t, datetime) else (str(t) if t else None),
            f["modified"].timestamp() if isinstance(f["modified"], datetime) else float(f["modified"]),
            f.get("size", 0),
        ))
    BATCH = 2000
    sql = """
        INSERT INTO log_files
            (path, server, top_folder, name, folder, sn, station, status, date, ts, modified, size,
             content_indexed, content_cached)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)
        ON CONFLICT(path) DO UPDATE SET
            server=excluded.server,
            top_folder=excluded.top_folder,
            name=excluded.name,
            folder=excluded.folder,
            sn=excluded.sn,
            station=excluded.station,
            status=excluded.status,
            date=excluded.date,
            ts=excluded.ts,
            modified=excluded.modified,
            size=excluded.size,
            content_indexed=CASE WHEN excluded.modified != log_files.modified THEN 0 ELSE log_files.content_indexed END,
            content_cached =CASE WHEN excluded.modified != log_files.modified THEN 0 ELSE log_files.content_cached  END
    """
    for i in range(0, len(rows), BATCH):
        con.executemany(sql, rows[i:i + BATCH])
        con.commit()


# ── Queries ────────────────────────────────────────────────────────────────────
def get_top_folders(server_name: str) -> list[str]:
    con = _conn()
    rows = con.execute(
        "SELECT DISTINCT top_folder FROM log_files WHERE server=? AND top_folder!='' ORDER BY top_folder",
        (server_name,),
    ).fetchall()
    return [r[0] for r in rows]


def get_subfolders(server_name: str, top_folder: str, current_folder: str) -> list[str]:
    """Return immediate child folder names under current_folder."""
    con = _conn()
    prefix = "" if current_folder == "." else current_folder + "\\"
    rows = con.execute(
        "SELECT DISTINCT folder FROM log_files WHERE server=? AND top_folder=? AND folder LIKE ? ESCAPE '\\'",
        (server_name, top_folder, _like_escape(prefix) + "%"),
    ).fetchall()
    seen: dict[str, bool] = {}
    children = []
    for (folder,) in rows:
        if folder == current_folder:
            continue
        rest = folder[len(prefix):]
        child = rest.split("\\")[0]
        if child and child not in seen:
            seen[child] = True
            children.append(child)
    return sorted(children)


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_files(
    server_name: str,
    top_folder: str,
    folder: str | None = None,
    status: str | None = None,
    sn: str | None = None,
    station: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort_col: str = "modified",
    sort_asc: bool = False,
    page: int = 1,
    page_size: int = TABLE_PAGE_SIZE,
) -> tuple[list[sqlite3.Row], int]:
    con = _conn()
    safe_cols = {"modified", "name", "sn", "status", "date", "ts", "size"}
    if sort_col not in safe_cols:
        sort_col = "modified"
    direction = "ASC" if sort_asc else "DESC"

    clauses = ["server=?", "top_folder=?"]
    params: list = [server_name, top_folder]

    if folder and folder != ".":
        clauses.append("(folder=? OR folder LIKE ? ESCAPE '\\')")
        params += [folder, _like_escape(folder) + "\\\\%"]
    if status:
        clauses.append("status=?")
        params.append(status)
    if sn:
        clauses.append("sn LIKE ? ESCAPE '\\'")
        params.append("%" + _like_escape(sn) + "%")
    if station:
        clauses.append("station=?")
        params.append(station)
    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date <= ?")
        params.append(date_to)

    where = " AND ".join(clauses)
    total = con.execute(f"SELECT COUNT(*) FROM log_files WHERE {where}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = con.execute(
        f"SELECT * FROM log_files WHERE {where} ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return rows, total


def get_file(path: str) -> sqlite3.Row | None:
    con = _conn()
    return con.execute("SELECT * FROM log_files WHERE path=?", (path,)).fetchone()


def search_fts_grouped(query: str, limit: int = 500) -> list[dict]:
    """FTS search results grouped by SN, sorted by match count descending."""
    rows = search_fts(query, limit)
    groups: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        sn = d.get("sn") or "(no SN)"
        if sn not in groups:
            groups[sn] = {"sn": sn, "count": 0, "files": []}
        groups[sn]["count"] += 1
        groups[sn]["files"].append(d)
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)


def search_fts(query: str, limit: int = 200) -> list[sqlite3.Row]:
    con = _conn()
    try:
        rows = con.execute(
            """
            SELECT f.*, snippet(log_fts_idx, 4, '<mark>', '</mark>', '…', 20) AS snippet
            FROM log_fts_idx
            JOIN log_files f ON f.path = log_fts_idx.path
            WHERE log_fts_idx MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return rows
    except sqlite3.OperationalError:
        return []


def get_all_paths(server_name: str, top_folder: str, folder: str | None = None, limit: int = 5000) -> list[str]:
    con = _conn()
    clauses = ["server=?", "top_folder=?"]
    params: list = [server_name, top_folder]
    if folder and folder != ".":
        clauses.append("(folder=? OR folder LIKE ? ESCAPE '\\')")
        params += [folder, _like_escape(folder) + "\\\\%"]
    rows = con.execute(
        f"SELECT path FROM log_files WHERE {' AND '.join(clauses)} ORDER BY modified DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [r[0] for r in rows]


def count_folder_files(server_name: str, top_folder: str, folder: str | None = None) -> int:
    con = _conn()
    clauses = ["server=?", "top_folder=?"]
    params: list = [server_name, top_folder]
    if folder and folder != ".":
        clauses.append("(folder=? OR folder LIKE ? ESCAPE '\\')")
        params += [folder, _like_escape(folder) + "\\\\%"]
    return con.execute(
        f"SELECT COUNT(*) FROM log_files WHERE {' AND '.join(clauses)}", params
    ).fetchone()[0]


def get_server_status() -> list[dict]:
    con = _conn()
    rows = con.execute("""
        SELECT f.server,
               COUNT(*) OVER (PARTITION BY f.server) AS file_count,
               f.modified        AS last_modified,
               f.name            AS latest_name,
               f.path            AS latest_path,
               f.top_folder      AS latest_top,
               sl.last_scanned_at
        FROM log_files f
        INNER JOIN (
            SELECT server, MAX(modified) AS max_mod
            FROM log_files
            GROUP BY server
        ) m ON f.server = m.server AND f.modified = m.max_mod
        LEFT JOIN scan_log sl ON f.server = sl.server
        GROUP BY f.server
        ORDER BY f.modified DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    con = _conn()
    row = con.execute(
        "SELECT COUNT(*), SUM(content_indexed), SUM(content_cached) FROM log_files"
    ).fetchone()
    total, indexed, cached = row
    return {
        "total":   total   or 0,
        "indexed": indexed or 0,
        "cached":  cached  or 0,
    }


# ── Backfill ───────────────────────────────────────────────────────────────────
def backfill_index(progress_fn: Callable[[int, int], None] | None = None) -> None:
    """Read files not yet content-indexed, insert into FTS, commit every 100."""
    from cache import read_cached_or_direct  # local import to avoid circular

    con = _conn()
    rows = con.execute(
        "SELECT path FROM log_files WHERE content_indexed=0 ORDER BY modified DESC"
    ).fetchall()
    total = len(rows)
    for i, (path,) in enumerate(rows):
        try:
            text = read_cached_or_direct(path)
        except OSError:
            text = ""
        tail = text[-10000:] if text else ""
        rec = con.execute("SELECT sn, station, status FROM log_files WHERE path=?", (path,)).fetchone()
        if rec:
            con.execute(
                "INSERT INTO log_fts (path, sn, station, status, body) VALUES (?,?,?,?,?)",
                (path, rec["sn"], rec["station"], rec["status"], tail),
            )
            con.execute(
                "INSERT INTO log_fts_idx (path, sn, station, status, body) VALUES (?,?,?,?,?)",
                (path, rec["sn"], rec["station"], rec["status"], tail),
            )
            con.execute("UPDATE log_files SET content_indexed=1 WHERE path=?", (path,))
        if (i + 1) % 100 == 0:
            con.commit()
            if progress_fn:
                progress_fn(i + 1, total)
    con.commit()
    if progress_fn:
        progress_fn(total, total)


def mark_cached(path: str) -> None:
    con = _conn()
    con.execute("UPDATE log_files SET content_cached=1 WHERE path=?", (path,))
    con.commit()


# ── Auto-scan log ──────────────────────────────────────────────────────────────
def get_last_scanned(server: str) -> float | None:
    row = _conn().execute(
        "SELECT last_scanned_at FROM scan_log WHERE server=?", (server,)
    ).fetchone()
    return row[0] if row else None


def set_last_scanned(server: str, ts: float) -> None:
    con = _conn()
    con.execute(
        "INSERT INTO scan_log(server, last_scanned_at) VALUES(?,?)"
        " ON CONFLICT(server) DO UPDATE SET last_scanned_at=excluded.last_scanned_at",
        (server, ts),
    )
    con.commit()
