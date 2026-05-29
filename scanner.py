"""SMB mounting, status detection, date parsing, recursive file scanning."""

import os
import re
import subprocess
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, date
from typing import Callable

from config import LOG_EXTENSIONS

# ── Precompiled regexes ────────────────────────────────────────────────────────
_NAME_PASS_RE    = re.compile(r'\bpass(ed)?\b|_ok_|_pass_')
_NAME_FAIL_RE    = re.compile(r'\bfail(ed)?\b|_ng_|_fail_|_error_')
_CONTENT_FAIL_RE = re.compile(r'\bfail(ed)?\b|\berror\b|\bng\b')
_CONTENT_PASS_RE = re.compile(r'\bpass(ed)?\b|result.*ok\b|\btest.*ok\b')
_PATH_PASS_PARTS = frozenset(("pass", "passed", "ok"))
_PATH_FAIL_PARTS = frozenset(("fail", "failed", "error", "ng"))

_DATE_RE = re.compile(
    r'(?<!\d)(\d{4})[.\-_]?(\d{2})[.\-_]?(\d{2})(?!\d)'
    r'|(?<!\d)(\d{2})[.\-_](\d{2})[.\-_](\d{4})(?!\d)'
)

# ── Network mount ──────────────────────────────────────────────────────────────
_mounted: set[str] = set()
_mount_errors: dict[str, str] = {}   # unc_path → last error message


