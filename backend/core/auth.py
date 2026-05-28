"""
Shared-secret auth used by routes that aren't event-driven webhooks.

Currently used by /sync/* and /contacts/* (5/28). The /agents/* routes have
an inline copy of the same helper — fine to migrate them to this module
later; left alone for now to keep this change minimal.

Header: `X-CHAWQ-Secret`. Configured via `settings.chawq_shared_secret`
(populated from CHAWQ_SHARED_SECRET env var / Secret Manager in prod).
"""
from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, status

from core.settings import get_settings

logger = logging.getLogger(__name__)

CHAWQ_SECRET_HEADER = "X-CHAWQ-Secret"


def verify_chawq_shared_secret(provided: str | None, *, route_name: str) -> None:
    """
    Compare the provided X-CHAWQ-Secret header against settings.chawq_shared_secret.

    - Raises HTTPException(401) on mismatch or missing header.
    - Logs a warning and accepts the call when the secret is unset
      (preserves local-dev ergonomics — matches the agents.py behavior).
    - Constant-time comparison via secrets.compare_digest.

    `route_name` is logged on rejection so the log line points at the right
    route surface.
    """
    expected = get_settings().chawq_shared_secret
    if not expected:
        logger.warning(
            "chawq_shared_secret unset — accepting %s request unauthenticated. "
            "Set CHAWQ_SHARED_SECRET in prod.",
            route_name,
        )
        return

    if not provided:
        logger.info(
            "%s request missing %s header — rejecting",
            route_name, CHAWQ_SECRET_HEADER,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {route_name} secret header",
        )

    if not secrets.compare_digest(provided, expected):
        logger.info("%s request secret mismatch — rejecting", route_name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid {route_name} secret",
        )
