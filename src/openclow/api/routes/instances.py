"""Per-chat instance endpoints.

Two route groups live here:

* **User-facing** — ``GET /api/users/<user_id>/instances`` (T043).
  Authenticated via ``web_user_dep``. Powers the per-user-cap error UI
  (FR-030b).

* **Internal** — ``POST /internal/instances/<slug>/heartbeat`` (T050),
  called by ``projctl`` inside the instance. HMAC-authenticated against
  the per-instance ``heartbeat_secret``; rate-limited per slug via
  Redis ``INCR/EXPIRE``. These routes live on the orchestrator's
  internal port and MUST NOT be exposed publicly (nginx does not route
  ``/internal/*``). See [contracts/heartbeat-api.md](../../../specs/001-per-chat-instances/contracts/heartbeat-api.md).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from openclow.api.web_auth import web_user_dep
from openclow.models.base import async_session
from openclow.models.instance import Instance, InstanceStatus
from openclow.models.user import User
from openclow.services.instance_service import (
    HeartbeatSignals,
    InstanceNotFound,
    InstanceService,
)
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

router = APIRouter(prefix="/api", tags=["instances"])


@router.get("/users/{user_id}/instances")
async def list_user_instances(
    user_id: int,
    user: User = Depends(web_user_dep),
) -> dict:
    """Return the active instances owned by ``user_id``.

    Shape (one entry per active instance):

    ```json
    {
      "instances": [
        {
          "id": "<uuid>",
          "slug": "inst-<hex>",
          "chat_session_id": 42,
          "project_id": 7,
          "status": "running",
          "web_hostname": "inst-<hex>.dev.<domain>" | null,
          "started_at": "2026-04-23T12:34:56Z" | null,
          "last_activity_at": "2026-04-23T13:00:00Z",
          "expires_at": "2026-04-24T13:00:00Z"
        }
      ]
    }
    ```

    Non-admin users can only see their own instances — a 403 is returned
    if ``user_id`` is not the caller. This is the backing API for the
    ``PerUserCapExceeded`` chat card (FR-030b), so the path includes the
    user id explicitly rather than deriving it from the JWT, keeping the
    shape admin-ready.
    """
    if user.id != user_id and not user.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")

    rows = await InstanceService().list_active(user_id=user_id)
    payload = []
    for inst in rows:
        # Tunnels are loaded eagerly via `selectin` (see Instance.tunnels);
        # pick the first non-destroyed one for the preview URL surface.
        web_host: str | None = None
        for t in getattr(inst, "tunnels", []) or []:
            if t.status != "destroyed":
                web_host = t.web_hostname
                break
        payload.append({
            "id": str(inst.id),
            "slug": inst.slug,
            "chat_session_id": inst.chat_session_id,
            "project_id": inst.project_id,
            "status": inst.status,
            "web_hostname": web_host,
            "started_at": inst.started_at.isoformat() if inst.started_at else None,
            "last_activity_at": inst.last_activity_at.isoformat(),
            "expires_at": inst.expires_at.isoformat(),
        })
    return {"instances": payload}


# ---------------------------------------------------------------------------
# Internal heartbeat API — T050 / contracts/heartbeat-api.md
# ---------------------------------------------------------------------------
#
# Mounted with the ``/internal`` prefix. The orchestrator's public ingress
# (nginx / Cloudflare) MUST NOT route ``/internal/*``; only compose-network
# traffic from inside a per-instance container reaches here.


class _HeartbeatSignalsBody(BaseModel):
    dev_server_running: bool = False
    task_executing: bool = False
    shell_attached: bool = False


class _HeartbeatBody(BaseModel):
    at: str | None = None
    signals: _HeartbeatSignalsBody | None = None
    guide_state_sha: str | None = None


# Redis key namespaces.
_HB_RATE_KEY = "openclow:heartbeat_rate"  # INCR/EXPIRE per slug
_HB_FAIL_KEY = "openclow:heartbeat_auth_fail"  # alert counter per slug

# Contract — one heartbeat every 30 s hard floor (heartbeat-api.md §Response).
# Window is 1s INCR + EXPIRE so a burst of >1 req/s fails fast without
# blocking a well-behaved 60s cadence.
_HB_WINDOW_S = 1
_HB_MAX_PER_WINDOW = 1


async def _redis_client():
    import redis.asyncio as aioredis
    return aioredis.from_url(settings.redis_url, decode_responses=False)


def _verify_hmac(signature_header: str | None, raw_body: bytes, secret: str) -> bool:
    """HMAC-SHA256 of the raw request body, constant-time compare.

    Header shape: ``X-Signature: hmac-sha256=<hex>``. Missing header,
    wrong algorithm prefix, or wrong length all fail fast before the
    constant-time compare so those paths don't leak timing.
    """
    if not signature_header:
        return False
    prefix = "hmac-sha256="
    if not signature_header.startswith(prefix):
        return False
    provided_hex = signature_header[len(prefix):]
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if len(provided_hex) != len(expected):
        return False
    return hmac.compare_digest(provided_hex, expected)


async def _check_rate_limit(slug: str) -> bool:
    """Return True if the slug is within the allowed rate; False if limited.

    Best-effort: Redis unreachable → allow the request. That's a softer
    failure than rejecting every heartbeat during a Redis outage; the
    auth guard is the actual security boundary.
    """
    try:
        r = await _redis_client()
        try:
            key = f"{_HB_RATE_KEY}:{slug}"
            n = await r.incr(key)
            if n == 1:
                await r.expire(key, _HB_WINDOW_S)
            return int(n) <= _HB_MAX_PER_WINDOW
        finally:
            await r.aclose()
    except Exception as e:
        log.warning("heartbeat.rate_limit_unavailable", slug=slug, error=str(e))
        return True


async def _bump_auth_fail_counter(slug: str) -> None:
    """Record an HMAC failure for alerting (>10 in 5 min trips the alert)."""
    try:
        r = await _redis_client()
        try:
            key = f"{_HB_FAIL_KEY}:{slug}"
            n = await r.incr(key)
            if n == 1:
                await r.expire(key, 300)
        finally:
            await r.aclose()
    except Exception:
        pass  # Alerting is best-effort; never mask the auth refusal.


# Separate router so main.py can mount with a different prefix. Keeping
# ``/internal/*`` off the public ``/api`` prefix is part of the security
# contract — see heartbeat-api.md §Security rules.
internal_router = APIRouter(prefix="/internal", tags=["instances_internal"])


@internal_router.post("/instances/{slug}/heartbeat")
async def heartbeat(slug: str, request: Request) -> JSONResponse:
    """Bump ``last_activity_at`` for an instance; ack with new ``expires_at``.

    Security rules (contracts/heartbeat-api.md):
      1. HMAC-SHA256 of raw body using per-instance ``heartbeat_secret``.
      2. Slug in path MUST match the HMAC-signing instance (cross-instance
         forgery guard).
      3. Rate-limited at ~1 req/s per slug; >30 req/s → 429.
      4. Status ∈ {``terminating``, ``destroyed``, ``failed``} → 409.

    Contract tests in ``tests/contract/test_heartbeat_api.py``.
    """
    # Rate limit FIRST so a flood of forged requests cannot overwhelm the
    # auth / DB paths.
    if not await _check_rate_limit(slug):
        return JSONResponse(
            status_code=429,
            content={"status": "rate_limited"},
            headers={"Retry-After": str(_HB_WINDOW_S)},
        )

    raw_body = await request.body()

    # Look up the instance by slug; 404 is indistinguishable from a
    # forged HMAC at the response level, but we still need the row to
    # get the secret for the compare.
    async with async_session() as session:
        result = await session.execute(
            select(Instance).where(Instance.slug == slug)
        )
        inst = result.scalar_one_or_none()

    if inst is None:
        # Per contract §404 — same behaviour as 401; instance was most
        # likely re-provisioned with a new secret and this is an old
        # projctl still cranking.
        return JSONResponse(status_code=404, content={"status": "unknown_slug"})

    # HMAC verification. Header may be lowercased depending on the
    # proxy in front of FastAPI; match case-insensitively.
    sig_header = request.headers.get("x-signature") or request.headers.get("X-Signature")
    if not _verify_hmac(sig_header, raw_body, inst.heartbeat_secret):
        await _bump_auth_fail_counter(slug)
        log.warning("heartbeat.hmac_failed", slug=slug)
        return JSONResponse(status_code=401, content={"status": "unauthorized"})

    # Status gate per contract §409. Terminal rows must stop heartbeats.
    if inst.status not in (
        InstanceStatus.RUNNING.value,
        InstanceStatus.IDLE.value,
        InstanceStatus.PROVISIONING.value,
    ):
        return JSONResponse(
            status_code=409,
            content={"status": inst.status},
        )

    # Parse the body late — if it's malformed, the signature still has
    # to succeed first. Use try/except to avoid a 422 leaking schema.
    try:
        body = _HeartbeatBody.model_validate_json(raw_body or b"{}")
    except Exception:
        body = _HeartbeatBody()
    signals = HeartbeatSignals(
        dev_server_running=bool(body.signals.dev_server_running) if body.signals else False,
        task_executing=bool(body.signals.task_executing) if body.signals else False,
        shell_attached=bool(body.signals.shell_attached) if body.signals else False,
    )

    try:
        ack = await InstanceService().record_heartbeat(slug, signals)
    except InstanceNotFound:
        return JSONResponse(status_code=404, content={"status": "unknown_slug"})

    return JSONResponse({
        "acknowledged_at": ack.acknowledged_at.isoformat(),
        "expires_at": ack.expires_at.isoformat(),
        "status": ack.status,
    })
