"""Actions MCP — lets the Chat Agent trigger OpenClow commands.

The chat agent can create tasks, add projects, check status —
all through natural conversation. This is the bridge between
conversational AI and the orchestration engine.
"""
import asyncio
import uuid

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

mcp = FastMCP("actions")


@mcp.tool()
async def list_projects(include_inactive: bool = False) -> str:
    """List all connected projects with their status and config.
    Set include_inactive=True to also show unlinked projects."""
    from openclow.models import Project, async_session
    async with async_session() as session:
        query = select(Project).order_by(Project.name)
        if not include_inactive:
            query = query.where(Project.status == "active")
        result = await session.execute(query)
        projects = result.scalars().all()

    if not projects:
        return "No projects connected yet. Ask the user for a GitHub repo URL to add one."

    lines = []
    for p in projects:
        status = getattr(p, "status", "active")
        status_icon = "🟢" if status == "active" else "🔴"
        docker = f"Docker: {p.app_container_name}:{p.app_port}" if p.is_dockerized else "No Docker"
        lines.append(
            f"- {status_icon} {p.name} [{status}] | {p.tech_stack or 'unknown stack'} | {p.github_repo} | {docker}"
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
    chat_id is the Telegram chat to send updates to."""
    from openclow.models import Project, Task, User, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            projects = await list_projects()
            return f"Project '{project_name}' not found. Available:\n{projects}"

        result = await session.execute(
            select(User).where(User.is_allowed == True).limit(1)
        )
        user = result.scalar_one_or_none()
        if not user:
            return "No authorized users found."

        task_id = uuid.uuid4()
        task = Task(
            id=task_id, user_id=user.id, project_id=project.id,
            description=description, status="pending", chat_id=chat_id,
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
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("onboard_project", repo_url, chat_id, message_id)
    return f"Onboarding started for {repo_url}. I'm cloning and analyzing the project structure."


@mcp.tool()
async def unlink_project(project_name: str, chat_id: str, message_id: str) -> str:
    """Unlink a project — stops Docker containers and tunnel, marks as inactive.
    The project stays in the system and can be re-linked later with bootstrap.
    Use this when a user wants to disconnect a project without deleting it."""
    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if project.status == "inactive":
            return f"Project '{project_name}' is already unlinked."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("unlink_project_task", project_id, chat_id, message_id)
    return f"Unlinking {project_name}. Stopping Docker and tunnel..."


@mcp.tool()
async def remove_project(project_name: str, chat_id: str, message_id: str) -> str:
    """Permanently remove a project — stops Docker, deletes workspace, removes from DB.
    This is destructive and cannot be undone. Use unlink_project for soft disconnect."""
    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("remove_project_task", project_id, chat_id, message_id)
    return f"Removing {project_name} completely. This will delete everything."


@mcp.tool()
async def relink_project(project_name: str, chat_id: str, message_id: str) -> str:
    """Re-link an unlinked project — runs full bootstrap (clone, docker up, health check,
    tunnel, Playwright verify). Use this to reconnect a previously unlinked project."""
    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        if project.status == "active":
            return f"Project '{project_name}' is already active. Use bootstrap to re-setup."
        # Mark active again
        project.status = "active"
        await session.commit()
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id)
    return f"Re-linking {project_name}. Running full bootstrap setup..."


@mcp.tool()
async def docker_up(project_name: str, chat_id: str, message_id: str) -> str:
    """Start Docker containers for a project. Also starts tunnel and verifies with Playwright."""
    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("docker_up_task", project_id, chat_id, message_id)
    return f"Starting Docker for {project_name}..."


@mcp.tool()
async def docker_down(project_name: str, chat_id: str, message_id: str) -> str:
    """Stop Docker containers for a project. Also stops the tunnel."""
    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("docker_down_task", project_id, chat_id, message_id)
    return f"Stopping Docker for {project_name}..."


@mcp.tool()
async def bootstrap(project_name: str, chat_id: str, message_id: str) -> str:
    """Run full bootstrap for a project: clone, env setup, docker build, health check,
    tunnel, Playwright verification. Use when setting up or re-setting up a project."""
    from openclow.models import Project, async_session

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == project_name))
        project = result.scalar_one_or_none()
        if not project:
            return f"Project '{project_name}' not found."
        project_id = project.id

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    await pool.enqueue_job("bootstrap_project", project_id, chat_id, message_id)
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
    await pool.enqueue_job("run_qa_tests", chat_id, message_id, scope)
    return f"QA tests ({scope}) started. Results will appear in Telegram."


if __name__ == "__main__":
    mcp.run(transport="stdio")
