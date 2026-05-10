"""Flask application for MLLighting telemetry dashboard.

Serves the web UI for vehicle state monitoring, OAuth configuration,
poller control, database browsing, settings, and vehicle management.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.7
Date:        2026-05-09

3rd Party APIs (Trip Planner):
- Nominatim (OpenStreetMap): Geocoding - NO KEY REQUIRED
- US Census Bureau Geocoder: US address geocoding - NO KEY REQUIRED
- ArcGIS World Geocoder: Geocoding fallback - NO KEY REQUIRED
- Photon: OSM-based geocoding fallback - NO KEY REQUIRED
- OSRM: Open Source Routing Machine - NO KEY REQUIRED
- OpenRouteService: Routing fallback - REQUIRES API KEY (optional)
- Open-Meteo: Weather forecasting - NO KEY REQUIRED

3rd Party APIs (Core):
- Ford Connected Vehicle API: Vehicle telemetry - REQUIRES OAUTH CREDENTIALS
"""

import logging
import os
import re
import subprocess
import sys
import threading
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, redirect, render_template, request, url_for, flash, send_file, jsonify
import requests
from werkzeug.utils import secure_filename

import config
import db
import oauth
import poller
import units
import backup
import crypto
import nlr_chargers
import trip_planner as tp_service
import energy_model

