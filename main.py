"""FastAPI log viewer — live SMB file browser, no database."""

import asyncio
import concurrent.futures
import fnmatch
import html as _html
import io
import re
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlencode, quote

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import cache as file_cache
import config as _cfg
from config import (
    SERVERS, get_server, TABLE_PAGE_SIZE, LOG_PAGE_LINES, DOWNLOAD_BYTE_CAP,
)
from scanner import (
    mount_share, mount_share_verbose, force_remount, scan_folder, list_folder_live,
    _date_from_text, detect_status_from_path, _mounted, _mount_errors,
)
from datetime import date as _date

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Log Viewer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# key: (server, top, folder)  value: {state, files, count, started}
_scan_cache: dict[tuple, dict] = {}


@app.middleware("http")
async def _hot_reload_servers(request: Request, call_next):
    """Reload servers.json on every request if the file changed on disk.
    Clears stale SMB mount cache so changed credentials are re-authenticated."""
    old_state = {srv["path"]: (srv["username"], srv["password"]) for srv in SERVERS}
    changed = _cfg.reload_if_changed()
    if changed:
        new_paths = {srv["path"] for srv in SERVERS}
        for path, creds in old_state.items():
            new_srv = next((s for s in SERVERS if s["path"] == path), None)
            # Clear mount if path removed or credentials changed
            if new_srv is None or (new_srv["username"], new_srv["password"]) != creds:
                _mounted.discard(path)
                _mount_errors.pop(path, None)
        # Also clear mounts for paths not in old config (new server added mid-session)
        for path in new_paths - old_state.keys():
            _mounted.discard(path)
            _mount_errors.pop(path, None)
    return await call_next(request)

_grep_pool = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="grep")


