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
    from openclow.providers.llm.claude import _mcp_playwright, _mcp_docker, _mcp_actions, _mcp_github

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
            # If the session has a project assigned, focus the agent on it
            project_context = f"project_id:{ws.project_id}" if ws.project_id else ""
            context_parts, workspace, tunnel_url, project_name, _, ctx_is_admin = \
                await _build_context(chat_id, project_context=project_context, user_id=str(user.id))

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

            # 4b. Fetch access context for role-based tool filtering
            from openclow.services.access_service import get_accessible_projects_for_mcp, is_tool_allowed
            accessible_projects, effective_role = await get_accessible_projects_for_mcp(user.id, is_admin)

            # 5. Build system prompt — DevOps-aware with actions pipeline
            context_str = "\n".join(context_parts) if context_parts else "No active projects."
            current_project = project_name or "None selected — use list_projects() to see options"
            tunnel_display = tunnel_url or "not running"
            system_prompt = f"""You are TAGH DevOps — a senior-level AI DevOps engineer embedded in the TAGH orchestration platform. You respond with precision and professionalism. No greetings, no filler, no "Hey!" — go straight to what matters.

Tone rules:
- Speak like a principal engineer on-call: calm, precise, authoritative
- No casual phrases ("Hey!", "Sure!", "Of course!", "Great question!")
- Short answers for simple questions, detailed technical responses when depth is needed
- When asked what you can do, list concrete capabilities: deploy projects, trigger tasks, manage Docker, check system health, browse GitHub repos, run QA
- Never say "I'm just an AI" — you ARE the DevOps engineer for this platform

CURRENT PROJECT: {current_project}
TUNNEL: {tunnel_display}
CHAT ID: {chat_id}

PLATFORM CONTEXT:
{context_str}

CONVERSATION HISTORY:
{conv_str if conv_str else "(first message)"}

═══ INFORMATION HIERARCHY — USE THIS ORDER, EVERY TIME ═══

Before asking the user ANYTHING, exhaust tools in this priority order:

1. PLATFORM STATE   → list_projects() / list_tasks() / system_status()
   What's running, what's queued, what's healthy. Always start here.

2. GITHUB METADATA  → list_repos() / repo_info() / list_prs()
   Repo names, branch names, PR status. Use this to understand project structure.

3. RUNNING APP      → tunnel URL / Playwright browser tools
   Verify the live app behaves correctly. Tunnel may be down — that is fine,
   it does NOT block triggering tasks or checking platform state.

4. ASK THE USER     → ONLY if none of the above can answer the question.
   "I checked the platform state and GitHub — I still need X" is the bar.

═══ WHEN SOMETHING FAILS — OBSTACLE PROTOCOL ═══

Any time a tool call fails or returns nothing useful:
  → Don't stop. Don't ask. Try the next tool in the hierarchy above.
  → Log what failed internally and move on.
  → Only surface an obstacle to the user after you've tried ≥3 alternatives.

Examples:
  Tunnel down?       → Use Playwright to check health endpoint, or report container status.
  GitHub API limit?  → Use list_projects() / system_status() to answer from platform state.
  Container offline? → Check system_status(), then docker_container_logs if admin,
                       then report the specific error — not "containers aren't running".
  Task context unclear? → list_tasks(status="active") + list_projects() first. Then ask.

═══ NEVER GIVE UP ═══

You NEVER stop until the task is fully complete. You do NOT say "I'll get back to you", "the worker will handle it", or "sit tight". You stay in the loop, call tools, and finish the job yourself.

═══ FULL AUTONOMOUS SETUP ═══

When asked to "set up", "bootstrap", "run", "deploy", or "open" a project — do ALL of these steps yourself, one after another, no stopping:

STEP 1 — Check DB:
  mcp__actions__list_projects()
  → If project is IN DB → skip to STEP 4
  → If NOT in DB → continue to STEP 2

STEP 2 — Find repo and trigger onboarding (once):
  mcp__github__list_repos() → find the exact GitHub URL
  mcp__actions__trigger_addproject(repo_url=<url>, chat_id="{chat_id}", message_id="")
  → Do NOT call trigger_addproject again. Move immediately to STEP 3.

STEP 3 — Wait for analysis then auto-confirm:
  mcp__actions__check_pending_project(project_name=<name>, wait_seconds=120)
  → This blocks until worker finishes analyzing (up to 2 min) — that is fine
  → When it returns "READY" → immediately call:
  mcp__actions__confirm_project(project_name=<name>, chat_id="{chat_id}", message_id="")
  → Continue to STEP 4.

STEP 4 — If project already in DB, bootstrap it:
  mcp__actions__bootstrap(project_name=<name>, chat_id="{chat_id}", message_id="")
  → Continue to STEP 5.

STEP 5 — Wait for bootstrap and report URL:
  Wait ~30 seconds, then call mcp__actions__list_projects()
  → If tunnel URL appears → report it to user. DONE.
  → If not ready yet → wait and check again. Keep checking until URL appears or 5 min elapsed.
  → NEVER tell the user "the URL will appear later". Get it yourself and report it.

═══ FOLLOW-UP QUESTIONS ═══

If the user asks "what's happening" / "any update" / "where's the URL":
  → call list_tasks(status="active") + list_projects() in parallel
  → report what you see
  → if bootstrap is still running, keep polling (call those tools again) until done
  → never say "I don't know" or "check back later"

═══ OTHER TOOLS ═══
- mcp__actions__list_tasks(status="active") — see running jobs
- mcp__actions__system_status() — Redis, Postgres, Docker health
- mcp__actions__docker_up/down(project_name, chat_id="{chat_id}", message_id="")
- mcp__actions__relink_project / unlink_project
- mcp__github__repo_info(repo), mcp__github__list_prs(repo)
- "what projects do I have" → list_projects() + list_repos() in parallel
- "add feature / fix bug in X" → trigger_task(project_name, description, chat_id="{chat_id}")
"""

            # Inject role context section if user has a restricted role
            if not is_admin and effective_role is not None:
                proj_names = ", ".join(p.name for p in accessible_projects) or "(none)"
                role_section = (
                    f"\n═══ YOUR ACCESS SCOPE ═══\n"
                    f"YOUR ROLE: {effective_role.upper()}\n"
                    f"YOUR PROJECTS: {proj_names}\n"
                    f"You MUST NOT call tools or access projects outside your role and project list.\n"
                )
                system_prompt = system_prompt + role_section

            # 6. Set up tools — actions MCP for all users, docker for admins
            # NOTE: Read/Write/Edit/Glob/Grep are intentionally excluded from web chat.
            # Web users operate the platform (deploy, trigger, status) — they do NOT get
            # raw filesystem access. Exposing these tools via web would allow any
            # authenticated user to read .env files, API keys, and other sensitive configs.
            # File-level access remains available only through the Telegram/Slack bots.
            base_tools = [
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
                "mcp__playwright__browser_take_screenshot",
                "mcp__playwright__browser_click",
                "mcp__playwright__browser_fill_form",
                "mcp__playwright__browser_type",
                # Actions MCP — DevOps pipeline for ALL authenticated users
                "mcp__actions__list_projects",
                "mcp__actions__list_tasks",
                "mcp__actions__system_status",
                "mcp__actions__trigger_addproject",
                "mcp__actions__bootstrap",
                "mcp__actions__trigger_task",
                "mcp__actions__docker_up",
                "mcp__actions__docker_down",
                "mcp__actions__relink_project",
                "mcp__actions__unlink_project",
                "mcp__actions__check_pending_project",
                "mcp__actions__confirm_project",
                # GitHub MCP — repo browsing and PR access
                "mcp__github__list_repos",
                "mcp__github__repo_info",
                "mcp__github__list_branches",
                "mcp__github__list_prs",
                "mcp__github__check_repo_access",
            ]
            mcp_servers: dict = {
                "playwright": _mcp_playwright(),
                "actions": _mcp_actions(),
                "github": _mcp_github(),
            }

            # Filter base_tools for restricted users — agent can't even see tools it can't call
            if not is_admin and effective_role is not None:
                # Map mcp__actions__ tool names to the short names used in ROLE_TOOLS
                _action_map = {
                    "mcp__actions__trigger_task": "trigger_task",
                    "mcp__actions__trigger_addproject": "trigger_addproject",
                    "mcp__actions__bootstrap": "bootstrap",
                    "mcp__actions__docker_up": "docker_up",
                    "mcp__actions__docker_down": "docker_down",
                    "mcp__actions__relink_project": "relink_project",
                    "mcp__actions__unlink_project": "unlink_project",
                    "mcp__actions__remove_project": "remove_project",
                    "mcp__actions__check_pending_project": "check_pending_project",
                    "mcp__actions__confirm_project": "confirm_project",
                    "mcp__actions__run_qa": "run_qa",
                }
                base_tools = [
                    t for t in base_tools
                    if not t.startswith("mcp__actions__") or
                    is_tool_allowed(effective_role, _action_map.get(t, t))
                ]

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
                max_turns=40,
                include_partial_messages=True,  # yields StreamEvent for token-level streaming
            )

            final_text = ""
            cancelled = False
            try:
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
            except (GeneratorExit, Exception) as _cancel_exc:
                import asyncio
                if isinstance(_cancel_exc, asyncio.CancelledError) or type(_cancel_exc).__name__ in ("CancelledError", "GeneratorExit"):
                    cancelled = True
                    log.info("assistant.cancelled", session_id=session_id)
                else:
                    raise

            # 8. Save final assistant message to DB (skip placeholder if user cancelled)
            if cancelled:
                # Save any partial text we captured, but don't save empty placeholder
                if final_text:
                    await _save_message(session_id, user.id, "assistant", final_text)
            elif final_text:
                await _save_message(session_id, user.id, "assistant", final_text)
            else:
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
