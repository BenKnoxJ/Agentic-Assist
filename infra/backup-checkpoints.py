#!/usr/bin/env python3
"""Back up the conversation database. Run by gojo-backup.timer, daily.

Uses sqlite3's online backup API rather than cp: the service holds the file
open in WAL mode, and a filesystem copy taken mid-write is a corrupt backup
that looks fine until the day it is needed. The backup API takes a
consistent snapshot without stopping the service.

Retention is written here, at creation, per GOJO-MASTER 9.1: keep the most
recent KEEP copies, delete the rest. No unbounded growth, no separate
cleanup job to forget.

⚠ Same-disk backups protect against corruption and accidental deletion
(a stray rm, a bad migration, git clean in the wrong directory), not
against losing the VPS. VPS.md records that honestly.

Stdlib only - runs under system python3, no venv needed.
"""

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

SOURCE = Path("/home/ccuser/Agentic-Assist/checkpoints/gojo.sqlite")
DEST_DIR = Path("/home/ccuser/backups/gojo")
KEEP = 14


def main() -> int:
    if not SOURCE.exists():
        # Nothing to back up is not an error - a fresh box has no state yet.
        print(f"nothing to back up: {SOURCE} does not exist")
        return 0

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = DEST_DIR / f"gojo-{stamp}.sqlite"

    src = sqlite3.connect(SOURCE)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    # Verify the snapshot is a readable database before trusting it.
    check = sqlite3.connect(dest)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        print(f"backup FAILED integrity check: {ok}", file=sys.stderr)
        dest.unlink(missing_ok=True)
        return 1

    backups = sorted(DEST_DIR.glob("gojo-*.sqlite"))
    for stale in backups[:-KEEP]:
        stale.unlink()

    print(f"backed up {SOURCE.stat().st_size} bytes to {dest}; keeping {min(len(backups), KEEP)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
