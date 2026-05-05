"""Settings dashboard authentication — API key based."""
from __future__ import annotations

import os
import secrets

from fastapi import Cookie, HTTPException, Request


# Key from environment, or generate a random one on first boot
SETTINGS_API_KEY = os.environ.get("SETTINGS_API_KEY", "")


def _get_api_key() -> str:
    """Return the configured API key, or empty string if auth is disabled."""
    return SETTINGS_API_KEY


async def verify_settings_auth(
    request: Request,
    settings_token: str | None = Cookie(default=None),
    web_token: str | None = Cookie(default=None),
) -> None:
    """FastAPI dependency — checks cookie, Authorization header, or admin JWT.

    If SETTINGS_API_KEY is not set, auth is bypassed (dev mode).
    Admin users logged into the web chat can access settings via their JWT.
    """
    api_key = _get_api_key()
    if not api_key:
        # No key configured — allow access (dev / first-time setup)
        return

    # Check Authorization header first
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if secrets.compare_digest(token, api_key):
            return

    # Check settings cookie
    if settings_token and secrets.compare_digest(settings_token, api_key):
        return

    # Accept admin JWT from the web chat — allows admins to use settings from /chat
    if web_token:
        from taghdev.api.web_auth import verify_web_token
        user = await verify_web_token(web_token)
        if user and user.is_admin:
            return

    raise HTTPException(status_code=401, detail="Invalid or missing settings API key")
