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

from openclow.models import async_session
from openclow.models.instance import Instance, InstanceStatus, TerminatedReason
from openclow.models.web_chat import WebChatSession
from openclow.services.credentials_service import CredentialsService
from openclow.utils.logging import get_logger

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
})


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
    from openclow.services.bot_actions import enqueue_job
    return await enqueue_job(job_name, *args)


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
            lock_key = f"openclow:user:{user_id}:provision"
            async with self._lock_factory(lock_key, 30):
                # Re-check inside the lock: two concurrent provisions
                # could have both passed the capacity guard.
                existing = await self._find_active_for_chat(session, chat_session_id)
                if existing is not None:
                    return existing

                active_chat_ids = await self._list_active_chat_ids(session, user_id)
                if len(active_chat_ids) >= self._per_user_cap:
                    raise PerUserCapExceeded(
                        user_id=user_id,
                        cap=self._per_user_cap,
                        active_chat_ids=active_chat_ids,
                    )

                instance = self._build_row(chat, per_user_count=len(active_chat_ids))
                session.add(instance)
                await session.commit()
                await session.refresh(instance)

        log.info(
            "instance.provisioning",
            instance_slug=instance.slug,
            chat_session_id=chat_session_id,
            user_id=user_id,
            project_id=instance.project_id,
        )
        # Enqueue the actual infra work. Service stays cheap; the job is
        # idempotent on its own (research.md §4).
        await self._enqueue("provision_instance", str(instance.id))
        return instance

    async def get_or_resume(self, chat_session_id: int) -> Instance:
        """Return the chat's active instance, provisioning if none.

        Primary entry point from ``chat_task.py``. If the chat's only
        rows are terminal (destroyed / failed), a fresh instance is
        provisioned. If the active row is ``idle``, it is touched so the
        grace-window teardown is cancelled.
        """
        async with self._session_factory() as session:
            existing = await self._find_active_for_chat(session, chat_session_id)
            if existing is None:
                return await self.provision(chat_session_id)
            if existing.status == InstanceStatus.IDLE.value:
                # Returning from idle — reset the clock and clear the
                # grace notification so the chat banner disappears.
                await self._apply_touch(session, existing)
                await session.commit()
                await session.refresh(existing)
            return existing

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
            lock_key = f"openclow:instance:{inst.slug}"
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
