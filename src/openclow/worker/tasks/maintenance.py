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

        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
             "--format", "{{.Label \"com.docker.compose.project\"}}"],
            capture_output=True, text=True, timeout=10,
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
                capture_output=True, timeout=60,
            )
            removed += 1

        if removed:
            log.info("maintenance.orphan_stacks_cleaned", count=removed)
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
                capture_output=True, timeout=30,
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

async def prune_docker_if_needed(threshold_gb: float = 30.0):
    """Prune dangling images and build cache if Docker disk usage exceeds threshold.

    Only removes dangling (untagged) images and old build cache.
    Never removes named images, volumes, or running containers.
    """
    try:
        result = subprocess.run(
            ["docker", "system", "df", "--format", "{{.Size}}"],
            capture_output=True, text=True, timeout=10,
        )
        # Parse total size — Docker outputs like "38.26GB" or "411.1MB"
        lines = result.stdout.strip().split("\n")
        total_gb = 0.0
        for line in lines:
            line = line.strip()
            if "GB" in line:
                total_gb += float(line.replace("GB", ""))
            elif "MB" in line:
                total_gb += float(line.replace("MB", "")) / 1024

        if total_gb < threshold_gb:
            return

        log.info("maintenance.docker_prune_starting", usage_gb=round(total_gb, 1), threshold_gb=threshold_gb)

        # Remove dangling images (safe — only untagged/unreferenced)
        subprocess.run(
            ["docker", "image", "prune", "-f"],
            capture_output=True, timeout=60,
        )

        # Remove build cache older than 24h
        subprocess.run(
            ["docker", "builder", "prune", "-f", "--filter", "until=24h"],
            capture_output=True, timeout=120,
        )

        log.info("maintenance.docker_pruned")
    except Exception as e:
        log.warning("maintenance.docker_prune_failed", error=str(e))


# ─────────────────────────────────────────────────
# 4. Stuck tasks recovery
# ─────────────────────────────────────────────────

async def recover_stuck_tasks(max_stuck_minutes: int = 30):
    """Mark tasks stuck in intermediate states as failed.

    This runs periodically (not just on startup) to catch tasks
    that got stuck after the worker was already running.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select as sa_select, update as sa_update
    from openclow.models import Task, async_session

    stuck_statuses = ["coding", "reviewing", "preparing", "planning",
                      "pushing", "diff_preview", "code_review_in_progress"]

    try:
        cutoff = datetime.utcnow() - timedelta(minutes=max_stuck_minutes)

        # Step 1: Find stuck task IDs (lightweight query)
        async with async_session() as session:
            result = await session.execute(
                sa_select(Task.id, Task.status, Task.updated_at).where(
                    Task.status.in_(stuck_statuses),
                    Task.updated_at < cutoff,
                )
            )
            stuck = result.all()

        if not stuck:
            return

        # Step 2: Bulk update in a separate session (avoids greenlet issue)
        stuck_ids = [row[0] for row in stuck]
        async with async_session() as session:
            await session.execute(
                sa_update(Task)
                .where(Task.id.in_(stuck_ids))
                .values(status="failed", error_message="Task stuck — auto-recovered by maintenance")
            )
            await session.commit()

        for row in stuck:
            age = round((datetime.utcnow() - row[2]).total_seconds() / 60)
            log.warning("maintenance.stuck_task_recovered",
                        task_id=str(row[0]), old_status=row[1], age_minutes=age)

        log.info("maintenance.stuck_tasks_recovered", count=len(stuck))
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
            await trim_audit_logs()
            rotate_activity_log()

            elapsed = round(time.time() - t0, 1)
            log.info("maintenance.cycle_done", elapsed_s=elapsed)
        except Exception as e:
            log.error("maintenance.cycle_failed", error=str(e))

        await asyncio.sleep(MAINTENANCE_INTERVAL)
