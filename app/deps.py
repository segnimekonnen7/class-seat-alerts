"""
Shared dependencies.

The API key is optional by design: unset locally so the thing is easy to run,
required in production (config.validate_production_safety refuses to boot
without one). An always-on key would make the dev setup annoying enough that
people disable it and forget; an always-off one would leave a public endpoint
that lets anyone queue work against the school's servers.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    Guard write endpoints when API_KEY is configured.

    Compared with compare_digest rather than `==` so the check does not leak
    the key one character at a time through response timing.
    """
    settings = get_settings()
    if not settings.api_key:
        return

    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid X-API-Key header is required",
        )
