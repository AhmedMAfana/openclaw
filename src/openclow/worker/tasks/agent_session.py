"""Interactive agent session — full Claude agent with MCP tools for direct chat."""
from __future__ import annotations

import asyncio
import time as _time

from openclow.providers import factory
from openclow.providers.llm.claude import _mcp_docker, _mcp_git
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

# Per-user conversation memory — each user in a channel has their own history
# Key: openclow:conv:{chat_id}:{user_id}  (falls back to {chat_id} if no user_id)
_CONV_KEY = "openclow:conv:{session_key}"
_CONV_MAX = 20  # Keep last 20 messages


def _session_key(chat_id: str, user_id: str = "") -> str:
    """Build per-user session key. Isolates memory per user even in shared channels."""
    return f"{chat_id}:{user_id}" if user_id else chat_id


async def _load_conversation(chat_id: str, user_id: str = "") -> list[dict]:
    """Load recent conversation messages from Redis (per-user)."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        key = _CONV_KEY.format(session_key=_session_key(chat_id, user_id))
        raw = await r.lrange(key, 0, _CONV_MAX - 1)
        await r.aclose()
        import json
        return [json.loads(m) for m in reversed(raw)]  # oldest first
    except Exception as e:
        log.warning("agent_session.redis_load_failed", chat_id=chat_id, error=str(e))
        return []


async def _save_message(chat_id: str, role: str, text: str, user_id: str = ""):
    """Save a message to per-user conversation history in Redis."""
    try:
        import redis.asyncio as aioredis
        import json
        r = aioredis.from_url(settings.redis_url)
        key = _CONV_KEY.format(session_key=_session_key(chat_id, user_id))
        msg = json.dumps({"role": role, "text": text[:1000]})
        await r.rpush(key, msg)
        await r.ltrim(key, -_CONV_MAX, -1)
        await r.expire(key, 3600)  # 1 hour TTL
        await r.aclose()
    except Exception as e:
        log.warning("agent_session.redis_save_failed", chat_id=chat_id, error=str(e))


def _format_response(text: str, provider: str = "telegram") -> str:
    """Clean up agent response for chat display."""
    import re

    if provider == "slack":
        # Slack supports mrkdwn natively — keep rich formatting
        # Convert markdown bold **text** → *text* (Slack mrkdwn)
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        # Convert markdown headers to bold lines
        text = re.sub(r"^#{1,3}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
        # Convert markdown lists to bullets
        text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)
        # Clean up excessive newlines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # Telegram — convert markdown to HTML parse mode
    import html
    text = html.escape(text)
    # Code blocks
    text = re.sub(r"```(?:\w+)?\n(.+?)```", r"<pre><code>\1</code></pre>", text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Headers
    text = re.sub(r"^#{1,3}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # Lists
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def agent_session(ctx: dict, user_message: str, chat_id: str, message_id: str,
                        project_context: str = "", chat_provider_type: str = "telegram", user_id: str = ""):
    """Run a Claude agent session for direct user interaction.

    The agent has full MCP tools and can do anything:
    - Read/edit code, search codebase
    - Docker: exec, logs, restart, compose
    - Tunnels: start, stop, get URL
    - Git operations
    """
    # Timeout: 90 seconds for the entire agent session
    # If agent hangs on Claude API, this prevents infinite hang
    try:
        await asyncio.wait_for(_run_agent_session(ctx, user_message, chat_id, message_id, project_context, chat_provider_type, user_id), timeout=90)
    except asyncio.TimeoutError:
        log.error("agent_session.timeout", chat_id=chat_id)
        try:
            chat = await factory.get_chat_by_type(chat_provider_type)
            await chat.edit_message(chat_id, message_id, "⏱️ Agent session timed out (90s). Try a simpler request.", is_final=True)
        except Exception:
            pass
        return

async def _run_agent_session(ctx: dict, user_message: str, chat_id: str, message_id: str,
                        project_context: str = "", chat_provider_type: str = "telegram", user_id: str = ""):
    try:
        chat = await factory.get_chat_by_type(chat_provider_type)
    except Exception:
        chat = await factory.get_chat()

    # Auto-cleanup: Cancel old stuck diff_preview tasks when user sends a new message
    # This prevents the UI from showing multiple unapproved tasks
    try:
        from openclow.models import Task, async_session
        from sqlalchemy import select, update
        async with async_session() as session:
            # Find old diff_preview tasks in the same channel
            old_stuck = await session.execute(
                select(Task).where(
                    Task.chat_id == chat_id,
                    Task.status == "diff_preview"
                ).order_by(Task.created_at.desc()).offset(0).limit(10)
            )
            old_tasks = old_stuck.scalars().all()
            # Cancel all but the most recent one (which might still be reviewing)
            if len(old_tasks) > 1:
                for old_task in old_tasks[1:]:
                    old_task.status = "cancelled"
                    await session.flush()
                await session.commit()
                log.info("agent_session.cancelled_old_tasks", chat_id=chat_id, count=len(old_tasks)-1)
    except Exception as e:
        log.warning("agent_session.cleanup_failed", error=str(e))

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        await chat.edit_message(chat_id, message_id, "Agent SDK unavailable. Install claude-agent-sdk.")
        return

    # Pre-check: is Claude authenticated?
    try:
        import json as _json
        auth_proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        auth_out, _ = await asyncio.wait_for(auth_proc.communicate(), timeout=10)
        auth_status = _json.loads(auth_out.decode())
        if not auth_status.get("loggedIn"):
            log.warning("agent_session.auth_expired", chat_id=chat_id)
            try:
                from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
                kb = ActionKeyboard(rows=[
                    ActionRow([ActionButton("🔑 Authenticate Claude", "claude_auth")]),
                    ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
                ])
                await chat.edit_message_with_actions(
                    chat_id, message_id,
                    "🔑 Claude is not authenticated.\n\nTap to sign in:",
                    kb,
                )
            except Exception as edit_err:
                log.error("agent_session.auth_edit_failed", error=str(edit_err))
                # Fallback: plain text
                await chat.edit_message(
                    chat_id, message_id,
                    "🔑 Claude is not authenticated. Run /settings to re-authenticate.",
                )
            await chat.close()
            return
    except Exception as e:
        log.warning("agent_session.auth_check_failed", error=str(e))
        # If check fails, try anyway — might work

    # Build project context from DB
    from openclow.models import Project, Task, async_session
    from sqlalchemy import select

    context_parts: list[str] = []
    workspace: str | None = None
    tunnel_url: str | None = None  # For "Open App" button
    project_name: str | None = None  # For response header

    # Extract target project_id from context (channel binding)
    target_pid: int | None = None
    if project_context and "project_id:" in project_context:
        try:
            target_pid = int(project_context.split("project_id:")[1].strip())
        except (ValueError, IndexError):
            pass

    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.status == "active")
        )
        all_projects = result.scalars().all()

        # If channel is linked, scope to that project only
        if target_pid:
            projects = [p for p in all_projects if p.id == target_pid]
        else:
            projects = all_projects

        if projects:
            if len(projects) == 1:
                p = projects[0]
                project_name = p.name  # Store for response header
                context_parts.append(f"FOCUSED PROJECT: {p.name} ({p.tech_stack or 'N/A'})")
                if p.description:
                    context_parts.append(f"Description: {p.description}")
                if p.agent_system_prompt:
                    context_parts.append(f"Conventions:\n{p.agent_system_prompt}")
                try:
                    from openclow.services.tunnel_service import get_tunnel_url, check_tunnel_health
                    t_url = await get_tunnel_url(p.name)
                    if t_url:
                        # Verify tunnel is actually alive before showing "Open App" button
                        tunnel_alive = await check_tunnel_health(p.name)
                        if tunnel_alive:
                            tunnel_url = t_url
                            t_url_display = t_url
                        else:
                            t_url_display = "tunnel not responding"
                    else:
                        t_url_display = "no tunnel"
                except Exception as e:
                    log.warning("agent_session.tunnel_fetch_failed", project=p.name, error=str(e))
                    t_url_display = "error fetching tunnel"
                context_parts.append(
                    f"Container: openclow-{p.name}-{p.app_container_name or 'app'}-1 | tunnel: {t_url_display}"
                )
                workspace = f"{settings.workspace_base_path}/_cache/{p.name}"
            else:
                context_parts.append(
                    "GENERAL MODE — No project is selected for this conversation.\n"
                    "If the user asks to do work, ask which project they mean, or suggest the most relevant one.\n"
                    "Available projects:"
                )
                for p in projects[:3]:  # Limit to 3 to avoid prompt bloat
                    context_parts.append(f"  • {p.name} ({p.tech_stack or 'N/A'})")
                    if p.description:
                        context_parts.append(f"    Description: {p.description[:150]}")
                workspace = f"{settings.workspace_base_path}/_cache"

        # Recent tasks with full context (files changed, errors, summaries)
        # Filter by user if available, else show all tasks in channel
        from openclow.models import TaskLog
        task_query = select(Task).where(Task.chat_id == chat_id)
        if user_id:
            # Look up user by provider UID to filter tasks
            from openclow.models import User
            user_result = await session.execute(
                select(User).where(User.chat_provider_uid == user_id)
            )
            db_user = user_result.scalar_one_or_none()
            if db_user:
                task_query = task_query.where(Task.user_id == db_user.id)

        result = await session.execute(task_query.order_by(Task.created_at.desc()).limit(3))
        recent_tasks = result.scalars().all()
        if recent_tasks:
            context_parts.append("\nRECENT TASKS:")
            for t in recent_tasks:
                line = f"  - [{t.status}] {t.description[:60]}"
                if t.pr_url:
                    line += f" | PR: {t.pr_url}"
                if t.error_message:
                    line += f" | Error: {t.error_message[:60]}"
                context_parts.append(line)

                # Load task logs for detail (what was done, files changed)
                log_result = await session.execute(
                    select(TaskLog)
                    .where(TaskLog.task_id == t.id)
                    .where(TaskLog.agent.in_(["coder", "system", "reviewer"]))
                    .order_by(TaskLog.created_at.desc())
                    .limit(2)
                )
                task_logs = log_result.scalars().all()
                for tl in task_logs:
                    if tl.message:
                        context_parts.append(f"    {tl.agent}: {tl.message[:120]}")

    if not workspace:
        workspace = settings.workspace_base_path

    context_str = "\n".join(context_parts) if context_parts else "No active projects."

    # Load conversation history for context (per-user)
    conv_history = await _load_conversation(chat_id, user_id)
    conv_str = ""
    if conv_history:
        conv_lines = []
        for msg in conv_history:
            role = "User" if msg["role"] == "user" else "You"
            conv_lines.append(f"{role}: {msg['text']}")
        conv_str = "\n".join(conv_lines)

    # Save user message to per-user conversation history
    await _save_message(chat_id, "user", user_message, user_id)

    system_prompt = f"""You are OpenClow — an AI Dev Orchestrator running inside {'Telegram' if chat_provider_type == 'telegram' else 'Slack'}.
