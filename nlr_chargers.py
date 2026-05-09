"""NIR (National Renewable Energy Laboratory) Alt Fuel Stations API integration.

Fetches EV charging station data for route planning and AI model training.
- Manual import via /chargers/sync route
- Scheduled delta syncs (optional)
- Stores connector inventory normalized for ML feature engineering

Author:      Kevin Tigges
Description: Ford Lightning EV Tool - Charger Integration
Version:     0.6.0.0
Date:        2026-05-09
"""

import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import db
import config

log = logging.getLogger("nlr_chargers")
nlr_log = logging.getLogger("nlr_api")

# NLR API endpoint for EV stations
NLR_API_BASE = "https://developer.nrel.gov/api/alt-fuel-stations/v1.json"

# Fuel type code for electric vehicles
FUEL_TYPE_ELEC = "ELEC"

# US state codes (for validation)
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"
}


def _ensure_charger_tables() -> None:
    """Create charger tables/indexes if they are missing.

    This protects manual imports when startup migrations were skipped or failed.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ev_stations (
            id BIGSERIAL PRIMARY KEY,
            nlr_station_id BIGINT NOT NULL UNIQUE,
            station_name TEXT NOT NULL,
            street_address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            country TEXT DEFAULT 'US',
            latitude DOUBLE PRECISION NOT NULL,
            longitude DOUBLE PRECISION NOT NULL,
            status_code TEXT,
            fuel_type_code TEXT DEFAULT 'ELEC',
            access_code TEXT,
            access_detail TEXT,
            owner_type_code TEXT,
            facility_type TEXT,
            network_name TEXT,
            updated_at TIMESTAMPTZ DEFAULT now(),
            nlr_updated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            raw_data JSONB
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ev_charger_connectors (
            id BIGSERIAL PRIMARY KEY,
            station_id BIGINT NOT NULL REFERENCES ev_stations(id) ON DELETE CASCADE,
            nlr_station_id BIGINT NOT NULL,
            connector_type TEXT NOT NULL,
            network TEXT,
            charging_level TEXT,
            power_kw REAL,
            port_count INTEGER,
            updated_at TIMESTAMPTZ DEFAULT now(),
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (station_id, connector_type, network)
        )
        """
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ev_sync_runs (
            id BIGSERIAL PRIMARY KEY,
            sync_type TEXT NOT NULL,
            state_filter TEXT,
            status TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            stations_imported INTEGER DEFAULT 0,
            stations_updated INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )

    db.execute("CREATE INDEX IF NOT EXISTS idx_ev_stations_state ON ev_stations (state) WHERE country = 'US'")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ev_stations_nlr_id ON ev_stations (nlr_station_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ev_connectors_station ON ev_charger_connectors (station_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ev_connectors_network ON ev_charger_connectors (network)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ev_sync_runs_status ON ev_sync_runs (status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ev_sync_runs_started ON ev_sync_runs (started_at DESC)")


def get_nlr_api_key() -> Optional[str]:
    """Retrieve stored NLR API key from app_config."""
    try:
        row = db.fetch_one("SELECT value FROM app_config WHERE key = 'nlr_api_key'")
        return row["value"] if row and row.get("value") else None
    except Exception as e:
        log.warning("Failed to fetch NLR API key: %s", e)
        return None


def set_nlr_api_key(api_key: str) -> bool:
    """Store NLR API key in app_config."""
    try:
        db.execute(
            """
            INSERT INTO app_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            ("nlr_api_key", api_key, "NLR (NREL) Alt Fuel Stations API key for EV charger data"),
        )
        log.info("NLR API key updated in app_config")
        return True
    except Exception as e:
        log.error("Failed to store NLR API key: %s", e)
        return False


def _nlr_get(
    fuel_type: str = FUEL_TYPE_ELEC,
    state: Optional[str] = None,
    limit: int | str = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """Fetch EV stations from NLR API with pagination.

    Args:
        fuel_type: Fuel type code (default: ELEC for electric)
        state: US state code (optional filter)
        limit: Records per page (or "all" if API supports it)
        offset: Result offset for pagination

    Returns:
        Parsed JSON response or empty dict on error
    """
    api_key = get_nlr_api_key()
    if not api_key:
        nlr_log.error("NLR API key not configured")
        raise RuntimeError("NLR API key not configured in app settings")

    params = {
        "api_key": api_key,
        "fuel_type": fuel_type,
        "limit": limit,
        "offset": offset,
        "status": "E",  # Operational status only
    }
    if state:
        params["state"] = state

    headers = {
        "Accept": "application/json",
        "User-Agent": "Ford-Lightning-EV/1.0",
    }

    try:
        nlr_log.debug("NLR GET %s params=%s", NLR_API_BASE, params)
        resp = requests.get(NLR_API_BASE, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        nlr_log.info("NLR response: %d total results, %d returned", 
                     data.get("total_results", 0), len(data.get("fuel_stations", [])))
        return data
    except requests.exceptions.RequestException as e:
        nlr_log.error("NLR API error: %s", e)
        raise


def _upsert_ev_station(station: Dict[str, Any]) -> Optional[int]:
    """Upsert a single EV station record. Returns station record ID or None."""
    try:
        nlr_station_id = station.get("id")
        if not nlr_station_id:
            log.warning("Station missing id field: %s", station.get("station_name"))
            return None

        row = db.execute_returning(
            """
            INSERT INTO ev_stations (
                nlr_station_id, station_name, street_address, city, state, zip,
                latitude, longitude, country, status_code, fuel_type_code,
                access_code, access_detail, owner_type_code, facility_type,
                network_name, updated_at, nlr_updated_at, raw_data
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
            ON CONFLICT (nlr_station_id) DO UPDATE SET
                station_name = EXCLUDED.station_name,
                street_address = EXCLUDED.street_address,
                city = EXCLUDED.city,
                state = EXCLUDED.state,
                zip = EXCLUDED.zip,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                status_code = EXCLUDED.status_code,
                access_code = EXCLUDED.access_code,
                access_detail = EXCLUDED.access_detail,
                owner_type_code = EXCLUDED.owner_type_code,
                facility_type = EXCLUDED.facility_type,
                network_name = EXCLUDED.network_name,
                updated_at = now(),
                nlr_updated_at = EXCLUDED.nlr_updated_at,
                raw_data = EXCLUDED.raw_data
            RETURNING id
            """,
            (
                nlr_station_id,
                station.get("station_name"),
                station.get("street_address"),
                station.get("city"),
                station.get("state"),
                station.get("zip"),
                station.get("latitude"),
                station.get("longitude"),
                station.get("country", "US"),
                station.get("status_code"),
                station.get("fuel_type_code"),
                station.get("access_code"),
                station.get("access_detail_code"),
                station.get("owner_type_code"),
                station.get("facility_type"),
                station.get("ev_network"),
                station.get("updated_at"),
                json.dumps(station),
            ),
        )
        return row["id"] if row else None
    except Exception as e:
        log.error("Failed to upsert EV station %s: %s", station.get("id"), e)
        return None


def _upsert_ev_connector(station_db_id: int, charging_unit: Dict[str, Any], 
                         station_nlr_id: int) -> bool:
    """Upsert connector inventory for a station. Returns success status."""
    try:
        connectors = charging_unit.get("connectors", {})
        network = charging_unit.get("network", "Unknown")
        charging_level = charging_unit.get("charging_level", "")

        for connector_type, connector_info in connectors.items():
            if not connector_info:
                continue

            port_count = connector_info.get("port_count", 0)
            power_kw = connector_info.get("power_kw")

            if port_count > 0 or power_kw:
                db.execute(
                    """
                    INSERT INTO ev_charger_connectors (
                        station_id, nlr_station_id, connector_type, network,
                        charging_level, power_kw, port_count, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (station_id, connector_type, network) DO UPDATE SET
                        charging_level = EXCLUDED.charging_level,
                        power_kw = EXCLUDED.power_kw,
                        port_count = EXCLUDED.port_count,
                        updated_at = now()
                    """,
                    (station_db_id, station_nlr_id, connector_type, network, 
                     charging_level, power_kw, port_count),
                )
        return True
    except Exception as e:
        log.error("Failed to upsert connectors for station %d: %s", station_db_id, e)
        return False


def import_ev_stations(state: Optional[str] = None, limit_pages: Optional[int] = None) -> Dict[str, Any]:
    """Import EV stations from NLR API.

    Args:
        state: US state code (e.g. 'CA'). If None, imports all states.
        limit_pages: Max number of pages to fetch (for testing). If None, fetches all.

    Returns:
        Summary dict with imported/updated/error counts
    """
    api_key = get_nlr_api_key()
    if not api_key:
        raise RuntimeError("NLR API key not configured. Add it in Settings → Charger Locations.")

    _ensure_charger_tables()

    log.info("Starting EV station import (state=%s, limit_pages=%s)", state, limit_pages)

    imported_count = 0
    updated_count = 0
    error_count = 0
    page = 0
    total_results = None
    sync_run_id = None

    try:
        # Create a sync run record
        sync_result = db.execute_returning(
            """
            INSERT INTO ev_sync_runs (sync_type, state_filter, status, started_at)
            VALUES (%s, %s, %s, now())
            RETURNING id
            """,
            ("manual_import", state or "all", "in_progress"),
        )
        sync_run_id = sync_result["id"] if sync_result else None

        # Paginate through results
        while True:
            if limit_pages is not None and page >= limit_pages:
                log.info("Reached limit_pages=%d, stopping import", limit_pages)
                break

            offset = page * 1000
            try:
                data = _nlr_get(fuel_type=FUEL_TYPE_ELEC, state=state, limit=1000, offset=offset)
                total_results = data.get("total_results", 0)
                stations = data.get("fuel_stations", [])

                if not stations:
                    log.info("No more stations to import (page %d, offset %d)", page, offset)
                    break

                log.info("Processing page %d (%d stations, total_results=%d)", page, len(stations), total_results)

                for station in stations:
                    # Skip non-EV or non-operational stations
                    if station.get("fuel_type_code") != FUEL_TYPE_ELEC:
                        continue
                    if station.get("status_code") != "E":
                        continue

                    station_db_id = _upsert_ev_station(station)
                    if not station_db_id:
                        error_count += 1
                        continue

                    # Determine if inserted or updated
                    existing = db.fetch_one(
                        "SELECT id FROM ev_stations WHERE nlr_station_id = %s",
                        (station.get("id"),),
                    )
                    if existing:
                        updated_count += 1
                    else:
                        imported_count += 1

                    # Upsert connectors
                    for charging_unit in station.get("ev_charging_units", []):
                        _upsert_ev_connector(station_db_id, charging_unit, station.get("id"))

                page += 1

            except Exception as e:
                log.error("Error fetching page %d: %s", page, e)
                error_count += 1
                break

        # Update sync run record
        if sync_run_id:
            db.execute(
                """
                UPDATE ev_sync_runs
                SET status = %s, completed_at = now(),
                    stations_imported = %s, stations_updated = %s, errors = %s
                WHERE id = %s
                """,
                ("completed", imported_count, updated_count, error_count, sync_run_id),
            )

        result = {
            "success": True,
            "imported": imported_count,
            "updated": updated_count,
            "errors": error_count,
            "total_results": total_results,
            "pages_processed": page,
            "sync_run_id": sync_run_id,
        }
        log.info("EV station import complete: %s", result)
        return result

    except Exception as e:
        log.error("Import failed: %s", e)
        if sync_run_id:
            db.execute(
                "UPDATE ev_sync_runs SET status = %s, completed_at = now() WHERE id = %s",
                ("failed", sync_run_id),
            )
        raise


def import_ev_stations_with_strategy(
    state: Optional[str] = None,
    strategy: str = "all_then_200",
    page_size: int = 200,
    limit_pages: Optional[int] = None,
) -> Dict[str, Any]:
    """Import EV stations with selectable fetch strategy.

    Strategies:
    - "all_then_200": try one-shot limit="all" first, then fallback to state chunks.
    - "paged_200": compatibility alias; uses state chunks because this API ignores offset.
    """
    api_key = get_nlr_api_key()
    if not api_key:
        raise RuntimeError("NLR API key not configured. Add it in Settings -> Charger Locations.")

    _ensure_charger_tables()

    if page_size < 50:
        page_size = 50
    if page_size > 1000:
        page_size = 1000

    imported_count = 0
    updated_count = 0
    error_count = 0
    page = 0
    total_results = None
    sync_run_id = None
    fetch_mode_used = "state_chunks"

    try:
        sync_result = db.execute_returning(
            """
            INSERT INTO ev_sync_runs (sync_type, state_filter, status, started_at)
            VALUES (%s, %s, %s, now())
            RETURNING id
            """,
            (f"manual_import_{strategy}", state or "all", "in_progress"),
        )
        sync_run_id = sync_result["id"] if sync_result else None

        stations: list[dict[str, Any]] = []
        if strategy == "all_then_200":
            try:
                one_shot = _nlr_get(fuel_type=FUEL_TYPE_ELEC, state=state, limit="all", offset=0)
                total_results = one_shot.get("total_results", 0)
                stations = one_shot.get("fuel_stations", [])
                # Use one-shot result only if it appears complete.
                if stations and (total_results is None or len(stations) >= total_results):
                    fetch_mode_used = "all"
                    log.info("Using one-shot charger import (stations=%d)", len(stations))
                else:
                    stations = []
                    log.warning("One-shot charger import returned partial/empty result; falling back to state chunks")
            except Exception as e:
                log.warning("One-shot charger import failed (%s); falling back to state chunks", e)

        if fetch_mode_used == "all":
            for station in stations:
                if station.get("fuel_type_code") != FUEL_TYPE_ELEC:
                    continue
                if station.get("status_code") != "E":
                    continue

                station_db_id = _upsert_ev_station(station)
                if not station_db_id:
                    error_count += 1
                    continue
                updated_count += 1

                for charging_unit in station.get("ev_charging_units", []):
                    _upsert_ev_connector(station_db_id, charging_unit, station.get("id"))
            page = 1
        else:
            # NREL/NLR endpoint currently ignores offset for this resource,
            # so fallback uses state-by-state chunks instead of page offsets.
            states_to_fetch = [state] if state else sorted(US_STATES)
            chunks_seen = 0

            for st in states_to_fetch:
                if limit_pages is not None and chunks_seen >= limit_pages:
                    log.info("Reached limit_pages=%d, stopping import", limit_pages)
                    break

                try:
                    data = _nlr_get(
                        fuel_type=FUEL_TYPE_ELEC,
                        state=st,
                        limit="all",
                        offset=0,
                    )
                    batch = data.get("fuel_stations", [])
                    total_results = (total_results or 0) + len(batch)

                    if not batch:
                        chunks_seen += 1
                        continue

                    log.info("Processing state chunk %s (%d stations)", st, len(batch))
                    for station in batch:
                        if station.get("fuel_type_code") != FUEL_TYPE_ELEC:
                            continue
                        if station.get("status_code") != "E":
                            continue

                        station_db_id = _upsert_ev_station(station)
                        if not station_db_id:
                            error_count += 1
                            continue
                        updated_count += 1

                        for charging_unit in station.get("ev_charging_units", []):
                            _upsert_ev_connector(station_db_id, charging_unit, station.get("id"))
                    chunks_seen += 1
                except Exception as e:
                    log.error("Error fetching state chunk %s: %s", st, e)
                    error_count += 1
                    chunks_seen += 1

            page = chunks_seen

        if sync_run_id:
            db.execute(
                """
                UPDATE ev_sync_runs
                SET status = %s, completed_at = now(),
                    stations_imported = %s, stations_updated = %s, errors = %s
                WHERE id = %s
                """,
                ("completed", imported_count, updated_count, error_count, sync_run_id),
            )

        result = {
            "success": True,
            "imported": imported_count,
            "updated": updated_count,
            "errors": error_count,
            "total_results": total_results,
            "pages_processed": page,
            "sync_run_id": sync_run_id,
            "fetch_mode_used": fetch_mode_used,
            "page_size": page_size,
        }
        log.info("EV station import complete: %s", result)
        return result
    except Exception as e:
        log.error("Import with strategy failed: %s", e)
        if sync_run_id:
            db.execute(
                "UPDATE ev_sync_runs SET status = %s, completed_at = now() WHERE id = %s",
                ("failed", sync_run_id),
            )
        raise


def get_sync_status() -> Optional[Dict[str, Any]]:
    """Get the most recent sync run status."""
    try:
        row = db.fetch_one(
            """
            SELECT id, sync_type, state_filter, status, started_at, completed_at,
                   stations_imported, stations_updated, errors
            FROM ev_sync_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        return dict(row) if row else None
    except Exception as e:
        log.error("Failed to fetch sync status: %s", e)
        return None


def get_station_count(state: Optional[str] = None) -> int:
    """Get total EV stations count, optionally filtered by state."""
    try:
        if state:
            row = db.fetch_one(
                "SELECT COUNT(*) as cnt FROM ev_stations WHERE state = %s",
                (state,),
            )
        else:
            row = db.fetch_one("SELECT COUNT(*) as cnt FROM ev_stations")
        return row["cnt"] if row else 0
    except Exception as e:
        log.error("Failed to get station count: %s", e)
        return 0
