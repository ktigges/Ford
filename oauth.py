"""Reusable OAuth authentication module.

Handles token refresh, rotation-safe storage, and credential management
for Ford's Azure AD B2C OAuth2 endpoint. This module is independent of Flask
and can be used by the poller, UI, or CLI.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.2.1
Date:        2026-04-28
"""

import base64
import json
import logging
from datetime import datetime, timezone

import requests

import db
import crypto

log = logging.getLogger("oauth")

# Fields that must never appear in logs
_SENSITIVE_FIELDS = {"client_secret", "access_token", "refresh_token"}


# ── Token diagnostics ─────────────────────────────────────────────

def _decode_jwt_claims(token: str) -> dict | None:
    """Decode the payload of a JWT without verifying signature.

    Used purely for local diagnostic logging – never for auth decisions.
    Returns None if the token is not a valid JWT.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        # JWT base64url – add padding
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return None


def log_token_diagnostics(token: str, context: str = "") -> None:
    """Log non-sensitive JWT claims useful for debugging API errors.

    Logs: aud, scp/scope, exp, iss, iat, sub (truncated), tid.
    Never logs the full token value.
    """
    claims = _decode_jwt_claims(token)
    if claims is None:
        log.warning("[%s] Access token is not a decodable JWT (opaque token?)", context)
        return

    safe_fields = {}
    for key in ("aud", "scp", "scope", "exp", "iss", "iat", "tid", "azp", "appid"):
        if key in claims:
            safe_fields[key] = claims[key]

    # Truncate subject for privacy
    sub = claims.get("sub", "")
    if sub:
        safe_fields["sub"] = sub[:8] + "..."

    # Convert exp/iat to human-readable
    for ts_key in ("exp", "iat"):
        if ts_key in safe_fields and isinstance(safe_fields[ts_key], (int, float)):
            ts_val = datetime.fromtimestamp(safe_fields[ts_key], tz=timezone.utc)
            safe_fields[f"{ts_key}_utc"] = ts_val.isoformat()

    log.info("[%s] Token claims: %s", context, safe_fields)


# ── Credential lookup ──────────────────────────────────────────────

def get_credentials(provider: str, vin: str) -> dict | None:
    """Return the oauth_credentials row for a provider/VIN pair, or None.

    The client_secret is transparently decrypted before being returned.
    """
    row = db.fetch_one(
        "SELECT * FROM oauth_credentials WHERE provider = %s AND vin = %s AND enabled = TRUE",
        (provider, vin),
    )
    if row and row.get("client_secret"):
        row["client_secret"] = crypto.decrypt(row["client_secret"])
    return row


def get_valid_access_token(provider: str, vin: str) -> str | None:
    """Return a valid access token, refreshing first if expired.

    Returns None if credentials are missing or refresh fails.
    """
    creds = get_credentials(provider, vin)
    if creds is None:
        log.warning("No enabled OAuth credentials for provider=%s vin=%s", provider, vin)
        return None

    now = datetime.now(timezone.utc)
    expires = creds.get("access_token_expires_at")
    if creds.get("access_token") and expires and expires > now:
        return creds["access_token"]

    # Token is missing or expired – refresh
    log.info("Access token expired or missing; refreshing (provider=%s, vin=%s)", provider, vin)
    result = refresh_access_token(creds)
    if result is None:
        return None
    return result.get("access_token")


# ── Token refresh ──────────────────────────────────────────────────

def _build_token_fields(creds: dict) -> dict:
    """Build multipart form fields for a token refresh request.

    Uses the ``files`` parameter style for requests.post() which produces
    multipart/form-data – matching what Ford's Azure AD B2C endpoint expects.
    Each value is a tuple of (None, value) so requests sends it as a form
    field rather than a file upload.
    """
    fields = {
        "grant_type": (None, "refresh_token"),
        "client_id": (None, creds["client_id"]),
        "client_secret": (None, creds["client_secret"]),
        "refresh_token": (None, creds["refresh_token"]),
    }
    if creds.get("scope"):
        fields["scope"] = (None, creds["scope"])
    if creds.get("redirect_uri"):
        fields["redirect_url"] = (None, creds["redirect_uri"])
    return fields


def refresh_access_token(creds: dict) -> dict | None:
    """Perform an OAuth2 refresh-token grant and persist the result.

    Returns the updated credential dict on success, None on failure.
    Uses multipart/form-data to match Ford's Azure AD B2C token endpoint.
    """
    fields = _build_token_fields(creds)

    log.info("[TOKEN REFRESH] Requesting new access token from %s  fields=%s",
             creds["token_endpoint"],
             [k for k in fields])  # log field names only, never values

    try:
        resp = requests.post(creds["token_endpoint"], files=fields, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("[TOKEN REFRESH] FAILED for provider=%s vin=%s: %s", creds["provider"], creds["vin"], exc)
        if hasattr(exc, 'response') and exc.response is not None:
            log.error("[TOKEN REFRESH] Response status: %d  body: %s",
                      exc.response.status_code, exc.response.text[:500])
        return None

    body = resp.json()

    new_access_token = body.get("access_token")
    if not new_access_token:
        log.error("[TOKEN REFRESH] Response missing access_token (provider=%s, vin=%s). Keys returned: %s",
                  creds["provider"], creds["vin"], list(body.keys()))
        return None

    log.info("[TOKEN REFRESH] SUCCESS – access token received (expires_in=%s)", body.get("expires_in"))

    # Log token diagnostics to help debug audience/scope mismatches
    log_token_diagnostics(new_access_token, context=f"refresh:{creds['provider']}/{creds['vin']}")

    expires_in = body.get("expires_in")
    if expires_in:
        expires_at = datetime.now(timezone.utc).replace(microsecond=0)
        from datetime import timedelta
        expires_at += timedelta(seconds=int(expires_in))
    else:
        expires_at = None

    # Rotation-safe: only replace refresh_token if the provider sent a new one
    new_refresh_token = body.get("refresh_token") or creds["refresh_token"]

    _persist_tokens(
        cred_id=creds["id"],
        access_token=new_access_token,
        access_token_expires_at=expires_at,
        refresh_token=new_refresh_token,
    )

    log.info("Token refresh succeeded (provider=%s, vin=%s)", creds["provider"], creds["vin"])

    creds["access_token"] = new_access_token
    creds["access_token_expires_at"] = expires_at
    creds["refresh_token"] = new_refresh_token
    return creds


# ── Validation (used by setup UI) ─────────────────────────────────

def validate_credentials(form_data: dict) -> tuple[dict | None, str | None]:
    """Attempt a token refresh with the supplied credentials.

    Returns (token_data, None) on success or (None, error_message) on failure.
    ``form_data`` must contain the same keys as an oauth_credentials row.
    """
    fields = _build_token_fields(form_data)

    log.info("[TOKEN VALIDATE] Requesting token from %s  fields=%s",
             form_data["token_endpoint"],
             [k for k in fields])

    try:
        resp = requests.post(form_data["token_endpoint"], files=fields, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("[TOKEN VALIDATE] FAILED: %s", exc)
        if hasattr(exc, 'response') and exc.response is not None:
            log.error("[TOKEN VALIDATE] Response status: %d  body: %s",
                      exc.response.status_code, exc.response.text[:500])
        return None, f"OAuth validation failed: {exc}"

    body = resp.json()
    if "access_token" not in body:
        return None, "OAuth response missing access_token"

    # Diagnostic logging for the validated token
    log_token_diagnostics(body["access_token"], context="validate")

    return body, None


# ── Save credentials ──────────────────────────────────────────────

def save_credentials(provider: str, vin: str | None, form_data: dict, token_data: dict) -> None:
    """Insert or update OAuth credentials with validated token data."""
    from datetime import timedelta

    expires_in = token_data.get("expires_in")
    expires_at = None
    if expires_in:
        expires_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=int(expires_in))

    new_refresh = token_data.get("refresh_token") or form_data["refresh_token"]
    now = datetime.now(timezone.utc)

    db.execute(
        """
        INSERT INTO oauth_credentials
            (provider, vin, client_id, client_secret, scope, redirect_uri,
             refresh_token, token_endpoint, access_token, access_token_expires_at,
             enabled, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
        ON CONFLICT (provider, vin) DO UPDATE SET
            client_id = EXCLUDED.client_id,
            client_secret = EXCLUDED.client_secret,
            scope = EXCLUDED.scope,
            redirect_uri = EXCLUDED.redirect_uri,
            refresh_token = EXCLUDED.refresh_token,
            token_endpoint = EXCLUDED.token_endpoint,
            access_token = EXCLUDED.access_token,
            access_token_expires_at = EXCLUDED.access_token_expires_at,
            enabled = TRUE,
            updated_at = EXCLUDED.updated_at
        """,
        (
            provider, vin,
            form_data["client_id"], crypto.encrypt(form_data["client_secret"]),
            form_data["scope"], form_data["redirect_uri"],
            new_refresh, form_data["token_endpoint"],
            token_data["access_token"], expires_at,
            now, now,
        ),
    )
    log.info("OAuth credentials saved (provider=%s, vin=%s)", provider, vin)


# ── Internal helpers ───────────────────────────────────────────────

def _persist_tokens(*, cred_id: int, access_token: str,
                    access_token_expires_at: datetime | None,
                    refresh_token: str) -> None:
    db.execute(
        """
        UPDATE oauth_credentials
        SET access_token = %s,
            access_token_expires_at = %s,
            refresh_token = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (access_token, access_token_expires_at, refresh_token, cred_id),
    )
