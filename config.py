"""Ford Lightning source file.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
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


def external_id_config() -> dict:
    """Return Entra External ID authentication settings."""
    cfg = dict(get_config().get("external_id", {}))
    env_secret = (os.environ.get("LIGHTNING_EXTERNAL_ID_CLIENT_SECRET") or "").strip()
    if env_secret:
        cfg["client_secret"] = env_secret
    return cfg


def devloper_config() -> dict:
        """Return server-side Devloper auth-bypass settings.

        Config is read from config.json under "devloper":
            - enabled: on/off style flag
            - ip_allowlist: list of IP/CIDR entries (or comma-separated string)

        Optional environment overrides:
            - LIGHTNING_DEVLOPER_BYPASS_ENABLED
            - LIGHTNING_DEVLOPER_BYPASS_IP_ALLOWLIST
        """
        cfg = dict(get_config().get("devloper", {}))

        env_enabled = (os.environ.get("LIGHTNING_DEVLOPER_BYPASS_ENABLED") or "").strip()
        if env_enabled:
                cfg["enabled"] = env_enabled

        env_allow = (os.environ.get("LIGHTNING_DEVLOPER_BYPASS_IP_ALLOWLIST") or "").strip()
        if env_allow:
                cfg["ip_allowlist"] = [entry.strip() for entry in env_allow.split(",") if entry.strip()]

        return cfg


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
