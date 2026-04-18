"""Actions MCP — lets the Chat Agent trigger OpenClow commands.

The chat agent can create tasks, add projects, check status —
all through natural conversation. This is the bridge between
conversational AI and the orchestration engine.
"""
import asyncio
import logging
import sys
import uuid

# MCP stdio protocol uses stdout for JSON-RPC.
# Force ALL stdlib logging (including SQLAlchemy echo) to stderr so it
# never corrupts the JSON-RPC stream the Claude SDK reads on stdout.
logging.basicConfig(stream=sys.stderr, force=True)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

mcp = FastMCP("actions")


async def _fresh_web_message_id(chat_id: str, existing_message_id: str) -> str:
    """For web chat: create a NEW message for worker cards instead of reusing the
    agent's placeholder. This prevents the card from overwriting the agent's text
    response on page refresh (card wins in DB because send_progress_card persists it).
    For non-web providers, returns existing_message_id unchanged."""
    if not chat_id.startswith("web:"):
        return existing_message_id
    try:
        from openclow.providers import factory
        web_chat = await factory.get_chat_by_type("web")
        return await web_chat.send_message(chat_id, "")
    except Exception:
        return existing_message_id


async def _track_job(chat_id: str, job_id: str) -> None:
    """Store enqueued job ID in Redis so the session cancel endpoint can abort it."""
    if not chat_id.startswith("web:"):
        return
    try:
        import redis.asyncio as aioredis
        from openclow.settings import settings
        r = aioredis.from_url(settings.redis_url)
        key = f"openclow:session_jobs:{chat_id}"
        await r.lpush(key, job_id)
        await r.expire(key, 7200)  # 2h TTL — jobs finish long before this
        await r.aclose()
    except Exception:
        pass


def _provider_type(chat_id: str) -> str:
    """Detect chat provider type from chat_id prefix.

    web:{user_id}:{session_id}  → "web"
    slack:{...}                 → "slack"
    {numeric}                   → "telegram"  (Telegram uses bare numeric chat IDs)
    """
    if chat_id.startswith("web:"):
        return "web"
    if chat_id.startswith("slack:"):
        return "slack"
    return "telegram"


async def _get_web_access_context(
    chat_id: str,
) -> tuple[int | None, bool, list[int] | None, str | None]:
    """Parse a web chat_id and return (user_id, is_admin, accessible_project_ids, effective_role).

    Returns (None, False, None, None) for non-web chat_ids — no restriction applied.
    accessible_project_ids=None means unrestricted (admin or no rows configured).
    """
    if not chat_id.startswith("web:"):
        return None, False, None, None

    try:
        user_id = int(chat_id.split(":")[1])
    except (IndexError, ValueError):
        return None, False, None, None

    from openclow.models import User, async_session
    from openclow.services.access_service import get_accessible_projects_for_mcp

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return user_id, False, [], "viewer"
        is_admin = user.is_admin

    projects, effective_role = await get_accessible_projects_for_mcp(user_id, is_admin)
    if effective_role is None:
        return user_id, is_admin, None, None

    return user_id, is_admin, [p.id for p in projects], effective_role


