"""Symmetric encryption helpers and SSL certificate utilities.

Uses Fernet (AES-128-CBC with HMAC-SHA256) from the ``cryptography`` library.
The encryption key is stored in a local file (``secret.key``) which is
generated automatically on first use.

Also provides self-signed certificate generation for SSL recovery mode.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.2.1
Date:        2026-04-28
"""

import logging
import ipaddress
import os
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)

_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret.key")
_fernet: Fernet | None = None


def _load_or_create_key() -> bytes:
    """Load the Fernet key from disk, or generate and persist a new one."""
    if os.path.isfile(_KEY_PATH):
        with open(_KEY_PATH, "rb") as f:
            key = f.read().strip()
        log.debug("Encryption key loaded from %s", _KEY_PATH)
        return key

    key = Fernet.generate_key()
    # Write with restrictive permissions (owner-only read/write)
    fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    log.info("New encryption key generated and saved to %s", _KEY_PATH)
    return key


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return a URL-safe base64-encoded token."""
    if not plaintext:
        return plaintext
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token back to plaintext.

    If decryption fails (e.g. the value was stored before encryption was
    enabled), the original value is returned unchanged so the app doesn't
    crash during migration.
    """
    if not ciphertext:
        return ciphertext
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        # Graceful fallback for pre-encryption plaintext values
        log.debug("Decryption failed – returning value as-is (likely pre-encryption plaintext)")
        return ciphertext


# ── Self-signed certificate generation ─────────────────────────────

_CERTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
_RECOVERY_CERT = os.path.join(_CERTS_DIR, "recovery.crt")
_RECOVERY_KEY = os.path.join(_CERTS_DIR, "recovery.key")


def generate_self_signed_cert(cn: str = "Lightning Recovery",
                              days: int = 365) -> tuple[str, str]:
    """Generate a self-signed certificate and private key for recovery mode.

    Files are written to ``certs/recovery.crt`` and ``certs/recovery.key``.
    If they already exist and are not expired, they are reused.

    Returns (cert_path, key_path).
    """
    os.makedirs(_CERTS_DIR, exist_ok=True)

    # Reuse existing recovery cert if still valid
    if os.path.isfile(_RECOVERY_CERT) and os.path.isfile(_RECOVERY_KEY):
        try:
            with open(_RECOVERY_CERT, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            if cert.not_valid_after_utc > datetime.now(timezone.utc):
                log.info("Reusing existing recovery certificate (expires %s)",
                         cert.not_valid_after_utc.isoformat())
                return _RECOVERY_CERT, _RECOVERY_KEY
        except Exception:
            pass  # regenerate

    # Generate RSA private key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Build self-signed certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lightning EV Tool"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write private key (restrictive permissions)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(_RECOVERY_KEY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_pem)

    # Write certificate
    with open(_RECOVERY_CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    log.info("Self-signed recovery certificate generated: %s (valid %d days)",
             _RECOVERY_CERT, days)
    return _RECOVERY_CERT, _RECOVERY_KEY


def validate_ssl_files(cert_path: str, key_path: str) -> tuple[bool, str]:
    """Check that a cert and key file are valid and match each other.

    Returns (valid: bool, message: str).
    """
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)

        # Verify the public keys match
        cert_pub = cert.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        key_pub = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

        if cert_pub != key_pub:
            return False, "Certificate and private key do not match"

        # Check expiry
        if cert.not_valid_after_utc < datetime.now(timezone.utc):
            return False, "Certificate has expired"

        return True, "Certificate is valid"
    except FileNotFoundError:
        return False, "Certificate or key file not found"
    except Exception as exc:
        return False, f"Invalid certificate or key: {exc}"