# ── Jinja2 globals / filters ──────────────────────────────────────────────────
def _fmt_size(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b /= 1024
    return f"{b:.1f} GB"


def _fmt_modified(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


templates.env.globals["urlencode"] = quote
templates.env.globals["quote"]     = quote


def _hl(text: str, query: str) -> str:
    """HTML-escape text then wrap every query occurrence in <mark>."""
    escaped = _html.escape(str(text))
    if not query:
        return escaped
    pattern = re.escape(_html.escape(query))
    return re.sub(f"({pattern})", r"<mark>\1</mark>", escaped, flags=re.IGNORECASE)


templates.env.filters["hl"] = _hl


# ── Live top-folder cache (60 s TTL) ──────────────────────────────────────────
_live_folder_cache: dict[str, tuple[float, list[str]]] = {}
_LIVE_CACHE_TTL = 60.0


def _live_top_folders(server: dict) -> list[str]:
    name = server["name"]
    now  = time.monotonic()
    if name in _live_folder_cache:
        ts, cached = _live_folder_cache[name]
        if now - ts < _LIVE_CACHE_TTL:
            return cached
    try:
        mount_share(server["path"], server["username"], server["password"])
        root = Path(server["path"])
        excl = server.get("exclude_folders") or set()
        folders = sorted(
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in excl
        )
    except OSError:
        folders = []
    _live_folder_cache[name] = (now, folders)
    return folders


# ── Path helpers ───────────────────────────────────────────────────────────────
def _server_info_from_path(path: str) -> dict:
    """Extract server_name, top_folder, folder from a full UNC file path."""
    for srv in SERVERS:
        srv_path = srv["path"].rstrip("\\")
        if path.lower().startswith(srv_path.lower()):
            rest  = path[len(srv_path):].lstrip("\\")
            parts = rest.replace("/", "\\").split("\\")
            top    = parts[0] if parts else ""
            folder = "\\".join(parts[1:-1]) if len(parts) > 2 else "."
            return {"server": srv["name"], "top_folder": top, "folder": folder}
    return {"server": "Unknown", "top_folder": "", "folder": "."}


def _parse_file_meta(p: Path) -> dict:
    """Parse SN, TS, status, date, station from a file path."""
    stem      = p.stem
    stem_lower = stem.lower()
    uscore    = stem.find("_")
    if uscore != -1:
        sn     = stem[:uscore]
        ts_raw = stem_lower[uscore + 1:][:14]
    else:
        sn     = stem
        ts_raw = ""
    try:
        file_ts = datetime.strptime(ts_raw, "%Y%m%d%H%M%S") if len(ts_raw) == 14 else None
    except ValueError:
        file_ts = None

    lower_parts = [part.lower() for part in p.parts]
    if "bft" in lower_parts:
        station = "BFT"
    elif "fqa" in lower_parts:
        station = "FQA"
    else:
        station = "SIMPLE_FIXTURE"

    return {
        "sn":      sn,
        "ts":      file_ts.isoformat(sep=" ", timespec="seconds") if file_ts else None,
        "status":  detect_status_from_path(p),
        "date":    str(_date_from_text(p.name) or ""),
        "station": station,
    }


# ── Shared helpers ─────────────────────────────────────────────────────────────
def _server_context(server_name: str, top_folder: str = "") -> dict:
    if server_name:
        srv = get_server(server_name)
        top_folders = _live_top_folders(srv) if srv else []
    else:
        top_folders = []
    return {
        "servers":         SERVERS,
        "current_server":  server_name,
        "current_top":     top_folder,
        "top_folders":     top_folders,
        "folders_indexed": bool(top_folders),
    }


def _ensure_mount(server: dict) -> tuple[bool, str]:
    """Returns (success, error_message)."""
    ok, msg = mount_share_verbose(server["path"], server["username"], server["password"])
    return ok, msg


def _enrich(rows) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["modified_fmt"] = _fmt_modified(d.get("modified"))
        d["size_fmt"]     = _fmt_size(d.get("size") or 0)
        out.append(d)
    return out


def _breadcrumbs(server: str, top: str, folder: str) -> list[dict]:
    if not folder or folder == ".":
        return []
    parts = folder.replace("/", "\\").split("\\")
    crumbs = []
    for i, p in enumerate(parts):
        path = "\\".join(parts[: i + 1])
        crumbs.append({
            "name": p,
            "url":  "/browse?" + urlencode({"server": server, "top": top, "folder": path}),
        })
    return crumbs


def _parent_url(server: str, top: str, folder: str) -> str:
    if not folder or folder == "." or "\\" not in folder:
        return "/browse?" + urlencode({"server": server, "top": top})
    parent = folder.rsplit("\\", 1)[0]
    return "/browse?" + urlencode({"server": server, "top": top, "folder": parent})


def _make_sort_helpers(request: Request, base_params: dict):
    current_col = request.query_params.get("sort_col", "modified")
    current_asc = request.query_params.get("sort_asc", "0") == "1"
    icons = {"asc": " ▲", "desc": " ▼", "none": ""}

    def sort_url(col: str) -> str:
        if col == current_col:
            asc = "1" if not current_asc else "0"
        else:
            asc = "0"
        params = {**base_params, "sort_col": col, "sort_asc": asc, "page": "1"}
        return "?" + urlencode(params)

    def sort_icon(col: str) -> str:
        if col != current_col:
            return icons["none"]
        return icons["asc"] if current_asc else icons["desc"]

    return sort_url, sort_icon, current_col, current_asc


def _normalize_scan_result(f: dict) -> dict:
    """Convert scan_folder() output (datetime objects) to browse-compatible dict (strings/floats)."""
    modified = f.get("modified")
    if isinstance(modified, datetime):
        modified = modified.timestamp()
    d_val  = f.get("date")
    ts_val = f.get("ts")
    return {
        "path":    f.get("path_str", f.get("path", "")),
        "name":    f["name"],
        "folder":  f.get("folder", "."),
        "sn":      f.get("sn", ""),
        "ts":      ts_val.isoformat(sep=" ", timespec="seconds") if isinstance(ts_val, datetime) else (str(ts_val) if ts_val else None),
        "station": f.get("station", "SIMPLE_FIXTURE"),
        "status":  f.get("status", "UNKNOWN"),
        "date":    d_val.isoformat() if isinstance(d_val, _date) else (str(d_val) if d_val else None),
        "modified": modified,
        "size":    f.get("size", 0),
    }


def _run_scan(key: tuple, scan_id: str, folder_path: str, excl: set) -> None:
    entry0 = _scan_cache.get(key)
    if not entry0 or entry0.get("id") != scan_id:
        return  # stale — a newer scan already took over
    started = entry0["started"]

    def _progress(_dirs: int, files: int) -> None:
        e = _scan_cache.get(key)
        if e and e.get("id") == scan_id:
            e["count"] = files

    try:
        raw_files = scan_folder(folder_path, excl, progress=_progress, timeout=300)
        normalized = [_normalize_scan_result(f) for f in raw_files]
        if _scan_cache.get(key, {}).get("id") == scan_id:
            _scan_cache[key] = {"state": "done", "files": normalized, "count": len(normalized), "started": started, "id": scan_id}
    except Exception:
        if _scan_cache.get(key, {}).get("id") == scan_id:
            _scan_cache[key] = {"state": "error", "files": [], "count": 0, "started": started, "id": scan_id}


def _file_date(f: dict) -> str:
    """Return the effective date string for filtering — prefers the parsed 'date'
    field but falls back to the filesystem modification time so that files whose
    filenames contain no parseable date are still filtered correctly."""
    d = f.get("date") or ""
    if not d:
        mod = f.get("modified")
        if mod is not None:
            d = datetime.fromtimestamp(float(mod)).strftime("%Y-%m-%d")
    return d


def _filter_sort_paginate(
    files: list[dict],
    status: str,
    sn: str,
    station: str,
    date_from: str,
    date_to: str,
    sort_col: str,
    sort_asc: bool,
    page: int,
    page_size: int,
) -> tuple[list[dict], int]:
    rows = files
    if status:
        rows = [f for f in rows if f.get("status") == status]
    if sn:
        sns = [s.lower() for s in re.split(r"[\s,]+", sn) if s.strip()]
        rows = [f for f in rows if any(s in (f.get("sn") or "").lower() for s in sns)]
    if station:
        rows = [f for f in rows if f.get("station") == station]
    if date_from:
        rows = [f for f in rows if _file_date(f) >= date_from]
    if date_to:
        rows = [f for f in rows if _file_date(f) <= date_to]

    safe_cols = {"modified", "name", "sn", "status", "date", "ts", "size"}
    col = sort_col if sort_col in safe_cols else "modified"
    rows.sort(key=lambda r: (r.get(col) or ""), reverse=not sort_asc)

    total  = len(rows)
    offset = (page - 1) * page_size
    return rows[offset: offset + page_size], total


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    if SERVERS:
        return RedirectResponse(f"/browse?server={quote(SERVERS[0]['name'])}")
    return RedirectResponse("/admin")


@app.get("/browse", response_class=HTMLResponse)
async def browse(
    request: Request,
    server: str = "",
    top: str = "",
    folder: str = "",
    status: str = "",
    station: str = "",
    sn: str = "",
    date_from: str = "",
    date_to: str = "",
    hide: str = "",
    sort_col: str = "modified",
    sort_asc: str = "0",
    page: int = 1,
    page_size: int = TABLE_PAGE_SIZE,
    recursive: int = 0,
    content_q: str = "",
    force_scan: int = 0,
):
    _VALID_SIZES = {100, 200, 300, 500, 1000}
    page_size = page_size if page_size in _VALID_SIZES else TABLE_PAGE_SIZE

    if not server and SERVERS:
        server = SERVERS[0]["name"]

    ctx = _server_context(server, top)

    if not top:
        return templates.TemplateResponse(request, "browse.html", context={
            **ctx,
            "subfolders":   [],
            "files":        [],
            "filters":      {},
            "total_count":  0,
            "total_pages":  0,
            "page":         1,
            "page_size":    page_size,
            "breadcrumbs":  [],
            "parent_url":   "",
            "scanning":     False,
        })

    base_params = {k: v for k, v in {
        "server": server, "top": top, "folder": folder,
        "status": status, "station": station, "sn": sn,
        "date_from": date_from, "date_to": date_to,
        "hide": hide, "page_size": str(page_size),
        "recursive": "1" if recursive else "",
    }.items() if v}

    sort_url_fn, sort_icon_fn, s_col, s_asc = _make_sort_helpers(request, base_params)

    srv = get_server(server)
    raw_subfolders: list[str] = []
    all_files: list[dict]     = []
    mount_error: str          = ""

    if srv:
        _ok, _err = _ensure_mount(srv)
        if _ok:
            excl        = srv.get("exclude_folders") or set()
            folder_path = str(Path(srv["path"]) / top / folder) if folder else str(Path(srv["path"]) / top)
            rel_folder  = folder or "."
            if recursive:
                key = (server, top, folder or ".")
                entry = _scan_cache.get(key)
                if entry and (force_scan or time.time() - entry["started"] > 300):
                    _scan_cache.pop(key, None)
                    entry = None
                if entry is None:
                    scan_id = str(uuid.uuid4())
                    _scan_cache[key] = {"state": "scanning", "files": [], "count": 0, "started": time.time(), "id": scan_id}
                    asyncio.get_running_loop().run_in_executor(None, _run_scan, key, scan_id, folder_path, excl)
                if entry is None or entry["state"] == "scanning":
                    def _scan_page_url(p: int) -> str:
                        return "?" + urlencode({**base_params, "sort_col": s_col, "sort_asc": "1" if s_asc else "0", "page": p})
                    return templates.TemplateResponse(request, "browse.html", context={
                        **ctx,
                        "current_folder": folder,
                        "breadcrumbs":    _breadcrumbs(server, top, folder),
                        "parent_url":     _parent_url(server, top, folder),
                        "subfolders":     [],
                        "files":          [],
                        "filters": {
                            "status": status, "station": station, "sn": sn,
                            "date_from": date_from, "date_to": date_to,
                            "hide": hide, "content_q": content_q,
                        },
                        "total_count":  0,
                        "total_pages":  1,
                        "page":         1,
                        "page_size":    page_size,
                        "sort_url":     sort_url_fn,
                        "sort_icon":    sort_icon_fn,
                        "page_url":     _scan_page_url,
                        "scanning":     True,
                        "recursive":    True,
                        "mount_error":  "",
                    })
                if entry["state"] == "error":
                    mount_error = "Scan failed or timed out. Please try again."
                    _scan_cache.pop(key, None)
                else:
                    all_files = entry["files"]
            else:
                raw_subfolders, all_files = await asyncio.to_thread(
                    list_folder_live, folder_path, rel_folder, excl
                )
        else:
            mount_error = _err

    # Combined SN + content search — apply metadata filters then hand off to grep
    if content_q.strip() and top:
        filtered_all, _ = _filter_sort_paginate(
            all_files, status, sn, station, date_from, date_to,
            "modified", False, 1, 999_999,
        )
        _server_hide = (srv.get("hide_file_patterns") or []) if srv else []
        _ui_hide     = [p.strip() for p in hide.split(",") if p.strip()]
        _all_hide    = _server_hide + _ui_hide
        if _all_hide:
            filtered_all = [
                f for f in filtered_all
                if not any(fnmatch.fnmatch(f["name"].lower(), p.lower()) for p in _all_hide)
            ]
        paths = [f["path"] for f in filtered_all]
        return templates.TemplateResponse(request, "grep.html", context={
            **ctx,
            "query":         content_q.strip(),
            "paths":         paths,
            "file_count":    len(paths),
            "folder_mode":   False,
            "folder_server": server,
            "folder_top":    top,
            "folder_path":   folder,
            "from_browse_filters": {
                "sn": sn, "status": status, "station": station,
                "date_from": date_from, "date_to": date_to,
            },
        })

    rows, total = _filter_sort_paginate(
        all_files, status, sn, station, date_from, date_to,
        s_col, s_asc, page, page_size,
    )

    files = _enrich(rows)

    _server_hide = (srv.get("hide_file_patterns") or []) if srv else []
    _ui_hide     = [p.strip() for p in hide.split(",") if p.strip()]
    _all_hide    = _server_hide + _ui_hide
    if _all_hide:
        files = [
            f for f in files
            if not any(fnmatch.fnmatch(f["name"].lower(), p.lower()) for p in _all_hide)
        ]

    total_pages = max(1, (total + page_size - 1) // page_size)

    subfolders = [
        {
            "name": sf,
            "path": (folder + "\\" + sf) if (folder and folder != ".") else sf,
        }
        for sf in raw_subfolders
    ]

    def page_url(p: int) -> str:
        return "?" + urlencode({
            **base_params, "sort_col": s_col,
            "sort_asc": "1" if s_asc else "0", "page": p,
        })

    return templates.TemplateResponse(request, "browse.html", context={
        **ctx,
        "current_folder": folder,
        "breadcrumbs":    _breadcrumbs(server, top, folder),
        "parent_url":     _parent_url(server, top, folder),
        "subfolders":     subfolders,
        "files":          files,
        "filters": {
            "status": status, "station": station, "sn": sn,
            "date_from": date_from, "date_to": date_to, "hide": hide,
            "content_q": content_q,
        },
        "total_count":  total,
        "total_pages":  total_pages,
        "page":         page,
        "page_size":    page_size,
        "sort_url":     sort_url_fn,
        "sort_icon":    sort_icon_fn,
        "page_url":     page_url,
        "scanning":     False,
        "recursive":    bool(recursive),
        "mount_error":  mount_error,
    })


@app.get("/view", response_class=HTMLResponse)
async def view_file(request: Request, path: str, page: int = 1, hl: str = ""):
    p = Path(path)
    if not p.exists():
        return HTMLResponse("File not found.", status_code=404)

    try:
        file_cache.cache_file(path)
    except OSError as e:
        return HTMLResponse(f"Cannot read file: {e}", status_code=500)

    stat = p.stat()
    info = _server_info_from_path(path)
    meta = _parse_file_meta(p)
    file_dict = {
        "path":         path,
        "name":         p.name,
        "server":       info["server"],
        "top_folder":   info["top_folder"],
        "folder":       info["folder"],
        **meta,
        "modified":     stat.st_mtime,
        "size":         stat.st_size,
        "modified_fmt": _fmt_modified(stat.st_mtime),
        "size_fmt":     _fmt_size(stat.st_size),
    }

    start       = (page - 1) * LOG_PAGE_LINES
    page_lines, total_lines = file_cache.read_page_lines(path, start, LOG_PAGE_LINES)
    end         = start + len(page_lines)
    total_pages = max(1, (total_lines + LOG_PAGE_LINES - 1) // LOG_PAGE_LINES)
    page        = max(1, min(page, total_pages))
    content     = "\n".join(page_lines)

    server     = info["server"]
    top        = info["top_folder"]
    folder_rel = info["folder"]
    back_params = {"server": server, "top": top}
    if folder_rel and folder_rel != ".":
        back_params["folder"] = folder_rel
    back_url = "/browse?" + urlencode(back_params)

    ctx = _server_context(server, top)
    return templates.TemplateResponse(request, "viewer.html", context={
        **ctx,
        "file":        file_dict,
        "content":     content,
        "hl":          hl,
        "page":        page,
        "total_pages": total_pages,
        "total_lines": total_lines,
        "line_start":  start + 1,
        "line_end":    min(end, total_lines),
        "back_url":    back_url,
    })


@app.get("/download")
async def download_file(path: str):
    try:
        raw = Path(path).read_bytes()[:DOWNLOAD_BYTE_CAP]
    except OSError as e:
        return HTMLResponse(f"Cannot read file: {e}", status_code=500)
    filename = Path(path).name
    return StreamingResponse(
        io.BytesIO(raw),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_zip(paths: list[str]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: dict[str, int] = {}
        for p in paths:
            pp = Path(p)
            try:
                raw = pp.read_bytes()[:DOWNLOAD_BYTE_CAP]
            except OSError:
                continue
            # Folder structure: top/STATUS/SN/filename
            parts = pp.parts
            top = parts[1] if len(parts) > 1 else "logs"
            status = detect_status_from_path(pp)
            stem = pp.stem
            sn = stem[:stem.index("_")] if "_" in stem else stem
            arcname = f"{top}/{status}/{sn}/{pp.name}"
            # Deduplicate arcnames
            if arcname in seen:
                seen[arcname] += 1
                arcname = f"{top}/{status}/{sn}/{pp.stem}_{seen[arcname]}{pp.suffix}"
            else:
                seen[arcname] = 0
            zf.writestr(arcname, raw)
    buf.seek(0)
    return buf


_SAFE_FNAME = re.compile(r'[^a-zA-Z0-9_\-]')

def _zip_filename(paths: list[str], query: str = "") -> str:
    if not paths:
        return "logs.zip"
    sns: list[str] = []
    statuses: list[str] = []
    for p in paths:
        pp = Path(p)
        stem = pp.stem
        idx = stem.find("_")
        sns.append(stem[:idx] if idx != -1 else stem)
        statuses.append(detect_status_from_path(pp))
    seen: set[str] = set()
    unique_sns = [s for s in sns if not (s in seen or seen.add(s))]  # type: ignore[func-returns-value]
    raw_sn = "-".join(unique_sns[:3])
    if len(unique_sns) > 3:
        raw_sn += f"-plus{len(unique_sns) - 3}more"
    sn_part = _SAFE_FNAME.sub("_", raw_sn) or "files"
    status_part = "FAIL" if "FAIL" in statuses else "PASS" if "PASS" in statuses else "UNKNOWN"
    if query:
        q_part = _SAFE_FNAME.sub("_", query.strip())[:40].strip("_") or "search"
        return f"{q_part}_{sn_part}_{status_part}.zip"
    return f"{sn_part}_{status_part}.zip"


def _zip_response(buf: io.BytesIO, filename: str = "logs.zip") -> StreamingResponse:
    safe_name = filename.replace('"', "_")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.get("/download-zip")
async def download_zip(path: list[str] = Query(default=[])):
    return _zip_response(_build_zip(path), _zip_filename(path))


@app.post("/download-zip")
async def download_zip_post(request: Request):
    form = await request.form()
    paths = list(form.getlist("path"))
    query = str(form.get("q", ""))
    return _zip_response(_build_zip(paths), _zip_filename(paths, query))


@app.get("/tail")
async def tail_file(path: str):
    """SSE endpoint — streams last 200 lines then follows new content."""
    return StreamingResponse(
        _tail_generator(path),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _tail_generator(path: str) -> AsyncGenerator[str, None]:
    POLL    = 1.0
    INITIAL = 200
    try:
        raw  = Path(path).read_bytes()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        for line in (lines[-INITIAL:] if len(lines) > INITIAL else lines):
            yield f"data: {line}\n\n"
        current_size = len(raw)
    except OSError as exc:
        yield f"event: error\ndata: {exc}\n\n"
        return

    while True:
        await asyncio.sleep(POLL)
        try:
            size = Path(path).stat().st_size
            if size > current_size:
                with open(path, "rb") as fh:
                    fh.seek(current_size)
                    chunk = fh.read(size - current_size)
                current_size = size
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    yield f"data: {line}\n\n"
            elif size < current_size:
                yield "event: reset\ndata: file truncated\n\n"
                current_size = 0
            else:
                yield ": keepalive\n\n"
        except OSError as exc:
            yield f"event: error\ndata: {exc}\n\n"
            return


@app.get("/browse/rows", response_class=HTMLResponse)
async def browse_rows(
    request: Request,
    server: str,
    top: str,
    folder: str = "",
    status: str = "",
    station: str = "",
    sn: str = "",
    date_from: str = "",
    date_to: str = "",
    sort_col: str = "modified",
    sort_asc: str = "0",
    page: int = 1,
    page_size: int = TABLE_PAGE_SIZE,
):
    """Partial — returns only <tbody> rows for auto-refresh."""
    _VALID_SIZES = {100, 200, 300, 500, 1000}
    page_size = page_size if page_size in _VALID_SIZES else TABLE_PAGE_SIZE

    srv = get_server(server)
    all_files: list[dict] = []
    if srv and _ensure_mount(srv)[0]:
        excl        = srv.get("exclude_folders") or set()
        folder_path = str(Path(srv["path"]) / top / folder) if folder else str(Path(srv["path"]) / top)
        _, all_files = await asyncio.to_thread(
            list_folder_live, folder_path, folder or ".", excl
        )

    s_col = sort_col if sort_col in {"modified","name","sn","status","date","ts","size"} else "modified"
    rows, total = _filter_sort_paginate(
        all_files, status, sn, station, date_from, date_to,
        s_col, (sort_asc == "1"), page, page_size,
    )
    files       = _enrich(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    return templates.TemplateResponse(request, "browse_rows.html", context={
        "files":       files,
        "total_count": total,
        "total_pages": total_pages,
        "page":        page,
    })


@app.get("/api/scan-status")
async def scan_status(server: str, top: str, folder: str = ""):
    key = (server, top, folder or ".")
    entry = _scan_cache.get(key)
    if not entry:
        return JSONResponse({"state": "idle", "files": 0})
    return JSONResponse({"state": entry["state"], "files": entry["count"]})


@app.get("/api/auto-scan-status")
async def auto_scan_status():
    from fastapi.responses import JSONResponse
    return JSONResponse([
        {"server": srv["name"], "scanning": False, "last_scanned": None, "last_scanned_fmt": None}
        for srv in SERVERS
    ])


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", server: str = "", group: int = 0):
    if not server and SERVERS:
        server = SERVERS[0]["name"]
    ctx = _server_context(server)
    return templates.TemplateResponse(request, "search.html", context={
        **ctx,
        "query":   q,
        "group":   group,
        "results": [],
        "grouped": [],
    })


def _grep_one(path: str, q_lower: str) -> dict | None:
    """Search one file for q_lower. Streams line-by-line to avoid loading the
    full file into memory; stops collecting after 30 matches. Runs in thread pool."""
    MAX_MATCHES = 30
    matches: list[dict] = []

    cp = file_cache.get_cache_path(path)
    try:
        if cp.exists():
            # Fast path: cached file — stream from local disk, no full load
            with open(cp, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if q_lower in line.lower():
                        matches.append({"n": i + 1, "text": line.rstrip("\n\r")})
                        if len(matches) >= MAX_MATCHES:
                            break
        else:
            # Uncached: read raw bytes from SMB, decode once, search line-by-line
            raw = Path(path).read_bytes()
            if len(raw) > DOWNLOAD_BYTE_CAP:
                raw = raw[:DOWNLOAD_BYTE_CAP]
            text = ""
            for enc in ("utf-8", "cp932", "latin-1"):
                try:
                    text = raw.decode(enc); break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                text = raw.decode("latin-1")
            for i, line in enumerate(text.splitlines()):
                if q_lower in line.lower():
                    matches.append({"n": i + 1, "text": line})
                    if len(matches) >= MAX_MATCHES:
                        break
    except OSError:
        return None

    if not matches:
        return None

    p = Path(path)
    try:
        stat = p.stat()   # single SMB call — also confirms file still exists
    except OSError:
        return None
    d = {
        "path":     path,
        "name":     p.name,
        "modified": stat.st_mtime,
        "size":     stat.st_size,
        **_parse_file_meta(p),
    }
    d["matching_lines"] = matches
    d["modified_fmt"]   = _fmt_modified(d.get("modified"))
    d["size_fmt"]       = _fmt_size(d.get("size") or 0)
    return d


async def _grep_stream_gen(q: str, paths: list[str], request=None):
    import json as _json
    if not q or not paths:
        yield f"event: done\ndata: {_json.dumps({'matched': 0, 'searched': 0})}\n\n"
        return

    q_lower = q.lower()
    total   = len(paths)
    # Send a progress event on the 1st file, then every BATCH completions, then the last.
    # This caps SSE traffic to ~100 progress events regardless of file count.
    BATCH   = max(1, min(total // 100, 50))

    result_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def _run_one(p: str):
        r = await loop.run_in_executor(_grep_pool, _grep_one, p, q_lower)
        await result_queue.put((p, r))

    tasks = [asyncio.create_task(_run_one(p)) for p in paths]

    matched   = 0
    last_name = ""
    try:
        for searched in range(1, total + 1):
            while True:
                try:
                    path, result = await asyncio.wait_for(result_queue.get(), timeout=0.5)
                    break
                except asyncio.TimeoutError:
                    if request and await request.is_disconnected():
                        return
            last_name = Path(path).name

            # Throttle progress events: first, every BATCH, and last
            if searched == 1 or searched % BATCH == 0 or searched == total:
                yield f"event: progress\ndata: {_json.dumps({'current': searched, 'total': total, 'filename': last_name})}\n\n"

            if result:
                matched += 1
                yield f"event: match\ndata: {_json.dumps(result, default=str)}\n\n"
    finally:
        for t in tasks:
            t.cancel()

    yield f"event: done\ndata: {_json.dumps({'matched': matched, 'searched': total})}\n\n"



def _extract_paths(form) -> list[str]:
    """Accept paths either as a single JSON field (paths_json) or as repeated 'paths' fields."""
    import json as _json
    pj = str(form.get("paths_json", "")).strip()
    if pj:
        try:
            return [str(p) for p in _json.loads(pj)]
        except Exception:
            pass
    return list(form.getlist("paths"))


@app.post("/grep-stream")
async def grep_stream_post(request: Request):
    """SSE stream for explicit path list."""
    form  = await request.form()
    q     = str(form.get("q", "")).strip()
    paths = _extract_paths(form)
    return StreamingResponse(
        _grep_stream_gen(q, paths, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/grep", response_class=HTMLResponse)
async def grep_files(request: Request):
    """Render the grep page; results streamed via /grep-stream."""
    form  = await request.form()
    q     = str(form.get("q", "")).strip()
    paths = _extract_paths(form)
    ctx   = _server_context("")
    return templates.TemplateResponse(request, "grep.html", context={
        **ctx,
        "query":      q,
        "paths":      paths,
        "file_count": len(paths),
    })


@app.get("/api/file-content", response_class=HTMLResponse)
async def file_content_api(path: str, hl: str = ""):
    """Terminal-style HTML fragment for inline display in grep results."""
    MAX_LINES = 3000
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_grep_pool, file_cache.cache_file, path)
    except OSError as e:
        return HTMLResponse(
            f'<div class="term-loading" style="color:#f87171">Error: {_html.escape(str(e))}</div>'
        )

    page_lines, total_lines = await loop.run_in_executor(
        _grep_pool, file_cache.read_page_lines, path, 0, MAX_LINES
    )

    hl_lower = hl.lower()

    def _render_line(raw: str) -> str:
        escaped  = _html.escape(raw)
        is_match = hl_lower and hl_lower in raw.lower()
        cls      = " tl-match" if is_match else ""
        if is_match:
            pattern = re.escape(_html.escape(hl))
            escaped = re.sub(f"({pattern})", r"<mark>\1</mark>", escaped, flags=re.IGNORECASE)
        return f'<span class="tl{cls}">{escaped}</span>'

    body     = "".join(_render_line(l) for l in page_lines)
    more_html = ""
    if total_lines > MAX_LINES:
        more_html = (
            f'<p class="term-more">Showing first {MAX_LINES:,} of {total_lines:,} lines — '
            f'<a href="/view?path={quote(path)}&hl={quote(hl)}">open full file ↗</a></p>'
        )
    return HTMLResponse(f'<pre class="term-pre">{body}</pre>{more_html}')


@app.get("/api/test-connection")
async def api_test_connection():
    """Test SMB mount for every configured server and return results as JSON."""
    results = []
    for srv in SERVERS:
        unc = srv["path"]
        already = unc in _mounted
        if already:
            ok, msg = True, "Already mounted"
        else:
            ok, msg = await asyncio.to_thread(
                mount_share_verbose, unc, srv.get("username", ""), srv.get("password", "")
            )
        # Try listing root to verify access even if mount claims success
        folder_count = 0
        list_error   = ""
        if ok:
            try:
                import os as _os
                folder_count = sum(1 for e in _os.scandir(unc) if e.is_dir())
            except Exception as e:
                ok         = False
                list_error = str(e)
                msg        = f"Mount OK but listing failed: {list_error}"
        results.append({
            "server":       srv["name"],
            "path":         unc,
            "ok":           ok,
            "message":      msg,
            "folder_count": folder_count,
        })
    return JSONResponse(results)


@app.post("/api/fix-connection")
async def api_fix_connection(request: Request):
    """Force-clear SMB sessions and remount for one server (by name)."""
    form        = await request.form()
    server_name = str(form.get("server", "")).strip()
    srv = get_server(server_name)
    if not srv:
        return JSONResponse({"ok": False, "message": "Server not found"}, status_code=404)
    ok, msg = await asyncio.to_thread(
        force_remount, srv["path"], srv.get("username", ""), srv.get("password", "")
    )
    folder_count = 0
    if ok:
        try:
            import os as _os
            folder_count = sum(1 for e in _os.scandir(srv["path"]) if e.is_dir())
        except Exception as e:
            ok  = False
            msg = f"Re-mounted but listing failed: {e}"
    return JSONResponse({"ok": ok, "message": msg, "folder_count": folder_count})


@app.get("/status", response_class=HTMLResponse)
async def server_status(request: Request):
    ctx = _server_context(SERVERS[0]["name"] if SERVERS else "")
    # Run connection tests for all servers
    conn_results = []
    for srv in SERVERS:
        unc      = srv["path"]
        already  = unc in _mounted
        cached_err = _mount_errors.get(unc, "")
        if already:
            ok, msg = True, "Connected"
        else:
            ok, msg = await asyncio.to_thread(
                mount_share_verbose, unc, srv.get("username", ""), srv.get("password", "")
            )
        folder_count = 0
        if ok:
            try:
                import os as _os
                folder_count = sum(1 for e in _os.scandir(unc) if e.is_dir())
            except Exception as e:
                ok  = False
                msg = f"Mount OK but listing failed: {e}"
        conn_results.append({
            "server":       srv["name"],
            "path":         unc,
            "ok":           ok,
            "message":      msg,
            "folder_count": folder_count,
        })
    return templates.TemplateResponse(request, "status.html", context={
        **ctx,
        "server_stats":       [],
        "scan_interval_mins": 0,
        "conn_results":       conn_results,
    })


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, message: str = ""):
    server = SERVERS[0]["name"] if SERVERS else ""
    ctx    = _server_context(server)
    return templates.TemplateResponse(request, "admin.html", context={
        **ctx,
        "stats":   {"total": 0, "indexed": 0, "cached": 0},
        "message": message,
    })