@mcp.tool()
async def list_projects(include_inactive: bool = False, chat_id: str = "") -> str:
    """List connected projects with their status and config.
    Set include_inactive=True to also show unlinked/failed projects.
    Pass chat_id so results are filtered to your accessible projects.

    IMPORTANT: Projects with status 'bootstrapping' are actively being set up.
    Do NOT call bootstrap() again — wait for the current run to finish.
    """
    _, _, accessible_ids, _ = await _get_web_access_context(chat_id)

    from openclow.models import Project, async_session
    async with async_session() as session:
        query = select(Project).order_by(Project.name)
        if not include_inactive:
            # Include bootstrapping + failed so agent knows they exist
            query = query.where(Project.status.in_(["active", "bootstrapping", "failed"]))
        if accessible_ids is not None:
            query = query.where(Project.id.in_(accessible_ids))
        result = await session.execute(query)
        projects = result.scalars().all()

    if not projects:
        # Proactively fetch GitHub repos so the agent has everything in one response
        repos_section = ""
        try:
            from openclow.mcp_servers.github_mcp import list_repos as gh_list_repos
            repos_str = await gh_list_repos(limit=20)
            repos_section = f"\n\nAVAILABLE GITHUB REPOS:\n{repos_str}"
        except Exception:
            repos_section = "\n\n(Could not fetch GitHub repos — check GitHub token config)"
        return (
            f"No projects bootstrapped yet.{repos_section}\n\n"
            "To bootstrap one: call bootstrap(project_name='<name>', chat_id, message_id='') — "
            "handles git clone → Docker → migrations → tunnel automatically."
        )

    lines = []
    for p in projects:
        status = getattr(p, "status", "active")
        status_icon = {"active": "🟢", "bootstrapping": "⏳", "failed": "🔴"}.get(status, "🔴")
        docker = f"Docker: {p.app_container_name}:{p.app_port}" if p.is_dockerized else "No Docker"
        extra = ""
        if status == "bootstrapping":
            extra = " ⚠️ BOOTSTRAP IN PROGRESS — do NOT trigger bootstrap again, wait for it to complete"
        lines.append(
            f"- {status_icon} {p.name} [{status}] | {p.tech_stack or 'unknown stack'} | {p.github_repo} | {docker}{extra}"
        )
    return "\n".join(lines)


@mcp.tool()
async def list_tasks(status: str = "all", limit: int = 10) -> str:
    """List recent tasks. status: all, active, failed, merged, rejected."""
    from openclow.models import Task, async_session

    async with async_session() as session:
        query = select(Task).order_by(Task.created_at.desc()).limit(limit)
        if status == "active":
            active = ["pending", "preparing", "planning", "plan_review", "coding",
                       "reviewing", "diff_preview", "awaiting_approval", "pushing"]
            query = query.where(Task.status.in_(active))
        elif status != "all":
            query = query.where(Task.status == status)

        result = await session.execute(query)
        tasks = result.scalars().all()

    if not tasks:
        return f"No {status} tasks found."

    lines = []
    for t in tasks:
        pr = f" | PR: {t.pr_url}" if t.pr_url else ""
        dur = f" | {t.duration_seconds}s" if t.duration_seconds else ""
        lines.append(f"- [{t.status}] {t.description[:60]}{pr}{dur}")
    return "\n".join(lines)


