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

from taghdev.api.web_auth import web_user_dep
from taghdev.models.base import async_session
from taghdev.models.instance import Instance, InstanceStatus
from taghdev.models.user import User
from taghdev.services.instance_service import (
    HeartbeatSignals,
    InstanceNotFound,
    InstanceService,
)
from taghdev.settings import settings
from taghdev.utils.logging import get_logger

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
    from taghdev.services.instance_service import DEFAULT_PER_USER_CAP
    return {"instances": payload, "cap": DEFAULT_PER_USER_CAP}


@router.post("/users/{user_id}/instances/{instance_id}/terminate")
async def terminate_user_instance(
    user_id: int,
    instance_id: str,
    user: User = Depends(web_user_dep),
) -> dict:
    """Terminate a specific instance owned by user_id.

    Non-admin users may only terminate their own instances.
    """
    if user.id != user_id and not user.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")

    from uuid import UUID as _UUID
    try:
        inst_uuid = _UUID(instance_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid instance_id")

    # Verify ownership before terminating.
    rows = await InstanceService().list_active(user_id=user_id)
    owned_ids = {str(r.id) for r in rows}
    if instance_id not in owned_ids and not user.is_admin:
        raise HTTPException(status_code=403, detail="instance does not belong to this user")

    try:
        await InstanceService().terminate(inst_uuid, reason="user_request")
    except Exception as e:
        log.warning("user_terminate_instance.failed", instance_id=instance_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)[:200])

    return {"status": "ok", "instance_id": instance_id}


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
_HB_RATE_KEY = "taghdev:heartbeat_rate"  # INCR/EXPIRE per slug
_HB_FAIL_KEY = "taghdev:heartbeat_auth_fail"  # alert counter per slug

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


@internal_router.post("/instances/{slug}/rotate-git-token")
async def rotate_git_token(slug: str, request: Request) -> JSONResponse:
    """Mint + return a fresh 1-hour GitHub installation token.

    Contract: specs/001-per-chat-instances/contracts/heartbeat-api.md.
    Called by ``projctl rotate-git-token`` every 45 min inside the
    instance (cron). Same HMAC auth + rate-limit + status gate as the
    heartbeat endpoint; additional failure mode is a GitHub App outage
    → 503 with ``Retry-After`` so projctl silently waits for the next
    cron tick.

    Token is scoped to exactly one repo — the project's repo bound to
    this instance. GitHub rejects pushes elsewhere at the auth layer
    (T061 exercises that), so a token leak from this endpoint is still
    bounded to one repo's contents.
    """
    if not await _check_rate_limit(slug):
        return JSONResponse(
            status_code=429,
            content={"status": "rate_limited"},
            headers={"Retry-After": str(_HB_WINDOW_S)},
        )

    raw_body = await request.body()

    async with async_session() as session:
        result = await session.execute(
            select(Instance).where(Instance.slug == slug)
        )
        inst = result.scalar_one_or_none()

    if inst is None:
        return JSONResponse(status_code=404, content={"status": "unknown_slug"})

    sig_header = request.headers.get("x-signature") or request.headers.get("X-Signature")
    if not _verify_hmac(sig_header, raw_body, inst.heartbeat_secret):
        await _bump_auth_fail_counter(slug)
        log.warning("rotate_git_token.hmac_failed", slug=slug)
        return JSONResponse(status_code=401, content={"status": "unauthorized"})

    if inst.status not in (
        InstanceStatus.RUNNING.value,
        InstanceStatus.IDLE.value,
        InstanceStatus.PROVISIONING.value,
    ):
        return JSONResponse(
            status_code=409,
            content={"status": inst.status},
        )

    # Resolve the project repo.
    repo: str | None = None
    async with async_session() as session:
        proj_inst = await session.get(Instance, inst.id)
        if proj_inst is not None and proj_inst.project is not None:
            repo = proj_inst.project.github_repo
    if not repo:
        return JSONResponse(
            status_code=409,
            content={"status": "no_repo_bound"},
        )

    # Mint a scoped token. CredentialsService already does the JWT +
    # installation-token exchange; we just call it here.
    try:
        from taghdev.services.credentials_service import (
            CredentialsService,
            GitHubAppConfig,
            GitHubAppError,
        )
        from taghdev.services.config_service import get_config
        cfg = await get_config("github_app", "settings")
        if not cfg:
            # Operator hasn't configured the GitHub App yet.
            return JSONResponse(
                status_code=503,
                content={"status": "github_app_unconfigured"},
                headers={"Retry-After": "300"},
            )
        gh_cfg = GitHubAppConfig(
            app_id=str(cfg["app_id"]),
            private_key_pem=cfg["private_key_pem"],
        )
        token = await CredentialsService(gh_cfg).github_push_token(
            inst.id, repo
        )
    except GitHubAppError as e:
        # FR-027a: treat as upstream degradation. projctl retries on its
        # own 45-min cron; the banner policy in the chat UI gets a
        # separate upstream_degraded event out-of-band.
        log.warning(
            "rotate_git_token.github_app_outage",
            slug=slug, error=str(e)[:200],
        )
        return JSONResponse(
            status_code=503,
            content={"status": "github_app_degraded"},
            headers={"Retry-After": "300"},
        )
    except Exception as e:
        log.exception("rotate_git_token.unexpected", slug=slug, error=str(e))
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable"},
            headers={"Retry-After": "60"},
        )

    return JSONResponse({
        "token": token.token,
        "expires_at": datetime.fromtimestamp(
            token.expires_at, tz=timezone.utc
        ).isoformat(),
        "repo": token.repo,
    })


