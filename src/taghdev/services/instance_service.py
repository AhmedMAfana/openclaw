"""State-machine owner for per-chat isolated instances.

Spec:
  * Contract: specs/001-per-chat-instances/contracts/instance-service.md
  * Data model: specs/001-per-chat-instances/data-model.md §1
  * Research §9 (per-user quota enforcement), §11 (activity sources)

What this module owns:
  * All writes to the `instances` table.
  * State-transition validation (the DB-level CHECK is the first guard; the
    service is the second).
  * Enforcement of the per-user cap (FR-030a) and the platform-capacity
    guard (FR-030), both surfaced as distinct exceptions so `chat_task.py`
    can render distinct chat-facing messages.

What this module explicitly does NOT own:
  * Compose rendering → ``InstanceComposeRenderer``.
  * CF API → ``TunnelService``.
  * GitHub App tokens → ``CredentialsService``.
  * Concrete ``docker compose up`` / ``projctl up`` invocation → ARQ job
    ``provision_instance`` in ``instance_tasks.py`` (T036).
  * Audit logging / redaction → ``audit_service``.

The service is injectable at every I/O seam so contract tests can run
without Postgres / Redis / ARQ — pass fakes via the constructor.
"""
from __future__ import annotations

import dataclasses
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import func, select

from taghdev.models import async_session
from taghdev.models.instance import Instance, InstanceStatus, TerminatedReason
from taghdev.models.web_chat import WebChatSession
from taghdev.services.config_service import get_config
from taghdev.services.credentials_service import CredentialsService
from taghdev.utils.logging import get_logger

log = get_logger()


# Defaults per spec FR-007 / FR-008 / Q5 clarifications and FR-030a.
DEFAULT_IDLE_TTL = timedelta(hours=24)
DEFAULT_GRACE_WINDOW = timedelta(minutes=60)
DEFAULT_PER_USER_CAP = 3

# Statuses that count as "active" for the per-user cap. The DB partial
# unique index (uq_instances_active_per_chat) uses the same set minus
# 'terminating' is deliberate — see data-model.md §1.4. The cap check
# however DOES include 'terminating' because a user with a tearing-down
# instance should wait for it to finish before starting a new one.
ACTIVE_STATUSES: frozenset[str] = frozenset({
    InstanceStatus.PROVISIONING.value,
    InstanceStatus.RUNNING.value,
    InstanceStatus.IDLE.value,
    InstanceStatus.TERMINATING.value,
})

# Valid reasons for terminate(). Mirrors ck_instances_terminated_reason.
_VALID_TERMINATE_REASONS: frozenset[str] = frozenset({
    TerminatedReason.USER_REQUEST.value,
    TerminatedReason.IDLE_24H.value,
    TerminatedReason.FAILED.value,
    TerminatedReason.PROJECT_DELETED.value,
    TerminatedReason.CHAT_DELETED.value,
    TerminatedReason.ADMIN_FORCED.value,  # spec 003 — admin Force Terminate
})


# ---------------------------------------------------------------------------
# SSE emit helper (spec 003 — contracts/sse-events.md)
#
# All `instance_*` events go through one helper so the contract can be
# audited from a single grep. Order: caller commits DB → calls this →
# returns. The helper itself is best-effort and never raises.
# ---------------------------------------------------------------------------

# Rate-limit summary emits to one per minute (spec 003 — contracts/sse-events.md).
_SUMMARY_DEBOUNCE_S = 60.0
_last_summary_emit_at: float = 0.0


def emit_instance_event(payload: dict) -> None:
    """Append one instance event to the activity log (best-effort, sync)."""
    try:
        from taghdev.services import activity_log
        evt_type = payload.get("type", "instance_event")
        # log_event re-merges the type and timestamp — pass everything else.
        body = {k: v for k, v in payload.items() if k != "type"}
        activity_log.log_event(evt_type, body)
    except Exception:
        # Never let an emit failure break a state transition.
        pass


