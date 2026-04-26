"""Configuration loader for the Lightning application.

Reads config.json once and exposes typed accessors for each section.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.1.0
Date:        2026-04-26
"""

import json
import os

_CONFIG = None
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load(path: str | None = None) -> dict:
    """Load configuration from disk. Caches after first call."""
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    path = path or _CONFIG_PATH
    with open(path, "r") as f:
        _CONFIG = json.load(f)
    return _CONFIG


def get_config() -> dict:
    """Return the loaded config, loading if necessary."""
    if _CONFIG is None:
        load()
    return _CONFIG


# ── Convenience accessors ──────────────────────────────────────────

def database() -> dict:
    """Return database connection settings."""
    return get_config()["database"]


def vin() -> str:
    """Return the configured VIN."""
    return get_config()["vehicle"]["vin"]


def environment() -> str:
    """Return the current environment name (e.g. 'development', 'production')."""
    return get_config().get("environment", "development")


def logging_config() -> dict:
    """Return logging configuration (level, log_sql flag)."""
    return get_config().get("logging", {"level": "INFO", "log_sql": False})


def collector_config() -> dict:
    """Return collector/poller settings (intervals, failure thresholds)."""
    return get_config().get("collector", {})
