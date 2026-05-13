"""Local username/password + TOTP MFA helpers.

This module owns local auth schema management, password verification,
MFA secret lifecycle, and server-side authenticated session validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pyotp
from werkzeug.security import check_password_hash, generate_password_hash

import db


_SESSION_KEYS = (
    "auth_mode",
    "local_user_id",
    "local_username",
    "local_is_admin",
    "local_auth_session_id",
    "local_auth_expires_at",
    "local_pending_user_id",
    "local_pending_next",
    "local_pending_stage",
    "local_pending_mfa_secret",
)


def ensure_schema() -> None:
    """Create local auth tables/indexes when missing."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS local_users (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            is_admin BOOLEAN NOT NULL DEFAULT FALSE,
            mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            mfa_secret TEXT,
            mfa_enrolled_at TIMESTAMPTZ,
            last_login_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS local_users_username_lower_uidx
        ON local_users (LOWER(username))
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS local_auth_sessions (
            id UUID PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES local_users(id) ON DELETE CASCADE,
            issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            mfa_verified BOOLEAN NOT NULL DEFAULT FALSE,
            ip_address TEXT,
            user_agent TEXT
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS local_auth_sessions_user_idx
        ON local_auth_sessions (user_id, revoked_at, expires_at)
        """
    )


def clear_session(session_obj: dict) -> None:
    """Remove all local-auth related values from Flask session."""
    for key in _SESSION_KEYS:
        session_obj.pop(key, None)


def revoke_session(session_obj: dict) -> None:
    """Mark current auth session as revoked in DB (best effort)."""
    sid = str(session_obj.get("local_auth_session_id") or "").strip()
    if not sid:
        return
    try:
        db.execute(
            """
            UPDATE local_auth_sessions
            SET revoked_at = now()
            WHERE id::text = %s AND revoked_at IS NULL
            """,
            (sid,),
        )
    except Exception:
        pass


def user_count() -> int:
    row = db.fetch_one("SELECT COUNT(*) AS cnt FROM local_users")
    return int((row or {}).get("cnt") or 0)


def admin_count() -> int:
    row = db.fetch_one("SELECT COUNT(*) AS cnt FROM local_users WHERE is_admin = TRUE")
    return int((row or {}).get("cnt") or 0)


def get_user_by_id(user_id: int) -> dict | None:
    return db.fetch_one("SELECT * FROM local_users WHERE id = %s", (user_id,))


def get_user_by_username(username: str) -> dict | None:
    return db.fetch_one(
        "SELECT * FROM local_users WHERE LOWER(username) = LOWER(%s)",
        (username,),
    )


def list_users() -> list[dict]:
    rows = db.fetch_all(
        """
        SELECT id, username, email, is_active, is_admin, mfa_enabled,
               mfa_enrolled_at, last_login_at, created_at, updated_at
        FROM local_users
        ORDER BY LOWER(username)
        """
    )
    return [dict(r) for r in rows] if rows else []


def create_user(
    username: str,
    password: str,
    email: str = "",
    *,
    is_admin: bool = False,
    is_active: bool = True,
) -> dict:
    username = (username or "").strip()
    if not username:
        raise ValueError("Username is required")
    if not password:
        raise ValueError("Password is required")

    password_hash = generate_password_hash(password)
    return db.execute_returning(
        """
        INSERT INTO local_users (
            username, email, password_hash, is_active, is_admin, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, now(), now())
        RETURNING *
        """,
        (username, (email or "").strip() or None, password_hash, is_active, is_admin),
    )


def update_password(user_id: int, password: str) -> None:
    if not password:
        raise ValueError("Password is required")
    db.execute(
        """
        UPDATE local_users
        SET password_hash = %s, updated_at = now()
        WHERE id = %s
        """,
        (generate_password_hash(password), user_id),
    )


def set_user_active(user_id: int, is_active: bool) -> None:
    db.execute(
        "UPDATE local_users SET is_active = %s, updated_at = now() WHERE id = %s",
        (is_active, user_id),
    )


def disable_mfa(user_id: int) -> None:
    db.execute(
        """
        UPDATE local_users
        SET mfa_enabled = FALSE, mfa_secret = NULL, mfa_enrolled_at = NULL, updated_at = now()
        WHERE id = %s
        """,
        (user_id,),
    )


def enable_mfa(user_id: int, secret: str) -> None:
    db.execute(
        """
        UPDATE local_users
        SET mfa_enabled = TRUE, mfa_secret = %s, mfa_enrolled_at = now(), updated_at = now()
        WHERE id = %s
        """,
        (secret, user_id),
    )


def verify_password(username: str, password: str) -> dict | None:
    """Return user row on successful password auth, otherwise None."""
    user = get_user_by_username(username)
    if not user:
        return None
    if not bool(user.get("is_active")):
        return None
    password_hash = str(user.get("password_hash") or "")
    if not password_hash:
        return None
    if not check_password_hash(password_hash, password or ""):
        return None
    return user


def generate_mfa_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(user: dict, secret: str, issuer: str = "MLLightning") -> str:
    username = str(user.get("username") or "user")
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    secret = str(secret or "").strip()
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not secret or len(code) < 6:
        return False
    return bool(pyotp.TOTP(secret).verify(code, valid_window=valid_window))


def begin_pending_login(session_obj: dict, user_id: int, next_url: str, stage: str) -> None:
    session_obj["local_pending_user_id"] = int(user_id)
    session_obj["local_pending_next"] = next_url
    session_obj["local_pending_stage"] = stage


def pending_login(session_obj: dict) -> dict[str, Any]:
    return {
        "user_id": int(session_obj.get("local_pending_user_id") or 0),
        "next_url": str(session_obj.get("local_pending_next") or "").strip(),
        "stage": str(session_obj.get("local_pending_stage") or "").strip(),
    }


def issue_authenticated_session(
    session_obj: dict,
    user: dict,
    *,
    ip_address: str,
    user_agent: str,
    lifetime_hours: int = 8,
) -> str:
    """Persist a validated local auth session and mirror key fields in Flask session."""
    session_id = str(uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=lifetime_hours)
    db.execute(
        """
        INSERT INTO local_auth_sessions (id, user_id, expires_at, mfa_verified, ip_address, user_agent)
        VALUES (%s, %s, %s, TRUE, %s, %s)
        """,
        (session_id, int(user["id"]), expires_at, ip_address or None, user_agent or None),
    )

    clear_session(session_obj)
    session_obj["auth_mode"] = "local"
    session_obj["local_user_id"] = int(user["id"])
    session_obj["local_username"] = str(user.get("username") or "")
    session_obj["local_is_admin"] = bool(user.get("is_admin"))
    session_obj["local_auth_session_id"] = session_id
    session_obj["local_auth_expires_at"] = int(expires_at.timestamp())

    db.execute(
        "UPDATE local_users SET last_login_at = now(), updated_at = now() WHERE id = %s",
        (int(user["id"]),),
    )
    return session_id


def validate_authenticated_session(session_obj: dict) -> dict | None:
    """Validate local auth session against DB record and active user state."""
    if session_obj.get("auth_mode") != "local":
        return None

    user_id = int(session_obj.get("local_user_id") or 0)
    session_id = str(session_obj.get("local_auth_session_id") or "").strip()
    if not user_id or not session_id:
        return None

    row = db.fetch_one(
        """
        SELECT u.id, u.username, u.email, u.is_active, u.is_admin, u.mfa_enabled,
               s.id AS session_id, s.expires_at, s.revoked_at, s.mfa_verified
        FROM local_users u
        JOIN local_auth_sessions s ON s.user_id = u.id
        WHERE u.id = %s
          AND s.id::text = %s
          AND s.revoked_at IS NULL
          AND s.expires_at > now()
          AND s.mfa_verified = TRUE
        LIMIT 1
        """,
        (user_id, session_id),
    )
    if not row:
        return None
    if not bool(row.get("is_active")):
        return None
    if not bool(row.get("mfa_enabled")):
        # Enforce MFA enrollment for all local users.
        return None
    return dict(row)