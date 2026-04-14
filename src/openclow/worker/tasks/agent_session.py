"""Interactive agent session — full Claude agent with MCP tools for direct chat."""
from __future__ import annotations

import asyncio
import time as _time

from openclow.providers import factory
from openclow.providers.llm.claude import _mcp_docker, _mcp_git
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

# Cancel flag key — set by UI when user clicks Stop
_CANCEL_KEY = "openclow:cancel:{chat_id}:{message_id}"


async def _check_cancelled(chat_id: str, message_id: str) -> bool:
    """Check if user requested cancellation via Redis flag."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        val = await r.get(_CANCEL_KEY.format(chat_id=chat_id, message_id=message_id))
        await r.aclose()
        return val is not None
    except Exception:
        return False


async def set_session_cancelled(chat_id: str, message_id: str):
    """Set cancel flag for an agent session (called from UI handler)."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.set(
            _CANCEL_KEY.format(chat_id=chat_id, message_id=message_id),
            "1", ex=300,  # Auto-expire after 5 min
        )
        await r.aclose()
    except Exception as e:
        log.warning("agent_session.set_cancel_failed", error=str(e))


# Per-user conversation memory — each user in a channel has their own history
# Key: openclow:conv:{chat_id}:{user_id}  (falls back to {chat_id} if no user_id)
_CONV_KEY = "openclow:conv:{session_key}"
_CONV_MAX = 40  # Keep last 40 messages for richer context


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
        msg = json.dumps({"role": role, "text": text[:2000]})
        await r.rpush(key, msg)
        await r.ltrim(key, -_CONV_MAX, -1)
        await r.expire(key, 14400)  # 4 hour TTL — conversation persists for a work session
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

async def _check_auth_cached() -> bool | None:
    """Check Claude auth with Redis cache (5 min TTL). Returns True/False/None (unknown)."""
    _AUTH_CACHE_KEY = "openclow:claude_auth_ok"
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        cached = await r.get(_AUTH_CACHE_KEY)
        if cached is not None:
            await r.aclose()
            return cached == b"1"
        # Cache miss — check via subprocess
        import json as _json
        auth_proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        auth_out, _ = await asyncio.wait_for(auth_proc.communicate(), timeout=10)
        auth_status = _json.loads(auth_out.decode())
        ok = auth_status.get("loggedIn", False)
        await r.set(_AUTH_CACHE_KEY, "1" if ok else "0", ex=300)
        await r.aclose()
        return ok
    except Exception as e:
        log.warning("agent_session.auth_check_failed", error=str(e))
        return None  # Unknown — try anyway


async def _cleanup_old_tasks(chat_id: str):
    """Cancel old stuck diff_preview tasks in background."""
    try:
        from openclow.models import Task, async_session
        from sqlalchemy import select
        async with async_session() as session:
            old_stuck = await session.execute(
                select(Task).where(
                    Task.chat_id == chat_id,
                    Task.status == "diff_preview"
                ).order_by(Task.created_at.desc()).offset(0).limit(10)
            )
            old_tasks = old_stuck.scalars().all()
            if len(old_tasks) > 1:
                for old_task in old_tasks[1:]:
                    old_task.status = "cancelled"
                    await session.flush()
                await session.commit()
                log.info("agent_session.cancelled_old_tasks", chat_id=chat_id, count=len(old_tasks)-1)
    except Exception as e:
        log.warning("agent_session.cleanup_failed", error=str(e))


async def _build_context(chat_id: str, project_context: str, user_id: str):
    """Build project context + check admin status in a single DB session.

    Returns (context_parts, workspace, tunnel_url, project_name, target_pid, is_admin).
    """
    from openclow.models import Project, Task, TaskLog, User, async_session
    from sqlalchemy import select

    context_parts: list[str] = []
    workspace: str | None = None
    tunnel_url: str | None = None
    project_name: str | None = None
    is_admin = False

    target_pid: int | None = None
    if project_context and "project_id:" in project_context:
        try:
            target_pid = int(project_context.split("project_id:")[1].strip())
        except (ValueError, IndexError):
            pass

    async with async_session() as session:
        # ── Admin check (same session) ──
        if user_id:
            user_result = await session.execute(
                select(User).where(User.chat_provider_uid == user_id)
            )
            db_user = user_result.scalar_one_or_none()
            is_admin = db_user and db_user.is_admin
        else:
            db_user = None

        # ── Projects ──
        result = await session.execute(
            select(Project).where(Project.status == "active")
        )
        all_projects = result.scalars().all()

        if target_pid:
            projects = [p for p in all_projects if p.id == target_pid]
        else:
            projects = all_projects

        if projects:
            if len(projects) == 1:
                p = projects[0]
                project_name = p.name
                context_parts.append(f"FOCUSED PROJECT: {p.name} ({p.tech_stack or 'N/A'})")
                if p.description:
                    context_parts.append(f"Description: {p.description}")
                if p.agent_system_prompt:
                    context_parts.append(f"Conventions:\n{p.agent_system_prompt}")
                # Get tunnel URL without health check (fast path)
                try:
                    from openclow.services.tunnel_service import get_tunnel_url
                    t_url = await get_tunnel_url(p.name)
                    if t_url:
                        tunnel_url = t_url
                        t_url_display = t_url
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
                for p in projects[:3]:
                    context_parts.append(f"  • {p.name} ({p.tech_stack or 'N/A'})")
                    if p.description:
                        context_parts.append(f"    Description: {p.description[:150]}")
                workspace = f"{settings.workspace_base_path}/_cache"

        # ── Recent tasks ──
        task_query = select(Task).where(Task.chat_id == chat_id)
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

    return context_parts, workspace, tunnel_url, project_name, target_pid, is_admin


