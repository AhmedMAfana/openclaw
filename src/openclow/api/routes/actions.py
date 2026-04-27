"""Web action endpoint — handles button clicks from web chat worker messages.

When the worker sends a message with buttons (confirm_project, approve_plan, etc.),
the frontend renders them. Clicking calls POST /api/web-action with the action_id.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from openclow.api.web_auth import web_user_dep
from openclow.models.user import User
from openclow.utils.logging import get_logger

log = get_logger()

router = APIRouter(prefix="/api", tags=["web_actions"])


class WebActionRequest(BaseModel):
    action_id: str       # e.g. "confirm_project:tagh-fre"
    chat_id: str         # e.g. "web:1:21"
    message_id: str = "" # optional message to update


@router.post("/web-action")
async def web_action(body: WebActionRequest, user: User = Depends(web_user_dep)):
    """Route a button action from the web chat to the correct worker job."""
    action = body.action_id
    chat_id = body.chat_id
    message_id = body.message_id

    log.info("web_action.received", action=action, chat_id=chat_id, user_id=user.id)

    # ── confirm_project:{name} — save to DB and bootstrap ────────────────────
    if action.startswith("confirm_project:"):
        project_name = action.split(":", 1)[1]
        return await _confirm_and_bootstrap(project_name, chat_id, message_id, user)

    # ── project_relink:{id} — re-bootstrap an existing project ──────────────
    if action.startswith("project_relink:"):
        project_id = int(action.split(":", 1)[1])
        return await _bootstrap_project(project_id, chat_id, message_id)

    # ── approve_plan:{task_id} — approve a coding plan ───────────────────────
    if action.startswith("approve_plan:"):
        task_id = action.split(":", 1)[1]
        return await _enqueue("execute_plan", task_id)  # plan_review → execute_plan

    # ── reject_plan:{task_id} ────────────────────────────────────────────────
    if action.startswith("reject_plan:"):
        task_id = action.split(":", 1)[1]
        return await _enqueue("discard_task", task_id)  # plan_review → discard_task

    # ── approve_diff:{task_id} ───────────────────────────────────────────────
    if action.startswith("approve_diff:"):
        task_id = action.split(":", 1)[1]
        return await _enqueue("approve_task", task_id)

    # ── reject_diff:{task_id} ────────────────────────────────────────────────
    if action.startswith("reject_diff:"):
        task_id = action.split(":", 1)[1]
        return await _enqueue("reject_task", task_id)

    # ── create_pr:{task_id} ──────────────────────────────────────────────────
    if action.startswith("create_pr:"):
        task_id = action.split(":", 1)[1]
        return await _enqueue("merge_task", task_id)

    # ── discard_task:{task_id} ───────────────────────────────────────────────
    if action.startswith("discard_task:"):
        task_id = action.split(":", 1)[1]
        return await _enqueue("discard_task", task_id)

    raise HTTPException(400, f"Unknown action: {action}")


async def _enqueue(job_name: str, *args):
    """Enqueue a simple worker job."""
    import asyncio
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job(job_name, *args)
    return {"status": "queued", "job": job_name}


async def _bootstrap_project(project_id: int, chat_id: str, message_id: str):
    """Enqueue bootstrap for an existing project."""
    provider_type = "web" if chat_id.startswith("web:") else "telegram"
    import asyncio
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id, provider_type)
    return {"status": "queued", "job": "bootstrap_project", "project_id": project_id}


async def _confirm_and_bootstrap(project_name: str, chat_id: str, message_id: str, user: User):
    """Save pending project config to DB then immediately bootstrap it."""
    import json
    import asyncio
    import redis.asyncio as aioredis
    from openclow.settings import settings
    from openclow.models import Project, async_session
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    # Read pending config from Redis
    r = aioredis.from_url(settings.redis_url)
    pending_key = f"openclow:pending_project:{project_name}"
    try:
        data_raw = await r.get(pending_key)
        await r.delete(pending_key)
    finally:
        await r.aclose()

    if not data_raw:
        raise HTTPException(404, f"No pending config for '{project_name}'. Onboarding may have expired (1h TTL). Re-run addproject.")

    data = json.loads(data_raw)

    # Save to DB
    async with async_session() as session:
        # Check if already exists
        existing = await session.execute(
            select(Project).where(Project.name == data["name"])
        )
        existing_project = existing.scalar_one_or_none()

        if existing_project:
            project_id = existing_project.id
        else:
            project = Project(
                name=data["name"],
                github_repo=data["github_repo"],
                default_branch="main",
                tech_stack=data.get("tech_stack"),
                description=data.get("description"),
                is_dockerized=data.get("is_dockerized", True),
                docker_compose_file=data.get("docker_compose_file"),
                app_container_name=data.get("app_container_name"),
                app_port=data.get("app_port"),
                setup_commands=data.get("setup_commands"),
                status="bootstrapping",
            )
            session.add(project)
            try:
                await session.commit()
                await session.refresh(project)
                project_id = project.id
            except IntegrityError:
                await session.rollback()
                raise HTTPException(409, f"Project '{data['name']}' already exists in DB.")

    log.info("web_action.project_confirmed", project=project_name, project_id=project_id)

    # Immediately queue bootstrap
    provider_type = "web" if chat_id.startswith("web:") else "telegram"
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id, provider_type)

    return {"status": "bootstrapping", "project_id": project_id, "project": project_name}
