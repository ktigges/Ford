"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""

import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional

import requests

import db
import energy_model

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────

# API keys - load from environment
# OPENWEATHER_API_KEY: Optional, falls back to limited demo mode if not set
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY") or "demo"
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
OPENROUTESERVICE_API_KEY = os.getenv("OPENROUTESERVICE_API_KEY")

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
WEATHER_SAMPLE_INTERVAL_MILES = 20
WEATHER_MAX_CHECKPOINTS = 10
HTTP_TIMEOUT_GEOCODE_SEC = 6
HTTP_TIMEOUT_ROUTE_SEC = 8
HTTP_TIMEOUT_WEATHER_SEC = 4
WEATHER_FETCH_WORKERS = 4
MAX_ROUTE_POLYLINE_POINTS = 1200
MIN_ML_TRAINING_DRIVES = 30
ML_MIN_CONFIDENCE_LEVELS = {"high", "medium"}
ML_ENERGY_DEVIATION_LIMIT = 0.35
DEFAULT_CONNECTOR_TYPE = "CCS1"
DEFAULT_MIN_CHARGER_KW = 50.0

CONNECTOR_TYPE_ALIASES = {
    "CCS1": ["CCS1", "J1772COMBO", "CCS", "CCS_COMBO", "SAE_COMBO"],
    "J1772COMBO": ["J1772COMBO", "CCS1", "CCS", "CCS_COMBO", "SAE_COMBO"],
    "NACS": ["NACS", "TESLA"],
    "CHADEMO": ["CHADEMO"],
}

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
    # Both estimates for comparison
    baseline_energy_needed_kwh: float
    ml_energy_needed_kwh: float
    energy_needed_kwh: float  # For backward compatibility (use ML if available, else baseline)
    charging_stops: list[ChargingStop]  # Used estimate's stops
    baseline_charging_stops: list[ChargingStop]  # Stops calculated from baseline energy
    ml_charging_stops: list[ChargingStop]  # Stops calculated from ML energy
    feasible: bool
    feasibility_reason: str
    polyline: list[tuple[float, float]]
    route_steps: list[dict]
    baseline_route_steps: list[dict]  # Route steps with SOC based on baseline energy estimate
    ml_route_steps: list[dict]  # Route steps with SOC based on ML energy estimate
    route_weather: list[dict]
    weather_summary: str
    estimate_source: str
    estimate_confidence: str
    ml_training_drives: int
    ml_estimate_note: str
    created_at: str


def _connector_candidates(connector_type: str) -> list[str]:
    raw = str(connector_type or "").strip().upper()
    if not raw or raw == "ANY":
        return []
    return CONNECTOR_TYPE_ALIASES.get(raw, [raw])


