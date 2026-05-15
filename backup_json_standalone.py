"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
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

_SENSITIVE_OAUTH_FIELDS = ("client_secret", "refresh_token", "access_token")

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
    config.load()
    db.init_pool()
    try:
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
                    for field in _SENSITIVE_OAUTH_FIELDS:
                        if row.get(field):
                            row[field] = crypto.decrypt(row[field])
            data[table] = rows
            print(f"{table}: {len(rows)} rows")

        with open(filepath, "w") as f:
            json.dump(data, f, default=_json_encoder, indent=2)

        size = os.path.getsize(filepath)
        print(f"JSON backup created: {filename} ({size} bytes)")
        print(f"Location: {filepath}")
    finally:
        db.close_pool()

if __name__ == "__main__":
    main()