# ---------------------------------------------------------------------------
# LLM fallback — T079 / arch §9 / contracts/llm-fallback-envelope.schema.json
# ---------------------------------------------------------------------------


class _LLMEnvelopeStep(BaseModel):
    name: str
    cmd: str
    cwd: str
    success_check: str | None = None
    skippable: bool = False


class _LLMEnvelope(BaseModel):
    """Shape mirrors contracts/llm-fallback-envelope.schema.json.

    ``additionalProperties: false`` is enforced only at schema-validation
    time (T077); pydantic ignores unknown keys by default which is fine
    for the HTTP path — the JSON Schema validator is the gate.
    """
    instance_slug: str
    project_name: str
    step: _LLMEnvelopeStep
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    guide_section: str = ""
    previous_attempts: int = 0


@internal_router.post("/instances/{slug}/explain")
async def explain(slug: str, request: Request) -> JSONResponse:
    """Receive a failure envelope from ``projctl explain`` and return an action.

    Contract (arch §9): the orchestrator-side of the self-healing loop.
    projctl sends a bounded envelope built per
    [contracts/llm-fallback-envelope.schema.json]; the orchestrator:

      1. Verifies HMAC against the instance's heartbeat_secret.
      2. Runs ``audit_service.redact`` on every text field (belt-and-
         braces — projctl SHOULD already redact locally, but Principle
         IV mandates this redactor runs on the LLM path too).
      3. Forwards to the LLM with a structured system prompt.
      4. Returns ``{action, payload, reason}`` per the response schema.

    Response shape:
      ```json
      {"action": "shell_cmd" | "patch" | "skip" | "give_up",
       "payload": "<shell command or unified diff or empty>",
       "reason":  "<one-sentence human-readable explanation>"}
      ```

    Security rules match the heartbeat / rotate-git-token endpoints —
    same rate limit, same HMAC authentication.
    """
    if not await _check_rate_limit(slug):
        return JSONResponse(
            status_code=429,
            content={"status": "rate_limited"},
            headers={"Retry-After": str(_HB_WINDOW_S)},
        )

    raw_body = await request.body()

    async with async_session() as session:
        result = await session.execute(
            select(Instance).where(Instance.slug == slug)
        )
        inst = result.scalar_one_or_none()

    if inst is None:
        return JSONResponse(status_code=404, content={"status": "unknown_slug"})

    sig_header = (
        request.headers.get("x-signature") or request.headers.get("X-Signature")
    )
    if not _verify_hmac(sig_header, raw_body, inst.heartbeat_secret):
        await _bump_auth_fail_counter(slug)
        log.warning("explain.hmac_failed", slug=slug)
        return JSONResponse(status_code=401, content={"status": "unauthorized"})

    try:
        env = _LLMEnvelope.model_validate_json(raw_body)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "bad_envelope", "detail": str(e)[:200]},
        )

    # Belt-and-braces redaction. The redactor is idempotent; projctl
    # may have run it locally but the LLM path MUST call it per
    # Principle IV no matter who the upstream caller is.
    from taghdev.services.audit_service import redact as _redact

    env_safe = env.model_copy(update={
        "stdout_tail": _redact(env.stdout_tail or ""),
        "stderr_tail": _redact(env.stderr_tail or ""),
        "guide_section": _redact(env.guide_section or ""),
    })

    # Truncation guards: schema caps stdout/stderr at 32 KiB each. If
    # projctl sent more, keep the tail (most recent lines are most
    # informative). Keeps the LLM prompt predictable.
    def _cap_tail(s: str, cap: int = 32_768) -> str:
        if len(s) <= cap:
            return s
        marker = f"\n... {len(s) - cap} chars truncated ...\n"
        return marker + s[-cap:]

    # Cap previous_attempts at 3 per the schema — silently clamp.
    attempts = max(0, min(int(env_safe.previous_attempts), 3))

    log.info(
        "explain.received",
        slug=slug,
        step=env_safe.step.name,
        attempt=attempts,
        exit_code=env_safe.exit_code,
    )

    # v1 policy: if projctl has already burned 3 attempts, refuse to
    # call the LLM again and return `give_up` so the caller surfaces
    # the failure to the user. Saves tokens and matches arch §9.
    if attempts >= 3:
        return JSONResponse({
            "action": "give_up",
            "payload": "",
            "reason": "Reached the 3-attempt cap for this step.",
        })

    # Build the LLM prompt. Kept as a small inline template so the
    # system prompt is auditable in one place.
    prompt = (
        "You are fixing a failing deterministic setup step inside a "
        "project container. You MUST respond with exactly one JSON "
        "object of shape:\n"
        '  {"action": "shell_cmd"|"patch"|"skip"|"give_up", '
        '"payload": "...", "reason": "..."}\n'
        "Rules:\n"
        "- `shell_cmd`: a single shell command to run before retrying "
        "  the step's original cmd. Keep it short; one command only.\n"
        "- `patch`: a unified diff. Applied with `git apply --check` "
        "  first; rejected if it doesn't clean-apply.\n"
        "- `skip`: only permitted if the step is marked skippable.\n"
        "- `give_up`: no safe action — the user must intervene.\n"
        f"Context:\n"
        f"  instance:  {env_safe.instance_slug}\n"
        f"  project:   {env_safe.project_name}\n"
        f"  step:      {env_safe.step.name} ({env_safe.step.cmd})\n"
        f"  cwd:       {env_safe.step.cwd}\n"
        f"  skippable: {env_safe.step.skippable}\n"
        f"  exit_code: {env_safe.exit_code}\n"
        f"  attempt:   {attempts + 1} of 3\n"
        "Guide section:\n"
        f"```\n{env_safe.guide_section[:4000]}\n```\n"
        "Last stdout (tail):\n"
        f"```\n{_cap_tail(env_safe.stdout_tail, 4000)}\n```\n"
        "Last stderr (tail):\n"
        f"```\n{_cap_tail(env_safe.stderr_tail, 4000)}\n```\n"
        "Respond with the JSON object only."
    )

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock as _TextBlock
        options = ClaudeAgentOptions(
            system_prompt=(
                "Structured self-healing. One JSON object, no prose. "
                "Never emit secrets even if they appear in the log tail — "
                "the upstream redactor may miss context-specific tokens."
            ),
            model="claude-sonnet-4-6",
            allowed_tools=[],
            mcp_servers={},
            permission_mode="bypassPermissions",
            max_turns=1,
        )
        full = ""
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, _TextBlock):
                        full += block.text
        full = full.strip()
    except Exception as e:
        log.warning("explain.llm_failed", slug=slug, error=str(e)[:200])
        return JSONResponse({
            "action": "give_up",
            "payload": "",
            "reason": f"LLM unavailable: {str(e)[:120]}",
        })

    # Parse the LLM response. Defensive: an LLM that emits fence-wrapped
    # JSON is common; strip that before json.loads.
    import json as _json
    cleaned = full
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Drop the opening fence and (optionally) the closing fence.
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = _json.loads(cleaned or "{}")
        action = parsed.get("action", "give_up")
        if action not in ("shell_cmd", "patch", "skip", "give_up"):
            action = "give_up"
        payload = str(parsed.get("payload") or "")
        reason = str(parsed.get("reason") or "")
        if action == "skip" and not env_safe.step.skippable:
            # Hard-enforced policy: LLM cannot return skip on a
            # non-skippable step even if it tries.
            action = "give_up"
            reason = (
                "LLM asked to skip a non-skippable step — refused."
            )
            payload = ""
    except Exception:
        action, payload, reason = "give_up", "", "LLM returned unparseable JSON."

    return JSONResponse({
        "action": action,
        "payload": payload,
        "reason": reason,
    })
