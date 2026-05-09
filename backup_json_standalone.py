"""
Standalone script to export all tables to a portable JSON backup for Ford Lightning DB.

- Exports all tables (including credentials, decrypted for portability)
- Output file is placed in backups/ directory as lightning_backup_<timestamp>.json
- Usage: python backup_json_standalone.py

Author: Copilot
"""

import os
import sys
import json
from datetime import datetime, timezone
from decimal import Decimal

# Import your project modules
import config
import crypto
import db

# Tables in dependency order
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
]

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

def _json_encoder(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    return str(obj)

def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"lightning_backup_{ts}.json"
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
        # Decrypt sensitive fields for portability
        if table == "oauth_credentials":
            for row in rows:
                if row.get("client_secret"):
                    row["client_secret"] = crypto.decrypt(row["client_secret"])
        data[table] = rows
        print(f"{table}: {len(rows)} rows")

    with open(filepath, "w") as f:
        json.dump(data, f, default=_json_encoder, indent=2)

    size = os.path.getsize(filepath)
    print(f"JSON backup created: {filename} ({size} bytes)")
    print(f"Location: {filepath}")

if __name__ == "__main__":
    main()