def maybe_emit_summary(snapshot: dict) -> None:
    """Debounced wrapper for instance_summary events (≤1/min)."""
    global _last_summary_emit_at
    import time as _time
    now = _time.monotonic()
    if now - _last_summary_emit_at < _SUMMARY_DEBOUNCE_S:
        return
    _last_summary_emit_at = now
    emit_instance_event({"type": "instance_summary", **snapshot})


# ---------------------------------------------------------------------------
# Public errors — `chat_task.py` pattern-matches on these to choose the
# chat-facing message. Import paths are part of the contract; do not move.
# ---------------------------------------------------------------------------


class InstanceServiceError(Exception):
    """Base class for every InstanceService failure."""


class InstanceNotFound(InstanceServiceError):
    def __init__(self, instance_id: UUID | str) -> None:
        super().__init__(f"instance {instance_id} not found")
        self.instance_id = instance_id


class ChatNotFound(InstanceServiceError):
    def __init__(self, chat_session_id: int) -> None:
        super().__init__(f"chat_session {chat_session_id} not found")
        self.chat_session_id = chat_session_id


class ProjectNotContainerMode(InstanceServiceError):
    def __init__(self, project_id: int, mode: str) -> None:
        super().__init__(
            f"project {project_id} has mode={mode!r}, expected 'container'"
        )
        self.project_id = project_id
        self.mode = mode


class PerUserCapExceeded(InstanceServiceError):
    """FR-030a — user at the per-user cap."""

    def __init__(self, user_id: int, cap: int, active_chat_ids: list[int]) -> None:
        super().__init__(
            f"user {user_id} already has {len(active_chat_ids)} active "
            f"instances (cap={cap})"
        )
        self.user_id = user_id
        self.cap = cap
        self.active_chat_ids = active_chat_ids


class PlatformAtCapacity(InstanceServiceError):
    """FR-030 — distinct from PerUserCapExceeded by design."""


# ---------------------------------------------------------------------------
# Heartbeat payload shapes (contracts/heartbeat-api.md).
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HeartbeatSignals:
    dev_server_running: bool = False
    task_executing: bool = False
    shell_attached: bool = False


@dataclasses.dataclass(frozen=True)
class HeartbeatAck:
    acknowledged_at: datetime
    expires_at: datetime
    status: str


# ---------------------------------------------------------------------------
# Type aliases for injected seams. Tests pass fakes; production wires these
# at construction time in worker/api startup.
# ---------------------------------------------------------------------------

SessionFactory = Callable[[], Any]             # async context manager yielding AsyncSession
RedisLockFactory = Callable[[str, int], Any]   # (key, ttl_s) -> async-context-manager lock
CapacityGuard = Callable[[], Awaitable[None]]  # raises PlatformAtCapacity to refuse
JobEnqueuer = Callable[[str, Any], Awaitable[Any]]  # (job_name, *args) -> job handle


@asynccontextmanager
async def _noop_lock(_key: str, _ttl_s: int):
    """Default lock factory for environments without Redis (tests).

    Production callers MUST pass a real Redis lock factory — the cap check
    is racy without it (two concurrent provisions can both pass the count
    check). See research.md §9.
    """
    yield


async def _noop_capacity_guard() -> None:
    """Default platform-capacity guard: always passes.

    v1 does not implement host-capacity checking; operators rely on the
    per-user cap + observability. T034a monkey-patches this to prove the
    distinct-error surface works when we DO add a real guard.
    """
    return None


async def _default_enqueuer(job_name: str, *args: Any) -> Any:
    """Default: hand off to arq_app pool. Tests pass a fake that records."""
    from taghdev.services.bot_actions import enqueue_job
    return await enqueue_job(job_name, *args)


