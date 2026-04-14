"""Shared agentic repair helper — Claude agent with Docker MCP tools.

NEVER gives up. Retries on crash with context. Timeout + heartbeat.
Always shows buttons at the end.
"""
from __future__ import annotations

import asyncio
import time
from openclow.utils.logging import get_logger

log = get_logger()

_SYSTEM_PROMPT = """\
Senior DevOps engineer. You have FULL CONTROL over Docker infrastructure via MCP tools.

AVAILABLE TOOLS:
- compose_build — build images only (no start). Use when you need to rebuild after Dockerfile changes.
- compose_up — start a stack. Set build=True to build images first then start.
  compose_up(build=True) handles Docker-in-Docker path translation automatically.
- compose_down/compose_ps — stop stack / list containers in stack
- container_logs/container_health — inspect containers
- docker_exec — run ANY command inside a running container
- restart_container — restart specific containers
- list_containers — see all containers
- tunnel_start/tunnel_stop/tunnel_get_url — manage Cloudflare tunnels
- Read/Edit/Glob/Grep — read and modify files on host

CRITICAL ARCHITECTURE:
- Project apps run inside Docker containers (managed by docker-compose).
- Cloudflare tunnels run on the WORKER HOST, NOT inside project containers.
- NEVER run "which cloudflared" or "cloudflared" inside containers — it does not exist there.
- Use tunnel_start/tunnel_get_url/tunnel_stop MCP tools for tunnel management.
- Always read container_logs BEFORE attempting fixes — understand the error first.
- Docker-in-Docker: the worker runs in a container but uses the host Docker socket.
  Build contexts use container paths; volume mounts use host paths. The MCP tools handle
  this automatically — just call compose_up(build=True) or compose_build().

RULES:
- No Bash. Use ONLY the MCP tools above.
- When a command fails, read the error output carefully.
- Use docker_exec to investigate: pwd, ls, cat, env (inside containers)
- Fix the root cause, not the symptom.
- NEVER repeat a failed approach. Always try something new.
- NEVER say you can't fix it. You have all the tools.
- NEVER modify docker-compose.yml image names or build contexts — they are set by the project.
- NEVER use sed to change docker-compose.yml — use Edit tool if you must change a file.
- If an image doesn't exist, use compose_up with build=True or compose_build.
- If "port already allocated", find and stop the conflicting container first.

OUTPUT FORMAT:
- STATUS: <what you're doing now>
- DIAGNOSIS: <what's wrong and why>
- ACTION: <what you're fixing>
- FIXED: <tunnel_url or summary> — when everything works
"""