OPEN_METEO_WEATHER_CODE_LABELS = {
    0: "Clear",
    1: "Mostly Clear",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime Fog",
    51: "Light Drizzle",
    53: "Drizzle",
    55: "Heavy Drizzle",
    56: "Freezing Drizzle",
    57: "Heavy Freezing Drizzle",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    66: "Freezing Rain",
    67: "Heavy Freezing Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    77: "Snow Grains",
    80: "Rain Showers",
    81: "Heavy Showers",
    82: "Violent Showers",
    85: "Snow Showers",
    86: "Heavy Snow Showers",
    95: "Thunderstorm",
    96: "Thunderstorm/Hail",
    99: "Severe Thunderstorm/Hail",
}


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
                return {
                    "street": None,
                    "city": None,
                    "state": None,
                    "zip": None,
                    "is_state_only": False,
                }

            q_clean = re.sub(r"\s+", " ", q)
            parts = [p.strip() for p in q_clean.split(",") if p.strip()]
            zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", q_clean)
            zip_code = zip_match.group(1) if zip_match else None

            street = None
            city = None
            state = None

            if len(parts) >= 3:
                # Most common address format: "street, city, ST ZIP"
                street = parts[0]
                city = parts[1]
                trailing_tokens = [t for t in re.split(r"\s+", parts[-1]) if t]
                if trailing_tokens:
                    state = _normalize_state_token(trailing_tokens[0])
            elif len(parts) == 2:
                trailing_tokens = [t for t in re.split(r"\s+", parts[-1]) if t]
                if trailing_tokens:
                    state = _normalize_state_token(trailing_tokens[0])

                # If first segment looks like an address (starts with house number), treat it as street.
                if re.match(r"^\d+\s+", parts[0]):
                    street = parts[0]
                    if state and len(trailing_tokens) > 1:
                        city = " ".join(trailing_tokens[1:])
                else:
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

            if street:
                street = street.strip().strip(",")
            if city:
                city = city.strip().strip(",")
                # If parsed city still looks like an address, move it to street.
                if re.match(r"^\d+\s+", city):
                    street = city
                    city = None

            is_state_only = bool(state and not city and not zip_code)
            return {
                "street": street,
                "city": city,
                "state": state,
                "zip": zip_code,
                "is_state_only": is_state_only,
            }

        def _normalize_text_for_match(text: str) -> str:
            t = (text or "").lower()
            t = re.sub(r"\bgrey\b", "gray", t)
            t = re.sub(r"\s+", " ", t).strip()
            return t

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
                if actual_city and _normalize_text_for_match(actual_city) == _normalize_text_for_match(expected_city):
                    score += 100
                elif actual_city:
                    score -= 60

            expected_street = us_hint.get("street") or ""
            if expected_street:
                expected_street_norm = _normalize_text_for_match(expected_street)
                road = _normalize_text_for_match(str(address.get("road", "")))
                display_name = _normalize_text_for_match(str((result or {}).get("display_name", "")))
                if road and road in expected_street_norm:
                    score += 90
                elif road and expected_street_norm in road:
                    score += 90
                elif road:
                    score -= 70

                if expected_street_norm and expected_street_norm in display_name:
                    score += 70

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

            # Common US spelling alternations (e.g., Gray/Grey) for street names.
            q_gray = re.sub(r"\bgray\b", "grey", query, flags=re.IGNORECASE)
            q_grey = re.sub(r"\bgrey\b", "gray", query, flags=re.IGNORECASE)
            for alt in (q_gray, q_grey):
                if alt and alt not in variants:
                    variants.append(alt)

            lower_query = query.lower()
            if _looks_like_us_location(query) and not any(s in lower_query for s in ("usa", "united states", "us")):
                variants = [f"{v}, USA" for v in variants] + variants

            # Keep deterministic order without duplicates.
            deduped = []
            seen = set()
            for v in variants:
                if v not in seen:
                    deduped.append(v)
                    seen.add(v)
            variants = deduped
            return variants

        query_variants = _build_query_variants(location_query)
        looks_us = _looks_like_us_location(location_query)
        us_hint = _extract_us_hint(location_query)
        
        def _is_rate_limited_error(err: requests.RequestException) -> bool:
            response = getattr(err, "response", None)
            return bool(response is not None and response.status_code == 429)

        # Primary provider: Nominatim with a short retry window.
        nominatim_url = "https://nominatim.openstreetmap.org/search"
        headers = {
            "User-Agent": "MLLighting-Trip-Planner/1.0"
        }
        nominatim_rate_limited = False

        for query in query_variants:
            # Structured search is more reliable for city/state/zip lookups.
            structured_params = None
            if us_hint.get("state"):
                structured_params = {
                    "format": "json",
                    "limit": 6,
                    "addressdetails": 1,
                    "countrycodes": "us",
                    "state": us_hint["state"],
                }
                if us_hint.get("street"):
                    structured_params["street"] = us_hint["street"]
                if us_hint.get("city"):
                    structured_params["city"] = us_hint["city"]
                if us_hint.get("zip"):
                    structured_params["postalcode"] = us_hint["zip"]

            structured_candidates = []
            if structured_params and not nominatim_rate_limited:
                try:
                    response = requests.get(
                        nominatim_url,
                        params=structured_params,
                        headers=headers,
                        timeout=HTTP_TIMEOUT_GEOCODE_SEC,
                    )
                    response.raise_for_status()
                    structured_candidates = response.json() or []
                except requests.RequestException as e:
                    log.warning("Structured geocode failed for '%s': %s", location_query, e)
                    if _is_rate_limited_error(e):
                        nominatim_rate_limited = True

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
                    "limit": 6,
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
                        "limit": 6,
                        "addressdetails": 1,
                        "countrycodes": "us",
                        "accept-language": "en",
                    },
                )

            for nominatim_params in search_attempts:
                if nominatim_rate_limited:
                    break
                try:
                    response = requests.get(
                        nominatim_url,
                        params=nominatim_params,
                        headers=headers,
                        timeout=HTTP_TIMEOUT_GEOCODE_SEC,
                    )
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
                    if _is_rate_limited_error(e):
                        nominatim_rate_limited = True
            if nominatim_rate_limited:
                break

        # Fallback provider for US addresses: US Census Geocoder.
        # Useful when Nominatim throttles and for precise house-number matching.
        if looks_us and (us_hint.get("street") or us_hint.get("city") or us_hint.get("zip")):
            census_url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

            census_queries = []
            if us_hint.get("street") and us_hint.get("city") and us_hint.get("state"):
                if us_hint.get("zip"):
                    census_queries.append(
                        f"{us_hint['street']}, {us_hint['city']}, {us_hint['state']} {us_hint['zip']}"
                    )
                census_queries.append(
                    f"{us_hint['street']}, {us_hint['city']}, {us_hint['state']}"
                )

            census_queries.extend(query_variants)

            seen_census = set()
            for c_query in census_queries:
                c_query = (c_query or "").strip()
                if not c_query or c_query in seen_census:
                    continue
                seen_census.add(c_query)
                try:
                    response = requests.get(
                        census_url,
                        params={
                            "address": c_query,
                            "benchmark": "Public_AR_Current",
                            "format": "json",
                        },
                        headers=headers,
                        timeout=HTTP_TIMEOUT_GEOCODE_SEC,
                    )
                    response.raise_for_status()
                    data = response.json() or {}
                    matches = (
                        data.get("result", {})
                        .get("addressMatches", [])
                    )
                    if matches:
                        match = matches[0]
                        coords = (match.get("coordinates") or {})
                        if "y" in coords and "x" in coords:
                            lat = float(coords["y"])
                            lon = float(coords["x"])
                            log.info(
                                "Geocoded '%s' via Census (%s) to %s, %s (%s)",
                                location_query,
                                c_query,
                                lat,
                                lon,
                                match.get("matchedAddress", ""),
                            )
                            return (lat, lon)
                except requests.RequestException as e:
                    log.warning("Census geocode failed for '%s' query '%s': %s", location_query, c_query, e)

        # Fallback provider: ArcGIS World Geocoder (no key required for low-volume usage).
        arcgis_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
        seen_arcgis = set()
        for query in query_variants:
            query = (query or "").strip()
            if not query or query in seen_arcgis:
                continue
            seen_arcgis.add(query)
            try:
                response = requests.get(
                    arcgis_url,
                    params={
                        "SingleLine": query,
                        "f": "json",
                        "maxLocations": 5,
                        "outFields": "Match_addr,Addr_type,City,Region,Country",
                    },
                    headers=headers,
                    timeout=HTTP_TIMEOUT_GEOCODE_SEC,
                )
                response.raise_for_status()
                data = response.json() or {}
                candidates = data.get("candidates") or []
                if not candidates:
                    continue

                # Prefer high-score candidates and expected US state when available.
                expected_state = us_hint.get("state")
                best = None
                best_score = -10_000.0
                for cand in candidates:
                    score = float(cand.get("score") or 0.0)
                    attrs = cand.get("attributes") or {}
                    region = str(attrs.get("Region") or "").upper()
                    country = str(attrs.get("Country") or "").upper()
                    if looks_us:
                        if country in {"USA", "US", "UNITED STATES"}:
                            score += 20
                        else:
                            score -= 80
                    if expected_state:
                        if region == expected_state:
                            score += 30
                        elif region:
                            score -= 25
                    if score > best_score:
                        best_score = score
                        best = cand

                if best and best_score >= 70:
                    location = best.get("location") or {}
                    if "y" in location and "x" in location:
                        lat = float(location["y"])
                        lon = float(location["x"])
                        log.info(
                            "Geocoded '%s' via ArcGIS (%s) to %s, %s (%s)",
                            location_query,
                            query,
                            lat,
                            lon,
                            best.get("address", ""),
                        )
                        return (lat, lon)
            except requests.RequestException as e:
                log.warning("ArcGIS geocode failed for '%s' query '%s': %s", location_query, query, e)

        # Fallback provider: Photon
        photon_url = "https://photon.komoot.io/api/"
        for query in query_variants:
            photon_params = {
                "q": query,
                "limit": 3,
                "lang": "en",
            }
            response = requests.get(
                photon_url,
                params=photon_params,
                headers=headers,
                timeout=HTTP_TIMEOUT_GEOCODE_SEC,
            )
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