@mcp.tool()
async def system_status() -> str:
    """Get full system health: database, redis, queue, docker containers."""
    import asyncio
    checks = []

    # Redis
    try:
        import redis.asyncio as aioredis
        from openclow.settings import settings
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        queue_len = await r.zcard("arq:queue")
        checks.append(f"Redis: healthy | queue: {queue_len} jobs")
        await r.aclose()
    except Exception as e:
        checks.append(f"Redis: ERROR — {str(e)[:80]}")

    # PostgreSQL
    try:
        from openclow.models import async_session
        from sqlalchemy import text
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        checks.append("PostgreSQL: healthy")
    except Exception as e:
        checks.append(f"PostgreSQL: ERROR — {str(e)[:80]}")

    # Docker
    try:
        proc = await asyncio.create_subprocess_shell(
            "docker ps --format '{{.Names}}: {{.Status}}' 2>/dev/null | head -20",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        containers = stdout.decode().strip()
        if containers:
            checks.append(f"Docker:\n{containers}")
        else:
            checks.append("Docker: no containers running")
    except Exception:
        checks.append("Docker: unavailable")

    return "\n".join(checks)


@mcp.tool()
async def project_health(project_name: str, auto_fix: bool = True) -> str:
    """Health check for a project: DB status, live tunnel verify, container state.

    Verifies the tunnel URL actually responds (HTTP check) — not just what's in the DB.
    If auto_fix=True (default): automatically restarts a dead tunnel and re-syncs app config.
    Returns full health summary including the verified live URL.

    IMPORTANT — what to do with the result:
    - If "containers: none running" → call docker_up() IMMEDIATELY. Do not report this to the user without fixing it first.
    - If "tunnel: dead" but containers are running → auto_fix already tried to restart. If it failed, call docker_up() to re-verify everything.
    - If "containers: none running" AND tunnel is dead → call docker_up() which will fix both.
    - A tunnel URL shown alongside "containers: none running" is a broken URL — do NOT show it to the user.
    - Only report a live, serving URL — one where containers are up AND tunnel responds.
    """
    from sqlalchemy import select
    from openclow.models import Project, async_session
    import asyncio

    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.name == project_name)
        )
        proj = result.scalar_one_or_none()

    if not proj:
        return f"Project '{project_name}' not found in DB."

    # Tunnel URL is stored in PlatformConfig, not on Project model
    from openclow.services.tunnel_service import get_tunnel_url, start_tunnel
    stored_url = await get_tunnel_url(project_name)
    compose_project = f"openclow-{project_name}"

    # ── Step 1: check containers first — ground truth ──────────────────────
    containers = ""
    containers_running = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps",
            "--filter", f"label=com.docker.compose.project={compose_project}",
            "--format", "{{.Names}}: {{.Status}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        containers = stdout.decode().strip()
        containers_running = bool(containers)
    except Exception:
        pass

    # ── Step 2: check tunnel HTTP — but only trust it if containers are up ──
    tunnel_status = "none"
    tunnel_alive = False
    if stored_url:
        try:
            import httpx
            async with httpx.AsyncClient(follow_redirects=True, timeout=8) as http:
                resp = await http.get(stored_url)
                if resp.status_code < 502 and containers_running:
                    # Tunnel alive AND containers up — genuine healthy state
                    tunnel_status = f"alive: {stored_url}"
                    tunnel_alive = True
                elif resp.status_code < 502 and not containers_running:
                    # Tunnel "responds" but containers are gone — stale/broken state
                    tunnel_status = f"stale (responds HTTP {resp.status_code} but NO containers running)"
                    tunnel_alive = False  # force auto-fix
                else:
                    tunnel_status = f"dead (HTTP {resp.status_code})"
        except Exception:
            tunnel_status = "dead (no response)"

    # ── Step 3: auto-fix — fires when tunnel dead/stale OR containers down ──
    if auto_fix and proj.status == "active" and (not tunnel_alive or not containers_running):
        try:
            from openclow.settings import settings
            import os

            workspace = os.path.join(settings.workspace_base_path, "_cache", project_name)
            service_name = proj.app_container_name or "app"

            # Find container IP for tunnel target
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps",
                "--filter", f"label=com.docker.compose.project={compose_project}",
                "--filter", f"label=com.docker.compose.service={service_name}",
                "--format", "{{.Names}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            container_name = stdout.decode().strip().splitlines()[0] if stdout.decode().strip() else ""

            container_ip = ""
            if container_name:
                proc2 = await asyncio.create_subprocess_exec(
                    "docker", "inspect",
                    "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                    container_name,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out2, _ = await proc2.communicate()
                container_ip = out2.decode().strip()

            port = proj.app_port or 80
            target = f"http://{container_ip}:{port}" if container_ip else None

            if target:
                new_url = await start_tunnel(project_name, target)
                if new_url:
                    if os.path.exists(workspace):
                        from openclow.worker.tasks.bootstrap import _configure_app_for_tunnel
                        await _configure_app_for_tunnel(workspace, new_url, compose_project)
                    tunnel_status = f"restarted: {new_url} (config synced)"
                    tunnel_alive = True
                else:
                    tunnel_status = "dead — tunnel restart failed"
            else:
                if not containers_running:
                    tunnel_status = "dead — no containers running (call docker_up to start them)"
                else:
                    tunnel_status = "dead — no app container found to tunnel to"
        except Exception as e:
            tunnel_status = f"dead — auto-fix failed: {str(e)[:80]}"

    lines = [
        f"status: {proj.status}",
        f"tunnel: {tunnel_status}",
        f"stack: {proj.tech_stack or 'unknown'}",
        f"containers:\n{containers}" if containers_running else "containers: none running",
    ]

    return "\n".join(lines)


@mcp.tool()
async def trigger_task(project_name: str, description: str, chat_id: str, skip_planning: bool = False) -> str:
    """Create a development task and start processing.
    skip_planning=False (default): plan → user approves → code → review → PR.
    skip_planning=True (quick mode): skip plan step, go straight to coding.
    chat_id is the chat to send updates to."""
    from openclow.services.access_service import is_tool_allowed
    user_id, is_admin, accessible_ids, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "trigger_task"):
            return f"Access denied: your role ({effective_role}) does not allow creating coding tasks."

    from openclow.models import Project, Task, User, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            projects = await list_projects(chat_id=chat_id)
            return f"Project '{project_name}' not found. Available:\n{projects}"

        if accessible_ids is not None and project.id not in accessible_ids:
            return f"Access denied: you don't have access to project '{project_name}'."

        # Use the requesting web user if available, otherwise first allowed user
        if user_id:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
        else:
            result = await session.execute(select(User).where(User.is_allowed == True).limit(1))
            user = result.scalar_one_or_none()
        if not user:
            return "No authorized users found."

        task_id = uuid.uuid4()
        task = Task(
            id=task_id, user_id=user.id, project_id=project.id,
            description=description, status="pending", chat_id=chat_id,
            chat_provider_type=_provider_type(chat_id),
        )
        session.add(task)
        await session.commit()

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    job = await pool.enqueue_job("execute_task", str(task_id), skip_planning)
    if job:
        await _track_job(chat_id, job.job_id)

    if skip_planning:
        return f"Task created ({str(task_id)[:8]}). Starting immediately — no plan step. I'll notify you when coding is complete."
    return f"Task created ({str(task_id)[:8]}). I'll analyze the codebase and send you a plan to approve."


