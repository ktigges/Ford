"""Timezone to country code inference for multi-region support.

Maps common timezone identifiers to ISO 3166-1 alpha-2 country codes.
Used for geo-based features (charger region, route planning bounds).

Author:      Kevin Tigges
Description: Ford Lightning EV Tool - Geo Utils
Version:     0.6.0.0
Date:        2026-05-09
"""

from typing import Optional

# Comprehensive timezone → country code mapping
# Timezone identifier → ISO 3166-1 alpha-2 country code
TIMEZONE_TO_COUNTRY = {
    # North America - USA
    "America/New_York": "US",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Los_Angeles": "US",
    "America/Anchorage": "US",
    "Pacific/Honolulu": "US",
    "America/Phoenix": "US",
    "America/Toronto": "CA",
    "America/Vancouver": "CA",
    "America/Mexico_City": "MX",
    
    # Europe
    "Europe/London": "GB",
    "Europe/Paris": "FR",
    "Europe/Berlin": "DE",
    "Europe/Madrid": "ES",
    "Europe/Rome": "IT",
    "Europe/Amsterdam": "NL",
    "Europe/Brussels": "BE",
    "Europe/Vienna": "AT",
    "Europe/Prague": "CZ",
    "Europe/Warsaw": "PL",
    "Europe/Zurich": "CH",
    "Europe/Stockholm": "SE",
    "Europe/Oslo": "NO",
    "Europe/Copenhagen": "DK",
    "Europe/Moscow": "RU",
    "Europe/Dublin": "IE",
    "Europe/Athens": "GR",
    "Europe/Istanbul": "TR",
    
    # Asia
    "Asia/Tokyo": "JP",
    "Asia/Shanghai": "CN",
    "Asia/Hong_Kong": "HK",
    "Asia/Singapore": "SG",
    "Asia/Bangkok": "TH",
    "Asia/Seoul": "KR",
    "Asia/Mumbai": "IN",
    "Asia/Dubai": "AE",
    "Asia/Bangkok": "TH",
    "Asia/Manila": "PH",
    "Asia/Taipei": "TW",
    
    # Australia/Oceania
    "Australia/Sydney": "AU",
    "Australia/Melbourne": "AU",
    "Australia/Perth": "AU",
    "Australia/Brisbane": "AU",
    "Pacific/Auckland": "NZ",
    "Pacific/Fiji": "FJ",
    
    # South America
    "America/Sao_Paulo": "BR",
    "America/Buenos_Aires": "AR",
    "America/Santiago": "CL",
    "America/Bogota": "CO",
    "America/Lima": "PE",
    
    # Africa
    "Africa/Cairo": "EG",
    "Africa/Johannesburg": "ZA",
    "Africa/Lagos": "NG",
    "Africa/Nairobi": "KE",
    
    # UTC and generic
    "UTC": "US",
    "Etc/UTC": "US",
}


def infer_country_code(timezone_name: Optional[str]) -> str:
    """Infer ISO 3166-1 alpha-2 country code from timezone name.

    Args:
        timezone_name: Timezone identifier (e.g., 'America/New_York')

    Returns:
        Country code (default 'US' if unable to infer or None provided)
    """
    if not timezone_name:
        return "US"
    
    # Exact match
    if timezone_name in TIMEZONE_TO_COUNTRY:
        return TIMEZONE_TO_COUNTRY[timezone_name]
    
    # Partial match (handle timezone aliases and variations)
    timezone_lower = timezone_name.lower()
    for tz, country in TIMEZONE_TO_COUNTRY.items():
        if tz.lower() == timezone_lower:
            return country
    
    # Default to US if unable to infer
    # (most EV infrastructure is currently US-focused)
    return "US"


def get_country_name(country_code: str) -> str:
    """Get full country name from ISO code."""
    codes = {
        "US": "United States",
        "CA": "Canada",
        "MX": "Mexico",
        "GB": "United Kingdom",
        "FR": "France",
        "DE": "Germany",
        "ES": "Spain",
        "IT": "Italy",
        "NL": "Netherlands",
        "BE": "Belgium",
        "AT": "Austria",
        "CH": "Switzerland",
        "SE": "Sweden",
        "NO": "Norway",
        "DK": "Denmark",
        "CZ": "Czech Republic",
        "PL": "Poland",
        "RU": "Russia",
        "IE": "Ireland",
        "GR": "Greece",
        "TR": "Turkey",
        "JP": "Japan",
        "CN": "China",
        "HK": "Hong Kong",
        "SG": "Singapore",
        "TH": "Thailand",
        "KR": "South Korea",
        "IN": "India",
        "AE": "United Arab Emirates",
        "PH": "Philippines",
        "TW": "Taiwan",
        "AU": "Australia",
        "NZ": "New Zealand",
        "FJ": "Fiji",
        "BR": "Brazil",
        "AR": "Argentina",
        "CL": "Chile",
        "CO": "Colombia",
        "PE": "Peru",
        "EG": "Egypt",
        "ZA": "South Africa",
        "NG": "Nigeria",
        "KE": "Kenya",
    }
    return codes.get(country_code, country_code)