def _bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees from point A to point B (0-360)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    brng = math.degrees(math.atan2(y, x))
    return (brng + 360.0) % 360.0


def _downsample_polyline(polyline: list[tuple[float, float]], max_points: int = MAX_ROUTE_POLYLINE_POINTS) -> list[tuple[float, float]]:
    """Reduce route payload size while preserving start/end and overall shape."""
    if not polyline or len(polyline) <= max_points:
        return polyline

    stride = (len(polyline) - 1) / float(max_points - 1)
    reduced = []
    for i in range(max_points):
        reduced.append(polyline[int(round(i * stride))])

    reduced[0] = polyline[0]
    reduced[-1] = polyline[-1]
    return reduced


def _point_along_polyline(polyline: list[tuple[float, float]], target_km: float) -> tuple[float, float]:
    """Interpolate a point along polyline at target distance from start."""
    if not polyline:
        return (0.0, 0.0)
    if len(polyline) == 1 or target_km <= 0:
        return polyline[0]

    traveled = 0.0
    for i in range(1, len(polyline)):
        a_lat, a_lon = polyline[i - 1]
        b_lat, b_lon = polyline[i]
        seg_km = haversine_distance(a_lat, a_lon, b_lat, b_lon)
        if seg_km <= 0:
            continue

        if traveled + seg_km >= target_km:
            t = (target_km - traveled) / seg_km
            lat = a_lat + t * (b_lat - a_lat)
            lon = a_lon + t * (b_lon - a_lon)
            return (lat, lon)
        traveled += seg_km

    return polyline[-1]


