"""Configuration loader for the Lightning application.

Reads config.json once and exposes typed accessors for each section.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.2.1
Date:        2026-04-28
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


def environment() -> str:
    """Return the current environment name (e.g. 'development', 'production')."""
    return get_config().get("environment", "development")


def flask_port() -> int:
    """Return the Flask listening port (default 5000)."""
    return int(get_config().get("port", 5000))


def logging_config() -> dict:
    """Return logging configuration (level, log_sql flag)."""
    return get_config().get("logging", {"level": "INFO", "log_sql": False})


def collector_config() -> dict:
    """Return collector/poller settings (intervals, failure thresholds)."""
    return get_config().get("collector", {})


def ssl_config() -> dict:
    """Return SSL/TLS settings (cert and key file paths)."""
    return get_config().get("ssl", {})


def save_database(db_settings: dict) -> None:
    """Update the database section in config.json and reload."""
    global _CONFIG
    cfg = get_config()
    cfg["database"] = db_settings
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    _CONFIG = cfg


def save_ssl(ssl_settings: dict) -> None:
    """Update the ssl section in config.json and reload."""
    global _CONFIG
    cfg = get_config()
    cfg["ssl"] = ssl_settings
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    _CONFIG = cfg
