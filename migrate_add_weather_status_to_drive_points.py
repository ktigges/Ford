#!/usr/bin/env python3
"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""

import logging

import config
import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def migrate() -> bool:
    """Add weather fetch status/error columns to drive_points."""
    config.load()
    db.init_pool()
    try:
        db.execute(
            """
            ALTER TABLE drive_points
            ADD COLUMN IF NOT EXISTS weather_fetch_status TEXT,
            ADD COLUMN IF NOT EXISTS weather_fetch_error TEXT;
            """
        )
        log.info("Added columns: weather_fetch_status, weather_fetch_error")
        return True
    except Exception as exc:
        log.error("Migration failed: %s", exc)
        return False
    finally:
        db.close_pool()


if __name__ == "__main__":
    ok = migrate()
    raise SystemExit(0 if ok else 1)