def _weather_label_from_code(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return OPEN_METEO_WEATHER_CODE_LABELS.get(int(code), "Unknown")


def get_route_weather_timeline(
    polyline: list[tuple[float, float]],
    total_distance_km: float,
    duration_sec: int,
    interval_miles: float = WEATHER_SAMPLE_INTERVAL_MILES,
    max_points: int = WEATHER_MAX_CHECKPOINTS,
) -> list[dict]:
    """Sample weather along route at start, ~every N miles, and destination ETA."""
    if not polyline or total_distance_km <= 0:
        return []

    interval_km = max(1.0, interval_miles * 1.60934)
    checkpoints_km = [0.0]
    cursor = interval_km
    while cursor < total_distance_km:
        checkpoints_km.append(cursor)
        cursor += interval_km
    if checkpoints_km[-1] < total_distance_km:
        checkpoints_km.append(total_distance_km)

    # Keep UI compact on long routes.
    if len(checkpoints_km) > max_points:
        stride = (len(checkpoints_km) - 1) / float(max_points - 1)
        reduced = []
        for i in range(max_points):
            reduced.append(checkpoints_km[int(round(i * stride))])
        checkpoints_km = sorted(set(reduced))
        if checkpoints_km[0] != 0.0:
            checkpoints_km.insert(0, 0.0)
        if checkpoints_km[-1] != total_distance_km:
            checkpoints_km.append(total_distance_km)

    now_utc = datetime.utcnow()
    timeline = []

    for idx, km_mark in enumerate(checkpoints_km):
        lat, lon = _point_along_polyline(polyline, km_mark)
        progress = km_mark / total_distance_km if total_distance_km > 0 else 0.0
        eta = now_utc + timedelta(seconds=int(duration_sec * progress))

        label = "Start"
        if idx == len(checkpoints_km) - 1:
            label = "Destination"
        elif idx > 0:
            label = f"Mile {int(round(km_mark * 0.621371))}"

        timeline.append(
            {
                "label": label,
                "distance_miles": round(km_mark * 0.621371, 1),
                "eta_utc": eta.strftime("%Y-%m-%d %H:%M UTC"),
                "eta_iso": eta.replace(minute=0, second=0, microsecond=0).isoformat(),
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "temp_c": None,
                "is_day": None,
                "weather": "Unknown",
                "wind_kmh": None,
                "wind_dir_deg": None,
                "route_bearing_deg": None,
                "wind_impact": "low",
                "wind_context": "",
                "driving_alert": "",
                "precipitation_pct": None,
                "precipitation_mm": None,
                "humidity_pct": None,
                "pressure_hpa": None,
            }
        )

    # Estimate route direction at each checkpoint for wind impact context.
    for i, entry in enumerate(timeline):
        if len(timeline) == 1:
            continue
        if i == len(timeline) - 1:
            prev_pt = timeline[i - 1]
            next_pt = entry
        else:
            prev_pt = entry
            next_pt = timeline[i + 1]

        entry["route_bearing_deg"] = round(
            _bearing_degrees(prev_pt["lat"], prev_pt["lon"], next_pt["lat"], next_pt["lon"]),
            1,
        )

    def _enrich_weather(entry: dict) -> None:
        try:
            response = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": entry["lat"],
                    "longitude": entry["lon"],
                    "hourly": "temperature_2m,weather_code,precipitation_probability,precipitation,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,is_day",
                    "forecast_days": 3,
                    "timezone": "UTC",
                },
                timeout=HTTP_TIMEOUT_WEATHER_SEC,
            )
            response.raise_for_status()
            data = response.json() or {}
            hourly = data.get("hourly") or {}

            times = hourly.get("time") or []
            temps = hourly.get("temperature_2m") or []
            codes = hourly.get("weather_code") or []
            precips = hourly.get("precipitation_probability") or []
            precip_amounts = hourly.get("precipitation") or []
            humidities = hourly.get("relative_humidity_2m") or []
            pressures = hourly.get("surface_pressure") or []
            winds = hourly.get("wind_speed_10m") or []
            wind_dirs = hourly.get("wind_direction_10m") or []
            is_day_vals = hourly.get("is_day") or []

            if not times:
                return

            target_hour = datetime.fromisoformat(entry["eta_iso"])
            best_idx = 0
            best_delta = None
            for i, t in enumerate(times):
                try:
                    dt = datetime.fromisoformat(t)
                except ValueError:
                    continue
                delta = abs((dt - target_hour).total_seconds())
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_idx = i

            temp_val = temps[best_idx] if best_idx < len(temps) else None
            code_val = codes[best_idx] if best_idx < len(codes) else None
            precip_val = precips[best_idx] if best_idx < len(precips) else None
            precip_mm_val = precip_amounts[best_idx] if best_idx < len(precip_amounts) else None
            humidity_val = humidities[best_idx] if best_idx < len(humidities) else None
            pressure_val = pressures[best_idx] if best_idx < len(pressures) else None
            wind_val = winds[best_idx] if best_idx < len(winds) else None
            wind_dir_val = wind_dirs[best_idx] if best_idx < len(wind_dirs) else None
            is_day_val = is_day_vals[best_idx] if best_idx < len(is_day_vals) else None

            entry["temp_c"] = round(float(temp_val), 1) if temp_val is not None else None
            entry["weather"] = _weather_label_from_code(code_val)
            entry["precipitation_pct"] = int(round(float(precip_val))) if precip_val is not None else None
            entry["precipitation_mm"] = round(float(precip_mm_val), 2) if precip_mm_val is not None else None
            entry["humidity_pct"] = int(round(float(humidity_val))) if humidity_val is not None else None
            entry["pressure_hpa"] = round(float(pressure_val), 1) if pressure_val is not None else None
            entry["wind_kmh"] = round(float(wind_val), 1) if wind_val is not None else None
            entry["wind_dir_deg"] = round(float(wind_dir_val), 1) if wind_dir_val is not None else None
            if is_day_val is not None:
                entry["is_day"] = bool(int(is_day_val))

            wind_speed = float(entry["wind_kmh"] or 0.0)
            bearing = entry.get("route_bearing_deg")
            wind_dir = entry.get("wind_dir_deg")

            # Tightened thresholds so moderate winds surface sooner.
            if wind_speed >= 40:
                entry["wind_impact"] = "high"
            elif wind_speed >= 20:
                entry["wind_impact"] = "medium"
            else:
                entry["wind_impact"] = "low"

            if bearing is not None and wind_dir is not None:
                # Convert "from" wind direction to where wind is blowing toward.
                wind_to = (float(wind_dir) + 180.0) % 360.0
                angle = abs((wind_to - float(bearing) + 180.0) % 360.0 - 180.0)
                if angle <= 45:
                    entry["wind_context"] = "headwind"
                    if wind_speed >= 15 and entry["wind_impact"] == "low":
                        entry["wind_impact"] = "medium"
                    if entry["wind_impact"] == "medium" and wind_speed >= 30:
                        entry["wind_impact"] = "high"
                elif angle >= 135:
                    entry["wind_context"] = "tailwind"
                else:
                    entry["wind_context"] = "crosswind"

            weather_text = (entry.get("weather") or "").lower()
            precip = int(entry.get("precipitation_pct") or 0)
            alert_parts = []
            if any(k in weather_text for k in ("snow", "thunder", "fog")):
                alert_parts.append("visibility/traction risk")
            if any(k in weather_text for k in ("rain", "drizzle")) and precip >= 35:
                alert_parts.append("wet roads")
            if entry["wind_context"] == "headwind" and wind_speed >= 15 and entry["wind_impact"] == "medium":
                alert_parts.append("minor range impact from headwind")
            elif entry["wind_impact"] == "high":
                if entry["wind_context"] == "headwind":
                    alert_parts.append("range impact from headwind")
                elif entry["wind_context"] == "crosswind":
                    alert_parts.append("stability impact from crosswind")
                else:
                    alert_parts.append("wind impact")

            if alert_parts:
                entry["driving_alert"] = " | ".join(alert_parts)
        except Exception as exc:
            log.warning("Route weather fetch failed at %.4f,%.4f: %s", entry["lat"], entry["lon"], exc)

    worker_count = max(1, min(WEATHER_FETCH_WORKERS, len(timeline)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(_enrich_weather, timeline))

    for entry in timeline:
        entry.pop("eta_iso", None)

    return timeline


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
        "steps": [{"instruction": str, "distance_km": float, "duration_min": int}, ...],
    }
    """
    def _format_osrm_instruction(step: dict) -> str:
        maneuver = step.get("maneuver") or {}
        mtype = (maneuver.get("type") or "").replace("_", " ")
        modifier = (maneuver.get("modifier") or "").replace("_", " ")
        road = (step.get("name") or "").strip()

        if mtype in {"depart", "arrive"}:
            instruction = mtype.capitalize()
        elif modifier:
            instruction = f"{mtype.capitalize()} {modifier}".strip()
        else:
            instruction = mtype.capitalize() if mtype else "Continue"

        if road:
            instruction = f"{instruction} onto {road}"
        return instruction.strip()

    # Primary provider: OSRM public router (road-accurate geometry + steps, no key).
    try:
        url = (
            "https://router.project-osrm.org/route/v1/driving/"
            f"{start_lon},{start_lat};{end_lon},{end_lat}"
        )
        params = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "true",
        }

        response = requests.get(url, params=params, timeout=HTTP_TIMEOUT_ROUTE_SEC)
        response.raise_for_status()
        data = response.json()

        routes = data.get("routes") or []
        if routes:
            route = routes[0]
            coords = (route.get("geometry") or {}).get("coordinates") or []
            polyline = _downsample_polyline([(lat, lon) for lon, lat in coords])

            distance_km = float(route.get("distance", 0)) / 1000
            duration_sec = int(route.get("duration", 0))

            steps: list[dict] = []
            for leg in route.get("legs", []) or []:
                for step in leg.get("steps", []) or []:
                    steps.append(
                        {
                            "instruction": _format_osrm_instruction(step),
                            "distance_km": round(float(step.get("distance", 0)) / 1000, 2),
                            "duration_min": max(1, int(round(float(step.get("duration", 0)) / 60))),
                        }
                    )

            return {
                "distance_km": distance_km,
                "duration_sec": duration_sec,
                "polyline": polyline,
                "elevation_gain_m": 0,
                "steps": steps,
            }

        log.warning("No route found in OSRM response")
    except Exception as e:
        log.warning(f"OSRM failed: {e}")

    # Optional fallback: OpenRouteService if OPENROUTESERVICE_API_KEY is configured.
    if OPENROUTESERVICE_API_KEY:
        try:
            url = "https://api.openrouteservice.org/v2/directions/driving-car"
            params = {
                "start": f"{start_lon},{start_lat}",
                "end": f"{end_lon},{end_lat}",
                "api_key": OPENROUTESERVICE_API_KEY,
                "format": "geojson",
                "geometry_format": "geojson",
            }

            response = requests.get(url, params=params, timeout=HTTP_TIMEOUT_ROUTE_SEC)
            response.raise_for_status()
            data = response.json()

            if "features" in data and data["features"]:
                route = data["features"][0]
                coords = route["geometry"]["coordinates"]
                polyline = _downsample_polyline([(lat, lon) for lon, lat in coords])

                segment = route["properties"]["segments"][0]
                distance_km = float(segment.get("distance", 0)) / 1000
                duration_sec = int(segment.get("duration", 0))

                steps = []
                for step in segment.get("steps", []) or []:
                    steps.append(
                        {
                            "instruction": step.get("instruction", "Continue"),
                            "distance_km": round(float(step.get("distance", 0)) / 1000, 2),
                            "duration_min": max(1, int(round(float(step.get("duration", 0)) / 60))),
                        }
                    )

                return {
                    "distance_km": distance_km,
                    "duration_sec": duration_sec,
                    "polyline": polyline,
                    "elevation_gain_m": distance_km * 20,
                    "steps": steps,
                }
        except Exception as e:
            log.warning(f"OpenRouteService failed: {e}")

    # Last-resort fallback if routing providers are unavailable.
    try:
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
            "steps": [
                {
                    "instruction": "Approximate route fallback used (road network unavailable)",
                    "distance_km": round(distance_km, 2),
                    "duration_min": max(1, int(round(duration_sec / 60))),
                }
            ],
        }
    except Exception as e:
        log.warning("Fallback route generation failed: %s", e)
        return None


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
    lat: float,
    lon: float,
    radius_km: float = 50,
    limit: int = 5,
    preferred_connector_type: str = DEFAULT_CONNECTOR_TYPE,
    min_charger_kw: float = DEFAULT_MIN_CHARGER_KW,
) -> list[dict]:
    """Find chargers near a location using PostGIS spatial query.
    
    Uses PostGIS ST_DWithin() for efficient spatial indexing.
    Falls back to Haversine distance if PostGIS unavailable.
    """
    try:
        connector_candidates = _connector_candidates(preferred_connector_type)
        connector_filter_key = str(preferred_connector_type or "").strip().upper()
        connector_candidates_param = connector_candidates if connector_candidates else [""]
        
        # Convert radius from km to meters for ST_DWithin
        radius_m = radius_km * 1000

        # Try PostGIS ST_DWithin() first (5-10x faster with spatial index)
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
                -- Calculate distance in km for display (using geometry degrees)
                ST_Distance(
                    ST_Point(%s, %s),
                    ST_Point(s.longitude, s.latitude)
                ) * 111.32 AS distance_km
            FROM ev_stations s
            JOIN ev_charger_connectors c ON s.id = c.station_id
            WHERE 
                -- Use PostGIS spatial index for efficient filtering
                ST_DWithin(
                    ST_Point(%s, %s),
                    ST_Point(s.longitude, s.latitude),
                    %s / 111320.0
                )
                AND COALESCE(c.power_kw, 0) >= %s
                AND (%s = '' OR %s = 'ANY' OR UPPER(c.connector_type) = ANY(%s))
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
        """, (
            DC_FAST_CHARGER_KW,
            lon, lat,  # For ST_Point() in distance calculation
            lon, lat,  # For ST_Point() in ST_DWithin()
            radius_m,  # ST_DWithin expects meters
            float(min_charger_kw),
            connector_filter_key,
            connector_filter_key,
            connector_candidates_param,
            limit,
        ))
        
        return chargers if chargers else []
    
    except Exception as e:
        log.error(f"Charger search failed (ST_DWithin): {e}, falling back to Haversine")
        # Fallback to Haversine formula if PostGIS fails (for compatibility)
        try:
            connector_candidates = _connector_candidates(preferred_connector_type)
            connector_filter_key = str(preferred_connector_type or "").strip().upper()
            connector_candidates_param = connector_candidates if connector_candidates else [""]

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
                JOIN ev_charger_connectors c ON s.id = c.station_id
                WHERE 
                    SQRT(
                        POW(s.latitude - %s, 2) + 
                        POW(s.longitude - %s, 2)
                    ) * 111 < %s
                    AND COALESCE(c.power_kw, 0) >= %s
                    AND (%s = '' OR %s = 'ANY' OR UPPER(c.connector_type) = ANY(%s))
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
            """, (
                DC_FAST_CHARGER_KW,
                lat,
                lon,
                lat,
                lon,
                radius_km,
                float(min_charger_kw),
                connector_filter_key,
                connector_filter_key,
                connector_candidates_param,
                limit,
            ))
            
            return chargers if chargers else []
        except Exception as fallback_e:
            log.error(f"Charger search fallback also failed: {fallback_e}")
            return []


def optimize_charging_stops(
    polyline: list[tuple[float, float]],
    total_distance_km: float,
    current_soc_percent: float,
    energy_needed_kwh: float,
    preferred_connector_type: str = DEFAULT_CONNECTOR_TYPE,
    min_charger_kw: float = DEFAULT_MIN_CHARGER_KW,
) -> list[ChargingStop]:
        """Determine optimal charging stops along route.

        When a real charger is found in the database near the needed stop location it
        is used.  When none is found the stop is modelled as an *estimated* virtual
        stop so the SOC/timing information is still meaningful even in areas with
        sparse database coverage.

        Returns list of recommended stops with:
        - Location (real charger or estimated)
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

            # ── SOC math (always computed, regardless of real charger availability) ──
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

            # ── Try to find a real charger near this location ──
            nearby: list[dict] = []
            for radius in (25, 40, 60):
                nearby = find_nearby_chargers(
                    stop_lat,
                    stop_lon,
                    radius_km=radius,
                    limit=10,
                    preferred_connector_type=preferred_connector_type,
                    min_charger_kw=min_charger_kw,
                )
                if nearby:
                    break

            if nearby:
                # Prefer high-power chargers among nearest options.
                charger = max(
                    nearby,
                    key=lambda c: (
                        float(c.get("max_power_kw") or 0),
                        -float(c.get("distance_km") or 0),
                    ),
                )
                charger_power = max(float(charger.get("max_power_kw") or DC_FAST_CHARGER_KW), 25.0)
                charge_time_min = int(round((energy_to_add / charger_power) * 60))
                stop = ChargingStop(
                    charger_id=charger["id"],
                    charger_name=charger["name"],
                    lat=float(charger["latitude"]),
                    lon=float(charger["longitude"]),
                    distance_km_from_start=stop_location_km,
                    arrival_soc_percent=round(arrival_soc, 1),
                    charge_to_soc_percent=round(charge_to_soc, 1),
                    charger_power_kw=charger_power,
                    charge_time_min=max(1, charge_time_min),
                    network=charger.get("network", "Unknown"),
                    address=charger.get("address", ""),
                )
                log.info(
                    "Stop %s: %s at km %.0f (%.0f mi), arrive %.0f%%, charge to %.0f%% (%s min)",
                    len(stops) + 1, charger["name"], stop_location_km, stop_location_km * 0.621371,
                    arrival_soc, charge_to_soc, stop.charge_time_min,
                )
            else:
                # No DB charger found – create an estimated virtual stop so the
                # SOC projection is still meaningful.
                charger_power = DC_FAST_CHARGER_KW
                charge_time_min = int(round((energy_to_add / charger_power) * 60))
                stop = ChargingStop(
                    charger_id=0,
                    charger_name="Estimated Charging Stop",
                    lat=round(stop_lat, 5),
                    lon=round(stop_lon, 5),
                    distance_km_from_start=stop_location_km,
                    arrival_soc_percent=round(arrival_soc, 1),
                    charge_to_soc_percent=round(charge_to_soc, 1),
                    charger_power_kw=charger_power,
                    charge_time_min=max(1, charge_time_min),
                    network="Any DCFC",
                    address="Estimated location along route",
                )
                log.info(
                    "Stop %s: ESTIMATED at km %.0f (%.0f mi), arrive %.0f%%, charge to %.0f%% (%s min)",
                    len(stops) + 1, stop_location_km, stop_location_km * 0.621371,
                    arrival_soc, charge_to_soc, stop.charge_time_min,
                )

            stops.append(stop)
            distance_traveled = stop_location_km
            current_soc = charge_to_soc

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


