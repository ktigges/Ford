"""
Trip planner service for personalized EV routing.

Orchestrates:
1. Route analysis (distance, elevation, polyline)
2. Weather forecast along route
3. Energy consumption prediction (using your trained model)
4. Charging stop optimization
5. Charger network recommendations

Outputs trip plan with:
- Total distance & duration
- Energy needed & available capacity
- Recommended charging stops
- Charging duration at each stop

Author:      Kevin Tigges
Date:        2026-05-09
"""

import json
import logging
import math
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import requests

import db

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────

# API keys - load from config or environment
OPENWEATHER_API_KEY = "demo"  # TODO: Set from environment or config.json
GOOGLE_MAPS_API_KEY = None  # TODO: Set if using Google Maps

# EV-specific parameters
BATTERY_CAPACITY_KWH = 131  # Ford F-150 Lightning Standard (adjust per vehicle)
MIN_SAFE_SOC_PERCENT = 10   # Never drain below 10%
DC_FAST_CHARGER_KW = 150    # Average DC fast charger power
AC_CHARGER_KW = 11          # Home charger power (Level 2)

# Route optimization
MAX_DISTANCE_BETWEEN_CHARGERS_KM = 400  # Don't plan stops > 400km apart
MIN_EFFICIENCY_KWH_PER_KM = 0.12
MAX_EFFICIENCY_KWH_PER_KM = 0.60
DEFAULT_EFFICIENCY_KWH_PER_KM = 0.28

US_STATE_ABBREVIATIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
    "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
    "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
}

ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")

STATE_NAME_TO_ABBR = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


# ── Data Structures ────────────────────────────────────────────────

@dataclass
class ChargingStop:
    """Recommended charging stop along route."""
    charger_id: int
    charger_name: str
    lat: float
    lon: float
    distance_km_from_start: float
    arrival_soc_percent: float
    charge_to_soc_percent: float
    charger_power_kw: float
    charge_time_min: int
    network: str
    address: str


@dataclass
class TripSegment:
    """Route segment between two points."""
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    distance_km: float
    elevation_gain_m: float
    avg_temp_c: float
    polyline: list[tuple[float, float]]


@dataclass
class TripPlan:
    """Complete trip plan from source to destination."""
    source_name: str
    destination_name: str
    total_distance_km: float
    estimated_duration_min: int
    start_soc_percent: float
    arrival_soc_percent: float
    energy_needed_kwh: float
    charging_stops: list[ChargingStop]
    feasible: bool
    feasibility_reason: str
    polyline: list[tuple[float, float]]
    weather_summary: str
    created_at: str


# ── Geocoding ──────────────────────────────────────────────────────

