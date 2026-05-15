"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""


import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from psycopg2.extras import Json

import config
import crypto
import db

log = logging.getLogger("backup")


# Directory where backups are stored
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
# Directory where model files are stored
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

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
    "charging_sessions",
    "charging_history",
    "location_state",
    "tire_state",
    "door_state",
    "window_state",
    "brake_state",
    "security_state",
    "environment_state",
    "vehicle_configuration",
    "departure_schedule",
    "drives",
    "drive_points",
    "ev_stations",
    "ev_charger_connectors",
    "ev_sync_runs",
]

_SENSITIVE_OAUTH_FIELDS = ("client_secret", "refresh_token", "access_token")


def _adapt_value_for_insert(value):
    """Adapt Python values to PostgreSQL-friendly insert values.

    psycopg2 cannot bind native dict/list directly; JSON/JSONB columns must
    receive an adapted JSON object.
    """
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


def _ensure_backup_dir() -> str:
    """Create the backups directory if it doesn't exist and return its path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR


def _db_config() -> dict:
    """Return the database connection parameters from config.json."""
    return config.database()


def _running_db_container_name() -> str | None:
    """Return running DB container name when available, else None.

    Prefers DB_CONTAINER env var, defaulting to lightning-db.
    """
    container = (os.environ.get("DB_CONTAINER") or "lightning-db").strip()
    if not container:
        return None

    if shutil.which("docker") is None:
        return None

    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    running = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return container if container in running else None


# ═══════════════════════════════════════════════════════════════════
# SQL dump backup (pg_dump / psql)
# ═══════════════════════════════════════════════════════════════════


def backup_sql(label: str = "") -> str:
    """Create a full SQL dump of the database using pg_dump and copy model files."""
    _ensure_backup_dir()
    cfg = _db_config()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    filename = f"lightning_backup_{ts}{suffix}.sql"
    filepath = os.path.join(BACKUP_DIR, filename)

    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["password"]

    container = _running_db_container_name()
    if container:
        # Use pg_dump inside the running Postgres container to avoid client/server version mismatch.
        cmd = [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={cfg['password']}",
            container,
            "pg_dump",
            "-U",
            cfg["user"],
            "-d",
            cfg["name"],
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
        ]
        log.info("Running pg_dump in container '%s' -> %s", container, filename)
        with open(filepath, "w") as out_f:
            result = subprocess.run(
                cmd,
                env=env,
                stdout=out_f,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
    else:
        cmd = [
            "pg_dump",
            "-h", cfg["host"],
            "-p", str(cfg["port"]),
            "-U", cfg["user"],
            "-d", cfg["name"],
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "-f", filepath,
        ]

        log.info("Running host pg_dump -> %s", filename)
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        log.error("pg_dump failed: %s", result.stderr)
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")

    size = os.path.getsize(filepath)
    log.info("SQL backup created: %s (%d bytes)", filename, size)

    # Copy model files to backup dir with timestamp
    for model_file in ["energy_model.pkl", "energy_scaler.pkl", "energy_model_schema.json"]:
        src = os.path.join(MODEL_DIR, model_file)
        if os.path.exists(src):
            dst = os.path.join(BACKUP_DIR, f"{model_file}.{ts}")
            shutil.copy2(src, dst)
            log.info(f"Model file {model_file} copied to backup as {dst}")
        else:
            log.warning(f"Model file {model_file} not found in models/ directory")

    return filepath



def restore_sql(filepath: str) -> None:
    """Restore the database from a SQL dump file using psql and restore model files if present."""
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Backup file not found: {filepath}")

    cfg = _db_config()
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg["password"]

    container = _running_db_container_name()

    log.info("Restoring SQL backup from %s", os.path.basename(filepath))
    if container:
        cmd = [
            "docker",
            "exec",
            "-i",
            "-e",
            f"PGPASSWORD={cfg['password']}",
            container,
            "psql",
            "-U",
            cfg["user"],
            "-d",
            cfg["name"],
            "-v",
            "ON_ERROR_STOP=1",
        ]
        with open(filepath, "r") as in_f:
            result = subprocess.run(
                cmd,
                env=env,
                stdin=in_f,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
            )
    else:
        cmd = [
            "psql",
            "-h", cfg["host"],
            "-p", str(cfg["port"]),
            "-U", cfg["user"],
            "-d", cfg["name"],
            "-v", "ON_ERROR_STOP=1",
            "-f", filepath,
        ]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        log.error("psql restore failed: %s", result.stderr)
        raise RuntimeError(f"psql restore failed: {result.stderr.strip()}")

    log.info("SQL restore complete")

    # Restore model files if present in backup dir (matching timestamp)
    # Try to infer timestamp from backup filename
    base = os.path.basename(filepath)
    ts = None
    if base.startswith("lightning_backup_"):
        try:
            ts = base.split("_")[2].split(".")[0]
        except Exception:
            pass
    if ts:
        for model_file in ["energy_model.pkl", "energy_scaler.pkl", "energy_model_schema.json"]:
            backup_model = os.path.join(BACKUP_DIR, f"{model_file}.{ts}")
            if os.path.exists(backup_model):
                dst = os.path.join(MODEL_DIR, model_file)
                shutil.copy2(backup_model, dst)
                log.info(f"Restored model file {model_file} from backup {backup_model}")
            else:
                log.warning(f"Model file {model_file}.{ts} not found in backup dir")


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
                for field in _SENSITIVE_OAUTH_FIELDS:
                    if row.get(field):
                        row[field] = crypto.decrypt(row[field])
        data[table] = rows
        log.info("  %s: %d rows", table, len(rows))

    with open(filepath, "w") as f:
        json.dump(data, f, cls=_JSONEncoder, indent=2)

    size = os.path.getsize(filepath)
    log.info("JSON backup created: %s (%d bytes)", filename, size)
    return filepath


def restore_json(
    filepath: str,
    progress_cb: Callable[[str, int, int, int], None] | None = None,
    progress_every: int = 500,
) -> dict:
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
        total_rows = len(rows)
        if progress_cb:
            progress_cb(table, 0, total_rows, restored)

        for idx, row in enumerate(rows, start=1):
            # Re-encrypt sensitive fields with this host's key
            if table == "oauth_credentials":
                for field in _SENSITIVE_OAUTH_FIELDS:
                    if row.get(field):
                        row[field] = crypto.encrypt(row[field])

            columns = list(row.keys())
            placeholders = ", ".join(["%s"] * len(columns))
            col_list = ", ".join(columns)

            # Use ON CONFLICT DO NOTHING to be safe with existing data
            sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            values = [_adapt_value_for_insert(row[c]) for c in columns]

            try:
                db.execute(sql, tuple(values))
                restored += 1
            except Exception as exc:
                log.warning("  %s: row insert failed: %s", table, exc)

            if progress_cb and (idx % max(1, progress_every) == 0 or idx == total_rows):
                progress_cb(table, idx, total_rows, restored)

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
