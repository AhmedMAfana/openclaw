"""Main orchestrator pipeline — the heart of OpenClow.

Interactive flow:
1. Analyze project → create plan → send to user for approval
2. User approves plan → agent codes step by step with progress updates
3. Reviewer checks quality → fixes if needed
4. Send summary + diff → user approves → create PR
"""
import asyncio
import os
import re
import time
import uuid

from slugify import slugify
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from openclow.models import Task, TaskLog, async_session
from openclow.providers import factory
from openclow.providers.base import ProviderMismatchError
from openclow.services.workspace_service import WorkspaceService
from openclow.settings import settings
from openclow.utils.logging import get_logger
from openclow.worker.tasks import git_ops

log = get_logger()


async def _make_cancel_watcher(chat_id: str) -> "asyncio.Task | None":
    """Spawn a background task that watches the Redis cancel key for web sessions.

    When the user clicks Stop, the cancel endpoint sets
    openclow:cancel_session:{session_id}. This watcher detects it every 5s
    and cancels the current arq task, which raises CancelledError into the
    orchestrator's main coroutine (caught by existing except handlers).

    Returns the watcher Task (caller should cancel it in finally).
    Returns None for non-web chat_ids.
    """
    if not chat_id or not chat_id.startswith("web:"):
        return None
    parts = chat_id.split(":")
    if len(parts) != 3:
        return None
    session_id = parts[2]
    outer_task = asyncio.current_task()

    async def _watcher():
        try:
            import redis.asyncio as aioredis
            from openclow.settings import settings as _s
            while True:
                await asyncio.sleep(5)
                try:
                    r = aioredis.from_url(_s.redis_url)
                    val = await r.get(f"openclow:cancel_session:{session_id}")
                    await r.aclose()
                    if val is not None and outer_task and not outer_task.done():
                        log.info("orchestrator.cancel_detected", session_id=session_id)
                        outer_task.cancel()
                        return
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass  # watcher itself was cancelled in finally block

    return asyncio.create_task(_watcher())


async def _check_claude_auth() -> bool:
    """Check if Claude is authenticated. Returns True if OK."""
    try:
        import json as _json
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        status = _json.loads(out.decode())
        return status.get("loggedIn", False)
    except Exception as e:
        log.warning("claude.auth_check_failed", error=str(e))
        return False  # If check fails, assume not authenticated


async def _get_chat_for_task(task: Task):
    """Get the chat provider that matches the task's originating platform."""
    ptype = task.chat_provider_type or "telegram"
    try:
        return await factory.get_chat_by_type(ptype)
    except ValueError:
        raise ProviderMismatchError(
            f"Task was created on {ptype} but that provider is not configured. "
            f"Add {ptype} config via Settings Dashboard → Chat."
        )


# Valid status transitions — used to guard against out-of-order execution
_VALID_ENTRY_STATUS = {
    "execute_task": {"pending", "preparing"},
    "execute_plan": {"plan_review", "coding"},  # "coding" = quick mode entry point
    "approve_task": {"diff_preview"},
    "merge_task": {"awaiting_approval"},
    "reject_task": {"awaiting_approval"},
    "discard_task": {"diff_preview", "plan_review"},
}


async def _get_task(task_id: str) -> Task:
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .options(selectinload(Task.project), selectinload(Task.user))
            .where(Task.id == uuid.UUID(task_id))
        )
        task = result.scalar_one_or_none()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        # Expunge so the object can be used after session closes
        session.expunge(task)
        return task


async def _update_task(task_id: str, **kwargs):
    async with async_session() as session:
        await session.execute(
            update(Task).where(Task.id == uuid.UUID(task_id)).values(**kwargs)
        )
        await session.commit()


async def _log_to_db(task_id: str, agent: str, level: str, message: str, metadata: dict | None = None):
    async with async_session() as session:
        entry = TaskLog(
            task_id=uuid.UUID(task_id),
            agent=agent, level=level, message=message, metadata_=metadata,
        )
        session.add(entry)
        await session.commit()


def _parse_plan_steps(plan_text: str) -> list[str]:
    """Extract numbered steps from plan text."""
    steps = []
    for line in plan_text.split("\n"):
        line = line.strip()
        match = re.match(r"^\d+[\.\)]\s+(.+)$", line)
        if match:
            steps.append(match.group(1))
    # Fallback: if no numbered steps found, count substantive lines
    if not steps:
        for line in plan_text.split("\n"):
            line = line.strip()
            if line and len(line) > 10 and not line.startswith("#") and not line.startswith("---"):
                steps.append(line[:80])
    return steps[:20]  # Cap at 20 steps


async def _run_deploy_agent(task, workspace_path: str, diff_summary: str, tunnel_url: str | None) -> str:
    """Agent-driven post-task deploy: looks at the diff, decides what actions to take.

    The agent decides — not regex. It might:
    - Rebuild frontend (npm run build)
    - Run migrations (php artisan migrate)
    - Clear caches
    - Run seeders
    - Verify via curl
    - Restart containers
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock
    except ImportError:
        return "deploy agent unavailable"

    from openclow.providers.llm.claude import _mcp_docker

    compose_project = f"openclow-{task.project.name}"
    app_container = task.project.app_container_name or "app"
    app_container_full = f"{compose_project}-{app_container}-1"
    project_workspace = os.path.join(settings.workspace_base_path, "_cache", task.project.name)

    prompt = f"""Deploy the changes for {task.project.name} to the running app.

## Changed Files (diff summary)

{diff_summary}

## Environment

Project: {task.project.name} ({task.project.tech_stack or 'Unknown'})
App container: {app_container_full}
Compose project: {compose_project}
Task workspace (source): {workspace_path}
Project workspace (destination): {project_workspace}
Tunnel URL: {tunnel_url or 'none'}

## Steps

### Step 1: Sync files
Copy changed files from task workspace ({workspace_path}) to project workspace ({project_workspace}).
Only copy files that appear in the diff — do not overwrite unrelated files.

### Step 2: Run post-deploy actions based on what changed