@mcp.tool()
async def trigger_addproject(repo_url: str, chat_id: str, message_id: str) -> str:
    """Start onboarding a new project from a GitHub repo URL.
    Will clone, analyze docker setup, detect tech stack, and ask for confirmation."""
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, _, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "trigger_addproject"):
            return f"Access denied: your role ({effective_role}) does not allow adding new projects."

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    card_msg_id = await _fresh_web_message_id(chat_id, message_id)
    job = await pool.enqueue_job("onboard_project", repo_url, chat_id, card_msg_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"Onboarding started for {repo_url}. I'm cloning and analyzing the project structure."


@mcp.tool()
async def unlink_project(project_name: str, chat_id: str, message_id: str) -> str:
    """Unlink a project — stops Docker containers and tunnel, marks as inactive.
    The project stays in the system and can be re-linked later with bootstrap.
    Use this when a user wants to disconnect a project without deleting it."""
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, accessible_ids, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "unlink_project"):
            return f"Access denied: your role ({effective_role}) does not allow unlinking projects."

    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if project.status == "inactive":
            return f"Project '{project_name}' is already unlinked."
        if accessible_ids is not None and project.id not in accessible_ids:
            return f"Access denied: you don't have access to project '{project_name}'."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    card_msg_id = await _fresh_web_message_id(chat_id, message_id)
    job = await pool.enqueue_job("unlink_project_task", project_id, chat_id, card_msg_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"Unlinking {project_name}. Stopping Docker and tunnel..."


@mcp.tool()
async def remove_project(project_name: str, chat_id: str, message_id: str) -> str:
    """Permanently remove a project — stops Docker, deletes workspace, removes from DB.
    This is destructive and cannot be undone. Use unlink_project for soft disconnect."""
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, accessible_ids, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "remove_project"):
            return f"Access denied: your role ({effective_role}) does not allow removing projects."

    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if accessible_ids is not None and project.id not in accessible_ids:
            return f"Access denied: you don't have access to project '{project_name}'."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    card_msg_id = await _fresh_web_message_id(chat_id, message_id)
    job = await pool.enqueue_job("remove_project_task", project_id, chat_id, card_msg_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"Removing {project_name} completely. This will delete everything."


@mcp.tool()
async def relink_project(project_name: str, chat_id: str, message_id: str) -> str:
    """Re-link an unlinked project — runs full bootstrap (clone, docker up, health check,
    tunnel, Playwright verify). Use this to reconnect a previously unlinked project."""
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, accessible_ids, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "relink_project"):
            return f"Access denied: your role ({effective_role}) does not allow relinking projects."

    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if project.status == "active":
            return f"Project '{project_name}' is already active. Use bootstrap to re-setup."
        if accessible_ids is not None and project.id not in accessible_ids:
            return f"Access denied: you don't have access to project '{project_name}'."
        # Mark active again
        project.status = "active"
        await session.commit()
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    card_msg_id = await _fresh_web_message_id(chat_id, message_id)
    job = await pool.enqueue_job("bootstrap_project", project_id, chat_id, card_msg_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"Re-linking {project_name}. Running full bootstrap setup..."


@mcp.tool()
async def docker_up(project_name: str, chat_id: str, message_id: str) -> str:
    """Start Docker containers for a project. Also starts tunnel and verifies with Playwright."""
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, accessible_ids, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "docker_up"):
            return f"Access denied: your role ({effective_role}) does not allow starting Docker."

    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if accessible_ids is not None and project.id not in accessible_ids:
            return f"Access denied: you don't have access to project '{project_name}'."
        project_id = project.id
        project_status = project.status

    # Guard: don't start a docker_up job while bootstrap is already running —
    # both would compete for the same containers and create two progress cards.
    if project_status == "bootstrapping":
        return (
            f"Bootstrap is already running for '{project_name}'. "
            f"Do NOT call docker_up — the bootstrap job handles Docker startup itself. "
            f"Use poll_project_ready('{project_name}') to track progress."
        )

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    card_msg_id = await _fresh_web_message_id(chat_id, message_id)
    job = await pool.enqueue_job("docker_up_task", project_id, chat_id, card_msg_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"Starting Docker for {project_name}..."


@mcp.tool()
async def docker_down(project_name: str, chat_id: str, message_id: str) -> str:
    """Stop Docker containers for a project. Also stops the tunnel."""
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, accessible_ids, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "docker_down"):
            return f"Access denied: your role ({effective_role}) does not allow stopping Docker."

    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if accessible_ids is not None and project.id not in accessible_ids:
            return f"Access denied: you don't have access to project '{project_name}'."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    card_msg_id = await _fresh_web_message_id(chat_id, message_id)
    job = await pool.enqueue_job("docker_down_task", project_id, chat_id, card_msg_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"Stopping Docker for {project_name}..."


@mcp.tool()
async def bootstrap(project_name: str, chat_id: str, message_id: str) -> str:
    """Run full bootstrap for a project: clone, env setup, docker build, health check,
    tunnel, Playwright verification. Use when setting up or re-setting up a project.

    IMPORTANT: Do NOT call this if the project is already 'bootstrapping' — it's already running.
    Do NOT call this if status is 'active' and Docker containers are up — use docker_up instead.
    Only call this for initial setup or after a confirmed failure."""
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, accessible_ids, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "bootstrap"):
            return f"Access denied: your role ({effective_role}) does not allow bootstrapping projects."

    from openclow.models import Project, async_session
    from openclow.services.project_lock import get_lock_holder

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if accessible_ids is not None and project.id not in accessible_ids:
            return f"Access denied: you don't have access to project '{project_name}'."
        project_id = project.id
        project_status = project.status

    # Guard: already bootstrapping — don't pile on
    if project_status == "bootstrapping":
        holder = await get_lock_holder(project_id)
        return (
            f"Bootstrap for '{project_name}' is already in progress (lock held by {holder}). "
            f"Do NOT call bootstrap again. Wait for the worker to finish — it will send a completion message."
        )

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    card_msg_id = await _fresh_web_message_id(chat_id, message_id)
    job = await pool.enqueue_job("bootstrap_project", project_id, chat_id, card_msg_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"Bootstrapping {project_name}. Full setup starting..."


@mcp.tool()
async def run_qa(chat_id: str, message_id: str, scope: str = "smoke") -> str:
    """Run automated QA tests on the Telegram bot using Playwright.

    Args:
        chat_id: Telegram chat ID for reporting results
        message_id: Message ID to update with progress
        scope: "smoke" for basic tests, "full" for all tests including project health
    """
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    job = await pool.enqueue_job("run_qa_tests", chat_id, message_id, scope, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)
    return f"QA tests ({scope}) started. Results will appear in {chat_id.split(':')[0] if ':' in chat_id else 'Telegram'}."


@mcp.tool()
async def check_pending_project(project_name: str) -> str:
    """Check if the onboarding worker has finished analyzing a project.

    Non-blocking — returns current state immediately.
    Call this every 5-10s after trigger_addproject until you see READY.

    Returns:
      "READY — call confirm_project(...)"   — analysis done, config available
      "PENDING — call again in 10s"         — worker still analyzing
    """
    import json
    import redis.asyncio as aioredis
    from openclow.settings import settings

    pending_key = f"openclow:pending_project:{project_name}"
    r = aioredis.from_url(settings.redis_url)
    try:
        data_raw = await r.get(pending_key)
        if not data_raw:
            # Fuzzy match — worker may have slugified the name differently
            all_keys = await r.keys("openclow:pending_project:*")
            for k in all_keys:
                val = await r.get(k)
                if val:
                    candidate = json.loads(val)
                    repo_slug = candidate.get("github_repo", "").split("/")[-1].lower()
                    if (repo_slug == project_name.lower()
                            or project_name.lower() in repo_slug
                            or repo_slug in project_name.lower()):
                        data_raw = val
                        break
    finally:
        await r.aclose()

    if not data_raw:
        return (
            f"PENDING — analysis not ready yet for '{project_name}'. "
            "Call check_pending_project again in 10s, or list_tasks(status='active') to see queue."
        )

    data = json.loads(data_raw)
    actual_name = data.get("name", project_name)
    return (
        f"READY — analysis complete for '{actual_name}' (repo: {data.get('github_repo', '')}):\n"
        f"Tech: {data.get('tech_stack', 'unknown')}\n"
        f"Docker: {'Yes' if data.get('is_dockerized') else 'No'}\n"
        f"Compose: {data.get('docker_compose_file', 'N/A')}\n"
        f"Container: {data.get('app_container_name', 'N/A')}:{data.get('app_port', 'N/A')}\n"
        f"Description: {data.get('description', 'N/A')}\n"
        f"Call confirm_project('{actual_name}', chat_id, message_id) to save and bootstrap."
    )


@mcp.tool()
async def poll_project_ready(project_name: str) -> str:
    """Check if a project has finished bootstrapping and is live.

    Non-blocking — returns the current status immediately.
    Call this every 10-15s after bootstrap() until you see LIVE or FAILED.

    Returns:
      "LIVE: <url>"      — project is up, tunnel is live
      "FAILED"           — bootstrap crashed, report to user and ask if they want to retry
      "BUILDING: ..."    — still in progress, call again in 10-15s
    """
    from sqlalchemy import select
    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.name == project_name)
        )
        proj = result.scalar_one_or_none()

    if not proj:
        return f"Project '{project_name}' not found in DB."
    if proj.status == "active" and proj.tunnel_url:
        return f"LIVE: {proj.tunnel_url}"
    if proj.status == "failed":
        return "FAILED — bootstrap did not complete. Report this to the user and ask them whether to retry."

    return f"BUILDING — status: {proj.status}. Call poll_project_ready('{project_name}') again in 15s."


