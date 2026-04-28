"""Backup and restore module for Ford Lightning telemetry database.

Provides two backup strategies:
1. **SQL dump** — Uses pg_dump/psql for full-fidelity PostgreSQL backup & restore.
2. **JSON export** — Python-native export of all tables to a portable JSON file
   that can be restored without pg_dump/psql installed.

Backups are stored in the `backups/` directory (relative to the project root).

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.2.1
Date:        2026-04-28
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from decimal import Decimal

import config
import crypto
import db

log = logging.getLogger("backup")

# Directory where backups are stored
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

# Tables in dependency order (parents before children).
# Restore must follow this order; backup can be any order.
TABLES_ORDERED = [
    "app_config",
    "garage",
    "oauth_credentials",
    "polling_config",
    "collector_status",
    "telemetry",
    "vehicle_state",
    "battery_state",
    "charging_state",
    "location_state",
    "tire_state",
    "door_state",
    "window_state",
    "brake_state",
    "security_state",
    "environment_state",
    "vehicle_configuration",
    "departure_schedule",
]


def _ensure_backup_dir() -> str:
    """Create the backups directory if it doesn't exist and return its path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR


def _db_config() -> dict:
    """Return the database connection parameters from config.json."""
    return config.database()


# ═══════════════════════════════════════════════════════════════════
# SQL dump backup (pg_dump / psql)
# ═══════════════════════════════════════════════════════════════════

def backup_sql(label: str = "") -> str:
    """Create a full SQL dump of the database using pg_dump.

    Returns the path to the created .sql file.
    Raises RuntimeError if pg_dump is not available or fails.
    """
    _ensure_backup_dir()
    cfg = _db_config()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    filename = f"lightning_backup_{ts}{suffix}.sql"
    filepath = os.path.join(BACKUP_DIR, filename)

    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["password"]

    cmd = [
        "pg_dump",
        "-h", cfg["host"],
        "-p", str(cfg["port"]),
        "-U", cfg["user"],
        "-d", cfg["name"],
        "--no-owner",
        "--no-privileges",
        "-f", filepath,
    ]

    log.info("Running pg_dump → %s", filename)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        log.error("pg_dump failed: %s", result.stderr)
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")

    size = os.path.getsize(filepath)
    log.info("SQL backup created: %s (%d bytes)", filename, size)
    return filepath


def restore_sql(filepath: str) -> None:
    """Restore the database from a SQL dump file using psql.

    WARNING: This will overwrite existing data in conflicting rows.
    The dump uses CREATE-or-replace semantics from pg_dump.
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    cfg = _db_config()
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["password"]

    cmd = [
        "psql",
        "-h", cfg["host"],
        "-p", str(cfg["port"]),
        "-U", cfg["user"],
        "-d", cfg["name"],
        "-f", filepath,
    ]

    log.info("Restoring SQL backup from %s", os.path.basename(filepath))
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        log.error("psql restore failed: %s", result.stderr)
        raise RuntimeError(f"psql restore failed: {result.stderr.strip()}")

    log.info("SQL restore complete")


# ═══════════════════════════════════════════════════════════════════
# JSON export / import (portable, no pg_dump required)
# ═══════════════════════════════════════════════════════════════════

class _JSONEncoder(json.JSONEncoder):
    """Custom encoder that handles datetime, Decimal, and other DB types."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, bytes):
            return obj.hex()
        return super().default(obj)


def backup_json(label: str = "") -> str:
    """Export all tables to a single JSON file.

    Returns the path to the created .json file.
    The file contains a dict mapping table names to lists of row dicts.
    """
    _ensure_backup_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    filename = f"lightning_backup_{ts}{suffix}.json"
    filepath = os.path.join(BACKUP_DIR, filename)

    data = {
        "_meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "format_version": "1.0",
            "tables": TABLES_ORDERED,
        }
    }

    for table in TABLES_ORDERED:
        rows = db.fetch_all(f"SELECT * FROM {table}")
        # Decrypt sensitive fields so backups are portable across hosts
        if table == "oauth_credentials":
            for row in rows:
                if row.get("client_secret"):
                    row["client_secret"] = crypto.decrypt(row["client_secret"])
        data[table] = rows
        log.info("  %s: %d rows", table, len(rows))

    with open(filepath, "w") as f:
        json.dump(data, f, cls=_JSONEncoder, indent=2)

    size = os.path.getsize(filepath)
    log.info("JSON backup created: %s (%d bytes)", filename, size)
    return filepath


def restore_json(filepath: str) -> dict:
    """Restore all tables from a JSON backup file.

    Uses INSERT ... ON CONFLICT DO NOTHING to avoid overwriting
    existing rows. Returns a summary dict of rows restored per table.

    Tables are restored in dependency order (parents first).
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    with open(filepath, "r") as f:
        data = json.load(f)

    summary = {}

    for table in TABLES_ORDERED:
        rows = data.get(table, [])
        if not rows:
            summary[table] = 0
            continue

        restored = 0
        for row in rows:
            # Re-encrypt sensitive fields with this host's key
            if table == "oauth_credentials" and row.get("client_secret"):
                row["client_secret"] = crypto.encrypt(row["client_secret"])

            columns = list(row.keys())
            placeholders = ", ".join(["%s"] * len(columns))
            col_list = ", ".join(columns)

            # Use ON CONFLICT DO NOTHING to be safe with existing data
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            values = [row[c] for c in columns]

            try:
                db.execute(sql, tuple(values))
                restored += 1
            except Exception as exc:
                log.warning("  %s: row insert failed: %s", table, exc)

        summary[table] = restored
        log.info("  %s: %d/%d rows restored", table, restored, len(rows))

    log.info("JSON restore complete")
    return summary


# ═══════════════════════════════════════════════════════════════════
# Backup listing & management
# ═══════════════════════════════════════════════════════════════════

def list_backups() -> list[dict]:
    """List all backup files in the backups directory.

    Returns a list of dicts with filename, size, created timestamp, and type.
    Sorted by creation time (newest first).
    """
    _ensure_backup_dir()
    backups = []

    for fname in os.listdir(BACKUP_DIR):
        if not (fname.endswith(".sql") or fname.endswith(".json")):
            continue
        fpath = os.path.join(BACKUP_DIR, fname)
        stat = os.stat(fpath)
        backups.append({
            "filename": fname,
            "path": fpath,
            "size": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            "type": "sql" if fname.endswith(".sql") else "json",
        })

    backups.sort(key=lambda b: b["created"], reverse=True)
    return backups


def delete_backup(filename: str) -> bool:
    """Delete a backup file by filename. Returns True if deleted."""
    # Prevent path traversal
    safe_name = os.path.basename(filename)
    fpath = os.path.join(BACKUP_DIR, safe_name)
    if os.path.isfile(fpath):
        os.remove(fpath)
        log.info("Deleted backup: %s", safe_name)
        return True
    return False


def _format_size(size_bytes: int) -> str:
    """Format file size in human-readable units."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
