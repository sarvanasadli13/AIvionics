"""Back up the database, verify the copy, and report versions.

    python scripts/backup.py                       # into data/backups/
    python scripts/backup.py --out D:\backups
    python scripts/backup.py --restore <file>      # put one back
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config, db          # noqa: E402
from aivionics.admin import maintenance   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--out", default=str(config.DATA_DIR / "backups"))
    ap.add_argument("--restore", help="restore this backup over --db")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.restore:
        try:
            dest = maintenance.restore(args.restore, args.db,
                                       overwrite=args.force)
        except (FileExistsError, ValueError, FileNotFoundError) as exc:
            print(f"restore refused: {exc}")
            return 1
        print(f"restored {args.restore} -> {dest}")
        return 0

    con = db.connect(Path(args.db))
    dest = Path(args.out) / maintenance.default_backup_name()
    result = maintenance.backup(con, dest)
    print(result.summary())
    print()
    for line in maintenance.versions(con).lines():
        print("  " + line)
    con.close()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