You have FULL access to the user's projects via MCP tools.

{context_str}

CONVERSATION HISTORY (recent messages):
{conv_str if conv_str else "(first message)"}

YOU CAN:
- Read, edit, write files in project workspaces
- Run commands in Docker containers via docker_exec
- Check container status, logs, health
- Start/stop tunnels for public access
- Restart containers
- Search code with Grep/Glob
- Browse the app with Playwright — navigate to URLs, take screenshots, click, fill forms
- Use the tunnel URL to browse the live app and verify changes visually

RULES:
- Be concise — Telegram messages are small
- Show what you're doing as you do it
- If the user asks to change code, DO IT — don't just explain
- If something fails, investigate and fix it — don't give up after one try
- Always verify your changes work (curl the app, check logs)
- For file paths: project workspaces are at /workspaces/_cache/<project_name>/
- If you hit a blocker you can't solve (need API key, credentials, user decision),
  CLEARLY ASK the user what you need — don't fail silently
- If containers are down, try to bring them up. If ports conflict, fix the conflict.
- Be persistent — try at least 2-3 approaches before giving up
"""

    from openclow.providers.llm.claude import _mcp_playwright

    # Determine tools based on user role
    # Admins get full access, normal users get limited tools
    base_tools = [
        "Read", "Write", "Edit", "Glob", "Grep",
        # Playwright MCP — browse apps, screenshots (safe for all users)
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_snapshot",
        "mcp__playwright__browser_take_screenshot",
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_fill_form",
        "mcp__playwright__browser_type",
    ]

    # Check if user is admin (has is_admin flag set)
    is_admin = False
    if user_id:
        from openclow.models import User, async_session
        from sqlalchemy import select
        async with async_session() as session:
            user_result = await session.execute(
                select(User).where(User.chat_provider_uid == user_id)
            )
            db_user = user_result.scalar_one_or_none()
            is_admin = db_user and db_user.is_admin

    # Only add docker/infrastructure tools for admins
    if is_admin:
        base_tools.extend([
            # Docker MCP — containers, commands, tunnels (admin only)
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
            "mcp__docker__compose_up",
            "mcp__docker__compose_ps",
            # Tunnel MCP (admin only)
            "mcp__docker__tunnel_start",
            "mcp__docker__tunnel_stop",
            "mcp__docker__tunnel_get_url",
            "mcp__docker__tunnel_list",
        ])

    # Build MCP servers dict based on user role
    mcp_servers = {"playwright": _mcp_playwright()}
    if is_admin:
        mcp_servers["docker"] = _mcp_docker()

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt=system_prompt,
        model="claude-sonnet-4-6",
        allowed_tools=base_tools,
        mcp_servers=mcp_servers,
        permission_mode="bypassPermissions",
        max_turns=20,
    )

    # Stream agent response, collecting text and updating chat
    response_text = ""
    last_update = ""
    tool_lines: list[str] = []
    _start = _time.time()
    _is_slack = chat_provider_type == "slack"

    try:
        async for message in query(prompt=user_message, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text = block.text
                        display = response_text[:3800]
                        if tool_lines:
                            display = "\n".join(tool_lines[-3:]) + "\n\n" + display
                        if display != last_update:
                            last_update = display
                            try:
                                await chat.edit_message(chat_id, message_id, display)
                            except Exception:
                                pass

                    elif isinstance(block, ToolUseBlock):
                        from openclow.worker.tasks._agent_base import describe_tool
                        tool_lines.append(describe_tool(block))
                        if len(tool_lines) > 5:
                            tool_lines = tool_lines[-5:]

                        try:
                            if _is_slack:
                                from openclow.providers.chat.slack.blocks import agent_working_blocks
                                blks = agent_working_blocks(tool_lines, elapsed=int(_time.time() - _start))
                                await chat.edit_message_blocks(chat_id, message_id, blks)
                            else:
                                display = "\n".join(tool_lines) + "\n\nWorking..."
                                await chat.edit_message(chat_id, message_id, display)
                        except Exception:
                            pass

    except asyncio.CancelledError:
        log.warning("agent_session.cancelled", chat_id=chat_id)
        response_text = "Agent session was cancelled. Try again."
    except Exception as e:
        import traceback
        log.error("agent_session.failed", error=str(e), traceback=traceback.format_exc())
        from openclow.worker.tasks._agent_base import is_auth_error
        if is_auth_error(e):
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔑 Re-authenticate", "claude_auth")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            try:
                await chat.edit_message_with_actions(
                    chat_id, message_id,
                    "🔑 Claude auth expired.\n\nTap to re-authenticate:",
                    kb,
                )
            except Exception:
                pass
            return
        response_text = f"Agent error: {str(e)[:300]}"

    if not response_text:
        response_text = "Done."

    # Format response for clean display
    final_text = _format_response(response_text[:3800], chat_provider_type)

    # Save assistant response to per-user conversation memory
    await _save_message(chat_id, "assistant", final_text[:1000], user_id)

    pid = target_pid  # Already extracted above

    if _is_slack:
        # Rich Block Kit response for Slack
        from openclow.providers.chat.slack.blocks import agent_response_blocks
        blks = agent_response_blocks(final_text, project_id=pid, tunnel_url=tunnel_url, project_name=project_name)
        try:
            await chat.edit_message_blocks(chat_id, message_id, blks, is_final=True)
        except Exception:
            try:
                await chat.edit_message(chat_id, message_id, final_text, is_final=True)
            except Exception:
                pass
    else:
        # Telegram — plain text with action buttons
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        rows = []
        if pid:
            row1 = [ActionButton("🤖 Chat Again", f"agent_diagnose:{pid}")]
            from openclow.providers.actions import open_app_btn
            row1.append(open_app_btn(pid))
            rows.append(ActionRow(row1))
            rows.append(ActionRow([
                ActionButton("🚀 New Task", "menu:task"),
                ActionButton("◀️ Main Menu", "menu:main"),
            ]))
        else:
            rows.append(ActionRow([
                ActionButton("🚀 New Task", "menu:task"),
                ActionButton("◀️ Main Menu", "menu:main"),
            ]))
        kb = ActionKeyboard(rows=rows)

        try:
            await chat.edit_message_with_actions(chat_id, message_id, final_text, kb)
        except Exception:
            try:
                await chat.edit_message(chat_id, message_id, final_text)
            except Exception:
                pass