| Changed files include... | Action |
|---|---|
| database/migrations/ or alembic/versions/ or *_migration.* | Run migrations via docker_exec |
| resources/js/ or resources/css/ or *.vue or *.tsx or *.jsx | npm run build in project workspace |
| config/ or .env (non-secret keys only) | Clear config/route cache via docker_exec |
| database/seeders/ | Run seeders only if idempotent (--force or equivalent) |
| Only backend PHP/Python/Go files | Check container_health; restart_container if unhealthy |
| Nothing actionable | Sync is enough — skip post-deploy steps |

### Step 3: Verify the app works
docker_exec("{app_container_full}", "curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{task.project.app_port or 80}")
- 200–399: success
- 500: app crashed — read container_logs and fix if possible
- Connection refused: restart_container, wait 5s, re-verify

### Step 4: Report

End with one of:
DEPLOY_RESULT: [what you did — e.g. "synced 3 files, ran migrations, verified 200 OK"]
DEPLOY_BLOCKED: [what failed and why — e.g. "migration failed: duplicate column"]"""

    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt=(
            f"You are a deploy specialist for {task.project.name}. Sync changed files, run required post-deploy steps based on what changed, verify the app responds. Be fast and surgical."
            " NEVER run 'curl --unix-socket /run/docker.sock' or any raw Docker API call via docker_exec inside a project container — the socket is NOT mounted there and will hang forever. Use ONLY MCP tools for all Docker operations."
        ),
        model="claude-sonnet-4-6",
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
            "mcp__docker__docker_exec",
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__restart_container",
            "mcp__docker__tunnel_get_url",
        ],
        mcp_servers={"docker": _mcp_docker()},
        permission_mode="bypassPermissions",
        max_turns=15,
    )

    result = "deployed"
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and "DEPLOY_RESULT:" in block.text:
                        result = block.text.split("DEPLOY_RESULT:", 1)[1].strip()[:200]
    except Exception as e:
        log.warning("orchestrator.deploy_agent_failed", error=str(e))
        result = f"deploy error: {str(e)[:100]}"

    return result


def _extract_summary(agent_output: str) -> str:
    """Extract DONE_SUMMARY from agent output."""
    if "DONE_SUMMARY:" in agent_output:
        parts = agent_output.split("DONE_SUMMARY:", 1)
        return parts[1].strip()[:2000]
    return ""


def _main_menu_keyboard():
    """Rich next-action keyboard for terminal states."""
    from openclow.providers.actions import terminal_keyboard
    return terminal_keyboard()


def _retry_keyboard(project_id: int | None = None):
    """Retry + main menu keyboard for interrupted tasks."""
    from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
    rows = [
        ActionRow([ActionButton("🔄 Retry", "menu:task")]),
        ActionRow([
            ActionButton("📋 View Logs", "menu:logs"),
            ActionButton("◀️ Main Menu", "menu:main"),
        ]),
    ]
    return ActionKeyboard(rows=rows)


HEALTH_GUARD_PROMPT = """You are a DevOps health guard for "{project_name}". Your job: ensure all containers are running and the app is responding before the coder starts work.

Project: {project_name} ({tech_stack})
Compose project: openclow-{project_name}
App container pattern: *{app_container}*
App port: {app_port}
Workspace: {workspace}

## Workflow

### Fast path (do this first)
1. container_health on the app container
2. If HEALTHY: tunnel_get_url("{project_name}") — get the URL
3. End immediately: HEALTHY: <tunnel_url or "no tunnel">

### Slow path (only if something is broken)
If container is down or unhealthy:
a. container_logs — read the last 50 lines, identify the specific error
b. Apply ONE targeted fix (edit .env, fix config, restart_container)
c. container_health again to verify recovery
d. If still broken: compose_up to rebuild and restart the service
e. Verify HTTP: docker_exec on app container: curl -s -o /dev/null -w '%{{http_code}}' localhost:{app_port}

If HTTP returns 500:
- Read container_logs again — app may have crashed after start
- Common causes: missing .env key, DB not ready, supervisor not started
- docker_exec to diagnose: check supervisord, check process list (ps aux)
- Fix root cause and restart

If no tunnel or tunnel is dead:
- tunnel_get_url("{project_name}") first — don't create a duplicate
- If none found: tunnel_start("{project_name}", "http://localhost:{app_port}")

## Rules

- If healthy on first check: respond in 1–2 turns max, don't do unnecessary work
- Only dig deeper if something is actually broken
- Never give up — try a different approach when a fix fails
- End with:
  HEALTHY: <tunnel_url or "no tunnel">
  or
  UNHEALTHY: <specific error still blocking>
