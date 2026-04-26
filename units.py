"""Unit conversion helpers for display-time metric ↔ imperial conversion.

Ford sends ALL numeric telemetry in SI / metric units regardless of the
vehicle's displaySystemOfMeasure setting.  We store raw metric in the DB
and convert only at presentation time.

Usage in templates via Jinja globals:
    {{ convert(value, 'km', 'mi') }}
    {{ unit_label('distance') }}

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.2.0
Date:        2026-04-26
"""


# ── Conversion factors ──────────────────────────────────────────────

_KM_TO_MI = 0.621371
_KPA_TO_PSI = 0.145038
_NM_TO_LBFT = 0.737562


def km_to_mi(val):
    """Kilometers → miles."""
    if val is None:
        return None
    return round(float(val) * _KM_TO_MI, 1)


def mi_to_km(val):
    """Miles → kilometers."""
    if val is None:
        return None
    return round(float(val) / _KM_TO_MI, 1)


def kmh_to_mph(val):
    """km/h → mph."""
    if val is None:
        return None
    return round(float(val) * _KM_TO_MI, 1)


def mph_to_kmh(val):
    """mph → km/h."""
    if val is None:
        return None
    return round(float(val) / _KM_TO_MI, 1)


def c_to_f(val):
    """Celsius → Fahrenheit."""
    if val is None:
        return None
    return round(float(val) * 9.0 / 5.0 + 32, 1)


def f_to_c(val):
    """Fahrenheit → Celsius."""
    if val is None:
        return None
    return round((float(val) - 32) * 5.0 / 9.0, 1)


def kpa_to_psi(val):
    """Kilopascals → PSI."""
    if val is None:
        return None
    return round(float(val) * _KPA_TO_PSI, 1)


def psi_to_kpa(val):
    """PSI → kilopascals."""
    if val is None:
        return None
    return round(float(val) / _KPA_TO_PSI, 1)


def m_to_ft(val):
    """Meters → feet."""
    if val is None:
        return None
    return round(float(val) * 3.28084, 1)


def ft_to_m(val):
    """Feet → meters."""
    if val is None:
        return None
    return round(float(val) / 3.28084, 1)


def nm_to_lbft(val):
    """Newton-meters → lb-ft."""
    if val is None:
        return None
    return round(float(val) * _NM_TO_LBFT, 1)


# ── Dispatch table ──────────────────────────────────────────────────

# Maps (metric_unit, imperial_unit) → converter function
_CONVERTERS = {
    ("km", "mi"): km_to_mi,
    ("mi", "km"): mi_to_km,
    ("km/h", "mph"): kmh_to_mph,
    ("mph", "km/h"): mph_to_kmh,
    ("°C", "°F"): c_to_f,
    ("°F", "°C"): f_to_c,
    ("kPa", "PSI"): kpa_to_psi,
    ("PSI", "kPa"): psi_to_kpa,
    ("m", "ft"): m_to_ft,
    ("ft", "m"): ft_to_m,
    ("Nm", "lb-ft"): nm_to_lbft,
}

# Label maps per measurement category for each unit system
_LABELS = {
    "metric": {
        "speed": "km/h",
        "distance": "km",
        "temperature": "°C",
        "pressure": "kPa",
        "altitude": "m",
        "torque": "Nm",
        "energy": "kWh",
        "voltage": "V",
        "current": "A",
    },
    "imperial": {
        "speed": "mph",
        "distance": "mi",
        "temperature": "°F",
        "pressure": "PSI",
        "altitude": "ft",
        "torque": "lb-ft",
        "energy": "kWh",       # same in both systems
        "voltage": "V",
        "current": "A",
    },
}

# Which fields need conversion and what category they belong to
FIELD_CATEGORIES = {
    "speed_mph": "speed",
    "odometer_miles": "distance",
    "range_miles": "distance",
    "temperature_c": "temperature",
    "ambient_temp_c": "temperature",
    "outside_temp_c": "temperature",
    "pressure_kpa": "pressure",
    "placard_kpa": "pressure",
    "altitude_m": "altitude",
    "brake_torque": "torque",
    "transmission_torque": "torque",
}


def convert(val, from_unit: str, to_unit: str):
    """Convert a value between units. Returns original if no converter found."""
    if val is None:
        return None
    if from_unit == to_unit:
        return val
    fn = _CONVERTERS.get((from_unit, to_unit))
    if fn:
        return fn(val)
    return val


def unit_label(category: str, system: str = "metric") -> str:
    """Return the display label for a measurement category."""
    return _LABELS.get(system, _LABELS["metric"]).get(category, "")


def convert_for_display(val, field_name: str, system: str = "metric"):
    """Auto-convert a value based on the DB field name and target unit system.

    If system is 'metric', returns the raw value (DB stores metric).
    If system is 'imperial', converts to the imperial equivalent.
    """
    if val is None or system == "metric":
        return val

    cat = FIELD_CATEGORIES.get(field_name)
    if cat is None:
        return val

    metric_label = _LABELS["metric"][cat]
    imperial_label = _LABELS["imperial"][cat]
    return convert(val, metric_label, imperial_label)
