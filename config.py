import json
import re
import sys
from pathlib import Path
from datetime import date

# When bundled by PyInstaller, __file__ is inside a temp _MEIPASS dir.
# Use the exe's directory instead so db/cache land next to the executable.
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
DB_PATH           = BASE_DIR / "log_index.db"
CACHE_DIR         = BASE_DIR / "file_cache"
CACHE_DIR.mkdir(exist_ok=True)

LOG_EXTENSIONS    = {".txt", ".log"}
TABLE_PAGE_SIZE   = 100
LOG_PAGE_LINES    = 500
DOWNLOAD_BYTE_CAP = 100 * 1024 * 1024   # 100 MB

AUTO_SCAN_INTERVAL_SECS      = 300   # seconds between incremental scan rounds
AUTO_SCAN_INITIAL_DELAY_SECS = 60    # startup delay before first auto-scan

_REQUIRED_KEYS = {"name", "path", "username", "password"}

DEFAULT_SERVERS: list[dict] = [
    {"name": "Main", "path": r"\\10.94.94.20\store",
     "username": "test", "password": "qcitest", "exclude_folders": []},
]


def load_servers() -> list[dict]:
    cfg = BASE_DIR / "servers.json"
    if not cfg.exists():
        return DEFAULT_SERVERS
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_SERVERS
    if not isinstance(data, list) or not data:
        return DEFAULT_SERVERS
    cleaned = []
    for entry in data:
        if not isinstance(entry, dict) or not _REQUIRED_KEYS.issubset(entry):
            continue
        raw_hide = entry.get("hide_file_patterns", [])
        cleaned.append({
            "name":              entry["name"],
            "path":              entry["path"],
            "username":          entry["username"],
            "password":          entry["password"],
            "exclude_folders":   {str(x).lower() for x in entry.get("exclude_folders", [])},
            "hide_file_patterns": [str(x) for x in raw_hide] if isinstance(raw_hide, list) else [],
        })
    return cleaned or DEFAULT_SERVERS


SERVERS: list[dict] = load_servers()

# Track servers.json mtime so we can hot-reload without restarting the exe.
_servers_cfg_mtime: float = 0.0
try:
    _cfg_path = BASE_DIR / "servers.json"
    _servers_cfg_mtime = _cfg_path.stat().st_mtime if _cfg_path.exists() else 0.0
except OSError:
    pass


def reload_if_changed() -> bool:
    """Reload servers.json if the file has been modified. Mutates SERVERS in place
    so all existing references (including main.py's imported name) see the update.
    Returns True if config was reloaded, False otherwise."""
    global _servers_cfg_mtime
    cfg = BASE_DIR / "servers.json"
    try:
        mtime = cfg.stat().st_mtime if cfg.exists() else 0.0
    except OSError:
        return False
    if mtime == _servers_cfg_mtime:
        return False
    _servers_cfg_mtime = mtime
    new = load_servers()
    SERVERS.clear()
    SERVERS.extend(new)
    return True


def get_server(name: str) -> dict | None:
    for s in SERVERS:
        if s["name"] == name:
            return s
    return None
