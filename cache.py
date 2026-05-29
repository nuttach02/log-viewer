"""Local file pre-cache — sha256-named copies in file_cache/."""

import hashlib
from pathlib import Path

from config import CACHE_DIR, DOWNLOAD_BYTE_CAP

_ENCODING_TRIES = ("utf-8", "cp932", "latin-1")


def _cache_path(src_path: str) -> Path:
    h = hashlib.sha256(src_path.encode()).hexdigest()
    return CACHE_DIR / h


def is_cached(src_path: str) -> bool:
    return _cache_path(src_path).exists()


def read_cached_or_direct(src_path: str) -> str:
    cp = _cache_path(src_path)
    if cp.exists():
        return cp.read_text(encoding="utf-8", errors="replace")
    return _read_direct(src_path)


def cache_file(src_path: str) -> str:
    """Read src_path (up to DOWNLOAD_BYTE_CAP), write to cache, return text."""
    cp = _cache_path(src_path)
    if cp.exists():
        return cp.read_text(encoding="utf-8", errors="replace")
    text = _read_direct(src_path)
    cp.write_text(text, encoding="utf-8")
    return text


def _read_direct(src_path: str) -> str:
    raw = Path(src_path).read_bytes()
    if len(raw) > DOWNLOAD_BYTE_CAP:
        raw = raw[:DOWNLOAD_BYTE_CAP]
    for enc in _ENCODING_TRIES:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1")


def read_page_lines(src_path: str, start: int, count: int) -> tuple[list[str], int]:
    """Return (page_lines, total_line_count) streaming line-by-line — no full load into memory.

    Ensures the file is cached locally first, then streams through the cache file
    so only `count` lines are held in memory at once.
    """
    cp = _cache_path(src_path)
    if not cp.exists():
        cache_file(src_path)  # populate cache on first access

    page: list[str] = []
    total = 0
    with open(cp, "r", encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh):
            total += 1
            if start <= i < start + count:
                page.append(raw.rstrip("\n\r"))
    return page, total


def get_cache_path(src_path: str) -> Path:
    """Return the cache path (may not exist yet)."""
    return _cache_path(src_path)
