"""
GHL webhook signature verification.

GHL is migrating webhook signing from HMAC-SHA256 (X-WH-Signature) to
Ed25519 (X-GHL-Signature) on 2026-07-01. We accept both during the
transition.

Stub only — fill in during Sprint 2 GHL integration work.
"""
from __future__ import annotations

import hashlib
import hmac


def verify_hmac_sha256(body: bytes, signature: str, secret: str) -> bool:
    """Legacy X-WH-Signature verification. Deprecated 2026-07-01."""
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_ed25519(body: bytes, signature: str, public_key_pem: str) -> bool:
    """
    X-GHL-Signature verification using GHL's Ed25519 public key.

    TODO: implement using `cryptography` library. Stub returns False so
    unverified requests are rejected in prod.
    """
    del body, signature, public_key_pem  # silence unused warnings
    return False
