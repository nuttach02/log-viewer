# Log Viewer — Codebase Guide

## What This Project Is

A web-based log file browser for SMB network shares (UNC paths). Browse, filter, search, and download `.log`/`.txt` files from remote servers. Two frontends exist:

- **`main.py`** — FastAPI production server (primary, actively maintained)
- **`app.py`** — Streamlit legacy UI (alternative, standalone)

The FastAPI frontend (`main.py`) is the production version. The Streamlit version (`app.py`) is an older alternative that duplicates some logic. They can share the same `log_index.db` but the FastAPI schema is richer (adds `top_folder`, `content_cached` columns via migration).

---

## Architecture

```
main.py          FastAPI routes — browse, view, grep, tail, download
scanner.py       SMB mounting (net use), file scan, status/date detection
db.py            SQLite layer — upsert, FTS5 search, backfill indexer
cache.py         Local sha256 file cache in file_cache/
config.py        Load servers.json, constants (DB_PATH, CACHE_DIR, etc.)
launcher.py      PyInstaller entry point — wraps main.py for exe bundling
migrate_db.py    One-shot migration script (backfills top_folder column)
```

**Templates:** `templates/` (Jinja2, base.html → browse.html, viewer.html, grep.html, search.html, status.html, admin.html)

**Static:** `static/app.js` (select-all, auto-refresh, live tail SSE, find-in-file, infinite scroll), `static/style.css`

---

## Running

```bash
# Dev server (FastAPI)
.venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8000 --reload

# Or via bat (no reload)
start_server.bat

# Streamlit alternative
.venv\Scripts\streamlit.exe run app.py
```

## Building Executable

```bash
build.bat   # runs PyInstaller with log_viewer.spec → dist\log_viewer\log_viewer.exe
```

The `launcher.py` is the PyInstaller entry point. It `os.chdir(sys._MEIPASS)` when frozen so `templates/` and `static/` paths resolve correctly.

---

## Configuration — servers.json

Each entry in `servers.json` (next to the exe/script) defines a server:

```json
[
  {
    "name": "display name",
    "path": "\\\\10.x.x.x\\share",
    "username": "user",
    "password": "pass",
    "exclude_folders": ["tmp", "archive"],
    "hide_file_patterns": ["*_debug.log"]
  }
]
```

`config.py` loads this at import time into the module-level `SERVERS` list. Changes require restart. Use `servers.json.example` as a template — do not commit real credentials.

---

## Database (SQLite — log_index.db)

Managed by `db.py`. Key tables:

- **`log_files`** — one row per file: `path` (PK), `server`, `top_folder`, `folder`, `sn`, `station`, `status`, `date`, `ts`, `modified`, `size`, `content_indexed`, `content_cached`
- **`log_fts`** — content store backing the FTS index
- **`log_fts_idx`** — FTS5 virtual table (`content='log_fts'`, porter tokenizer) for full-text search
- **`scan_log`** — last scan timestamp per server

`init_db()` runs DDL + migration on startup. `_migrate()` uses ALTER TABLE to add missing columns (safe to re-run). WAL mode, 64 MB page cache, 512 MB mmap.

Thread safety: each thread gets its own SQLite connection via `threading.local()`.

---

## File Status Detection

Files are classified as `PASS`, `FAIL`, or `UNKNOWN`:

1. **Path-level** (`detect_status_from_path`): checks if any path component is in `{pass, passed, ok}` / `{fail, failed, error, ng}`, then checks filename stem with regex
2. **Content-level** (`detect_status_from_content`): scans last 3000 chars of file content

Priority: folder path status → filename → content (optional, only in Streamlit deep-scan mode).

Station detection: `BFT` or `FQA` in path parts → station; otherwise `SIMPLE_FIXTURE`.

---

## Key Routes (FastAPI)

| Route | Description |
|---|---|
| `GET /` | Redirect to `/browse` |
| `GET /browse` | Live file browser (no DB, direct SMB scan) |
| `GET /browse/rows` | HTMX partial for auto-refresh / infinite scroll |
| `GET /view` | Log file viewer with pagination (500 lines/page) |
| `GET /download` | Single file download |
| `GET /download-zip`, `POST /download-zip` | Multi-file ZIP |
| `GET /tail` | SSE live tail (streams from current file position) |
| `POST /grep`, `POST /grep-stream` | Content search with SSE progress |
| `POST /grep-folder`, `POST /grep-folder-stream` | Folder-wide grep |
| `GET /api/file-content` | Inline file content for grep results |
| `GET /search` | FTS search page (stub — no DB in main.py) |
| `GET /status` | Server status page |
| `GET /admin` | Admin page |

Note: `main.py` is the **live browser** — it does not use the SQLite DB at all. The Streamlit `app.py` uses the DB for caching. The DB-heavy features (FTS search, backfill indexing) are implemented in `db.py` but currently only wired up via the Streamlit frontend.

---

## File Cache (cache.py)

Files are cached locally in `file_cache/` using sha256(path) as filename. Up to `DOWNLOAD_BYTE_CAP` (100 MB). Encoding: tries UTF-8, CP932, Latin-1 in order. `read_page_lines()` streams the cached file line-by-line — O(n) for each page access (acceptable given typical log sizes).

---

## Scanner (scanner.py)

`scan_folder()` — parallel recursive SMB scan using `ThreadPoolExecutor` (8 workers). Uses `threading.Event` + `in_flight` counter to know when all directories are processed. Each directory increments `in_flight` for its children before decrementing itself, preventing premature completion.

`list_folder_live()` — non-recursive single-directory listing (used for the main browse view).

`mount_share()` — Windows: `net use`; handles error 1219 (conflicting credentials) by deleting and re-mounting.

---

## Known Limitations

- **FTS not wired in FastAPI**: `main.py`'s `/search` route returns empty results; full-text search is only in the Streamlit app via `db.py`
- **No authentication**: any user with network access to the server can browse all logs
- **Scan is synchronous in browse**: `/browse` with `recursive=1` blocks the request until scan completes
- **Cache grows unbounded**: no eviction policy in `file_cache/`

---

## Coding Conventions

- Python 3.11+, type hints throughout, no `Optional[X]` (use `X | None`)
- SQLite queries use positional `?` params (never string interpolation)
- Sort column names whitelisted before use in dynamic ORDER BY
- LIKE patterns escape `\`, `%`, `_` via `_like_escape()` and use `ESCAPE '\\'`; folder suffix pattern must be `"\\\\%"` (Python literal) = `\\%` in SQL = literal backslash + wildcard
- `asyncio.get_running_loop()` inside coroutines (not the deprecated `get_event_loop()`)
- Streamlit errors/warnings must not be called before `st.set_page_config()`; collect into `_STARTUP_WARNINGS` list instead
