"""Web chat assistant endpoint — streaming Claude responses directly (no worker queue)."""
from __future__ import annotations

from typing import Any, Union

import sqlalchemy
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from assistant_stream import create_run, RunController
from assistant_stream.serialization import DataStreamResponse

from openclow.api.web_auth import web_user_dep
from openclow.models.user import User
from openclow.models.base import async_session
from openclow.models.web_chat import WebChatSession, WebChatMessage
from openclow.utils.logging import get_logger

log = get_logger()

router = APIRouter(prefix="/api", tags=["web_chat"])


# ── Request models ────────────────────────────────────────────

class MessagePart(BaseModel):
    type: str
    text: str | None = None
    image: str | None = None


class UserMessage(BaseModel):
    role: str
    parts: list[MessagePart]


class AddMessageCommand(BaseModel):
    type: str = "add-message"
    message: UserMessage
    parentId: str | None = None
    sourceId: str | None = None


class AddToolResultCommand(BaseModel):
    type: str = "add-tool-result"
    toolCallId: str
    toolName: str
    result: dict[str, Any]
    isError: bool = False


class AssistantRequest(BaseModel):
    """Request payload matching assistant-ui protocol."""
    commands: list[Union[AddMessageCommand, AddToolResultCommand]]
    threadId: str | None = None  # session id
    state: dict[str, Any] | None = None
    system: str | None = None
    tools: dict[str, Any] | None = None
    mode: str = "quick"  # "quick" | "plan"
    retry: bool = False  # if True, delete last assistant msg and re-run (no new user msg saved)


# ── Helper functions ──────────────────────────────────────────

async def _get_or_create_session(user_id: int, thread_id: str | None) -> WebChatSession:
    """Get existing session or create new one."""
    async with async_session() as session:
        if thread_id:
            result = await session.get(WebChatSession, int(thread_id))
            if result and result.user_id == user_id:
                return result

        new_session = WebChatSession(
            user_id=user_id,
            title="New Chat",
            mode="quick",
        )
        session.add(new_session)
        await session.commit()
        await session.refresh(new_session)
        return new_session


async def _load_session_history(session_id: int, limit: int = 40) -> list[dict]:
    """Load message history from DB for a session."""
    async with async_session() as session:
        result = await session.execute(
            sqlalchemy.select(WebChatMessage)
            .where(WebChatMessage.session_id == session_id)
            .order_by(WebChatMessage.created_at.asc())
            .limit(limit)
        )
        messages = result.scalars().all()
        return [{"role": msg.role, "content": msg.content} for msg in messages]