def _attach_weather_to_route_steps(route_steps: list[dict], route_weather: list[dict]) -> list[dict]:
    """Attach nearest weather checkpoint stats to each route step for compact turn-level context."""
    if not route_steps or not route_weather:
        return route_steps

    cumulative_km = 0.0
    for step in route_steps:
        step_km = float(step.get("distance_km") or 0.0)
        cumulative_km += max(0.0, step_km)
        target_miles = cumulative_km * 0.621371

        nearest = min(
            route_weather,
            key=lambda wx: abs(float(wx.get("distance_miles") or 0.0) - target_miles),
        )

        step["step_weather"] = nearest.get("weather")
        step["step_temp_c"] = nearest.get("temp_c")
        step["step_wind_kmh"] = nearest.get("wind_kmh")
        step["step_precipitation_pct"] = nearest.get("precipitation_pct")
        step["step_is_day"] = nearest.get("is_day")

    return route_steps


def _attach_soc_to_route_steps(
    route_steps: list[dict],
    start_soc_percent: float,
    total_distance_km: float,
    energy_needed_kwh: float,
    charging_stops: list[ChargingStop] | None = None,
) -> list[dict]:
    """Attach estimated SOC at each step, accounting for planned charging stops."""
    if not route_steps or total_distance_km <= 0:
        return route_steps

    kwh_per_km = energy_needed_kwh / total_distance_km if energy_needed_kwh > 0 else DEFAULT_EFFICIENCY_KWH_PER_KM
    cumulative_km = 0.0

    stop_boosts: list[tuple[float, float]] = []
    for stop in charging_stops or []:
        boost = max(0.0, float(stop.charge_to_soc_percent) - float(stop.arrival_soc_percent))
        stop_boosts.append((float(stop.distance_km_from_start), boost))

    stop_boosts.sort(key=lambda x: x[0])

    for step in route_steps:
        step_km = max(0.0, float(step.get("distance_km") or 0.0))
        cumulative_km += step_km

        consumed_soc = (cumulative_km * kwh_per_km / BATTERY_CAPACITY_KWH * 100) if BATTERY_CAPACITY_KWH > 0 else 0.0
        recovered_soc = sum(boost for km_mark, boost in stop_boosts if km_mark <= cumulative_km)

        est_soc = max(0.0, min(100.0, float(start_soc_percent) - consumed_soc + recovered_soc))
        step["step_soc_percent"] = round(est_soc, 1)

    return route_steps


