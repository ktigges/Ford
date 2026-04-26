"""Database helper – thin wrapper around psycopg2 connection pooling.

Provides a thread-safe PostgreSQL connection pool with convenience methods
for common query patterns (fetch_one, fetch_all, execute).

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.1.0
Date:        2026-04-26
"""

import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

log = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None


def init_pool() -> None:
    """Create the connection pool from config.json settings."""
    global _pool
    db = config.database()
    _pool = ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        host=db["host"],
        port=db["port"],
        dbname=db["name"],
        user=db["user"],
        password=db["password"],
        connect_timeout=db.get("connect_timeout", 10),
    )
    log.info("Database connection pool initialised (host=%s, db=%s)", db["host"], db["name"])


def close_pool() -> None:
    """Shut down the connection pool and release all connections."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


@contextmanager
def get_conn():
    """Yield a connection from the pool; return it when done."""
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


@contextmanager
def get_cursor(commit: bool = False):
    """Yield a RealDictCursor. Optionally commit on success."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


# ── Query helpers ──────────────────────────────────────────────────

def fetch_one(sql: str, params: tuple | None = None) -> dict | None:
    """Execute a query and return the first row as a dict, or None."""
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple | None = None) -> list[dict]:
    """Execute a query and return all rows as a list of dicts."""
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def execute(sql: str, params: tuple | None = None) -> None:
    """Execute a write query (INSERT/UPDATE/DELETE) and auto-commit."""
    with get_cursor(commit=True) as cur:
        cur.execute(sql, params)


def execute_returning(sql: str, params: tuple | None = None) -> dict | None:
    """Execute a write query with RETURNING clause and return the first row."""
    with get_cursor(commit=True) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ── VIN helpers ────────────────────────────────────────────────────

def active_vin() -> str | None:
    """Return the single active VIN from the garage table, or None if empty.

    For this prototype we expect exactly one vehicle in the garage.
    If multiple exist, returns the most recently updated one.
    """
    row = fetch_one("SELECT vin FROM garage ORDER BY updated_at DESC LIMIT 1")
    return row["vin"] if row else None