def geocode_location(location_query: str) -> tuple[float, float] | None:
    """Convert place name to lat/lon using Nominatim (OpenStreetMap).
    
    Args:
        location_query: "Denver, CO" or "40.7128, -74.0060"
    
    Returns:
        (latitude, longitude) or None if not found
    """
    location_query = (location_query or "").strip()
    if not location_query:
        return None

    # Normalize accidental extra whitespace and wrapping quotes from pasted inputs.
    location_query = " ".join(location_query.strip("\"'").split())

    try:
        # Try parsing as coordinates first
        if "," in location_query:
            parts = [p.strip() for p in location_query.split(",")]
            if len(parts) == 2:
                try:
                    lat, lon = float(parts[0]), float(parts[1])
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        log.info(f"Parsed coordinates: {lat}, {lon}")
                        return (lat, lon)
                except ValueError:
                    pass

        def _looks_like_us_location(query: str) -> bool:
            q = query.strip()
            q_lower = q.lower()
            if ZIP_RE.match(q):
                return True
            if q_lower in US_STATE_NAMES:
                return True
            if q.upper() in US_STATE_ABBREVIATIONS:
                return True

            tokens = [t.strip(". ") for t in q.replace(",", " ").split() if t.strip()]
            if any(ZIP_RE.match(t) for t in tokens):
                return True
            if any(t.upper() in US_STATE_ABBREVIATIONS for t in tokens):
                return True
            return False

        def _normalize_state_token(token: str) -> str | None:
            t = (token or "").strip().strip(".").lower()
            if not t:
                return None
            if len(t) == 2 and t.upper() in US_STATE_ABBREVIATIONS:
                return t.upper()
            if t in STATE_NAME_TO_ABBR:
                return STATE_NAME_TO_ABBR[t]
            return None

        def _extract_us_hint(query: str) -> dict:
            q = (query or "").strip()
            if not q:
                return {"city": None, "state": None, "zip": None, "is_state_only": False}

            q_clean = re.sub(r"\s+", " ", q)
            parts = [p.strip() for p in q_clean.split(",") if p.strip()]
            zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", q_clean)
            zip_code = zip_match.group(1) if zip_match else None

            city = None
            state = None

            if len(parts) >= 2:
                state = _normalize_state_token(parts[-1])
                if not state:
                    # Handle "City, ST ZIP"
                    trailing_tokens = [t for t in re.split(r"\s+", parts[-1]) if t]
                    if trailing_tokens:
                        state = _normalize_state_token(trailing_tokens[0])

                if state:
                    city = parts[0]
            else:
                tokens = [t for t in re.split(r"\s+", q_clean) if t]
                for i in range(len(tokens) - 1, -1, -1):
                    maybe_state = _normalize_state_token(tokens[i])
                    if maybe_state:
                        state = maybe_state
                        if i > 0:
                            city = " ".join(tokens[:i])
                        break

                if not state:
                    maybe_state = _normalize_state_token(q_clean)
                    if maybe_state:
                        state = maybe_state

            if city:
                city = city.strip().strip(",")
                # If city looks like an address, keep free-form mode only.
                if re.match(r"^\d+\s+", city):
                    city = None

            is_state_only = bool(state and not city and not zip_code)
            return {
                "city": city,
                "state": state,
                "zip": zip_code,
                "is_state_only": is_state_only,
            }

        def _state_from_nominatim_address(address: dict) -> str | None:
            if not isinstance(address, dict):
                return None
            for key in ("state_code", "ISO3166-2-lvl4", "state"):
                raw = address.get(key)
                if not raw:
                    continue
                text = str(raw).strip()
                if key == "ISO3166-2-lvl4" and "-" in text:
                    text = text.split("-", 1)[1]
                normalized = _normalize_state_token(text)
                if normalized:
                    return normalized
            return None

        def _city_from_nominatim_address(address: dict) -> str | None:
            if not isinstance(address, dict):
                return None
            for key in ("city", "town", "village", "hamlet", "municipality", "county"):
                value = address.get(key)
                if value:
                    return str(value).strip().lower()
            return None

        def _score_nominatim_result(result: dict, us_hint: dict, prefer_us: bool) -> int:
            score = 0
            address = (result or {}).get("address") or {}
            country_code = str(address.get("country_code", "")).lower()
            if prefer_us:
                score += 200 if country_code == "us" else -200

            expected_state = us_hint.get("state")
            if expected_state:
                actual_state = _state_from_nominatim_address(address)
                if actual_state == expected_state:
                    score += 140
                elif actual_state:
                    score -= 120

            expected_city = (us_hint.get("city") or "").lower()
            if expected_city:
                actual_city = _city_from_nominatim_address(address)
                if actual_city and actual_city == expected_city:
                    score += 100
                elif actual_city:
                    score -= 60

            expected_zip = us_hint.get("zip")
            if expected_zip:
                actual_zip = str(address.get("postcode", "")).strip()
                if actual_zip.startswith(expected_zip):
                    score += 90
                elif actual_zip:
                    score -= 60

            if us_hint.get("is_state_only"):
                result_type = str((result or {}).get("type", "")).lower()
                if result_type in {"administrative", "state"}:
                    score += 40

            # Favor more specific rank when scores tie.
            rank = int((result or {}).get("place_rank", 0) or 0)
            score += min(rank, 30)
            return score

        def _build_query_variants(query: str) -> list[str]:
            variants = [query]
            lower_query = query.lower()
            if _looks_like_us_location(query) and not any(s in lower_query for s in ("usa", "united states", "us")):
                variants.insert(0, f"{query}, USA")
            return variants

        query_variants = _build_query_variants(location_query)
        looks_us = _looks_like_us_location(location_query)
        us_hint = _extract_us_hint(location_query)
        
        # Primary provider: Nominatim with a short retry window.
        nominatim_url = "https://nominatim.openstreetmap.org/search"
        headers = {
            "User-Agent": "MLLighting-Trip-Planner/1.0"
        }

        for query in query_variants:
            # Structured search is more reliable for city/state/zip lookups.
            structured_params = None
            if us_hint.get("state") and not re.match(r"^\d+\s+", query.strip()):
                structured_params = {
                    "format": "json",
                    "limit": 10,
                    "addressdetails": 1,
                    "countrycodes": "us",
                    "state": us_hint["state"],
                }
                if us_hint.get("city"):
                    structured_params["city"] = us_hint["city"]
                if us_hint.get("zip"):
                    structured_params["postalcode"] = us_hint["zip"]

            structured_candidates = []
            if structured_params:
                try:
                    response = requests.get(nominatim_url, params=structured_params, headers=headers, timeout=10)
                    response.raise_for_status()
                    structured_candidates = response.json() or []
                except requests.RequestException as e:
                    log.warning("Structured geocode failed for '%s': %s", location_query, e)

                if structured_candidates:
                    best = max(
                        structured_candidates,
                        key=lambda r: _score_nominatim_result(r, us_hint, looks_us),
                    )
                    lat = float(best["lat"])
                    lon = float(best["lon"])
                    log.info(
                        "Geocoded '%s' via Nominatim structured query to %s, %s (%s)",
                        location_query,
                        lat,
                        lon,
                        best.get("display_name", ""),
                    )
                    return (lat, lon)

            search_attempts = [
                {
                    "q": query,
                    "format": "json",
                    "limit": 10,
                    "addressdetails": 1,
                    "accept-language": "en",
                }
            ]
            if looks_us:
                search_attempts.insert(
                    0,
                    {
                        "q": query,
                        "format": "json",
                        "limit": 10,
                        "addressdetails": 1,
                        "countrycodes": "us",
                        "accept-language": "en",
                    },
                )

            for nominatim_params in search_attempts:
                try:
                    response = requests.get(nominatim_url, params=nominatim_params, headers=headers, timeout=10)
                    response.raise_for_status()
                    results = response.json() or []
                    if results:
                        best = max(
                            results,
                            key=lambda r: _score_nominatim_result(r, us_hint, looks_us),
                        )

                        lat = float(best["lat"])
                        lon = float(best["lon"])
                        log.info(
                            "Geocoded '%s' via Nominatim (%s) to %s, %s (%s)",
                            location_query,
                            query,
                            lat,
                            lon,
                            best.get("display_name", ""),
                        )
                        return (lat, lon)
                except requests.RequestException as e:
                    log.warning("Nominatim geocode failed for '%s' query '%s': %s", location_query, query, e)

        # Fallback provider: Photon
        photon_url = "https://photon.komoot.io/api/"
        for query in query_variants:
            photon_params = {
                "q": query,
                "limit": 3,
                "lang": "en",
            }
            response = requests.get(photon_url, params=photon_params, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            features = data.get("features") or []
            if features:
                best_feature = features[0]
                if looks_us:
                    for feature in features:
                        cc = (feature.get("properties") or {}).get("countrycode", "").lower()
                        if cc == "us":
                            best_feature = feature
                            break

                coords = best_feature.get("geometry", {}).get("coordinates") or []
                if len(coords) >= 2:
                    lon = float(coords[0])
                    lat = float(coords[1])
                    log.info("Geocoded '%s' via Photon (%s) to %s, %s", location_query, query, lat, lon)
                    return (lat, lon)

        log.warning(f"Could not geocode location: {location_query}")
        return None

    except requests.RequestException as e:
        log.error(f"Geocoding API error for '{location_query}': {e}")
        return None


# ── Distance/Elevation Helpers ─────────────────────────────────────

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two coordinates in km."""
    R = 6371  # Earth radius in km
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    return R * c


# ── Route Service (OpenRouteService or fallback) ────────────────────

def get_route(
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float
) -> Optional[dict]:
    """Get route between two points.
    
    Returns:
    {
        "distance_km": float,
        "duration_sec": int,
        "polyline": [(lat, lon), ...],
        "elevation_gain_m": float,
    }
    """
    # Try OpenRouteService (free tier available)
    try:
        url = "https://api.openrouteservice.org/v2/directions/driving-car"
        params = {
            "start": f"{start_lon},{start_lat}",
            "end": f"{end_lon},{end_lat}",
            "api_key": "5b3ce3597851110001cf6248",  # Public demo key (limited)
            "format": "geojson",
            "geometry_format": "geojson"
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "features" in data and data["features"]:
            route = data["features"][0]
            coords = route["geometry"]["coordinates"]
            polyline = [(lat, lon) for lon, lat in coords]  # ORS returns [lon, lat]
            
            distance_km = route["properties"]["segments"][0]["distance"] / 1000
            duration_sec = int(route["properties"]["segments"][0]["duration"])
            
            # Rough elevation gain estimation (2% elevation per 100m horizontal)
            elevation_gain_m = distance_km * 20
            
            log.info(f"Route found: {distance_km:.1f} km, {duration_sec//60} min")
            
            return {
                "distance_km": distance_km,
                "duration_sec": duration_sec,
                "polyline": polyline,
                "elevation_gain_m": elevation_gain_m,
            }
        else:
            log.warning("No route found in ORS response")
            return None
    
    except Exception as e:
        log.warning(f"OpenRouteService failed: {e}, using fallback")
        
        # Fallback: simple great-circle distance
        distance_km = haversine_distance(start_lat, start_lon, end_lat, end_lon)
        duration_sec = int(distance_km / 80 * 3600)  # Assume 80 km/h avg
        
        # Linear interpolation for polyline
        steps = max(10, int(distance_km / 10))
        polyline = []
        for i in range(steps + 1):
            t = i / steps
            lat = start_lat + t * (end_lat - start_lat)
            lon = start_lon + t * (end_lon - start_lon)
            polyline.append((lat, lon))
        
        return {
            "distance_km": distance_km,
            "duration_sec": duration_sec,
            "polyline": polyline,
            "elevation_gain_m": 0,
        }


# ── Weather Service ───────────────────────────────────────────────

def get_weather_forecast(
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float,
    route_distance_km: float
) -> Optional[dict]:
    """Get weather forecast for route.
    
    Returns:
    {
        "start_temp_c": float,
        "end_temp_c": float,
        "avg_temp_c": float,
        "wind_speed_kmh": float,
        "precipitation_pct": int,
        "condition": str
    }
    """
    try:
        # Get weather at both endpoints
        url = "https://api.openweathermap.org/data/2.5/weather"
        
        params_start = {"lat": start_lat, "lon": start_lon, "appid": OPENWEATHER_API_KEY, "units": "metric"}
        params_end = {"lat": end_lat, "lon": end_lon, "appid": OPENWEATHER_API_KEY, "units": "metric"}
        
        r_start = requests.get(url, params=params_start, timeout=5)
        r_end = requests.get(url, params=params_end, timeout=5)
        
        if r_start.status_code == 200 and r_end.status_code == 200:
            start_data = r_start.json()
            end_data = r_end.json()
            
            start_temp = start_data["main"]["temp"]
            end_temp = end_data["main"]["temp"]
            
            return {
                "start_temp_c": start_temp,
                "end_temp_c": end_temp,
                "avg_temp_c": (start_temp + end_temp) / 2,
                "wind_speed_kmh": start_data["wind"]["speed"] * 3.6,
                "precipitation_pct": start_data.get("clouds", {}).get("cloudiness", 0),
                "condition": start_data["weather"][0]["main"],
            }
    except Exception as e:
        log.warning(f"Weather API failed: {e}")
    
    # Fallback: assume mild weather
    return {
        "start_temp_c": 15,
        "end_temp_c": 15,
        "avg_temp_c": 15,
        "wind_speed_kmh": 0,
        "precipitation_pct": 0,
        "condition": "Unknown",
    }


# ── Charger Recommendations ────────────────────────────────────────

def find_nearby_chargers(
    lat: float, lon: float, radius_km: float = 50, limit: int = 5
) -> list[dict]:
    """Find chargers near a location.
    
    Uses your existing EV stations database.
    """
    try:
        chargers = db.fetch_all("""
            SELECT 
                s.id,
                s.station_name AS name,
                s.latitude,
                s.longitude,
                s.city,
                s.state,
                s.street_address AS address,
                COALESCE(MAX(NULLIF(c.network, '')), s.network_name, 'Unknown') AS network,
                COALESCE(MAX(c.power_kw), %s) AS max_power_kw,
                SQRT(
                    POW(s.latitude - %s, 2) + 
                    POW(s.longitude - %s, 2)
                ) * 111 AS distance_km
            FROM ev_stations s
            LEFT JOIN ev_charger_connectors c ON s.id = c.station_id
            WHERE 
                SQRT(
                    POW(s.latitude - %s, 2) + 
                    POW(s.longitude - %s, 2)
                ) * 111 < %s
            GROUP BY
                s.id,
                s.station_name,
                s.latitude,
                s.longitude,
                s.city,
                s.state,
                s.street_address,
                s.network_name
            ORDER BY distance_km ASC
            LIMIT %s
        """, (DC_FAST_CHARGER_KW, lat, lon, lat, lon, radius_km, limit))
        
        return chargers if chargers else []
    
    except Exception as e:
        log.error(f"Charger search failed: {e}")
        return []


def optimize_charging_stops(
    polyline: list[tuple[float, float]],
    total_distance_km: float,
    current_soc_percent: float,
    energy_needed_kwh: float
) -> list[ChargingStop]:
    """Determine optimal charging stops along route.
    
    Returns list of recommended stops with:
    - Location (charger)
    - Arrival SOC
    - Charge-to SOC
    - Charging time
    """
    stops: list[ChargingStop] = []

    if total_distance_km <= 0:
        return stops

    kwh_per_km = energy_needed_kwh / total_distance_km if energy_needed_kwh > 0 else DEFAULT_EFFICIENCY_KWH_PER_KM
    distance_traveled = 0.0
    current_soc = float(current_soc_percent)
    max_stops = 8

    for _ in range(max_stops):
        usable_kwh = max(0.0, (current_soc - MIN_SAFE_SOC_PERCENT) / 100 * BATTERY_CAPACITY_KWH)
        reachable_km = usable_kwh / kwh_per_km if kwh_per_km > 0 else total_distance_km
        remaining_km = total_distance_km - distance_traveled

        if reachable_km >= remaining_km:
            break

        # Place the next stop well before empty to reduce edge-case failures.
        stop_location_km = distance_traveled + max(5.0, min(reachable_km * 0.75, remaining_km - 1.0))
        segment_idx = min(int(stop_location_km / total_distance_km * max(1, len(polyline) - 1)), len(polyline) - 1)
        stop_lat, stop_lon = polyline[segment_idx]

        nearby: list[dict] = []
        for radius in (25, 40, 60):
            nearby = find_nearby_chargers(stop_lat, stop_lon, radius_km=radius, limit=10)
            if nearby:
                break

        if not nearby:
            log.warning("No chargers found near route km %.1f (lat=%.4f lon=%.4f)", stop_location_km, stop_lat, stop_lon)
            break

        # Prefer high-power chargers among nearest options.
        charger = max(
            nearby,
            key=lambda c: (
                float(c.get("max_power_kw") or 0),
                -float(c.get("distance_km") or 0),
            ),
        )

        distance_to_stop = stop_location_km - distance_traveled
        energy_to_stop = distance_to_stop * kwh_per_km
        arrival_soc = current_soc - (energy_to_stop / BATTERY_CAPACITY_KWH * 100)
        if arrival_soc < MIN_SAFE_SOC_PERCENT:
            arrival_soc = MIN_SAFE_SOC_PERCENT

        remaining_after_stop_km = total_distance_km - stop_location_km
        target_soc_for_destination = (
            MIN_SAFE_SOC_PERCENT
            + ((remaining_after_stop_km * kwh_per_km) / BATTERY_CAPACITY_KWH * 100)
            + 5
        )
        charge_to_soc = max(70.0, min(90.0, target_soc_for_destination))
        if charge_to_soc <= arrival_soc:
            charge_to_soc = min(90.0, arrival_soc + 10.0)

        energy_to_add = max(0.0, (charge_to_soc - arrival_soc) / 100 * BATTERY_CAPACITY_KWH)
        charger_power = max(float(charger.get("max_power_kw") or DC_FAST_CHARGER_KW), 25.0)
        charge_time_min = int(round((energy_to_add / charger_power) * 60))

        stop = ChargingStop(
            charger_id=charger["id"],
            charger_name=charger["name"],
            lat=charger["latitude"],
            lon=charger["longitude"],
            distance_km_from_start=stop_location_km,
            arrival_soc_percent=arrival_soc,
            charge_to_soc_percent=charge_to_soc,
            charger_power_kw=charger_power,
            charge_time_min=max(1, charge_time_min),
            network=charger.get("network", "Unknown"),
            address=charger.get("address", ""),
        )

        stops.append(stop)
        distance_traveled = stop_location_km
        current_soc = charge_to_soc

        log.info(
            "Stop %s: %s at km %.0f, arrive %.0f%%, charge to %.0f%% (%s min)",
            len(stops),
            charger["name"],
            stop_location_km,
            arrival_soc,
            charge_to_soc,
            stop.charge_time_min,
        )

    return stops


def _is_direct_trip_feasible(start_soc_percent: float, energy_needed_kwh: float) -> bool:
    usable_kwh = max(0.0, (start_soc_percent - MIN_SAFE_SOC_PERCENT) / 100 * BATTERY_CAPACITY_KWH)
    return usable_kwh >= max(0.0, energy_needed_kwh)


def _estimate_arrival_soc(start_soc_percent: float, energy_used_kwh: float) -> float:
    soc_drop = (energy_used_kwh / BATTERY_CAPACITY_KWH) * 100 if BATTERY_CAPACITY_KWH > 0 else 100
    return max(0.0, min(100.0, start_soc_percent - soc_drop))


def _can_complete_with_stops(
    total_distance_km: float,
    start_soc_percent: float,
    energy_needed_kwh: float,
    stops: list[ChargingStop],
) -> tuple[bool, float]:
    if total_distance_km <= 0:
        return False, 0.0

    kwh_per_km = energy_needed_kwh / total_distance_km if energy_needed_kwh > 0 else DEFAULT_EFFICIENCY_KWH_PER_KM
    distance_traveled = 0.0
    current_soc = float(start_soc_percent)

    for stop in sorted(stops, key=lambda s: s.distance_km_from_start):
        leg_km = max(0.0, stop.distance_km_from_start - distance_traveled)
        leg_kwh = leg_km * kwh_per_km
        usable_kwh = max(0.0, (current_soc - MIN_SAFE_SOC_PERCENT) / 100 * BATTERY_CAPACITY_KWH)
        if leg_kwh > usable_kwh + 1e-6:
            return False, 0.0
        current_soc = max(MIN_SAFE_SOC_PERCENT, current_soc - (leg_kwh / BATTERY_CAPACITY_KWH * 100))
        current_soc = max(current_soc, float(stop.charge_to_soc_percent))
        distance_traveled = stop.distance_km_from_start

    final_leg_km = max(0.0, total_distance_km - distance_traveled)
    final_leg_kwh = final_leg_km * kwh_per_km
    final_usable_kwh = max(0.0, (current_soc - MIN_SAFE_SOC_PERCENT) / 100 * BATTERY_CAPACITY_KWH)
    if final_leg_kwh > final_usable_kwh + 1e-6:
        return False, 0.0

    arrival_soc = max(
        0.0,
        current_soc - (final_leg_kwh / BATTERY_CAPACITY_KWH * 100),
    )
    return True, arrival_soc


# ── Main Trip Planning ─────────────────────────────────────────────

def plan_trip(
    source: str,
    destination: str,
    current_soc_percent: float = 100,
    current_temp_c: float = 15,
) -> TripPlan:
    """Plan a complete trip from source to destination.
    
    Args:
        source: Place name or "lat,lon"
        destination: Place name or "lat,lon"
        current_soc_percent: Current battery state of charge (0-100)
        current_temp_c: Current outside temperature for energy adjustment
    
    Returns:
        TripPlan with route, energy prediction, charging stops, and feasibility
    """
    
    log.info(f"Planning trip: {source} → {destination} (SOC: {current_soc_percent}%)")
    
    # Geocode locations
    start_coords = geocode_location(source)
    end_coords = geocode_location(destination)
    
    if not start_coords or not end_coords:
        missing = []
        if not start_coords:
            missing.append(f"source '{source}'")
        if not end_coords:
            missing.append(f"destination '{destination}'")
        missing_text = " and ".join(missing) if missing else "source/destination"
        return TripPlan(
            source_name=source,
            destination_name=destination,
            total_distance_km=0,
            estimated_duration_min=0,
            start_soc_percent=current_soc_percent,
            arrival_soc_percent=0,
            energy_needed_kwh=0,
            charging_stops=[],
            feasible=False,
            feasibility_reason=(
                f"Could not geolocate {missing_text}. Try 'City, ST' or 'lat,lon' format."
            ),
            polyline=[],
            weather_summary="",
            created_at=datetime.now().isoformat()
        )
    
    start_lat, start_lon = start_coords
    end_lat, end_lon = end_coords
    
    log.info(f"Start: {start_lat:.4f}, {start_lon:.4f}")
    log.info(f"End: {end_lat:.4f}, {end_lon:.4f}")
    
    # Get route
    route = get_route(start_lat, start_lon, end_lat, end_lon)
    if not route:
        return TripPlan(
            source_name=source,
            destination_name=destination,
            total_distance_km=0,
            estimated_duration_min=0,
            start_soc_percent=current_soc_percent,
            arrival_soc_percent=0,
            energy_needed_kwh=0,
            charging_stops=[],
            feasible=False,
            feasibility_reason="Could not calculate route",
            polyline=[],
            weather_summary="",
            created_at=datetime.now().isoformat()
        )
    
    distance_km = route["distance_km"]
    duration_sec = route["duration_sec"]
    polyline = route["polyline"]
    
    # Phase 1 estimate: distance + fixed efficiency baseline.
    energy_needed_kwh = distance_km * DEFAULT_EFFICIENCY_KWH_PER_KM

    min_plausible_kwh = distance_km * MIN_EFFICIENCY_KWH_PER_KM
    max_plausible_kwh = distance_km * MAX_EFFICIENCY_KWH_PER_KM
    energy_needed_kwh = max(min_plausible_kwh, min(max_plausible_kwh, energy_needed_kwh))

    feasible = _is_direct_trip_feasible(current_soc_percent, energy_needed_kwh)

    charging_stops = []
    arrival_soc = _estimate_arrival_soc(current_soc_percent, energy_needed_kwh)
    
    plan = TripPlan(
        source_name=source,
        destination_name=destination,
        total_distance_km=distance_km,
        estimated_duration_min=int(duration_sec / 60),
        start_soc_percent=current_soc_percent,
        arrival_soc_percent=arrival_soc,
        energy_needed_kwh=energy_needed_kwh,
        charging_stops=charging_stops,
        feasible=feasible,
        feasibility_reason=(
            "Baseline estimate generated successfully"
            if feasible
            else "Baseline estimate generated (trip may require charging)"
        ),
        polyline=polyline,
        weather_summary="Not included in baseline estimate",
        created_at=datetime.now().isoformat()
    )
    
    log.info(f"\n{'='*60}")
    log.info("Trip Plan Summary:")
    log.info(f"  Distance: {distance_km:.1f} km")
    log.info(f"  Duration: {plan.estimated_duration_min} min")
    log.info(f"  Energy: {energy_needed_kwh:.1f} kWh (baseline estimate)")
    log.info(f"  Start SOC: {current_soc_percent:.0f}% → Arrival SOC: {arrival_soc:.0f}%")
    log.info(f"  Charging stops: {len(charging_stops)}")
    log.info(f"  Feasible: {feasible}")
    log.info(f"  Reason: {plan.feasibility_reason}")
    log.info(f"{'='*60}\n")
    
    return plan


# ── Testing ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_pool()
    
    try:
        # Test trip planning with coordinates
        # Denver: 39.7392, -104.9903
        # Fort Collins: 40.5853, -105.0844
        plan = plan_trip(
            source="39.7392,-104.9903",
            destination="40.5853,-105.0844",
            current_soc_percent=85,
        )
        
        print(f"\n{'='*60}")
        print("TRIP PLAN OUTPUT")
        print(f"{'='*60}")
        print(json.dumps(asdict(plan), indent=2, default=str))
        
    except Exception as e:
        log.exception(f"Trip planning failed: {e}")
    
    finally:
        db.close_pool()
