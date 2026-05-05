"""Web chat sessions/threads endpoints — conversation list and history."""
from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timezone

import redis.asyncio as aioredis
import sqlalchemy as _sa
from arq import create_pool
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select, desc, delete


def _utc_iso(dt: datetime | None) -> str | None:
    """Serialize a naive UTC datetime as an ISO string with `Z` suffix.

    The `web_chat_sessions` / `web_chat_messages` tables use Postgres
    `timestamp without time zone` columns; SQLAlchemy returns naive
    datetimes. `dt.isoformat()` on those produces e.g.
    `2026-04-26T15:24:26.969811` with no offset — browsers parse that
    string as LOCAL time (so a UTC+8 user sees timestamps 8 hours in
    the past). Forcing the offset on the wire makes the parse correct
    on every client.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

from taghdev.api.web_auth import web_user_dep
from taghdev.models.user import User
from taghdev.models.base import async_session
from taghdev.models.web_chat import WebChatSession, WebChatMessage
from taghdev.settings import settings
from taghdev.worker.arq_app import get_arq_pool, parse_redis_url
from taghdev.utils.logging import get_logger

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
                "gitMode": s.git_mode,
                "lastMessageAt": _utc_iso(s.last_message_at) or _utc_iso(s.created_at),
            }
            for s in sessions
        ]
    }


@router.post("/threads")
async def create_thread(
    user: User = Depends(web_user_dep),
    body: dict | None = Body(default=None),
):
    """Create a new session.

    Optional ``project_id`` in the request body binds the thread to a
    project atomically at creation time (Change 4 of senior-DevOps
    refactor). Without this, a fresh thread is born with
    ``project_id=NULL`` and the picker writes it via a follow-up PUT —
    which races a fast first message: the LLM sees a no-project chat
    and falls back to the kiosk-bullet path. Atomic binding closes the
    race.
    """
    project_id: int | None = None
    if isinstance(body, dict):
        raw = body.get("project_id")
        if isinstance(raw, int):
            project_id = raw
        elif isinstance(raw, str) and raw.isdigit():
            project_id = int(raw)
    async with async_session() as session:
        new_session = WebChatSession(
            user_id=user.id,
            title="New Chat",
            mode="quick",
            project_id=project_id,
        )
        session.add(new_session)
        await session.commit()
        await session.refresh(new_session)

    return {
        "remoteId": str(new_session.id),
        "externalId": None,
        "projectId": new_session.project_id,
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
        "gitMode": result.git_mode,
        "createdAt": _utc_iso(result.created_at),
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


@router.patch("/threads/{thread_id}/git-mode")
async def set_thread_git_mode(thread_id: int, body: dict, user: User = Depends(web_user_dep)):
    """Update the git mode for a session."""
    valid_modes = {"branch_per_task", "direct_commit", "session_branch"}
    new_mode = body.get("git_mode", "session_branch")
    if new_mode not in valid_modes:
        raise HTTPException(400, f"Invalid git_mode. Must be one of: {', '.join(valid_modes)}")

    async with async_session() as session:
        ws = await session.get(WebChatSession, thread_id)
        if not ws or ws.user_id != user.id:
            raise HTTPException(404, "Thread not found")
        ws.git_mode = new_mode
        await session.commit()

    return {"status": "ok", "git_mode": new_mode}


@router.put("/threads/{thread_id}/project")
async def set_thread_project(
    thread_id: int, body: dict, user: User = Depends(web_user_dep)
):
    """Bind (or unbind) a project to a chat session.

    Frontend's project picker calls this on selection change. Before
    this endpoint existed the call silently 404'd inside an empty
    try/catch — caught by `pipeline-fitness::api_route_contract`.

    Body: ``{"project_id": <int|null>}``. ``null`` clears the binding.
    Returns ``{"status": "ok", "project_id": <id>}``.
    """
    raw = body.get("project_id")
    project_id = int(raw) if raw is not None else None

    async with async_session() as session:
        ws = await session.get(WebChatSession, thread_id)
        if not ws or ws.user_id != user.id:
            raise HTTPException(404, "Thread not found")
        ws.project_id = project_id
        await session.commit()

    return {"status": "ok", "project_id": project_id}


@router.post("/threads/{thread_id}/archive")
async def archive_thread(thread_id: int, user: User = Depends(web_user_dep)):
    """Archive a session — T086 full-cascade delete.

    Ownership check first; then delegate to
    ``chat_session_service.delete_chat_cascade`` which also tears down
    the chat's active instance (if any), cleans audit rows keyed by
    slug, and enqueues a session-branch GC job.
    """
    async with async_session() as session:
        result = await session.get(WebChatSession, thread_id)
        if not result or result.user_id != user.id:
            raise HTTPException(404, "Thread not found")

    from taghdev.services.chat_session_service import delete_chat_cascade
    summary = await delete_chat_cascade(thread_id)
    return {"status": "ok", **summary}


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

        result = await session.execute(
            select(WebChatMessage.id)
            .where(WebChatMessage.session_id == thread_id)
            .order_by(WebChatMessage.created_at.asc())
        )
        all_ids = [row[0] for row in result.all()]

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
        ws = await session.get(WebChatSession, thread_id)
        if not ws or ws.user_id != user.id:
            raise HTTPException(404, "Thread not found")

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
                "createdAt": _utc_iso(m.created_at),
                "isComplete": m.is_complete,
            }
            for m in messages
        ]
    }


# ── Current user (for WebSocket URL construction) ────────────

@router.get("/me")
async def get_me(user: User = Depends(web_user_dep)):
    """Return the authenticated user's profile."""
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "has_git_token": bool(getattr(user, "git_token", None)),
    }


