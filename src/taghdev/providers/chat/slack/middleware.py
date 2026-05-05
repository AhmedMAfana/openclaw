"""Slack auth middleware — user lookup by Slack member ID + dev mode."""
from __future__ import annotations

import time

from taghdev.services.bot_actions import lookup_user
from taghdev.utils.logging import get_logger

log = get_logger()

# Dev mode sessions: {slack_user_id: expiry_timestamp}
# Cleared on bot restart (intentional — security).
_dev_sessions: dict[str, float] = {}
DEV_SESSION_TTL = 3600  # 1 hour


async def check_auth(user_id: str) -> tuple[bool, object | None]:
    """Check if a Slack user is authorized.

    Returns (authorized, db_user). If not authorized, db_user is None.
    """
    db_user = await lookup_user("slack", user_id)
    if not db_user or not db_user.is_allowed:
        return False, None
    return True, db_user


def is_admin(user_id: str) -> bool:
    """Check if a Slack user is an admin (persistent DB flag + optional dev session)."""
    # Dev session also grants admin for backward compat
    if is_dev_mode(user_id):
        return True
    # Note: for persistent admin checks, callers should use db_user.is_admin after check_auth
    return False


def grant_dev_mode(user_id: str) -> None:
    """Grant dev mode to a Slack user for DEV_SESSION_TTL seconds."""
    _dev_sessions[user_id] = time.time() + DEV_SESSION_TTL
    log.info("slack.dev_mode_granted", user_id=user_id, ttl=DEV_SESSION_TTL)


def revoke_dev_mode(user_id: str) -> None:
    """Revoke dev mode for a Slack user."""
    _dev_sessions.pop(user_id, None)
    log.info("slack.dev_mode_revoked", user_id=user_id)


async def is_admin_async(user_id: str) -> bool:
    """Check if a Slack user is an admin (DB flag preferred, dev session fallback)."""
    if is_dev_mode(user_id):
        return True
    db_user = await lookup_user("slack", user_id)
    return db_user is not None and getattr(db_user, "is_admin", False)


def is_dev_mode(user_id: str) -> bool:
    """Check if a Slack user has active dev mode."""
    expiry = _dev_sessions.get(user_id)
    if expiry is None:
        return False
    if time.time() > expiry:
        _dev_sessions.pop(user_id, None)
        return False
    return True
