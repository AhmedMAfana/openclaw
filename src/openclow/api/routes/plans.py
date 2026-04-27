"""Plan preview and management endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from pathlib import Path

from openclow.api.web_auth import web_user_dep
from openclow.models.user import User
from openclow.models.base import async_session
from openclow.models.web_chat import Plan
from openclow.services.bot_actions import enqueue_job
from openclow.utils.logging import get_logger

log = get_logger()

router = APIRouter(prefix="/api", tags=["plans"])


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: int, user: User = Depends(web_user_dep)):
    """Read plan markdown file."""
    async with async_session() as session:
        plan = await session.get(Plan, plan_id)
        if not plan or plan.user_id != user.id:
            raise HTTPException(404, "Plan not found")

    # Read file from disk
    try:
        file_path = Path(plan.file_path)
        content = file_path.read_text()
        return {
            "id": str(plan.id),
            "title": plan.title,
            "content": content,
            "status": plan.status,
        }
    except FileNotFoundError:
        raise HTTPException(404, "Plan file not found on disk")
    except Exception as e:
        log.error("plan.read_error", plan_id=plan_id, error=str(e))
        raise HTTPException(500, "Failed to read plan file")


@router.post("/plans/{plan_id}/approve")
async def approve_plan(plan_id: int, user: User = Depends(web_user_dep)):
    """Approve a plan and enqueue execution."""
    async with async_session() as session:
        plan = await session.get(Plan, plan_id)
        if not plan or plan.user_id != user.id:
            raise HTTPException(404, "Plan not found")

        plan.status = "approved"
        await session.commit()

    # Enqueue execute_plan job
    try:
        await enqueue_job(
            "execute_plan",
            plan_id=plan_id,
            user_id=user.id,
        )
    except Exception as e:
        log.error("plan.enqueue_error", plan_id=plan_id, error=str(e))
        raise HTTPException(500, "Failed to enqueue execution")

    return {"status": "approved"}


@router.post("/plans/{plan_id}/reject")
async def reject_plan(plan_id: int, user: User = Depends(web_user_dep)):
    """Reject a plan."""
    async with async_session() as session:
        plan = await session.get(Plan, plan_id)
        if not plan or plan.user_id != user.id:
            raise HTTPException(404, "Plan not found")

        plan.status = "rejected"
        await session.commit()

    return {"status": "rejected"}
