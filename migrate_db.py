"""One-shot DB migration: backfill top_folder and normalise server names.

Run once:  python migrate_db.py
Safe to re-run — uses UPDATE ... WHERE to skip already-filled rows.
"""

import sqlite3
from config import DB_PATH, SERVERS

con = sqlite3.connect(str(DB_PATH))
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA synchronous=NORMAL")
con.execute("PRAGMA cache_size=-65536")

# ── 1. Normalise server names ─────────────────────────────────────────────────
#  Build a map: UNC prefix (lower) → config name
server_by_prefix: list[tuple[str, str]] = []
for s in SERVERS:
    prefix = s["path"].rstrip("\\/").lower()
    server_by_prefix.append((prefix, s["name"]))

old_names = [r[0] for r in con.execute("SELECT DISTINCT server FROM log_files").fetchall()]
for old in old_names:
    if not any(old == s["name"] for s in SERVERS):
        # figure out which config server this old name belongs to by path prefix
        sample = con.execute(
            "SELECT path FROM log_files WHERE server=? LIMIT 1", (old,)
        ).fetchone()
        if not sample:
            continue
        path_lower = sample[0].lower()
        matched = None
        for prefix, name in server_by_prefix:
            if path_lower.startswith(prefix):
                matched = name
                break
        if matched:
            print(f"  Renaming server '{old}' → '{matched}' …")
            con.execute("UPDATE log_files SET server=? WHERE server=?", (matched, old))
            con.commit()
            print(f"  Done.")
        else:
            print(f"  WARNING: could not match server '{old}' to any config entry (sample path: {sample[0][:80]})")

# ── 2. Backfill top_folder from path ─────────────────────────────────────────
for s in SERVERS:
    server_root = s["path"].rstrip("\\/")
    prefix      = server_root.lower()
    name        = s["name"]

    total = con.execute(
        "SELECT COUNT(*) FROM log_files WHERE server=? AND top_folder=''", (name,)
    ).fetchone()[0]
    if total == 0:
        print(f"  {name}: top_folder already populated, skipping.")
        continue

    print(f"  {name}: backfilling top_folder for {total:,} rows …")
    prefix_len = len(server_root) + 1  # +1 for the trailing backslash

    rows = con.execute(
        "SELECT path FROM log_files WHERE server=? AND top_folder=''", (name,)
    ).fetchall()

    batch = []
    for (path,) in rows:
        rest = path[prefix_len:]                # e.g. "33F0EMA08X0\bft\pass\..."
        top  = rest.split("\\")[0]              # first component
        if top:
            batch.append((top, path))

    CHUNK = 5000
    for i in range(0, len(batch), CHUNK):
        con.executemany(
            "UPDATE log_files SET top_folder=? WHERE path=?", batch[i:i+CHUNK]
        )
        con.commit()
        done = min(i + CHUNK, len(batch))
        print(f"    {done:,}/{len(batch):,}", end="\r", flush=True)

    print(f"\n  {name}: done.")

print("\nMigration complete.")
