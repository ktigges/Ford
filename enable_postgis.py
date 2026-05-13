#!/usr/bin/env python3
"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""

import sys
import logging

import config
import db

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(message)s')


def enable_postgis():
    """Enable PostGIS extension and create spatial index if needed."""
    try:
        config.load()
        db.init_pool()
        
        log.info("Enabling PostGIS extension...")
        db.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        log.info("PostGIS extension enabled")
        
        # Verify the extension is available
        result = db.fetch_one("SELECT extversion FROM pg_extension WHERE extname = 'postgis';")
        if result:
            log.info("PostGIS version: %s", result.get('extversion'))
        else:
            log.warning("PostGIS extension not found in pg_extension table")
        
        # Check if the spatial index exists, create if not
        log.info("Checking spatial index...")
        has_index = db.fetch_one(
            """
            SELECT indexname FROM pg_indexes 
            WHERE indexname = 'idx_ev_stations_location'
            """
        )
        
        if has_index:
            log.info("Spatial index already exists")
        else:
            log.info("Creating spatial index...")
            db.execute("""
                CREATE INDEX idx_ev_stations_location ON ev_stations USING GIST (
                    ST_Point(longitude, latitude)
                );
            """)
            log.info("Spatial index created")
        
        log.info("\nPostGIS setup complete!")
        log.info("Trip planner charger lookups will now use efficient spatial queries.")
        return True
        
    except Exception as e:
        log.error("Failed to enable PostGIS: %s", e)
        import traceback
        traceback.print_exc()
        return False
    finally:
        db.close_pool()


if __name__ == "__main__":
    success = enable_postgis()
    sys.exit(0 if success else 1)