def _clear_smb_sessions(server_host: str, unc_path: str) -> None:
    """Aggressively remove all SMB sessions to server_host."""
    cmds = [
        ["net", "use", unc_path, "/delete", "/yes"],
        ["net", "use", f"\\\\{server_host}\\IPC$", "/delete", "/yes"],
        ["net", "use", f"\\\\{server_host}\\*", "/delete", "/yes"],
        ["cmdkey", f"/delete:{server_host}"],
        ["cmdkey", f"/delete:TERMSRV/{server_host}"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            pass


def mount_share_verbose(unc_path: str, username: str, password: str) -> tuple[bool, str]:
    """Like mount_share() but always returns (success, message) — used for diagnostics."""
    system = platform.system()
    if system == "Windows":
        try:
            r = subprocess.run(
                ["net", "use", unc_path, f"/user:{username}", password, "/persistent:no"],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            msg = "Connection timed out (15 s) — server unreachable or firewall blocking SMB"
            _mount_errors[unc_path] = msg
            return False, msg
        except Exception as exc:
            msg = f"Mount error: {exc}"
            _mount_errors[unc_path] = msg
            return False, msg
        if r.returncode == 0:
            _mounted.add(unc_path)
            _mount_errors.pop(unc_path, None)
            return True, "Connected"
        combined = (r.stderr + r.stdout).strip()
        if "1219" in combined:
            server_host = unc_path.lstrip("\\").split("\\")[0]
            _clear_smb_sessions(server_host, unc_path)
            time.sleep(0.5)  # give Windows time to release the session before remounting
            subprocess.run(
                ["cmdkey", f"/add:{server_host}", f"/user:{username}", f"/pass:{password}"],
                capture_output=True, timeout=10,
            )
            try:
                r2 = subprocess.run(
                    ["net", "use", unc_path, f"/user:{username}", password, "/persistent:no"],
                    capture_output=True, text=True, timeout=15,
                )
            except subprocess.TimeoutExpired:
                msg = "Connection timed out on re-mount (15 s)"
                _mount_errors[unc_path] = msg
                return False, msg
            except Exception as exc:
                msg = f"Re-mount error: {exc}"
                _mount_errors[unc_path] = msg
                return False, msg
            if r2.returncode == 0:
                _mounted.add(unc_path)
                _mount_errors.pop(unc_path, None)
                return True, "Connected (re-mounted after credential conflict)"
            combined = (r2.stderr + r2.stdout).strip()
        msg = combined or "Unknown error"
        _mount_errors[unc_path] = msg
        return False, msg
    elif system in ("Linux", "Darwin"):
        mp = Path("/mnt/logviewer_share")
        mp.mkdir(parents=True, exist_ok=True)
        smb = unc_path.replace("\\\\", "//").replace("\\", "/")
        try:
            r = subprocess.run(
                ["sudo", "mount", "-t", "cifs", smb, str(mp),
                 "-o", f"username={username},password={password},vers=3.0"],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            msg = "Connection timed out (15 s)"
            _mount_errors[unc_path] = msg
            return False, msg
        except Exception as exc:
            msg = f"Mount error: {exc}"
            _mount_errors[unc_path] = msg
            return False, msg
        if r.returncode == 0:
            _mounted.add(unc_path)
            _mount_errors.pop(unc_path, None)
            return True, "Connected"
        msg = (r.stderr + r.stdout).strip() or "Unknown error"
        _mount_errors[unc_path] = msg
        return False, msg
    return False, "Unsupported platform"


def force_remount(unc_path: str, username: str, password: str) -> tuple[bool, str]:
    """Remove from mount cache, clear all SMB sessions, then remount fresh."""
    _mounted.discard(unc_path)
    _mount_errors.pop(unc_path, None)
    if platform.system() == "Windows":
        server_host = unc_path.lstrip("\\").split("\\")[0]
        _clear_smb_sessions(server_host, unc_path)
        import time as _time; _time.sleep(0.5)
    return mount_share_verbose(unc_path, username, password)


def mount_share(unc_path: str, username: str, password: str) -> bool:
    if unc_path in _mounted:
        return True
    ok, _ = mount_share_verbose(unc_path, username, password)
    return ok


# ── Status detection ───────────────────────────────────────────────────────────
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


# ── Date extraction ────────────────────────────────────────────────────────────
def _date_from_text(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        if m.group(1):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
    except ValueError:
        return None


# ── Recursive scanner ──────────────────────────────────────────────────────────
def scan_folder(
    scan_root_str: str,
    exclude_folders: set[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
    max_workers: int = 8,
    since_ts: float | None = None,
    timeout: float = 300,
) -> list[dict]:
    """
    Parallel recursive scan of scan_root_str for .log/.txt files.
    Uses a thread pool so multiple SMB directory listings are in flight at once.
    progress(dirs_visited, files_found) called every 25 directories.
    """
    scan_root_norm = scan_root_str.rstrip("\\/")
    prefix_len     = len(scan_root_norm) + 1
    excl           = exclude_folders or set()
    pass_re        = _NAME_PASS_RE.search
    fail_re        = _NAME_FAIL_RE.search
    pp             = _PATH_PASS_PARTS
    fp             = _PATH_FAIL_PARTS
    fromts         = datetime.fromtimestamp

    all_files: list[dict] = []
    lock         = threading.Lock()
    dirs_visited = [0]
    in_flight    = [1]   # root directory counts as 1
    done_event   = threading.Event()

    def _scan_one(current: str) -> None:
        if current == scan_root_norm:
            rel_folder         = "."
            folder_status_hint = None
            folder_date        = None
            folder_station     = "SIMPLE_FIXTURE"
        else:
            rel_folder  = current[prefix_len:]
            lower_parts = [p for p in rel_folder.replace("/", "\\").lower().split("\\") if p]
            folder_status_hint = None
            for part in lower_parts:
                if part in pp:
                    folder_status_hint = "PASS"; break
                if part in fp:
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

        local_files: list[dict] = []
        new_dirs: list[str]     = []

        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in excl:
                                new_dirs.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        name_lower = entry.name.lower()
                        if not name_lower.endswith((".log", ".txt")):
                            continue
                        try:
                            stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue

                        if since_ts is not None and stat.st_mtime <= since_ts:
                            continue

                        stem_lower = name_lower[:-4]
                        if folder_status_hint is not None:
                            status_val = folder_status_hint
                        elif pass_re(stem_lower):
                            status_val = "PASS"
                        elif fail_re(stem_lower):
                            status_val = "FAIL"
                        else:
                            status_val = "UNKNOWN"

                        file_date = _date_from_text(entry.name) or folder_date
                        rel_name  = entry.name if rel_folder == "." else f"{rel_folder}\\{entry.name}"

                        uscore = stem_lower.find("_")
                        if uscore != -1:
                            sn     = entry.name[:uscore]
                            ts_raw = stem_lower[uscore + 1:][:14]
                        else:
                            sn     = entry.name[:-4]
                            ts_raw = ""
                        try:
                            file_ts = datetime.strptime(ts_raw, "%Y%m%d%H%M%S") if len(ts_raw) == 14 else None
                        except ValueError:
                            file_ts = None

                        local_files.append({
                            "path_str": entry.path,
                            "name":     entry.name,
                            "relative": rel_name,
                            "folder":   rel_folder,
                            "status":   status_val,
                            "size":     stat.st_size,
                            "modified": fromts(stat.st_mtime),
                            "date":     file_date,
                            "sn":       sn,
                            "ts":       file_ts,
                            "station":  folder_station,
                        })
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass

        # Increment for child dirs before decrementing self so in_flight never
        # hits 0 before all children are accounted for.
        with lock:
            all_files.extend(local_files)
            dirs_visited[0] += 1
            dv = dirs_visited[0]
            af = len(all_files)
            in_flight[0] += len(new_dirs) - 1
            finished = in_flight[0] == 0

        for d in new_dirs:
            executor.submit(_scan_one, d)

        if dv % 25 == 0 and progress:
            progress(dv, af)
        if finished:
            done_event.set()

    executor = ThreadPoolExecutor(max_workers=max_workers)
    executor.submit(_scan_one, scan_root_norm)
    timed_out = not done_event.wait(timeout=timeout)
    executor.shutdown(wait=False)
    if timed_out:
        raise TimeoutError(f"scan_folder timed out after {timeout}s")
    return all_files


def list_folder_live(
    folder_path: str,
    rel_folder: str = ".",
    exclude_folders: set[str] | None = None,
) -> tuple[list[str], list[dict]]:
    """
    Non-recursive listing of folder_path (like `ls`).
    Returns (sorted_subfolders, files_with_metadata).
    rel_folder is the relative path from the top_folder root.
    """
    excl = exclude_folders or set()
    subfolders: list[str] = []
    files: list[dict] = []

    lower_parts = [p.lower() for p in Path(folder_path).parts if p not in ("\\", "/", "")]

    folder_status_hint: str | None = None
    for part in lower_parts:
        if part in _PATH_PASS_PARTS:
            folder_status_hint = "PASS"
            break
        if part in _PATH_FAIL_PARTS:
            folder_status_hint = "FAIL"
            break

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

    pass_re = _NAME_PASS_RE.search
    fail_re = _NAME_FAIL_RE.search
    fromts  = datetime.fromtimestamp

    try:
        with os.scandir(folder_path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() not in excl:
                            subfolders.append(entry.name)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name_lower = entry.name.lower()
                    if not name_lower.endswith((".log", ".txt")):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue

                    stem_lower = name_lower[:-4]
                    if folder_status_hint is not None:
                        status_val = folder_status_hint
                    elif pass_re(stem_lower):
                        status_val = "PASS"
                    elif fail_re(stem_lower):
                        status_val = "FAIL"
                    else:
                        status_val = "UNKNOWN"

                    file_date = _date_from_text(entry.name) or folder_date

                    uscore = stem_lower.find("_")
                    if uscore != -1:
                        sn     = entry.name[:uscore]
                        ts_raw = stem_lower[uscore + 1:][:14]
                    else:
                        sn     = entry.name[:-4]
                        ts_raw = ""
                    try:
                        file_ts = datetime.strptime(ts_raw, "%Y%m%d%H%M%S") if len(ts_raw) == 14 else None
                    except ValueError:
                        file_ts = None

                    files.append({
                        "path":     entry.path,
                        "name":     entry.name,
                        "folder":   rel_folder,
                        "sn":       sn,
                        "ts":       file_ts.isoformat(sep=" ", timespec="seconds") if file_ts else None,
                        "station":  folder_station,
                        "status":   status_val,
                        "date":     file_date.isoformat() if file_date else None,
                        "modified": stat.st_mtime,
                        "size":     stat.st_size,
                    })
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass

    return sorted(subfolders), files


def scan_server_all(server: dict, progress: Callable[[str, int, int], None] | None = None) -> dict[str, list[dict]]:
    """Scan every top-level folder on a server. Returns {top_folder: [files]}."""
    server_root = Path(server["path"])
    excl        = server.get("exclude_folders") or set()
    results: dict[str, list[dict]] = {}

    top_dirs = sorted(
        [d for d in server_root.iterdir()
         if d.is_dir() and not d.name.startswith(".") and d.name.lower() not in excl],
        key=lambda d: d.name.lower(),
    )
    for d in top_dirs:
        def _prog(dirs, found, _name=d.name):
            if progress:
                progress(_name, dirs, found)
        results[d.name] = scan_folder(str(d), excl, _prog)

    return results