async def _save_message(session_id: int, user_id: int, role: str, content: str) -> WebChatMessage:
    """Save a message to DB."""
    async with async_session() as session:
        msg = WebChatMessage(
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            is_complete=(role == "user"),
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg


def _extract_user_text(commands: list) -> str:
    """Extract text from add-message command."""
    for cmd in commands:
        if isinstance(cmd, AddMessageCommand):
            texts = [p.text for p in cmd.message.parts if p.text]
            return " ".join(texts).strip()
        if isinstance(cmd, dict) and cmd.get("type") == "add-message":
            parts = cmd.get("message", {}).get("parts", [])
            texts = [p.get("text", "") for p in parts if p]
            return " ".join(texts).strip()
    return ""


# ── Main streaming endpoint ────────────────────────────────────

@router.post("/assistant")
async def assistant_endpoint(
    request: AssistantRequest,
    user: User = Depends(web_user_dep),
) -> DataStreamResponse:
    """Stream Claude responses directly — no worker queue, no Redis hop.

    Architecture: FastAPI → claude_agent_sdk.query() inline → StreamEvent → browser
    Connection stays alive as long as the agent runs (no artificial timeout).
    """
    from claude_agent_sdk import query, ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock, StreamEvent
    from openclow.worker.tasks.agent_session import _build_context
    from openclow.worker.tasks._agent_base import describe_tool
    from openclow.providers.llm.claude import _mcp_playwright, _mcp_docker

    async def run(controller: RunController):
        try:
            # 1. Get or create session
            ws = await _get_or_create_session(user.id, request.threadId)
            session_id = ws.id
            chat_id = f"web:{user.id}:{session_id}"

            # 2. Extract user text
            user_text = _extract_user_text(request.commands)

            if request.retry:
                # Retry: delete last assistant message, recover last user text
                from sqlalchemy import select as sa_select, desc as sa_desc
                async with async_session() as db:
                    r = await db.execute(
                        sa_select(WebChatMessage)
                        .where(WebChatMessage.session_id == session_id, WebChatMessage.role == "assistant")
                        .order_by(sa_desc(WebChatMessage.created_at))
                        .limit(1)
                    )
                    last_asst = r.scalar_one_or_none()
                    if last_asst:
                        await db.delete(last_asst)
                        await db.commit()

                    if not user_text:
                        ru = await db.execute(
                            sa_select(WebChatMessage)
                            .where(WebChatMessage.session_id == session_id, WebChatMessage.role == "user")
                            .order_by(sa_desc(WebChatMessage.created_at))
                            .limit(1)
                        )
                        last_user = ru.scalar_one_or_none()
                        user_text = last_user.content if last_user else ""

                if not user_text:
                    controller.append_text("(no message to retry)")
                    return
            else:
                if not user_text:
                    controller.append_text("(empty message)")
                    return
                await _save_message(session_id, user.id, "user", user_text)

            # 3. Build project context — loads from DB (projects, recent tasks, tunnel URL)
            context_parts, workspace, tunnel_url, project_name, _, ctx_is_admin = \
                await _build_context(chat_id, project_context="", user_id=str(user.id))

            # Use the authenticated user's admin flag (more reliable for web users)
            is_admin = user.is_admin or ctx_is_admin

            # Ensure workspace exists — /workspaces is a volume only mounted in worker+api.
            # If misconfigured (e.g. fresh compose without the volume), fall back to /tmp
            # rather than crashing. The agent can still read/write files there.
            import os
            if workspace and not os.path.isdir(workspace):
                try:
                    os.makedirs(workspace, exist_ok=True)
                except OSError:
                    workspace = "/tmp"
                    log.warning("assistant.workspace_fallback", workspace=workspace)

            # 4. Load conversation history from DB
            history = await _load_session_history(session_id, limit=40)
            conv_lines = [
                f"{'User' if m['role'] == 'user' else 'You'}: {m['content'][:500]}"
                for m in history[:-1]  # exclude current user message (already appended)
            ]
            conv_str = "\n".join(conv_lines)

            # 5. Build system prompt — identical structure to agent_session.py
            context_str = "\n".join(context_parts) if context_parts else "No active projects."
            system_prompt = f"""You are THAG GROUP specialist — an AI Dev Orchestrator. You're chatting with the user in real time.

{context_str}

CONVERSATION HISTORY:
{conv_str if conv_str else "(first message)"}

You are chatting with a developer. Read their message, understand the intent, and respond naturally.
Use your judgment — you know when someone needs help vs just chatting.

When you do take action (code changes, fixes, checks), always:
- Say what you're about to do before doing it
- Give updates as you work
- Verify the result (curl the app, check logs, screenshot with Playwright)
- End with the live app link (use tunnel_get_url, or tunnel_start if none exists)

TOOLS AVAILABLE:
- Read/Write/Edit/Glob/Grep — files in {workspace}/
- docker_exec, container_logs, container_health, restart_container
- compose_up, compose_ps — Docker Compose management
- tunnel_start, tunnel_stop, tunnel_get_url — public URLs
- Playwright — navigate, screenshot, click, fill forms in the live app

Be concise. Talk like a person, not a manual.
"""

            # 6. Set up tools — same as agent_session.py
            base_tools = [
                "Read", "Write", "Edit", "Glob", "Grep",
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
                "mcp__playwright__browser_take_screenshot",
                "mcp__playwright__browser_click",
                "mcp__playwright__browser_fill_form",
                "mcp__playwright__browser_type",
            ]
            mcp_servers: dict = {"playwright": _mcp_playwright()}

            if is_admin:
                base_tools.extend([
                    "mcp__docker__list_containers",
                    "mcp__docker__container_logs",
                    "mcp__docker__container_health",
                    "mcp__docker__docker_exec",
                    "mcp__docker__restart_container",
                    "mcp__docker__compose_up",
                    "mcp__docker__compose_ps",
                    "mcp__docker__tunnel_start",
                    "mcp__docker__tunnel_stop",
                    "mcp__docker__tunnel_get_url",
                    "mcp__docker__tunnel_list",
                ])
                mcp_servers["docker"] = _mcp_docker()

            # 7. Run agent INLINE — tokens stream directly to browser, no Redis hop
            options = ClaudeAgentOptions(
                cwd=workspace,
                system_prompt=system_prompt,
                model="claude-sonnet-4-6",
                allowed_tools=base_tools,
                mcp_servers=mcp_servers,
                permission_mode="bypassPermissions",
                max_turns=20,
                include_partial_messages=True,  # yields StreamEvent for token-level streaming
            )

            final_text = ""
            async for message in query(prompt=user_text, options=options):
                if isinstance(message, StreamEvent):
                    # Token-level streaming — send each delta straight to the browser
                    evt = message.event
                    if evt.get("type") == "content_block_delta":
                        delta = evt.get("delta", {})
                        if delta.get("type") == "text_delta":
                            controller.append_text(delta.get("text", ""))

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            final_text = block.text
                        elif isinstance(block, ToolUseBlock):
                            # Show in Thinking panel
                            controller.add_data({
                                "type": "tool_use",
                                "tool": describe_tool(block),
                                "input": "",
                                "status": "running",
                            })

            # 8. Save final assistant message to DB
            if final_text:
                await _save_message(session_id, user.id, "assistant", final_text)
            elif not final_text:
                # Nothing streamed — save placeholder so history is consistent
                await _save_message(session_id, user.id, "assistant", "(no response)")

            # 9. Update thread title on first message
            if ws.title == "New Chat" and user_text:
                async with async_session() as db:
                    session_obj = await db.get(WebChatSession, session_id)
                    if session_obj:
                        session_obj.title = user_text[:50]
                        await db.commit()

        except Exception as e:
            log.error("assistant.error", error=str(e), exc_info=True)
            controller.append_text(f"Error: {str(e)[:200]}")

    stream = create_run(run)
    return DataStreamResponse(stream)
