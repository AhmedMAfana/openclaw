"""Task status API endpoint."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from openclow.api.auth import verify_settings_auth
from openclow.models import Task, async_session

router = APIRouter()


@router.get("/tasks/{task_id}", dependencies=[Depends(verify_settings_auth)])
async def get_task(task_id: str):
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == tid))
        task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "id": str(task.id),
        "status": task.status,
        "description": task.description,
        "branch_name": task.branch_name,
        "pr_url": task.pr_url,
        "error_message": task.error_message,
        "agent_turns": task.agent_turns,
        "duration_seconds": task.duration_seconds,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }
