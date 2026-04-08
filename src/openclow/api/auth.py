"""Settings dashboard authentication — API key based."""
import os
import secrets

from fastapi import Cookie, Depends, HTTPException, Request


# Key from environment, or generate a random one on first boot
SETTINGS_API_KEY = os.environ.get("SETTINGS_API_KEY", "")


def _get_api_key() -> str:
    """Return the configured API key, or empty string if auth is disabled."""
    return SETTINGS_API_KEY


async def verify_settings_auth(
    request: Request,
    settings_token: str | None = Cookie(default=None),
) -> None:
    """FastAPI dependency — checks cookie or Authorization header.

    If SETTINGS_API_KEY is not set, auth is bypassed (dev mode).
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

    # Check cookie
    if settings_token and secrets.compare_digest(settings_token, api_key):
        return

    raise HTTPException(status_code=401, detail="Invalid or missing settings API key")
