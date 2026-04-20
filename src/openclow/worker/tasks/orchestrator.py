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

from openclow.models import Task, TaskLog, WebChatSession, async_session
from openclow.providers import factory
from openclow.providers.base import ProviderMismatchError
from openclow.services.workspace_service import WorkspaceService
from openclow.settings import settings
from openclow.utils.logging import get_logger
from openclow.worker.tasks import git_ops

log = get_logger()


async def _make_cancel_watcher(chat_id: str) -> "asyncio.Task | None":
    """Spawn a background task that watches for cancel via Redis pub/sub (instant).

    When the user clicks Stop, the cancel endpoint sets the Redis key AND
    publishes to openclow:cancel:{session_id}. This watcher subscribes to
    that channel — cancel is detected in < 100ms (vs 5s polling before).

    Also checks the key on startup in case the cancel was set before we subscribed.

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
        import redis.asyncio as aioredis
        from openclow.settings import settings as _s
        try:
            r = aioredis.from_url(_s.redis_url)

            # Clear any stale cancel flag from a previous task in this session.
            # Without this, retrying a cancelled task immediately re-cancels because
            # the old flag (600s TTL) is still set.
            await r.delete(f"openclow:cancel_session:{session_id}")

            # Subscribe to cancel channel — instant notification
            pubsub = r.pubsub()
            await pubsub.subscribe(f"openclow:cancel:{session_id}")
            try:
                async for msg in pubsub.listen():
                    if msg["type"] == "message":
                        if outer_task and not outer_task.done():
                            log.info("orchestrator.cancel_detected", session_id=session_id, via="pubsub")
                            outer_task.cancel()
                        return
            finally:
                await pubsub.unsubscribe(f"openclow:cancel:{session_id}")
                await pubsub.aclose()
                await r.aclose()
        except asyncio.CancelledError:
            pass  # watcher itself was cancelled in finally block
        except Exception as e:
            log.warning("orchestrator.cancel_watcher_error", error=str(e))

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
    "discard_task": {"diff_preview", "plan_review", "failed"},
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


_FRONTEND_EXTS = (".vue", ".tsx", ".jsx", ".ts", ".js", ".css", ".scss", ".sass", ".less")


async def _run_agent_with_streaming(
    agent_gen,
    reporter,
    step_index: int,
    stream_to_web,
    is_web: bool,
    label: str = "agent",
    idle_timeout: int = 90,
) -> tuple[str, int]:
    """Run any LLM agent with streaming + idle-based stall detection.

    Same pattern as the coder agent: streams every token/tool to web chat,
    tracks activity timestamps, and kills the agent if it goes idle for
    too long (MCP hang, SDK stuck, infinite loop).

    Args:
        agent_gen: Async generator yielding SDK messages (from query()).
        reporter: ChecklistReporter to update step details.
        step_index: Which step in the checklist to update.
        stream_to_web: Async callback(text="", tool="") for web streaming.
        is_web: Whether this is a web chat session.
        label: Human label for logging ("review", "deploy", "fix").
        idle_timeout: Seconds of no activity before killing the agent.

    Returns:
        (full_output, turn_count) — collected text and number of turns.
    """
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock, StreamEvent

    full_output = ""
    turn_count = 0
    last_activity = time.monotonic()

    # Idle watchdog — cancels the stream task if agent goes silent
    _stream_task: asyncio.Task | None = None

    async def _idle_watchdog():
        nonlocal _stream_task
        while True:
            await asyncio.sleep(5)
            idle_secs = time.monotonic() - last_activity
            if idle_secs >= idle_timeout:
                log.warning(f"agent.idle_timeout.{label}",
                            idle_secs=int(idle_secs), idle_timeout=idle_timeout)
                if _stream_task and not _stream_task.done():
                    _stream_task.cancel()
                return

    async def _run_stream():
        nonlocal full_output, turn_count, last_activity
        async for message in agent_gen:
            last_activity = time.monotonic()

            # Stream raw tokens to web
            if isinstance(message, StreamEvent):
                evt = message.event
                if evt.get("type") == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        if is_web:
                            await stream_to_web(text=delta.get("text", ""))
                continue

            if not isinstance(message, AssistantMessage):
                continue

            turn_count += 1

            for block in message.content:
                if isinstance(block, TextBlock):
                    full_output += block.text
                    if is_web:
                        await stream_to_web(text=f"\n{block.text}")
                elif isinstance(block, ToolUseBlock):
                    from openclow.worker.tasks._agent_base import describe_tool
                    tool_desc = describe_tool(block)
                    await reporter.update_step(step_index, tool_desc[:50])
                    if is_web:
                        await stream_to_web(tool=tool_desc)

    _wd_task = asyncio.create_task(_idle_watchdog())
    _stream_task = asyncio.create_task(_run_stream())

    try:
        await _stream_task
    except asyncio.CancelledError:
        idle_secs = int(time.monotonic() - last_activity)
        if idle_secs >= idle_timeout:
            log.warning(f"agent.stalled.{label}", idle_secs=idle_secs)
            await reporter.update_step(step_index, f"stalled ({idle_secs}s idle) — skipping")
        else:
            # Cancelled by external source (web Stop button, worker shutdown)
            raise
    finally:
        _wd_task.cancel()
        try:
            await _wd_task
        except (asyncio.CancelledError, Exception):
            pass

    return full_output, turn_count


async def _run_frontend_build(
    task, workspace_path: str, task_id_str: str, reporter, step_index: int,
) -> str | None:
    """Deterministic frontend build — runs outside the agent turn loop.

    Checks the git diff for frontend file changes. If found, runs npm run build
    via direct subprocess (run_docker) with a 120s timeout. No MCP, no SDK,
    no agent timeout pressure — just a straightforward docker exec.

    Returns a short result string, or None if no build was needed.
    """
    # Check if any frontend files changed (use --name-only for clean filenames;
    # diff --stat appends "| N +++--" which breaks endswith() checks)
    changed = await git_ops.changed_files(workspace_path)
    has_frontend = any(
        f.endswith(ext) for ext in _FRONTEND_EXTS for f in changed
    )

    if not has_frontend:
        await _log_to_db(task_id_str, "system", "info", "Build: no frontend changes detected, skipping")
        return None

    await reporter.update_step(step_index, "npm run build")
    await _log_to_db(task_id_str, "system", "info", "Build: frontend files changed, running npm run build")

    compose_project = f"openclow-{task.project.name}"
    app_container = task.project.app_container_name or "app"
    app_container_full = f"{compose_project}-{app_container}-1"
    project_workspace = os.path.join(settings.workspace_base_path, "_cache", task.project.name)

    # First sync changed files from task workspace to project workspace
    # (the build needs to see the updated source files)
    try:
        from openclow.services.docker_guard import run_docker
        # Copy changed files
        rc, out = await run_docker(
            "docker", "cp", f"{workspace_path}/.", f"{app_container_full}:/var/www/html/",
            actor="build_step", timeout=30,
        )
        if rc != 0:
            # Try alternate workdir
            rc, out = await run_docker(
                "docker", "cp", f"{workspace_path}/.", f"{app_container_full}:/app/",
                actor="build_step", timeout=30,
            )
    except Exception as e:
        log.warning("orchestrator.build_sync_failed", error=str(e))

    # Run the build — generous 120s timeout, direct subprocess
    try:
        from openclow.services.docker_guard import run_docker
        await reporter.update_step(step_index, "building assets...")
        rc, out = await run_docker(
            "docker", "exec", app_container_full, "sh", "-c",
            "cd /var/www/html 2>/dev/null || cd /app 2>/dev/null || true; "
            "npm run build 2>&1 || npx vite build 2>&1",
            actor="build_step", timeout=120,
        )
        if rc == 0:
            log.info("orchestrator.build_ok", project=task.project.name)
            await _log_to_db(task_id_str, "system", "info", "Build: success")
            return "build OK"
        else:
            # Build failed — log but don't block the pipeline
            log.warning("orchestrator.build_failed", project=task.project.name,
                        rc=rc, output=out[:300])
            await _log_to_db(task_id_str, "system", "warning",
                             f"Build failed (rc={rc}): {out[:200]}")
            await reporter.update_step(step_index, f"build failed (rc={rc})")
            return f"build failed: {out[:60]}"
    except asyncio.TimeoutError:
        log.warning("orchestrator.build_timeout", project=task.project.name)
        await _log_to_db(task_id_str, "system", "warning", "Build: timed out after 120s")
        return "build timed out"
    except Exception as e:
        log.warning("orchestrator.build_error", error=str(e))
        await _log_to_db(task_id_str, "system", "warning", f"Build error: {str(e)[:200]}")
        return f"build error: {str(e)[:60]}"


async def _run_lightweight_deploy_host(
    task, workspace_path: str, diff_summary: str,
) -> str:
    """Host-mode deploy: files are already on the host filesystem; just rebuild
    frontend assets if they changed, refresh framework caches, and verify the
    public URL responds. No docker cp, no docker exec — straight subprocess in
    the project directory via the host_guard allowlist."""
    from openclow.services.host_guard import run_host

    project_dir = task.project.project_dir
    if not project_dir or not os.path.isdir(project_dir):
        return f"host deploy skipped: project_dir missing ({project_dir!r})"

    actor_name = "deploy_lite_host"

    # Frontend rebuild if needed
    changed = await git_ops.changed_files(workspace_path)
    has_frontend = any(f.endswith(ext) for ext in _FRONTEND_EXTS for f in changed)
    if has_frontend:
        rc, out = await run_host(
            "npm run build", cwd=project_dir, timeout=300,
            actor=actor_name, project_name=task.project.name,
        )
        if rc != 0:
            log.warning("deploy.host_build_failed",
                        project=task.project.name, output=out[-400:])
            # Don't abort — surface the failure but let cache/verify continue.
            # The agent's source edit landed; a build failure is recoverable.

    # Laravel cache refresh — best-effort, harmless on non-Laravel apps.
    if os.path.isfile(os.path.join(project_dir, "artisan")):
        await run_host(
            "php artisan config:cache",
            cwd=project_dir, timeout=20,
            actor=actor_name, project_name=task.project.name,
        )
        await run_host(
            "php artisan view:cache",
            cwd=project_dir, timeout=20,
            actor=actor_name, project_name=task.project.name,
        )
        await run_host(
            "php artisan route:cache",
            cwd=project_dir, timeout=20,
            actor=actor_name, project_name=task.project.name,
        )

    # HTTP verify — hit the public URL (or health URL if configured).
    url = task.project.health_url or task.project.public_url
    if url:
        rc, code = await run_host(
            f"curl -sS -o /dev/null -w '%{{http_code}}' -L {url}",
            cwd=project_dir, timeout=15,
            actor=actor_name, project_name=task.project.name,
        )
        code = (code or "").strip()
        if rc == 0 and code.startswith(("2", "3")):
            return f"built, verified {code}"
        return f"built, HTTP {code or 'unknown'}"
    return "built, no public_url to verify"


async def _run_lightweight_deploy(
    task, workspace_path: str, diff_summary: str, tunnel_url: str | None,
) -> str:
    """Deterministic deploy: file sync + cache clear + HTTP verify.

    No LLM agent — direct subprocess calls, ~2-5s total.
    Used for subsequent tasks where health was cached (containers known-good).
    """
    # Host-mode projects bypass the entire docker pipeline.
    if (task.project.mode or "docker").lower() == "host":
        return await _run_lightweight_deploy_host(task, workspace_path, diff_summary)

    from openclow.services.docker_guard import run_docker

    compose_project = f"openclow-{task.project.name}"
    app_container = task.project.app_container_name or "app"
    app_container_full = f"{compose_project}-{app_container}-1"
    project_workspace = os.path.join(settings.workspace_base_path, "_cache", task.project.name)

    # Step 1: Discover workdir inside the container (don't hardcode /var/www/html)
    workdir = "/var/www/html"
    try:
        rc, out = await run_docker(
            "docker", "exec", app_container_full, "sh", "-c",
            "for d in /var/www/html /app /opt/app /srv; do "
            "[ -f \"$d/package.json\" ] || [ -f \"$d/composer.json\" ] || [ -f \"$d/manage.py\" ] && echo $d && exit 0; "
            "done; echo /var/www/html",
            actor="deploy_lite", timeout=5,
        )
        if rc == 0 and out.strip():
            workdir = out.strip().split("\n")[0]
    except Exception:
        pass

    # Step 2: Sync files from task workspace to the running app.
    #
    # CRITICAL: Many Docker Compose setups bind-mount the project directory
    # (e.g., .:/var/www/html). In this case, docker cp writes to the container's
    # overlay filesystem, but the bind mount HIDES those files — the container
    # actually serves the host directory. We must detect this and sync the host
    # cache instead.
    cache_path = os.path.join(settings.workspace_base_path, "_cache", task.project.name)
    has_bind_mount = False
    try:
        rc, out = await run_docker(
            "docker", "inspect", app_container_full,
            "--format", "{{json .HostConfig.Binds}}",
            actor="deploy_lite", timeout=5,
        )
        if rc == 0 and out.strip() and out.strip() != "null":
            import json
            binds = json.loads(out.strip())
            for bind in binds:
                parts = bind.split(":")
                if len(parts) >= 2:
                    container_path = parts[1]
                    if workdir.startswith(container_path) or container_path in workdir:
                        has_bind_mount = True
                        break
    except Exception:
        pass

    if has_bind_mount:
        # Bind mount detected — sync host cache so the container sees changes.
        # The cache and worktree share the same git repo (worktree setup),
        # so the task branch is available for checkout.
        try:
            if task.branch_name and task.git_mode != "direct_commit":
                await git_ops.run_exec("git", "checkout", task.branch_name, cwd=cache_path, ignore_errors=True)
            else:
                await git_ops.run_exec("git", "fetch", "origin", task.project.default_branch or "main", cwd=cache_path, ignore_errors=True)
                await git_ops.run_exec("git", "reset", "--hard", f"origin/{task.project.default_branch or 'main'}", cwd=cache_path, ignore_errors=True)
        except Exception as e:
            log.warning("deploy.bind_mount_sync_failed", error=str(e))
            return f"sync failed (bind mount): {str(e)[:60]}"
    else:
        # No bind mount — standard docker cp
        try:
            rc, out = await run_docker(
                "docker", "cp", f"{workspace_path}/.", f"{app_container_full}:{workdir}/",
                actor="deploy_lite", timeout=30,
            )
            if rc != 0:
                return f"sync failed (rc={rc}): {out[:60]}"
        except Exception as e:
            return f"sync failed: {str(e)[:60]}"

    # Step 3: Rebuild frontend assets inside the container (if frontend files changed)
    changed = await git_ops.changed_files(workspace_path)
    has_frontend = any(
        f.endswith(ext) for ext in _FRONTEND_EXTS for f in changed
    )
    if has_frontend:
        try:
            await run_docker(
                "docker", "exec", app_container_full, "sh", "-c",
                f"cd {workdir} && npm run build 2>&1 || npx vite build 2>&1 || true",
                actor="deploy_lite", timeout=120,
            )
        except Exception:
            pass  # Build failure shouldn't block deploy

    # Step 4: Clear framework caches (deterministic, no LLM needed)
    try:
        await run_docker(
            "docker", "exec", app_container_full, "sh", "-c",
            f"cd {workdir} && "
            "php artisan config:cache 2>/dev/null; "
            "php artisan cache:clear 2>/dev/null; "
            "php artisan view:clear 2>/dev/null; "
            "true",
            actor="deploy_lite", timeout=15,
        )
    except Exception:
        pass

    # Step 5: Verify HTTP response
    port = task.project.app_port or 80
    try:
        rc, status_code = await run_docker(
            "docker", "exec", app_container_full, "sh", "-c",
            f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{port}",
            actor="deploy_lite", timeout=10,
        )
        code = status_code.strip()
        if code.startswith(("2", "3")):
            return f"synced, verified {code}"
        else:
            return f"synced, HTTP {code}"
    except Exception:
        return "synced, verify skipped"


async def _deploy_agent_gen(
    task, workspace_path: str, diff_summary: str, tunnel_url: str | None,
):
    """Async generator that yields SDK messages from the deploy agent.

    The orchestrator runs this through _run_agent_with_streaming for
    streaming + stall detection.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
    except ImportError:
        return

    from openclow.providers.llm.claude import _mcp_docker

    compose_project = f"openclow-{task.project.name}"
    app_container = task.project.app_container_name or "app"
    app_container_full = f"{compose_project}-{app_container}-1"
    project_workspace = os.path.join(settings.workspace_base_path, "_cache", task.project.name)

    prompt = f"""Run post-deploy actions for {task.project.name}.

Files have ALREADY been synced to the container and frontend assets have ALREADY been built.
Do NOT run `npm run build`, `npx vite build`, or any frontend compilation.
Do NOT copy or sync files — they are already in the container.

## Changed Files (diff summary)

{diff_summary}

## Environment

Project: {task.project.name} ({task.project.tech_stack or 'Unknown'})
App container: {app_container_full}
Compose project: {compose_project}
Tunnel URL: {tunnel_url or 'none'}

## Steps

### Step 1: Run post-deploy actions based on what changed

| Changed files include... | Action |
|---|---|
| database/migrations/ or alembic/ | Run migrations via docker_exec |
| config/ or .env | Clear config/route cache via docker_exec |
| database/seeders/ | Run seeders only if idempotent |
| Nothing actionable | Skip to verification |

### Step 2: Verify the app works
docker_exec("{app_container_full}", "curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{task.project.app_port or 80}")
- 200–399: success
- 500+: read container_logs, attempt fix
- Connection refused: restart_container, wait 5s, re-verify

### Step 3: Report
DEPLOY_RESULT: [what you did — e.g. "ran migrations, verified 200 OK"]
DEPLOY_BLOCKED: [what failed and why]"""

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

    log.info("orchestrator.deploy_agent.started", project=task.project.name)
    try:
        async for message in query(prompt=prompt, options=options):
            yield message
    except Exception as e:
        from openclow.worker.tasks._agent_base import is_auth_error
        if is_auth_error(e):
            raise
        log.warning("orchestrator.deploy_agent_failed", error=str(e))


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


