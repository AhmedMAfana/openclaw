"""Web chat sessions/threads endpoints — conversation list and history."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc, delete

from openclow.api.web_auth import web_user_dep
from openclow.models.user import User
from openclow.models.base import async_session
from openclow.models.web_chat import WebChatSession, WebChatMessage
from openclow.models.project import Project
from openclow.utils.logging import get_logger

log = get_logger()

router = APIRouter(prefix="/api", tags=["threads"])


# ── Response models ──────────────────────────────────────────

class MessageResponse(dict):
    """Message in thread history."""
    pass


class ThreadResponse(dict):
    """Thread/session response."""
    pass


# ── Thread list endpoint ──────────────────────────────────────

@router.get("/threads")
async def list_threads(user: User = Depends(web_user_dep)):
    """List all sessions for the user."""
    async with async_session() as session:
        result = await session.execute(
            select(WebChatSession)
            .where(WebChatSession.user_id == user.id)
            .order_by(desc(WebChatSession.last_message_at))
        )
        sessions = result.scalars().all()

    return {
        "threads": [
            {
                "remoteId": str(s.id),
                "title": s.title,
                "status": "regular",
                "projectId": s.project_id,
            }
            for s in sessions
        ]
    }


@router.post("/threads")
async def create_thread(user: User = Depends(web_user_dep)):
    """Create a new session."""
    async with async_session() as session:
        new_session = WebChatSession(
            user_id=user.id,
            title="New Chat",
            mode="quick",
        )
        session.add(new_session)
        await session.commit()
        await session.refresh(new_session)

    return {
        "remoteId": str(new_session.id),
        "externalId": None,
    }


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: int, user: User = Depends(web_user_dep)):
    """Get thread metadata."""
    async with async_session() as session:
        result = await session.get(WebChatSession, thread_id)
        if not result or result.user_id != user.id:
            raise HTTPException(404, "Thread not found")

    return {
        "remoteId": str(result.id),
        "title": result.title,
        "createdAt": result.created_at.isoformat(),
    }


@router.put("/threads/{thread_id}/rename")
async def rename_thread(thread_id: int, body: dict, user: User = Depends(web_user_dep)):
    """Rename a session."""
    async with async_session() as session:
        result = await session.get(WebChatSession, thread_id)
        if not result or result.user_id != user.id:
            raise HTTPException(404, "Thread not found")

        result.title = body.get("title", "Untitled")
        await session.commit()

    return {"status": "ok"}


@router.put("/threads/{thread_id}/project")
async def set_thread_project(thread_id: int, body: dict, user: User = Depends(web_user_dep)):
    """Assign (or clear) a project on a session."""
    async with async_session() as session:
        result = await session.get(WebChatSession, thread_id)
        if not result or result.user_id != user.id:
            raise HTTPException(404, "Thread not found")

        project_id = body.get("project_id")  # int or None
        if project_id is not None:
            proj = await session.get(Project, int(project_id))
            if not proj:
                raise HTTPException(404, "Project not found")
        result.project_id = project_id
        await session.commit()

    return {"status": "ok", "project_id": project_id}


@router.post("/threads/{thread_id}/archive")
async def archive_thread(thread_id: int, user: User = Depends(web_user_dep)):
    """Archive a session (soft delete via status)."""
    async with async_session() as session:
        result = await session.get(WebChatSession, thread_id)
        if not result or result.user_id != user.id:
            raise HTTPException(404, "Thread not found")
        # For now, just return ok. Could add a status field if needed.
        await session.delete(result)
        await session.commit()

    return {"status": "ok"}


# ── Truncate endpoint (used by edit flow) ────────────────────

@router.post("/threads/{thread_id}/truncate")
async def truncate_thread_messages(thread_id: int, body: dict, user: User = Depends(web_user_dep)):
    """Keep only the first N messages, delete the rest.

    Called before re-sending an edited message so DB history stays clean.
    Body: { "keep_count": N }
    """
    keep_count = int(body.get("keep_count", 0))
    async with async_session() as session:
        ws = await session.get(WebChatSession, thread_id)
        if not ws or ws.user_id != user.id:
            raise HTTPException(404, "Thread not found")

        # Get all message IDs ordered by created_at
        result = await session.execute(
            select(WebChatMessage.id)
            .where(WebChatMessage.session_id == thread_id)
            .order_by(WebChatMessage.created_at.asc())
        )
        all_ids = [row[0] for row in result.all()]

        # Delete everything after the first keep_count messages
        ids_to_delete = all_ids[keep_count:]
        if ids_to_delete:
            await session.execute(
                delete(WebChatMessage).where(WebChatMessage.id.in_(ids_to_delete))
            )
            await session.commit()

    return {"status": "ok", "deleted": len(ids_to_delete) if ids_to_delete else 0}


# ── History endpoint (for ThreadHistoryAdapter) ──────────────

@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(thread_id: int, user: User = Depends(web_user_dep)):
    """Load message history for a thread."""
    async with async_session() as session:
        # Verify thread belongs to user
        ws = await session.get(WebChatSession, thread_id)
        if not ws or ws.user_id != user.id:
            raise HTTPException(404, "Thread not found")

        # Load messages
        result = await session.execute(
            select(WebChatMessage)
            .where(WebChatMessage.session_id == thread_id)
            .order_by(WebChatMessage.created_at.asc())
        )
        messages = result.scalars().all()

    return {
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "createdAt": m.created_at.isoformat(),
            }
            for m in messages
        ]
    }


# ── Current user (for WebSocket URL construction) ────────────

@router.get("/me")
async def get_me(user: User = Depends(web_user_dep)):
    """Return the authenticated user's id, email, and admin flag."""
    return {"id": user.id, "email": getattr(user, "email", None), "is_admin": user.is_admin}


# ── Projects list (for project selector UI) ──────────────────

@router.get("/projects")
async def list_projects(user: User = Depends(web_user_dep)):
    """Return accessible projects for the project selector (filtered by user access)."""
    from openclow.services.access_service import get_accessible_projects_for_mcp
    accessible, _ = await get_accessible_projects_for_mcp(user.id, user.is_admin)
    # Only show active projects in the selector
    active = [p for p in accessible if p.status == "active"]
    return {
        "projects": [
            {"id": p.id, "name": p.name, "techStack": p.tech_stack}
            for p in active
        ]
    }
