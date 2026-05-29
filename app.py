import streamlit as st
import os
import re
import html
import io
import json
import subprocess
import platform
import sqlite3
import zipfile
import fnmatch
import pandas as pd
from pathlib import Path
from datetime import datetime, date

# ─────────────────────────────────────────────
# CONFIG    
# ─────────────────────────────────────────────

# Fallback server, used only if servers.json is missing or invalid.
DEFAULT_SERVERS = [
    {
        "name":     "Main",
        "path":     r"\\10.94.94.20\store",
        "username": "test",
        "password": "qcitest",
    },
]

LOG_EXTENSIONS    = {".txt", ".log"}
TABLE_PAGE_SIZE   = 100                  # rows per page in the file list table
LOG_PAGE_LINES    = 500                  # lines per page in the log content viewer
LIVE_TAIL_LINES   = 200                  # lines shown in live-tail mode
DISPLAY_BYTE_CAP  = 5  * 1024 * 1024    # render at most 5 MB inline
DOWNLOAD_BYTE_CAP = 100 * 1024 * 1024   # refuse downloads above 100 MB
DB_PATH           = Path(__file__).parent / "log_index.db"
_RESULT_LABEL     = {"PASS": "✅ PASS", "FAIL": "❌ FAIL"}


_STARTUP_WARNINGS: list[str] = []


def load_servers() -> list[dict]:
    """Read servers.json next to app.py. Each entry needs name/path/username/password.
    Falls back to DEFAULT_SERVERS if the file is missing or malformed.
    Errors are collected into _STARTUP_WARNINGS and displayed after set_page_config()."""
    cfg_path = Path(__file__).parent / "servers.json"
    if not cfg_path.exists():
        return DEFAULT_SERVERS
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        _STARTUP_WARNINGS.append(f"⚠️ servers.json is invalid JSON ({e}); using default server list.")
        return DEFAULT_SERVERS
    if not isinstance(data, list) or not data:
        _STARTUP_WARNINGS.append("⚠️ servers.json must be a non-empty list; using default server list.")
        return DEFAULT_SERVERS
    required = {"name", "path", "username", "password"}
    cleaned = []
    for i, s in enumerate(data):
        if not isinstance(s, dict) or not required.issubset(s.keys()):
            _STARTUP_WARNINGS.append(f"⚠️ servers.json entry #{i} is missing keys; skipped.")
            continue
        entry = {k: s[k] for k in required}
        raw_excl = s.get("exclude_folders", [])
        entry["exclude_folders"] = (
            {str(x).lower() for x in raw_excl}
            if isinstance(raw_excl, list) else set()
        )
        raw_hide = s.get("hide_file_patterns", [])
        entry["hide_file_patterns"] = (
            [str(x) for x in raw_hide]
            if isinstance(raw_hide, list) else []
        )
        cleaned.append(entry)
    return cleaned or DEFAULT_SERVERS


SERVERS = load_servers()


# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Log Viewer",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

for _w in _STARTUP_WARNINGS:
    st.error(_w) if "invalid JSON" in _w or "non-empty list" in _w else st.warning(_w)

# ─────────────────────────────────────────────
# CUSTOM CSS — Inter (UI) + JetBrains Mono (code/paths)
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

html, body, [class*="css"], .stApp, .stMarkdown,
.stButton button, .stTextInput input, .stSelectbox, .stMultiSelect,
.stDateInput, .stTimeInput, .stNumberInput, .stToggle {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
}

.stApp { background: #0f1117; color: #e8eaf0; }

h1, h2, h3, h4, h5 {
    font-family: 'Inter', sans-serif !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em;
    color: #ffffff;
}

.main-title { font-size: 1.7rem; font-weight: 700; color: #ffffff; margin-bottom: 0.1rem; letter-spacing: -0.02em; }
.sub-title  { color: #6b7280; font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 1.2rem; font-weight: 500; }

.stat-card { background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 10px; padding: 1rem 1.2rem; text-align: center; }
.stat-number { font-size: 1.8rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; line-height: 1; }
.stat-label  { font-size: 0.7rem; letter-spacing: 0.08em; text-transform: uppercase; color: #6b7280; margin-top: 0.3rem; font-weight: 500; }

.pass-color    { color: #22d3a0; }
.fail-color    { color: #f87171; }
.unknown-color { color: #94a3b8; }
.total-color   { color: #818cf8; }

.badge-pass    { background: #052e1d; color: #22d3a0; border: 1px solid #22d3a0; padding: 2px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 600; font-family: 'JetBrains Mono', monospace; }
.badge-fail    { background: #2d0a0a; color: #f87171; border: 1px solid #f87171; padding: 2px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 600; font-family: 'JetBrains Mono', monospace; }
.badge-unknown { background: #1e2232; color: #94a3b8; border: 1px solid #3a4155; padding: 2px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 600; font-family: 'JetBrains Mono', monospace; }

.filename  { font-family: 'JetBrains Mono', monospace; font-size: 0.82rem; color: #c5cae0; word-break: break-all; }
.file-path { font-family: 'JetBrains Mono', monospace; font-size: 0.7rem;  color: #5b6680; word-break: break-all; margin-top: 3px; }
.file-meta { font-family: 'JetBrains Mono', monospace; font-size: 0.72rem; color: #6b7280; }

.log-content {
    background: #0a0c14; border: 1px solid #1e2232; border-radius: 10px;
    padding: 1.2rem 1.5rem; font-family: 'JetBrains Mono', monospace; font-size: 0.78rem;
    color: #cbd2dc; white-space: pre-wrap; word-break: break-all;
    max-height: 65vh; overflow-y: auto; line-height: 1.7;
}

div[data-testid="stSidebar"] { background: #0d0f18 !important; border-right: 1px solid #1e2232; }

.stTextInput input, .stSelectbox div[data-baseweb="select"] > div, .stMultiSelect div[data-baseweb="select"] > div {
    background: #1a1d27 !important; border: 1px solid #2a2d3a !important; color: #e8eaf0 !important;
}

.stButton > button {
    background: #1a1d27; color: #e8eaf0; border: 1px solid #2a2d3a;
    border-radius: 8px; font-weight: 500; font-family: 'Inter', sans-serif;
    padding: 0.45rem 0.9rem; transition: all 0.15s; font-size: 0.85rem;
}
.stButton > button:hover { background: #252938; border-color: #3a4155; transform: none; }
.stButton > button[kind="primary"] { background: #818cf8; color: #0f1117; border-color: #818cf8; font-weight: 600; }
.stButton > button[kind="primary"]:hover { background: #6366f1; border-color: #6366f1; }

hr { border-color: #1e2232 !important; }
.section-header { font-size: 0.68rem; letter-spacing: 0.12em; text-transform: uppercase; color: #4b5563; margin-bottom: 0.4rem; margin-top: 1.2rem; font-weight: 600; }
.folder-header  { font-size: 0.74rem; letter-spacing: 0.08em; text-transform: uppercase; color: #818cf8; margin-top: 1rem; margin-bottom: 0.3rem; padding: 0.4rem 0.8rem; background: #1a1d27; border-left: 3px solid #818cf8; border-radius: 0 6px 6px 0; }

.folder-card-icon { font-size: 1.4rem; }
.crumb-current   { font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; color: #818cf8; padding: 0.4rem 0.7rem; background: #1a1d27; border: 1px solid #2a2d3a; border-radius: 6px; display: inline-block; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# NETWORK SHARE MOUNT
# ─────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def mount_share(unc_path: str, username: str, password: str) -> bool:
    """Mount once per (path, user) per Streamlit process. Cached via st.cache_resource."""
    system = platform.system()
    if system == "Windows":
        result = subprocess.run(
            ["net", "use", unc_path, f"/user:{username}", password, "/persistent:no"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True
        if "1219" in (result.stderr or "") or "1219" in (result.stdout or ""):
            server = unc_path.lstrip("\\").split("\\")[0]
            subprocess.run(["net", "use", unc_path, "/delete", "/yes"], capture_output=True)
            subprocess.run(["net", "use", f"\\\\{server}\\*", "/delete", "/yes"], capture_output=True)
            subprocess.run(["cmdkey", f"/delete:{server}"], capture_output=True)
            subprocess.run(["cmdkey", f"/add:{server}", f"/user:{username}", f"/pass:{password}"], capture_output=True)
            result = subprocess.run(
                ["net", "use", unc_path, f"/user:{username}", password, "/persistent:no"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return True
        st.error(f"❌ Failed to connect to {unc_path}: {(result.stderr or result.stdout or '').strip()}")
        return False
    elif system in ("Linux", "Darwin"):
        mount_point = Path("/mnt/logviewer_share")
        mount_point.mkdir(parents=True, exist_ok=True)
        smb_path = unc_path.replace("\\\\", "//").replace("\\", "/")
        result = subprocess.run(
            ["sudo", "mount", "-t", "cifs", smb_path, str(mount_point),
             "-o", f"username={username},password={password},vers=3.0"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            st.error(f"❌ Failed to mount share: {result.stderr.strip()}")
            return False
        return True
    else:
        st.error(f"❌ Unsupported OS: {system}")
        return False


# ─────────────────────────────────────────────
# STATUS DETECTION
# ─────────────────────────────────────────────

# Precompiled at module level — re-compiling on every file is expensive at 20k+.
_NAME_PASS_RE    = re.compile(r'\bpass(ed)?\b|_ok_|_pass_')
_NAME_FAIL_RE    = re.compile(r'\bfail(ed)?\b|_ng_|_fail_|_error_')
_CONTENT_FAIL_RE = re.compile(r'\bfail(ed)?\b|\berror\b|\bng\b')
_CONTENT_PASS_RE = re.compile(r'\bpass(ed)?\b|result.*ok\b|\btest.*ok\b')
_PATH_PASS_PARTS = frozenset(("pass", "passed", "ok"))
_PATH_FAIL_PARTS = frozenset(("fail", "failed", "error", "ng"))


def detect_status_from_path(file_path: Path) -> str:
    parts = [p.lower() for p in file_path.parts]
    name  = file_path.stem.lower()
    for part in parts:
        if part in _PATH_PASS_PARTS:
            return "PASS"
        if part in _PATH_FAIL_PARTS:
            return "FAIL"
    if _NAME_PASS_RE.search(name):
        return "PASS"
    if _NAME_FAIL_RE.search(name):
        return "FAIL"
    return "UNKNOWN"


def detect_status_from_content(content: str) -> str:
    tail = content[-3000:].lower()
    if _CONTENT_FAIL_RE.search(tail):
        return "FAIL"
    if _CONTENT_PASS_RE.search(tail):
        return "PASS"
    return "UNKNOWN"


def get_status(file_path: Path, read_content: bool = False) -> str:
    status = detect_status_from_path(file_path)
    if status == "UNKNOWN" and read_content:
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            status  = detect_status_from_content(content)
        except Exception:
            pass
    return status


# ─────────────────────────────────────────────
# DATE EXTRACTION
# ─────────────────────────────────────────────

DATE_RE = re.compile(
    r'(?<!\d)(\d{4})[.\-_]?(\d{2})[.\-_]?(\d{2})(?!\d)'
    r'|(?<!\d)(\d{2})[.\-_](\d{2})[.\-_](\d{4})(?!\d)'
)


def _date_from_text(text: str) -> date | None:
    """Pull a date out of a single filename or folder segment. Used per-file."""
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        if m.group(1):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
    except ValueError:
        return None


def extract_date_from_path(file_path: Path, scan_root: Path) -> date | None:
    try:
        rel_parts = file_path.relative_to(scan_root).parts
    except ValueError:
        rel_parts = file_path.parts
    for part in rel_parts:
        d = _date_from_text(part)
        if d is not None:
            return d
    return None


# ─────────────────────────────────────────────
# RECURSIVE SCANNER (os.scandir + live progress)
# ─────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def _scan_cache() -> dict:
    """Process-wide scan results, keyed by (server_name, scan_root, deep_scan).
    Cleared by the Refresh button; survives reruns and across sessions."""
    return {}


def scan_folder(scan_root_str: str, deep_scan: bool, cache_key: tuple, exclude_folders: set | None = None) -> list[dict]:
    """
    Recursively scan a folder for .log/.txt files.

    Hot-loop choices for 20k+ file folders on UNC shares:
    - os.scandir minimises syscalls; entry.stat() returns cached data on Windows
      (FindNextFile already populated it — no extra roundtrip).
    - Folder-level metadata (relative path, path-date, status hint) computed once
      per directory, not per file.
    - Path objects avoided in the hot loop — string slicing replaces
      Path.relative_to(), which is allocation-heavy.
    - sn (serial number) and ts (test timestamp) extracted from the filename once
      at scan time and stored so the render layer never re-parses.
    """
    cache = _scan_cache()
    if cache_key in cache:
        return cache[cache_key]

    scan_root_norm = scan_root_str.rstrip("\\/")
    prefix_len     = len(scan_root_norm) + 1   # +1 to skip the leading separator
    files: list[dict] = []
    dirs_visited = 0

    excl_folders = exclude_folders or set()

    # Bind hot names to locals — measurable at 20k+ iterations.
    pass_re_search = _NAME_PASS_RE.search
    fail_re_search = _NAME_FAIL_RE.search
    pass_parts     = _PATH_PASS_PARTS
    fail_parts     = _PATH_FAIL_PARTS
    fromtimestamp  = datetime.fromtimestamp

    with st.status(f"Scanning {Path(scan_root_str).name}…", expanded=False) as status:
        stack = [scan_root_norm]
        while stack:
            current = stack.pop()
            dirs_visited += 1

            # Per-folder metadata — computed once, applied to every file inside.
            if current == scan_root_norm:
                rel_folder         = "."
                folder_status_hint = None
                folder_date        = None
                folder_station     = "SIMPLE_FIXTURE"
            else:
                rel_folder   = current[prefix_len:]
                lower_parts  = [p for p in rel_folder.replace("/", "\\").lower().split("\\") if p]
                folder_status_hint = None
                for part in lower_parts:
                    if part in pass_parts:
                        folder_status_hint = "PASS"; break
                    if part in fail_parts:
                        folder_status_hint = "FAIL"; break
                folder_date = None
                for part in lower_parts:
                    folder_date = _date_from_text(part)
                    if folder_date is not None:
                        break
                if "bft" in lower_parts:
                    folder_station = "BFT"
                elif "fqa" in lower_parts:
                    folder_station = "FQA"
                else:
                    folder_station = "SIMPLE_FIXTURE"

            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() not in excl_folders:
                                    stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue

                            name       = entry.name
                            name_lower = name.lower()
                            if not name_lower.endswith((".log", ".txt")):
                                continue

                            try:
                                stat = entry.stat(follow_symlinks=False)
                            except OSError:
                                continue

                            stem_lower = name_lower[:-4]
                            if folder_status_hint is not None:
                                status_val = folder_status_hint
                            elif pass_re_search(stem_lower):
                                status_val = "PASS"
                            elif fail_re_search(stem_lower):
                                status_val = "FAIL"
                            else:
                                status_val = "UNKNOWN"

                            if status_val == "UNKNOWN" and deep_scan:
                                try:
                                    content = Path(entry.path).read_text(encoding="utf-8", errors="ignore")
                                    status_val = detect_status_from_content(content)
                                except Exception:
                                    pass

                            file_date = _date_from_text(name) or folder_date
                            relative  = name if rel_folder == "." else f"{rel_folder}\\{name}"

                            # SN = filename before first underscore (original case).
                            # ts = YYYYMMDDHHMMSS embedded after the first underscore.
                            uscore_idx = stem_lower.find("_")
                            if uscore_idx != -1:
                                sn     = name[:uscore_idx]
                                ts_raw = stem_lower[uscore_idx + 1:][:14]
                            else:
                                sn     = name[:-4]
                                ts_raw = ""
                            try:
                                file_ts = datetime.strptime(ts_raw, "%Y%m%d%H%M%S") \
                                          if len(ts_raw) == 14 else None
                            except ValueError:
                                file_ts = None

                            files.append({
                                "path_str": entry.path,
                                "name":     name,
                                "relative": relative,
                                "folder":   rel_folder,
                                "status":   status_val,
                                "size":     stat.st_size,
                                "modified": fromtimestamp(stat.st_mtime),
                                "date":     file_date,
                                "sn":       sn,
                                "ts":       file_ts,
                                "station":  folder_station,
                            })
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError):
                continue

            if dirs_visited % 25 == 0:
                status.update(label=f"Scanning… {dirs_visited} folders · {len(files)} log files")

        status.update(
            label=f"Scanned {dirs_visited} folders · found {len(files)} log files",
            state="complete",
        )

    upsert_files_metadata(files, cache_key[0])
    cache[cache_key] = files
    return files


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes/1024/1024:.1f} MB"
    return f"{size_bytes/1024/1024/1024:.2f} GB"


def build_zip_bytes(files: list[dict]) -> bytes:
    """Pack selected files into an in-memory ZIP and return the raw bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            try:
                zf.write(f["path_str"], arcname=f["name"])
            except OSError:
                pass
    return buf.getvalue()


def status_badge(status: str) -> str:
    if status == "PASS":
        return '<span class="badge-pass">✓ PASS</span>'
    if status == "FAIL":
        return '<span class="badge-fail">✗ FAIL</span>'
    return '<span class="badge-unknown">? UNKNOWN</span>'


def highlight_log(content: str) -> str:
    out = []
    for line in content.split("\n"):
        ll   = line.lower()
        safe = html.escape(line)
        if re.search(r'\bfail(ed)?\b|\berror\b|\bng\b', ll):
            out.append(f'<span style="color:#f87171">{safe}</span>')
        elif re.search(r'\bpass(ed)?\b|\bok\b', ll):
            out.append(f'<span style="color:#22d3a0">{safe}</span>')
        else:
            out.append(safe)
    return "\n".join(out)


def _natural_key(s: str) -> list:
    """Sort key that orders 'item2' before 'item10' (numeric-aware)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_subfolders(path: Path, exclude: set[str] | None = None) -> list[str]:
    """Direct child directories of path, naturally sorted, hidden entries skipped."""
    excl = exclude or set()
    try:
        names = [
            d.name for d in path.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in excl
        ]
        return sorted(names, key=_natural_key)
    except (PermissionError, OSError):
        return []


def navigate_to(rel_path: str) -> None:
    """Set the current location and clear any open dialog."""
    st.session_state["current_path"]       = rel_path
    st.session_state["selected_file_info"] = None


# ─────────────────────────────────────────────
# SEARCH INDEX (SQLite FTS5)
# ─────────────────────────────────────────────

@st.cache_resource
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS log_files (
            path            TEXT PRIMARY KEY,
            server          TEXT NOT NULL,
            name            TEXT NOT NULL,
            folder          TEXT,
            sn              TEXT,
            station         TEXT,
            status          TEXT,
            date            TEXT,
            ts              TEXT,
            modified        REAL,
            size            INTEGER,
            content_indexed INTEGER DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(
            path    UNINDEXED,
            sn,
            station,
            status,
            body,
            tokenize='porter unicode61'
        );
        CREATE INDEX IF NOT EXISTS idx_content_indexed ON log_files(content_indexed);
    """)
    conn.commit()
    return conn


def upsert_files_metadata(files: list[dict], server_name: str) -> None:
    if not files:
        return
    conn = get_db()
    rows = [
        (
            f["path_str"],
            server_name,
            f["name"],
            f["folder"],
            f["sn"],
            f.get("station", ""),
            f["status"],
            str(f["date"]) if f["date"] else None,
            f["ts"].strftime("%Y-%m-%dT%H:%M:%S") if f["ts"] else None,
            f["modified"].timestamp(),
            f["size"],
        )
        for f in files
    ]
    conn.executemany("""
        INSERT INTO log_files
            (path, server, name, folder, sn, station, status, date, ts, modified, size)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            status          = excluded.status,
            modified        = excluded.modified,
            size            = excluded.size,
            content_indexed = CASE
                WHEN excluded.modified != log_files.modified THEN 0
                ELSE log_files.content_indexed
            END
    """, rows)
    conn.commit()


def index_file_content(path_str: str, sn: str, station: str, status: str, content: str) -> None:
    conn = get_db()
    row = conn.execute(
        "SELECT content_indexed FROM log_files WHERE path=?", (path_str,)
    ).fetchone()
    if row is None or row[0] == 1:
        return
    conn.execute("DELETE FROM log_fts WHERE path=?", (path_str,))
    conn.execute(
        "INSERT INTO log_fts(path, sn, station, status, body) VALUES (?,?,?,?,?)",
        (path_str, sn, station, status, content[:500_000]),
    )
    conn.execute("UPDATE log_files SET content_indexed=1 WHERE path=?", (path_str,))
    conn.commit()


def search_index(query: str, limit: int = 100) -> list[dict] | None:
    """Return matching file dicts, or None on bad query syntax."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT f.path, f.server, f.name, f.folder, f.sn, f.station,
                   f.status, f.date, f.modified, f.size,
                   snippet(log_fts, 4, '<b style="color:#fde68a">', '</b>', ' … ', 20)
            FROM log_fts
            JOIN log_files f ON log_fts.path = f.path
            WHERE log_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
    except sqlite3.OperationalError:
        return None
    return [
        {
            "path_str": r[0],
            "server":   r[1],
            "name":     r[2],
            "folder":   r[3],
            "sn":       r[4],
            "station":  r[5],
            "status":   r[6],
            "date":     r[7],
            "ts":       None,
            "modified": datetime.fromtimestamp(r[8]) if r[8] else datetime.now(),
            "size":     r[9],
            "snippet":  r[10] or "",
        }
        for r in rows
    ]


@st.cache_data(ttl=60)
def get_index_stats() -> tuple[int, int]:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(content_indexed), 0) FROM log_files"
    ).fetchone()
    return (row[0], row[1])


def backfill_content_index(progress_callback=None) -> tuple[int, int]:
    """
    Index the content of every file in log_files that has content_indexed=0.
    Reads each file from disk, inserts into log_fts, commits every 100 rows.
    Returns (indexed, skipped) counts.
    """
    conn = get_db()
    pending = conn.execute(
        "SELECT path, sn, station, status FROM log_files WHERE content_indexed=0"
    ).fetchall()

    indexed = 0
    skipped = 0
    total   = len(pending)

    for i, (path_str, sn, station, status) in enumerate(pending):
        try:
            content = Path(path_str).read_text(encoding="utf-8", errors="ignore")
            conn.execute("DELETE FROM log_fts WHERE path=?", (path_str,))
            conn.execute(
                "INSERT INTO log_fts(path, sn, station, status, body) VALUES (?,?,?,?,?)",
                (path_str, sn, station, status, content[:500_000]),
            )
            conn.execute("UPDATE log_files SET content_indexed=1 WHERE path=?", (path_str,))
            indexed += 1
        except Exception:
            skipped += 1

        if (i + 1) % 100 == 0:
            conn.commit()
            if progress_callback:
                progress_callback(i + 1, total)

    conn.commit()
    if progress_callback:
        progress_callback(total, total)
    return indexed, skipped


# ─────────────────────────────────────────────
# LOG POPUP (st.dialog)
# ─────────────────────────────────────────────

@st.dialog("📄 Log File Viewer", width="large")
def show_log_dialog(file_info: dict):
    path = Path(file_info["path_str"])

    st.markdown(
        f'<div class="filename" style="font-size:1rem;color:#818cf8">📄 {html.escape(file_info["name"])}</div>'
        f'<div class="file-path">📁 {html.escape(file_info["folder"])}'
        + (f'&nbsp;·&nbsp;📅 {file_info["date"]}' if file_info["date"] else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    try:
        file_size = path.stat().st_size
    except OSError as e:
        st.error(f"Cannot stat file: {e}")
        return

    if file_size > DOWNLOAD_BYTE_CAP:
        st.error(
            f"File is {format_size(file_size)} — exceeds {format_size(DOWNLOAD_BYTE_CAP)} "
            f"server limit. Open it directly via the UNC path:"
        )
        st.code(str(path))
        if st.button("Close", key=f"close_too_big_{file_info['path_str']}"):
            st.session_state["selected_file_info"] = None
            st.rerun()
        return

    try:
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
    except Exception as e:
        st.error(f"Cannot read file: {e}")
        return

    content    = raw_bytes.decode("utf-8", errors="ignore")
    index_file_content(
        file_info["path_str"], file_info["sn"],
        file_info.get("station", ""), file_info["status"], content,
    )
    all_lines  = content.splitlines()
    total_lines = len(all_lines)
    total_log_pages = max(1, (total_lines + LOG_PAGE_LINES - 1) // LOG_PAGE_LINES)

    path_status    = detect_status_from_path(path)
    content_status = detect_status_from_content(content)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(f"Path status: {status_badge(path_status)}", unsafe_allow_html=True)
    with c2:
        st.markdown(f"Content status: {status_badge(content_status)}", unsafe_allow_html=True)
    with c3:
        st.markdown(
            f'<span class="file-meta">{format_size(file_size)} · {file_info["modified"].strftime("%Y-%m-%d %H:%M")}</span>',
            unsafe_allow_html=True,
        )

    live_mode = st.toggle(
        "🔴 Live tail", key=f"live_{file_info['path_str']}",
        help="Auto-refresh every 2 s and show the last 200 lines. Use while a test is running.",
    )

    if live_mode:
        @st.fragment(run_every=2)
        def _live_tail():
            try:
                fsize    = path.stat().st_size
                read_back = min(fsize, LIVE_TAIL_LINES * 300)
                with open(path, "rb") as _fh:
                    _fh.seek(max(0, fsize - read_back))
                    tail_bytes = _fh.read()
                tail_text = tail_bytes.decode("utf-8", errors="ignore")
                if fsize > read_back:
                    nl = tail_text.find("\n")
                    tail_text = tail_text[nl + 1:] if nl != -1 else tail_text
                tail_lines = tail_text.splitlines()[-LIVE_TAIL_LINES:]
                st.markdown(
                    f'<div class="log-content">{highlight_log(chr(10).join(tail_lines))}</div>',
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"Last {len(tail_lines)} lines · {format_size(fsize)} · "
                    f"refreshing every 2 s · {datetime.now().strftime('%H:%M:%S')}"
                )
            except Exception as _e:
                st.error(f"Cannot read file: {_e}")

        _live_tail()

        if st.button("✕ Close", key=f"dialog_close_live_{file_info['path_str']}", use_container_width=True):
            st.session_state["selected_file_info"] = None
            st.rerun()

    else:
        st.markdown("<br>", unsafe_allow_html=True)

        search_in_file = st.text_input(
            "🔍 Search inside log",
            placeholder="Highlight text within this file…",
            key=f"inner_search_{file_info['path_str']}",
        )

        # Page state — reset to 0 whenever a different file is opened
        page_key = f"log_page_{file_info['path_str']}"
        if page_key not in st.session_state:
            st.session_state[page_key] = 0
        log_page = min(st.session_state[page_key], total_log_pages - 1)

        if total_log_pages > 1:
            st.caption(
                f"Page {log_page + 1} of {total_log_pages}  ·  "
                f"lines {log_page * LOG_PAGE_LINES + 1}–{min((log_page + 1) * LOG_PAGE_LINES, total_lines)} "
                f"of {total_lines} total"
            )

        page_lines   = all_lines[log_page * LOG_PAGE_LINES : (log_page + 1) * LOG_PAGE_LINES]
        page_content = "\n".join(page_lines)

        content_html = highlight_log(page_content)
        if search_in_file.strip():
            escaped_q = re.escape(html.escape(search_in_file.strip()))
            content_html = re.sub(
                f"({escaped_q})",
                r'<mark style="background:#854d0e;color:#fde68a;border-radius:3px">\1</mark>',
                content_html,
                flags=re.IGNORECASE,
            )

        st.markdown(f'<div class="log-content">{content_html}</div>', unsafe_allow_html=True)

        btn_prev, btn_next, btn_dl, btn_close = st.columns([1, 1, 2, 1])
        with btn_prev:
            if st.button("◀ Prev", key=f"log_prev_{file_info['path_str']}",
                         disabled=log_page == 0, use_container_width=True):
                st.session_state[page_key] = log_page - 1
                st.rerun()
        with btn_next:
            if st.button("Next ▶", key=f"log_next_{file_info['path_str']}",
                         disabled=log_page >= total_log_pages - 1, use_container_width=True):
                st.session_state[page_key] = log_page + 1
                st.rerun()
        with btn_dl:
            st.download_button(
                label="⬇️ Download log",
                data=raw_bytes,
                file_name=path.name,
                mime="text/plain",
                use_container_width=True,
            )
        with btn_close:
            if st.button("✕ Close", key=f"dialog_close_{file_info['path_str']}", use_container_width=True):
                st.session_state["selected_file_info"] = None
                st.rerun()


# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
if "current_server_name" not in st.session_state:
    st.session_state["current_server_name"] = SERVERS[0]["name"]
if "current_path" not in st.session_state:
    st.session_state["current_path"] = ""           # relative to share root, "/"-separated
if "selected_file_info" not in st.session_state:
    st.session_state["selected_file_info"] = None


def get_current_server() -> dict:
    name = st.session_state["current_server_name"]
    for s in SERVERS:
        if s["name"] == name:
            return s
    return SERVERS[0]


# ─────────────────────────────────────────────
# SIDEBAR — server, filters, page size, refresh
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown('<div class="main-title">📋 Log Viewer</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-title">Fixture Log Browser</div>', unsafe_allow_html=True)

    # Server selector
    st.markdown('<div class="section-header">Server</div>', unsafe_allow_html=True)
    server_names = [s["name"] for s in SERVERS]
    chosen_idx   = server_names.index(st.session_state["current_server_name"]) \
                   if st.session_state["current_server_name"] in server_names else 0
    chosen_name  = st.selectbox(
        "Server", options=server_names, index=chosen_idx, label_visibility="collapsed",
    )
    if chosen_name != st.session_state["current_server_name"]:
        st.session_state["current_server_name"] = chosen_name
        st.session_state["current_path"]        = ""
        st.session_state["selected_file_info"]  = None
        st.rerun()

    # Status filter
    st.markdown('<div class="section-header">Filter Status</div>', unsafe_allow_html=True)
    filter_status = st.multiselect(
        "Status", options=["PASS", "FAIL", "UNKNOWN"],
        default=["PASS", "FAIL", "UNKNOWN"],
        label_visibility="collapsed",
    )

    # Per-field search
    st.markdown('<div class="section-header">Search</div>', unsafe_allow_html=True)
    search_pn      = st.text_input("PN",      placeholder="PN…",      key="s_pn")
    search_sn      = st.text_input("SN",      placeholder="SN…",      key="s_sn")
    search_station = st.text_input("Station", placeholder="Station…", key="s_station")

    # Full-text search across indexed log content
    st.markdown('<div class="section-header">Content Search</div>', unsafe_allow_html=True)
    fts_query = st.text_input(
        "FTS", placeholder="error AND timeout…", key="s_fts",
        help="Searches content of files you have opened. AND / OR / phrase search supported.",
        label_visibility="collapsed",
    )
    _meta_count, _content_count = get_index_stats()
    _pending_count = _meta_count - _content_count
    st.caption(f"{_meta_count:,} files in index · {_content_count:,} content-indexed")
    if _pending_count > 0:
        if st.button(
            f"⚡ Backfill {_pending_count:,} files",
            use_container_width=True,
            help="Index the content of all scanned-but-not-yet-opened files so they appear in search.",
        ):
            st.session_state["run_backfill"] = True
            st.rerun()

    # Date filter (matches date folders in path)
    st.markdown('<div class="section-header">Date Filter</div>', unsafe_allow_html=True)
    date_filter_on = st.toggle("🗓️ Filter by date folder", value=False)
    date_from = date_to = None
    if date_filter_on:
        c1, c2 = st.columns(2)
        with c1:
            date_from = st.date_input("From", value=date(2026, 1, 1))
        with c2:
            date_to   = st.date_input("To",   value=date.today())

    # Modified-time filter
    st.markdown('<div class="section-header">Modified Time Filter</div>', unsafe_allow_html=True)
    mtime_filter_on = st.toggle("⏰ Filter by modified time", value=False)
    mtime_from = mtime_to = None
    if mtime_filter_on:
        c1, c2 = st.columns(2)
        with c1:
            mt_from_d = st.date_input("From date", value=date.today(), key="mt_from_d")
            mt_from_t = st.time_input("From time", value=datetime.min.time(), key="mt_from_t")
            mtime_from = datetime.combine(mt_from_d, mt_from_t)
        with c2:
            mt_to_d   = st.date_input("To date",   value=date.today(), key="mt_to_d")
            mt_to_t   = st.time_input("To time",   value=datetime.max.time().replace(microsecond=0), key="mt_to_t")
            mtime_to  = datetime.combine(mt_to_d, mt_to_t)

    # Display options
    st.markdown('<div class="section-header">Display</div>', unsafe_allow_html=True)
    deep_scan = st.toggle(
        "🔍 Deep scan (read content)", value=False,
        help="Read file content to detect Pass/Fail when path is ambiguous. Slower on large folders.",
    )

    # Hide file patterns
    st.markdown('<div class="section-header">Hide File Patterns</div>', unsafe_allow_html=True)
    hide_patterns_input = st.text_input(
        "Hide patterns",
        placeholder="e.g. *_sfc.log, *_debug.txt",
        help="Comma-separated glob patterns. Files whose names match any pattern are hidden. Example: *_sfc.log hides files ending with _sfc.log.",
        key="hide_file_patterns_ui",
        label_visibility="collapsed",
    )

    # Auto-refresh
    st.markdown('<div class="section-header">Auto-Refresh</div>', unsafe_allow_html=True)
    auto_refresh = st.toggle("🔄 Auto-refresh file list", value=False, key="auto_refresh")
    if auto_refresh:
        st.select_slider(
            "Interval", options=[5, 10, 30, 60], value=30,
            format_func=lambda x: f"{x}s",
            key="refresh_secs", label_visibility="collapsed",
        )

    st.markdown("---")
    _btn_ref, _btn_all = st.columns(2)
    with _btn_ref:
        if st.button("🔄 Refresh", use_container_width=True):
            _scan_cache().clear()
            st.rerun()
    with _btn_all:
        if st.button(
            "📂 Scan All", use_container_width=True,
            help="Scan every top-level folder on this server and index all metadata into the search DB.",
        ):
            st.session_state["run_scan_all"] = True
            st.rerun()


# ─────────────────────────────────────────────
# MOUNT + RESOLVE LOCATION
# ─────────────────────────────────────────────
server      = get_current_server()
share_ok    = mount_share(server["path"], server["username"], server["password"])
if not share_ok:
    st.stop()

server_root = Path(server["path"])
if not server_root.exists():
    st.error(f"❌ Server path not found: {server['path']} — check VPN / network")
    st.stop()

# ─────────────────────────────────────────────
# SCAN ALL FOLDERS
# ─────────────────────────────────────────────
if st.session_state.pop("run_scan_all", False):
    _excl       = server.get("exclude_folders") or set()
    _top_dirs   = sorted(
        [d for d in server_root.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in _excl],
        key=lambda d: d.name.lower(),
    )
    if not _top_dirs:
        st.warning("No folders found at server root.")
    else:
        _sa_total_files = 0
        with st.status(f"Scanning {len(_top_dirs)} folders on {server['name']}…", expanded=True) as _sa_s:
            for _sa_i, _sa_dir in enumerate(_top_dirs, 1):
                _sa_s.update(label=f"Scanning {_sa_dir.name} ({_sa_i} / {len(_top_dirs)})…")
                _sa_ck = (server["name"], str(_sa_dir), False, frozenset(_excl))
                _scan_cache().pop(_sa_ck, None)
                _sa_files = scan_folder(str(_sa_dir), False, _sa_ck, exclude_folders=_excl)
                _sa_total_files += len(_sa_files)
            _sa_s.update(
                label=f"Done — {len(_top_dirs)} folders · {_sa_total_files:,} log files indexed",
                state="complete",
            )
    st.rerun()

current_path = st.session_state["current_path"]
location     = server_root / current_path if current_path else server_root

# Guard against a stale current_path (folder was deleted/renamed on the share)
if current_path and not location.exists():
    st.error(f"⚠️ Folder no longer exists: {location}")
    if st.button("← Back to server root"):
        navigate_to("")
        st.rerun()
    st.stop()


# ─────────────────────────────────────────────
# BREADCRUMB — clickable pills
# ─────────────────────────────────────────────
parts        = current_path.split("/") if current_path else []
crumb_labels = [f"🏠 {server['name']}"] + parts
n_crumbs     = len(crumb_labels)

# Each pill takes 1 unit; trailing filler keeps pills compact on shallow paths.
filler_units = max(1, 10 - n_crumbs)
crumb_cols   = st.columns([1] * n_crumbs + [filler_units])

for i, label in enumerate(crumb_labels):
    with crumb_cols[i]:
        is_current = (i == n_crumbs - 1)
        if is_current:
            st.markdown(f'<div class="crumb-current">{html.escape(label)}</div>', unsafe_allow_html=True)
        else:
            if st.button(label, key=f"crumb_{i}", use_container_width=True):
                navigate_to("/".join(parts[:i]))
                st.rerun()

st.markdown("<br>", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# PHASE 3 — BACKFILL EXECUTION
# ─────────────────────────────────────────────
if st.session_state.pop("run_backfill", False):
    _bf_meta, _bf_done = get_index_stats()
    _bf_pending = _bf_meta - _bf_done
    if _bf_pending > 0:
        _prog_bar  = st.progress(0.0, text=f"Indexing 0 / {_bf_pending:,}…")
        _prog_text = st.empty()

        def _on_progress(done: int, total: int) -> None:
            _prog_bar.progress(done / total, text=f"Indexing {done:,} / {total:,}…")

        _bf_indexed, _bf_skipped = backfill_content_index(progress_callback=_on_progress)
        _prog_bar.empty()
        _prog_text.success(
            f"Backfill complete — {_bf_indexed:,} files indexed"
            + (f", {_bf_skipped:,} skipped (file not readable)" if _bf_skipped else "")
        )
    st.rerun()


# ─────────────────────────────────────────────
# DIALOG — rendered here so it works in all modes (browse + search)
# ─────────────────────────────────────────────
if st.session_state.get("selected_file_info"):
    show_log_dialog(st.session_state["selected_file_info"])


# ─────────────────────────────────────────────
# FULL-TEXT SEARCH RESULTS
# ─────────────────────────────────────────────
_fts_q = st.session_state.get("s_fts", "").strip()
if _fts_q:
    _results = search_index(_fts_q)
    if _results is None:
        st.error(
            "Invalid search query. Wrap phrases in quotes or use AND / OR between terms. "
            'Example: `"connection error"` or `fail AND timeout`'
        )
    elif not _results:
        st.info(
            f'No results for **{html.escape(_fts_q)}**. '
            "Only files you have opened in the viewer are content-indexed."
        )
    else:
        st.markdown(
            f'<div class="section-header">'
            f'{len(_results)} result{"s" if len(_results) != 1 else ""} for '
            f'<em>{html.escape(_fts_q)}</em></div>',
            unsafe_allow_html=True,
        )
        _SR_COLS = [1.8, 0.9, 0.75, 0.85, 1.1, 3.2, 0.4]
        _sr_hdr = st.columns(_SR_COLS)
        for _col, _lbl in zip(_sr_hdr, ["SN", "Station", "Status", "Date", "Server", "Match", ""]):
            _col.markdown(
                f'<div class="section-header" style="margin:0;padding-bottom:4px">{_lbl}</div>',
                unsafe_allow_html=True,
            )
        st.markdown('<hr style="border-color:#1e2232;margin:2px 0 2px 0">', unsafe_allow_html=True)
        with st.container(height=560, border=False):
            for _ri, _r in enumerate(_results):
                _rc0, _rc1, _rc2, _rc3, _rc4, _rc5, _rc6 = st.columns(_SR_COLS)
                _rc0.markdown(f'<div class="filename"    style="padding-top:6px">{html.escape(_r["sn"])}</div>',            unsafe_allow_html=True)
                _rc1.markdown(f'<div class="file-meta"  style="padding-top:6px">{html.escape(_r["station"] or "")}</div>', unsafe_allow_html=True)
                _rc2.markdown(f'<div style="padding-top:6px">{_RESULT_LABEL.get(_r["status"], "➖ UNKNOWN")}</div>',        unsafe_allow_html=True)
                _rc3.markdown(f'<div class="file-meta"  style="padding-top:6px">{_r["date"] or ""}</div>',                 unsafe_allow_html=True)
                _rc4.markdown(f'<div class="file-meta"  style="padding-top:6px">{html.escape(_r["server"] or "")}</div>',  unsafe_allow_html=True)
                _rc5.markdown(f'<div class="file-meta"  style="padding-top:6px">{_r["snippet"]}</div>',                    unsafe_allow_html=True)
                with _rc6:
                    if st.button("👁", key=f"sr_view_{_ri}", help=_r["name"]):
                        st.session_state["selected_file_info"] = _r
                        st.rerun()
    st.stop()


# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────
header_label = current_path.split("/")[-1] if current_path else server["name"]
st.markdown(f'<div class="main-title">{html.escape(header_label)}</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="sub-title">{html.escape(str(location))}</div>',
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────
# SUBFOLDERS — clickable cards (searchable)
# ─────────────────────────────────────────────
subfolders = list_subfolders(location, exclude=server.get("exclude_folders"))

if subfolders:
    col_label, col_search = st.columns([2, 3])
    with col_label:
        st.markdown(
            f'<div class="section-header" style="margin-top:0.4rem">Folders ({len(subfolders)})</div>',
            unsafe_allow_html=True,
        )
    with col_search:
        folder_search = st.text_input(
            "Folder search",
            placeholder="🔍 Search folders…",
            label_visibility="collapsed",
            key=f"folder_search_{current_path}",
        )

    q = folder_search.strip().lower()
    visible_folders = [n for n in subfolders if q in n.lower()] if q else subfolders

    if visible_folders:
        cols_per_row = 4
        for i in range(0, len(visible_folders), cols_per_row):
            row = st.columns(cols_per_row)
            for j, name in enumerate(visible_folders[i:i + cols_per_row]):
                with row[j]:
                    if st.button(f"📁  {name}", key=f"folder_{i + j}_{name}", use_container_width=True):
                        new_path = f"{current_path}/{name}" if current_path else name
                        navigate_to(new_path)
                        st.rerun()
    elif q:
        st.caption(f"No folders match “{folder_search}”.")


# ─────────────────────────────────────────────
# AT SERVER ROOT — don't recursively scan everything; let the user drill in
# ─────────────────────────────────────────────
if not current_path:
    if not subfolders:
        st.info("No model folders visible at server root.")
    else:
        st.markdown("<br>", unsafe_allow_html=True)
        st.info("👆 Click a folder above to browse its log files.")
    st.stop()


# ─────────────────────────────────────────────
# SCAN CURRENT LOCATION + APPLY FILTERS
# ─────────────────────────────────────────────
excl      = server.get("exclude_folders") or set()
cache_key = (server["name"], str(location), deep_scan, frozenset(excl))
all_files = scan_folder(str(location), deep_scan, cache_key, exclude_folders=excl)

filtered = list(all_files)

if filter_status:
    filtered = [f for f in filtered if f["status"] in filter_status]

if search_pn.strip():
    q = search_pn.strip().lower()
    filtered = [f for f in filtered if q in f["relative"].lower()]
if search_sn.strip():
    q = search_sn.strip().lower()
    filtered = [f for f in filtered if q in f["sn"].lower()]
if search_station.strip():
    q = search_station.strip().lower()
    filtered = [f for f in filtered if q in f.get("station", "").lower()]

if date_filter_on and date_from and date_to:
    # Fall back to mtime when the path has no parseable date — otherwise those
    # files would silently bypass the range filter.
    def _in_range(f):
        d = f["date"] if f["date"] is not None else f["modified"].date()
        return date_from <= d <= date_to
    filtered = [f for f in filtered if _in_range(f)]

if mtime_filter_on and mtime_from and mtime_to:
    # Pad the To bound when it lands on 23:59:59 so files with sub-second
    # mtimes in that same second are still included.
    end = mtime_to
    if mtime_to.hour == 23 and mtime_to.minute == 59 and mtime_to.second == 59:
        end = mtime_to.replace(microsecond=999999)
    filtered = [f for f in filtered if mtime_from <= f["modified"] <= end]

_server_hide_patterns = server.get("hide_file_patterns", [])
_ui_hide_raw          = st.session_state.get("hide_file_patterns_ui", "")
_ui_hide_patterns     = [p.strip() for p in _ui_hide_raw.split(",") if p.strip()]
_all_hide_patterns    = _server_hide_patterns + _ui_hide_patterns
if _all_hide_patterns:
    filtered = [
        f for f in filtered
        if not any(fnmatch.fnmatch(f["name"].lower(), p.lower()) for p in _all_hide_patterns)
    ]


# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────
total         = len(all_files)
pass_count    = sum(1 for f in all_files if f["status"] == "PASS")
fail_count    = sum(1 for f in all_files if f["status"] == "FAIL")
unknown_count = sum(1 for f in all_files if f["status"] == "UNKNOWN")

st.markdown(
    f'<div class="sub-title" style="margin-top:1rem">{len(filtered)} files shown · {total} total</div>',
    unsafe_allow_html=True,
)

for col, num, label, css in zip(
    st.columns(4),
    [total, pass_count, fail_count, unknown_count],
    ["Total Files", "Pass", "Fail", "Unknown"],
    ["total-color", "pass-color", "fail-color", "unknown-color"],
):
    with col:
        st.markdown(f"""<div class="stat-card">
            <div class="stat-number {css}">{num}</div>
            <div class="stat-label">{label}</div>
        </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

if not filtered:
    st.info("No log files match the current filter / search at this location.")
    st.stop()


# ─────────────────────────────────────────────
# FILE LIST — st.dataframe (single widget, native sort + row selection)
# ─────────────────────────────────────────────

# Default sort: newest first.
sorted_list = sorted(filtered, key=lambda f: f.get("ts") or f["modified"], reverse=True)

total_pages = max(1, (len(sorted_list) + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE)

col_hdr, col_pg = st.columns([6, 1])
with col_hdr:
    st.markdown(
        f'<div class="section-header">Log Files ({len(filtered)})</div>',
        unsafe_allow_html=True,
    )
with col_pg:
    if total_pages > 1:
        page = st.number_input(
            "Page", min_value=1, max_value=total_pages, value=1, step=1,
            label_visibility="collapsed",
        ) - 1
    else:
        page = 0

if total_pages > 1:
    st.caption(f"Page {page + 1} of {total_pages}  ·  {TABLE_PAGE_SIZE} files per page")

page_files = sorted_list[page * TABLE_PAGE_SIZE : (page + 1) * TABLE_PAGE_SIZE]

# PN = the first folder component of the navigated path (the model folder).
pn = current_path.split("/")[0] if current_path else server["name"]

_df = pd.DataFrame([
    {
        "Time":    (_f.get("ts") or _f["modified"]).strftime("%Y-%m-%d %H:%M:%S"),
        "Server":  server["name"],
        "PN":      pn,
        "SN":      _f["sn"],
        "Station": _f.get("station", ""),
        "Result":  _RESULT_LABEL.get(_f["status"], "➖ UNKNOWN"),
    }
    for _f in page_files
])

_sel_event = st.dataframe(
    _df,
    use_container_width=True,
    height=560,
    hide_index=True,
    selection_mode="multi-row",
    on_select="rerun",
    column_config={
        "Time":    st.column_config.TextColumn("Time",    width="medium"),
        "SN":      st.column_config.TextColumn("SN",      width="medium"),
        "Station": st.column_config.TextColumn("Station", width="small"),
        "Result":  st.column_config.TextColumn("Result",  width="small"),
        "Server":  st.column_config.TextColumn("Server",  width="small"),
        "PN":      st.column_config.TextColumn("PN",      width="small"),
    },
)

_sel_rows    = _sel_event.selection.rows
selected_files = [page_files[i] for i in _sel_rows]

if len(_sel_rows) == 1:
    if st.button("👁 View file", use_container_width=False):
        st.session_state["selected_file_info"] = page_files[_sel_rows[0]]
        st.rerun()

# ─────────────────────────────────────────────
# ACTION BAR — appears when rows are checked
# ─────────────────────────────────────────────
if selected_files:
    st.markdown("---")
    ac1, ac2, _ = st.columns([1.5, 2.5, 5])
    if len(selected_files) == 1:
        sf = selected_files[0]
        with ac1:
            try:
                raw = Path(sf["path_str"]).read_bytes()
                st.download_button(
                    "⬇️ Download", raw, sf["name"],
                    mime="text/plain", use_container_width=True,
                )
            except OSError as e:
                st.error(f"Cannot read file: {e}")
    with ac2:
        if len(selected_files) > 1:
            zip_data = build_zip_bytes(selected_files)
            st.download_button(
                f"⬇️ Download {len(selected_files)} files as ZIP",
                zip_data, "logs.zip", "application/zip",
                use_container_width=True,
            )


# ─────────────────────────────────────────────
# AUTO-REFRESH WATCHDOG
# ─────────────────────────────────────────────
if st.session_state.get("auto_refresh"):
    _ar_interval = st.session_state.get("refresh_secs", 30)

    @st.fragment(run_every=_ar_interval)
    def _auto_refresh_watchdog():
        _wr_server = get_current_server()
        _wr_path   = st.session_state.get("current_path", "")
        _wr_root   = Path(_wr_server["path"])
        _wr_loc    = _wr_root / _wr_path if _wr_path else _wr_root
        _wr_excl   = frozenset(_wr_server.get("exclude_folders") or set())
        for _wr_deep in (False, True):
            _scan_cache().pop((_wr_server["name"], str(_wr_loc), _wr_deep, _wr_excl), None)
        st.rerun(scope="app")

    _auto_refresh_watchdog()
