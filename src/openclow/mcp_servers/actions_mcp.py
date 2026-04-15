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
        queue_len = await r.llen("arq:queue")
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
async def trigger_task(project_name: str, description: str, chat_id: str) -> str:
    """Create a development task and start processing. Goes through:
    plan → user approves → code → review → PR.
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
    await pool.enqueue_job("execute_task", str(task_id))

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
    await pool.enqueue_job("onboard_project", repo_url, chat_id, message_id, _provider_type(chat_id))
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
    await pool.enqueue_job("unlink_project_task", project_id, chat_id, message_id, _provider_type(chat_id))
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
    await pool.enqueue_job("remove_project_task", project_id, chat_id, message_id, _provider_type(chat_id))
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
    await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id, _provider_type(chat_id))
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

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("docker_up_task", project_id, chat_id, message_id, _provider_type(chat_id))
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
    await pool.enqueue_job("docker_down_task", project_id, chat_id, message_id, _provider_type(chat_id))
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
    await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id, _provider_type(chat_id))
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
    await pool.enqueue_job("run_qa_tests", chat_id, message_id, scope, _provider_type(chat_id))
    return f"QA tests ({scope}) started. Results will appear in {chat_id.split(':')[0] if ':' in chat_id else 'Telegram'}."


@mcp.tool()
async def check_pending_project(project_name: str, wait_seconds: int = 90) -> str:
    """Wait for the onboarding worker to finish analyzing a project, then return its config.

    Call this AFTER trigger_addproject. It polls Redis until the analysis is done
    (up to wait_seconds). Returns the config when ready, or a timeout message.
    Use this to get the config so you can call confirm_project next.
    """
    import json
    import redis.asyncio as aioredis
    from openclow.settings import settings

    pending_key = f"openclow:pending_project:{project_name}"
    waited = 0
    poll_interval = 3

    r = aioredis.from_url(settings.redis_url)
    try:
        while waited < wait_seconds:
            # Try exact key first, then scan all pending keys (handles name mismatch)
            data_raw = await r.get(pending_key)
            if not data_raw:
                all_keys = await r.keys("openclow:pending_project:*")
                for k in all_keys:
                    val = await r.get(k)
                    if val:
                        candidate = json.loads(val)
                        # Match by github_repo slug or any key containing project_name
                        repo_slug = candidate.get("github_repo", "").split("/")[-1].lower()
                        if (repo_slug == project_name.lower()
                                or project_name.lower() in repo_slug
                                or repo_slug in project_name.lower()):
                            data_raw = val
                            break
            if data_raw:
                data = json.loads(data_raw)
                actual_name = data.get("name", project_name)
                return (
                    f"Analysis complete for '{actual_name}' (repo: {data.get('github_repo', '')}):\n"
                    f"Tech: {data.get('tech_stack', 'unknown')}\n"
                    f"Docker: {'Yes' if data.get('is_dockerized') else 'No'}\n"
                    f"Compose: {data.get('docker_compose_file', 'N/A')}\n"
                    f"Container: {data.get('app_container_name', 'N/A')}:{data.get('app_port', 'N/A')}\n"
                    f"Description: {data.get('description', 'N/A')}\n"
                    f"READY — call confirm_project('{actual_name}', chat_id, message_id) to save and bootstrap."
                )
            await asyncio.sleep(poll_interval)
            waited += poll_interval
    finally:
        await r.aclose()

    return (
        f"Timeout after {wait_seconds}s waiting for '{project_name}' analysis. "
        "The worker may still be running — call check_pending_project again to keep waiting, "
        "or call list_tasks(status='active') to see queue status."
    )


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
    data_raw = await r.get(pending_key)
    if not data_raw:
        # Scan all pending keys — handles name mismatch between repo slug and detected app name
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
                    pending_key = k.decode() if isinstance(k, bytes) else k
                    break
    if data_raw:
        await r.delete(pending_key)
    await r.aclose()

    if not data_raw:
        # Already confirmed or expired — check if project exists in DB
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.name == project_name))
            existing = result.scalar_one_or_none()
        if existing:
            # Already in DB — just bootstrap it
            project_id = existing.id
        else:
            return (
                f"No pending config for '{project_name}' (expired or not found). "
                "Call trigger_addproject again to restart onboarding."
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
    await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id, _provider_type(chat_id))

    return (
        f"Project '{project_name}' saved (id={project_id}) and bootstrap queued. "
        "Worker is now: docker build → containers up → migrations → tunnel. "
        "Progress updates will appear in chat. "
        "Call list_projects() after ~2 minutes to get the tunnel URL."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