@router.put("/me/git-token")
async def set_my_git_token(body: dict, user: User = Depends(web_user_dep)):
    """Save the current user's personal GitHub PAT."""
    from taghdev.models.base import async_session
    from sqlalchemy import select as sa_select
    token = (body.get("git_token") or "").strip()
    async with async_session() as session:
        db_user = await session.get(user.__class__, user.id)
        if not db_user:
            raise HTTPException(404, "User not found")
        db_user.git_token = token or None
        await session.commit()
    return {"status": "ok", "has_git_token": bool(token)}


@router.post("/me/test-git-token")
async def test_my_git_token(user: User = Depends(web_user_dep)):
    """Test the current user's personal GitHub PAT against the API."""
    import urllib.request, json as _json
    token = getattr(user, "git_token", None)
    if not token:
        raise HTTPException(400, "No personal git token saved")
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())
            login = data.get("login", "?")
            return {"status": "ok", "message": f"Authenticated as @{login}"}
    except Exception as e:
        raise HTTPException(400, f"Token invalid: {e}")


# ── Projects list (for project selector UI) ──────────────────

@router.get("/projects")
async def list_projects(user: User = Depends(web_user_dep)):
    """Return accessible projects for the project selector (filtered by user access)."""
    from taghdev.services.access_service import get_accessible_projects_for_mcp
    accessible, _ = await get_accessible_projects_for_mcp(user.id, user.is_admin)
    return {
        "projects": [
            {"id": p.id, "name": p.name, "techStack": p.tech_stack, "status": p.status}
            for p in accessible
        ]
    }


# ── Cancel service — extracted from handler for clarity + proper cleanup ──────