class RepairCard:
    """Single evolving card — same layout from start to finish."""

    def __init__(self, project_name: str, chat, chat_id: str, message_id: str):
        self.project_name = project_name
        self.chat = chat
        self.chat_id = chat_id
        self.message_id = message_id
        self.phase = "checking"
        self.status = "Starting..."
        self.activities: list[str] = []
        self.result_url: str | None = None
        self.start_time = time.time()
        self._attempt = 0

    def _render(self) -> str:
        elapsed = int(time.time() - self.start_time)
        header = f"🔧 *{self.project_name}*"

        if self.phase == "checking":
            phase_icon = "🔍"
        elif self.phase == "repairing":
            phase_icon = "🤖"
        elif self.phase == "done":
            phase_icon = "✅" if self.result_url else "⚠️"
        else:
            phase_icon = "🔄"

        bar_len = 10
        filled = min(bar_len, max(1, elapsed // 6))
        if self.phase == "done":
            filled = bar_len
        bar = "🟩" * filled + "⬜" * (bar_len - filled)

        attempt_str = f" (attempt {self._attempt})" if self._attempt > 1 else ""
        status_line = f"{phase_icon} {self.status}{attempt_str}"
        recent = self.activities[-3:]
        activity_lines = "\n".join(
            f"  {a}" if a.startswith("❌") or a.startswith("⚠") else f"  ✅ {a}"
            for a in recent
        )
        url_line = f"\n🔗 {self.result_url}" if self.result_url else ""

        return f"{header} `{elapsed}s`\n{bar}\n\n{status_line}\n{activity_lines}{url_line}".strip()

    async def render(self):
        if not self.chat:
            return
        try:
            if self.phase == "done":
                # Final state — no cancel button
                await self.chat.edit_message(self.chat_id, self.message_id, self._render())
            else:
                # Working state — show cancel button
                from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
                kb = ActionKeyboard(rows=[
                    ActionRow([ActionButton("⏹ Cancel", f"cancel_repair:{self.chat_id}:{self.message_id}", style="danger")]),
                ])
                await self.chat.edit_message_with_actions(
                    self.chat_id, self.message_id, self._render(), kb,
                )
        except Exception:
            pass

    async def set_status(self, status: str):
        self.status = status[:80]
        await self.render()

    async def complete_activity(self, activity: str):
        self.activities.append(activity[:60])
        await self.render()

    async def set_phase(self, phase: str, status: str = ""):
        self.phase = phase
        if status:
            self.status = status[:80]
        await self.render()


class _Cancelled(Exception):
    pass


async def _is_cancelled(chat_id: str, message_id: str) -> bool:
    """Check if user cancelled this repair via Redis flag."""
    try:
        from openclow.models import get_redis
        r = await get_redis()
        return bool(await r.get(f"cancel_repair:{chat_id}:{message_id}"))
    except Exception:
        return False


async def set_cancel_flag(chat_id: str, message_id: str):
    """Set cancellation flag in Redis (called by Slack handler)."""
    try:
        from openclow.models import get_redis
        r = await get_redis()
        await r.set(f"cancel_repair:{chat_id}:{message_id}", "1", ex=600)
    except Exception:
        pass


async def _run_single_agent_with_timeout(prompt, options, card, notify_fn, timeout_seconds=300):
    """Run one agent session with timeout + heartbeat + cancellation. Returns True if FIXED."""
    from claude_agent_sdk import query
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock

    fixed = False
    last_update = time.time()
    chat_id = card.chat_id if card else ""
    message_id = card.message_id if card else ""

    async def _heartbeat():
        """Update card every 5s. Check for cancellation."""
        nonlocal last_update
        while True:
            await asyncio.sleep(5)
            # Check cancellation
            if chat_id and message_id and await _is_cancelled(chat_id, message_id):
                raise _Cancelled()
            if card and (time.time() - last_update) > 5:
                await card.render()

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        async def _stream():
            nonlocal fixed, last_update
            async for message in query(prompt=prompt, options=options):
                if not isinstance(message, AssistantMessage):
                    continue
                for block in message.content:
                    last_update = time.time()
                    if isinstance(block, TextBlock):
                        for line in block.text.split("\n"):
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("STATUS:"):
                                text = line[7:].strip()[:60]
                                if card:
                                    await card.set_status(text)
                                elif notify_fn:
                                    await notify_fn(text)
                            elif line.startswith("FIXED:"):
                                fixed = True
                                text = line[6:].strip()[:60]
                                if card:
                                    await card.complete_activity(text)
                                elif notify_fn:
                                    await notify_fn(f"✅ {text}")
                    elif isinstance(block, ToolUseBlock):
                        from openclow.worker.tasks._agent_base import describe_tool
                        desc = describe_tool(block)
                        if card:
                            await card.set_status(desc)
                        elif notify_fn:
                            await notify_fn(desc)

        await asyncio.wait_for(_stream(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log.warning("repair_agent.timeout", timeout=timeout_seconds)
        if card:
            await card.complete_activity(f"Timed out at {timeout_seconds}s")
    except _Cancelled:
        log.info("repair_agent.cancelled_by_user")
        if card:
            await card.set_phase("done", "Cancelled by user")
        raise asyncio.CancelledError()  # Propagate up to stop all retries
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, _Cancelled):
            pass

    return fixed


async def run_repair_agent(
    prompt: str,
    workspace: str,
    chat,
    chat_id: str,
    message_id: str,
    status_lines: list[str],
    max_turns: int = 30,
    max_attempts: int = 3,
    timeout_per_attempt: int = 300,
    notify_fn=None,
    card: RepairCard | None = None,
) -> bool:
    """Run Claude agent with Docker MCP tools. NEVER gives up.

    - Timeout per attempt (default 120s) — no more hanging
    - Heartbeat keeps card alive during silence
    - Retries carry forward what was accomplished
    - If agent finishes without FIXED → restart with stronger prompt
    """
    from claude_agent_sdk import ClaudeAgentOptions
    from openclow.providers.llm.claude import _mcp_docker

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt=_SYSTEM_PROMPT,
        model="claude-sonnet-4-6",
        allowed_tools=[
            "Read", "Glob", "Grep",
            "mcp__docker__compose_build",
            "mcp__docker__compose_up",
            "mcp__docker__compose_ps",
            "mcp__docker__compose_down",
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
            "mcp__docker__tunnel_start",
            "mcp__docker__tunnel_stop",
            "mcp__docker__tunnel_get_url",
            "mcp__docker__tunnel_list",
        ],
        mcp_servers={"docker": _mcp_docker()},
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )

    original_prompt = prompt
    current_prompt = prompt
    # Track what was accomplished across retries
    accomplished: list[str] = []

    for attempt in range(1, max_attempts + 1):
        if card:
            card._attempt = attempt
            if attempt == 1:
                await card.set_status("Agent working...")
            else:
                await card.set_status(f"Retrying with new approach...")

        try:
            fixed = await _run_single_agent_with_timeout(
                current_prompt, options, card, notify_fn, timeout_per_attempt,
            )

            if fixed:
                return True

            # Agent finished or timed out without fixing — build context for retry
            if card:
                accomplished.extend(card.activities[-3:])

            if attempt < max_attempts:
                log.warning("repair_agent.not_fixed", attempt=attempt)
                context = "\n".join(f"- {a}" for a in accomplished[-5:]) if accomplished else "Nothing completed yet"
                current_prompt = (
                    f"PREVIOUS ATTEMPT {attempt} DID NOT FIX THE ISSUE.\n"
                    f"What was already tried:\n{context}\n\n"
                    f"Try a COMPLETELY DIFFERENT approach. Do NOT repeat anything above.\n"
                    f"Check current state first (compose_ps, container_logs), then fix.\n\n"
                    f"{original_prompt}"
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("repair_agent.crashed", attempt=attempt, error=str(e))
            if card:
                accomplished.append(f"Attempt {attempt} crashed")

            if attempt < max_attempts:
                current_prompt = (
                    f"PREVIOUS ATTEMPT CRASHED: {str(e)[:200]}\n"
                    f"What was already tried:\n"
                    + "\n".join(f"- {a}" for a in accomplished[-5:])
                    + f"\n\nTry a COMPLETELY DIFFERENT approach.\n\n{original_prompt}"
                )

    log.warning("repair_agent.exhausted", attempts=max_attempts)
    return False
