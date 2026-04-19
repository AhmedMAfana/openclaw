"""Periodic self-maintenance — the system keeps itself clean.

Runs every 10 minutes via the worker's background loop. Handles:
1. Orphaned Docker containers/volumes from failed bootstraps
2. Stale task workspaces eating disk
3. Leaked tunnel processes
4. Docker image/build cache pruning (when disk is tight)
5. Stuck tasks in intermediate states
6. Audit log rotation
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

from openclow.settings import settings
from openclow.utils.docker_path import get_docker_env
from openclow.utils.logging import get_logger

log = get_logger()

# How often the maintenance loop runs (seconds)
MAINTENANCE_INTERVAL = 600  # 10 minutes


# ─────────────────────────────────────────────────
# 1. Orphaned Docker stacks
# ─────────────────────────────────────────────────

async def cleanup_orphan_containers():
    """Find and remove Docker stacks that don't belong to active projects.

    Catches two patterns:
    - Bare project names (LLM agent ran compose without -p flag)
    - Task-ID-suffixed stacks from old code
    """
    from sqlalchemy import select as sa_select
    from openclow.models import Project, async_session

    try:
        # Get known project names from DB
        async with async_session() as session:
            result = await session.execute(sa_select(Project.name))
            known_projects = {row[0] for row in result.all()}

        # Legitimate compose project names
        legitimate = {"openclow"} | {f"openclow-{name}" for name in known_projects}

        _denv = get_docker_env()
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
             "--format", "{{.Label \"com.docker.compose.project\"}}"],
            capture_output=True, text=True, timeout=10, env=_denv,
        )

        orphan_stacks = set()
        for line in result.stdout.strip().split("\n"):
            proj = line.strip()
            if not proj or proj in legitimate:
                continue
            # openclow-{name}-{extra} = task-suffixed orphan
            if proj.startswith("openclow-"):
                orphan_stacks.add(proj)
            # Bare project name = agent forgot -p flag
            elif proj in known_projects:
                orphan_stacks.add(proj)

        removed = 0
        for orphan in orphan_stacks:
            log.info("maintenance.removing_orphan_stack", project=orphan)
            subprocess.run(
                ["docker", "compose", "-p", orphan, "down", "--remove-orphans"],
                capture_output=True, timeout=60, env=_denv,
            )
            removed += 1

        if removed:
            log.info("maintenance.orphan_stacks_cleaned", count=removed)

        # Prune dangling volumes — these accumulate from stopped compose stacks
        # `docker volume prune -f` only removes volumes not attached to any container (safe)
        try:
            vol_result = subprocess.run(
                ["docker", "volume", "prune", "-f"],
                capture_output=True, text=True, timeout=30, env=_denv,
            )
            if "Total reclaimed space" in vol_result.stdout and "0B" not in vol_result.stdout:
                log.info("maintenance.volumes_pruned", output=vol_result.stdout.strip())
        except Exception as e:
            log.warning("maintenance.volume_prune_failed", error=str(e))

    except Exception as e:
        log.warning("maintenance.orphan_cleanup_failed", error=str(e))


# ─────────────────────────────────────────────────
# 2. Stale task workspaces
# ─────────────────────────────────────────────────

async def cleanup_stale_workspaces(max_age_hours: int = 12):
    """Remove task-* workspace directories older than max_age_hours.

    Keeps _cache (project repos) and only removes task working copies.
    Also removes associated Docker stacks before deleting.
    """
    base = settings.workspace_base_path
    if not os.path.exists(base):
        return

    now = time.time()
    removed = 0

    for entry in os.listdir(base):
        if not entry.startswith("task-"):
            continue
        path = os.path.join(base, entry)
        if not os.path.isdir(path):
            continue

        age_hours = (now - os.path.getmtime(path)) / 3600
        if age_hours < max_age_hours:
            continue

        # Check if this workspace has an active task
        task_id = entry.replace("task-", "")
        if await _is_task_active(task_id):
            continue

        try:
            # Stop any Docker stack running from this workspace
            compose_project = f"openclow-task-{task_id}"
            subprocess.run(
                ["docker", "compose", "-p", compose_project, "down", "--remove-orphans"],
                capture_output=True, timeout=30, env=get_docker_env(),
            )

            # Remove git worktree reference first
            cache_path = os.path.join(base, "_cache")
            if os.path.exists(cache_path):
                for cache_dir in os.listdir(cache_path):
                    cache_full = os.path.join(cache_path, cache_dir)
                    if os.path.isdir(cache_full):
                        subprocess.run(
                            ["git", "-C", cache_full, "worktree", "remove", "--force", path],
                            capture_output=True, timeout=10,
                        )

            # Delete the directory
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            log.info("maintenance.workspace_cleaned", path=entry, age_hours=round(age_hours, 1))
        except Exception as e:
            log.warning("maintenance.workspace_cleanup_failed", path=entry, error=str(e))

    if removed:
        log.info("maintenance.stale_workspaces_cleaned", count=removed)


async def _is_task_active(task_id_prefix: str) -> bool:
    """Check if any task matching this ID prefix is still active."""
    from sqlalchemy import text
    from openclow.models import async_session

    active_statuses = ("pending", "approved", "coding", "reviewing", "merging",
                       "planning", "preparing", "pushing", "diff_preview")
    try:
        # task-{hash} uses first 8 chars of UUID
        async with async_session() as session:
            result = await session.execute(
                text("SELECT 1 FROM tasks WHERE CAST(id AS TEXT) LIKE :prefix AND status = ANY(:statuses) LIMIT 1"),
                {"prefix": f"{task_id_prefix}%", "statuses": list(active_statuses)},
            )
            return result.first() is not None
    except Exception:
        return True  # If we can't check, assume active (don't delete)


# ─────────────────────────────────────────────────
# 3. Docker disk usage management
# ─────────────────────────────────────────────────

def _docker_daemon_uptime_seconds() -> float | None:
    """Return how many seconds ago Docker daemon started. None if unknown."""
    try:
        _denv = get_docker_env()
        result = subprocess.run(
            # ServerVersion is always present; Swarm.Error shows restart reason
            ["docker", "info", "--format",
             "{{.ContainersRunning}} {{.ContainersStopped}} {{.ContainersPaused}}"],
            capture_output=True, text=True, timeout=5, env=_denv,
        )
        if result.returncode != 0:
            return None
        # Docker exposes the daemon start time via /proc on Linux, not via 'docker info'.
        # Best proxy: check if any openclow containers were running very recently.
        # We rely on a separate approach: check 'docker system events' for daemon start.
        events = subprocess.run(
            ["docker", "system", "events", "--since", "3m", "--until", "now",
             "--filter", "type=daemon", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5, env=_denv,
        )
        lines = [l.strip() for l in events.stdout.strip().split("\n") if l.strip()]
        # If any daemon 'reload' or 'start' event in last 3 minutes → Docker just restarted
        if any(s in ("reload", "start") for s in lines):
            return 0.0  # Treat as "just restarted" (< DOCKER_RESTART_GRACE_SECONDS)
        return None
    except Exception:
        return None


# Don't prune containers if Docker daemon (re)started less than this long ago.
# After Docker Desktop restart, containers are stopped but auto-recover with unless-stopped.
# Pruning them before recovery is the root cause of the health-check → rebuild loop.
DOCKER_RESTART_GRACE_SECONDS = 300  # 5 minutes


async def prune_docker_if_needed(threshold_gb: float = 20.0):
    """Prune dangling images and build cache if Docker disk usage exceeds threshold.

    Runs proactively so builds never run out of space mid-run.
    Never removes named images, volumes, or running containers.

    Key safety rules:
    - Never prune containers stopped less than 2 hours ago (unless=stopped auto-recovery)
    - Skip container prune entirely if Docker daemon just restarted (within 5 min)
    - Never prune named/tagged images — only <none>:<none> dangling layers
    """
    try:
        _denv = get_docker_env()
        result = subprocess.run(
            ["docker", "system", "df", "--format", "{{.Size}}"],
            capture_output=True, text=True, timeout=10, env=_denv,
        )
        # Parse total size — Docker outputs like "38.26GB" or "411.1MB"
        lines = result.stdout.strip().split("\n")
        total_gb = 0.0
        for line in lines:
            line = line.strip()
            if "GB" in line:
                try:
                    total_gb += float(line.replace("GB", "").strip())
                except ValueError:
                    pass
            elif "MB" in line:
                try:
                    total_gb += float(line.replace("MB", "").strip()) / 1024
                except ValueError:
                    pass

        log.debug("maintenance.docker_disk_usage", usage_gb=round(total_gb, 1), threshold_gb=threshold_gb)

        if total_gb < threshold_gb:
            return

        log.info("maintenance.docker_prune_starting", usage_gb=round(total_gb, 1), threshold_gb=threshold_gb)

        # Check if Docker daemon just restarted — if so, skip container prune entirely.
        # After a Docker Desktop restart, project containers are in "Exited" state but
        # will auto-recover via unless-stopped. Pruning them now would destroy the
        # container instances Docker is trying to restart, causing a 20-30 min rebuild.
        daemon_uptime = _docker_daemon_uptime_seconds()
        docker_just_restarted = daemon_uptime is not None and daemon_uptime < DOCKER_RESTART_GRACE_SECONDS

        if docker_just_restarted:
            log.info("maintenance.docker_prune_skipped_restart_grace",
                     reason="Docker daemon restarted recently — skipping container prune to allow auto-recovery")
        else:
            # Only prune containers stopped for more than 2 hours.
            # The --filter "until=2h" ensures containers that just stopped
            # (Docker restart, manual stop) are NOT removed — they recover on their own.
            subprocess.run(
                ["docker", "container", "prune", "-f", "--filter", "until=2h"],
                capture_output=True, timeout=30, env=_denv,
            )

        # Remove dangling images (safe — only untagged/unreferenced <none>:<none> layers)
        # Named images like sail-8.4/app:latest are NEVER removed by this.
        subprocess.run(
            ["docker", "image", "prune", "-f"],
            capture_output=True, timeout=60, env=_denv,
        )

        # Remove build cache older than 1h (aggressive — saves disk proactively)
        subprocess.run(
            ["docker", "builder", "prune", "-f", "--filter", "until=1h"],
            capture_output=True, timeout=120, env=_denv,
        )

        log.info("maintenance.docker_pruned", skipped_container_prune=docker_just_restarted)
    except Exception as e:
        log.warning("maintenance.docker_prune_failed", error=str(e))


# ─────────────────────────────────────────────────
# 4. Stuck tasks recovery
# ─────────────────────────────────────────────────

async def _finalize_web_progress_cards(stuck_rows, error_msg: str):
    """Push final failed status to web progress cards for stuck tasks.

    Without this, the progress card freezes at whatever step it was on
    (e.g. "implement: running") instead of showing red/failed.
    """
    import json
    import redis.asyncio as aioredis
    from openclow.models.base import async_session
    from openclow.models.web_chat import WebChatMessage
    from openclow.models import Task
    from sqlalchemy import select as sa_select

    try:
        # Get full task objects with chat_id and chat_message_id
        task_ids = [row[0] for row in stuck_rows]
        async with async_session() as session:
            result = await session.execute(
                sa_select(Task.id, Task.chat_id, Task.chat_message_id)
                .where(Task.id.in_(task_ids))
            )
            tasks = result.all()

        r = aioredis.from_url(settings.redis_url)
        try:
            for task_id, chat_id, message_id in tasks:
                if not chat_id or not chat_id.startswith("web:") or not message_id:
                    continue
                try:
                    parts = chat_id.split(":")
                    if len(parts) < 3:
                        continue
                    user_id, session_id = parts[1], parts[2]

                    # Load the progress card from DB
                    async with async_session() as db:
                        msg = await db.get(WebChatMessage, int(message_id))
                        if not msg or not msg.content.startswith("__PROGRESS_CARD__"):
                            continue
                        card = json.loads(msg.content[len("__PROGRESS_CARD__"):])

                        # Mark overall status as failed
                        card["overall_status"] = "failed"
                        card["footer"] = f"{error_msg}. You can retry."
                        # Mark any running step as failed
                        for step in card.get("steps", []):
                            if step.get("status") == "running":
                                step["status"] = "failed"
                                step["detail"] = "stalled"

                        # Persist updated card
                        card_to_store = dict(card)
                        if "session_id" not in card_to_store:
                            card_to_store["session_id"] = session_id
                        msg.content = f"__PROGRESS_CARD__{json.dumps(card_to_store)}"
                        msg.is_complete = True
                        await db.commit()

                    # Push to WebSocket so connected browsers update immediately
                    channel = f"wc:{user_id}:{session_id}"
                    await r.publish(channel, json.dumps({
                        "type": "progress_card",
                        "message_id": message_id,
                        "card": card,
                    }))
                    log.info("maintenance.progress_card_finalized",
                             task_id=str(task_id), message_id=message_id)
                except Exception as e:
                    log.warning("maintenance.progress_card_finalize_failed",
                                task_id=str(task_id), error=str(e))
        finally:
            await r.aclose()
    except Exception as e:
        log.warning("maintenance.progress_cards_batch_failed", error=str(e))


async def recover_stuck_tasks(max_stuck_minutes: int = 30):
    """Mark tasks stuck in intermediate states as failed, and release their project locks.

    This runs periodically (not just on startup) to catch tasks
    that got stuck after the worker was already running.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select as sa_select, update as sa_update
    from openclow.models import Task, async_session
    from openclow.services.project_lock import force_release

    stuck_statuses = ["coding", "reviewing", "preparing", "planning",
                      "pushing", "diff_preview", "code_review_in_progress"]

    try:
        cutoff = datetime.utcnow() - timedelta(minutes=max_stuck_minutes)

        # Step 1: Find stuck tasks — include project_id so we can release locks
        async with async_session() as session:
            result = await session.execute(
                sa_select(Task.id, Task.status, Task.updated_at, Task.project_id).where(
                    Task.status.in_(stuck_statuses),
                    Task.updated_at < cutoff,
                )
            )
            stuck = result.all()

        if not stuck:
            return

        # Step 2: Bulk update status to failed
        stuck_ids = [row[0] for row in stuck]
        async with async_session() as session:
            await session.execute(
                sa_update(Task)
                .where(Task.id.in_(stuck_ids))
                .values(status="failed", error_message="Task stuck — auto-recovered by maintenance")
            )
            await session.commit()

        # Step 3: Release project locks for all affected projects
        released_projects = set()
        for row in stuck:
            age = round((datetime.utcnow() - row[2]).total_seconds() / 60)
            log.warning("maintenance.stuck_task_recovered",
                        task_id=str(row[0]), old_status=row[1], age_minutes=age,
                        project_id=row[3])
            if row[3] and row[3] not in released_projects:
                await force_release(row[3])
                released_projects.add(row[3])

        # Step 4: Finalize web progress cards so the UI shows red/failed
        await _finalize_web_progress_cards(stuck, "Task stuck — auto-recovered by maintenance")

        log.info("maintenance.stuck_tasks_recovered", count=len(stuck),
                 locks_released=len(released_projects))
    except Exception as e:
        log.warning("maintenance.stuck_recovery_failed", error=str(e))


