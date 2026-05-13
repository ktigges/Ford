#!/usr/bin/env python3
"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""

import db
import config
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def migrate():
    """Add weather columns to drive_points table."""
    config.load()
    db.init_pool()
    
    try:
        # Check if columns already exist
        result = db.fetch_all("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'drive_points' 
            AND column_name IN ('weather_temp_c', 'weather_humidity_pct')
        """)
        
        if result and len(result) > 0:
            log.info("Weather columns already exist in drive_points, skipping migration")
            db.close_pool()
            return True
        
        log.info("Adding weather columns to drive_points table...")
        
        db.execute("""
            ALTER TABLE drive_points
            ADD COLUMN weather_temp_c REAL,
            ADD COLUMN weather_humidity_pct REAL,
            ADD COLUMN weather_pressure_hpa REAL,
            ADD COLUMN precipitation_mm REAL,
            ADD COLUMN wind_speed_avg_kmh REAL,
            ADD COLUMN wind_direction_avg_deg REAL,
            ADD COLUMN headwind_component_kmh REAL,
            ADD COLUMN tailwind_component_kmh REAL,
            ADD COLUMN sidewind_component_kmh REAL;
        """)
        
        log.info("✓ Successfully added weather columns to drive_points")
        log.info("  - weather_temp_c")
        log.info("  - weather_humidity_pct")
        log.info("  - weather_pressure_hpa")
        log.info("  - precipitation_mm")
        log.info("  - wind_speed_avg_kmh")
        log.info("  - wind_direction_avg_deg")
        log.info("  - headwind_component_kmh")
        log.info("  - tailwind_component_kmh")
        log.info("  - sidewind_component_kmh")
        
        db.close_pool()
        return True
        
    except Exception as exc:
        log.error(f"Migration failed: {exc}")
        db.close_pool()
        return False

if __name__ == "__main__":
    success = migrate()
    exit(0 if success else 1)
