"""Web chat sessions/threads endpoints — conversation list and history."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc
from datetime import datetime

from openclow.api.web_auth import web_user_dep
from openclow.models.user import User
from openclow.models.base import async_session
from openclow.models.web_chat import WebChatSession, WebChatMessage
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