async def _cancel_session(session_id: int, user_id: int) -> int:
    """Cancel the most recent running task. Returns number of aborted jobs.

    Uses a single Redis connection (via async-with) to prevent leaks.
    """
    chat_id = f"web:{user_id}:{session_id}"
    key = f"taghdev:session_jobs:{chat_id}"
    aborted = 0

    # 1. Fetch job IDs + set cancel flag in one connection
    async with aioredis.from_url(settings.redis_url) as r:
        job_ids = await r.lrange(key, 0, -1)
        await r.set(f"taghdev:cancel_session:{session_id}", "1", ex=600)
        await r.publish(f"taghdev:cancel:{session_id}", "cancel")

    # 2. Mark the most recent running progress card as cancelled
    updated_card: tuple[int, dict] | None = None
    try:
        async with async_session() as db:
            import datetime as _dt
            # Primary: find the most recent incomplete assistant message
            result = await db.execute(
                _sa.select(WebChatMessage).where(
                    WebChatMessage.session_id == session_id,
                    WebChatMessage.role == "assistant",
                    WebChatMessage.is_complete == False,  # noqa: E712
                ).order_by(WebChatMessage.created_at.desc()).limit(1)
            )
            msg = result.scalar_one_or_none()
            if not msg:
                # Fallback: stuck progress cards (is_complete=True but still "running")
                result2 = await db.execute(
                    _sa.select(WebChatMessage).where(
                        WebChatMessage.session_id == session_id,
                        WebChatMessage.role == "assistant",
                        WebChatMessage.content.like("__PROGRESS_CARD__%running%"),
                    ).order_by(WebChatMessage.created_at.desc()).limit(1)
                )
                msg = result2.scalar_one_or_none()
            if msg and msg.content.startswith("__PROGRESS_CARD__"):
                try:
                    card = _json.loads(msg.content[len("__PROGRESS_CARD__"):])
                    if card.get("overall_status") == "running":
                        for step in card.get("steps", []):
                            if step.get("status") == "running":
                                step["status"] = "failed"
                                step["detail"] = "cancelled"
                        card["overall_status"] = "failed"
                        card["footer"] = "Cancelled by user"
                        msg.content = f"__PROGRESS_CARD__{_json.dumps(card)}"
                        msg.is_complete = True
                        updated_card = (msg.id, card)
                except Exception:
                    pass
            elif msg:
                # Regular SSE message (empty, loading, or partial) — mark as interrupted
                msg.content = "__INTERRUPTED__"
                msg.is_complete = True

            # Update Task.status in the same DB session
            from taghdev.models import Task
            task_result = await db.execute(
                _sa.select(Task).where(
                    Task.chat_id == chat_id,
                    Task.status.in_(["pending", "preparing", "planning",
                                     "plan_review", "coding", "reviewing", "pushing"]),
                ).order_by(Task.created_at.desc()).limit(1)
            )
            task = task_result.scalar_one_or_none()
            if task:
                task.status = "cancelled"
                task.error_message = "Cancelled by user"

            await db.commit()
    except Exception as e:
        log.warning("cancel.db_update_failed", error=str(e))

    # 3. Publish updated card to WebSocket (single connection)
    if updated_card:
        msg_id, card = updated_card
        try:
            async with aioredis.from_url(settings.redis_url) as r:
                payload = {
                    "type": "progress_card",
                    "message_id": str(msg_id),
                    "card": card,
                }
                await r.publish(f"wc:{user_id}:{session_id}", _json.dumps(payload))
        except Exception:
            pass

    # 4. Abort the most recent ARQ job
    if job_ids:
        try:
            redis_settings = parse_redis_url(settings.redis_url)
            arq_pool = await create_pool(redis_settings)
            try:
                for jid in job_ids[:1]:
                    jid_str = jid.decode() if isinstance(jid, bytes) else jid
                    try:
                        job = arq_pool.job(jid_str)
                        await job.abort(timeout=2)
                        aborted += 1
                    except Exception:
                        pass
            finally:
                await arq_pool.aclose()
        except Exception as e:
            log.warning("cancel.arq_abort_failed", error=str(e))

    return aborted


# ── Session job cancellation endpoint ─────────────────────────

@router.post("/threads/{session_id}/cancel")
async def cancel_session_jobs(session_id: int, user: User = Depends(web_user_dep)):
    """Cancel the most recent running task in this session."""
    try:
        aborted = await _cancel_session(session_id, user.id)
        log.info("session.cancelled", session_id=session_id, user_id=user.id, jobs=aborted)
        return {"cancelled": aborted}
    except Exception as e:
        log.warning("session.cancel_failed", error=str(e))
        return {"cancelled": 0, "error": str(e)}


@router.post("/threads/{session_id}/action")
async def session_action(session_id: int, body: dict, user: User = Depends(web_user_dep)):
    """Handle action buttons from the web chat progress card."""
    action_id = body.get("action_id", "")
    chat_id = f"web:{user.id}:{session_id}"
    log.info("session.action", session_id=session_id, action=action_id, user_id=user.id)

    if action_id.startswith("discard_task:"):
        task_id = action_id.split(":", 1)[1]
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job("discard_task", task_id)
        return {"status": "queued", "job": "discard_task"}

    if action_id.startswith("retry_task:"):
        task_id = action_id.split(":", 1)[1]
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job("execute_task", task_id)
        return {"status": "queued", "job": "execute_task"}

    from taghdev.api.routes.actions import web_action, WebActionRequest
    try:
        return await web_action(WebActionRequest(action_id=action_id, chat_id=chat_id), user)
    except Exception as e:
        log.warning("session.action_failed", error=str(e))
        return {"status": "error", "error": str(e)}