"""


async def _ensure_project_healthy(task, reporter, task_id_str: str) -> tuple[bool, str | None]:
    """Single LLM agent call: check health, diagnose, repair, tunnel — all in one."""
    project = task.project
    if not project.is_dockerized:
        return True, None

    workspace = f"{settings.workspace_base_path}/_cache/{project.name}"

    prompt = HEALTH_GUARD_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        app_container=project.app_container_name or "app",
        app_port=project.app_port or 80,
        workspace=workspace,
    )

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
        from openclow.providers.llm.claude import _mcp_docker

        options = ClaudeAgentOptions(
            cwd=workspace,
            system_prompt=(
                "You are a DevOps health guard. Check container health first — if healthy, get tunnel URL and stop immediately. Only investigate further if something is broken. Fix root causes, not symptoms."
                " NEVER run 'curl --unix-socket /run/docker.sock' or any raw Docker API call via docker_exec inside a project container — the socket is NOT mounted there and will hang forever. Use ONLY MCP tools for all Docker operations."
            ),
            model="claude-sonnet-4-6",
            allowed_tools=[
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
                "Read", "Edit", "Glob",
            ],
            mcp_servers={"docker": _mcp_docker()},
            permission_mode="bypassPermissions",
            max_turns=15,
        )

        full_output = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text
                    elif isinstance(block, ToolUseBlock):
                        from openclow.worker.tasks._agent_base import describe_tool
                        await reporter.log(describe_tool(block))

        # Parse result
        healthy = "HEALTHY:" in full_output
        tunnel_url = None

        # Extract tunnel URL from output
        import re
        url_match = re.search(r"(https://[a-z0-9-]+\.trycloudflare\.com)", full_output)
        if url_match:
            tunnel_url = url_match.group(1)

        if healthy:
            await reporter.log("💚 Project healthy")
        else:
            await reporter.log("⚠️ Issues remain — coder will try")

        if tunnel_url:
            await reporter.log(f"🌐 {tunnel_url}")

        return healthy, tunnel_url

    except Exception as e:
        log.warning("orchestrator.health_guard_failed", error=str(e))
        await reporter.log("⚠️ Health guard failed — proceeding anyway")
        # Fallback: just get tunnel URL from DB
        try:
            from openclow.services.tunnel_service import get_tunnel_url
            tunnel_url = await get_tunnel_url(project.name)
        except Exception:
            tunnel_url = None
        return False, tunnel_url


async def execute_task(ctx: dict, task_id: str, skip_planning: bool = False):
    """Phase 1: Analyze project and create plan, or skip to coding if quick mode."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    start_time = time.time()

    # Pre-check Claude auth
    if not await _check_claude_auth():
        try:
            chat = await _get_chat_for_task(task)
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔑 Authenticate Claude", "claude_auth")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await chat.edit_message_with_actions(
                task.chat_id, task.chat_message_id,
                "🔑 Claude Authentication Required\n\n"
                "Your Claude session has expired or is not authenticated. "
                "Please sign in to continue.\n\n"
                "After authenticating, you can retry your task.",
                kb,
            )
            await _update_task(task_id_str, status="failed", error_message="Claude auth expired - re-authentication required")
            await chat.close()
        except Exception as e:
            log.error("orchestrator.auth_check_failed", error=str(e))
        return

    llm = await factory.get_llm()
    try:
        chat = await _get_chat_for_task(task)
    except ProviderMismatchError as e:
        await _update_task(task_id_str, status="orphaned", error_message=str(e))
        return
    ws = WorkspaceService()

    # ── Web cancel watcher — detects Stop button and cancels this task ──
    _cancel_watcher = await _make_cancel_watcher(task.chat_id or "")

    # ── Acquire project lock (prevent concurrent tasks on same repo) ──
    from openclow.services.project_lock import acquire_project_lock, get_lock_holder
    lock = await acquire_project_lock(task.project_id, task_id=task_id_str, wait=10)
    if lock is None:
        holder = await get_lock_holder(task.project_id)
        await _update_task(task_id_str, status="failed",
                           error_message=f"Project busy — another task is running ({holder})")

        # Send interactive message with action buttons instead of plain text
        try:
            if task.chat_provider_type == "slack":
                from openclow.providers.chat.slack import blocks
                blks = blocks.project_busy_blocks(holder)
                await chat.edit_message_blocks(task.chat_id, task.chat_message_id, blks)
            else:
                # Telegram: use inline keyboard
                from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
                kb = ActionKeyboard(rows=[
                    ActionRow([ActionButton("🔄 Retry Now", "retry_task")]),
                    ActionRow([ActionButton("⏸️ View Task", f"view_task:{task_id_str}")]),
                    ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
                ])
                await chat.edit_message_with_actions(
                    task.chat_id, task.chat_message_id,
                    f"⏳ *Project is busy*\n\nAnother task is running: `{holder or 'unknown'}`\n\n"
                    f"Wait for it to finish or retry in a moment.", kb)
        except Exception as e:
            log.warning("orchestrator.busy_message_failed", error=str(e))
            await chat.edit_message(task.chat_id, task.chat_message_id,
                                    f"Project is busy. Another task ({holder or 'unknown'}) is already running.\n"
                                    f"Wait for it to finish or use /cancel.")

        await chat.close()
        return

    log.info("orchestrator.started", task_id=task_id_str, project=task.project.name)

    # Web tasks created via trigger_task MCP have no chat_message_id — create one now
    chat_message_id = task.chat_message_id
    if not chat_message_id:
        chat_message_id = await chat.send_message(task.chat_id, "")
        await _update_task(task_id_str, chat_message_id=chat_message_id)

    from openclow.services.checklist_reporter import ChecklistReporter
    reporter = ChecklistReporter(chat, task.chat_id, chat_message_id,
                                 title=task.description[:50])
    reporter.set_steps(["Prepare workspace", "Quick mode" if skip_planning else "Create plan"])
    await reporter.start()

    try:
        # ── Step 0: Prepare workspace ──
        await _update_task(task_id_str, status="preparing")
        await reporter.start_step(0)

        workspace = await ws.prepare(task.project, task_id_str)

        # Create branch
        branch_slug = slugify(task.description, max_length=50)
        branch_name = f"openclow/{task_id_str[:8]}-{branch_slug}"
        await git_ops.create_branch(workspace.path, branch_name)
        await _update_task(task_id_str, branch_name=branch_name)
        await reporter.complete_step(0, f"branch: {branch_name[:30]}")

        await _log_to_db(task_id_str, "system", "info", f"Branch: {branch_name}")

        if skip_planning:
            # ── Quick mode: skip planning, go straight to coding ──
            await reporter.start_step(1)
            await _update_task(task_id_str, status="coding")
            await reporter.complete_step(1, "dispatching to coder")
            await reporter.stop()

            # Auto-dispatch execute_plan immediately
            from openclow.worker.arq_app import get_arq_pool
            pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
            await pool.enqueue_job("execute_plan", task_id_str)
            log.info("orchestrator.quick_mode", task_id=task_id_str)
        else:
            # ── Full mode: Analyze and create plan ──
            await _update_task(task_id_str, status="planning")
            await reporter.start_step(1)

            plan_text = await llm.run_planner(
                workspace_path=workspace.path,
                task_description=task.description,
                project_name=task.project.name,
                tech_stack=task.project.tech_stack or "",
                description=task.project.description or "",
                agent_system_prompt=task.project.agent_system_prompt or "",
            )
            await reporter.complete_step(1, "plan ready")

            await _log_to_db(task_id_str, "planner", "info", "Plan created", {
                "plan": plan_text[:3000],
            })

            # ── Send plan to user for approval ──
            await reporter.stop()
            await _update_task(task_id_str, status="plan_review")
            await chat.send_plan_preview(
                task.chat_id, task.chat_message_id, task_id_str, plan_text,
            )
            # Pipeline pauses here — continues when user clicks [Approve Plan]

    except (asyncio.CancelledError, TimeoutError) as e:
        error_msg = "Task timed out" if isinstance(e, TimeoutError) else "Task was cancelled"
        log.warning("orchestrator.planning_interrupted", task_id=task_id_str, reason=error_msg)
        await _update_task(task_id_str, status="failed",
                           error_message=error_msg,
                           duration_seconds=int(time.time() - start_time))
        try:
            running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
            await reporter.fail_step(running_idx, error_msg[:40])
            reporter._footer = f"{error_msg}. You can retry."
            await reporter._force_render(keyboard=_retry_keyboard(task.project_id if task else None))
        except Exception:
            pass
        try:
            await ws.cleanup(task_id_str)
        except Exception:
            pass
    except Exception as e:
        duration = int(time.time() - start_time)
        log.error("orchestrator.planning_failed", task_id=task_id_str, error=str(e))
        await _update_task(task_id_str, status="failed",
                           error_message=str(e), duration_seconds=duration)
        try:
            running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
            await reporter.fail_step(running_idx, str(e)[:40])
            reporter._footer = str(e)[:200]
            await reporter._force_render(keyboard=_main_menu_keyboard())
        except Exception:
            pass
        try:
            await ws.cleanup(task_id_str)
        except Exception as cleanup_err:
            log.error("orchestrator.cleanup_failed", task_id=task_id_str, error=str(cleanup_err))
    finally:
        if _cancel_watcher and not _cancel_watcher.done():
            _cancel_watcher.cancel()
        await reporter.stop()
        # Release lock — user is now reviewing the plan (no repo access needed)
        if lock:
            await lock.release()
        await chat.close()