# ---------------------------------------------------------------------------
# T082 / T084 — Redis-backed upstream degradation state.
#
# Key shape:  ``taghdev:instance_upstream:<slug>:<capability>`` → ``<upstream>``
# TTL:        180 s (3× the T083 prober cadence). A dead prober cannot
#             leave a stuck banner; a flap-and-heal cycle clears quickly.
# ---------------------------------------------------------------------------


_UPSTREAM_STATE_PREFIX = "taghdev:instance_upstream"
_UPSTREAM_STATE_TTL_S = 180


async def _upstream_redis():
    import redis.asyncio as aioredis
    from taghdev.settings import settings as _s
    return aioredis.from_url(_s.redis_url, decode_responses=False)


async def _upstream_state_set(slug: str, capability: str, upstream: str) -> None:
    try:
        r = await _upstream_redis()
        try:
            await r.set(
                f"{_UPSTREAM_STATE_PREFIX}:{slug}:{capability}",
                upstream.encode("utf-8"),
                ex=_UPSTREAM_STATE_TTL_S,
            )
        finally:
            await r.aclose()
    except Exception as e:
        log.warning(
            "upstream_state.set_failed", slug=slug, capability=capability,
            error=str(e)[:200],
        )


async def _upstream_state_clear(slug: str, capability: str) -> None:
    try:
        r = await _upstream_redis()
        try:
            await r.delete(f"{_UPSTREAM_STATE_PREFIX}:{slug}:{capability}")
        finally:
            await r.aclose()
    except Exception as e:
        log.warning(
            "upstream_state.clear_failed", slug=slug, capability=capability,
            error=str(e)[:200],
        )


