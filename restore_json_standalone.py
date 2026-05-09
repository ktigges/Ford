"""
Standalone script to restore a JSON backup into the Ford Lightning DB.

Usage:
  python restore_json_standalone.py /path/to/lightning_backup_YYYYMMDD_HHMMSS.json

If no path is provided, the script restores the newest JSON file in backups/.
"""

import os
import sys
import time

import config
import db
import backup


BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def _latest_json_backup() -> str | None:
    if not os.path.isdir(BACKUP_DIR):
        return None
    candidates = [
        os.path.join(BACKUP_DIR, name)
        for name in os.listdir(BACKUP_DIR)
        if name.endswith(".json")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def main() -> int:
    if len(sys.argv) > 2:
        print("Usage: python restore_json_standalone.py [backup_file.json]")
        return 2

    filepath = sys.argv[1] if len(sys.argv) == 2 else _latest_json_backup()
    if not filepath:
        print("No JSON backup found in backups/ and no file argument provided.")
        return 1

    filepath = os.path.abspath(filepath)
    if not os.path.isfile(filepath):
        print(f"Backup file not found: {filepath}")
        return 1

    config.load()
    db.init_pool()
    start = time.time()

    def _progress(table: str, processed: int, total: int, restored: int) -> None:
        if processed == 0:
            print(f"[{table}] starting ({total} rows)", flush=True)
            return
        print(
            f"[{table}] {processed}/{total} processed, {restored} inserted",
            flush=True,
        )

    try:
        summary = backup.restore_json(filepath, progress_cb=_progress, progress_every=250)
    finally:
        db.close_pool()

    total = sum(summary.values())
    elapsed = time.time() - start
    print(f"JSON restore complete from: {filepath}")
    print(f"Total rows restored: {total}")
    print(f"Elapsed: {elapsed:.1f}s")
    for table, count in summary.items():
        print(f"  {table}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