async def _run_agent_session(ctx: dict, user_message: str, chat_id: str, message_id: str,
                        project_context: str = "", chat_provider_type: str = "telegram", user_id: str = ""):
    try:
        chat = await factory.get_chat_by_type(chat_provider_type)
    except Exception:
        chat = await factory.get_chat()

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        await chat.edit_message(chat_id, message_id, "Agent SDK unavailable. Install claude-agent-sdk.")
        return

    # ── Run all setup in parallel: auth, cleanup, context, conversation ──
    auth_result, _, (context_parts, workspace, tunnel_url, project_name, target_pid, is_admin), conv_history, _ = await asyncio.gather(
        _check_auth_cached(),
        _cleanup_old_tasks(chat_id),
        _build_context(chat_id, project_context, user_id),
        _load_conversation(chat_id, user_id),
        _save_message(chat_id, "user", user_message, user_id),
    )

    # Auth check: block only if definitively not logged in
    if auth_result is False:
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
        except Exception:
            await chat.edit_message(
                chat_id, message_id,
                "🔑 Claude is not authenticated. Run /settings to re-authenticate.",
            )
        await chat.close()
        return

    context_str = "\n".join(context_parts) if context_parts else "No active projects."

    conv_str = ""
    if conv_history:
        conv_lines = []
        for msg in conv_history:
            role = "User" if msg["role"] == "user" else "You"
            conv_lines.append(f"{role}: {msg['text']}")
        conv_str = "\n".join(conv_lines)

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
- Read/Write/Edit/Glob/Grep — files in /workspaces/_cache/<project_name>/
- docker_exec, container_logs, container_health, restart_container
- compose_up, compose_ps — Docker Compose management
- tunnel_start, tunnel_stop, tunnel_get_url — public URLs
- Playwright — navigate, screenshot, click, fill forms in the live app

Be concise. Talk like a person, not a manual.
"""

    from openclow.providers.llm.claude import _mcp_playwright

    base_tools = [
        "Read", "Write", "Edit", "Glob", "Grep",
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_snapshot",
        "mcp__playwright__browser_take_screenshot",
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_fill_form",
        "mcp__playwright__browser_type",
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

    # Stream agent response
    response_text = ""
    last_update = ""
    tool_lines: list[str] = []
    _start = _time.time()
    _is_slack = chat_provider_type == "slack"
    _using_tools = False
    _turn_count = 0
    _cancelled_by_user = False
    _SPINNERS = ["🔄", "⏳", "🔃", "⚙️"]

    from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
    _stop_kb = ActionKeyboard(rows=[
        ActionRow([ActionButton("⏹️ Stop", f"session_cancel:{chat_id}:{message_id}")]),
    ])

    def _spinner() -> str:
        elapsed = int(_time.time() - _start)
        icon = _SPINNERS[elapsed % len(_SPINNERS)]
        return f"{icon} `{elapsed}s`"

    async def _show_progress(display: str):
        """Update message with text + stop button."""
        nonlocal last_update
        if display != last_update:
            last_update = display
            try:
                await chat.edit_message_with_actions(chat_id, message_id, display, _stop_kb)
            except Exception:
                pass

    try:
        async for message in query(prompt=user_message, options=options):
            _turn_count += 1

            # Check cancel flag every 3 turns (avoid Redis spam)
            if _turn_count % 3 == 0 and await _check_cancelled(chat_id, message_id):
                _cancelled_by_user = True
                response_text = "Stopped by user."
                break

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text = block.text
                        display = response_text[:3800]
                        if tool_lines and _using_tools:
                            tool_ctx = " → ".join(t.split(" ", 1)[-1][:30] for t in tool_lines[-3:])
                            display = f"{display}\n\n{_spinner()}  `{tool_ctx}`"
                        await _show_progress(display)

                    elif isinstance(block, ToolUseBlock):
                        _using_tools = True
                        from openclow.worker.tasks._agent_base import describe_tool
                        tool_lines.append(describe_tool(block))
                        if len(tool_lines) > 5:
                            tool_lines = tool_lines[-5:]

                        tool_summary = " → ".join(t.split(" ", 1)[-1][:30] for t in tool_lines[-3:])
                        if response_text:
                            display = f"{response_text[:3000]}\n\n{_spinner()}  `{tool_summary}`"
                        else:
                            display = f"{_spinner()}  `{tool_summary}`"
                        await _show_progress(display)

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
            from openclow.providers.actions import open_app_btns
            rows.append(ActionRow(open_app_btns(pid, tunnel_url=tunnel_url)))
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