async def load_upstream_state(slug: str) -> dict[str, str]:
    """T084 — return the ``{capability: upstream}`` map of current outages.

    Reader API for the chat-UI banner. Empty dict = all upstreams
    healthy. ``assistant_endpoint`` calls this once per inbound message
    and ships any entries as an ``instance_upstream_degraded`` event.
    """
    out: dict[str, str] = {}
    try:
        r = await _upstream_redis()
        try:
            cursor = 0
            pattern = f"{_UPSTREAM_STATE_PREFIX}:{slug}:*"
            while True:
                cursor, keys = await r.scan(cursor, match=pattern, count=50)
                for key in keys:
                    val = await r.get(key)
                    if val is None:
                        continue
                    key_str = (key.decode() if isinstance(key, bytes) else key)
                    cap = key_str.rsplit(":", 1)[-1]
                    out[cap] = val.decode("utf-8", errors="replace")
                if cursor == 0:
                    break
        finally:
            await r.aclose()
    except Exception as e:
        log.warning("upstream_state.load_failed", slug=slug, error=str(e)[:200])
    return out


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class InstanceService:
    """State-machine owner for ``instances`` rows.

    Every method is idempotent (Principle VI) and async (Principle IX).
    Concrete infra (compose up, tunnel provision, GitHub token mint) is
    owned by the ARQ jobs this service enqueues; the service itself only
    writes DB state and emits events.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory = async_session,
        lock_factory: RedisLockFactory = _noop_lock,
        capacity_guard: CapacityGuard = _noop_capacity_guard,
        job_enqueuer: JobEnqueuer = _default_enqueuer,
        idle_ttl: timedelta = DEFAULT_IDLE_TTL,
        grace_window: timedelta = DEFAULT_GRACE_WINDOW,
        per_user_cap: int = DEFAULT_PER_USER_CAP,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._session_factory = session_factory
        self._lock_factory = lock_factory
        self._capacity_guard = capacity_guard
        self._enqueue = job_enqueuer
        self._idle_ttl = idle_ttl
        self._grace_window = grace_window
        self._per_user_cap = per_user_cap
        self._now = now

    # ------------------------------------------------------------------
    # Public API — mirrors contracts/instance-service.md method surface.
    # ------------------------------------------------------------------

    async def _effective_per_user_cap(self) -> int:
        """T053 — read ``per_user_cap`` fresh on every provision().

        Operators can tune FR-030a via
        ``platform_config(category='instance', key='per_user_cap')`` with
        a JSONB body ``{"value": <int>}``. Changes take effect on the
        next provision call without a worker restart. A missing row or a
        read failure falls back to the constructor-supplied default,
        keeping the contract tests (which don't wire ``platform_config``)
        green.
        """
        try:
            cfg = await get_config("instance", "per_user_cap")
            if cfg and "value" in cfg:
                v = int(cfg["value"])
                # 0 is a valid "block all new provisions" value — used
                # for maintenance windows. >0 is the normal range.
                # Negative is interpreted as "operator typo, fall
                # through to constructor default" so we don't honour
                # nonsense values blindly.
                if v >= 0:
                    return v
        except Exception:
            pass
        return self._per_user_cap

    async def provision(self, chat_session_id: int) -> Instance:
        """Begin provisioning a fresh instance for a chat with none.

        Idempotent: if the chat already has an active row, returns it
        unchanged. Enforces the per-user cap (FR-030a) and the platform
        capacity guard (FR-030) BEFORE creating any infra resources.
        """
        async with self._session_factory() as session:
            chat = await self._load_chat(session, chat_session_id)
            project = chat.project
            if project is None or project.mode != "container":
                raise ProjectNotContainerMode(
                    project_id=chat.project_id or 0,
                    mode=(project.mode if project else "<none>"),
                )
            user_id = chat.user_id

            # Re-use any active row for this chat. Terminal rows
            # (destroyed / failed) do NOT count; we provision a new one.
            existing = await self._find_active_for_chat(session, chat_session_id)
            if existing is not None:
                return existing

            # Platform capacity check first — a distinct error from the
            # per-user cap per FR-030 vs FR-030a.
            await self._capacity_guard()

            # Per-user cap is race-sensitive — hold a Redis lock across
            # the count + INSERT (see research.md §9).
            lock_key = f"taghdev:user:{user_id}:provision"
            async with self._lock_factory(lock_key, 30):
                # Re-check inside the lock: two concurrent provisions
                # could have both passed the capacity guard.
                existing = await self._find_active_for_chat(session, chat_session_id)
                if existing is not None:
                    return existing

                active_chat_ids = await self._list_active_chat_ids(session, user_id)
                effective_cap = await self._effective_per_user_cap()
                if len(active_chat_ids) >= effective_cap:
                    raise PerUserCapExceeded(
                        user_id=user_id,
                        cap=effective_cap,
                        active_chat_ids=active_chat_ids,
                    )

                instance = self._build_row(chat, per_user_count=len(active_chat_ids))
                session.add(instance)
                await session.commit()
                await session.refresh(instance)

                # Enqueue INSIDE the lock + shielded against caller
                # cancellation. The row was just committed; if we let
                # the parent task get cancelled here (e.g. a streaming
                # WS handler closes mid-flow), the enqueue would
                # silently skip and the row would sit in 'provisioning'
                # forever with no worker job to advance it. Shield
                # ensures: once the row is in DB, the job lands in
                # arq's queue, period. The orphan-recovery branch in
                # inactivity_reaper is the safety net if even this fails
                # (process kill / redis blip).
                import asyncio as _asyncio
                await _asyncio.shield(
                    self._enqueue("provision_instance", str(instance.id))
                )

        log.info(
            "instance.provisioning",
            instance_slug=instance.slug,
            chat_session_id=chat_session_id,
            user_id=user_id,
            project_id=instance.project_id,
        )
        return instance

    async def get_or_resume(self, chat_session_id: int) -> Instance:
        """Return the chat's active instance, provisioning if none.

        Primary entry point from ``chat_task.py``. If the chat's only
        rows are terminal (destroyed / failed), a fresh instance is
        provisioned — carrying forward the ``session_branch`` from the
        most-recent terminal row so the user's in-progress commits are
        preserved (FR-012/FR-013, T069). If the active row is ``idle``,
        it is touched so the grace-window teardown is cancelled.
        """
        async with self._session_factory() as session:
            existing = await self._find_active_for_chat(session, chat_session_id)
            if existing is None:
                # T069: before provisioning, inherit the session_branch
                # from the chat's most-recent terminal instance so the
                # new row reattaches to the user's live code instead of
                # starting fresh. Write it back to the chat row so a
                # second re-entry skips the history scan.
                prior_branch = await self._load_prior_session_branch(
                    session, chat_session_id
                )
                if prior_branch is not None:
                    chat = await session.get(WebChatSession, chat_session_id)
                    if chat is not None and chat.session_branch_name != prior_branch:
                        chat.session_branch_name = prior_branch
                        await session.commit()
                return await self.provision(chat_session_id)
            if existing.status == InstanceStatus.IDLE.value:
                # Returning from idle — reset the clock and clear the
                # grace notification so the chat banner disappears.
                await self._apply_touch(session, existing)
                await session.commit()
                await session.refresh(existing)
            return existing

    async def _load_prior_session_branch(
        self, session: Any, chat_session_id: int
    ) -> str | None:
        """Return ``session_branch`` of the chat's most-recent terminal row.

        Terminal = ``destroyed`` / ``failed`` / ``terminating``. The chat
        is about to re-provision; whichever ``session_branch`` the user
        last worked on is the one to reattach to (FR-012). Returns None
        if the chat never provisioned before — caller falls through to
        the default ``chat-<id>-session`` naming.
        """
        result = await session.execute(
            select(Instance)
            .where(
                Instance.chat_session_id == chat_session_id,
                Instance.status.in_((
                    InstanceStatus.DESTROYED.value,
                    InstanceStatus.FAILED.value,
                    InstanceStatus.TERMINATING.value,
                )),
            )
            .order_by(Instance.created_at.desc())
            .limit(1)
        )
        prior = result.scalar_one_or_none()
        if prior is None:
            return None
        return prior.session_branch

    async def touch(self, instance_id: UUID) -> None:
        """Bump activity. No-op outside {running, idle}.

        Called on every inbound chat message (chat_task.py) and on every
        heartbeat POST (api/routers/instances.py). Must be cheap — one
        indexed UPDATE.
        """
        async with self._session_factory() as session:
            inst = await session.get(Instance, instance_id)
            if inst is None:
                raise InstanceNotFound(instance_id)
            if inst.status not in (
                InstanceStatus.RUNNING.value,
                InstanceStatus.IDLE.value,
            ):
                return
            await self._apply_touch(session, inst)
            await session.commit()

    async def terminate(self, instance_id: UUID, *, reason: str) -> None:
        """Mark an instance terminating and enqueue the teardown job.

        Idempotent. Calling on an already-terminating / destroyed row is
        a no-op. ``reason`` must be one of the closed set enforced by
        ck_instances_terminated_reason.
        """
        if reason not in _VALID_TERMINATE_REASONS:
            raise ValueError(
                f"reason must be one of {sorted(_VALID_TERMINATE_REASONS)}, "
                f"got {reason!r}"
            )
        async with self._session_factory() as session:
            inst = await session.get(Instance, instance_id)
            if inst is None:
                raise InstanceNotFound(instance_id)
            if inst.status in (
                InstanceStatus.TERMINATING.value,
                InstanceStatus.DESTROYED.value,
                InstanceStatus.FAILED.value,
            ):
                return
            lock_key = f"taghdev:instance:{inst.slug}"
            async with self._lock_factory(lock_key, 60):
                await session.refresh(inst)
                if inst.status in (
                    InstanceStatus.TERMINATING.value,
                    InstanceStatus.DESTROYED.value,
                    InstanceStatus.FAILED.value,
                ):
                    return
                inst.status = InstanceStatus.TERMINATING.value
                inst.terminated_reason = reason
                await session.commit()

        log.info(
            "instance.terminating",
            instance_slug=inst.slug,
            reason=reason,
        )
        # Spec 003 — emit SSE so the admin UI converges in ≤10s.
        # Order: commit (above) → emit → enqueue teardown.
        emit_instance_event({
            "type": "instance_status",
            "slug": inst.slug,
            "status": InstanceStatus.TERMINATING.value,
            "previous_status": "running",  # best-effort; precise prior is not tracked here
            "reason": reason,
        })
        await self._enqueue("teardown_instance", str(instance_id))

    async def list_active(
        self, *, user_id: int | None = None
    ) -> list[Instance]:
        """Return rows with status in ACTIVE_STATUSES.

        user_id=None → platform-wide view (caller enforces admin perm).
        user_id=int  → one user's active instances.
        """
        async with self._session_factory() as session:
            stmt = select(Instance).where(Instance.status.in_(ACTIVE_STATUSES))
            if user_id is not None:
                stmt = (
                    stmt.join(
                        WebChatSession,
                        WebChatSession.id == Instance.chat_session_id,
                    )
                    .where(WebChatSession.user_id == user_id)
                )
            stmt = stmt.order_by(Instance.created_at.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def record_upstream_degradation(
        self,
        instance_id: UUID,
        *,
        capability: str,
        upstream: str,
    ) -> None:
        """T082 — mark one capability as upstream-degraded. DOES NOT flip
        the instance status.

        FR-027a: an upstream outage (Cloudflare, GitHub App, DNS) is NOT
        a reason to terminate a running instance. It's a banner-level
        event. Callers — the periodic prober (T083), the rotate-git-
        token handler — emit this on failure; ``record_upstream_recovery``
        clears it. The chat-UI reads the resulting state and renders a
        non-blocking banner (T084).

        State lives in Redis with a short TTL so a dead prober never
        leaves a banner stuck on screen: key
        ``taghdev:instance_upstream:<slug>:<capability>`` set to the
        upstream name, expires after 180 s (3× the 60 s prober cadence).

        ``capability`` is a short constant (``"preview_url"``,
        ``"github_push"``, ``"dns"``). ``upstream`` names the upstream
        that failed (``"cloudflare"``, ``"github_app"``, etc.) so the
        banner copy can be specific.
        """
        slug = await self._load_slug(instance_id)
        if slug is not None:
            await _upstream_state_set(slug, capability, upstream)
        log.info(
            "instance.upstream_degraded",
            instance_id=str(instance_id),
            capability=capability,
            upstream=upstream,
        )

    async def record_upstream_recovery(
        self,
        instance_id: UUID,
        *,
        capability: str,
        upstream: str,
    ) -> None:
        """T082 — clear a previously-recorded upstream outage.

        Removes the Redis state key so the chat banner clears on the
        next poll tick (FR-027b). Idempotent — a DEL on a missing key
        is a no-op.
        """
        slug = await self._load_slug(instance_id)
        if slug is not None:
            await _upstream_state_clear(slug, capability)
        log.info(
            "instance.upstream_recovered",
            instance_id=str(instance_id),
            capability=capability,
            upstream=upstream,
        )

    async def _load_slug(self, instance_id: UUID) -> str | None:
        """Look up the slug for an instance id. Cheap; cacheable later."""
        async with self._session_factory() as session:
            inst = await session.get(Instance, instance_id)
            return inst.slug if inst is not None else None

    async def record_heartbeat(
        self, slug: str, signals: HeartbeatSignals
    ) -> HeartbeatAck:
        """Bump activity + return the new expires_at for the HB response.

        Separate from ``touch()`` because the API layer needs both the
        signal record (for diagnostics) and the ack payload to return to
        ``projctl``. Raises InstanceNotFound on unknown slugs so the
        router returns 404.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(Instance).where(Instance.slug == slug)
            )
            inst = result.scalar_one_or_none()
            if inst is None:
                raise InstanceNotFound(slug)
            if inst.status not in (
                InstanceStatus.RUNNING.value,
                InstanceStatus.IDLE.value,
                InstanceStatus.PROVISIONING.value,
            ):
                # Router translates this into 409 per heartbeat-api.md.
                return HeartbeatAck(
                    acknowledged_at=self._now(),
                    expires_at=inst.expires_at,
                    status=inst.status,
                )
            await self._apply_touch(session, inst)
            await session.commit()
            await session.refresh(inst)

        log.debug(
            "instance.heartbeat",
            instance_slug=slug,
            signals=dataclasses.asdict(signals),
        )
        return HeartbeatAck(
            acknowledged_at=self._now(),
            expires_at=inst.expires_at,
            status=inst.status,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_chat(
        self, session: Any, chat_session_id: int
    ) -> WebChatSession:
        chat = await session.get(WebChatSession, chat_session_id)
        if chat is None:
            raise ChatNotFound(chat_session_id)
        return chat

    async def _find_active_for_chat(
        self, session: Any, chat_session_id: int
    ) -> Instance | None:
        """Return the chat's one active row, if any.

        The partial unique index uq_instances_active_per_chat guarantees
        there's at most one.
        """
        result = await session.execute(
            select(Instance).where(
                Instance.chat_session_id == chat_session_id,
                Instance.status.in_(ACTIVE_STATUSES),
            )
        )
        return result.scalar_one_or_none()

    async def _list_active_chat_ids(
        self, session: Any, user_id: int
    ) -> list[int]:
        """Chat IDs whose user_id == :user_id with an active instance.

        Used for both the cap check AND for FR-030b's navigation payload
        so the chat error message can link the user to their live chats.
        """
        result = await session.execute(
            select(Instance.chat_session_id)
            .join(
                WebChatSession,
                WebChatSession.id == Instance.chat_session_id,
            )
            .where(
                WebChatSession.user_id == user_id,
                Instance.status.in_(ACTIVE_STATUSES),
            )
        )
        return [row[0] for row in result.all()]

    def _build_row(
        self, chat: WebChatSession, *, per_user_count: int
    ) -> Instance:
        """Construct a new Instance row with a freshly-minted slug.

        Slug format is strict (ck_instances_slug_format). 56-bit entropy
        per FR-018a. Duplicate slug on INSERT would violate uq_instances_slug;
        the probability is ~0 but the DB is the final guard.
        """
        slug = f"inst-{secrets.token_hex(7)}"
        now = self._now()
        session_branch = (
            chat.session_branch_name or f"chat-{chat.id}-session"
        )
        return Instance(
            slug=slug,
            chat_session_id=chat.id,
            project_id=chat.project_id,
            status=InstanceStatus.PROVISIONING.value,
            compose_project=f"tagh-{slug}",
            workspace_path=f"/workspaces/{slug}/",
            session_branch=session_branch,
            heartbeat_secret=CredentialsService.generate_heartbeat_secret(),
            db_password=CredentialsService.generate_db_password(),
            per_user_count_at_provision=per_user_count,
            last_activity_at=now,
            expires_at=now + self._idle_ttl,
        )

    async def _apply_touch(self, session: Any, inst: Instance) -> None:
        """Bump last_activity_at / expires_at and reset idle state.

        Caller owns the commit. Keeps the UPDATE tight so it's safe under
        the partial unique index.
        """
        now = self._now()
        inst.last_activity_at = now
        inst.expires_at = now + self._idle_ttl
        inst.grace_notification_at = None
        if inst.status == InstanceStatus.IDLE.value:
            inst.status = InstanceStatus.RUNNING.value


__all__ = [
    "ACTIVE_STATUSES",
    "ChatNotFound",
    "HeartbeatAck",
    "HeartbeatSignals",
    "InstanceNotFound",
    "InstanceService",
    "InstanceServiceError",
    "PerUserCapExceeded",
    "PlatformAtCapacity",
    "ProjectNotContainerMode",
]
