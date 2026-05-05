"""Shared agentic repair helper — Claude agent with Docker MCP tools.

NEVER gives up. Retries on crash with context. Timeout + heartbeat.
Always shows buttons at the end.
"""
from __future__ import annotations

import asyncio
import time
from taghdev.utils.logging import get_logger

log = get_logger()

_SYSTEM_PROMPT = """\
You are a senior DevOps engineer with full control over Docker infrastructure and Cloudflare tunnels via MCP tools.

## Available Tools

- compose_build — rebuild Docker images only (no start). Use after Dockerfile changes.
- compose_up — start a stack. Set build=True to rebuild images first.
  Docker-in-Docker path translation is handled automatically.
- compose_down / compose_ps — stop stack / list running containers
- container_logs / container_health — inspect container state and logs
- docker_exec — run any command inside a running container
- restart_container — restart a specific container
- list_containers — list all containers on the host
- tunnel_start / tunnel_stop / tunnel_get_url — manage Cloudflare tunnels (on WORKER HOST)
- Read / Edit / Glob / Grep — read and modify files on the host filesystem

## Critical Architecture

- Project apps run inside Docker containers managed by docker-compose
- Cloudflare tunnels run on the WORKER HOST, NOT inside project containers
- NEVER run "cloudflared" or "which cloudflared" inside a container — it does not exist there
- Use tunnel_start / tunnel_get_url / tunnel_stop MCP tools exclusively for tunnel management
- Docker-in-Docker: the worker runs in a container but accesses the host Docker socket via MCP
  Build contexts use container paths; volume mounts use host paths — MCP handles this automatically

## Rules

- No Bash. Use ONLY the MCP tools listed above.
- Always read container_logs BEFORE attempting any fix — understand the error first
- Use docker_exec to investigate inside containers: pwd, ls, cat files, check env vars
- NEVER run 'curl --unix-socket /run/docker.sock' or any Docker API call via docker_exec — the Docker socket is NOT mounted inside project containers and will hang forever
- Fix the root cause, not the symptom
- NEVER repeat a failed approach — try something completely different each time
- NEVER modify docker-compose.yml image names or build contexts — they are set by the project
- NEVER use sed on docker-compose.yml — use the Edit tool if you must modify a file
- If image doesn't exist: compose_up with build=True, or compose_build first
- If "port already allocated": list_containers to find the conflict, stop it, then retry
- If tunnel_start fails: check if another tunnel is running for the same project with tunnel_get_url first
- NEVER GIVE UP. The word "unfixable" does not exist. If one approach fails, try a completely different one. Keep going until fixed or all turns used.

## Auth / Private Packages

- If auth.json exists in the workspace root: it contains Composer authentication tokens for private packages (Nova, Spark, Packagist, etc.).
- BEFORE any composer install or Docker build that involves private packages: copy auth.json to ~/.composer/auth.json using docker_exec or the Edit tool.
- If a Docker build fails with HTTP 401 or 403 on a package URL: this is an auth failure. Apply auth.json and rebuild with compose_up(build=True).
- Command to apply inside a container: `cp /path/to/workspace/auth.json ~/.composer/auth.json`

## Workflow for Each Fix

1. Read state: container_health + container_logs — identify the specific error
2. Fix: edit files or use docker_exec to correct config inside container
3. Rebuild/restart: compose_up(build=True) if Dockerfile changed, restart_container otherwise
4. Verify: wait 8–10s, then container_health to confirm recovery
5. If tunnel needed: tunnel_get_url first — only call tunnel_start if no URL exists

## Output Format

Report each step with:
STATUS: <what you're doing now>
DIAGNOSIS: <specific root cause — not "config issue" but "missing env var DB_HOST">
ACTION: <what fix you applied>
FIXED: <tunnel_url or fix summary> — output this only when everything is confirmed working
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
        if hasattr(self.chat, "send_progress_card"):
            await self._emit_web_card()
            return
        # Telegram/Slack: existing text path (unchanged)
        try:
            if self.phase == "done":
                await self.chat.edit_message(self.chat_id, self.message_id, self._render())
            else:
                from taghdev.providers.actions import ActionButton, ActionKeyboard, ActionRow
                kb = ActionKeyboard(rows=[
                    ActionRow([ActionButton("⏹ Cancel", f"cancel_repair:{self.chat_id}:{self.message_id}", style="danger")]),
                ])
                await self.chat.edit_message_with_actions(
                    self.chat_id, self.message_id, self._render(), kb,
                )
        except Exception:
            pass

    async def _emit_web_card(self):
        """Publish a structured progress_card event for web chat."""
        elapsed = int(time.time() - self.start_time)
        # Map the 3 RepairCard phases to step list
        checking_done = self.phase in ("repairing", "done")
        repairing_done = self.phase == "done"
        steps = [
            {
                "name": "Check containers",
                "status": "done" if checking_done else "running",
                "detail": "",
            },
            {
                "name": "Repair",
                "status": (
                    "done" if repairing_done
                    else "running" if self.phase == "repairing"
                    else "pending"
                ),
                "detail": self.status if self.phase == "repairing" else "",
            },
            {
                "name": "Verify",
                "status": "done" if repairing_done else "pending",
                "detail": self.result_url or "",
            },
        ]
        attempt_str = f" (attempt {self._attempt})" if self._attempt > 1 else ""
        try:
            await self.chat.send_progress_card(self.chat_id, self.message_id, {
                "title": f"{self.project_name}{attempt_str}",
                "elapsed": elapsed,
                "overall_status": (
                    "done" if self.phase == "done" and self.result_url
                    else "failed" if self.phase == "done"
                    else "running"
                ),
                "steps": steps,
                "footer": self.result_url or "",
            })
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
    """Check if user cancelled this repair via Redis flag.

    Checks both the message-specific key (Telegram/Slack cancel button)
    and the session-level key set by the web Stop button.
    """
    try:
        from taghdev.models import get_redis
        from taghdev.settings import settings
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        # Message-specific key (Telegram/Slack inline cancel)
        val = await r.get(f"cancel_repair:{chat_id}:{message_id}")
        if val:
            await r.aclose()
            return True
        # Session-level key set by web Stop button: web:{user_id}:{session_id}
        if chat_id.startswith("web:"):
            parts = chat_id.split(":")
            if len(parts) == 3:
                session_id = parts[2]
                val2 = await r.get(f"taghdev:cancel_session:{session_id}")
                if val2:
                    await r.aclose()
                    return True
        await r.aclose()
        return False
    except Exception:
        return False


async def set_cancel_flag(chat_id: str, message_id: str):
    """Set cancellation flag in Redis (called by Slack handler)."""
    try:
        from taghdev.models import get_redis
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
                        from taghdev.worker.tasks._agent_base import describe_tool
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
    from taghdev.providers.llm.claude import _mcp_docker

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt=_SYSTEM_PROMPT,
        model="claude-sonnet-4-6",
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
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