def _retry_keyboard(project_id: int | None = None, task_id: str = ""):
    """Retry + discard + main menu keyboard for interrupted/failed tasks."""
    from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
    retry_action = f"retry_task:{task_id}" if task_id else "menu:task"
    rows = [
        ActionRow([ActionButton("🔄 Retry", retry_action)]),
    ]
    if task_id:
        rows.append(ActionRow([ActionButton("🗑️ Discard", f"discard_task:{task_id}")]))
    rows.append(ActionRow([
        ActionButton("📋 View Logs", "menu:logs"),
        ActionButton("◀️ Main Menu", "menu:main"),
    ]))
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
- AFTER getting a URL: docker_exec the app container to curl the tunnel URL
  docker_exec("<app_container>", "curl -sf -o /dev/null -w '%{{http_code}}' <tunnel_url>")
  If HTTP 200-399: tunnel is verified, report it
  If 502/connection refused/timeout: tunnel_stop + tunnel_start + verify again
  NEVER report a tunnel URL without verifying it responds with HTTP < 502

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
    """Two-phase health check: fast Python path first, LLM only if broken."""
    project = task.project
    if not project.is_dockerized:
        return True, None

    workspace = f"{settings.workspace_base_path}/_cache/{project.name}"
    compose_project = f"openclow-{project.name}"
    app_container = f"{compose_project}-{project.app_container_name or 'app'}-1"

    # ── Phase 1: Fast Python check (no LLM, no MCP — < 1 second) ──
    # Name-based `docker inspect` first, then fall back to LABEL-based lookup.
    # Label-based is robust against compose config-hash drift (containers that
    # exist but `docker compose ps` can't see because the compose file changed
    # since they were created). Without the fallback, we'd wrongly conclude the
    # project is broken and trigger a full LLM repair agent.
    is_healthy = False
    try:
        from openclow.services.docker_guard import run_docker
        rc, output = await run_docker(
            "docker", "inspect", "--format",
            "{{.State.Status}}:{{.State.Health.Status}}",
            app_container, actor="orchestrator",
        )
        parts = output.strip().split(":") if rc == 0 else []
        status = parts[0] if parts else ""
        health = parts[1] if len(parts) > 1 else ""
        is_healthy = status == "running" and health in ("healthy", "", "<no value>")
    except Exception:
        is_healthy = False

    if not is_healthy:
        try:
            from openclow.services.health_service import find_project_containers
            containers = await find_project_containers(project.name)
            app_hint = (project.app_container_name or "app").lower()
            app_match = next(
                (c for c in containers if app_hint in c.name.lower()),
                None,
            )
            if app_match and app_match.state == "running" and (
                app_match.health in ("healthy", "", "none", "<no value>")
                or "healthy" in app_match.health.lower()
            ):
                is_healthy = True
                log.info(
                    "orchestrator.phase1_label_recovery",
                    project=project.name,
                    container=app_match.name,
                )
        except Exception as _e:
            log.debug("orchestrator.phase1_label_check_failed", error=str(_e))

    tunnel_url = None
    if is_healthy:
        try:
            from openclow.services.tunnel_service import get_tunnel_url
            tunnel_url = await get_tunnel_url(project.name)
        except Exception:
            pass

    if is_healthy and tunnel_url:
        # Verify the URL is actually reachable — DB can have stale URLs
        from openclow.services.tunnel_service import verify_tunnel_url
        if await verify_tunnel_url(tunnel_url):
            await reporter.log("💚 Healthy, tunnel verified")
            return True, tunnel_url
        else:
            await reporter.log("⚠️ Tunnel URL stale — restarting")
            tunnel_url = None  # fall through to Phase 1b

    # ── Phase 1b: Container healthy but no tunnel — start one directly (no LLM) ──
    if is_healthy and not tunnel_url:
        await reporter.log("💚 Container healthy — starting tunnel")
        try:
            from openclow.services.docker_guard import run_docker as _run_docker
            rc2, ip_out = await _run_docker(
                "docker", "inspect",
                "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                app_container, actor="orchestrator",
            )
            container_ip = ip_out.strip() if rc2 == 0 else ""
            port = project.app_port or 80
            target = f"http://{container_ip}:{port}" if container_ip else f"http://localhost:{port}"
            from openclow.services.tunnel_service import start_tunnel, stop_tunnel, verify_tunnel_url, check_tunnel_health

            # Retry loop: up to 3 attempts with backoff
            for attempt in range(1, 4):
                tunnel_url = await start_tunnel(project.name, target)
                if tunnel_url:
                    # Dual verification: HTTP probe + TCP origin check
                    http_ok = await verify_tunnel_url(tunnel_url)
                    origin_ok = await check_tunnel_health(project.name)
                    if http_ok or origin_ok:
                        await reporter.log(f"🌐 {tunnel_url}")
                        return True, tunnel_url
                    await reporter.log(f"⚠️ Tunnel not reachable (attempt {attempt}/3)")
                    await stop_tunnel(project.name)
                    await asyncio.sleep(2 * attempt)  # 2s, 4s, 6s backoff
                else:
                    await reporter.log(f"⚠️ Tunnel start failed (attempt {attempt}/3)")
                    await asyncio.sleep(2 * attempt)

            # Container is healthy but tunnel won't start — don't waste time in LLM
            await reporter.log("⚠️ Tunnel unavailable — proceeding without tunnel")
            return True, None
        except Exception as e:
            log.warning("orchestrator.fast_tunnel_failed", error=str(e))
            await reporter.log("⚠️ Tunnel error — proceeding without tunnel")
            return True, None

    # ── Phase 2: Something broken — use LLM agent for diagnosis + repair ──
    await reporter.log("🔍 Issue detected — starting repair agent")

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
                "You are a DevOps health guard. Fix the broken container/tunnel issue FAST.\n\n"
                "## Available Tools (use directly — do NOT search for tools)\n\n"
                "Docker tools (prefixed mcp__docker__):\n"
                "- list_containers(project_filter?) — list running containers\n"
                "- container_logs(container_name, tail=50) — get recent logs\n"
                "- container_health(container_name) — check container status\n"
                "- docker_exec(container_name, command) — run command inside container (60s timeout)\n"
                "- restart_container(container_name) — restart a container\n"
                "- compose_up(compose_file, project_name, working_dir) — start Docker Compose stack\n"
                "- compose_ps(project_name) — list containers in a Compose stack\n"
                "- tunnel_start(service_name, target_url, host_header?) — start Cloudflare tunnel\n"
                "- tunnel_stop(service_name) — stop a tunnel\n"
                "- tunnel_get_url(service_name) — get current public URL\n\n"
                "File tools: Read, Edit, Glob — for reading/editing project files.\n\n"
                "CRITICAL RULES:\n"
                "1. Tunnel state is stored in the DATABASE, NOT in `.tunnel` files on disk. "
                "Do NOT search for `.tunnel` files — they do not exist.\n"
                "2. If containers are running but tunnel is missing, just start it with tunnel_start and verify. "
                "Do not investigate further.\n"
                "3. Never report a tunnel URL as working without HTTP-verifying it first. "
                "Use docker_exec to curl the URL — if it returns 502 or fails, the tunnel is dead. "
                "Stop it, restart it, and verify again before saying HEALTHY.\n"
                "4. Be FAST: one diagnostic, one fix, verify, done. Do not explore.\n"
                "5. NEVER run 'curl --unix-socket /run/docker.sock' or any raw Docker API call via docker_exec — the socket is NOT mounted. Use ONLY the tools listed above."
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
            max_turns=5,
        )

        async def _run_health_agent() -> str:
            output = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            output += block.text
                        elif isinstance(block, ToolUseBlock):
                            from openclow.worker.tasks._agent_base import describe_tool
                            await reporter.log(describe_tool(block))
            return output

        full_output = ""
        try:
            full_output = await asyncio.wait_for(_run_health_agent(), timeout=45)
        except asyncio.TimeoutError:
            log.warning("orchestrator.health_guard_timeout", project=project.name)
            await reporter.log("⏱️ Health repair timed out — proceeding anyway")

        # Parse result
        healthy = "HEALTHY:" in full_output
        tunnel_url = None

        # Extract tunnel URL from output
        import re
        url_match = re.search(r"(https://[a-z0-9-]+\.trycloudflare\.com)", full_output)
        if url_match:
            tunnel_url = url_match.group(1)

        # Verify the URL actually works — agent might report a stale one
        if tunnel_url:
            from openclow.services.tunnel_service import verify_tunnel_url
            if not await verify_tunnel_url(tunnel_url):
                log.warning("orchestrator.agent_tunnel_url_dead", url=tunnel_url)
                tunnel_url = None  # don't hand back a dead URL

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

        if (task.project.mode or "docker").lower() == "host":
            workspace = await ws.prepare_host(task.project)
        else:
            workspace = await ws.prepare(task.project, task_id_str)

        # ── Branch handling based on git_mode ──
        branch_slug = slugify(task.description, max_length=50)
        branch_name = f"openclow/{task_id_str[:8]}-{branch_slug}"

        if task.git_mode == "direct_commit":
            # Stay on default_branch — no branch creation
            await _update_task(task_id_str, branch_name=None)
            await reporter.complete_step(0, "direct commit mode")
            await _log_to_db(task_id_str, "system", "info", "Direct commit mode — no branch created")

        elif task.git_mode == "session_branch":
            # Use or create a shared session branch
            session_branch = None
            web_session_id = None
            if task.chat_provider_type == "web" and task.chat_id.startswith("web:"):
                try:
                    _, _, sid_str = task.chat_id.split(":", 2)
                    web_session_id = int(sid_str)
                except (ValueError, IndexError):
                    pass

                if web_session_id:
                    async with async_session() as db_session:
                        from sqlalchemy import select as sa_select
                        ws_result = await db_session.execute(
                            sa_select(WebChatSession).where(WebChatSession.id == web_session_id)
                        )
                        ws_session = ws_result.scalar_one_or_none()
                        if ws_session and ws_session.session_branch_name:
                            session_branch = ws_session.session_branch_name

            if session_branch:
                # Existing session branch — fetch and checkout
                branch_name = session_branch
                try:
                    await git_ops.run_exec("git", "fetch", "origin", branch_name, cwd=workspace.path)
                    await git_ops.run_exec("git", "checkout", branch_name, cwd=workspace.path)
                except Exception:
                    # Fallback: create locally if fetch fails
                    await git_ops.create_branch(workspace.path, branch_name)
            else:
                # New session branch — create and store on session
                sid_prefix = str(web_session_id)[:8] if web_session_id else task_id_str[:8]
                branch_name = f"openclow/session-{sid_prefix}-{branch_slug}"
                await git_ops.create_branch(workspace.path, branch_name)
                if web_session_id:
                    async with async_session() as db_session:
                        from sqlalchemy import select as sa_select
                        ws_result = await db_session.execute(
                            sa_select(WebChatSession).where(WebChatSession.id == web_session_id)
                        )
                        ws_session = ws_result.scalar_one_or_none()
                        if ws_session:
                            ws_session.session_branch_name = branch_name
                            await db_session.commit()

            await _update_task(task_id_str, branch_name=branch_name)
            await reporter.complete_step(0, f"session branch: {branch_name[:30]}")
            await _log_to_db(task_id_str, "system", "info", f"Session branch: {branch_name}")

        else:
            # branch_per_task (default) — current behavior
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
        # Stop heartbeat IMMEDIATELY — prevents it from overwriting the
        # cancelled card state that the cancel endpoint already set in DB.
        await reporter.stop()
        error_msg = "Task timed out" if isinstance(e, TimeoutError) else "Task was cancelled"
        log.warning("orchestrator.planning_interrupted", task_id=task_id_str, reason=error_msg)
        # Check if user already cancelled via the cancel endpoint — skip card overwrite
        user_cancelled = False
        if task.chat_id.startswith("web:"):
            try:
                import redis.asyncio as aioredis
                parts = task.chat_id.split(":", 2)
                if len(parts) >= 3:
                    r = aioredis.from_url(settings.redis_url)
                    user_cancelled = await r.get(f"openclow:cancel_session:{parts[2]}") is not None
                    await r.aclose()
            except Exception:
                pass
        await _update_task(task_id_str, status="failed",
                           error_message=error_msg,
                           duration_seconds=int(time.time() - start_time))
        if not user_cancelled:
            try:
                running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
                await reporter.fail_step(running_idx, error_msg[:40])
                reporter._footer = f"{error_msg}. You can retry."
                await reporter._force_render(keyboard=_retry_keyboard(task.project_id if task else None, task_id_str))
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


