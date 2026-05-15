"""
Open Charge Map (OCM) charger data fetch and normalization routines.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""

import logging
import requests
from typing import Optional, List, Dict, Any
import config

log = logging.getLogger("ocm_chargers")

OCM_API_BASE = "https://api.openchargemap.io/v3/poi/"
OCM_HTTP_TIMEOUT_SEC = 45


def get_ocm_api_key() -> Optional[str]:
    """Retrieve OCM API key from config or app_config."""
    return config.ocm_api_key()


def fetch_ocm_chargers(
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    distance_km: Optional[float] = None,
    country_code: str = "US",
    maxresults: int = 100,
    state: Optional[str] = None,
    ocm_api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch charger locations from Open Charge Map API.

    Args:
        latitude, longitude, distance_km: Optional geo bounding for search
        country_code: Country filter (default US)
        maxresults: Max results to return
        state: Optional state filter (US only)
        ocm_api_key: Optional API key (uses config if not provided)

    Returns:
        List of OCM charger dicts (raw)
    """
    if ocm_api_key is None:
        ocm_api_key = get_ocm_api_key()
    if not ocm_api_key:
        log.warning("OCM API key not configured; results may be limited or rejected.")

    params = {
        "output": "json",
        "countrycode": country_code,
        "maxresults": maxresults,
        "compact": "true",
        "verbose": "false",
    }
    if latitude is not None and longitude is not None:
        params["latitude"] = latitude
        params["longitude"] = longitude
    if distance_km is not None:
        params["distance"] = distance_km
    if state:
        params["stateorprovince"] = state
    headers = {
        "X-API-Key": ocm_api_key or "",
        "User-Agent": "Ford-Lightning-EV/1.0",
        "Accept": "application/json",
    }
    log.info(
        "OCM request start (country=%s, state=%s, maxresults=%s, has_api_key=%s)",
        country_code,
        state or "all",
        maxresults,
        bool(ocm_api_key),
    )

    try:
        resp = requests.get(OCM_API_BASE, params=params, headers=headers, timeout=OCM_HTTP_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
        log.info("OCM request success (results=%s)", len(data) if isinstance(data, list) else 0)
        return data
    except Exception as e:
        log.error("OCM API request failed: %s", e)
        return []


def normalize_ocm_station(ocm_station: Dict[str, Any]) -> Dict[str, Any]:
    """Convert OCM station to NREL-like normalized dict for merging."""
    addr = ocm_station.get("AddressInfo", {})
    operator_name = ocm_station.get("OperatorInfo", {}).get("Title")

    ev_units: list[dict[str, Any]] = []
    for conn in ocm_station.get("Connections") or []:
        connector_label = (
            conn.get("ConnectionType", {}).get("Title")
            or conn.get("ConnectionTypeID")
            or "UNKNOWN"
        )
        connector_key = str(connector_label).upper().replace(" ", "_")

        quantity_raw = conn.get("Quantity")
        try:
            quantity = int(quantity_raw) if quantity_raw is not None else 1
        except (TypeError, ValueError):
            quantity = 1
        quantity = max(1, quantity)

        power_kw_raw = conn.get("PowerKW")
        try:
            power_kw = float(power_kw_raw) if power_kw_raw is not None else None
        except (TypeError, ValueError):
            power_kw = None

        level = ""
        level_info = conn.get("Level") or {}
        if level_info.get("IsFastChargeCapable"):
            level = "dc_fast"
        else:
            level_id = conn.get("LevelID")
            if level_id == 1:
                level = "level_1"
            elif level_id == 2:
                level = "level_2"
            elif level_id == 3:
                level = "dc_fast"

        ev_units.append(
            {
                "network": operator_name,
                "charging_level": level,
                "connectors": {
                    connector_key: {
                        "port_count": quantity,
                        "power_kw": power_kw,
                    }
                },
            }
        )

    return {
        "source": "OCM",
        "ocm_id": ocm_station.get("ID"),
        "station_name": addr.get("Title"),
        "street_address": addr.get("AddressLine1"),
        "city": addr.get("Town"),
        "state": addr.get("StateOrProvince"),
        "zip": addr.get("Postcode"),
        "country": addr.get("Country", {}).get("ISOCode", "US"),
        "latitude": addr.get("Latitude"),
        "longitude": addr.get("Longitude"),
        "status_code": ocm_station.get("StatusType", {}).get("IsOperational", None),
        "network_name": operator_name,
        "updated_at": ocm_station.get("DateLastStatusUpdate"),
        "ev_charging_units": ev_units,
        "raw_data": ocm_station,
    }


def normalize_ocm_stations(ocm_stations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize a list of OCM stations to NREL-like format."""
    return [normalize_ocm_station(st) for st in ocm_stations]