@mcp.tool()
async def confirm_project(project_name: str, chat_id: str, message_id: str) -> str:
    """Confirm a pending project (save to DB) and immediately start full bootstrap.

    Call this after check_pending_project confirms the analysis is ready.
    This saves the project config to DB and queues docker build + tunnel.
    The user will see live progress updates in chat.
    """
    from openclow.services.access_service import is_tool_allowed
    _, is_admin, _, effective_role = await _get_web_access_context(chat_id)
    if chat_id.startswith("web:") and not is_admin:
        if not is_tool_allowed(effective_role, "confirm_project"):
            return f"Access denied: your role ({effective_role}) does not allow confirming new projects."

    import json
    import redis.asyncio as aioredis
    from openclow.settings import settings
    from openclow.models import Project, async_session
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    r = aioredis.from_url(settings.redis_url)
    pending_key = f"openclow:pending_project:{project_name}"

    async def _scan_pending() -> tuple[bytes | None, str]:
        """Try exact key then fuzzy-match all pending keys. Returns (data_raw, key)."""
        raw = await r.get(pending_key)
        if raw:
            return raw, pending_key
        all_keys = await r.keys("openclow:pending_project:*")
        for k in all_keys:
            val = await r.get(k)
            if val:
                candidate = json.loads(val)
                repo_slug = candidate.get("github_repo", "").split("/")[-1].lower()
                if (repo_slug == project_name.lower()
                        or project_name.lower() in repo_slug
                        or repo_slug in project_name.lower()):
                    return val, (k.decode() if isinstance(k, bytes) else k)
        return None, pending_key

    data_raw, pending_key = await _scan_pending()

    if data_raw:
        await r.delete(pending_key)
    await r.aclose()

    if not data_raw:
        # Not ready yet or already confirmed — check if project exists in DB
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.name == project_name))
            existing = result.scalar_one_or_none()
        if existing:
            project_id = existing.id
        else:
            return (
                f"No pending config for '{project_name}' — analysis may not be done yet. "
                "Call check_pending_project first. If PENDING, wait and retry. "
                "If still missing, call trigger_addproject again."
            )
    else:
        data = json.loads(data_raw)
        async with async_session() as session:
            # Check if already exists
            result = await session.execute(select(Project).where(Project.name == data["name"]))
            existing = result.scalar_one_or_none()
            if existing:
                project_id = existing.id
            else:
                project = Project(
                    name=data["name"],
                    github_repo=data["github_repo"],
                    default_branch="main",
                    tech_stack=data.get("tech_stack"),
                    description=data.get("description"),
                    is_dockerized=data.get("is_dockerized", True),
                    docker_compose_file=data.get("docker_compose_file"),
                    app_container_name=data.get("app_container_name"),
                    app_port=data.get("app_port"),
                    setup_commands=data.get("setup_commands"),
                    status="bootstrapping",
                )
                session.add(project)
                try:
                    await session.commit()
                    await session.refresh(project)
                    project_id = project.id
                except IntegrityError:
                    await session.rollback()
                    return f"Project '{project_name}' already exists in DB."

    # Queue bootstrap immediately
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    job = await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id, _provider_type(chat_id))
    if job:
        await _track_job(chat_id, job.job_id)

    return (
        f"Project '{project_name}' saved (id={project_id}) and bootstrap queued. "
        "Worker is now: docker build → containers up → migrations → tunnel. "
        "Progress updates will appear in chat. "
        "Call list_projects() after ~2 minutes to get the tunnel URL."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