# ── Main Trip Planning ─────────────────────────────────────────────

def plan_trip(
    source: str,
    destination: str,
    current_soc_percent: float = 100,
    current_temp_c: float = 15,
    preferred_connector_type: str = DEFAULT_CONNECTOR_TYPE,
    min_charger_kw: float = DEFAULT_MIN_CHARGER_KW,
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

    t0 = time.perf_counter()
    
    # Geocode locations
    t_geocode = time.perf_counter()
    start_coords = geocode_location(source)
    end_coords = geocode_location(destination)
    geocode_elapsed = time.perf_counter() - t_geocode
    
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
            baseline_energy_needed_kwh=0,
            ml_energy_needed_kwh=0,
            energy_needed_kwh=0,
            charging_stops=[],
            baseline_charging_stops=[],
            ml_charging_stops=[],
            feasible=False,
            feasibility_reason=(
                f"Could not geolocate {missing_text}. Try 'City, ST' or 'lat,lon' format."
            ),
            polyline=[],
            route_steps=[],
            baseline_route_steps=[],
            ml_route_steps=[],
            route_weather=[],
            weather_summary="",
            estimate_source="Baseline",
            estimate_confidence="n/a",
            ml_training_drives=0,
            ml_estimate_note="ML estimate unavailable; using baseline",
            created_at=datetime.now().isoformat()
        )
    
    start_lat, start_lon = start_coords
    end_lat, end_lon = end_coords
    
    log.info(f"Start: {start_lat:.4f}, {start_lon:.4f}")
    log.info(f"End: {end_lat:.4f}, {end_lon:.4f}")
    
    # Get route
    t_route = time.perf_counter()
    route = get_route(start_lat, start_lon, end_lat, end_lon)
    route_elapsed = time.perf_counter() - t_route
    if not route:
        return TripPlan(
            source_name=source,
            destination_name=destination,
            total_distance_km=0,
            estimated_duration_min=0,
            start_soc_percent=current_soc_percent,
            arrival_soc_percent=0,
            baseline_energy_needed_kwh=0,
            ml_energy_needed_kwh=0,
            energy_needed_kwh=0,
            charging_stops=[],
            baseline_charging_stops=[],
            ml_charging_stops=[],
            feasible=False,
            feasibility_reason="Could not calculate route",
            polyline=[],
            route_steps=[],
            baseline_route_steps=[],
            ml_route_steps=[],
            route_weather=[],
            weather_summary="",
            estimate_source="Baseline",
            estimate_confidence="n/a",
            ml_training_drives=0,
            ml_estimate_note="ML estimate unavailable; using baseline",
            created_at=datetime.now().isoformat()
        )
    
    distance_km = route["distance_km"]
    duration_sec = route["duration_sec"]
    polyline = route["polyline"]
    route_steps = route.get("steps") or []
    t_weather = time.perf_counter()
    route_weather = get_route_weather_timeline(polyline, distance_km, duration_sec)
    weather_elapsed = time.perf_counter() - t_weather
    route_steps = _attach_weather_to_route_steps(route_steps, route_weather)

    if route_weather:
        start_weather = route_weather[0]
        end_weather = route_weather[-1]
        start_temp = f"{start_weather['temp_c']:.1f}°C" if start_weather.get("temp_c") is not None else "n/a"
        end_temp = f"{end_weather['temp_c']:.1f}°C" if end_weather.get("temp_c") is not None else "n/a"
        weather_summary = (
            f"Start: {start_weather.get('weather', 'Unknown')} {start_temp} • "
            f"Arrive: {end_weather.get('weather', 'Unknown')} {end_temp}"
        )
    else:
        weather_summary = "Route weather unavailable"
    

    # Baseline estimate: distance + fixed efficiency.
    baseline_energy_kwh = distance_km * DEFAULT_EFFICIENCY_KWH_PER_KM
    min_plausible_kwh = distance_km * MIN_EFFICIENCY_KWH_PER_KM
    max_plausible_kwh = distance_km * MAX_EFFICIENCY_KWH_PER_KM
    baseline_energy_kwh = max(min_plausible_kwh, min(max_plausible_kwh, baseline_energy_kwh))

    ml_energy_kwh = None
    estimate_source = "Baseline"
    estimate_confidence = "n/a"
    ml_training_drives = 0
    ml_estimate_note = "ML estimate unavailable; using baseline"

    # Step 1: ML prediction (if available)
    try:
        if energy_model.is_available():
            avg_speed_kmh = (distance_km / (duration_sec / 3600.0)) if duration_sec > 0 else 0.0
            temps = [float(wx.get("temp_c")) for wx in route_weather if wx.get("temp_c") is not None]
            avg_temp_c = (sum(temps) / len(temps)) if temps else float(current_temp_c)

            humidities = [float(wx.get("humidity_pct")) for wx in route_weather if wx.get("humidity_pct") is not None]
            precip_mm = [float(wx.get("precipitation_mm")) for wx in route_weather if wx.get("precipitation_mm") is not None]
            pressures = [float(wx.get("pressure_hpa")) for wx in route_weather if wx.get("pressure_hpa") is not None]

            avg_humidity_pct = (sum(humidities) / len(humidities)) if humidities else 50.0
            avg_precip_mm = (sum(precip_mm) / len(precip_mm)) if precip_mm else 0.0
            avg_pressure_hpa = (sum(pressures) / len(pressures)) if pressures else 1013.0

            headwind_components = []
            tailwind_components = []
            sidewind_components = []
            wind_speeds = []
            for wx in route_weather:
                wind_kmh = wx.get("wind_kmh")
                wind_dir = wx.get("wind_dir_deg")
                route_bearing = wx.get("route_bearing_deg")
                if wind_kmh is None:
                    continue
                wind_speed = float(wind_kmh)
                wind_speeds.append(wind_speed)
                if wind_dir is None or route_bearing is None:
                    continue

                wind_to = (float(wind_dir) + 180.0) % 360.0
                angle = math.radians(abs((wind_to - float(route_bearing) + 180.0) % 360.0 - 180.0))
                along = wind_speed * math.cos(angle)
                cross = abs(wind_speed * math.sin(angle))

                headwind_components.append(abs(along) if along < 0 else 0.0)
                tailwind_components.append(along if along > 0 else 0.0)
                sidewind_components.append(cross)

            avg_wind_speed_kmh = (sum(wind_speeds) / len(wind_speeds)) if wind_speeds else 0.0
            avg_headwind_kmh = (sum(headwind_components) / len(headwind_components)) if headwind_components else 0.0
            avg_tailwind_kmh = (sum(tailwind_components) / len(tailwind_components)) if tailwind_components else 0.0
            avg_sidewind_kmh = (sum(sidewind_components) / len(sidewind_components)) if sidewind_components else 0.0

            ml_pred = energy_model.predict_energy(
                distance_km=distance_km,
                avg_speed_kmh=max(1.0, avg_speed_kmh),
                avg_ambient_temp_c=avg_temp_c,
                avg_outside_temp_c=avg_temp_c,
                duration_min=max(1.0, duration_sec / 60.0),
                weather_temp_c=avg_temp_c,
                weather_humidity_pct=avg_humidity_pct,
                weather_pressure_hpa=avg_pressure_hpa,
                precipitation_mm=avg_precip_mm,
                wind_speed_avg_kmh=avg_wind_speed_kmh,
                headwind_component_kmh=avg_headwind_kmh,
                tailwind_component_kmh=avg_tailwind_kmh,
                sidewind_component_kmh=avg_sidewind_kmh,
            )

            ml_raw_energy_kwh = float(ml_pred.get("energy_used_kwh", baseline_energy_kwh))
            estimate_confidence = str(ml_pred.get("confidence") or "unknown").lower()
            training_drives = int((ml_pred.get("model_info") or {}).get("num_training_drives") or 0)
            ml_training_drives = training_drives

            # Clamp ML estimate so undertrained models cannot create extreme plans.
            deviation_floor = baseline_energy_kwh * (1.0 - ML_ENERGY_DEVIATION_LIMIT)
            deviation_ceiling = baseline_energy_kwh * (1.0 + ML_ENERGY_DEVIATION_LIMIT)
            ml_energy_kwh = max(deviation_floor, min(deviation_ceiling, ml_raw_energy_kwh))
            ml_energy_kwh = max(min_plausible_kwh, min(max_plausible_kwh, ml_energy_kwh))

            ml_reliable = (
                training_drives >= MIN_ML_TRAINING_DRIVES
                and estimate_confidence in ML_MIN_CONFIDENCE_LEVELS
            )

            if ml_reliable:
                estimate_source = "ML"
                ml_estimate_note = (
                    f"ML active ({training_drives} training drives, confidence {estimate_confidence})"
                )
            else:
                estimate_source = "Baseline"
                ml_estimate_note = (
                    f"ML shown for comparison only ({training_drives} training drives, confidence {estimate_confidence}); "
                    "baseline used for primary planning"
                )
    except Exception as exc:
        log.warning("ML prediction unavailable, using baseline estimate: %s", exc)

    # Use ML only when model quality is sufficient; otherwise use baseline for primary planning.
    use_ml_for_primary = estimate_source == "ML" and ml_energy_kwh is not None
    energy_needed_kwh = ml_energy_kwh if use_ml_for_primary else baseline_energy_kwh

    # Step 2: Calculate charging stops for BOTH baseline and ML estimates
    baseline_charging_stops = []
    baseline_feasible_direct = _is_direct_trip_feasible(current_soc_percent, baseline_energy_kwh)
    if not baseline_feasible_direct:
        baseline_charging_stops = optimize_charging_stops(
            polyline=polyline,
            total_distance_km=distance_km,
            current_soc_percent=current_soc_percent,
            energy_needed_kwh=baseline_energy_kwh,
            preferred_connector_type=preferred_connector_type,
            min_charger_kw=min_charger_kw,
        ) or []

    ml_charging_stops = []
    ml_energy_for_stops = ml_energy_kwh if ml_energy_kwh is not None else baseline_energy_kwh
    ml_feasible_direct = _is_direct_trip_feasible(current_soc_percent, ml_energy_for_stops)
    if not ml_feasible_direct:
        ml_charging_stops = optimize_charging_stops(
            polyline=polyline,
            total_distance_km=distance_km,
            current_soc_percent=current_soc_percent,
            energy_needed_kwh=ml_energy_for_stops,
            preferred_connector_type=preferred_connector_type,
            min_charger_kw=min_charger_kw,
        ) or []

    # Use the active estimate's stops
    feasible_direct = _is_direct_trip_feasible(current_soc_percent, energy_needed_kwh)
    charging_stops = ml_charging_stops if use_ml_for_primary else baseline_charging_stops
    arrival_soc = _estimate_arrival_soc(current_soc_percent, energy_needed_kwh)
    feasible = feasible_direct

    # Step 3: Assess feasibility with stops if needed
    if not feasible_direct:
        if charging_stops:
            feasible_with_stops, arrival_soc_with_stops = _can_complete_with_stops(
                total_distance_km=distance_km,
                start_soc_percent=current_soc_percent,
                energy_needed_kwh=energy_needed_kwh,
                stops=charging_stops,
            )
            feasible = feasible_with_stops
            if feasible_with_stops:
                arrival_soc = arrival_soc_with_stops
    
    # Calculate route steps with SOC for baseline estimate (with baseline charging stops)
    baseline_route_steps = _attach_soc_to_route_steps(
        route_steps=route_steps,
        start_soc_percent=current_soc_percent,
        total_distance_km=distance_km,
        energy_needed_kwh=baseline_energy_kwh,
        charging_stops=baseline_charging_stops,
    )
    
    # Calculate route steps with SOC for ML estimate (with ML charging stops)
    ml_route_steps = _attach_soc_to_route_steps(
        route_steps=route_steps,
        start_soc_percent=current_soc_percent,
        total_distance_km=distance_km,
        energy_needed_kwh=ml_energy_for_stops,
        charging_stops=ml_charging_stops,
    ) if ml_energy_kwh is not None else baseline_route_steps
    
    # Use the active estimate (ML if available, else baseline) for display
    route_steps = _attach_soc_to_route_steps(
        route_steps=route_steps,
        start_soc_percent=current_soc_percent,
        total_distance_km=distance_km,
        energy_needed_kwh=energy_needed_kwh,
        charging_stops=charging_stops,
    )

    if feasible_direct:
        reason = f"{estimate_source} estimate generated successfully"
    elif charging_stops and feasible:
        reason = f"{estimate_source} estimate recommends {len(charging_stops)} charging stop(s)"
    elif not charging_stops:
        reason = f"{estimate_source} estimate generated (trip may require charging, but no suitable stops were found)"
    else:
        reason = f"{estimate_source} estimate generated (route may still require additional charging flexibility)"

    plan = TripPlan(
        source_name=source,
        destination_name=destination,
        total_distance_km=distance_km,
        estimated_duration_min=int(duration_sec / 60),
        start_soc_percent=current_soc_percent,
        arrival_soc_percent=arrival_soc,
        baseline_energy_needed_kwh=baseline_energy_kwh,
        ml_energy_needed_kwh=ml_energy_kwh if ml_energy_kwh is not None else 0.0,
        energy_needed_kwh=energy_needed_kwh,
        charging_stops=charging_stops,
        baseline_charging_stops=baseline_charging_stops,
        ml_charging_stops=ml_charging_stops,
        feasible=feasible,
        feasibility_reason=reason,
        polyline=polyline,
        route_steps=route_steps,
        baseline_route_steps=baseline_route_steps,
        ml_route_steps=ml_route_steps,
        route_weather=route_weather,
        weather_summary=weather_summary,
        estimate_source=estimate_source,
        estimate_confidence=estimate_confidence,
        ml_training_drives=ml_training_drives,
        ml_estimate_note=ml_estimate_note,
        created_at=datetime.now().isoformat()
    )
    
    total_elapsed = time.perf_counter() - t0

    log.info(f"\n{'='*60}")
    log.info("Trip Plan Summary:")
    log.info(f"  Timing: geocode={geocode_elapsed:.2f}s route={route_elapsed:.2f}s weather={weather_elapsed:.2f}s total={total_elapsed:.2f}s")
    log.info(f"  Estimate source: {estimate_source} (confidence: {estimate_confidence})")
    log.info(f"  Distance: {distance_km:.1f} km")
    log.info(f"  Duration: {plan.estimated_duration_min} min")
    log.info(f"  Energy: {energy_needed_kwh:.1f} kWh")
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
