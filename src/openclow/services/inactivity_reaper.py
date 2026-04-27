"""Idle-instance reaper — two-phase teardown for chats that went quiet.

Spec: specs/001-per-chat-instances/tasks.md T048; research.md §11;
FR-007 (24h idle TTL), FR-008 (60min grace window after the warning).

Two phases per sweep:

  * **running → idle** for rows where ``expires_at <= now()`` and no
    ``grace_notification_at`` is set. Also emits the chat banner so the
    user knows a teardown is imminent and can keep the chat alive by
    sending a message (which calls ``InstanceService.touch`` → row flips
    back to ``running`` and ``grace_notification_at`` clears).
  * **idle → terminating** for rows where
    ``grace_notification_at + grace_window <= now()``. This just flips
    state and enqueues ``teardown_instance``; the ARQ job does the
    actual docker/CF/filesystem work.

Called from an ARQ cron (every 5 minutes per research.md §11). The
selection query uses ``FOR UPDATE SKIP LOCKED`` so multiple reaper
replicas never race on the same row — v1 runs one, but the design does
not preclude scaling out.

DRY-RUN mode via ``REAPER_DRY_RUN=1`` env: every planned transition is
logged through ``log.info`` but NO DB mutation occurs and NO teardown
is enqueued. T046 exercises this.

Operator tunables (FR-007 / FR-008 / FR-030a, T053): ``idle_ttl_hours``
and ``idle_grace_minutes`` are read fresh from ``platform_config``
(``category='instance'``) on every sweep. An operator can tune them
without restarting the worker — the next 5-min tick picks up the new
values.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select

from openclow.models import async_session
from openclow.models.instance import Instance, InstanceStatus
from openclow.services.config_service import get_config
from openclow.utils.logging import get_logger

log = get_logger()


# Defaults mirror InstanceService.DEFAULT_* and are used when
# platform_config has no explicit row. Kept here rather than imported
# to avoid coupling the reaper to InstanceService's frozen-at-init
# values.
_DEFAULT_IDLE_TTL = timedelta(hours=24)
_DEFAULT_GRACE_WINDOW = timedelta(minutes=60)

# Claim-and-process batch size per phase. FOR UPDATE SKIP LOCKED means
# concurrent reaper replicas each take their own batch without blocking.
_BATCH_SIZE = 50

# A real provision_instance job finishes in ~90s end-to-end. Anything
# in 'provisioning' status for more than this is almost certainly an
# orphan — the row was committed but the worker never ran the job
# (process crash, redis blip, or upstream-cancellation race that
# bypassed the shielded enqueue in instance_service.provision()).
# Mark it failed so the user sees a Retry button instead of a stuck
# spinner forever.
_PROVISIONING_ORPHAN_AGE = timedelta(minutes=5)


def _dry_run_enabled() -> bool:
    return os.environ.get("REAPER_DRY_RUN") == "1"


async def _load_tunables() -> tuple[timedelta, timedelta]:
    """Read ``idle_ttl_hours`` + ``idle_grace_minutes`` fresh.

    Operator can tune these in ``platform_config`` with
    ``category='instance'`` rows. Missing rows fall back to v1 defaults.
    """
    ttl_hours: float = 24.0
    grace_min: float = 60.0
    try:
        ttl_cfg = await get_config("instance", "idle_ttl_hours")
        if ttl_cfg and "value" in ttl_cfg:
            ttl_hours = float(ttl_cfg["value"])
    except Exception:
        pass
    try:
        grace_cfg = await get_config("instance", "idle_grace_minutes")
        if grace_cfg and "value" in grace_cfg:
            grace_min = float(grace_cfg["value"])
    except Exception:
        pass
    return (
        timedelta(hours=ttl_hours) if ttl_hours > 0 else _DEFAULT_IDLE_TTL,
        timedelta(minutes=grace_min) if grace_min > 0 else _DEFAULT_GRACE_WINDOW,
    )


async def reap(
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    on_grace_notification: Callable[[Instance], "object"] | None = None,
    job_enqueuer: Callable[..., "object"] | None = None,
) -> dict:
    """Run one sweep of the two-phase reaper.

    Returns a summary dict: ``{"notified": int, "terminated": int,
    "dry_run": bool}``. Handy for the ARQ job's return value and for
    tests that want to assert zero-mutation behaviour under DRY-RUN.

    ``on_grace_notification`` is called once per row transitioned
    ``running → idle`` with the Instance row. Tests pass a capturing
    fake; production wires it to the provider abstraction's
    ``send_message_with_actions`` through ``chat_session_service``.

    ``job_enqueuer`` is called for each ``idle → terminating`` row with
    ``("teardown_instance", str(instance_id))`` arguments. Tests pass a
    capturing fake; production wires it to ARQ via ``bot_actions``.
    """
    dry_run = _dry_run_enabled()
    idle_ttl, grace_window = await _load_tunables()
    now = now_fn()
    notified = 0
    terminated = 0

    orphan_failed = 0

    # ── Phase 0: provisioning → failed (orphan: no worker job in flight) ─
    # Safety net for the rare case where a row got committed in
    # 'provisioning' but the enqueue never reached the worker. The
    # shielded enqueue in InstanceService.provision() handles the common
    # cancellation race; this picks up anything that slipped through
    # (process kill / redis blip / etc). Threshold = 5min, generous vs
    # the typical ~90s real provision time.
    async with async_session() as session:
        orphan_cutoff = now - _PROVISIONING_ORPHAN_AGE
        orphan_stmt = (
            select(Instance)
            .where(
                Instance.status == InstanceStatus.PROVISIONING.value,
                Instance.created_at <= orphan_cutoff,
            )
            .limit(_BATCH_SIZE)
            .with_for_update(skip_locked=True, of=Instance)
        )
        result = await session.execute(orphan_stmt)
        rows = list(result.scalars().all())
        for inst in rows:
            log.warning(
                "instance.orphan_recovered",
                instance_slug=inst.slug,
                age_minutes=(now - inst.created_at).total_seconds() / 60,
                dry_run=dry_run,
            )
            if dry_run:
                orphan_failed += 1
                continue
            # Mark as destroyed (with terminated_reason) instead of failed
            # because we don't know the actual failure point — could be
            # the enqueue, the worker startup, anything before the
            # provision job itself wrote a real failure_code.
            inst.status = InstanceStatus.DESTROYED.value
            inst.terminated_reason = "admin_forced"
            inst.terminated_at = now
            await session.commit()
            orphan_failed += 1

    # ── Phase 1: running → idle (TTL expired, no warning sent yet) ────
    async with async_session() as session:
        expired_stmt = (
            select(Instance)
            .where(
                Instance.status == InstanceStatus.RUNNING.value,
                Instance.expires_at <= now,
                Instance.grace_notification_at.is_(None),
            )
            .limit(_BATCH_SIZE)
            # FOR UPDATE SKIP LOCKED is a Postgres-only hint; it's a no-op
            # on SQLite (dev/test). Multiple replicas never see the same
            # row because the first replica's UPDATE bumps the status
            # before a second tick can hit it.
            # `of=Instance` scopes the row lock to instances ONLY — not
            # the LEFT-JOINed projects table that SQLAlchemy auto-adds via
            # Instance.project (lazy='joined'). Postgres rejects FOR UPDATE
            # on the nullable side of an outer join, which is why the old
            # bare `.with_for_update(skip_locked=True)` was raising
            # "FeatureNotSupportedError: FOR UPDATE cannot be applied to
            # the nullable side of an outer join" every reaper tick.
            .with_for_update(skip_locked=True, of=Instance)
        )
        result = await session.execute(expired_stmt)
        rows = list(result.scalars().all())
        for inst in rows:
            log.info(
                "instance.grace_notified",
                instance_slug=inst.slug,
                grace_expires_at=(now + grace_window).isoformat(),
                dry_run=dry_run,
            )
            if dry_run:
                notified += 1
                continue
            inst.status = InstanceStatus.IDLE.value
            inst.grace_notification_at = now
            # Commit per-row so a crash mid-batch still saves progress.
            # Safe because each row is independent.
            await session.commit()
            notified += 1
            if on_grace_notification is not None:
                try:
                    _ = on_grace_notification(inst)
                    if hasattr(_, "__await__"):
                        await _
                except Exception as e:
                    log.warning(
                        "reaper.grace_callback_failed",
                        slug=inst.slug, error=str(e),
                    )

    # ── Phase 2: idle → terminating (grace window elapsed) ────────────
    async with async_session() as session:
        grace_cutoff = now - grace_window
        terminating_stmt = (
            select(Instance)
            .where(
                Instance.status == InstanceStatus.IDLE.value,
                Instance.grace_notification_at.isnot(None),
                Instance.grace_notification_at <= grace_cutoff,
            )
            .limit(_BATCH_SIZE)
            # `of=Instance` scopes the row lock to instances ONLY — not
            # the LEFT-JOINed projects table that SQLAlchemy auto-adds via
            # Instance.project (lazy='joined'). Postgres rejects FOR UPDATE
            # on the nullable side of an outer join, which is why the old
            # bare `.with_for_update(skip_locked=True)` was raising
            # "FeatureNotSupportedError: FOR UPDATE cannot be applied to
            # the nullable side of an outer join" every reaper tick.
            .with_for_update(skip_locked=True, of=Instance)
        )
        result = await session.execute(terminating_stmt)
        rows = list(result.scalars().all())
        for inst in rows:
            log.info(
                "instance.terminating",
                instance_slug=inst.slug,
                reason="idle_24h",
                dry_run=dry_run,
            )
            if dry_run:
                terminated += 1
                continue
            inst.status = InstanceStatus.TERMINATING.value
            inst.terminated_reason = "idle_24h"
            await session.commit()
            terminated += 1
            if job_enqueuer is not None:
                try:
                    _ = job_enqueuer("teardown_instance", str(inst.id))
                    if hasattr(_, "__await__"):
                        await _
                except Exception as e:
                    log.warning(
                        "reaper.enqueue_failed",
                        slug=inst.slug, error=str(e),
                    )
            else:
                # Production default: hand off to the ARQ pool. Reaper
                # does not import ARQ at module-top so tests can run
                # without a redis pool.
                try:
                    from openclow.services.bot_actions import enqueue_job
                    await enqueue_job("teardown_instance", str(inst.id))
                except Exception as e:
                    log.warning(
                        "reaper.default_enqueue_failed",
                        slug=inst.slug, error=str(e),
                    )

    summary = {
        "notified": notified,
        "terminated": terminated,
        "orphan_failed": orphan_failed,
        "dry_run": dry_run,
    }
    log.info("reaper.sweep_complete", **summary)
    return summary


# ARQ cron entry point. Kept thin so `reap()` stays unit-testable
# without passing an arq `ctx` dict around.
async def reaper_cron(ctx: dict) -> dict:
    return await reap()