class AgentRetryNeeded(Exception):
    """Raised when the coder agent needs to retry instead of failing."""
    def __init__(self, reason: str, turn_count: int = 0):
        self.reason = reason
        self.turn_count = turn_count
        super().__init__(f"Retry needed: {reason}")


def _is_max_turns_reached(message) -> bool:
    """Check if a message from the Claude SDK indicates max turns were reached."""
    if hasattr(message, "attachments") and message.attachments:
        for att in message.attachments:
            if getattr(att, "type", "") == "max_turns_reached":
                return True
    return False


def _build_recovery_prompt(task_description: str, attempt: int, plan_text: str) -> str:
    """Build an escalating recovery prompt for retry attempts."""
    prompts = [
        # Attempt 1 — gentle nudge
        (
            "You previously worked on this task but didn't complete it. "
            "The workspace has been reset. Focus on the MOST IMPORTANT change first. "
            "Don't overthink — make one clear edit, then verify it works."
        ),
        # Attempt 2 — direct instruction
        (
            "Previous attempts failed to produce results. You MUST make concrete file edits NOW.\n"
            "1. Read the relevant files\n"
            "2. Identify the EXACT lines to change\n"
            "3. Use the write tool to modify those files\n"
            "4. Do NOT just explore — EDIT files immediately"
        ),
        # Attempt 3 — nuclear option
        (
            "This is your final attempt. The task is: {task}\n"
            "You have been unable to make progress. Use the SIMPLEST possible approach.\n"
            "- If you need a new file: CREATE it\n"
            "- If you need to change existing code: EDIT it directly\n"
            "- If you're unsure: pick ONE file and make ONE change\n"
            "Report DONE_SUMMARY as soon as you make any meaningful change."
        ),
    ]
    base = prompts[min(attempt, len(prompts) - 1)]
    if attempt == 2:
        base = base.format(task=task_description)

    parts = [base]
    if plan_text:
        parts.append(f"\n\nOriginal plan:\n{plan_text}")
    parts.append(f"\n\nOriginal task: {task_description}")
    return "\n".join(parts)