async def execute_plan(ctx: dict, task_id: str):
    """Phase 2: User approved plan → code it, review it, send summary."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)

    # Guard: only proceed if task is in the right state
    valid = _VALID_ENTRY_STATUS.get("execute_plan", set())
    if task.status not in valid:
        log.warning("orchestrator.invalid_status", task_id=task_id_str,
                    expected=valid, actual=task.status)
        return

    start_time = time.time()

    # Pre-check Claude auth before coding
    if not await _check_claude_auth():
        try:
            chat = await _get_chat_for_task(task)
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔑 Authenticate Claude", "claude_auth")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await chat.edit_message_with_actions(
                task.chat_id, task.chat_message_id,
                "🔑 Claude Authentication Required\n\n"
                "Your Claude session expired while waiting for approval. "
                "Please sign in to continue with the implementation.\n\n"
                "After authenticating, you can re-approve the plan.",
                kb,
            )
            await _update_task(task_id_str, status="failed", error_message="Claude auth expired during plan approval")
            await chat.close()
        except Exception as e:
            log.error("orchestrator.auth_check_failed_execute_plan", error=str(e))
        return

    llm = await factory.get_llm()
    try:
        chat = await _get_chat_for_task(task)
    except ProviderMismatchError as e:
        await _update_task(task_id_str, status="orphaned", error_message=str(e))
        return
    ws = WorkspaceService()

    # ── Web cancel watcher — detects Stop button and cancels this task ──
    _cancel_watcher = await _make_cancel_watcher(task.chat_id or "")

    # Re-acquire project lock for coding phase — wait with backoff
    from openclow.services.project_lock import acquire_project_lock, get_lock_holder
    lock = None
    for _wait_secs in [10, 30, 60]:
        lock = await acquire_project_lock(task.project_id, task_id=task_id_str, wait=_wait_secs)
        if lock:
            break
        holder = await get_lock_holder(task.project_id)
        await chat.edit_message(task.chat_id, task.chat_message_id,
                                f"⏳ Waiting for another task to finish ({holder})...")
    if lock is None:
        holder = await get_lock_holder(task.project_id)
        await _update_task(task_id_str, status="failed",
                           error_message=f"Project busy — lock held by {holder}")
        await chat.edit_message(task.chat_id, task.chat_message_id,
                                f"Cannot start coding — project is locked by another task ({holder}).")
        await chat.close()
        return

    workspace_path = ws.get_path(task_id_str)

    # Get the plan from task_logs
    plan_text = ""
    async with async_session() as session:
        result = await session.execute(
            select(TaskLog).where(
                TaskLog.task_id == uuid.UUID(task_id_str),
                TaskLog.agent == "planner",
            ).order_by(TaskLog.created_at.desc()).limit(1)
        )
        plan_log = result.scalar_one_or_none()
        if plan_log and plan_log.metadata_:
            plan_text = plan_log.metadata_.get("plan", "")

    # Ensure we have a real message_id (web tasks may still be None here)
    chat_message_id = task.chat_message_id
    if not chat_message_id:
        chat_message_id = await chat.send_message(task.chat_id, "")
        await _update_task(task_id_str, chat_message_id=chat_message_id)

    from openclow.services.checklist_reporter import ChecklistReporter
    reporter = ChecklistReporter(chat, task.chat_id, chat_message_id,
                                 title=task.description[:50])
    reporter.set_steps(["Check health", "Implement", "Review", "Deploy"])
    await reporter.start()

    try:
        # ── Fast container guard — catch dead projects before the LLM health agent ──
        # The LLM health guard repairs unhealthy containers but CANNOT bootstrap from scratch.
        # If there are zero containers, save 2-3 minutes of wasted agent turns and tell
        # the user to bootstrap first.
        if task.project.is_dockerized:
            from openclow.services.health_service import find_project_containers
            _existing = await find_project_containers(task.project.name)
            if not _existing:
                await reporter.start_step(0)
                await reporter.fail_step(0, "no containers")
                for _i in [1, 2, 3]:
                    await reporter.skip_step(_i, "skipped")
                await _update_task(
                    task_id_str, status="failed",
                    error_message="No containers running — bootstrap the project first",
                )
                from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
                kb = ActionKeyboard(rows=[
                    ActionRow([ActionButton("🔄 Bootstrap Project", f"project_bootstrap:{task.project_id}")]),
                    ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
                ])
                reporter._footer = "❌ No containers — bootstrap the project first, then retry this task"
                await reporter._force_render(keyboard=kb)
                log.warning("orchestrator.no_containers_abort",
                            task_id=task_id_str, project=task.project.name)
                return

        # ── Pre-flight: smart health check + repair + tunnel ──
        await _update_task(task_id_str, status="coding")
        await reporter.start_step(0)

        _healthy, tunnel_url_for_display = await _ensure_project_healthy(
            task, reporter, task_id_str,
        )
        await reporter.complete_step(0, "healthy" if _healthy else "proceeding")

        # ── Step 1: Run Coder Agent with plan ──
        await reporter.start_step(1)

        turn_count = 0
        current_step = 0
        last_diff_size = 0
        stall_count = 0
        last_tool_turn = 0
        write_tool_seen = False
        full_output = ""

        async for message in llm.run_coder(
            workspace_path=workspace_path,
            task_description=task.description,
            project_name=task.project.name,
            tech_stack=task.project.tech_stack or "",
            description=task.project.description or "",
            agent_system_prompt=task.project.agent_system_prompt or "",
            max_turns=0,
            plan=plan_text,
            app_container_name=task.project.app_container_name,
            app_port=task.project.app_port,
        ):
            turn_count += 1

            # Track agent text output for STEP_DONE markers
            from claude_agent_sdk.types import AssistantMessage, TextBlock
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text

            # Tool use progress — show active tool in the Implement step detail
            tool_name = llm.is_tool_use(message)
            if tool_name:
                last_tool_turn = turn_count
                if tool_name in ("Edit", "Write"):
                    write_tool_seen = True
                from openclow.worker.tasks._agent_base import describe_tool
                tool_desc = tool_name
                if hasattr(message, 'content'):
                    for block in message.content:
                        if hasattr(block, 'name') and hasattr(block, 'input'):
                            tool_desc = describe_tool(block)
                            break
                await reporter.update_step(1, tool_desc[:50])

            # Stall detection — tracks tool activity, not just git diff
            # Check every 10 turns starting at turn 20 (catch stalls earlier)
            if turn_count % 10 == 0 and turn_count > 20:
                diff_size = await git_ops.diff_size(workspace_path)
                tools_active = (turn_count - last_tool_turn) < 10

                if diff_size != last_diff_size or write_tool_seen:
                    # Agent is producing file changes — productive
                    if stall_count > 0:
                        log.info("orchestrator.stall_reset", task_id=task_id_str,
                                 turn=turn_count, new_diff_size=diff_size)
                    stall_count = 0
                    last_diff_size = diff_size
                    write_tool_seen = False
                elif tools_active:
                    # No file changes but agent is using tools (reading/exploring)
                    stall_count += 1
                    log.info("orchestrator.exploring", task_id=task_id_str,
                             turn=turn_count, stall_score=stall_count)
                else:
                    # No file changes AND no tool activity — real stall
                    stall_count += 2
                    log.warning("orchestrator.stall_warning", task_id=task_id_str,
                                turn=turn_count, stall_count=stall_count)

                if stall_count >= 4:
                    log.error("orchestrator.agent_stalled", task_id=task_id_str,
                              turns=turn_count, last_diff_size=last_diff_size)
                    raise RuntimeError(
                        f"Agent stalled — no meaningful activity for ~{turn_count} turns. "
                        f"The task may need clarification."
                    )

            result_turns = llm.is_result(message)
            if result_turns is not None:
                turn_count = result_turns

        await reporter.complete_step(1, f"{turn_count} turns")
        await _log_to_db(task_id_str, "coder", "info",
                         f"Coding complete. Turns: {turn_count}")
        await _update_task(task_id_str, agent_turns=turn_count)

        # ── Step 2: Run Reviewer ──
        await _update_task(task_id_str, status="reviewing")
        await reporter.start_step(2)

        review_result = await llm.run_reviewer(
            workspace_path=workspace_path,
            task_description=task.description,
            project_name=task.project.name,
            tech_stack=task.project.tech_stack or "",
            max_turns=0,
            description=task.project.description or "",
            agent_system_prompt=task.project.agent_system_prompt or "",
        )
        await _log_to_db(task_id_str, "reviewer", "info",
                         f"Review: {'ISSUES' if review_result.has_issues else 'APPROVED'}")

        # Fix loop
        if review_result.has_issues:
            for retry in range(2):
                await reporter.update_step(2, f"fixing issues (attempt {retry + 1})")
                async for _ in llm.run_coder_fix(
                    workspace_path=workspace_path,
                    task_description=task.description,
                    project_name=task.project.name,
                    tech_stack=task.project.tech_stack or "",
                    description=task.project.description or "",
                    agent_system_prompt=task.project.agent_system_prompt or "",
                    issues=review_result.issues,
                    max_turns=10,  # Fixes should be quick
                    app_container_name=task.project.app_container_name,
                    app_port=task.project.app_port,
                ):
                    pass
                review_result = await llm.run_reviewer(
                    workspace_path=workspace_path,
                    task_description=task.description,
                    project_name=task.project.name,
                    tech_stack=task.project.tech_stack or "",
                    max_turns=0,
                    description=task.project.description or "",
                    agent_system_prompt=task.project.agent_system_prompt or "",
                )
                if not review_result.has_issues:
                    break
        await reporter.complete_step(2, "approved" if not review_result.has_issues else "fixed")

        # ── Step 3: Stage changes + send summary ──
        await git_ops.add_all(workspace_path)
        diff_summary = await git_ops.diff_stat(workspace_path)

        if not diff_summary.strip() and not getattr(task, '_empty_diff_retried', False):
            # First attempt produced no changes — retry with stronger prompt
            task._empty_diff_retried = True
            log.warning("orchestrator.no_changes_retrying", task_id=task_id_str,
                       turns=turn_count, output_length=len(full_output))
            await reporter.start_step(1)
            await reporter.update_step(1, "retrying — no changes detected")

            # Reset git state for clean retry
            await git_ops.reset_hard(workspace_path)

            retry_turn_count = 0
            async for message in llm.run_coder(
                workspace_path=workspace_path,
                task_description=(
                    f"IMPORTANT: Your previous attempt made NO file changes in {turn_count} turns.\n"
                    f"You MUST make actual edits to solve this task. Read the codebase carefully, then EDIT files.\n"
                    f"Do NOT just read and explore — make real modifications.\n\n"
                    f"Original task: {task.description}"
                ),
                project_name=task.project.name,
                tech_stack=task.project.tech_stack or "",
                description=task.project.description or "",
                agent_system_prompt=task.project.agent_system_prompt or "",
                max_turns=0,
                plan=plan_text,
                app_container_name=task.project.app_container_name,
                app_port=task.project.app_port,
            ):
                tool_name = llm.is_tool_use(message)
                if tool_name:
                    from openclow.worker.tasks._agent_base import describe_tool
                    tool_desc = tool_name
                    if hasattr(message, 'content'):
                        for block in message.content:
                            if hasattr(block, 'name') and hasattr(block, 'input'):
                                tool_desc = describe_tool(block)
                                break
                    await reporter.update_step(1, tool_desc[:50])
                retry_turn_count += 1

            # Re-check diff after retry
            await git_ops.add_all(workspace_path)
            diff_summary = await git_ops.diff_stat(workspace_path)
            turn_count += retry_turn_count

        if not diff_summary.strip():
            # Retried and still no changes — now fail with detailed message
            log.warning("orchestrator.no_changes_after_retry", task_id=task_id_str,
                       turns=turn_count, output_length=len(full_output))

            await _update_task(task_id_str, status="failed",
                               error_message="Agent made no changes after retry",
                               agent_turns=turn_count)

            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry Task", "menu:task")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await reporter.error(
                "⚠️ No Changes Detected\n\n"
                "The agent completed two attempts but didn't modify any files.\n"
                "Try rephrasing the task with more specific details.",
                keyboard=kb
            )
            return

        # ── Step 3: Agent-driven deploy — let the LLM decide what to do ──
        await reporter.start_step(3)
        # Use tunnel URL from pre-flight health check (already verified)
        tunnel_url = tunnel_url_for_display

        deploy_result = await _run_deploy_agent(
            task, workspace_path, diff_summary, tunnel_url,
        )

        # Refresh tunnel URL (deploy agent may have restarted it)
        try:
            from openclow.services.tunnel_service import get_tunnel_url
            refreshed = await get_tunnel_url(task.project.name)
            if refreshed:
                tunnel_url = refreshed
        except Exception:
            pass

        await reporter.complete_step(3, deploy_result[:40] if deploy_result else "done")

        duration = int(time.time() - start_time)
        await _update_task(task_id_str, status="diff_preview", duration_seconds=duration)

        # Extract summary from agent output
        summary = _extract_summary(full_output)
        if not summary:
            summary = f"Task completed in {turn_count} turns, {duration}s"

        # Verify tunnel is actually reachable before showing it
        if tunnel_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5, follow_redirects=True) as hc:
                    probe = await hc.get(tunnel_url)
                    if probe.status_code < 502:
                        summary += f"\n\n🌐 Review: {tunnel_url}"
                    else:
                        summary += "\n\n⚠️ App may not be responding — check container health"
            except Exception:
                summary += "\n\n⚠️ Could not verify app — tunnel may be down"

        await reporter.stop()
        await chat.send_summary(
            task.chat_id, task.chat_message_id, task_id_str,
            summary, diff_summary,
        )
        await _log_to_db(task_id_str, "system", "info",
                         f"Summary sent. Duration: {duration}s")

    except (asyncio.CancelledError, TimeoutError) as e:
        error_msg = "Task timed out" if isinstance(e, TimeoutError) else "Task was cancelled"
        log.warning("orchestrator.coding_interrupted", task_id=task_id_str, reason=error_msg)
        await _update_task(task_id_str, status="failed",
                           error_message=error_msg,
                           duration_seconds=int(time.time() - start_time))
        try:
            running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
            await reporter.fail_step(running_idx, error_msg[:40])
            reporter._footer = f"{error_msg}. You can retry."
            await reporter._force_render(keyboard=_retry_keyboard(task.project_id if task else None))
        except Exception:
            pass
    except Exception as e:
        error_str = str(e).lower()

        from openclow.worker.tasks._agent_base import is_auth_error
        running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)

        if is_auth_error(e):
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔑 Authenticate Claude", "claude_auth")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await reporter.fail_step(running_idx, "auth expired")
            reporter._footer = "🔑 Claude session expired. Please authenticate to continue."
            await reporter._force_render(keyboard=kb)
            await _update_task(task_id_str, status="failed",
                               error_message="Claude auth expired - re-authentication required")

        elif "stalled" in error_str or "no progress" in error_str:
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry Task", "menu:task")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await reporter.fail_step(running_idx, "stalled")
            reporter._footer = "Agent stopped making progress. Try rephrasing the task."
            await reporter._force_render(keyboard=kb)
            await _update_task(task_id_str, status="failed",
                               error_message=str(e), duration_seconds=int(time.time() - start_time))

        else:
            duration = int(time.time() - start_time)
            raw = str(e)
            if "exit code" in raw and "Check stderr" in raw:
                stderr = getattr(e, "stderr", None) or getattr(e, "output", None)
                if stderr:
                    raw = f"Agent crashed: {str(stderr)[:400]}"
                else:
                    raw = "Agent crashed during execution. Check worker logs for details."
            log.error("orchestrator.coding_failed", task_id=task_id_str, error=str(e))
            await _update_task(task_id_str, status="failed",
                               error_message=raw, duration_seconds=duration)
            await reporter.fail_step(running_idx, raw[:40])
            reporter._footer = raw[:200]
            await reporter._force_render(keyboard=_retry_keyboard(task.project_id if task else None))
        try:
            git_status = await git_ops.status(workspace_path)
            await _log_to_db(task_id_str, "system", "error", str(e),
                             {"git_status": git_status})
        except Exception:
            await _log_to_db(task_id_str, "system", "error", str(e))
        try:
            await ws.cleanup(task_id_str)
        except Exception as cleanup_err:
            log.error("orchestrator.cleanup_failed", task_id=task_id_str, error=str(cleanup_err))
    finally:
        if _cancel_watcher and not _cancel_watcher.done():
            _cancel_watcher.cancel()
        await reporter.stop()
        if lock:
            await lock.release()
        await chat.close()


async def approve_task(ctx: dict, task_id: str):
    """User clicked [Create PR] — push and create PR."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)

    try:
        chat = await _get_chat_for_task(task)
    except ProviderMismatchError as e:
        await _update_task(task_id_str, status="orphaned", error_message=str(e))
        return

    if not task.chat_message_id:
        log.warning("orchestrator.no_chat_context", task_id=task_id_str)
        await _update_task(task_id_str, status="orphaned", error_message="No chat context")
        await chat.close()
        return

    if not task.branch_name:
        await _update_task(task_id_str, status="failed", error_message="No branch created")
        await chat.edit_message(task.chat_id, task.chat_message_id or "0", "Cannot create PR — no branch.")
        await chat.close()
        return

    git = await factory.get_git()
    ws = WorkspaceService()

    from openclow.services.checklist_reporter import ChecklistReporter
    checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                 title=f"Creating PR")
    checklist.set_steps(["Commit changes", "Push to GitHub", "Create pull request"])
    await checklist.start()

    try:
        workspace = ws.get_path(task_id_str)
        await _update_task(task_id_str, status="pushing")

        # Step 1: Commit
        await checklist.start_step(0)
        await git_ops.commit_and_push(workspace, task.branch_name,
                                       f"feat: {task.description[:72]}")
        await checklist.complete_step(0, "changes committed")

        # Step 2: Push
        await checklist.start_step(1)
        await checklist.complete_step(1, f"pushed to {task.branch_name[:25]}")

        # Step 3: Create PR
        await checklist.start_step(2)
        pr_url, pr_number = await git.create_pr(
            repo=task.project.github_repo,
            branch=task.branch_name,
            base=task.project.default_branch,
            title=f"[THAG GROUP] {task.description[:60]}",
            body=git.generate_pr_body(task),
        )
        await checklist.complete_step(2, f"PR #{pr_number}")

        await _update_task(task_id_str, status="awaiting_approval",
                           pr_url=pr_url, pr_number=pr_number)

        # Get tunnel URL for review
        tunnel_url = None
        try:
            from openclow.services.tunnel_service import get_tunnel_url
            tunnel_url = await get_tunnel_url(task.project.name)
        except Exception:
            pass

        checklist._footer = f"PR #{pr_number} ready for review"
        await checklist.stop()

        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        rows = []
        rows.append(ActionRow([ActionButton("🔗 View PR", f"open_pr:{task_id_str}", url=pr_url)]))
        from openclow.providers.actions import open_app_btns
        from openclow.services.tunnel_service import get_tunnel_url
        _t_url = await get_tunnel_url(task.project.name) if task.project else None
        rows.append(ActionRow(open_app_btns(task.project_id, tunnel_url=_t_url)))
        rows.append(ActionRow([
            ActionButton("✅ Merge", f"merge:{task_id_str}"),
            ActionButton("❌ Reject", f"reject:{task_id_str}"),
        ]))
        await checklist._force_render(keyboard=ActionKeyboard(rows=rows))

        await _log_to_db(task_id_str, "system", "info", f"PR created: {pr_url}")

    except Exception as e:
        log.error("approve.failed", task_id=task_id_str, error=str(e))
        await _update_task(task_id_str, status="failed", error_message=str(e))
        for i in range(3):
            if checklist.steps[i]["status"] in ("pending", "running"):
                await checklist.fail_step(i, "failed")
        checklist._footer = f"❌ {str(e)[:300]}"
        await checklist.stop()
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id))
    finally:
        await chat.close()


