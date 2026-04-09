"""Shared business logic for bot handlers — platform-agnostic.

Every chat platform (Telegram, Slack, Discord) calls these functions.
They handle DB queries, job enqueuing, and validation.
The platform-specific handler is responsible for UI rendering.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select, update

from openclow.models import Project, Task, User, async_session
from openclow.services import project_service
from openclow.utils.logging import get_logger

log = get_logger()


# ── Job enqueuing ────────────────────────────────────────────────


async def enqueue_job(job_name: str, *args: Any, timeout: float = 5.0) -> Any:
    """Enqueue an arq job. Returns the job object. Raises on failure."""
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=timeout)
    return await pool.enqueue_job(job_name, *args)


# ── Task queries ─────────────────────────────────────────────────


async def get_active_tasks(chat_id: str, limit: int = 5) -> list[Task]:
    """Get active tasks for a chat."""
    active_statuses = [
        "pending", "preparing", "planning", "coding", "reviewing",
        "diff_preview", "awaiting_approval", "pushing",
    ]
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.chat_id == chat_id)
            .where(Task.status.in_(active_statuses))
            .order_by(Task.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_task_status(task_id: str) -> str | None:
    """Read current task status from DB. Returns None if not found."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Task.status).where(Task.id == uuid.UUID(task_id))
            )
            row = result.one_or_none()
            return row[0] if row else None
    except Exception:
        return None


# Expected status for each review action
_EXPECTED_STATUS = {
    "approve_plan": "plan_review",
    "approve": "diff_preview",
    "discard": "diff_preview",
    "merge": "awaiting_approval",
    "reject": "awaiting_approval",
}

# Job name for each review action
_JOB_NAMES = {
    "approve_plan": "execute_plan",
    "approve": "approve_task",
    "discard": "discard_task",
    "merge": "merge_task",
    "reject": "reject_task",
}


async def review_guard(action: str, task_id: str) -> tuple[bool, str]:
    """Validate task status before enqueueing a review action.

    Returns (ok, error_message). If ok is True, the job has been enqueued.
    """
    expected = _EXPECTED_STATUS.get(action)
    current_status = await get_task_status(task_id)

    if expected and current_status != expected:
        return False, f"Task is already being processed ({current_status or 'unknown'})"

    job_name = _JOB_NAMES.get(action)
    if not job_name:
        return False, f"Unknown action: {action}"

    try:
        await enqueue_job(job_name, task_id)
        return True, ""
    except Exception as e:
        return False, str(e)


# ── Task creation ────────────────────────────────────────────────


async def create_task(
    user_id: int,
    project_id: int,
    description: str,
    chat_id: str,
    chat_provider_type: str = "telegram",
) -> Task:
    """Create a new task in the DB."""
    task_id = uuid.uuid4()
    async with async_session() as session:
        task = Task(
            id=task_id,
            user_id=user_id,
            project_id=project_id,
            description=description,
            status="pending",
            chat_id=chat_id,
            chat_provider_type=chat_provider_type,
        )
        session.add(task)
        await session.commit()
    return task


async def update_task_message(task_id: uuid.UUID, message_id: str, job_id: str):
    """Save the status message ID and arq job ID to the task."""
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one()
        task.chat_message_id = message_id
        task.arq_job_id = job_id
        await session.commit()


async def cancel_latest_task(chat_id: str) -> Task | None:
    """Cancel the latest cancellable task. Returns the task or None."""
    cancellable = ["pending", "preparing", "coding", "reviewing"]
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.chat_id == chat_id)
            .where(Task.status.in_(cancellable))
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()

    if not task:
        return None

    # Abort the arq job
    if task.arq_job_id:
        try:
            from openclow.worker.arq_app import get_arq_pool
            pool = await get_arq_pool()
            await pool.abort_job(task.arq_job_id)
        except Exception as e:
            log.warning("cancel.abort_failed", error=str(e))

    async with async_session() as session:
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(status="failed", error_message="Cancelled by user")
        )
        await session.commit()

    return task


# ── Project queries ──────────────────────────────────────────────


async def get_all_projects() -> list[Project]:
    """Get all projects."""
    return await project_service.get_all_projects()


async def get_project_by_id(project_id: int) -> Project | None:
    """Get a project by ID."""
    return await project_service.get_project_by_id(project_id)


# ── URL lookups ──────────────────────────────────────────────────


async def get_dashboard_url(service: str = "dozzle") -> str | None:
    """Get the tunnel URL for the dashboard (dozzle) service."""
    from openclow.services.tunnel_service import get_tunnel_url
    return await get_tunnel_url(service)


async def get_settings_url() -> str | None:
    """Get the settings dashboard URL."""
    from openclow.services.tunnel_service import get_tunnel_url
    url = await get_tunnel_url("settings")
    return f"{url}/settings" if url else None


# ── GitHub repo fetching ─────────────────────────────────────────


async def fetch_github_repos() -> list[dict]:
    """Fetch GitHub repos — tries MCP worker first, falls back to direct API."""

    # 1. Try MCP/worker path
    try:
        job = await enqueue_job("list_github_repos")
        repos_data = await job.result(timeout=15)
        if repos_data:
            log.info("github.repos_fetched_via_worker", count=len(repos_data))
            return repos_data
    except Exception as e:
        log.warning("github.worker_fetch_failed", error=str(e))

    # 2. Fallback: direct GitHub API
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if not config or not config.get("token"):
            log.error("github.no_token_configured")
            return []

        token = config["token"]
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={
                    "per_page": 30,
                    "sort": "updated",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            if resp.status_code != 200:
                log.error("github.api_failed", status=resp.status_code, body=resp.text[:200])
                return []

            repos = resp.json()
            log.info("github.repos_fetched_via_api", count=len(repos))
            return [
                {"name": r.get("full_name", ""), "desc": r.get("description", "") or ""}
                for r in repos
            ]
    except Exception as e:
        log.error("github.fetch_repos_failed", error=str(e))
        return []


# ── User lookup ──────────────────────────────────────────────────


async def lookup_user(provider_type: str, provider_uid: str) -> User | None:
    """Look up a user by provider type and UID."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(
                User.chat_provider_type == provider_type,
                User.chat_provider_uid == provider_uid,
            )
        )
        return result.scalar_one_or_none()