async def _prepare_retry_workspace(workspace_path: str):
    """Reset workspace to clean state before a retry attempt."""
    try:
        await git_ops.reset_hard(workspace_path)
    except Exception:
        pass


async def _notify_retry(
    reporter, attempt: int, reason: str, task_id_str: str,
):
    """Update the progress card to show a retry is happening."""
    reason_labels = {
        "stalled": "agent got stuck",
        "max_turns_reached": "hit turn limit",
        "empty_diff": "no changes made",
    }
    label = reason_labels.get(reason, reason)
    msg = f"Retrying with a fresh approach ({attempt}/{settings.coder_max_retries}) — {label}"
    log.info("orchestrator.retrying", task_id=task_id_str, attempt=attempt, reason=reason)
    await reporter.update_step(1, msg)


async def _ask_user_continue_or_cancel(
    chat, task, reporter, reason: str, task_id_str: str,
):
    """After max retries, ask the user whether to keep trying or cancel."""
    from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
    reason_labels = {
        "stalled": "The agent got stuck and couldn't make progress",
        "max_turns_reached": "The agent hit its turn limit",
        "empty_diff": "The agent didn't modify any files",
    }
    label = reason_labels.get(reason, "The agent is struggling")
    kb = ActionKeyboard(rows=[
        ActionRow([
            ActionButton("🔄 Keep Trying", f"retry_task:{task_id_str}"),
            ActionButton("❌ Cancel", f"discard_task:{task_id_str}"),
        ]),
        ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
    ])
    await reporter.fail_step(1, "needs help")
    reporter._footer = (
        f"{label} after {settings.coder_max_retries} attempts.\n"
        f"Keep Trying → resets and retries from scratch\n"
        f"Cancel → discards this task"
    )
    await reporter._force_render(keyboard=kb)
    await _update_task(task_id_str, status="failed",
                       error_message=f"Agent retry exhausted ({reason}). User prompted to continue or cancel.")


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

    # Host-mode: project_dir IS the workspace (mounted into the container).
    # Docker-mode: ws.get_path() returns the per-task /workspaces/task-{id} dir.
    if (task.project.mode or "docker").lower() == "host":
        workspace_path = task.project.project_dir
    else:
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
    reporter.set_steps(["Check health", "Implement", "Build", "Review", "Deploy"])
    await reporter.start()

    _agent_gen = None  # Holds async generator ref for explicit cleanup on cancel
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
                for _i in [1, 2, 3, 4]:
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

        # ── Detect subsequent task in same session ──
        from sqlalchemy import func as sa_func
        from openclow.services.pipeline_cache import get_cached_health, set_health_cache
        is_subsequent_task = False
        try:
            async with async_session() as _sess:
                _count = await _sess.execute(
                    select(sa_func.count(Task.id)).where(
                        Task.chat_id == task.chat_id,
                        Task.project_id == task.project_id,
                        Task.status.in_(("merged", "diff_preview", "awaiting_approval")),
                    )
                )
                is_subsequent_task = (_count.scalar() or 0) > 0
        except Exception:
            pass

        # ── Pre-flight: smart health check + repair + tunnel ──
        await _update_task(task_id_str, status="coding")
        await reporter.start_step(0)

        cached_health = await get_cached_health(task.project_id) if is_subsequent_task else None
        if cached_health is not None:
            _healthy, tunnel_url_for_display = cached_health
            await reporter.complete_step(0, "cached healthy" if _healthy else "cached")
            await _log_to_db(task_id_str, "system", "info", "Health: cache hit, skipping check")
        else:
            _healthy, tunnel_url_for_display = await _ensure_project_healthy(
                task, reporter, task_id_str,
            )
            await set_health_cache(task.project_id, _healthy, tunnel_url_for_display)
            await reporter.complete_step(0, "healthy" if _healthy else "proceeding")

        # ── Step 1: Run Coder Agent with plan (retry loop — never give up) ──
        await reporter.start_step(1)

        coding_attempt = 0
        max_coding_retries = settings.coder_max_retries if settings.coder_retry_enabled else 0
        full_output = ""

        # Web streaming helper
        _is_web = task.chat_provider_type == "web"

        async def _stream_to_web(text: str = "", tool: str = ""):
            if not _is_web or not hasattr(chat, "send_agent_token"):
                return
            try:
                if text:
                    await chat.send_agent_token(task.chat_id, chat_message_id, text)
                if tool:
                    await chat.send_tool_use(task.chat_id, chat_message_id, tool, "", "running")
            except Exception:
                pass

        while coding_attempt <= max_coding_retries:
            try:
                # Reset per-attempt state
                turn_count = 0
                current_step = 0
                last_diff_size = 0
                stall_count = 0
                last_tool_turn = 0
                write_tool_seen = False
                attempt_output = ""
                attempt_start_time = time.time()
                last_productive_time = attempt_start_time

                # Build escalating recovery prompt for retries
                task_description = task.description
                if coding_attempt > 0:
                    task_description = _build_recovery_prompt(
                        task.description, coding_attempt - 1, plan_text
                    )

                _agent_gen = llm.run_coder(
                    workspace_path=workspace_path,
                    task_description=task_description,
                    project_name=task.project.name,
                    tech_stack=task.project.tech_stack or "",
                    description=task.project.description or "",
                    agent_system_prompt=task.project.agent_system_prompt or "",
                    max_turns=0,
                    plan=plan_text,
                    app_container_name=task.project.app_container_name,
                    app_port=task.project.app_port,
                    mode=getattr(task.project, "mode", "docker") or "docker",
                    project_dir=getattr(task.project, "project_dir", None),
                )
                async for message in _agent_gen:
                    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock, StreamEvent

                    # Stream raw tokens to web chat for real-time visibility
                    if isinstance(message, StreamEvent):
                        evt = message.event
                        if evt.get("type") == "content_block_delta":
                            delta = evt.get("delta", {})
                            if delta.get("type") == "text_delta":
                                await _stream_to_web(text=delta.get("text", ""))
                        continue

                    # Only count real assistant messages as turns — skip SDK
                    # metadata events (RateLimitEvent, SystemMessage) which inflate
                    # the count and trigger false stall detection.
                    if not isinstance(message, AssistantMessage):
                        # Still check for max turns and result on non-assistant messages
                        if _is_max_turns_reached(message):
                            raise AgentRetryNeeded("max_turns_reached", turn_count)
                        result_turns = llm.is_result(message)
                        if result_turns is not None:
                            turn_count = result_turns
                        continue

                    turn_count += 1

                    # Track agent text output for STEP_DONE markers
                    # (message is guaranteed to be AssistantMessage here)
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            attempt_output += block.text
                            await _stream_to_web(text=f"\n{block.text}")
                        elif isinstance(block, ToolUseBlock):
                            from openclow.worker.tasks._agent_base import describe_tool
                            tool_desc = describe_tool(block)
                            await _stream_to_web(tool=tool_desc)

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

                    # Track productivity timestamp on file changes and tool use
                    if tool_name and tool_name in ("Edit", "Write"):
                        last_productive_time = time.time()

                    # Stall detection — hybrid: turn-based + time-based fallback
                    # Check every 10 turns after turn 20, OR if >2 min of no productivity
                    time_since_productive = time.time() - last_productive_time
                    should_check_stall = (turn_count % 10 == 0 and turn_count > 20) or time_since_productive > 120

                    if should_check_stall and turn_count > 20:
                        diff_size = await git_ops.diff_size(workspace_path)
                        # With real turns (not SDK events), 5 turns ≈ 2-4 min of work
                        tools_active = (turn_count - last_tool_turn) < 5

                        if diff_size != last_diff_size or write_tool_seen:
                            # Agent is producing file changes — productive
                            if stall_count > 0:
                                log.info("orchestrator.stall_reset", task_id=task_id_str,
                                         turn=turn_count, new_diff_size=diff_size)
                            stall_count = 0
                            last_diff_size = diff_size
                            write_tool_seen = False
                            last_productive_time = time.time()
                        elif tools_active:
                            # No file changes but agent is using tools (reading/exploring)
                            stall_count += 1
                            log.info("orchestrator.exploring", task_id=task_id_str,
                                     turn=turn_count, stall_score=stall_count)
                        else:
                            # No file changes AND no tool activity — real stall
                            stall_count += 2
                            log.warning("orchestrator.stall_warning", task_id=task_id_str,
                                        turn=turn_count, stall_count=stall_count,
                                        time_idle=round(time_since_productive))

                        if stall_count >= 4:
                            log.error("orchestrator.agent_stalled", task_id=task_id_str,
                                      turns=turn_count, last_diff_size=last_diff_size,
                                      time_idle=round(time_since_productive))
                            raise AgentRetryNeeded("stalled", turn_count)

                # Coder finished — check for empty diff
                await git_ops.add_all(workspace_path)
                diff_summary = await git_ops.diff_stat(workspace_path)

                if not diff_summary.strip():
                    raise AgentRetryNeeded("empty_diff", turn_count)

                # Success — keep the output from this attempt
                full_output = attempt_output
                break

            except AgentRetryNeeded as e:
                if coding_attempt < max_coding_retries:
                    coding_attempt += 1
                    await _notify_retry(reporter, coding_attempt, e.reason, task_id_str)
                    await _prepare_retry_workspace(workspace_path)
                    continue
                else:
                    # Max retries exhausted — ask user to continue or cancel
                    await _ask_user_continue_or_cancel(chat, task, reporter, e.reason, task_id_str)
                    return

        await reporter.complete_step(1, f"{turn_count} turns")
        await _log_to_db(task_id_str, "coder", "info",
                         f"Coding complete. Turns: {turn_count}")
        await _update_task(task_id_str, agent_turns=turn_count)

        # ── Step 2: Build — handled by deploy agent (it discovers paths, syncs, builds) ──
        # The deploy agent handles sync + build + verify as one atomic step.
        # It inspects the container to find the right workdir and build command.
        changed = await git_ops.changed_files(workspace_path)
        has_frontend = any(f.endswith(ext) for ext in _FRONTEND_EXTS for f in changed)
        if has_frontend:
            await reporter.start_step(2)
            await reporter.update_step(2, "frontend changes — deploy will build")
            await reporter.complete_step(2, "pending deploy")
        else:
            await reporter.start_step(2)
            await reporter.complete_step(2, "no build needed")

        # ── Step 3: Run Reviewer (skip for quick mode subsequent tasks) ──
        _is_quick_mode = task.status == "coding"
        _skip_review = _is_quick_mode and is_subsequent_task

        if _skip_review:
            await reporter.skip_step(3, "quick mode")
            await _log_to_db(task_id_str, "system", "info",
                             "Review: skipped (quick mode, subsequent task)")
        else:
            await _update_task(task_id_str, status="reviewing")
            await reporter.start_step(3)

            review_gen = llm.run_reviewer(
                workspace_path=workspace_path,
                task_description=task.description,
                project_name=task.project.name,
                tech_stack=task.project.tech_stack or "",
                max_turns=8,
                description=task.project.description or "",
                agent_system_prompt=task.project.agent_system_prompt or "",
            )
            review_output, review_turns = await _run_agent_with_streaming(
                review_gen, reporter, 3, _stream_to_web, _is_web,
                label="review", idle_timeout=90,
            )
            await reporter.update_step(3, f"reviewed ({review_turns} turns)")

            # Parse ReviewResult from collected output
            from openclow.providers.base import ReviewResult
            has_issues = "STATUS: ISSUES" in review_output
            issues = ""
            if has_issues:
                parts = review_output.split("STATUS: ISSUES", 1)
                if len(parts) > 1:
                    issues = parts[1].strip()
            review_result = ReviewResult(has_issues=has_issues, issues=issues, raw_output=review_output)
            await _log_to_db(task_id_str, "reviewer", "info",
                             f"Review: {'ISSUES' if review_result.has_issues else 'APPROVED'} ({review_turns} turns)")

            # Fix loop
            if review_result.has_issues:
                for retry in range(2):
                    await reporter.update_step(3, f"fixing issues (attempt {retry + 1})")
                    fix_gen = llm.run_coder_fix(
                        workspace_path=workspace_path,
                        task_description=task.description,
                        project_name=task.project.name,
                        tech_stack=task.project.tech_stack or "",
                        description=task.project.description or "",
                        agent_system_prompt=task.project.agent_system_prompt or "",
                        issues=review_result.issues,
                        max_turns=10,
                        app_container_name=task.project.app_container_name,
                        app_port=task.project.app_port,
                        mode=getattr(task.project, "mode", "docker") or "docker",
                        project_dir=getattr(task.project, "project_dir", None),
                    )
                    await _run_agent_with_streaming(
                        fix_gen, reporter, 3, _stream_to_web, _is_web,
                        label="fix", idle_timeout=120,
                    )
                    re_review_gen = llm.run_reviewer(
                        workspace_path=workspace_path,
                        task_description=task.description,
                        project_name=task.project.name,
                        tech_stack=task.project.tech_stack or "",
                        max_turns=8,
                        description=task.project.description or "",
                        agent_system_prompt=task.project.agent_system_prompt or "",
                    )
                    rr_output, _ = await _run_agent_with_streaming(
                        re_review_gen, reporter, 3, _stream_to_web, _is_web,
                        label="re-review", idle_timeout=90,
                    )
                    rr_has_issues = "STATUS: ISSUES" in rr_output
                    rr_issues = ""
                    if rr_has_issues:
                        rr_parts = rr_output.split("STATUS: ISSUES", 1)
                        if len(rr_parts) > 1:
                            rr_issues = rr_parts[1].strip()
                    review_result = ReviewResult(has_issues=rr_has_issues, issues=rr_issues, raw_output=rr_output)
                    if not review_result.has_issues:
                        break
            await reporter.complete_step(3, "approved" if not review_result.has_issues else "fixed")

        # ── Step 4: Stage changes + send summary ──
        await git_ops.add_all(workspace_path)
        diff_summary = await git_ops.diff_stat(workspace_path)

        # ── Step 4: Deploy ──
        # Phase A: deterministic sync + build (reliable, no LLM)
        # Phase B: LLM agent for post-deploy actions (migrations, etc.) — only when needed
        await reporter.start_step(4)
        tunnel_url = tunnel_url_for_display

        # Phase A: always deterministic — sync files + build frontend
        await reporter.update_step(4, "syncing files")
        deploy_result = await _run_lightweight_deploy(
            task, workspace_path, diff_summary, tunnel_url,
        )

        # Phase B: LLM agent for post-deploy — only if migrations/config/seeders changed
        _needs_post_deploy = any(
            p in diff_summary for p in (
                "migration", "alembic", "seeder", "config/", ".env",
            )
        )
        # Host mode: the post-deploy agent is docker-only today (uses
        # _mcp_docker, references the project's compose container by name).
        # Skip with a clear log; migrations / config changes for host-mode
        # projects need to be triggered explicitly until we ship a host
        # post-deploy agent.
        _is_host_mode = (task.project.mode or "docker").lower() == "host"
        if _needs_post_deploy and _is_host_mode:
            log.info(
                "orchestrator.host_post_deploy_skipped",
                project=task.project.name,
                diff_summary=diff_summary[:200],
                hint="Run migrations/config changes via the next chat turn — "
                     "the coder agent has host_run_command access.",
            )
            await _log_to_db(
                task_id_str, "system", "info",
                "Post-deploy actions detected (migrations/.env) but skipped — "
                "host-mode post-deploy agent not implemented yet. Run them via "
                "a follow-up chat task.",
            )
        elif _needs_post_deploy:
            await reporter.update_step(4, "post-deploy actions")
            deploy_gen = _deploy_agent_gen(task, workspace_path, diff_summary, tunnel_url)
            deploy_output, deploy_turns = await _run_agent_with_streaming(
                deploy_gen, reporter, 4, _stream_to_web, _is_web,
                label="deploy", idle_timeout=90,
            )
            if "DEPLOY_RESULT:" in deploy_output:
                deploy_result = deploy_output.split("DEPLOY_RESULT:", 1)[1].strip()[:200]
            elif "DEPLOY_BLOCKED:" in deploy_output:
                deploy_result = deploy_output.split("DEPLOY_BLOCKED:", 1)[1].strip()[:200]
            log.info("orchestrator.deploy_done", project=task.project.name,
                     result=deploy_result[:100], turns=deploy_turns)
        else:
            log.info("orchestrator.deploy_done", project=task.project.name,
                     result=deploy_result[:100])

        # Refresh tunnel URL (deploy agent may have restarted it)
        try:
            from openclow.services.tunnel_service import get_tunnel_url
            refreshed = await get_tunnel_url(task.project.name)
            if refreshed:
                tunnel_url = refreshed
        except Exception:
            pass

        # ── Final tunnel attempt: container is healthy but tunnel missing ──
        # Coding may have proceeded without a tunnel. Give the user a link.
        if not tunnel_url and task.project.is_dockerized:
            try:
                from openclow.services.docker_guard import run_docker as _run_docker
                from openclow.services.tunnel_service import start_tunnel, verify_tunnel_url
                rc, ip_out = await _run_docker(
                    "docker", "inspect",
                    "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    f"openclow-{task.project.name}-{task.project.app_container_name or 'app'}-1",
                    actor="orchestrator",
                )
                container_ip = ip_out.strip() if rc == 0 else ""
                port = task.project.app_port or 80
                target = f"http://{container_ip}:{port}" if container_ip else f"http://localhost:{port}"
                tunnel_url = await start_tunnel(task.project.name, target)
                if tunnel_url and await verify_tunnel_url(tunnel_url):
                    await reporter.log(f"🌐 {tunnel_url}")
            except Exception:
                pass

        await reporter.complete_step(4, deploy_result[:40] if deploy_result else "done")

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
        # Stop heartbeat IMMEDIATELY — prevents it from overwriting the
        # cancelled card state that the cancel endpoint already set in DB.
        await reporter.stop()
        # Invalidate health cache — next task must re-verify
        try:
            from openclow.services.pipeline_cache import invalidate_health_cache
            await invalidate_health_cache(task.project_id)
        except Exception:
            pass
        error_msg = "Task timed out" if isinstance(e, TimeoutError) else "Task was cancelled"
        log.warning("orchestrator.coding_interrupted", task_id=task_id_str, reason=error_msg)
        # Kill the Claude SDK subprocess — aclose triggers transport.close() → SIGTERM → SIGKILL
        if _agent_gen is not None:
            try:
                await _agent_gen.aclose()
            except Exception:
                pass
        # Check if user already cancelled via the cancel endpoint — skip card overwrite
        user_cancelled = False
        if task.chat_id.startswith("web:"):
            try:
                import redis.asyncio as aioredis
                parts = task.chat_id.split(":", 2)
                if len(parts) >= 3:
                    r = aioredis.from_url(settings.redis_url)
                    user_cancelled = await r.get(f"openclow:cancel_session:{parts[2]}") is not None
                    await r.aclose()
            except Exception:
                pass
        await _update_task(task_id_str, status="failed",
                           error_message=error_msg,
                           duration_seconds=int(time.time() - start_time))
        if not user_cancelled:
            try:
                running_idx = next((i for i, s in enumerate(reporter.steps) if s["status"] == "running"), 0)
                await reporter.fail_step(running_idx, error_msg[:40])
                reporter._footer = f"{error_msg}. You can retry."
                await reporter._force_render(keyboard=_retry_keyboard(task.project_id if task else None, task_id_str))
            except Exception:
                pass
    except Exception as e:
        error_str = str(e).lower()

        from openclow.worker.tasks._agent_base import is_auth_error

        # Only invalidate health cache for infrastructure-related failures.
        # Agent crashes, auth errors, SDK "Fatal error in message reader",
        # and review rejections are NOT container/tunnel problems — invalidating
        # for these forces the next task into a full LLM repair loop and
        # confuses the user with phantom "Fixing <project>" cards.
        _infra_markers = (
            "container", "docker", "tunnel", "compose", "port ",
            "connection refused", "502", "503", "504",
            "no such host", "dns", "timeout waiting for",
        )
        _is_infra_failure = (
            not is_auth_error(e)
            and any(m in error_str for m in _infra_markers)
        )
        if _is_infra_failure:
            try:
                from openclow.services.pipeline_cache import invalidate_health_cache
                await invalidate_health_cache(task.project_id)
            except Exception:
                pass

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

        elif isinstance(e, AgentRetryNeeded):
            # Should be handled by retry loop, but fallback if it leaked
            from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
            kb = ActionKeyboard(rows=[
                ActionRow([ActionButton("🔄 Retry Task", "menu:task")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ])
            await reporter.fail_step(running_idx, "retry failed")
            reporter._footer = f"Agent couldn't recover after retries ({e.reason}). Try rephrasing the task."
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
            await reporter._force_render(keyboard=_retry_keyboard(task.project_id if task else None, task_id_str))
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

    # ── Branch validation (skip for direct_commit) ──
    if task.git_mode != "direct_commit" and not task.branch_name:
        await _update_task(task_id_str, status="failed", error_message="No branch created")
        await chat.edit_message(task.chat_id, task.chat_message_id or "0", "Cannot create PR — no branch.")
        await chat.close()
        return

    git = await factory.get_git()
    ws = WorkspaceService()

    from openclow.services.checklist_reporter import ChecklistReporter

    # ── Branch approve flow by git_mode ──
    if task.git_mode == "direct_commit":
        checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                     title="Committing to main")
        checklist.set_steps(["Commit changes", "Push to main"])
        await checklist.start()

        try:
            # Host-mode: project_dir IS the workspace.
            if (task.project.mode or "docker").lower() == "host":
                workspace = task.project.project_dir
            else:
                workspace = ws.get_path(task_id_str)
            await _update_task(task_id_str, status="pushing")

            await checklist.start_step(0)
            await git_ops.commit_and_push(workspace, task.project.default_branch or "main",
                                           f"feat: {task.description[:72]}")
            await checklist.complete_step(0, "changes committed")

            await checklist.start_step(1)
            await checklist.complete_step(1, f"pushed to {task.project.default_branch or 'main'}")

            await _update_task(task_id_str, status="merged")
            await _log_to_db(task_id_str, "system", "info", "Committed directly to main")

            checklist._footer = "✅ Changes committed directly to main"
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
            await checklist._force_render(keyboard=ActionKeyboard(rows=rows))

            # Cleanup workspace immediately (no PR to merge)
            await WorkspaceService().cleanup(task_id_str, task.project.name)

        except Exception as e:
            log.error("approve.direct_commit_failed", task_id=task_id_str, error=str(e))
            await _update_task(task_id_str, status="failed", error_message=str(e))
            for i in range(2):
                if checklist.steps[i]["status"] in ("pending", "running"):
                    await checklist.fail_step(i, "failed")
            checklist._footer = f"❌ {str(e)[:300]}"
            await checklist.stop()
            await checklist._force_render(keyboard=_retry_keyboard(task.project_id, task_id_str))
        finally:
            await chat.close()
        return

    # ── branch_per_task and session_branch ──
    checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                 title=f"Creating PR")
    checklist.set_steps(["Commit changes", "Push to GitHub", "Create pull request"])
    await checklist.start()

    try:
        # Host-mode: project_dir IS the workspace.
        if (task.project.mode or "docker").lower() == "host":
            workspace = task.project.project_dir
        else:
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

        # Step 3: Create PR (or reuse existing for session_branch)
        await checklist.start_step(2)
        pr_url = None
        pr_number = None
        if task.git_mode == "session_branch":
            pr_url, pr_number = await git.get_pr_for_branch(task.project.github_repo, task.branch_name)

        if pr_url and pr_number:
            await checklist.complete_step(2, f"PR #{pr_number} (existing)")
        else:
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
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id, task_id_str))
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

    # ── direct_commit tasks are already merged after approve ──
    if task.git_mode == "direct_commit":
        await chat.edit_message(task.chat_id, task.chat_message_id or "0",
                                "Direct commit tasks are already merged. No action needed.")
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
            # Host-mode: pull straight into the live project_dir.
            # Docker-mode: pull into the project _cache so the next task starts fresh.
            if (task.project.mode or "docker").lower() == "host":
                sync_dir = task.project.project_dir
            else:
                sync_dir = os.path.join(settings.workspace_base_path, "_cache", task.project.name)
            if sync_dir and os.path.exists(sync_dir):
                proc = await asyncio.create_subprocess_exec(
                    "git", "pull", "origin", task.project.default_branch or "main",
                    cwd=sync_dir,
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
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id, task_id_str))
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
    # Only close PR for branch_per_task (session_branch keeps PR open for other tasks)
    if task.git_mode == "branch_per_task" and task.pr_number:
        steps.append("Close PR")
    if task.git_mode == "branch_per_task" and task.branch_name:
        steps.append("Delete branch")
    steps.append("Clean workspace")

    checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                 title="Rejecting task")
    checklist.set_steps(steps)
    await checklist.start()

    try:
        step_idx = 0
        if task.git_mode == "branch_per_task" and task.pr_number:
            await checklist.start_step(step_idx)
            await git.close_pr(task.project.github_repo, task.pr_number)
            await checklist.complete_step(step_idx, f"PR #{task.pr_number} closed")
            step_idx += 1

        if task.git_mode == "branch_per_task" and task.branch_name:
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
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id, task_id_str))
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
    # Only delete branch for branch_per_task (session_branch is shared)
    if task.git_mode == "branch_per_task" and task.branch_name:
        steps.append("Delete branch")
    steps.append("Remove workspace")

    checklist = ChecklistReporter(chat, task.chat_id, task.chat_message_id,
                                 title="Discarding changes")
    checklist.set_steps(steps)
    await checklist.start()

    try:
        step_idx = 0
        if task.git_mode == "branch_per_task" and task.branch_name:
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
        await checklist._force_render(keyboard=_retry_keyboard(task.project_id, task_id_str))
    finally:
        await chat.close()