# ── Logging setup ──────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure logging with clean console output and detailed per-module debug files.

    Console: brief one-line summaries (configurable level)
    logs/lightning_app.log: combined log (configurable level)
    logs/debug_oauth.log: OAuth token exchange details (DEBUG)
    logs/debug_api.log: Ford API calls – URIs, headers, params, responses (DEBUG)
    logs/debug_poller.log: Poller lifecycle + state upserts (DEBUG)

    The console and app-file handler levels can be changed at runtime via
    ``set_log_level()`` (exposed through the Settings page).
    """
    cfg = config.logging_config()
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    # ── Formatters ─────────────────────────────────────────────
    brief_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                  datefmt="%H:%M:%S")
    detail_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s")

    # ── Console handler (brief) ────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(brief_fmt)
    console.setLevel(level)
    console.set_name("lightning_console")

    # ── Combined app log ───────────────────────────────────────
    app_file = logging.FileHandler(os.path.join(log_dir, "lightning_app.log"))
    app_file.setFormatter(detail_fmt)
    app_file.setLevel(level)
    app_file.set_name("lightning_app_file")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(app_file)

    # ── Per-module debug files ─────────────────────────────────
    debug_files = {
        "oauth": "debug_oauth.log",
        "ford_api": "debug_api.log",
        "poller": "debug_poller.log",
        "nlr_chargers": "debug_chargers.log",
        "nlr_api": "debug_nlr_api.log",
    }
    for logger_name, filename in debug_files.items():
        fh = logging.FileHandler(os.path.join(log_dir, filename))
        fh.setFormatter(detail_fmt)
        fh.setLevel(logging.DEBUG)
        logging.getLogger(logger_name).addHandler(fh)

    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def set_log_level(level_name: str) -> str:
    """Change the console and app-file log level at runtime.

    Per-module debug files always remain at DEBUG.
    Returns the level name actually applied.
    """
    level_name = level_name.upper()
    if level_name not in _VALID_LOG_LEVELS:
        level_name = "INFO"
    level = getattr(logging, level_name)

    root = logging.getLogger()
    for handler in root.handlers:
        if handler.get_name() in ("lightning_console", "lightning_app_file"):
            handler.setLevel(level)

    root_log = logging.getLogger(__name__)
    root_log.info("Log level changed to %s", level_name)
    return level_name


def get_log_level() -> str:
    """Return the current effective level of the console handler."""
    root = logging.getLogger()
    for handler in root.handlers:
        if handler.get_name() == "lightning_console":
            return logging.getLevelName(handler.level)
    return config.logging_config().get("level", "INFO").upper()


# ── Scheduler/thread helpers (must be defined before create_app) ──


# (Repeat this pattern for _ml_retrain_scheduler helpers: move them above create_app)

# ...existing code for _ml_retrain_scheduler helpers...

# ── App factory ────────────────────────────────────────────────────


def create_app():
    """Flask application factory."""
    app = Flask(__name__)
    config.load()
    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("Starting MLLighting app (env=%s)", config.environment())

    _SETTINGS_DEFAULTS = {
        "units": "imperial",
        "timezone": "UTC",
        "log_level": "INFO",
        "conservative_polling": "off",
        "autostart_poller": "off",
        "developing": "off",
        "poll_interval_off": "120",
        "poll_interval_on": "60",
        "poll_interval_moving": "15",
        "poll_interval_charging": "60",
        "charger_scope": "all_us",
        "charger_state_filter": "",
        "charger_fetch_strategy": "all_then_200",
        "charger_page_size": "200",
        "charger_auto_update": "off",
        "charger_auto_update_hours": "24",
        "ml_retrain_schedule_enabled": "off",
        "ml_retrain_schedule_hours": "24",
        "ml_retrain_after_x_drives_enabled": "on",
        "ml_retrain_after_x_drives": "10",
        "backup_schedule_enabled": "off",
        "backup_schedule_hours": "24",
        "backup_last_completed_at": "",
        "backup_last_error": "",
    }

    _POLL_LIMITS = {
        "poll_interval_off": {"min": 60, "max": 3600},
        "poll_interval_on": {"min": 60, "max": 3600},
        "poll_interval_moving": {"min": 15, "max": 3600},
        "poll_interval_charging": {"min": 60, "max": 3600},
    }

    _charger_import_guard = threading.Lock()
    _charger_import_thread: dict[str, threading.Thread | None] = {"thread": None}
    _charger_scheduler_thread: dict[str, threading.Thread | None] = {"thread": None}
    _charger_scheduler_stop_event = threading.Event()

    _ml_retrain_guard = threading.Lock()
    _ml_retrain_thread: dict[str, threading.Thread | None] = {"thread": None}
    _ml_retrain_scheduler_thread: dict[str, threading.Thread | None] = {"thread": None}
    _ml_retrain_scheduler_stop_event = threading.Event()

    _backup_scheduler_thread: dict[str, threading.Thread | None] = {"thread": None}
    _backup_scheduler_stop_event = threading.Event()



    def _run_charger_import_background(state_for_import: str | None, fetch_strategy: str, page_size: int) -> None:
        try:
            log.info(
                "Background charger import started (state=%s, strategy=%s, page_size=%s)",
                state_for_import or "all",
                fetch_strategy,
                page_size,
            )
            result = nlr_chargers.import_ev_stations_with_strategy(
                state=state_for_import,
                strategy=fetch_strategy,
                page_size=page_size,
            )
            log.info(
                "Background charger import completed (sync_run_id=%s, mode=%s, processed=%s, errors=%s)",
                result.get("sync_run_id"),
                result.get("fetch_mode_used", "paged"),
                result.get("processed", result.get("updated", 0)),
                result.get("errors", 0),
            )
        except Exception as exc:
            log.exception(
                "Background charger import failed (state=%s, strategy=%s, page_size=%s): %s",
                state_for_import or "all",
                fetch_strategy,
                page_size,
                exc,
            )
        finally:
            with _charger_import_guard:
                _charger_import_thread["thread"] = None

    def _charger_import_db_in_progress() -> bool:
        """Return True if the DB has an active in-progress charger sync run."""
        try:
            if not _table_exists("ev_sync_runs"):
                return False
            row = db.fetch_one(
                """
                SELECT 1 AS active
                FROM ev_sync_runs
                WHERE status = 'in_progress' AND completed_at IS NULL
                LIMIT 1
                """
            )
            return bool(row)
        except Exception as exc:
            log.warning("Failed checking in-progress charger runs: %s", exc)
            return False

    def _start_charger_import_job(
        state_for_import: str | None,
        fetch_strategy: str,
        page_size: int,
        trigger: str,
    ) -> tuple[bool, str]:
        """Start a charger import background thread if nothing else is already running."""
        with _charger_import_guard:
            if _charger_import_is_running():
                return False, "thread_running"

            try:
                stale_count = nlr_chargers.mark_stale_sync_runs(stale_after_minutes=5)
                if stale_count:
                    log.warning("Detected and closed %d stale charger import run(s)", stale_count)
            except Exception as exc:
                log.warning("Failed stale-run cleanup before charger import start: %s", exc)

            if _charger_import_db_in_progress():
                return False, "db_run_in_progress"

            t = threading.Thread(
                target=_run_charger_import_background,
                args=(state_for_import, fetch_strategy, page_size),
                name="charger-import-job",
                daemon=True,
            )
            _charger_import_thread["thread"] = t
            t.start()

        log.info(
            "Charger import submitted (trigger=%s, state=%s, strategy=%s, page_size=%s)",
            trigger,
            state_for_import or "all",
            fetch_strategy,
            page_size,
        )
        return True, "started"

    def _charger_scheduler_is_running() -> bool:
        t = _charger_scheduler_thread.get("thread")
        return bool(t and t.is_alive())

    def _int_setting(key: str, default: int, min_value: int, max_value: int) -> int:
        """Read a numeric app setting with min/max clamping and fallback."""
        try:
            value = int((_get_setting(key) or str(default)).strip())
        except (ValueError, TypeError, AttributeError):
            value = default
        return max(min_value, min(max_value, value))

    def _run_charger_auto_sync_loop() -> None:
        """Periodic charger sync scheduler loop."""
        while not _charger_scheduler_stop_event.is_set():
            try:
                if not db.is_available():
                    _charger_scheduler_stop_event.wait(60)
                    continue

                enabled = (_get_setting("charger_auto_update") or "off").strip().lower() == "on"
                if not enabled:
                    _charger_scheduler_stop_event.wait(60)
                    continue

                interval_hours = _int_setting("charger_auto_update_hours", 24, 1, 168)

                if _charger_import_is_running() or _charger_import_db_in_progress():
                    _charger_scheduler_stop_event.wait(60)
                    continue

                last_completed = None
                if _table_exists("ev_sync_runs"):
                    row = db.fetch_one(
                        """
                        SELECT completed_at
                        FROM ev_sync_runs
                        WHERE status = 'completed' AND completed_at IS NOT NULL
                        ORDER BY completed_at DESC
                        LIMIT 1
                        """
                    )
                    if row:
                        last_completed = row.get("completed_at")

                now_utc = datetime.now(timezone.utc)
                due = last_completed is None or (now_utc - last_completed) >= timedelta(hours=interval_hours)
                if due:
                    scope = (_get_setting("charger_scope") or _SETTINGS_DEFAULTS["charger_scope"]).strip()
                    if scope not in ("all_us", "single_state"):
                        scope = "all_us"

                    state_filter = (_get_setting("charger_state_filter") or "").strip().upper()
                    if scope != "single_state":
                        state_filter = ""
                    if scope == "single_state" and state_filter not in nlr_chargers.US_STATES:
                        state_filter = ""
                        scope = "all_us"

                    fetch_strategy = (
                        (_get_setting("charger_fetch_strategy") or _SETTINGS_DEFAULTS["charger_fetch_strategy"])
                        .strip()
                    )
                    if fetch_strategy not in ("all_then_200", "paged_200"):
                        fetch_strategy = "all_then_200"

                    page_size = _int_setting("charger_page_size", 200, 50, 1000)
                    state_for_import = state_filter if scope == "single_state" and state_filter else None

                    started, reason = _start_charger_import_job(
                        state_for_import,
                        fetch_strategy,
                        page_size,
                        trigger=f"auto_{interval_hours}h",
                    )
                    if started:
                        log.info(
                            "Auto charger sync started (interval_hours=%s, scope=%s, state=%s, strategy=%s)",
                            interval_hours,
                            scope,
                            state_for_import or "all",
                            fetch_strategy,
                        )
                    else:
                        log.info("Auto charger sync skipped (%s)", reason)
            except Exception as exc:
                log.exception("Charger auto-sync loop error: %s", exc)

            _charger_scheduler_stop_event.wait(60)

    def _start_charger_auto_sync_scheduler() -> None:
        """Ensure the periodic charger sync scheduler is running."""
        if _charger_scheduler_is_running():
            return

        t = threading.Thread(
            target=_run_charger_auto_sync_loop,
            name="charger-auto-sync",
            daemon=True,
        )
        _charger_scheduler_thread["thread"] = t
        t.start()
        log.info("Charger auto-sync scheduler thread started")

    def _ml_retrain_is_running() -> bool:
        t = _ml_retrain_thread.get("thread")
        return bool(t and t.is_alive())

    def _ml_retrain_scheduler_is_running() -> bool:
        t = _ml_retrain_scheduler_thread.get("thread")
        return bool(t and t.is_alive())

    def _parse_utc_iso(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _read_model_schema() -> dict:
        schema_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "models",
            "energy_model_schema.json",
        )
        if not os.path.isfile(schema_path):
            return {}
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Failed reading model schema: %s", exc)
            return {}

    def _count_completed_training_drives() -> int:
        try:
            if not _table_exists("drives"):
                return 0
            row = db.fetch_one(
                """
                SELECT COUNT(*) AS cnt
                FROM drives
                WHERE in_progress = FALSE
                  AND energy_used_kwh > 0
                  AND distance_km > 5
                """
            )
            return int(row["cnt"]) if row and row.get("cnt") is not None else 0
        except Exception as exc:
            log.warning("Failed counting completed drives for retraining: %s", exc)
            return 0

    def _ml_last_trained_drive_count() -> int:
        raw = (_get_setting("ml_retrain_last_trained_drive_count") or "").strip()
        if raw:
            try:
                value = int(raw)
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass

        schema = _read_model_schema()
        schema_count = schema.get("num_training_drives")
        if isinstance(schema_count, int) and schema_count >= 0:
            return schema_count

        return _count_completed_training_drives()

    def _run_ml_retrain_background(trigger: str) -> None:
        started_at = datetime.now(timezone.utc)
        project_dir = os.path.dirname(os.path.abspath(__file__))
        python_bin = os.path.join(project_dir, "venv", "bin", "python")
        if not os.path.isfile(python_bin):
            python_bin = sys.executable

        train_script = os.path.join(project_dir, "train_energy_model.py")
        retrain_log_path = os.path.join(project_dir, "logs", "ml_retrain.log")

        _set_setting("ml_retrain_status", "in_progress", "ML retraining job status")
        _set_setting("ml_retrain_last_started_at", started_at.isoformat(), "ML retraining last start timestamp")
        _set_setting("ml_retrain_last_trigger", trigger, "ML retraining trigger source")
        _set_setting("ml_retrain_last_error", "", "ML retraining last error message")

        try:
            os.makedirs(os.path.dirname(retrain_log_path), exist_ok=True)
            with open(retrain_log_path, "a", encoding="utf-8") as retrain_log:
                retrain_log.write(
                    f"\n=== ML retrain start {started_at.isoformat()} trigger={trigger} ===\n"
                )
                result = subprocess.run(
                    [python_bin, train_script],
                    cwd=project_dir,
                    stdout=retrain_log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

            completed_at = datetime.now(timezone.utc)
            duration_sec = max(0, int((completed_at - started_at).total_seconds()))
            _set_setting("ml_retrain_last_completed_at", completed_at.isoformat(), "ML retraining last completion timestamp")
            _set_setting("ml_retrain_last_duration_sec", str(duration_sec), "ML retraining duration in seconds")
            _set_setting("ml_retrain_last_exit_code", str(result.returncode), "ML retraining process exit code")

            if result.returncode == 0:
                schema = _read_model_schema()
                trained_count = schema.get("num_training_drives")
                if not isinstance(trained_count, int) or trained_count < 0:
                    trained_count = _count_completed_training_drives()
                _set_setting(
                    "ml_retrain_last_trained_drive_count",
                    str(trained_count),
                    "Completed drive baseline count from last successful ML retraining",
                )
                _set_setting("ml_retrain_status", "completed", "ML retraining job status")
                _set_setting("ml_retrain_last_error", "", "ML retraining last error message")

                # Reset lazy-loaded model cache so new predictions use fresh artifacts.
                for attr in ("_model", "_scaler", "_schema"):
                    if hasattr(energy_model, attr):
                        setattr(energy_model, attr, None)

                log.info(
                    "ML retraining completed successfully (trigger=%s, duration=%ss, trained_drives=%s)",
                    trigger,
                    duration_sec,
                    trained_count,
                )
            else:
                err_msg = (
                    f"Retraining failed with exit code {result.returncode}. "
                    "See logs/ml_retrain.log"
                )
                _set_setting("ml_retrain_status", "failed", "ML retraining job status")
                _set_setting("ml_retrain_last_error", err_msg, "ML retraining last error message")
                log.warning("%s (trigger=%s)", err_msg, trigger)

        except Exception as exc:
            _set_setting("ml_retrain_status", "failed", "ML retraining job status")
            _set_setting("ml_retrain_last_error", str(exc), "ML retraining last error message")
            _set_setting("ml_retrain_last_exit_code", "", "ML retraining process exit code")
            log.exception("ML retraining background job failed (trigger=%s): %s", trigger, exc)
        finally:
            with _ml_retrain_guard:
                _ml_retrain_thread["thread"] = None

    def _start_ml_retrain_job(trigger: str) -> tuple[bool, str]:
        with _ml_retrain_guard:
            if _ml_retrain_is_running():
                return False, "thread_running"

            t = threading.Thread(
                target=_run_ml_retrain_background,
                args=(trigger,),
                name="ml-retrain-job",
                daemon=True,
            )
            _ml_retrain_thread["thread"] = t
            t.start()

        log.info("ML retraining submitted (trigger=%s)", trigger)
        return True, "started"

    def _run_ml_retrain_scheduler_loop() -> None:
        while not _ml_retrain_scheduler_stop_event.is_set():
            try:
                if not db.is_available():
                    _ml_retrain_scheduler_stop_event.wait(60)
                    continue

                if _ml_retrain_is_running():
                    _ml_retrain_scheduler_stop_event.wait(60)
                    continue

                now_utc = datetime.now(timezone.utc)
                due = False
                trigger = ""

                schedule_enabled = (_get_setting("ml_retrain_schedule_enabled") or "off").strip().lower() == "on"
                if schedule_enabled:
                    interval_hours = _int_setting("ml_retrain_schedule_hours", 24, 1, 168)
                    last_completed = _parse_utc_iso((_get_setting("ml_retrain_last_completed_at") or "").strip())
                    if last_completed is None or (now_utc - last_completed) >= timedelta(hours=interval_hours):
                        due = True
                        trigger = f"schedule_{interval_hours}h"

                if not due:
                    after_x_enabled = (
                        (_get_setting("ml_retrain_after_x_drives_enabled") or "off").strip().lower() == "on"
                    )
                    if after_x_enabled:
                        threshold = _int_setting("ml_retrain_after_x_drives", 10, 1, 500)
                        current_count = _count_completed_training_drives()
                        baseline_count = _ml_last_trained_drive_count()
                        new_drives = max(0, current_count - baseline_count)
                        if new_drives >= threshold:
                            due = True
                            trigger = f"after_{threshold}_drives"

                if due:
                    started, reason = _start_ml_retrain_job(trigger)
                    if started:
                        log.info("Auto ML retraining started (trigger=%s)", trigger)
                    else:
                        log.info("Auto ML retraining skipped (%s)", reason)

            except Exception as exc:
                log.exception("ML retraining scheduler loop error: %s", exc)

            _ml_retrain_scheduler_stop_event.wait(60)

    def _start_ml_retrain_scheduler() -> None:
        if _ml_retrain_scheduler_is_running():
            return

        t = threading.Thread(
            target=_run_ml_retrain_scheduler_loop,
            name="ml-retrain-scheduler",
            daemon=True,
        )
        _ml_retrain_scheduler_thread["thread"] = t
        t.start()
        log.info("ML retraining scheduler thread started")

    def _backup_scheduler_is_running() -> bool:
        t = _backup_scheduler_thread.get("thread")
        return bool(t and t.is_alive())

    def _cleanup_old_backups() -> None:
        """Retain only the 5 most recent backup files."""
        backup_dir = backup.BACKUP_DIR
        if not os.path.isdir(backup_dir):
            return

        keep = 5
        candidates: list[tuple[float, str]] = []
        for name in os.listdir(backup_dir):
            if not (name.startswith("lightning_backup_") and (name.endswith(".sql") or name.endswith(".json"))):
                continue
            path = os.path.join(backup_dir, name)
            try:
                candidates.append((os.path.getmtime(path), path))
            except OSError:
                continue

        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, old_path in candidates[keep:]:
            try:
                os.remove(old_path)
                log.info("Deleted old backup: %s", os.path.basename(old_path))
            except Exception as exc:
                log.warning("Failed deleting old backup %s: %s", old_path, exc)

    def _run_backup_scheduler_loop() -> None:
        while not _backup_scheduler_stop_event.is_set():
            try:
                if not db.is_available():
                    _backup_scheduler_stop_event.wait(60)
                    continue

                enabled = (_get_setting("backup_schedule_enabled") or "off").strip().lower() == "on"
                if not enabled:
                    _backup_scheduler_stop_event.wait(60)
                    continue

                interval_hours = _int_setting("backup_schedule_hours", 24, 1, 168)
                last_completed = _parse_utc_iso((_get_setting("backup_last_completed_at") or "").strip())
                now_utc = datetime.now(timezone.utc)
                due = last_completed is None or (now_utc - last_completed) >= timedelta(hours=interval_hours)

                if due:
                    try:
                        backup_path = backup.backup_sql()
                        _set_setting("backup_last_completed_at", now_utc.isoformat(), "Last scheduled backup time")
                        _set_setting("backup_last_error", "", "Last backup error")
                        _cleanup_old_backups()
                        log.info("Scheduled backup completed: %s", backup_path)
                    except Exception as exc:
                        _set_setting("backup_last_error", str(exc), "Last backup error")
                        log.warning("Scheduled backup failed: %s", exc)
            except Exception as exc:
                log.exception("Backup scheduler loop error: %s", exc)

            _backup_scheduler_stop_event.wait(60)

    def _start_backup_scheduler() -> None:
        if _backup_scheduler_is_running():
            return

        t = threading.Thread(
            target=_run_backup_scheduler_loop,
            name="backup-scheduler",
            daemon=True,
        )
        _backup_scheduler_thread["thread"] = t
        t.start()
        log.info("Backup scheduler thread started")

    def _clamp_interval(key: str, raw_value: str) -> int:
        """Clamp a polling interval to its configured min/max range."""
        limits = _POLL_LIMITS.get(key, {"min": 15, "max": 3600})
        try:
            val = int(raw_value)
        except (ValueError, TypeError):
            val = int(_SETTINGS_DEFAULTS.get(key, "60"))
        return max(limits["min"], min(limits["max"], val))

    def _get_setting(key: str) -> str:
        """Read a single app setting from the app_config table, with fallback defaults."""
        row = db.fetch_one("SELECT value FROM app_config WHERE key = %s", (key,))
        return row["value"] if row else _SETTINGS_DEFAULTS.get(key, "")

    def _set_setting(key: str, value: str, description: str = "") -> None:
        """Write a single app setting to the app_config table (upsert)."""
        db.execute(
            """
            INSERT INTO app_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, value, description),
        )

    def _charger_failure_class(run: dict | None) -> str:
        """Classify charger sync failures into clearer user-facing categories."""
        if not run:
            return "N/A"

        status = str(run.get("status") or "").strip().lower()
        if status != "failed":
            return "N/A"

        err = str(run.get("last_error") or "").strip().lower()
        if not err:
            return "Unknown failure"

        if "heartbeat" in err and ("stale" in err or "timeout" in err or "no heartbeat" in err):
            return "Stale heartbeat timeout"

        if "api key" in err or "not configured" in err or "config" in err:
            return "Configuration error"

        if "request failed" in err or "http" in err or "api" in err:
            return "API request failure"

        if "timeout" in err:
            return "Request timeout"

        return "Other failure"

    def _table_exists(table_name: str) -> bool:
        """Return True when a table exists in the current PostgreSQL schema."""
        row = db.fetch_one(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s",
            (table_name,),
        )
        return row is not None

    def _column_exists(table_name: str, column_name: str) -> bool:
        """Return True when a column exists on a table in public schema."""
        row = db.fetch_one(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = %s
            """,
            (table_name, column_name),
        )
        return row is not None

    def _resolve_time_column(table_name: str, candidates: tuple[str, ...]) -> str | None:
        """Return the first existing timestamp-like column for a table, else None."""
        if not _table_exists(table_name):
            return None
        for col in candidates:
            if _column_exists(table_name, col):
                return col
        return None

    def _align_id_sequence(table_name: str) -> bool:
        """Align table id sequence to MAX(id)+1 when the table uses a serial/bigserial id."""
        if not _table_exists(table_name) or not _column_exists(table_name, "id"):
            return False

        row = db.fetch_one("SELECT pg_get_serial_sequence(%s, 'id') AS seq", (table_name,))
        seq_name = row["seq"] if row else None
        if not seq_name:
            return False

        db.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1,
                false
            )
            """
        )
        return True

    _SEQUENCE_ALIGNMENT_MARKER_KEY = "startup_sequence_alignment_v1_done"
    _SEQUENCE_ALIGNMENT_FORCE_KEY = "startup_sequence_alignment_force_next_startup"
    _SEQUENCE_ALIGNMENT_TABLES = (
        "telemetry",
        "charging_history",
        "charging_sessions",
        "polling_config",
        "oauth_credentials",
        "drives",
        "drive_points",
        "ev_stations",
        "ev_charger_connectors",
        "ev_sync_runs",
    )

    def _run_sequence_alignment(force: bool = False) -> list[str]:
        """Align serial/bigserial sequences to MAX(id)+1 (one-time unless forced)."""
        if not _table_exists("app_config"):
            return []

        marker = db.fetch_one(
            "SELECT value FROM app_config WHERE key = %s",
            (_SEQUENCE_ALIGNMENT_MARKER_KEY,),
        )
        force_row = db.fetch_one(
            "SELECT value FROM app_config WHERE key = %s",
            (_SEQUENCE_ALIGNMENT_FORCE_KEY,),
        )
        force_requested = (
            force_row is not None
            and str(force_row.get("value", "")).strip().lower() in ("on", "true", "1", "yes")
        )

        should_run = force or force_requested or marker is None
        if not should_run:
            return []

        aligned_tables: list[str] = []
        for table_name in _SEQUENCE_ALIGNMENT_TABLES:
            if _align_id_sequence(table_name):
                aligned_tables.append(table_name)

        now_iso = datetime.now(timezone.utc).isoformat()
        db.execute(
            """
            INSERT INTO app_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (
                _SEQUENCE_ALIGNMENT_MARKER_KEY,
                now_iso,
                "One-time startup repair: aligned serial sequences to MAX(id)+1",
            ),
        )
        # Clear one-shot force flag after a run.
        db.execute(
            """
            INSERT INTO app_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (
                _SEQUENCE_ALIGNMENT_FORCE_KEY,
                "off",
                "If on, run sequence alignment once at next app startup",
            ),
        )

        return aligned_tables

    def _run_startup_migrations() -> list[str]:
        """Apply lightweight schema migrations at startup and return applied change labels."""
        if not db.is_available():
            return []

        applied: list[str] = []

        if _table_exists("charging_state") and not _column_exists("charging_state", "charge_display_status"):
            db.execute("ALTER TABLE charging_state ADD COLUMN IF NOT EXISTS charge_display_status TEXT")
            applied.append("Added charging_state.charge_display_status")

        if not _table_exists("charging_sessions"):
            db.execute(
                """
                CREATE TABLE charging_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    session_uuid UUID NOT NULL UNIQUE,
                    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
                    started_at TIMESTAMPTZ NOT NULL,
                    last_update TIMESTAMPTZ NOT NULL,
                    ended_at TIMESTAMPTZ,
                    in_progress BOOLEAN NOT NULL DEFAULT TRUE,
                    charger_power_type TEXT,
                    start_soc_percent REAL CHECK (start_soc_percent BETWEEN 0 AND 100),
                    end_soc_percent REAL CHECK (end_soc_percent BETWEEN 0 AND 100),
                    start_energy_remaining_kwh REAL CHECK (start_energy_remaining_kwh >= 0),
                    end_energy_remaining_kwh REAL CHECK (end_energy_remaining_kwh >= 0),
                    max_power_kw REAL CHECK (max_power_kw >= 0),
                    sample_count INTEGER NOT NULL DEFAULT 1 CHECK (sample_count >= 1),
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            applied.append("Created charging_sessions table")

        if _table_exists("charging_sessions"):
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_charging_sessions_vin_start ON charging_sessions (vin, started_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_charging_sessions_open ON charging_sessions (vin) WHERE in_progress = TRUE"
            )

        # Create EV charger tables for NLR API integration
        if not _table_exists("ev_stations"):
            db.execute(
                """
                CREATE TABLE ev_stations (
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
                    raw_data JSONB,
                    UNIQUE (nlr_station_id)
                )
                """
            )
            applied.append("Created ev_stations table")

        if not _table_exists("ev_charger_connectors"):
            db.execute(
                """
                CREATE TABLE ev_charger_connectors (
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
            applied.append("Created ev_charger_connectors table")

        if not _table_exists("ev_sync_runs"):
            db.execute(
                """
                CREATE TABLE ev_sync_runs (
                    id BIGSERIAL PRIMARY KEY,
                    sync_type TEXT NOT NULL,
                    state_filter TEXT,
                    status TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    completed_at TIMESTAMPTZ,
                    stations_imported INTEGER DEFAULT 0,
                    stations_updated INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            applied.append("Created ev_sync_runs table")

        # Create indexes for charger tables
        if _table_exists("ev_stations"):
            db.execute("CREATE INDEX IF NOT EXISTS idx_ev_stations_state ON ev_stations (state) WHERE country = 'US'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ev_stations_nlr_id ON ev_stations (nlr_station_id)")

        if _table_exists("ev_charger_connectors"):
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ev_connectors_station ON ev_charger_connectors (station_id)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_ev_connectors_network ON ev_charger_connectors (network)")

        if _table_exists("ev_sync_runs"):
            if not _column_exists("ev_sync_runs", "last_heartbeat_at"):
                db.execute(
                    "ALTER TABLE ev_sync_runs ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                )
                applied.append("Added ev_sync_runs.last_heartbeat_at")
            if not _column_exists("ev_sync_runs", "last_error"):
                db.execute("ALTER TABLE ev_sync_runs ADD COLUMN IF NOT EXISTS last_error TEXT")
                applied.append("Added ev_sync_runs.last_error")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ev_sync_runs_status ON ev_sync_runs (status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ev_sync_runs_started ON ev_sync_runs (started_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ev_sync_runs_heartbeat ON ev_sync_runs (last_heartbeat_at DESC)")

        # One-time repair after backup/restore (or when queued by UI):
        # align serial sequences to table MAX(id)+1 to prevent duplicate PKs.
        aligned_tables = _run_sequence_alignment(force=False)
        if aligned_tables:
            applied.append("Aligned ID sequences: " + ", ".join(aligned_tables))

        if _table_exists("app_config") and applied:
            db.execute(
                """
                INSERT INTO app_config (key, value, description, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (
                    "last_startup_migration",
                    "; ".join(applied),
                    "Most recent automatic startup schema migration summary",
                ),
            )

        return applied

    def _db_vacuum() -> str:
        """Run VACUUM to clean up dead tuples and reclaim space."""
        try:
            db.execute("VACUUM ANALYZE")
            return "VACUUM ANALYZE completed successfully"
        except Exception as e:
            error_msg = str(e)
            if "cannot" in error_msg.lower() or "permission" in error_msg.lower() or "container" in error_msg.lower():
                return "⚠️  VACUUM cannot run (may be restricted in Docker containers). This is safe to skip—your database is still functional."
            raise

    def _db_reindex() -> str:
        """Rebuild all indexes for performance."""
        try:
            # Reindex all tables to maintain good performance
            for table_name in _VIEWABLE_TABLES:
                if _table_exists(table_name):
                    db.execute(f"REINDEX TABLE {table_name}")
            return "REINDEX completed for all tables"
        except Exception as e:
            error_msg = str(e)
            if "cannot" in error_msg.lower() or "permission" in error_msg.lower() or "container" in error_msg.lower():
                return "⚠️  REINDEX cannot run (may be restricted in Docker containers). This is safe to skip—index performance is maintained automatically."
            raise

    def _db_check_stale_data() -> dict:
        """Check for potentially stale data in charging/drive tables."""
        results = {}
        
        # Check for old charging records (>30 days)
        if _table_exists("charging_history"):
            charging_ts_col = _resolve_time_column("charging_history", ("polled_at", "created_at", "last_update"))
            if charging_ts_col:
                row = db.fetch_one(
                    f"SELECT COUNT(*) as cnt FROM charging_history WHERE {charging_ts_col} < NOW() - INTERVAL '30 days'"
                )
                old_charging = row["cnt"] if row else 0
                results["old_charging_records"] = (old_charging, f"records >30 days old in charging_history ({charging_ts_col})")
            else:
                results["old_charging_records"] = (0, "timestamp column unavailable in charging_history")
        
        # Check for old drive records (>90 days)
        if _table_exists("drives"):
            drives_ts_col = _resolve_time_column("drives", ("ended_at", "started_at", "created_at"))
            if drives_ts_col:
                row = db.fetch_one(
                    f"SELECT COUNT(*) as cnt FROM drives WHERE {drives_ts_col} < NOW() - INTERVAL '90 days'"
                )
                old_drives = row["cnt"] if row else 0
                results["old_drive_records"] = (old_drives, f"records >90 days old in drives ({drives_ts_col})")
            else:
                results["old_drive_records"] = (0, "timestamp column unavailable in drives")
        
        # Check for old vehicle_state records (>30 days)
        if _table_exists("vehicle_state"):
            vehicle_ts_col = _resolve_time_column("vehicle_state", ("last_update", "created_at", "updated_at"))
            if vehicle_ts_col:
                row = db.fetch_one(
                    f"SELECT COUNT(*) as cnt FROM vehicle_state WHERE {vehicle_ts_col} < NOW() - INTERVAL '30 days'"
                )
                old_vehicle_state = row["cnt"] if row else 0
                results["old_vehicle_state"] = (old_vehicle_state, f"records >30 days old in vehicle_state ({vehicle_ts_col})")
            else:
                results["old_vehicle_state"] = (0, "timestamp column unavailable in vehicle_state")
        
        return results

    def _db_table_stats() -> list[dict]:
        """Get table statistics (size, row count, last vacuum time)."""
        stats = []
        for table_name in _VIEWABLE_TABLES:
            if not _table_exists(table_name):
                continue
            
            row_count = db.fetch_one(f"SELECT count(*) as cnt FROM {table_name}")
            size_row = db.fetch_one(
                f"SELECT pg_size_pretty(pg_total_relation_size('{table_name}')) as size"
            )
            
            size_str = size_row["size"] if size_row else "0 B"
            count = row_count["cnt"] if row_count else 0
            
            stats.append({
                "name": table_name,
                "rows": count,
                "size": size_str
            })
        
        return stats

    def _db_index_stats() -> list[dict]:
        """Get index statistics and usage info."""
        rows = db.fetch_all(
            """
            SELECT
                schemaname,
                relname AS tablename,
                indexrelname AS indexname,
                idx_scan AS scans,
                idx_tup_read AS tuples_read,
                idx_tup_fetch AS tuples_fetched
            FROM pg_stat_user_indexes
            ORDER BY idx_scan DESC, relname
            """
        )
        return [dict(r) for r in rows] if rows else []

    def _db_cleanup_old_charging() -> str:
        """Delete charging records older than 90 days."""
        ts_col = _resolve_time_column("charging_history", ("polled_at", "created_at", "last_update"))
        if not ts_col:
            return "No compatible timestamp column found in charging_history"

        # First count how many will be deleted
        count_result = db.fetch_one(
            f"SELECT COUNT(*) as cnt FROM charging_history WHERE {ts_col} < NOW() - INTERVAL '90 days'"
        )
        to_delete = count_result["cnt"] if count_result else 0
        
        if to_delete > 0:
            db.execute(f"DELETE FROM charging_history WHERE {ts_col} < NOW() - INTERVAL '90 days'")
        
        return f"Deleted {to_delete} charging records older than 90 days using {ts_col}"

    def _db_cleanup_old_drives() -> str:
        """Delete drive records older than 180 days."""
        ts_col = _resolve_time_column("drives", ("ended_at", "started_at", "created_at"))
        if not ts_col:
            return "No compatible timestamp column found in drives"

        # First count how many will be deleted
        count_result = db.fetch_one(
            f"SELECT COUNT(*) as cnt FROM drives WHERE {ts_col} < NOW() - INTERVAL '180 days'"
        )
        to_delete = count_result["cnt"] if count_result else 0
        
        if to_delete > 0:
            db.execute(f"DELETE FROM drives WHERE {ts_col} < NOW() - INTERVAL '180 days'")
        
        return f"Deleted {to_delete} drive records older than 180 days using {ts_col}"

    def _charging_power_kw_from_row(charging: dict | None) -> float | None:
        """Compute kW from the latest charging_state row when possible."""
        if not charging:
            return None
        voltage = charging.get("charger_voltage")
        current = charging.get("charger_current")
        if voltage is None or current is None:
            return None
        try:
            return (float(voltage) * float(current)) / 1000.0
        except (TypeError, ValueError):
            return None

    def _plug_status_idle(plug_status: str | None) -> bool:
        """Return True when Ford's plug status represents unplugged/idle state."""
        normalized = (plug_status or "").strip().lower().replace("-", "_").replace(" ", "_")
        return normalized in {
            "",
            "unknown",
            "unplugged",
            "disconnected",
            "not_connected",
            "no_connection",
            "dc_disconnected",
            "ac_disconnected",
        }

    def _plug_status_connected(plug_status: str | None) -> bool:
        """Return True when Ford's plug status indicates the cable is attached."""
        if _plug_status_idle(plug_status):
            return False
        normalized = (plug_status or "").strip().lower()
        return any(token in normalized for token in ("connected", "plugged", "charging"))

    def _charging_is_active(charging: dict | None) -> bool:
        """Return True when the latest charging row indicates active charging."""
        if not charging:
            return False

        # Guard against stale charging_state rows showing old active values.
        last_update = charging.get("last_update")
        if isinstance(last_update, datetime):
            lu = last_update if last_update.tzinfo else last_update.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - lu) > timedelta(minutes=10):
                return False

        plug_status = (charging.get("plug_status") or "").lower()
        communication_status = (charging.get("communication_status") or "").lower()
        charge_display_status = (charging.get("charge_display_status") or "").lower()
        if _plug_status_idle(plug_status):
            return False

        idle_status_tokens = (
            "station_ready", "ready", "waiting", "scheduled", "paused", "standby",
            "not_detected", "complete", "completed", "not_ready", "charge_scheduling",
            "stopped", "stop",
        )
        active_status_tokens = ("charging", "in_progress", "active", "powering")

        idle_display = any(token in charge_display_status for token in idle_status_tokens)
        idle_comm = any(token in communication_status for token in idle_status_tokens)
        active_display = any(token in charge_display_status for token in active_status_tokens)
        active_comm = any(token in communication_status for token in active_status_tokens)

        # Explicitly idle from both status channels means not charging.
        if idle_display and idle_comm:
            return False

        power_kw = _charging_power_kw_from_row(charging)
        if power_kw is not None:
            try:
                if float(power_kw) > 0.5:
                    # If communication is explicitly idle and only display appears active,
                    # do not trust stale electrical values by themselves.
                    if idle_comm and active_display and not active_comm:
                        break_flow = True
                        time_to_full = charging.get("time_to_full_min")
                        try:
                            break_flow = not (time_to_full is not None and float(time_to_full) > 0)
                        except (TypeError, ValueError):
                            break_flow = True
                        if break_flow:
                            return False
                    return True
            except (TypeError, ValueError):
                pass

        charger_voltage = charging.get("charger_voltage")
        charger_current = charging.get("charger_current")
        evse_dc_current = charging.get("evse_dc_current")
        try:
            if (
                charger_voltage is not None and float(charger_voltage) > 20 and
                charger_current is not None and float(charger_current) > 0.5
            ):
                return True
        except (TypeError, ValueError):
            pass
        try:
            if evse_dc_current is not None and float(evse_dc_current) > 0.5:
                # Stale DC current data can contradict fresh status indicators.
                # If comm + display both say idle/stopped, don't trust high DC current alone.
                if idle_comm and idle_display:
                    return False
                return True
        except (TypeError, ValueError):
            pass

        # If there is no measurable electrical flow, require corroborating
        # active signals (status + time-to-full) to avoid stale "IN_PROGRESS"
        # causing false positives.
        if idle_comm and not active_display:
            return False

        time_to_full = charging.get("time_to_full_min")
        ttf_positive = False
        try:
            ttf_positive = time_to_full is not None and float(time_to_full) > 0
        except (TypeError, ValueError):
            ttf_positive = False

        active_signal_count = sum([1 if active_display else 0, 1 if active_comm else 0, 1 if ttf_positive else 0])
        return active_signal_count >= 2

    def _charging_mode_from_data(charging: dict | None, voltage_series: list[int | None]) -> str:
        """Infer charging profile from power-type hints and observed voltage samples."""
        power_type = (charging.get("charger_power_type") if charging else "") or ""
        normalized = power_type.strip().lower()
        if "dc" in normalized:
            return "dc"
        if "ac" in normalized:
            return "ac"

        valid_voltages = [float(v) for v in voltage_series if v is not None]
        if valid_voltages:
            high_count = sum(1 for v in valid_voltages if v >= 280)
            if high_count >= max(1, int(len(valid_voltages) * 0.25)):
                return "dc"
        return "ac"

    def _charging_axis_for_mode(mode: str) -> dict:
        """Return fixed, readable voltage axis bounds for AC/DC charging profiles."""
        if mode == "dc":
            return {"mode": "dc", "label": "DC Fast", "min": 200, "max": 520}
        return {"mode": "ac", "label": "AC / L1-L2", "min": 80, "max": 300}

    def _charging_sample_meaningful(row: dict) -> bool:
        """Return True when a history row likely represents a real charging sample."""
        comm = (row.get("communication_status") or "").strip().lower()
        active_tokens = ("charging", "active", "in_progress", "powering")
        idle_tokens = (
            "not_detected", "station_ready", "ready", "waiting", "scheduled",
            "complete", "completed", "stopped", "stop",
        )

        # Prefer physical electrical flow over status tokens.
        try:
            if row.get("charge_power_kw") is not None and float(row.get("charge_power_kw")) > 0.5:
                return True
        except (TypeError, ValueError):
            pass
        try:
            if (
                row.get("charger_voltage") is not None and float(row.get("charger_voltage")) > 20
                and row.get("charger_current") is not None and float(row.get("charger_current")) > 0.5
            ):
                return True
        except (TypeError, ValueError):
            pass
        try:
            if row.get("evse_dc_current") is not None and float(row.get("evse_dc_current")) > 0.5:
                return True
        except (TypeError, ValueError):
            pass

        if any(token in comm for token in active_tokens):
            return True
        if any(token in comm for token in idle_tokens):
            return False

        if not _plug_status_idle(row.get("plug_status")):
            return True
        power_raw = row.get("charge_power_kw")
        if power_raw is None:
            return False
        try:
            return float(power_raw) > 0.1
        except (TypeError, ValueError):
            return False

    def _latest_charging_session(rows_desc: list[dict], max_gap_minutes: int = 45) -> list[dict]:
        """Return the most recent contiguous charging session from DESC history rows."""
        # Prefer explicit session UUIDs when they exist.
        for row in rows_desc:
            session_uuid = row.get("charging_session_uuid")
            if not session_uuid:
                continue
            matched = [r for r in rows_desc if r.get("charging_session_uuid") == session_uuid]
            if matched:
                return list(reversed(matched))

        session_rows_desc = []
        last_polled_at = None
        max_gap = timedelta(minutes=max_gap_minutes)

        for row in rows_desc:
            if not _charging_sample_meaningful(row):
                if session_rows_desc:
                    break
                continue

            polled_at = row.get("polled_at")
            if last_polled_at and polled_at and (last_polled_at - polled_at) > max_gap:
                break

            session_rows_desc.append(row)
            last_polled_at = polled_at

        return list(reversed(session_rows_desc))

    def _split_charging_sessions(rows_desc: list[dict], max_gap_minutes: int = 45) -> list[list[dict]]:
        """Split DESC charging rows into logical sessions (newest session first)."""
        if not rows_desc:
            return []

        sessions: list[list[dict]] = []
        current_desc: list[dict] = []
        current_uuid = None
        last_polled_at = None
        max_gap = timedelta(minutes=max_gap_minutes)

        for row in rows_desc:
            row_uuid = row.get("charging_session_uuid")
            row_polled_at = row.get("polled_at")

            if not current_desc:
                current_desc = [row]
                current_uuid = row_uuid
                last_polled_at = row_polled_at
                continue

            split_session = False
            if current_uuid and row_uuid:
                split_session = current_uuid != row_uuid
            elif current_uuid or row_uuid:
                split_session = True
            elif last_polled_at and row_polled_at and (last_polled_at - row_polled_at) > max_gap:
                split_session = True

            if split_session:
                sessions.append(list(reversed(current_desc)))
                current_desc = [row]
                current_uuid = row_uuid
                last_polled_at = row_polled_at
                continue

            current_desc.append(row)
            last_polled_at = row_polled_at

        if current_desc:
            sessions.append(list(reversed(current_desc)))

        return sessions

    def _charging_sessions_summary(rows_desc: list[dict], limit: int = 20) -> list[dict]:
        """Build summary rows for recent charging sessions."""
        session_groups = _split_charging_sessions(rows_desc)
        summaries: list[dict] = []

        for idx, session_rows in enumerate(session_groups[:limit]):
            if not session_rows:
                continue

            first_row = session_rows[0]
            last_row = session_rows[-1]
            session_uuid = None
            for row in session_rows:
                if row.get("charging_session_uuid"):
                    session_uuid = str(row.get("charging_session_uuid"))
                    break

            start_soc = first_row.get("soc_percent")
            end_soc = last_row.get("soc_percent")
            try:
                soc_delta = (float(end_soc) - float(start_soc)) if start_soc is not None and end_soc is not None else None
            except (TypeError, ValueError):
                soc_delta = None

            max_power_kw = None
            for row in session_rows:
                raw_power = row.get("charge_power_kw")
                try:
                    power_val = float(raw_power)
                except (TypeError, ValueError):
                    continue
                max_power_kw = power_val if max_power_kw is None else max(max_power_kw, power_val)

            duration_min = None
            start_ts = first_row.get("polled_at")
            end_ts = last_row.get("polled_at")
            if start_ts and end_ts:
                try:
                    duration_min = max(0.0, (end_ts - start_ts).total_seconds() / 60.0)
                except Exception:
                    duration_min = None

            summaries.append(
                {
                    "session_label": (session_uuid[:8] if session_uuid else f"gap-{idx + 1}"),
                    "session_uuid": session_uuid,
                    "started_at": start_ts,
                    "ended_at": end_ts,
                    "duration_min": duration_min,
                    "start_soc": start_soc,
                    "end_soc": end_soc,
                    "soc_delta": soc_delta,
                    "max_power_kw": max_power_kw,
                    "samples": len(session_rows),
                }
            )

        return summaries

    def _build_charging_chart_data(latest_session: list[dict], system: str) -> dict:
        """Build charging chart series for a single charging session."""
        charging_chart_data = {
            "labels": [],
            "soc": [],
            "actual_soc": [],
            "energy_remaining": [],
            "charge_power": [],
            "voltage": [],
            "current": [],
            "dc_current": [],
            "time_to_full": [],
            "battery_temp": [],
            "outside_temp": [],
            "ambient_temp": [],
        }

        if latest_session:
            for row in latest_session:
                try:
                    soc_val = round(float(row["soc_percent"]), 1) if row.get("soc_percent") is not None else None
                except (TypeError, ValueError):
                    soc_val = None
                try:
                    actual_soc_val = round(float(row["actual_soc_percent"]), 1) if row.get("actual_soc_percent") is not None else None
                except (TypeError, ValueError):
                    actual_soc_val = None
                try:
                    energy_remaining_val = round(float(row["energy_remaining_kwh"]), 2) if row.get("energy_remaining_kwh") is not None else None
                except (TypeError, ValueError):
                    energy_remaining_val = None
                try:
                    charge_power_val = round(float(row["charge_power_kw"]), 2) if row.get("charge_power_kw") is not None else None
                except (TypeError, ValueError):
                    charge_power_val = None
                try:
                    voltage_val = int(float(row["charger_voltage"])) if row.get("charger_voltage") is not None else None
                except (TypeError, ValueError):
                    voltage_val = None
                try:
                    current_val = round(float(row["charger_current"]), 1) if row.get("charger_current") is not None else None
                except (TypeError, ValueError):
                    current_val = None
                try:
                    dc_current_val = round(float(row["evse_dc_current"]), 1) if row.get("evse_dc_current") is not None else None
                except (TypeError, ValueError):
                    dc_current_val = None
                try:
                    time_to_full_val = round(float(row["time_to_full_min"]), 0) if row.get("time_to_full_min") is not None else None
                except (TypeError, ValueError):
                    time_to_full_val = None

                if soc_val is not None and not (0 <= soc_val <= 100):
                    soc_val = None
                if actual_soc_val is not None and not (0 <= actual_soc_val <= 100):
                    actual_soc_val = None
                if energy_remaining_val is not None and energy_remaining_val < 0:
                    energy_remaining_val = None
                if charge_power_val is not None and not (0 <= charge_power_val <= 500):
                    charge_power_val = None
                if voltage_val is not None and not (80 <= voltage_val <= 520):
                    voltage_val = None
                if current_val is not None and not (0 <= current_val <= 1000):
                    current_val = None
                if dc_current_val is not None and not (0 <= dc_current_val <= 1000):
                    dc_current_val = None
                if time_to_full_val is not None and time_to_full_val < 0:
                    time_to_full_val = None

                battery_temp_val = (
                    round(units.convert_for_display(row["battery_temp_c"], "battery_temp_c", system), 1)
                    if row.get("battery_temp_c") is not None else None
                )
                outside_temp_val = (
                    round(units.convert_for_display(row["outside_temp_c"], "outside_temp_c", system), 1)
                    if row.get("outside_temp_c") is not None else None
                )
                ambient_temp_val = (
                    round(units.convert_for_display(row["ambient_temp_c"], "ambient_temp_c", system), 1)
                    if row.get("ambient_temp_c") is not None else None
                )

                charging_chart_data["labels"].append(_format_local_datetime(row.get("polled_at"), "%m-%d %H:%M"))
                charging_chart_data["soc"].append(soc_val)
                charging_chart_data["actual_soc"].append(actual_soc_val)
                charging_chart_data["energy_remaining"].append(energy_remaining_val)
                charging_chart_data["charge_power"].append(charge_power_val)
                charging_chart_data["voltage"].append(voltage_val)
                charging_chart_data["current"].append(current_val)
                charging_chart_data["dc_current"].append(dc_current_val)
                charging_chart_data["time_to_full"].append(time_to_full_val)
                charging_chart_data["battery_temp"].append(battery_temp_val)
                charging_chart_data["outside_temp"].append(outside_temp_val)
                charging_chart_data["ambient_temp"].append(ambient_temp_val)

        max_chart_points = 48
        point_count = len(charging_chart_data["labels"])
        if point_count > max_chart_points:
            step = max(1, point_count // max_chart_points)
            sampled_indices = list(range(0, point_count, step))
            if sampled_indices[-1] != point_count - 1:
                sampled_indices.append(point_count - 1)
            for key, series in charging_chart_data.items():
                charging_chart_data[key] = [series[idx] for idx in sampled_indices]

        return charging_chart_data

    def _display_timezone() -> tuple[timezone | ZoneInfo, str]:
        """Resolve configured display timezone, with safe UTC fallback."""
        if db.is_available():
            tz_name = (_get_setting("timezone") or _SETTINGS_DEFAULTS["timezone"]).strip() or "UTC"
        else:
            tz_name = _SETTINGS_DEFAULTS["timezone"]
        try:
            return ZoneInfo(tz_name), tz_name
        except ZoneInfoNotFoundError:
            return timezone.utc, "UTC"

    def _format_local_datetime(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """Format datetimes in configured local timezone for UI display."""
        if value is None:
            return "-"
        try:
            tz_obj, _ = _display_timezone()
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(tz_obj).strftime(fmt)
        except Exception:
            return value.strftime(fmt)

    def _build_vehicle_summary(
        vehicle: dict,
        battery: dict,
        charging: dict,
        environment: dict,
        security: dict,
        doors: list[dict],
        windows: list[dict],
    ) -> dict:
        """Build a compact, dashboard-friendly snapshot from multiple state tables."""
        charging_active = _charging_is_active(charging if charging else None)
        plug_connected = _plug_status_connected(charging.get("plug_status") if charging else None)

        door_open_count = 0
        door_unlocked_count = 0
        for row in doors:
            status_val = (row.get("status") or "").strip().lower()
            lock_val = (row.get("lock_status") or "").strip().lower()
            if status_val and status_val not in ("closed", "close", "shut"):
                door_open_count += 1
            if "unlock" in lock_val:
                door_unlocked_count += 1

        window_open_count = 0
        for row in windows:
            try:
                upper = float(row.get("upper_bound")) if row.get("upper_bound") is not None else 0.0
                lower = float(row.get("lower_bound")) if row.get("lower_bound") is not None else 0.0
                if max(abs(upper), abs(lower)) > 0.5:
                    window_open_count += 1
            except (TypeError, ValueError):
                continue

        climate_active = False
        try:
            climate_active = float(security.get("remote_start_countdown") or 0) > 0
        except (TypeError, ValueError):
            climate_active = False

        battery_draw_kw = None
        raw_battery_kw = None
        try:
            voltage = battery.get("voltage") if battery else None
            current = battery.get("current") if battery else None
            if voltage is not None and current is not None:
                raw_battery_kw = (float(voltage) * float(current)) / 1000.0
                if abs(raw_battery_kw) >= 0.05:
                    battery_draw_kw = round(abs(raw_battery_kw), 2)
        except (TypeError, ValueError):
            battery_draw_kw = None
            raw_battery_kw = None

        climate_energy_kw = None
        if climate_active and not charging_active and battery_draw_kw is not None:
            climate_energy_kw = battery_draw_kw

        return {
            "ignition": vehicle.get("ignition_status"),
            "gear": vehicle.get("gear_position"),
            "speed": vehicle.get("speed_mph"),
            "soc": battery.get("soc_percent"),
            "range": battery.get("range_miles"),
            "outside_temp": environment.get("outside_temp_c"),
            "ambient_temp": environment.get("ambient_temp_c"),
            "battery_temp": battery.get("temperature_c"),
            "plug_connected": plug_connected,
            "charging_active": charging_active,
            "charge_power_kw": _charging_power_kw_from_row(charging if charging else None),
            "door_open_count": door_open_count,
            "door_unlocked_count": door_unlocked_count,
            "window_open_count": window_open_count,
            "climate_active": climate_active,
            "remote_start_countdown": security.get("remote_start_countdown"),
            "battery_draw_kw": battery_draw_kw,
            "raw_battery_kw": raw_battery_kw,
            "climate_energy_kw": climate_energy_kw,
        }

    # ── Register Jinja globals for unit conversion ─────────────────

    @app.context_processor
    def _inject_units():
        system = _get_setting("units") if db.is_available() else "imperial"
        _, tz_name = _display_timezone()

        def _ulabel_for_field(field_name):
            """Return the unit label for a DB field name, or empty string."""
            cat = units.FIELD_CATEGORIES.get(field_name)
            if cat:
                return units.unit_label(cat, system)
            return ""

        return {
            "unit_system": system,
            "convert": lambda val, field: units.convert_for_display(val, field, system),
            "ulabel": lambda cat: units.unit_label(cat, system),
            "ulabel_for_field": _ulabel_for_field,
            "format_local_dt": _format_local_datetime,
            "display_timezone": tz_name,
        }

    # ── Startup checks ─────────────────────────────────────────────

    def _active_vin() -> str | None:
        """Return the current active VIN from the garage table (may be None)."""
        if not db.is_available():
            return None
        return db.active_vin()

    def _needs_setup() -> bool:
        """Check whether the system still needs OAuth / garage configuration."""
        if not db.is_available():
            return True
        vin = _active_vin()
        if not vin:
            return True
        creds = db.fetch_one(
            "SELECT id FROM oauth_credentials WHERE vin = %s AND enabled = TRUE", (vin,)
        )
        return creds is None

    # Run startup DB migrations once the table helpers are available.
    if db.is_available():
        try:
            migrated_items = _run_startup_migrations()
            if migrated_items:
                app.config["STARTUP_DB_NOTICE"] = (
                    "Database schema auto-updated for this app version: "
                    + ", ".join(migrated_items)
                )
                log.info("Startup DB migration applied: %s", ", ".join(migrated_items))
        except Exception as exc:
            log.error("Startup DB migration failed: %s", exc)

    # ── Request hook: force HTTPS when SSL is active ─────────────

    @app.before_request
    def _force_https():
        """Redirect HTTP requests to HTTPS when SSL is enabled."""
        if app.config.get("SSL_ACTIVE") and not request.is_secure:
            url = request.url.replace("http://", "https://", 1)
            return redirect(url, code=301)

    # ── Request hook: redirect to setup if not configured ──────────

    _SETUP_SAFE_ENDPOINTS = frozenset({
        "db_setup", "db_setup_test", "db_setup_create",
        "db_setup_restore", "db_setup_upload",
        "oauth_config", "reset", "manage", "manage_delete_vin",
        "manage_repoll", "db_browser", "db_table", "db_delete_row",
        "settings", "backup_page", "backup_create", "backup_restore",
        "backup_download", "backup_delete", "backup_upload", "static",
    })

    @app.before_request
    def _check_setup():
        # Show one-time startup DB migration notice.
        startup_notice = app.config.get("STARTUP_DB_NOTICE")
        if startup_notice:
            flash(startup_notice, "success")
            app.config["STARTUP_DB_NOTICE"] = None

        # If DB is not connected, only allow the database setup pages
        if not db.is_available():
            if request.endpoint not in ("db_setup", "db_setup_test",
                                         "db_setup_create", "db_setup_restore",
                                         "db_setup_upload", "static"):
                return redirect(url_for("db_setup"))
            return

        if _needs_setup() and request.endpoint not in _SETUP_SAFE_ENDPOINTS:
            return redirect(url_for("oauth_config"))

    # ── Database setup routes (no-DB mode) ─────────────────────────

    def _setup_backup_list() -> list[dict]:
        """List backup files with formatted sizes (works without DB)."""
        backups_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        if not os.path.isdir(backups_dir):
            return []
        files = []
        for name in sorted(os.listdir(backups_dir), reverse=True):
            if name.endswith((".sql", ".json")):
                full = os.path.join(backups_dir, name)
                size = os.path.getsize(full)
                files.append({"name": name, "size": size, "size_fmt": backup._format_size(size)})
        return files

    @app.route("/setup", methods=["GET", "POST"])
    def db_setup():
        """Database setup page — shown when PostgreSQL is unreachable."""
        db_cfg = config.database()
        if request.method == "POST":
            new_cfg = {
                "host": request.form.get("host", "localhost").strip(),
                "port": int(request.form.get("port", 5432)),
                "name": request.form.get("name", "lightning").strip(),
                "user": request.form.get("user", "lightning").strip(),
                "password": request.form.get("password", "").strip(),
                "connect_timeout": int(request.form.get("connect_timeout", 10)),
            }
            config.save_database(new_cfg)
            db_cfg = new_cfg

            # Attempt to connect with the new settings
            try:
                db.init_pool()
                flash("Database connected successfully!", "success")
                return redirect(url_for("db_setup"))
            except Exception as exc:
                flash(f"Connection failed: {exc}", "error")

        return render_template("db_setup.html", db=db_cfg, connected=db.is_available(),
                               backup_files=_setup_backup_list())

    @app.route("/setup/test", methods=["POST"])
    def db_setup_test():
        """Test a database connection without saving."""
        ok, msg = db.test_connection(
            host=request.form.get("host", "localhost").strip(),
            port=int(request.form.get("port", 5432)),
            name=request.form.get("name", "lightning").strip(),
            user=request.form.get("user", "lightning").strip(),
            password=request.form.get("password", "").strip(),
            timeout=5,
        )
        flash(msg, "success" if ok else "error")
        return redirect(url_for("db_setup"))

    @app.route("/setup/create-schema", methods=["POST"])
    def db_setup_create():
        """Apply schema.sql to create all tables."""
        if not db.is_available():
            flash("Connect to the database first.", "error")
            return redirect(url_for("db_setup"))

        ok, msg = db.apply_schema()
        flash(msg, "success" if ok else "error")
        return redirect(url_for("db_setup"))

    @app.route("/setup/restore", methods=["POST"])
    def db_setup_restore():
        """Restore a backup during setup."""
        if not db.is_available():
            flash("Connect to the database first.", "error")
            return redirect(url_for("db_setup"))

        filename = request.form.get("filename", "")
        if not filename:
            flash("No backup file selected.", "error")
            return redirect(url_for("db_setup"))

        safe_name = os.path.basename(filename)
        filepath = os.path.join(backup.BACKUP_DIR, safe_name)
        if not os.path.isfile(filepath):
            flash("Backup file not found.", "error")
            return redirect(url_for("db_setup"))

        try:
            # Apply schema first to ensure tables exist
            db.apply_schema()
            if safe_name.endswith(".sql"):
                backup.restore_sql(filepath)
                flash(f"SQL restore complete: {safe_name}", "success")
            elif safe_name.endswith(".json"):
                summary = backup.restore_json(filepath)
                total = sum(summary.values())
                flash(f"JSON restore complete: {total} rows from {safe_name}", "success")
            else:
                flash("Unknown backup format.", "error")
        except Exception as exc:
            log.error("Setup restore failed: %s", exc)
            flash(f"Restore failed: {exc}", "error")
        return redirect(url_for("db_setup"))

    @app.route("/setup/upload", methods=["POST"])
    def db_setup_upload():
        """Upload a backup file during setup (DB may not be connected yet)."""
        file = request.files.get("backup_file")
        if not file or file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("db_setup"))

        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ("sql", "json"):
            flash("Only .sql and .json backup files are accepted.", "error")
            return redirect(url_for("db_setup"))

        safe_name = secure_filename(file.filename)
        save_path = os.path.join(backup.BACKUP_DIR, safe_name)
        os.makedirs(backup.BACKUP_DIR, exist_ok=True)
        file.save(save_path)
        flash(f"Uploaded: {safe_name}", "success")
        return redirect(url_for("db_setup"))

    # ── Routes ─────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        """Main dashboard showing vehicle overview, battery, poller status."""
        vin = _active_vin()
        garage = db.fetch_one("SELECT * FROM garage WHERE vin = %s", (vin,)) if vin else None
        status = db.fetch_one("SELECT * FROM collector_status WHERE vin = %s", (vin,)) if vin else None
        battery = db.fetch_one("SELECT * FROM battery_state WHERE vin = %s", (vin,)) if vin else None
        vehicle = db.fetch_one("SELECT * FROM vehicle_state WHERE vin = %s", (vin,)) if vin else None
        charging = db.fetch_one("SELECT * FROM charging_state WHERE vin = %s", (vin,)) if vin else None
        tires = db.fetch_all("SELECT * FROM tire_state WHERE vin = %s ORDER BY wheel_position", (vin,)) if vin else []
        environment = db.fetch_one("SELECT * FROM environment_state WHERE vin = %s", (vin,)) if vin else None
        security = db.fetch_one("SELECT * FROM security_state WHERE vin = %s", (vin,)) if vin else None
        doors = db.fetch_all("SELECT * FROM door_state WHERE vin = %s", (vin,)) if vin else []
        windows = db.fetch_all("SELECT * FROM window_state WHERE vin = %s", (vin,)) if vin else []

        vehicle_summary = _build_vehicle_summary(
            vehicle or {},
            battery or {},
            charging or {},
            environment or {},
            security or {},
            doors,
            windows,
        )

        # Determine vehicle image filename
        vehicle_img = _get_setting("vehicle_image") or "vehicle.png"
        refresh_interval = request.args.get("refresh", 0, type=int)

        return render_template(
            "dashboard.html",
            vin=vin,
            garage=garage,
            status=status,
            battery=battery,
            vehicle=vehicle,
            charging=charging,
            plug_connected=_plug_status_connected(charging.get("plug_status") if charging else None),
            charging_active=_charging_is_active(charging),
            charging_power_kw=_charging_power_kw_from_row(charging),
            vehicle_summary=vehicle_summary,
            tires=tires,
            vehicle_img=vehicle_img,
            poller_running=poller.is_running(),
            refresh_interval=refresh_interval,
        )

    @app.route("/vehicle")
    def vehicle_state():
        """Detailed view of all state tables for the active VIN."""
        vin = _active_vin()
        unit_system = _get_setting("units") if db.is_available() else "imperial"
        refresh_interval = request.args.get("refresh", 0, type=int)
        states = {}
        tables = [
            "vehicle_state", "battery_state", "charging_state", "location_state",
            "brake_state", "security_state", "environment_state",
        ]
        for t in tables:
            states[t] = db.fetch_one(f"SELECT * FROM {t} WHERE vin = %s", (vin,)) if vin else None

        # Composite-key tables
        states["tire_state"] = db.fetch_all("SELECT * FROM tire_state WHERE vin = %s", (vin,)) if vin else []
        states["door_state"] = db.fetch_all("SELECT * FROM door_state WHERE vin = %s", (vin,)) if vin else []
        states["window_state"] = db.fetch_all("SELECT * FROM window_state WHERE vin = %s", (vin,)) if vin else []

        vehicle = states.get("vehicle_state") or {}
        battery = states.get("battery_state") or {}
        charging = states.get("charging_state") or {}
        environment = states.get("environment_state") or {}
        security = states.get("security_state") or {}
        doors = states.get("door_state") or []
        windows = states.get("window_state") or []

        vehicle_summary = _build_vehicle_summary(
            vehicle,
            battery,
            charging,
            environment,
            security,
            doors,
            windows,
        )

        return render_template(
            "vehicle_state.html",
            vin=vin,
            states=states,
            unit_system=unit_system,
            vehicle_summary=vehicle_summary,
            refresh_interval=refresh_interval,
        )

    @app.route("/telemetry")
    def telemetry_overview():
        """Show telemetry count, latest poll time, and recent poll history."""
        vin = _active_vin()
        count_row = db.fetch_one("SELECT count(*) AS cnt FROM telemetry WHERE vin = %s", (vin,)) if vin else None
        count = count_row["cnt"] if count_row else 0
        latest = db.fetch_one(
            "SELECT polled_at FROM telemetry WHERE vin = %s ORDER BY polled_at DESC LIMIT 1", (vin,)
        ) if vin else None
        recent = db.fetch_all(
            "SELECT id, polled_at, created_at FROM telemetry WHERE vin = %s ORDER BY polled_at DESC LIMIT 20",
            (vin,),
        ) if vin else []
        return render_template(
            "telemetry.html", vin=vin, count=count,
            latest=latest, recent=recent,
        )

    @app.route("/charging")
    def charging_overview():
        """Show current charging state plus recent charging history samples."""
        vin = _active_vin()
        system = _get_setting("units") if db.is_available() else "imperial"
        charging = db.fetch_one("SELECT * FROM charging_state WHERE vin = %s", (vin,)) if vin else None
        charging_display = dict(charging) if charging else None
        if charging_display and not _charging_is_active(charging_display):
            charging_display["time_to_full_min"] = None
            charging_display["charger_current"] = None
            charging_display["charger_voltage"] = None
            charging_display["evse_dc_current"] = None
        battery = db.fetch_one("SELECT * FROM battery_state WHERE vin = %s", (vin,)) if vin else None
        environment = db.fetch_one("SELECT * FROM environment_state WHERE vin = %s", (vin,)) if vin else None

        history = []
        history_rows = []
        if vin and _table_exists("charging_history"):
            history_rows = db.fetch_all(
                "SELECT * FROM charging_history WHERE vin = %s ORDER BY polled_at DESC LIMIT 400",
                (vin,),
            )

            # Avoid stale unplugged rows in UI/charts (legacy data may contain these).
            meaningful_rows = [row for row in history_rows if _charging_sample_meaningful(row)]
            history = meaningful_rows[:100]

        latest_session = _latest_charging_session(history) if history else []

        charging_chart_data = _build_charging_chart_data(latest_session, system)

        charging_axis = _charging_axis_for_mode(
            _charging_mode_from_data(charging, charging_chart_data["voltage"])
        )
        charging_chart_data["voltage"] = [
            v if (v is None or (charging_axis["min"] <= v <= charging_axis["max"])) else None
            for v in charging_chart_data["voltage"]
        ]

        refresh_interval = request.args.get("refresh", 0, type=int)

        return render_template(
            "charging.html",
            vin=vin,
            charging=charging_display,
            plug_connected=_plug_status_connected(charging.get("plug_status") if charging else None),
            charging_active=_charging_is_active(charging),
            charging_power_kw=_charging_power_kw_from_row(charging_display),
            battery=battery,
            environment=environment,
            history=history,
            history_available=_table_exists("charging_history"),
            charging_chart_data=charging_chart_data,
            charging_axis=charging_axis,
            latest_session=latest_session,
            refresh_interval=refresh_interval,
            display_timezone=_display_timezone()[1],
        )

    @app.route("/charging/sessions")
    def charging_sessions_view():
        """Dedicated charging session view focused on SOC, charge rate, and temperatures."""
        vin = _active_vin()
        system = _get_setting("units") if db.is_available() else "imperial"
        selected_session_uuid = (request.args.get("session") or "").strip()
        refresh_interval = request.args.get("refresh", 0, type=int)
        charging = db.fetch_one("SELECT * FROM charging_state WHERE vin = %s", (vin,)) if vin else None

        history_rows = []
        if vin and _table_exists("charging_history"):
            history_rows = db.fetch_all(
                "SELECT * FROM charging_history WHERE vin = %s ORDER BY polled_at DESC LIMIT 800",
                (vin,),
            )

        meaningful_rows = [row for row in history_rows if _charging_sample_meaningful(row)]
        selected_session_rows = []
        if selected_session_uuid:
            selected_session_rows = [
                row for row in meaningful_rows
                if str(row.get("charging_session_uuid") or "") == selected_session_uuid
            ]

        latest_session = selected_session_rows or (_latest_charging_session(meaningful_rows) if meaningful_rows else [])
        chart_data = _build_charging_chart_data(latest_session, system)
        sessions_summary = []
        if vin and _table_exists("charging_sessions"):
            session_rows = db.fetch_all(
                """
                SELECT session_uuid, started_at, ended_at, in_progress,
                       start_soc_percent, end_soc_percent,
                       start_energy_remaining_kwh, end_energy_remaining_kwh,
                       max_power_kw, sample_count
                FROM charging_sessions
                WHERE vin = %s
                ORDER BY started_at DESC
                LIMIT 25
                """,
                (vin,),
            )
            for row in session_rows:
                start_soc = row.get("start_soc_percent")
                end_soc = row.get("end_soc_percent")
                try:
                    soc_delta = (float(end_soc) - float(start_soc)) if start_soc is not None and end_soc is not None else None
                except (TypeError, ValueError):
                    soc_delta = None

                started_at = row.get("started_at")
                ended_at = row.get("ended_at")
                duration_min = None
                if started_at and ended_at:
                    try:
                        duration_min = max(0.0, (ended_at - started_at).total_seconds() / 60.0)
                    except Exception:
                        duration_min = None

                session_uuid = str(row.get("session_uuid")) if row.get("session_uuid") else None
                sessions_summary.append(
                    {
                        "session_label": (session_uuid[:8] if session_uuid else "session"),
                        "session_uuid": session_uuid,
                        "selected": bool(session_uuid and session_uuid == selected_session_uuid),
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "in_progress": bool(row.get("in_progress")),
                        "duration_min": duration_min,
                        "start_soc": start_soc,
                        "end_soc": end_soc,
                        "soc_delta": soc_delta,
                        "max_power_kw": row.get("max_power_kw"),
                        "samples": row.get("sample_count") or 0,
                    }
                )
        else:
            sessions_summary = _charging_sessions_summary(meaningful_rows, limit=25)
            for s in sessions_summary:
                s_uuid = str(s.get("session_uuid") or "")
                s["selected"] = bool(s_uuid and s_uuid == selected_session_uuid)

        selected_session_label = None
        if selected_session_uuid:
            selected_session_label = selected_session_uuid[:8]
        elif sessions_summary:
            selected_session_label = sessions_summary[0].get("session_label")

        return render_template(
            "charging_sessions.html",
            vin=vin,
            charging=charging,
            charging_active=_charging_is_active(charging),
            charging_power_kw=_charging_power_kw_from_row(charging),
            latest_session=latest_session,
            sessions_summary=sessions_summary,
            selected_session_uuid=selected_session_uuid,
            selected_session_label=selected_session_label,
            chart_data=chart_data,
            refresh_interval=refresh_interval,
            display_timezone=_display_timezone()[1],
        )

    @app.route("/analytics")
    def analytics_overview():
        """Show phase-1 analytics charts and a latest-drive route preview map."""
        vin = _active_vin()
        system = _get_setting("units") if db.is_available() else "imperial"

        drive_rows = db.fetch_all(
            """
            SELECT d.id,
                   d.drive_uuid,
                   d.started_at,
                   d.ended_at,
                   d.distance_km,
                   d.energy_used_kwh,
                   d.start_soc_percent,
                   d.end_soc_percent
            FROM drives d
            WHERE d.vin = %s
            ORDER BY d.started_at DESC
            LIMIT 60
            """,
            (vin,),
        ) if vin else []

        labels = []
        distance_data = []
        energy_data = []
        efficiency_data = []

        for row in reversed(drive_rows):
            labels.append(_format_local_datetime(row.get("started_at"), "%m-%d %H:%M"))

            distance_km = row.get("distance_km")
            energy_kwh = row.get("energy_used_kwh")
            start_soc = row.get("start_soc_percent")
            end_soc = row.get("end_soc_percent")

            if distance_km is not None:
                distance_data.append(round(units.convert_for_display(distance_km, "distance_km", system), 2))
            else:
                distance_data.append(None)

            energy_data.append(round(float(energy_kwh), 2) if energy_kwh is not None else None)

            if energy_kwh is not None and distance_km is not None and float(distance_km) > 0:
                distance_display = units.convert_for_display(distance_km, "distance_km", system)
                # Ignore tiny-distance samples that create unrealistic efficiency spikes.
                if distance_display >= 0.5:
                    efficiency_val = (float(energy_kwh) / distance_display) * 100.0
                    if 0 <= efficiency_val <= 200:
                        efficiency_data.append(round(efficiency_val, 2))
                    else:
                        efficiency_data.append(None)
                else:
                    efficiency_data.append(None)
            else:
                efficiency_data.append(None)

        # Charging history data (if available)
        charging_data = {"labels": [], "soc": [], "voltage": []}
        charging = db.fetch_one("SELECT * FROM charging_state WHERE vin = %s", (vin,)) if vin else None
        if _table_exists("charging_history"):
            charging_rows = db.fetch_all(
                """
                SELECT polled_at, charger_voltage, soc_percent
                FROM charging_history
                WHERE vin = %s
                ORDER BY polled_at ASC
                LIMIT 500
                """,
                (vin,),
            )
            for row in charging_rows:
                if not _charging_sample_meaningful(row):
                    continue

                soc_raw = row.get("soc_percent")
                voltage_raw = row.get("charger_voltage")
                try:
                    soc_val = round(float(soc_raw), 1) if soc_raw is not None else None
                except (TypeError, ValueError):
                    soc_val = None
                try:
                    voltage_val = int(float(voltage_raw)) if voltage_raw is not None else None
                except (TypeError, ValueError):
                    voltage_val = None

                if soc_val is not None and not (0 <= soc_val <= 100):
                    soc_val = None
                if voltage_val is not None and not (80 <= voltage_val <= 500):
                    voltage_val = None

                charging_data["labels"].append(_format_local_datetime(row.get("polled_at"), "%m-%d %H:%M"))
                charging_data["soc"].append(soc_val)
                charging_data["voltage"].append(voltage_val)

        charging_axis = _charging_axis_for_mode(
            _charging_mode_from_data(charging, charging_data["voltage"])
        )
        charging_data["voltage"] = [
            v if (v is None or (charging_axis["min"] <= v <= charging_axis["max"])) else None
            for v in charging_data["voltage"]
        ]

        latest_drive = db.fetch_one(
            """
            SELECT id, drive_uuid, started_at, ended_at, in_progress
            FROM drives
            WHERE vin = %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (vin,),
        ) if vin else None

        map_points = []
        if latest_drive:
            point_rows = db.fetch_all(
                """
                SELECT recorded_at, latitude, longitude, speed_kmh
                FROM drive_points
                WHERE drive_id = %s
                  AND latitude IS NOT NULL
                  AND longitude IS NOT NULL
                ORDER BY recorded_at ASC
                """,
                (latest_drive["id"],),
            )
            for point in point_rows:
                map_points.append({
                    "lat": float(point["latitude"]),
                    "lon": float(point["longitude"]),
                    "time": _format_local_datetime(point.get("recorded_at"), "%Y-%m-%d %H:%M:%S"),
                    "speed": (
                        round(units.convert_for_display(point["speed_kmh"], "speed_kmh", system), 1)
                        if point.get("speed_kmh") is not None else None
                    ),
                })

        chart_data = {
            "labels": labels,
            "distance": distance_data,
            "energy": energy_data,
            "efficiency": efficiency_data,
        }

        return render_template(
            "analytics.html",
            vin=vin,
            chart_data=chart_data,
            charging_data=charging_data,
            charging_axis=charging_axis,
            latest_drive=latest_drive,
            map_points=map_points,
            distance_label=units.unit_label("distance", system),
            speed_label=units.unit_label("speed", system),
            efficiency_label=f"kWh/100{units.unit_label('distance', system)}",
            display_timezone=_display_timezone()[1],
        )

    @app.route("/drives")
    def drives_list():
        """Show all drives with summary counters and status."""
        vin = _active_vin()
        raw_drives = db.fetch_all(
            """SELECT d.*,
                      (SELECT count(*) FROM drive_points WHERE drive_id = d.id) AS point_count
               FROM drives d
               WHERE d.vin = %s
               ORDER BY d.started_at DESC LIMIT 200""",
            (vin,),
        ) if vin else []

        drives = []
        for d in raw_drives:
            row = dict(d)

            duration_sec = row.get("duration_sec")
            if duration_sec is None and row.get("started_at") and row.get("ended_at"):
                duration_sec = int((row["ended_at"] - row["started_at"]).total_seconds())
            row["summary_duration_sec"] = duration_sec

            distance_km = row.get("distance_km")
            energy_kwh = row.get("energy_used_kwh")
            miles_driven = None
            avg_mi_per_kwh = None
            if distance_km is not None:
                try:
                    miles_driven = float(distance_km) * 0.621371
                except (TypeError, ValueError):
                    miles_driven = None
            if miles_driven is not None and energy_kwh is not None:
                try:
                    energy_val = float(energy_kwh)
                    if energy_val > 0:
                        avg_mi_per_kwh = miles_driven / energy_val
                except (TypeError, ValueError):
                    avg_mi_per_kwh = None

            row["summary_miles_driven"] = miles_driven
            row["summary_avg_mi_per_kwh"] = avg_mi_per_kwh
            row["summary_kwh_remaining"] = row.get("end_energy_kwh")
            drives.append(row)

        total_count = len(drives)
        completed_count = sum(1 for d in drives if not d.get("in_progress"))
        active_count = total_count - completed_count

        distance_values_km = [float(d["distance_km"]) for d in drives if d.get("distance_km") is not None]
        energy_values_kwh = [float(d["energy_used_kwh"]) for d in drives if d.get("energy_used_kwh") is not None]
        max_speed_values_kmh = [float(d["max_speed_kmh"]) for d in drives if d.get("max_speed_kmh") is not None]

        drive_stats = {
            "total_distance_km": sum(distance_values_km) if distance_values_km else None,
            "avg_distance_km": (sum(distance_values_km) / len(distance_values_km)) if distance_values_km else None,
            "total_energy_kwh": sum(energy_values_kwh) if energy_values_kwh else None,
            "avg_energy_kwh": (sum(energy_values_kwh) / len(energy_values_kwh)) if energy_values_kwh else None,
            "avg_max_speed_kmh": (sum(max_speed_values_kmh) / len(max_speed_values_kmh)) if max_speed_values_kmh else None,
        }

        return render_template(
            "drives.html",
            vin=vin,
            drives=drives,
            total_count=total_count,
            completed_count=completed_count,
            active_count=active_count,
            drive_stats=drive_stats,
        )

    @app.route("/drives/<int:drive_id>")
    def drive_detail(drive_id):
        """Show a single drive with all its data points and simple analytics."""
        drive = db.fetch_one("SELECT * FROM drives WHERE id = %s", (drive_id,))
        if not drive:
            flash("Drive not found.", "error")
            return redirect(url_for("drives_list"))

        points = db.fetch_all(
            "SELECT * FROM drive_points WHERE drive_id = %s ORDER BY recorded_at ASC",
            (drive_id,),
        )

        system = _get_setting("units") if db.is_available() else "imperial"

        # Some historical rows stored Ford speed metric as m/s in speed_kmh.
        # Detect this per-drive and normalize to km/h so charts/tables are accurate.
        speed_scale_to_kmh = 1.0
        raw_speed_values = []
        for p in points:
            if p.get("speed_kmh") is None:
                continue
            try:
                raw_speed_values.append(float(p["speed_kmh"]))
            except (TypeError, ValueError):
                continue

        if raw_speed_values:
            raw_max_speed = max(raw_speed_values)
            avg_kmh = None
            try:
                if drive.get("distance_km") is not None and drive.get("duration_sec"):
                    duration_hours = float(drive["duration_sec"]) / 3600.0
                    if duration_hours > 0:
                        avg_kmh = float(drive["distance_km"]) / duration_hours
            except (TypeError, ValueError, ZeroDivisionError):
                avg_kmh = None

            # If average speed exceeds observed max, stored values are almost certainly m/s.
            if avg_kmh is not None and raw_max_speed > 0 and avg_kmh > (raw_max_speed * 1.05):
                speed_scale_to_kmh = 3.6

        labels = []
        speed_series = []
        soc_series = []
        energy_series = []
        energy_used_series = []
        efficiency_series = []
        battery_temp_series = []
        outside_temp_series = []
        elevation_series = []

        # Initialize temp variables to prevent undefined variable errors
        speed_val = None
        soc_val = None
        energy_val = None
        energy_used_val = None
        efficiency_val = None
        battery_temp_val = None
        outside_temp_val = None
        elevation_val = None

        # Calculate starting energy and odometer for cumulative calculations
        starting_energy_kwh = None
        starting_odometer_km = None
        if points:
            if points[0].get("energy_remaining_kwh") is not None:
                starting_energy_kwh = float(points[0]["energy_remaining_kwh"])
            if points[0].get("odometer_km") is not None:
                starting_odometer_km = float(points[0]["odometer_km"])

        for row in points:
            labels.append(_format_local_datetime(row.get("recorded_at"), "%H:%M:%S"))
            speed_val = None
            if row.get("speed_kmh") is not None:
                speed_kmh_normalized = float(row["speed_kmh"]) * speed_scale_to_kmh
                speed_val = units.convert_for_display(speed_kmh_normalized, "speed_kmh", system)
                # Do not round here; keep raw converted value for chart
                if not (0 <= speed_val <= 120):
                    speed_val = None

            soc_val = round(float(row["soc_percent"]), 1) if row.get("soc_percent") is not None else None
            if soc_val is not None and not (0 <= soc_val <= 100):
                soc_val = None

            energy_val = round(float(row["energy_remaining_kwh"]), 2) if row.get("energy_remaining_kwh") is not None else None
            if energy_val is not None and energy_val < 0:
                energy_val = None

            energy_used_val = None
            if starting_energy_kwh is not None and energy_val is not None:
                energy_used_val = round(max(0, starting_energy_kwh - energy_val), 2)

            efficiency_val = None
            if (starting_odometer_km is not None and 
                row.get("odometer_km") is not None and 
                energy_used_val is not None and 
                energy_used_val > 0):
                try:
                    distance_km = float(row["odometer_km"]) - starting_odometer_km
                    distance_miles = distance_km * 0.621371
                    if distance_miles > 0:
                        efficiency_val = round(distance_miles / energy_used_val, 2)
                except (TypeError, ValueError):
                    efficiency_val = None

            battery_temp_val = (
                round(units.convert_for_display(row["battery_temp_c"], "battery_temp_c", system), 1)
                if row.get("battery_temp_c") is not None else None
            )

            elevation_val = (
                round(units.convert_for_display(row["altitude_m"], "altitude_m", system), 1)
                if row.get("altitude_m") is not None else None
            )

            outside_temp_val = (
                round(units.convert_for_display(row["outside_temp_c"], "outside_temp_c", system), 1)
                if row.get("outside_temp_c") is not None else None
            )
            speed_series.append(speed_val)
            soc_series.append(soc_val)
            energy_series.append(energy_val)
            energy_used_series.append(energy_used_val)
            efficiency_series.append(efficiency_val)
            battery_temp_series.append(battery_temp_val)
            outside_temp_series.append(outside_temp_val)
            elevation_series.append(elevation_val)

        max_chart_points = 24
        point_count = len(labels)
        speed_axis_max = None
        full_speed_values = [v for v in speed_series if v is not None]
        if full_speed_values:
            speed_axis_max = max(full_speed_values)
        if drive.get("max_speed_kmh") is not None:
            try:
                drive_max_speed = round(
                    units.convert_for_display(float(drive["max_speed_kmh"]) * speed_scale_to_kmh, "max_speed_kmh", system),
                    1,
                )
                if speed_axis_max is None or drive_max_speed > speed_axis_max:
                    speed_axis_max = drive_max_speed
            except (TypeError, ValueError):
                pass

        if point_count > max_chart_points:
            step = max(1, point_count // max_chart_points)
            sampled_indices = list(range(0, point_count, step))
            if sampled_indices[-1] != point_count - 1:
                sampled_indices.append(point_count - 1)

            # Downsample by taking the max speed in each interval for more accurate charting
            def max_in_interval(series, indices):
                result = []
                for i in range(len(indices) - 1):
                    interval = [v for v in series[indices[i]:indices[i+1]] if v is not None]
                    result.append(max(interval) if interval else None)
                # Last interval
                interval = [v for v in series[indices[-2]:indices[-1]+1] if v is not None] if len(indices) > 1 else []
                result.append(max(interval) if interval else None)
                return result

            labels = [labels[idx] for idx in sampled_indices]
            speed_series = max_in_interval(speed_series, sampled_indices)
            soc_series = [soc_series[idx] for idx in sampled_indices]
            energy_series = [energy_series[idx] for idx in sampled_indices]
            energy_used_series = [energy_used_series[idx] for idx in sampled_indices]
            efficiency_series = [efficiency_series[idx] for idx in sampled_indices]
            battery_temp_series = [battery_temp_series[idx] for idx in sampled_indices]
            outside_temp_series = [outside_temp_series[idx] for idx in sampled_indices]
            elevation_series = [elevation_series[idx] for idx in sampled_indices]

        drive_chart_data = {
            "labels": labels,
            "speed": speed_series,
            "speed_axis_max": speed_axis_max,
            "soc": soc_series,
            "energy": energy_series,
            "energy_used": energy_used_series,
            "efficiency": efficiency_series,
            "battery_temp": battery_temp_series,
            "elevation": elevation_series,
                    "outside_temp": outside_temp_series,
        }

        summary_duration_sec = drive.get("duration_sec")
        if summary_duration_sec is None and drive.get("started_at") and drive.get("ended_at"):
            summary_duration_sec = int((drive["ended_at"] - drive["started_at"]).total_seconds())

        summary_miles_driven = None
        if drive.get("distance_km") is not None:
            try:
                summary_miles_driven = float(drive["distance_km"]) * 0.621371
            except (TypeError, ValueError):
                summary_miles_driven = None

        summary_avg_mi_per_kwh = None
        if summary_miles_driven is not None and drive.get("energy_used_kwh") is not None:
            try:
                energy_val = float(drive["energy_used_kwh"])
                if energy_val > 0:
                    summary_avg_mi_per_kwh = summary_miles_driven / energy_val
            except (TypeError, ValueError):
                summary_avg_mi_per_kwh = None

        summary_kwh_remaining = drive.get("end_energy_kwh")
        if summary_kwh_remaining is None and points:
            # Fallback for in-progress drives where end_energy_kwh may not be finalized yet.
            for p in reversed(points):
                if p.get("energy_remaining_kwh") is not None:
                    summary_kwh_remaining = p.get("energy_remaining_kwh")
                    break

        wh_per_mile = None
        if summary_miles_driven is not None and drive.get("energy_used_kwh") is not None:
            try:
                energy_val = float(drive["energy_used_kwh"])
                if summary_miles_driven > 0:
                    wh_per_mile = (energy_val * 1000.0) / summary_miles_driven
            except (TypeError, ValueError):
                wh_per_mile = None

        summary_avg_speed = None
        if drive.get("distance_km") is not None and summary_duration_sec is not None:
            try:
                duration_hours = float(summary_duration_sec) / 3600.0
                if duration_hours > 0:
                    avg_speed_kmh = float(drive["distance_km"]) / duration_hours
                    summary_avg_speed = units.convert_for_display(avg_speed_kmh, "speed_kmh", system)
            except (TypeError, ValueError, ZeroDivisionError):
                summary_avg_speed = None

        battery_temps = [float(p["battery_temp_c"]) for p in points if p.get("battery_temp_c") is not None]
        outside_temps = [float(p["outside_temp_c"]) for p in points if p.get("outside_temp_c") is not None]
        altitudes = [float(p["altitude_m"]) for p in points if p.get("altitude_m") is not None]

        battery_temp_avg_c = (sum(battery_temps) / len(battery_temps)) if battery_temps else None
        battery_temp_min_c = min(battery_temps) if battery_temps else None
        battery_temp_max_c = max(battery_temps) if battery_temps else None
        outside_temp_avg_c = (sum(outside_temps) / len(outside_temps)) if outside_temps else None

        elevation_delta_m = None
        elevation_gain_m = None
        if altitudes:
            elevation_delta_m = altitudes[-1] - altitudes[0]
            elevation_gain_m = 0.0
            for idx in range(1, len(altitudes)):
                climb = altitudes[idx] - altitudes[idx - 1]
                if climb > 0:
                    elevation_gain_m += climb

        regen_ratio_pct = None
        if drive.get("regen_energy_kwh") is not None and drive.get("energy_used_kwh") is not None:
            try:
                used_val = float(drive["energy_used_kwh"])
                regen_val = float(drive["regen_energy_kwh"])
                if used_val > 0:
                    regen_ratio_pct = (regen_val / used_val) * 100.0
            except (TypeError, ValueError):
                regen_ratio_pct = None

        drive_summary = {
            "miles_driven": summary_miles_driven,
            "duration_sec": summary_duration_sec,
            "energy_used_kwh": drive.get("energy_used_kwh"),
            "avg_mi_per_kwh": summary_avg_mi_per_kwh,
            "avg_speed": summary_avg_speed,
            "max_speed": speed_axis_max,
            "wh_per_mile": wh_per_mile,
            "kwh_remaining": summary_kwh_remaining,
            "battery_temp_avg_c": battery_temp_avg_c,
            "battery_temp_min_c": battery_temp_min_c,
            "battery_temp_max_c": battery_temp_max_c,
            "outside_temp_avg_c": outside_temp_avg_c,
            "elevation_delta_m": elevation_delta_m,
            "elevation_gain_m": elevation_gain_m,
            "regen_ratio_pct": regen_ratio_pct,
        }

        map_points = [
            {
                "lat": p.get("latitude"),
                "lon": p.get("longitude"),
                "speed": round(
                    units.convert_for_display(float(p["speed_kmh"]) * speed_scale_to_kmh, "speed_kmh", system),
                    1,
                ) if p.get("speed_kmh") is not None else None,
                "time": _format_local_datetime(p.get("recorded_at"), "%H:%M:%S"),
            }
            for p in points
            if p.get("latitude") is not None and p.get("longitude") is not None
        ]

        return render_template(
            "drive_detail.html",
            drive=drive,
            points=points,
            drive_chart_data=drive_chart_data,
            drive_summary=drive_summary,
            drive_chart_point_count=len(drive_chart_data["labels"]),
            drive_total_point_count=len(points),
            speed_label=units.unit_label("speed", system),
            map_points=map_points,
        )

    @app.route("/oauth", methods=["GET", "POST"])
    def oauth_config():
        """OAuth configuration form. Validates credentials and kicks off initial data poll."""

        if request.method == "POST":
            form = {
                "provider": request.form.get("provider", "ford").strip(),
                "client_id": request.form.get("client_id", "").strip(),
                "client_secret": request.form.get("client_secret", "").strip(),
                "scope": request.form.get("scope", "").strip(),
                "redirect_uri": request.form.get("redirect_uri", "").strip(),
                "refresh_token": request.form.get("refresh_token", "").strip(),
                "token_endpoint": request.form.get("token_endpoint", "").strip(),
                "authorize_endpoint": request.form.get("authorize_endpoint", "").strip(),
                "authorization_code": request.form.get("authorization_code", "").strip(),
            }

            vin = _active_vin()

            # Save authorize endpoint preference so it persists in the UI.
            if form["authorize_endpoint"]:
                _set_setting(
                    "oauth_authorize_endpoint",
                    form["authorize_endpoint"],
                    "OAuth authorization endpoint URL",
                )

            # Shared required fields
            missing = [k for k in ("client_id", "client_secret", "token_endpoint") if not form[k]]
            if missing:
                flash(f"Missing required fields: {', '.join(missing)}", "error")
                return render_template("oauth_config.html", vin=vin, form=form)

            # Two supported paths:
            # 1) Existing refresh-token validation flow
            # 2) New authorization-code exchange flow
            if form["authorization_code"]:
                log.info("OAuth form submitted – exchanging authorization code...")
                token_data, err = oauth.exchange_authorization_code(
                    form, form["authorization_code"]
                )
            else:
                if not form["refresh_token"]:
                    flash("Provide either a refresh token or an authorization code.", "error")
                    return render_template("oauth_config.html", vin=vin, form=form)
                log.info("OAuth form submitted – validating credentials via refresh token...")
                token_data, err = oauth.validate_credentials(form)

            if err:
                log.warning("OAuth validation FAILED: %s", err)
                flash(err, "error")
                return render_template("oauth_config.html", vin=vin, form=form)
            log.info("OAuth validation SUCCEEDED – token received")

            # Ensure refresh token is available for ongoing background polling.
            if not token_data.get("refresh_token") and not form["refresh_token"]:
                flash(
                    "OAuth succeeded but no refresh_token was returned. "
                    "Request offline access and include required scopes.",
                    "error",
                )
                return render_template("oauth_config.html", vin=vin, form=form)

            # Save credentials (VIN may be None on first setup — will be
            # updated after garage discovery in initial_setup_poll)
            provider = form["provider"]
            oauth.save_credentials(provider, vin, form, token_data)
            log.info("OAuth credentials saved to database (vin=%s)", vin)

            # Initial setup: discover VIN from garage, then fetch telemetry
            try:
                discovered_vin = poller.initial_setup_poll(provider, vin)
                log.info("Initial setup discovered VIN=%s", discovered_vin)

                # Initialize collector_status for the discovered VIN
                db.execute(
                    """
                    INSERT INTO collector_status (vin, consecutive_failures)
                    VALUES (%s, 0)
                    ON CONFLICT (vin) DO NOTHING
                    """,
                    (discovered_vin,),
                )

                flash("Configuration saved. Garage and telemetry data loaded.", "success")
            except Exception as exc:
                log.error("Initial setup poll failed: %s", exc)
                flash(f"Credentials saved but initial poll failed: {exc}", "warning")

            return redirect(url_for("dashboard"))

        # GET – pre-populate from DB if available
        vin = _active_vin()
        existing = db.fetch_one(
            "SELECT * FROM oauth_credentials WHERE provider = 'ford' ORDER BY id DESC LIMIT 1"
        )
        # Decrypt client_secret for display in the form
        secret_raw = (existing or {}).get("client_secret", "")
        if secret_raw:
            secret_raw = crypto.decrypt(secret_raw)
        form = {
            "provider": (existing or {}).get("provider", "ford"),
            "client_id": (existing or {}).get("client_id", ""),
            "client_secret": secret_raw,
            "scope": (existing or {}).get("scope", ""),
            "redirect_uri": (existing or {}).get("redirect_uri", ""),
            "refresh_token": (existing or {}).get("refresh_token", ""),
            "token_endpoint": (existing or {}).get("token_endpoint", ""),
            "authorize_endpoint": _get_setting("oauth_authorize_endpoint"),
            "authorization_code": "",
        }
        return render_template("oauth_config.html", vin=vin, form=form)

    @app.route("/poller", methods=["GET", "POST"])
    def poller_control():
        """Start/stop the background telemetry poller and view its status."""
        if request.method == "POST":
            action = request.form.get("action")
            if action == "start":
                if poller.start():
                    flash("Poller started.", "success")
                else:
                    flash("Poller is already running.", "warning")
            elif action == "stop":
                if poller.stop():
                    flash("Poller stop requested.", "success")
                else:
                    flash("Poller is not running.", "warning")
            return redirect(url_for("poller_control"))

        status = db.fetch_one("SELECT * FROM collector_status WHERE vin = %s", (_active_vin(),))
        polling_cfg = db.fetch_one(
            "SELECT * FROM polling_config WHERE vin = %s AND enabled = TRUE ORDER BY id DESC LIMIT 1",
            (_active_vin(),),
        )
        return render_template(
            "poller.html", vin=_active_vin(),
            status=status, polling_cfg=polling_cfg,
            running=poller.is_running(),
            conservative=poller.conservative_mode(),
        )

    @app.route("/reset", methods=["GET", "POST"])
    def reset():
        """Factory reset: delete all data for the active VIN and return to setup mode."""
        vin = _active_vin()
        if request.method == "POST":
            confirm = request.form.get("confirm")
            if confirm != "RESET":
                flash("Type RESET to confirm.", "error")
                return render_template("reset.html", vin=vin)

            # Stop poller if running
            if poller.is_running():
                poller.stop()
                log.info("Poller stopped for reset")

            if vin:
                # Delete all data for this VIN (cascade handles FK references)
                tables_to_clear = [
                    "telemetry", "vehicle_state", "battery_state", "charging_state",
                    "charging_history",
                    "location_state", "tire_state", "door_state", "window_state",
                    "brake_state", "security_state", "environment_state",
                    "collector_status", "polling_config", "oauth_credentials",
                    "vehicle_configuration", "departure_schedule",
                ]
                # drive_points cascade-deletes when drives rows are removed
                db.execute("DELETE FROM drives WHERE vin = %s", (vin,))
                for t in tables_to_clear:
                    if _table_exists(t):
                        db.execute(f"DELETE FROM {t} WHERE vin = %s", (vin,))

                db.execute("DELETE FROM garage WHERE vin = %s", (vin,))
                log.info("All data cleared for VIN=%s", vin)

            # Also clear any orphan credentials (NULL VIN)
            db.execute("DELETE FROM oauth_credentials WHERE vin IS NULL")

            flash("All data and OAuth configuration cleared. Please re-configure.", "success")
            return redirect(url_for("oauth_config"))

        return render_template("reset.html", vin=vin)

    # ── Settings ───────────────────────────────────────────────────────

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        """Application settings: display units, polling intervals, log level."""
        if request.method == "POST":
            action = (request.form.get("action") or "save_settings").strip()

            if action in ("save_retrain_options", "run_retrain_now"):
                retrain_schedule_enabled = (
                    "on" if request.form.get("ml_retrain_schedule_enabled") == "on" else "off"
                )
                retrain_schedule_hours_raw = (request.form.get("ml_retrain_schedule_hours", "24") or "24").strip()
                try:
                    retrain_schedule_hours = int(retrain_schedule_hours_raw)
                except (ValueError, TypeError):
                    retrain_schedule_hours = 24
                retrain_schedule_hours = max(1, min(168, retrain_schedule_hours))

                retrain_after_x_enabled = (
                    "on" if request.form.get("ml_retrain_after_x_drives_enabled") == "on" else "off"
                )
                retrain_after_x_raw = (request.form.get("ml_retrain_after_x_drives", "10") or "10").strip()
                try:
                    retrain_after_x = int(retrain_after_x_raw)
                except (ValueError, TypeError):
                    retrain_after_x = 10
                retrain_after_x = max(1, min(500, retrain_after_x))

                if action == "save_retrain_options":
                    _set_setting(
                        "ml_retrain_schedule_enabled",
                        retrain_schedule_enabled,
                        "Enable periodic ML retraining scheduler",
                    )
                    _set_setting(
                        "ml_retrain_schedule_hours",
                        str(retrain_schedule_hours),
                        "Periodic ML retraining interval in hours",
                    )
                    _set_setting(
                        "ml_retrain_after_x_drives_enabled",
                        retrain_after_x_enabled,
                        "Enable ML retraining after X new drives",
                    )
                    _set_setting(
                        "ml_retrain_after_x_drives",
                        str(retrain_after_x),
                        "Retrain ML model after this many new completed drives",
                    )

                    baseline_raw = (_get_setting("ml_retrain_last_trained_drive_count") or "").strip()
                    if not baseline_raw:
                        _set_setting(
                            "ml_retrain_last_trained_drive_count",
                            str(_ml_last_trained_drive_count()),
                            "Completed drive baseline count from last successful ML retraining",
                        )

                    flash(
                        "ML retraining settings saved. "
                        f"Schedule: {'enabled' if retrain_schedule_enabled == 'on' else 'disabled'} "
                        f"({retrain_schedule_hours}h), "
                        f"After-X: {'enabled' if retrain_after_x_enabled == 'on' else 'disabled'} "
                        f"({retrain_after_x} drives).",
                        "success",
                    )
                    return redirect(url_for("settings"))

                started, reason = _start_ml_retrain_job(trigger="manual_settings")
                if not started:
                    flash("Model retraining is already running in the background.", "warning")
                    log.info("Manual ML retraining skipped (%s)", reason)
                else:
                    flash(
                        "Model retraining started in background. "
                        "You can leave this page; status updates below.",
                        "success",
                    )
                return redirect(url_for("settings"))

            if action == "save_backup_schedule":
                backup_enabled = "on" if request.form.get("backup_schedule_enabled") == "on" else "off"
                backup_hours_raw = (request.form.get("backup_schedule_hours", "24") or "24").strip()
                try:
                    backup_hours = int(backup_hours_raw)
                except (ValueError, TypeError):
                    backup_hours = 24
                backup_hours = max(1, min(168, backup_hours))

                _set_setting("backup_schedule_enabled", backup_enabled, "Enable periodic backup scheduler")
                _set_setting("backup_schedule_hours", str(backup_hours), "Periodic backup interval in hours")

                flash(
                    f"Backup schedule saved ({'enabled' if backup_enabled == 'on' else 'disabled'}, every {backup_hours}h).",
                    "success",
                )
                return redirect(url_for("settings"))

            if action in ("save_charger_api_key", "save_charger_options", "save_charger_schedule", "run_charger_import"):
                nlr_api_key = (request.form.get("nlr_api_key", "") or "").strip()
                if nlr_api_key:
                    nlr_chargers.set_nlr_api_key(nlr_api_key)
                    log.info("NLR API key updated")

                charger_scope = (request.form.get("charger_scope", "all_us") or "all_us").strip()
                if charger_scope not in ("all_us", "single_state"):
                    charger_scope = "all_us"

                state_filter = (request.form.get("charger_state_filter", "") or "").strip().upper()
                if charger_scope != "single_state":
                    state_filter = ""
                if charger_scope == "single_state" and state_filter and state_filter not in nlr_chargers.US_STATES:
                    flash(f"Invalid state code '{state_filter}'. Falling back to all US.", "warning")
                    charger_scope = "all_us"
                    state_filter = ""

                fetch_strategy = (request.form.get("charger_fetch_strategy", "all_then_200") or "all_then_200").strip()
                if fetch_strategy not in ("all_then_200", "paged_200"):
                    fetch_strategy = "all_then_200"

                page_size_raw = (request.form.get("charger_page_size", "200") or "200").strip()
                try:
                    page_size = int(page_size_raw)
                except (ValueError, TypeError):
                    page_size = 200
                page_size = max(50, min(1000, page_size))

                if action == "save_charger_options":
                    _set_setting("charger_scope", charger_scope, "Charger import scope: all_us or single_state")
                    _set_setting("charger_state_filter", state_filter, "State code filter for charger import")
                    _set_setting(
                        "charger_fetch_strategy",
                        fetch_strategy,
                        "Charger import strategy: all_then_200 or paged_200",
                    )
                    _set_setting("charger_page_size", str(page_size), "Charger import page size")

                auto_update = "on" if request.form.get("charger_auto_update") == "on" else "off"
                auto_hours_raw = (request.form.get("charger_auto_update_hours", "24") or "24").strip()
                try:
                    auto_hours = int(auto_hours_raw)
                except (ValueError, TypeError):
                    auto_hours = 24
                auto_hours = max(1, min(168, auto_hours))

                if action == "save_charger_schedule":
                    _set_setting("charger_auto_update", auto_update, "Enable periodic charger sync scheduler")
                    _set_setting(
                        "charger_auto_update_hours",
                        str(auto_hours),
                        "Periodic charger sync interval in hours",
                    )
                    flash(
                        f"Charger auto-update schedule saved ({'enabled' if auto_update == 'on' else 'disabled'}, every {auto_hours}h).",
                        "success",
                    )
                    return redirect(url_for("settings"))

                if action == "save_charger_api_key":
                    flash("NIL/NLR API key saved.", "success")
                    return redirect(url_for("settings"))

                if action == "save_charger_options":
                    flash("Charger import options saved.", "success")
                    return redirect(url_for("settings"))

                state_for_import = state_filter if charger_scope == "single_state" and state_filter else None
                started, reason = _start_charger_import_job(
                    state_for_import,
                    fetch_strategy,
                    page_size,
                    trigger="manual_settings",
                )

                if not started:
                    flash("A charger import is already running. Check Last Sync Status/logs for progress.", "warning")
                    log.info("Manual charger import skipped (%s)", reason)
                else:
                    log.info(
                        "Manual charger import submitted as background job (scope=%s, state=%s, strategy=%s, page_size=%s)",
                        charger_scope,
                        state_for_import or "all",
                        fetch_strategy,
                        page_size,
                    )
                    flash(
                        "Charger import started in background. You can leave this page; progress is logged and status updates below.",
                        "success",
                    )
                return redirect(url_for("settings"))

            _set_setting("units", request.form.get("units", "imperial"), "Display unit system")

            requested_tz = (request.form.get("timezone", "") or "").strip() or _SETTINGS_DEFAULTS["timezone"]
            try:
                ZoneInfo(requested_tz)
                _set_setting("timezone", requested_tz, "Display timezone (IANA, e.g. America/Chicago)")
            except ZoneInfoNotFoundError:
                fallback_tz = _SETTINGS_DEFAULTS["timezone"]
                _set_setting("timezone", fallback_tz, "Display timezone (IANA, e.g. America/Chicago)")
                flash(
                    f"Invalid timezone '{requested_tz}'. Using {fallback_tz} instead.",
                    "warning",
                )

            # Runtime log level switching
            new_level = request.form.get("log_level", "INFO").upper()
            applied = set_log_level(new_level)
            _set_setting("log_level", applied, "Console / app-file log level")
            log.info("Settings: log level set to %s", applied)

            # Conservative polling toggle
            cons = "on" if request.form.get("conservative_polling") == "on" else "off"
            _set_setting("conservative_polling", cons, "Conservative idle polling (write once per hour when idle)")

            autostart = "on" if request.form.get("autostart_poller") == "on" else "off"
            _set_setting("autostart_poller", autostart, "Automatically start poller when app starts")


            # Developing mode toggle
            developing = "on" if request.form.get("developing") == "on" else "off"
            _set_setting("developing", developing, "Disable startup delay for development")

            # Clamp all polling intervals to safe limits
            iv_off      = _clamp_interval("poll_interval_off",      request.form.get("poll_interval_off", "120"))
            iv_on       = _clamp_interval("poll_interval_on",       request.form.get("poll_interval_on", "60"))
            iv_moving   = _clamp_interval("poll_interval_moving",   request.form.get("poll_interval_moving", "15"))
            iv_charging = _clamp_interval("poll_interval_charging", request.form.get("poll_interval_charging", "60"))

            _set_setting("poll_interval_off",      str(iv_off),      "Ignition-off poll interval (sec)")
            _set_setting("poll_interval_on",       str(iv_on),       "Ignition-on poll interval (sec)")
            _set_setting("poll_interval_moving",   str(iv_moving),   "Moving poll interval (sec)")
            _set_setting("poll_interval_charging", str(iv_charging), "Charging poll interval (sec)")

            # Sync poller intervals to polling_config table for the active VIN
            active_vin = _active_vin()
            db.execute(
                """
                INSERT INTO polling_config (vin, ignition_off_interval_sec, ignition_on_interval_sec,
                    moving_interval_sec, charging_interval_sec, enabled)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT ON CONSTRAINT polling_config_pkey DO NOTHING
                """,
                (active_vin, iv_off, iv_on, iv_moving, iv_charging),
            )
            # Also update the latest row for this VIN if one exists
            existing = db.fetch_one(
                "SELECT id FROM polling_config WHERE vin = %s ORDER BY id DESC LIMIT 1",
                (active_vin,),
            )
            if existing:
                db.execute(
                    """
                    UPDATE polling_config SET
                        ignition_off_interval_sec = %s,
                        ignition_on_interval_sec = %s,
                        moving_interval_sec = %s,
                        charging_interval_sec = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (iv_off, iv_on, iv_moving, iv_charging, existing["id"]),
                )

            flash("Settings saved.", "success")
            return redirect(url_for("settings"))

        current = {
            "units": _get_setting("units"),
            "timezone": _get_setting("timezone") or _SETTINGS_DEFAULTS["timezone"],
            "log_level": get_log_level(),
            "poll_interval_off": _get_setting("poll_interval_off"),
            "poll_interval_on": _get_setting("poll_interval_on"),
            "poll_interval_moving": _get_setting("poll_interval_moving"),
            "poll_interval_charging": _get_setting("poll_interval_charging"),
            "conservative_polling": _get_setting("conservative_polling"),
            "autostart_poller": _get_setting("autostart_poller"),
            "developing": _get_setting("developing"),
            "nlr_api_key": _get_setting("nlr_api_key") or "",
            "charger_scope": _get_setting("charger_scope") or _SETTINGS_DEFAULTS["charger_scope"],
            "charger_state_filter": _get_setting("charger_state_filter") or _SETTINGS_DEFAULTS["charger_state_filter"],
            "charger_fetch_strategy": _get_setting("charger_fetch_strategy") or _SETTINGS_DEFAULTS["charger_fetch_strategy"],
            "charger_page_size": _get_setting("charger_page_size") or _SETTINGS_DEFAULTS["charger_page_size"],
            "charger_auto_update": _get_setting("charger_auto_update") or _SETTINGS_DEFAULTS["charger_auto_update"],
            "charger_auto_update_hours": _get_setting("charger_auto_update_hours") or _SETTINGS_DEFAULTS["charger_auto_update_hours"],
            "ml_retrain_schedule_enabled": _get_setting("ml_retrain_schedule_enabled") or _SETTINGS_DEFAULTS["ml_retrain_schedule_enabled"],
            "ml_retrain_schedule_hours": _get_setting("ml_retrain_schedule_hours") or _SETTINGS_DEFAULTS["ml_retrain_schedule_hours"],
            "ml_retrain_after_x_drives_enabled": _get_setting("ml_retrain_after_x_drives_enabled") or _SETTINGS_DEFAULTS["ml_retrain_after_x_drives_enabled"],
            "ml_retrain_after_x_drives": _get_setting("ml_retrain_after_x_drives") or _SETTINGS_DEFAULTS["ml_retrain_after_x_drives"],
            "backup_schedule_enabled": _get_setting("backup_schedule_enabled") or _SETTINGS_DEFAULTS["backup_schedule_enabled"],
            "backup_schedule_hours": _get_setting("backup_schedule_hours") or _SETTINGS_DEFAULTS["backup_schedule_hours"],
            "backup_last_completed_at": _get_setting("backup_last_completed_at") or _SETTINGS_DEFAULTS["backup_last_completed_at"],
            "backup_last_error": _get_setting("backup_last_error") or _SETTINGS_DEFAULTS["backup_last_error"],
        }
        ssl_cfg = config.ssl_config()
        ssl_status = {
            "active": app.config.get("SSL_ACTIVE", False),
            "recovery": app.config.get("SSL_RECOVERY", False),
        }
        # Validate current user certs if they exist
        if ssl_cfg.get("cert") and ssl_cfg.get("key"):
            if os.path.isfile(ssl_cfg["cert"]) and os.path.isfile(ssl_cfg["key"]):
                valid, msg = crypto.validate_ssl_files(ssl_cfg["cert"], ssl_cfg["key"])
                ssl_status["cert_valid"] = valid
                ssl_status["cert_message"] = msg
            else:
                ssl_status["cert_valid"] = False
                ssl_status["cert_message"] = "Certificate or key file not found on disk"
        
        charger_job_running = _charger_import_is_running()
        if not charger_job_running:
            stale_count = nlr_chargers.mark_stale_sync_runs(stale_after_minutes=5)
            if stale_count:
                log.warning("Detected and closed %d stale charger import run(s)", stale_count)

        # Get charger sync status
        charger_status = nlr_chargers.get_sync_status()
        charger_failure_class = _charger_failure_class(charger_status)
        charger_heartbeat_stale = False
        if charger_status and charger_status.get("status") == "in_progress":
            try:
                charger_heartbeat_stale = int(charger_status.get("heartbeat_age_seconds") or 0) > 300
            except (TypeError, ValueError):
                charger_heartbeat_stale = False

        ml_retrain_job_running = _ml_retrain_is_running()
        ml_baseline_drives = _ml_last_trained_drive_count()
        ml_completed_drives = _count_completed_training_drives()
        ml_new_drives = max(0, ml_completed_drives - ml_baseline_drives)
        ml_schema = _read_model_schema()
        ml_retrain_status = {
            "status": _get_setting("ml_retrain_status") or "idle",
            "last_started_at": _get_setting("ml_retrain_last_started_at") or "",
            "last_completed_at": _get_setting("ml_retrain_last_completed_at") or "",
            "last_trigger": _get_setting("ml_retrain_last_trigger") or "",
            "last_error": _get_setting("ml_retrain_last_error") or "",
            "last_duration_sec": _get_setting("ml_retrain_last_duration_sec") or "",
            "last_exit_code": _get_setting("ml_retrain_last_exit_code") or "",
            "baseline_drives": ml_baseline_drives,
            "completed_drives": ml_completed_drives,
            "new_drives_since_last_train": ml_new_drives,
            "schema_training_date": str(ml_schema.get("training_date") or ""),
            "schema_num_training_drives": ml_schema.get("num_training_drives"),
            "scheduler_running": _ml_retrain_scheduler_is_running(),
        }
        if ml_retrain_job_running:
            ml_retrain_status["status"] = "in_progress"

        seq_marker = db.fetch_one(
            "SELECT value FROM app_config WHERE key = %s",
            (_SEQUENCE_ALIGNMENT_MARKER_KEY,),
        )
        seq_force = db.fetch_one(
            "SELECT value FROM app_config WHERE key = %s",
            (_SEQUENCE_ALIGNMENT_FORCE_KEY,),
        )
        sequence_alignment = {
            "last_run": seq_marker["value"] if seq_marker else None,
            "force_next_startup": (
                seq_force is not None
                and str(seq_force.get("value", "")).strip().lower() in ("on", "true", "1", "yes")
            ),
        }
        
        return render_template("settings.html", settings=current, ssl=ssl_cfg,
                               ssl_status=ssl_status, charger_status=charger_status,
                               charger_failure_class=charger_failure_class,
                               sequence_alignment=sequence_alignment,
                               charger_job_running=charger_job_running,
                               charger_heartbeat_stale=charger_heartbeat_stale,
                               ml_retrain_status=ml_retrain_status,
                               ml_retrain_job_running=ml_retrain_job_running)

    @app.route("/settings/sequence-alignment", methods=["POST"])
    def sequence_alignment_settings():
        """Manage database ID sequence alignment controls from the Settings page."""
        action = (request.form.get("action") or "").strip()

        if action == "run_now":
            aligned_tables = _run_sequence_alignment(force=True)
            if aligned_tables:
                flash(
                    "Sequence alignment completed for: " + ", ".join(aligned_tables),
                    "success",
                )
            else:
                flash(
                    "Sequence alignment completed. No serial ID tables required adjustment.",
                    "success",
                )
        elif action == "save_restore_option":
            force_next = "on" if request.form.get("force_next_startup") == "on" else "off"
            _set_setting(
                _SEQUENCE_ALIGNMENT_FORCE_KEY,
                force_next,
                "If on, run sequence alignment once at next app startup",
            )
            if force_next == "on":
                flash("Sequence alignment is queued for next startup.", "success")
            else:
                flash("Sequence alignment is not queued for next startup.", "success")
        else:
            flash("Unknown sequence-alignment action.", "warning")

        return redirect(url_for("settings"))

    @app.route("/settings/upload-image", methods=["POST"])
    def upload_vehicle_image():
        """Handle vehicle image upload from the settings page."""
        file = request.files.get("vehicle_image")
        if not file or file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("settings"))

        ALLOWED = {"png", "jpg", "jpeg", "gif", "webp"}
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED:
            flash(f"Invalid file type. Allowed: {', '.join(ALLOWED)}", "error")
            return redirect(url_for("settings"))

        filename = secure_filename(f"vehicle.{ext}")
        save_path = os.path.join(app.static_folder, filename)
        file.save(save_path)
        _set_setting("vehicle_image", filename, "Custom vehicle image filename")
        flash("Vehicle image updated.", "success")
        return redirect(url_for("settings"))

    @app.route("/settings/ssl", methods=["POST"])
    def ssl_settings():
        """Upload SSL cert/key files and toggle SSL on/off.

        Files are saved to a `certs/` directory next to the project.
        Paths and enabled flag are persisted to config.json.
        A restart is required for SSL changes to take effect.
        """
        certs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
        os.makedirs(certs_dir, exist_ok=True)

        ssl_cfg = config.ssl_config()

        # Handle cert file upload
        cert_file = request.files.get("ssl_cert")
        if cert_file and cert_file.filename:
            cert_ext = cert_file.filename.rsplit(".", 1)[-1].lower() if "." in cert_file.filename else ""
            if cert_ext not in ("pem", "crt", "cer"):
                flash("Certificate must be .pem, .crt, or .cer", "error")
                return redirect(url_for("settings"))
            cert_path = os.path.join(certs_dir, "server.crt")
            cert_file.save(cert_path)
            ssl_cfg["cert"] = cert_path
            log.info("SSL certificate uploaded: %s", cert_path)

        # Handle key file upload
        key_file = request.files.get("ssl_key")
        if key_file and key_file.filename:
            key_ext = key_file.filename.rsplit(".", 1)[-1].lower() if "." in key_file.filename else ""
            if key_ext not in ("pem", "key"):
                flash("Key must be .pem or .key", "error")
                return redirect(url_for("settings"))
            key_path = os.path.join(certs_dir, "server.key")
            key_file.save(key_path)
            # Restrict permissions on the private key
            os.chmod(key_path, 0o600)
            ssl_cfg["key"] = key_path
            log.info("SSL private key uploaded: %s", key_path)

        # Toggle enabled
        ssl_cfg["enabled"] = request.form.get("ssl_enabled") == "on"

        config.save_ssl(ssl_cfg)
        flash("SSL settings saved. Restart the application for changes to take effect.", "success")
        return redirect(url_for("settings"))

    # ── Manage Vehicles ──────────────────────────────────────────────

    # Backup scheduler thread and functions (moved out of _SETTINGS_DEFAULTS)

    @app.route("/manage/delete-vin", methods=["POST"])
    def manage_delete_vin():
        """Delete a VIN and all its data (ON DELETE CASCADE handles child tables)."""
        target_vin = request.form.get("vin", "").strip()
        if not target_vin:
            flash("No VIN specified.", "error")
            return redirect(url_for("manage"))

        # Safety: stop poller if deleting the active VIN
        active_vin = _active_vin()
        if target_vin == active_vin and poller.is_running():
            poller.stop()
            log.info("Poller stopped – deleting active VIN %s", target_vin)

        # CASCADE deletes all child rows
        db.execute("DELETE FROM garage WHERE vin = %s", (target_vin,))
        log.info("Deleted VIN=%s and all associated data (cascade)", target_vin)
        flash(f"Deleted VIN {target_vin} and all associated records.", "success")

        return redirect(url_for("manage"))

    @app.route("/manage/repoll", methods=["POST"])
    def manage_repoll():
        """Re-run initial setup poll for the active VIN."""
        active_vin = _active_vin()
        if not active_vin:
            flash("No active VIN in garage. Configure OAuth first.", "error")
            return redirect(url_for("manage"))

        creds = db.fetch_one(
            "SELECT provider FROM oauth_credentials WHERE vin = %s AND enabled = TRUE",
            (active_vin,),
        )
        if creds is None:
            flash("No enabled OAuth credentials for the active VIN.", "error")
            return redirect(url_for("manage"))

        try:
            poller.initial_setup_poll(creds["provider"], active_vin)
            flash(f"Re-polled data for VIN {active_vin} successfully.", "success")
        except Exception as exc:
            log.error("Re-poll failed: %s", exc)
            flash(f"Re-poll failed: {exc}", "error")

        return redirect(url_for("manage"))

    @app.route("/chargers/public")
    def public_chargers():
        """Show summary analytics for imported public charger data."""
        if not _table_exists("ev_stations"):
            flash("Public charger table is missing. Run a charger import first.", "warning")
            return redirect(url_for("settings"))

        if _table_exists("ev_sync_runs"):
            nlr_chargers.mark_stale_sync_runs(stale_after_minutes=5)

        location_query = (request.args.get("location") or "").strip()
        origin_query = (request.args.get("origin") or "").strip()
        radius_miles_raw = (request.args.get("radius_miles") or "").strip()
        network_filter = (request.args.get("network") or "").strip()
        min_kw_raw = (request.args.get("min_kw") or "").strip()
        result_limit = request.args.get("limit", 100, type=int)
        result_limit = max(25, min(500, result_limit))

        min_kw = None
        if min_kw_raw:
            try:
                min_kw = float(min_kw_raw)
            except (TypeError, ValueError):
                min_kw = None

        radius_miles = None
        if radius_miles_raw:
            try:
                radius_miles = float(radius_miles_raw)
            except (TypeError, ValueError):
                radius_miles = None

        def _geocode_location(query: str) -> dict[str, object] | None:
            """Geocode free-form location text into lat/lon for distance filtering."""
            q = (query or "").strip()
            if not q:
                return None
            try:
                resp = requests.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": q,
                        "format": "json",
                        "limit": 1,
                        "countrycodes": "us",
                    },
                    headers={"User-Agent": "Ford-Lightning-EV/1.0"},
                    timeout=8,
                )
                resp.raise_for_status()
                rows = resp.json() or []
                if not rows:
                    return None
                top = rows[0]
                return {
                    "lat": float(top.get("lat")),
                    "lon": float(top.get("lon")),
                    "label": str(top.get("display_name") or q),
                }
            except Exception as exc:
                log.warning("Location geocode failed for '%s': %s", q, exc)
                return None

        origin_geo = _geocode_location(origin_query) if origin_query else None
        if origin_query and not origin_geo:
            flash("Could not resolve origin location for distance filter. Showing non-distance results.", "warning")

        totals = {
            "stations": 0,
            "connectors": 0,
            "states": 0,
            "networks": 0,
        }

        row = db.fetch_one("SELECT COUNT(*) AS cnt FROM ev_stations")
        totals["stations"] = row["cnt"] if row else 0

        row = db.fetch_one("SELECT COUNT(DISTINCT state) AS cnt FROM ev_stations WHERE state IS NOT NULL AND state <> ''")
        totals["states"] = row["cnt"] if row else 0

        row = db.fetch_one("SELECT COUNT(DISTINCT network_name) AS cnt FROM ev_stations WHERE network_name IS NOT NULL AND network_name <> ''")
        totals["networks"] = row["cnt"] if row else 0

        if _table_exists("ev_charger_connectors"):
            row = db.fetch_one("SELECT COUNT(*) AS cnt FROM ev_charger_connectors")
            totals["connectors"] = row["cnt"] if row else 0

        by_state = db.fetch_all(
            """
            SELECT
                COALESCE(state, 'UNKNOWN') AS state,
                COUNT(*) AS station_count
            FROM ev_stations
            GROUP BY COALESCE(state, 'UNKNOWN')
            ORDER BY station_count DESC, state ASC
            """
        )

        state_count_map = {str(row["state"]): int(row["station_count"]) for row in by_state}
        all_states_counts = [
            {"state": st, "station_count": state_count_map.get(st, 0)}
            for st in sorted(nlr_chargers.US_STATES)
        ]

        connector_types = []
        charging_levels = []
        if _table_exists("ev_charger_connectors"):
            connector_types = db.fetch_all(
                """
                SELECT
                    COALESCE(NULLIF(connector_type, ''), 'UNKNOWN') AS connector_type,
                    COUNT(*) AS connector_count,
                    COUNT(DISTINCT station_id) AS station_count
                FROM ev_charger_connectors
                GROUP BY COALESCE(NULLIF(connector_type, ''), 'UNKNOWN')
                ORDER BY connector_count DESC, connector_type ASC
                """
            )
            charging_levels = db.fetch_all(
                """
                SELECT
                    COALESCE(NULLIF(charging_level, ''), 'UNKNOWN') AS charging_level,
                    COUNT(*) AS connector_count
                FROM ev_charger_connectors
                GROUP BY COALESCE(NULLIF(charging_level, ''), 'UNKNOWN')
                ORDER BY connector_count DESC, charging_level ASC
                """
            )

        network_breakdown = db.fetch_all(
            """
            SELECT
                COALESCE(NULLIF(network_name, ''), 'UNKNOWN') AS network_name,
                COUNT(*) AS station_count
            FROM ev_stations
            GROUP BY COALESCE(NULLIF(network_name, ''), 'UNKNOWN')
            ORDER BY station_count DESC, network_name ASC
            LIMIT 30
            """
        )

        network_options = db.fetch_all(
            """
            SELECT DISTINCT COALESCE(NULLIF(network_name, ''), 'UNKNOWN') AS network_name
            FROM ev_stations
            ORDER BY network_name ASC
            """
        )

        has_connectors = _table_exists("ev_charger_connectors")
        connector_join = """
            LEFT JOIN (
                SELECT
                    station_id,
                    MAX(COALESCE(power_kw, 0)) AS max_power_kw,
                    STRING_AGG(DISTINCT COALESCE(NULLIF(connector_type, ''), 'UNKNOWN'), ', ') AS connector_types,
                    STRING_AGG(DISTINCT COALESCE(NULLIF(charging_level, ''), 'UNKNOWN'), ', ') AS charging_levels,
                    SUM(CASE WHEN COALESCE(port_count, 0) > 0 THEN port_count ELSE 1 END) AS connector_count
                FROM ev_charger_connectors
                GROUP BY station_id
            ) conn ON conn.station_id = s.id
        """ if has_connectors else ""

        where_clauses: list[str] = []
        where_params: list[object] = []
        distance_expr = """
            (3958.7613 * 2 * ASIN(SQRT(
                POWER(SIN(RADIANS(s.latitude - %s) / 2), 2)
                + COS(RADIANS(%s)) * COS(RADIANS(s.latitude))
                * POWER(SIN(RADIANS(s.longitude - %s) / 2), 2)
            )))
        """
        distance_select_sql = "NULL::DOUBLE PRECISION AS distance_miles"
        select_params: list[object] = []

        if location_query:
            tokens = [tok for tok in re.split(r"[\s,]+", location_query) if tok]
            for token in tokens:
                where_clauses.append(
                    "concat_ws(' ', "
                    "COALESCE(s.station_name, ''), "
                    "COALESCE(s.street_address, ''), "
                    "COALESCE(s.city, ''), "
                    "COALESCE(s.state, ''), "
                    "COALESCE(s.zip, '')"
                    ") ILIKE %s"
                )
                where_params.append(f"%{token}%")

        if network_filter:
            where_clauses.append("COALESCE(NULLIF(s.network_name, ''), 'UNKNOWN') = %s")
            where_params.append(network_filter)

        if min_kw is not None:
            if has_connectors:
                where_clauses.append("COALESCE(conn.max_power_kw, 0) >= %s")
                where_params.append(min_kw)
            else:
                where_clauses.append("1 = 0")

        if origin_geo:
            lat = float(origin_geo["lat"])
            lon = float(origin_geo["lon"])
            distance_select_sql = f"{distance_expr} AS distance_miles"
            select_params.extend([lat, lat, lon])
            if radius_miles is not None and radius_miles > 0:
                where_clauses.append(f"{distance_expr} <= %s")
                where_params.extend([lat, lat, lon, radius_miles])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        connector_select = (
            "COALESCE(conn.max_power_kw, 0) AS max_power_kw, "
            "COALESCE(conn.connector_types, '') AS connector_types, "
            "COALESCE(conn.charging_levels, '') AS charging_levels, "
            "COALESCE(conn.connector_count, 0) AS connector_count"
        ) if has_connectors else (
            "0::REAL AS max_power_kw, ''::TEXT AS connector_types, ''::TEXT AS charging_levels, 0::BIGINT AS connector_count"
        )

        filtered_total_row = db.fetch_one(
            f"""
            SELECT COUNT(*) AS cnt
            FROM ev_stations s
            {connector_join}
            {where_sql}
            """,
            tuple(where_params),
        )
        filtered_total = filtered_total_row["cnt"] if filtered_total_row else 0

        filtered_stations = db.fetch_all(
            f"""
            SELECT
                s.id,
                s.station_name,
                s.street_address,
                s.city,
                s.state,
                s.zip,
                s.country,
                s.latitude,
                s.longitude,
                s.status_code,
                s.fuel_type_code,
                s.access_code,
                s.access_detail,
                s.owner_type_code,
                s.facility_type,
                s.nlr_station_id,
                s.updated_at,
                COALESCE(NULLIF(s.network_name, ''), 'UNKNOWN') AS network_name,
                {distance_select_sql},
                {connector_select}
            FROM ev_stations s
            {connector_join}
            {where_sql}
            ORDER BY
                distance_miles ASC NULLS LAST,
                max_power_kw DESC,
                s.state ASC,
                s.city ASC,
                s.station_name ASC
            LIMIT %s
            """,
            tuple([*select_params, *where_params, result_limit]),
        )

        map_points = [
            {
                "name": row.get("station_name"),
                "city": row.get("city"),
                "state": row.get("state"),
                "zip": row.get("zip"),
                "network": row.get("network_name"),
                "max_kw": float(row.get("max_power_kw") or 0),
                "distance_miles": float(row.get("distance_miles")) if row.get("distance_miles") is not None else None,
                "lat": float(row.get("latitude")) if row.get("latitude") is not None else None,
                "lon": float(row.get("longitude")) if row.get("longitude") is not None else None,
            }
            for row in filtered_stations
            if row.get("latitude") is not None and row.get("longitude") is not None
        ]

        sync_status = nlr_chargers.get_sync_status()
        recent_runs = []
        if _table_exists("ev_sync_runs"):
            recent_runs = db.fetch_all(
                """
                SELECT id, sync_type, state_filter, status, started_at, last_heartbeat_at, completed_at,
                       stations_imported, stations_updated, errors, last_error
                FROM ev_sync_runs
                ORDER BY started_at DESC
                LIMIT 10
                """
            )

        sync_status_failure_class = _charger_failure_class(sync_status)
        recent_runs_view = []
        for run in recent_runs:
            run_view = dict(run)
            run_view["failure_class"] = _charger_failure_class(run_view)
            recent_runs_view.append(run_view)

        return render_template(
            "public_chargers.html",
            totals=totals,
            filters={
                "location": location_query,
                "origin": origin_query,
                "radius_miles": radius_miles_raw,
                "network": network_filter,
                "min_kw": min_kw_raw,
                "limit": result_limit,
            },
            origin_geo=origin_geo,
            map_points=map_points,
            network_options=network_options,
            filtered_stations=filtered_stations,
            filtered_total=filtered_total,
            by_state=by_state,
            all_states_counts=all_states_counts,
            connector_types=connector_types,
            charging_levels=charging_levels,
            network_breakdown=network_breakdown,
            sync_status=sync_status,
            sync_status_failure_class=sync_status_failure_class,
            recent_runs=recent_runs_view,
        )

    # ── Trip Planner (Phase 2: ML routing) ─────────────────────────

    def _current_vehicle_location_coords() -> tuple[float, float] | None:
        """Return latest known vehicle coordinates for active VIN, if available."""
        vin = _active_vin()
        if not vin:
            return None

        location_row = db.fetch_one(
            """
            SELECT latitude, longitude, last_update
            FROM location_state
            WHERE vin = %s
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
            """,
            (vin,),
        )

        drive_row = db.fetch_one(
            """
            SELECT dp.latitude, dp.longitude, dp.recorded_at
            FROM drive_points dp
            JOIN drives d ON d.id = dp.drive_id
            WHERE d.vin = %s
              AND dp.latitude IS NOT NULL
              AND dp.longitude IS NOT NULL
            ORDER BY dp.recorded_at DESC
            LIMIT 1
            """,
            (vin,),
        )

        candidates = []
        if location_row:
            candidates.append(
                (
                    location_row.get("last_update"),
                    float(location_row["latitude"]),
                    float(location_row["longitude"]),
                )
            )
        if drive_row:
            candidates.append(
                (
                    drive_row.get("recorded_at"),
                    float(drive_row["latitude"]),
                    float(drive_row["longitude"]),
                )
            )

        if candidates:
            candidates.sort(key=lambda item: item[0] or datetime.min, reverse=True)
            _, lat, lon = candidates[0]
            return (lat, lon)

        return None

    def _reverse_geocode_label(lat: float, lon: float) -> str:
        """Resolve a user-friendly address label for coordinates."""
        # Try ArcGIS first because Nominatim may be temporarily rate-limited.
        try:
            response = requests.get(
                "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode",
                params={
                    "location": f"{lon},{lat}",
                    "f": "json",
                    "langCode": "EN",
                },
                headers={"User-Agent": "MLLighting-Trip-Planner/1.0"},
                timeout=8,
            )
            response.raise_for_status()
            data = response.json() or {}
            address = data.get("address") or {}
            label = (address.get("Match_addr") or address.get("LongLabel") or "").strip()
            if label:
                return label
        except Exception as exc:
            log.warning("ArcGIS reverse geocode failed for %s,%s: %s", lat, lon, exc)

        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": 18,
                    "addressdetails": 1,
                },
                headers={"User-Agent": "MLLighting-Trip-Planner/1.0"},
                timeout=8,
            )
            response.raise_for_status()
            data = response.json() or {}
            display_name = (data.get("display_name") or "").strip()
            if display_name:
                return display_name
        except Exception as exc:
            log.warning("Reverse geocode failed for %s,%s: %s", lat, lon, exc)
        return f"{lat:.6f},{lon:.6f}"

    def _parse_coord_string(value: str) -> tuple[float, float] | None:
        value = (value or "").strip()
        if not value or "," not in value:
            return None
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 2:
            return None
        try:
            lat = float(parts[0])
            lon = float(parts[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
        except (TypeError, ValueError):
            return None
        return None

    @app.route("/trip-planner", methods=["GET", "POST"])
    def trip_planner():
        """Interactive trip planner for EV routing with charger recommendations."""
        plan = None
        preview = None
        form_data = {
            "source": "",
            "destination": "",
            "start_soc": 85,
            "use_current_source": False,
        }
        unit_system = _get_setting("units") if db.is_available() else "imperial"
        timezone_name = _get_setting("timezone") if db.is_available() else "UTC"
        try:
            display_tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone_name = "UTC"
            display_tz = timezone.utc

        if request.method == "POST":
            form_data["source"] = request.form.get("source", "").strip()
            form_data["destination"] = request.form.get("destination", "").strip()
            form_data["use_current_source"] = request.form.get("use_current_source") == "on"
            try:
                form_data["start_soc"] = int(request.form.get("start_soc", 85))
            except (ValueError, TypeError):
                form_data["start_soc"] = 85

            form_data["start_soc"] = max(0, min(100, form_data["start_soc"]))
            action = (request.form.get("action") or "preview").strip().lower()

            if action == "calculate":
                source_coords_text = request.form.get("source_resolved_coords", "").strip()
                destination_coords_text = request.form.get("destination_resolved_coords", "").strip()
                source_label = request.form.get("source_resolved_label", "").strip() or source_coords_text
                destination_label = request.form.get("destination_resolved_label", "").strip() or destination_coords_text

                source_coords = _parse_coord_string(source_coords_text)
                destination_coords = _parse_coord_string(destination_coords_text)

                if not source_coords or not destination_coords:
                    flash("Preview locations first, then calculate route.", "warning")
                else:
                    preview = {
                        "ready": True,
                        "source": {
                            "label": source_label,
                            "coords_text": source_coords_text,
                        },
                        "destination": {
                            "label": destination_label,
                            "coords_text": destination_coords_text,
                        },
                    }

                    try:
                        plan = tp_service.plan_trip(
                            source=source_coords_text,
                            destination=destination_coords_text,
                            current_soc_percent=form_data["start_soc"],
                        )
                        if plan:
                            plan.source_name = source_label
                            plan.destination_name = destination_label
                            for wx in (plan.route_weather or []):
                                eta_utc = str(wx.get("eta_utc") or "").strip()
                                if not eta_utc:
                                    continue
                                try:
                                    dt_utc = datetime.strptime(eta_utc, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                                    wx["eta_local"] = dt_utc.astimezone(display_tz).strftime("%Y-%m-%d %I:%M %p")
                                except Exception:
                                    wx["eta_local"] = eta_utc
                    except Exception as exc:
                        log.exception(f"Trip planning failed: {exc}")
                        flash(f"Trip planning failed: {exc}", "error")
            else:
                source_coords = None
                destination_coords = None
                source_label = ""
                destination_label = ""

                if not form_data["destination"]:
                    flash("Please enter a destination.", "warning")
                elif not form_data["use_current_source"] and not form_data["source"]:
                    flash("Please enter a source or enable current vehicle location.", "warning")
                else:
                    if form_data["use_current_source"]:
                        current_coords = _current_vehicle_location_coords()
                        if not current_coords:
                            flash(
                                "Current vehicle location is unavailable. Poll the vehicle first or enter a source manually.",
                                "warning",
                            )
                        else:
                            source_coords = current_coords
                            source_label = f"Current Vehicle Location: {_reverse_geocode_label(source_coords[0], source_coords[1])}"
                    else:
                        source_coords = tp_service.geocode_location(form_data["source"])
                        if source_coords:
                            source_label = _reverse_geocode_label(source_coords[0], source_coords[1])

                    destination_coords = tp_service.geocode_location(form_data["destination"])
                    if destination_coords:
                        destination_label = _reverse_geocode_label(destination_coords[0], destination_coords[1])

                    if not source_coords:
                        flash("Could not resolve source location. Edit source and preview again.", "warning")
                    if not destination_coords:
                        flash("Could not resolve destination location. Edit destination and preview again.", "warning")

                    if source_coords and destination_coords:
                        preview = {
                            "ready": True,
                            "source": {
                                "label": source_label,
                                "coords_text": f"{source_coords[0]:.6f},{source_coords[1]:.6f}",
                            },
                            "destination": {
                                "label": destination_label,
                                "coords_text": f"{destination_coords[0]:.6f},{destination_coords[1]:.6f}",
                            },
                        }
                        flash("Locations resolved. Review below, then calculate route.", "success")

        return render_template(
            "trip_planner.html",
            form=form_data,
            plan=plan,
            preview=preview,
            unit_system=unit_system,
            timezone_name=timezone_name,
        )

    @app.route("/api/predict/trip", methods=["POST"])
    def api_predict_trip():
        """API endpoint for trip planning (JSON).
        
        POST /api/predict/trip
        {
            "source": "40.7128,-74.0060",
            "destination": "39.7392,-104.9903",
            "current_soc_percent": 85,
            "use_current_vehicle_location": false
        }
        
        Returns: TripPlan as JSON
        """
        try:
            data = request.get_json() or {}
            
            source = data.get("source", "").strip()
            destination = data.get("destination", "").strip()
            use_current = bool(data.get("use_current_vehicle_location", False))
            current_soc = data.get("current_soc_percent", 85)

            if not destination:
                return jsonify({"error": "destination required"}), 400

            if use_current:
                current_coords = _current_vehicle_location_coords()
                if not current_coords:
                    return jsonify({"error": "current vehicle location unavailable"}), 400
                source = f"{current_coords[0]:.6f},{current_coords[1]:.6f}"
            elif not source:
                return jsonify({"error": "source required unless use_current_vehicle_location=true"}), 400
            
            try:
                current_soc = int(current_soc)
            except (ValueError, TypeError):
                current_soc = 85
            
            current_soc = max(0, min(100, current_soc))
            
            plan = tp_service.plan_trip(
                source=source,
                destination=destination,
                current_soc_percent=current_soc,
            )
            if use_current:
                plan.source_name = "Current Vehicle Location"
            
            # Convert dataclass to dict for JSON serialization
            from dataclasses import asdict
            plan_dict = asdict(plan)
            
            # Serialize charging stops
            plan_dict["charging_stops"] = [
                asdict(stop) for stop in plan.charging_stops
            ]
            
            return jsonify(plan_dict), 200
        
        except Exception as exc:
            log.exception(f"API trip planning failed: {exc}")
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/predict/energy", methods=["POST"])
    def api_predict_energy():
        """API endpoint for energy prediction (JSON).
        
        POST /api/predict/energy
        {
            "distance_km": 100,
            "avg_speed_kmh": 80,
            "avg_ambient_temp_c": 15
        }
        
        Returns: Energy prediction with confidence
        """
        try:
            data = request.get_json() or {}
            
            result = energy_model.predict_energy(
                distance_km=data.get("distance_km", 50),
                avg_speed_kmh=data.get("avg_speed_kmh", 80),
                avg_ambient_temp_c=data.get("avg_ambient_temp_c", 20),
                avg_outside_temp_c=data.get("avg_outside_temp_c", 20),
            )
            
            return jsonify(result), 200
        
        except Exception as exc:
            log.exception(f"API energy prediction failed: {exc}")
            return jsonify({"error": str(exc)}), 500

    # ── Database viewer ────────────────────────────────────────────

    # Whitelist of tables the viewer can display
    _VIEWABLE_TABLES = [
        "garage", "telemetry", "drives", "drive_points", "charging_history",
        "vehicle_state", "battery_state", "charging_state", "location_state",
        "tire_state", "door_state", "window_state",
        "brake_state", "security_state", "environment_state",
        "vehicle_configuration", "departure_schedule",
        "polling_config", "collector_status", "app_config", "oauth_credentials",
        "ev_stations", "ev_charger_connectors", "ev_sync_runs",
    ]

    @app.route("/db")
    def db_browser():
        """Show all tables with row counts."""
        table_info = []
        for t in _VIEWABLE_TABLES:
            if not _table_exists(t):
                continue
            row = db.fetch_one(f"SELECT count(*) AS cnt FROM {t}")
            table_info.append({"name": t, "count": row["cnt"] if row else 0})
        return render_template("db_browser.html", tables=table_info)

    @app.route("/db/<table_name>")
    def db_table(table_name):
        """Show contents of a single table."""
        if table_name not in _VIEWABLE_TABLES:
            flash(f"Table '{table_name}' is not viewable.", "error")
            return redirect(url_for("db_browser"))
        if not _table_exists(table_name):
            flash(f"Table '{table_name}' does not exist in the current database.", "error")
            return redirect(url_for("db_browser"))

        limit = request.args.get("limit", 100, type=int)
        limit = min(max(limit, 1), 1000)

        # Get column info
        cols_rows = db.fetch_all(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (table_name,),
        )
        columns = [r["column_name"] for r in cols_rows]
        col_types = {r["column_name"]: r["data_type"] for r in cols_rows}

        # Fetch rows – order by first column (usually PK)
        rows = db.fetch_all(
            f"SELECT * FROM {table_name} ORDER BY 1 DESC LIMIT %s",
            (limit,),
        )

        # Mask secrets in oauth_credentials display
        if table_name == "oauth_credentials":
            for row in rows:
                for secret_col in ("client_secret", "access_token", "refresh_token"):
                    if secret_col in row and row[secret_col]:
                        val = str(row[secret_col])
                        row[secret_col] = val[:6] + "…" if len(val) > 10 else "***"
                # Truncate other long fields for readability
                for long_col in ("token_endpoint", "redirect_uri", "scope", "client_id"):
                    if long_col in row and row[long_col] and len(str(row[long_col])) > 50:
                        row[long_col] = str(row[long_col])[:50] + "…"

        # Determine primary key column for delete buttons
        _PK_MAP = {
            "garage": "vin",
            "telemetry": "id",
            "polling_config": "id",
            "oauth_credentials": "id",
            "app_config": "key",
        }
        # Most state tables use 'vin' as PK
        pk_col = _PK_MAP.get(table_name, "vin")

        # Tables that render better as vertical cards instead of wide horizontal tables
        _CARD_LAYOUT_TABLES = {"oauth_credentials"}

        return render_template(
            "db_table.html",
            table_name=table_name,
            columns=columns,
            col_types=col_types,
            rows=rows,
            limit=limit,
            row_count=len(rows),
            pk_col=pk_col,
            card_layout=(table_name in _CARD_LAYOUT_TABLES),
        )

    @app.route("/db/<table_name>/delete", methods=["POST"])
    def db_delete_row(table_name):
        """Delete a specific row from a table."""
        if table_name not in _VIEWABLE_TABLES:
            flash(f"Table '{table_name}' is not viewable.", "error")
            return redirect(url_for("db_browser"))
        if not _table_exists(table_name):
            flash(f"Table '{table_name}' does not exist in the current database.", "error")
            return redirect(url_for("db_browser"))

        pk_col = request.form.get("pk_col", "").strip()
        pk_val = request.form.get("pk_val", "").strip()
        if not pk_col or not pk_val:
            flash("Missing primary key info for deletion.", "error")
            return redirect(url_for("db_table", table_name=table_name))

        # Validate pk_col is an actual column in the table to prevent injection
        valid_cols = db.fetch_all(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table_name,),
        )
        valid_col_names = {r["column_name"] for r in valid_cols}
        if pk_col not in valid_col_names:
            flash(f"Invalid column '{pk_col}'.", "error")
            return redirect(url_for("db_table", table_name=table_name))

        db.execute(f"DELETE FROM {table_name} WHERE {pk_col} = %s", (pk_val,))
        log.info("Deleted row from %s WHERE %s = %s", table_name, pk_col, pk_val)
        flash(f"Deleted row from {table_name} where {pk_col} = {pk_val}.", "success")
        return redirect(url_for("db_table", table_name=table_name))

    @app.route("/db/<table_name>/<int:row_id>")
    def db_row_detail(table_name, row_id):
        """Show a single row with full JSON expansion (useful for telemetry raw_metrics)."""
        if table_name not in _VIEWABLE_TABLES:
            flash(f"Table '{table_name}' is not viewable.", "error")
            return redirect(url_for("db_browser"))
        if not _table_exists(table_name):
            flash(f"Table '{table_name}' does not exist in the current database.", "error")
            return redirect(url_for("db_browser"))

        # Try common PK column names
        pk_col = "id"
        if table_name == "garage":
            pk_col = "vin"
        elif table_name == "app_config":
            pk_col = "key"

        row = db.fetch_one(f"SELECT * FROM {table_name} WHERE {pk_col} = %s", (row_id,))
        if row is None:
            flash(f"Row {row_id} not found in {table_name}.", "error")
            return redirect(url_for("db_table", table_name=table_name))

        import json as json_mod
        formatted = {}
        for k, v in row.items():
            if isinstance(v, (dict, list)):
                formatted[k] = json_mod.dumps(v, indent=2, default=str)
            else:
                formatted[k] = v

        return render_template(
            "db_row_detail.html",
            table_name=table_name,
            row_id=row_id,
            row=formatted,
        )

    # ── Database Maintenance ───────────────────────────────────────

    @app.route("/db/maintenance")
    def db_maintenance():
        """Show database maintenance page with stats and available operations."""
        table_stats = _db_table_stats()
        index_stats = _db_index_stats()
        stale_data = _db_check_stale_data()
        
        # Calculate total size
        total_size_result = db.fetch_one("SELECT pg_size_pretty(pg_database_size(current_database())) as size")
        total_size = total_size_result["size"] if total_size_result else "unknown"
        
        return render_template(
            "db_maintenance.html",
            table_stats=table_stats,
            index_stats=index_stats,
            stale_data=stale_data,
            total_size=total_size
        )

    @app.route("/db/maintenance/vacuum", methods=["POST"])
    def db_maintenance_vacuum():
        """Run VACUUM ANALYZE to clean up and optimize."""
        try:
            msg = _db_vacuum()
            log.info("Database maintenance: %s", msg)
            flash(msg, "success")
        except Exception as exc:
            log.error("VACUUM failed: %s", exc)
            flash(f"VACUUM failed: {exc}", "error")
        return redirect(url_for("db_maintenance"))

    @app.route("/db/maintenance/reindex", methods=["POST"])
    def db_maintenance_reindex():
        """Rebuild all indexes for performance."""
        try:
            msg = _db_reindex()
            log.info("Database maintenance: %s", msg)
            flash(msg, "success")
        except Exception as exc:
            log.error("REINDEX failed: %s", exc)
            flash(f"REINDEX failed: {exc}", "error")
        return redirect(url_for("db_maintenance"))

    @app.route("/db/maintenance/cleanup-charging", methods=["POST"])
    def db_maintenance_cleanup_charging():
        """Delete old charging records (>90 days)."""
        try:
            msg = _db_cleanup_old_charging()
            log.info("Database maintenance: %s", msg)
            flash(msg, "success")
        except Exception as exc:
            log.error("Cleanup old charging records failed: %s", exc)
            flash(f"Cleanup failed: {exc}", "error")
        return redirect(url_for("db_maintenance"))

    @app.route("/db/maintenance/cleanup-drives", methods=["POST"])
    def db_maintenance_cleanup_drives():
        """Delete old drive records (>180 days)."""
        try:
            msg = _db_cleanup_old_drives()
            log.info("Database maintenance: %s", msg)
            flash(msg, "success")
        except Exception as exc:
            log.error("Cleanup old drives failed: %s", exc)
            flash(f"Cleanup failed: {exc}", "error")
        return redirect(url_for("db_maintenance"))

    # ── Backup & Restore ───────────────────────────────────────────

    @app.route("/backup")
    def backup_page():
        """List available backups and provide create/restore/download/delete controls."""
        backups = backup.list_backups()
        for b in backups:
            b["size_fmt"] = backup._format_size(b["size"])
        return render_template("backup.html", backups=backups)

    @app.route("/backup/create", methods=["POST"])
    def backup_create():
        """Create a new backup (SQL or JSON)."""
        fmt = request.form.get("format", "json")
        label = secure_filename(request.form.get("label", "").strip()[:40])
        try:
            if fmt == "sql":
                path = backup.backup_sql(label=label)
            else:
                path = backup.backup_json(label=label)
            flash(f"Backup created: {os.path.basename(path)}", "success")
        except Exception as exc:
            log.error("Backup failed: %s", exc)
            flash(f"Backup failed: {exc}", "error")
        return redirect(url_for("backup_page"))

    @app.route("/backup/restore", methods=["POST"])
    def backup_restore():
        """Restore from an existing backup file."""
        filename = request.form.get("filename", "")
        if not filename:
            flash("No backup file specified.", "error")
            return redirect(url_for("backup_page"))

        safe_name = os.path.basename(filename)
        filepath = os.path.join(backup.BACKUP_DIR, safe_name)
        if not os.path.isfile(filepath):
            flash("Backup file not found.", "error")
            return redirect(url_for("backup_page"))

        try:
            if safe_name.endswith(".sql"):
                backup.restore_sql(filepath)
                flash(f"SQL restore complete: {safe_name}", "success")
            elif safe_name.endswith(".json"):
                summary = backup.restore_json(filepath)
                total = sum(summary.values())
                flash(f"JSON restore complete: {total} rows from {safe_name}", "success")
            else:
                flash("Unknown backup format.", "error")
        except Exception as exc:
            log.error("Restore failed: %s", exc)
            flash(f"Restore failed: {exc}", "error")
        return redirect(url_for("backup_page"))

    @app.route("/backup/download/<filename>")
    def backup_download(filename):
        """Download a backup file."""
        safe_name = os.path.basename(filename)
        filepath = os.path.join(backup.BACKUP_DIR, safe_name)
        if not os.path.isfile(filepath):
            flash("Backup file not found.", "error")
            return redirect(url_for("backup_page"))
        return send_file(filepath, as_attachment=True, download_name=safe_name)

    @app.route("/backup/delete", methods=["POST"])
    def backup_delete():
        """Delete a backup file."""
        filename = request.form.get("filename", "")
        if backup.delete_backup(filename):
            flash(f"Deleted: {os.path.basename(filename)}", "success")
        else:
            flash("File not found.", "error")
        return redirect(url_for("backup_page"))

    @app.route("/backup/upload", methods=["POST"])
    def backup_upload():
        """Upload a backup file to the backups directory."""
        file = request.files.get("backup_file")
        if not file or file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("backup_page"))

        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ("sql", "json"):
            flash("Only .sql and .json backup files are accepted.", "error")
            return redirect(url_for("backup_page"))

        safe_name = secure_filename(file.filename)
        save_path = os.path.join(backup.BACKUP_DIR, safe_name)
        os.makedirs(backup.BACKUP_DIR, exist_ok=True)
        file.save(save_path)
        flash(f"Uploaded: {safe_name}", "success")
        return redirect(url_for("backup_page"))

    @app.teardown_appcontext
    def _shutdown(exc):
        """Application teardown hook. Pool cleanup handled by atexit."""
        pass  # pool cleanup handled by atexit / signal


    # ── Startup pause and UI suppression ──
    app.config["STARTUP_READY"] = False

    def _delayed_startup():
        developing_mode = "off"
        try:
            if db.is_available():
                developing_mode = _get_setting("developing")
        except Exception as exc:
            log.warning("Failed reading developing mode at startup: %s", exc)

        if developing_mode == "on":
            app.config["STARTUP_READY"] = True
            log.info("Developing mode enabled: skipping startup delay.")
        else:
            log.info("Delaying poller/UI startup for 30 seconds to allow normalization...")
            import time
            time.sleep(30)
            app.config["STARTUP_READY"] = True
            log.info("Startup pause complete. UI and poller now enabled.")
        # Start poller if configured
        try:
            if db.is_available() and (_get_setting("autostart_poller") == "on"):
                if poller.start():
                    log.info("Autostart poller is enabled — poller started at app init")
                else:
                    log.info("Autostart poller enabled, but poller already running")
        except Exception as exc:
            log.warning("Autostart poller check failed: %s", exc)

        try:
            if db.is_available():
                _start_charger_auto_sync_scheduler()
        except Exception as exc:
            log.warning("Charger auto-sync scheduler startup failed: %s", exc)

        try:
            if db.is_available():
                _start_ml_retrain_scheduler()
        except Exception as exc:
            log.warning("ML retraining scheduler startup failed: %s", exc)

        try:
            if db.is_available():
                _start_backup_scheduler()
        except Exception as exc:
            log.warning("Backup scheduler startup failed: %s", exc)

    threading.Thread(target=_delayed_startup, daemon=True).start()

    @app.before_request
    def _suppress_ui_until_ready():
        # Allow setup, static, and API endpoints before ready
        if app.config.get("STARTUP_READY", False):
            return
        safe = {"db_setup", "db_setup_test", "db_setup_create", "db_setup_restore", "db_setup_upload", "static"}
        if request.endpoint in safe or (request.endpoint and request.endpoint.startswith("static")):
            return
        return render_template("startup_wait.html"), 503

    return app


# ── Entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    app = create_app()
    _log = logging.getLogger(__name__)
    debug_mode = (config.environment() == "development")
    use_reloader = os.environ.get("LIGHTNING_USE_RELOADER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    if debug_mode and not use_reloader:
        _log.info(
            "Running in development mode with Flask reloader disabled. "
            "Set LIGHTNING_USE_RELOADER=1 to re-enable code auto-reload."
        )
    elif debug_mode and use_reloader:
        _log.warning(
            "Flask reloader explicitly enabled (LIGHTNING_USE_RELOADER=1). "
            "Background jobs can be interrupted during reloads."
        )

    # SSL/TLS support – try user certs, fall back to self-signed recovery
    ssl_cfg = config.ssl_config()
    ssl_context = None
    ssl_recovery = False

    if ssl_cfg.get("enabled"):
        cert_path = ssl_cfg.get("cert", "")
        key_path = ssl_cfg.get("key", "")

        # Try the user-provided certificates first
        if cert_path and key_path and os.path.isfile(cert_path) and os.path.isfile(key_path):
            valid, msg = crypto.validate_ssl_files(cert_path, key_path)
            if valid:
                ssl_context = (cert_path, key_path)
                _log.info("SSL enabled with user certificates: cert=%s key=%s", cert_path, key_path)
            else:
                _log.warning("User SSL certificates invalid: %s – falling back to recovery certs", msg)

        # Fall back to self-signed recovery certificates
        if ssl_context is None:
            _log.warning("Generating self-signed recovery certificate for HTTPS access")
            try:
                recovery_cert, recovery_key = crypto.generate_self_signed_cert()
                ssl_context = (recovery_cert, recovery_key)
                ssl_recovery = True
                _log.warning(
                    "*** RECOVERY MODE: Using self-signed certificate. "
                    "Upload valid certs in Settings → SSL to clear this warning. ***"
                )
            except Exception as exc:
                _log.error("Failed to generate recovery certificate: %s – starting without SSL", exc)

    # Store SSL state on the app so templates/hooks can see it
    app.config["SSL_ACTIVE"] = ssl_context is not None
    app.config["SSL_RECOVERY"] = ssl_recovery

    app.run(
        host="0.0.0.0",
        port=config.flask_port(),
        debug=debug_mode,
        use_reloader=use_reloader,
        ssl_context=ssl_context,
    )