async def merge_task(ctx: dict, task_id: str):
    """User clicked [Merge]."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    try:
        chat = await _get_chat_for_task(task)
    except ProviderMismatchError as e:
        await _update_task(task_id_str, status="orphaned", error_message=str(e))
        return

    if not task.chat_message_id:
        log.warning("orchestrator.no_chat_context", task_id=task_id_str)
        await _update_task(task_id_str, status="orphaned", error_message="No chat context")
        await chat.close()
        return

    git = await factory.get_git()

    from openclow.services.checklist_reporter import ChecklistReporter
    checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                 title=f"Merging PR #{task.pr_number or ''}")
    checklist.set_steps(["Merge PR", "Clean workspace", "Sync to live"])
    await checklist.start()

    try:
        # Step 1: Merge
        await checklist.start_step(0)
        await git.merge_pr(task.project.github_repo, task.pr_number)
        await checklist.complete_step(0, f"PR #{task.pr_number} merged")

        await _update_task(task_id_str, status="merged")
        await _log_to_db(task_id_str, "system", "info", "PR merged")

        # Step 2: Cleanup
        await checklist.start_step(1)
        await WorkspaceService().cleanup(task_id_str, task.project.name)
        await checklist.complete_step(1, "workspace cleaned")

        # Step 3: Sync live (pull latest + rebuild)
        await checklist.start_step(2)
        try:
            project_workspace = os.path.join(settings.workspace_base_path, "_cache", task.project.name)
            if os.path.exists(project_workspace):
                proc = await asyncio.create_subprocess_exec(
                    "git", "pull", "origin", task.project.default_branch or "main",
                    cwd=project_workspace,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)
            await checklist.complete_step(2, "synced to live")
        except Exception:
            await checklist.complete_step(2, "sync skipped")

        # Get tunnel URL
        tunnel_url = None
        try:
            from openclow.services.tunnel_service import get_tunnel_url
            tunnel_url = await get_tunnel_url(task.project.name)
        except Exception:
            pass

        checklist._footer = f"✅ PR #{task.pr_number} merged and live!"
        await checklist.stop()

        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        rows = []
        from openclow.providers.actions import open_app_btns
        from openclow.services.tunnel_service import get_tunnel_url
        _t_url = await get_tunnel_url(task.project.name) if task.project else None
        rows.append(ActionRow(open_app_btns(task.project_id, tunnel_url=_t_url)))
        rows.append(ActionRow([
            ActionButton("🚀 New Task", "menu:task"),
            ActionButton("📦 Project", f"project_detail:{task.project_id}"),
        ]))
        rows.append(ActionRow([ActionButton("◀️ Main Menu", "menu:main")]))
        await checklist._force_render(keyboard=ActionKeyboard(rows=rows))

    except Exception as e:
        log.error("merge.failed", task_id=task_id_str, error=str(e))
        # Ensure workspace is cleaned up even on error
        try:
            await WorkspaceService().cleanup(task_id_str, task.project.name)
        except Exception:
            pass
        for i in range(3):
            if checklist.steps[i]["status"] in ("pending", "running"):
                await checklist.fail_step(i, "failed")
        checklist._footer = f"❌ {str(e)[:300]}"
        await checklist.stop()
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id))
    finally:
        await chat.close()


async def reject_task(ctx: dict, task_id: str):
    """User clicked [Reject]."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    try:
        chat = await _get_chat_for_task(task)
    except ProviderMismatchError as e:
        await _update_task(task_id_str, status="orphaned", error_message=str(e))
        return

    if not task.chat_message_id:
        log.warning("orchestrator.no_chat_context", task_id=task_id_str)
        await _update_task(task_id_str, status="orphaned", error_message="No chat context")
        await chat.close()
        return

    git = await factory.get_git()

    from openclow.services.checklist_reporter import ChecklistReporter
    steps = []
    if task.pr_number:
        steps.append("Close PR")
    if task.branch_name:
        steps.append("Delete branch")
    steps.append("Clean workspace")

    checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                 title="Rejecting task")
    checklist.set_steps(steps)
    await checklist.start()

    try:
        step_idx = 0
        if task.pr_number:
            await checklist.start_step(step_idx)
            await git.close_pr(task.project.github_repo, task.pr_number)
            await checklist.complete_step(step_idx, f"PR #{task.pr_number} closed")
            step_idx += 1

        if task.branch_name:
            await checklist.start_step(step_idx)
            await git.delete_branch(task.project.github_repo, task.branch_name)
            await checklist.complete_step(step_idx, "branch deleted")
            step_idx += 1

        await checklist.start_step(step_idx)
        await _update_task(task_id_str, status="rejected")
        await _log_to_db(task_id_str, "system", "info", "Task rejected")
        await WorkspaceService().cleanup(task_id_str, task.project.name)
        await checklist.complete_step(step_idx, "workspace cleaned")

        checklist._footer = "Task rejected. Changes removed."
        await checklist.stop()

        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        kb = ActionKeyboard(rows=[
            ActionRow([
                ActionButton("🚀 New Task", "menu:task"),
                ActionButton("📦 Project", f"project_detail:{task.project_id}"),
            ]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await checklist._force_render(keyboard=kb)

    except Exception as e:
        log.error("reject.failed", task_id=task_id_str, error=str(e))
        for i, s in enumerate(checklist.steps):
            if s["status"] in ("pending", "running"):
                await checklist.fail_step(i, "failed")
        checklist._footer = f"❌ {str(e)[:300]}"
        await checklist.stop()
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id))
    finally:
        await chat.close()


async def discard_task(ctx: dict, task_id: str):
    """User clicked [Discard] — clean up workspace and branch."""
    task = await _get_task(task_id)
    task_id_str = str(task.id)
    try:
        chat = await _get_chat_for_task(task)
    except ProviderMismatchError as e:
        await _update_task(task_id_str, status="orphaned", error_message=str(e))
        return

    if not task.chat_message_id:
        log.warning("orchestrator.no_chat_context", task_id=task_id_str)
        await _update_task(task_id_str, status="orphaned", error_message="No chat context")
        await chat.close()
        return

    git = await factory.get_git()
    ws = WorkspaceService()

    from openclow.services.checklist_reporter import ChecklistReporter
    steps = []
    if task.branch_name:
        steps.append("Delete branch")
    steps.append("Remove workspace")

    checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                 title="Discarding changes")
    checklist.set_steps(steps)
    await checklist.start()

    try:
        step_idx = 0
        if task.branch_name:
            await checklist.start_step(step_idx)
            await git.delete_branch(task.project.github_repo, task.branch_name)
            await checklist.complete_step(step_idx, f"branch deleted")
            step_idx += 1

        await checklist.start_step(step_idx)
        await ws.cleanup(task_id_str, task.project.name)
        await checklist.complete_step(step_idx, "workspace removed")

        await _update_task(task_id_str, status="discarded")
        await _log_to_db(task_id_str, "system", "info", "Task discarded by user")

        checklist._footer = "Changes discarded. Ready for next task!"
        await checklist.stop()

        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        kb = ActionKeyboard(rows=[
            ActionRow([
                ActionButton("🚀 New Task", "menu:task"),
                ActionButton("📦 Project", f"project_detail:{task.project_id}"),
            ]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await checklist._force_render(keyboard=kb)

    except Exception as e:
        log.error("discard.failed", task_id=task_id_str, error=str(e))
        for i, s in enumerate(checklist.steps):
            if s["status"] in ("pending", "running"):
                await checklist.fail_step(i, "failed")
        checklist._footer = f"❌ {str(e)[:300]}"
        await checklist.stop()
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id))
    finally:
        await chat.close()