# ─────────────────────────────────────────────────
# 5. Audit log trimming
# ─────────────────────────────────────────────────

async def trim_audit_logs(keep_days: int = 7):
    """Delete audit log entries older than keep_days."""
    from datetime import datetime, timedelta
    from sqlalchemy import delete as sa_delete
    from openclow.models import AuditLog, async_session

    try:
        cutoff = datetime.utcnow() - timedelta(days=keep_days)
        async with async_session() as session:
            result = await session.execute(
                sa_delete(AuditLog).where(AuditLog.created_at < cutoff)
            )
            deleted = result.rowcount
            await session.commit()

            if deleted:
                log.info("maintenance.audit_trimmed", deleted=deleted, keep_days=keep_days)
    except Exception as e:
        log.warning("maintenance.audit_trim_failed", error=str(e))


# ─────────────────────────────────────────────────
# 6. Activity log rotation
# ─────────────────────────────────────────────────

def rotate_activity_log(max_size_mb: float = 50.0):
    """Rotate activity log if it exceeds max_size_mb.

    Keeps the latest half, discards the oldest half.
    """
    log_path = settings.activity_log
    if not os.path.exists(log_path):
        return

    try:
        size_mb = os.path.getsize(log_path) / (1024 * 1024)
        if size_mb < max_size_mb:
            return

        # Keep latest half of lines
        with open(log_path) as f:
            lines = f.readlines()

        keep = lines[len(lines) // 2:]
        with open(log_path, "w") as f:
            f.writelines(keep)

        log.info("maintenance.activity_log_rotated",
                 old_mb=round(size_mb, 1),
                 kept_lines=len(keep))
    except Exception as e:
        log.warning("maintenance.activity_rotation_failed", error=str(e))


# ─────────────────────────────────────────────────
# 7. Orphaned project lock cleanup
# ─────────────────────────────────────────────────

async def release_orphan_locks(max_stuck_minutes: int = 40):
    """Force-release project locks that have been held too long.

    Strategy: check Redis lock TTL directly. The lock is set with a 3900s TTL.
    If remaining TTL < (3900 - max_stuck_minutes*60), the lock has been held
    for at least max_stuck_minutes → orphan → force-release it.

    Also marks stuck 'bootstrapping' projects as 'failed'.
    Uses lock TTL instead of Project.updated_at (which doesn't exist on the model).
    """
    import redis.asyncio as aioredis
    from sqlalchemy import select as sa_select, update as sa_update
    from openclow.models import Project, async_session
    from openclow.services.project_lock import force_release
    from openclow.settings import settings

    try:
        # Find all projects currently in bootstrapping state
        async with async_session() as session:
            result = await session.execute(
                sa_select(Project.id, Project.name).where(
                    Project.status == "bootstrapping",
                )
            )
            bootstrapping = result.all()

        if not bootstrapping:
            return

        # Check each lock's remaining TTL — if held for >max_stuck_minutes, it's an orphan
        _DEFAULT_TTL = 3900
        threshold_remaining = _DEFAULT_TTL - (max_stuck_minutes * 60)

        r = aioredis.from_url(settings.redis_url)
        stuck = []
        try:
            for project_id, project_name in bootstrapping:
                key = f"openclow:project_lock:{project_id}"
                ttl = await r.ttl(key)
                # ttl == -2: key gone (lock already released — status stuck at bootstrapping)
                # ttl < threshold_remaining: lock held for > max_stuck_minutes
                if ttl == -2 or ttl < threshold_remaining:
                    age_min = round((_DEFAULT_TTL - ttl) / 60) if ttl >= 0 else "unknown"
                    stuck.append((project_id, project_name, age_min))
        finally:
            await r.aclose()

        if not stuck:
            return

        stuck_ids = [row[0] for row in stuck]
        async with async_session() as session:
            await session.execute(
                sa_update(Project)
                .where(Project.id.in_(stuck_ids))
                .values(status="failed")
            )
            await session.commit()

        for project_id, project_name, age_min in stuck:
            log.warning("maintenance.orphan_lock_released",
                        project_id=project_id, project_name=project_name, age_minutes=age_min)
            await force_release(project_id)

        log.info("maintenance.orphan_locks_released", count=len(stuck))
    except Exception as e:
        log.warning("maintenance.orphan_lock_cleanup_failed", error=str(e))


# ─────────────────────────────────────────────────
# Main maintenance loop
# ─────────────────────────────────────────────────

async def maintenance_loop():
    """Background loop — runs all maintenance tasks periodically.

    Called from arq_app.py on_startup as a background asyncio task.
    Runs every MAINTENANCE_INTERVAL seconds.
    """
    # Wait before first run — let the system settle after startup
    await asyncio.sleep(60)

    while True:
        try:
            log.debug("maintenance.cycle_start")
            t0 = time.time()

            await cleanup_orphan_containers()
            await cleanup_stale_workspaces()
            await prune_docker_if_needed()
            await recover_stuck_tasks()
            await release_orphan_locks()
            await trim_audit_logs()
            rotate_activity_log()

            elapsed = round(time.time() - t0, 1)
            log.info("maintenance.cycle_done", elapsed_s=elapsed)
        except Exception as e:
            log.error("maintenance.cycle_failed", error=str(e))

        await asyncio.sleep(MAINTENANCE_INTERVAL)
