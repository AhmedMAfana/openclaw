"""Admin commands: /addproject, /adduser, /removeproject."""
import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from openclow.models import Project, User, async_session
from openclow.utils.logging import get_logger

router = Router()
log = get_logger()


async def _fetch_github_repos() -> list[dict]:
    """Fetch GitHub repos — tries MCP worker first, falls back to direct API."""

    # 1. Try MCP/worker path (uses gh CLI with token from DB)
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        job = await pool.enqueue_job("list_github_repos")
        repos_data = await job.result(timeout=15)
        if repos_data:
            log.info("github.repos_fetched_via_worker", count=len(repos_data))
            return repos_data
    except Exception as e:
        log.warning("github.worker_fetch_failed", error=str(e))

    # 2. Fallback: direct GitHub API (no worker/gh CLI dependency)
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if not config or not config.get("token"):
            log.error("github.no_token_configured")
            return []

        token = config["token"]
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"per_page": 30, "sort": "updated", "affiliation": "owner,collaborator,organization_member"},
            )
            if resp.status_code != 200:
                log.error("github.api_failed", status=resp.status_code, body=resp.text[:200])
                return []

            repos = resp.json()
            log.info("github.repos_fetched_via_api", count=len(repos))
            return [
                {"name": r.get("full_name", ""), "desc": r.get("description", "") or ""}
                for r in repos
            ]
    except Exception as e:
        log.error("github.fetch_repos_failed", error=str(e))
        return []


async def _build_repo_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    """Build the repo selection keyboard. Returns (header_text, keyboard)."""
    repos_data = await _fetch_github_repos()

    # Map repo (lowercase) -> project id for connected repos (case-insensitive match)
    existing_repos = {}  # github_repo_lower -> project_id
    try:
        async with async_session() as session:
            result = await session.execute(select(Project.id, Project.github_repo))
            existing_repos = {r[1].lower(): r[0] for r in result.all() if r[1]}
    except Exception:
        pass

    buttons = []
    if repos_data:
        for repo_info in repos_data:
            repo = repo_info["name"]
            desc = repo_info.get("desc", "")

            if repo.lower() in existing_repos:
                buttons.append([InlineKeyboardButton(
                    text=f"✅ {repo}",
                    callback_data=f"health:{existing_repos[repo.lower()]}",
                )])
            else:
                label = repo
                if desc:
                    label += f" — {desc[:30]}"
                buttons.append([InlineKeyboardButton(
                    text=label,
                    callback_data=f"add_repo:{repo}",
                )])

    buttons.append([InlineKeyboardButton(text="📝 Enter repo URL", callback_data="add_repo_manual")])
    buttons.append([InlineKeyboardButton(text="🔄 Retry fetch", callback_data="add_repo_retry")])
    buttons.append([InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")])

    if repos_data:
        header = "Select a repository to add:"
    else:
        header = (
            "Could not fetch repos.\n\n"
            "Possible causes:\n"
            "• GitHub token not configured (run setup wizard)\n"
            "• Token lacks repo permissions\n\n"
            "Add a repo manually or retry:"
        )

    return header, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("addproject"))
async def cmd_add_project(message: Message):
    """Add a new project. Lists GitHub repos or accepts a URL."""
    args = message.text.strip().split(maxsplit=1)

    # If URL provided directly, skip listing
    if len(args) >= 2 and ("github.com" in args[1] or "/" in args[1]):
        repo_url = args[1].strip()
        status_msg = await message.answer(f"⏳ Onboarding {repo_url}...")
        try:
            from openclow.worker.arq_app import get_arq_pool
            pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
            await pool.enqueue_job("onboard_project", repo_url, str(message.chat.id), str(status_msg.message_id))
        except Exception as e:
            log.error("addproject.enqueue_failed", error=str(e))
            await status_msg.edit_text("Failed to start onboarding. Check worker/Redis status.")
        return

    # No URL — fetch repos directly via GitHub API
    status_msg = await message.answer("Fetching your GitHub repositories...")
    header, keyboard = await _build_repo_keyboard()
    await status_msg.edit_text(header, reply_markup=keyboard)


@router.callback_query(F.data.startswith("add_repo:"))
async def add_repo_selected(callback: CallbackQuery):
    """User selected a repo to add."""
    repo = callback.data.split(":", 1)[1]
    repo_url = f"https://github.com/{repo}"

    await callback.message.edit_text(f"⏳ Onboarding {repo}...")

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "onboard_project", repo_url,
            str(callback.message.chat.id),
            str(callback.message.message_id),
        )
    except Exception as e:
        log.error("addproject.enqueue_failed", error=str(e))
        await callback.message.edit_text(
            f"Failed to start onboarding for {repo}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Retry", callback_data=f"add_repo:{repo}")],
            ]),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("health:"))
async def health_check(callback: CallbackQuery):
    """User tapped a project — run real-time health check."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔍 Running health check...")

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "check_project_health", project_id,
            str(callback.message.chat.id),
            str(callback.message.message_id),
        )
    except Exception as e:
        log.error("health.enqueue_failed", error=str(e))
        await callback.message.edit_text("Health check failed — worker unavailable.")
    await callback.answer()


@router.callback_query(F.data.startswith("health_ref:"))
async def health_refresh(callback: CallbackQuery):
    """Refresh health check."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔍 Refreshing health check...")

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "check_project_health", project_id,
            str(callback.message.chat.id),
            str(callback.message.message_id),
        )
    except Exception as e:
        log.error("health.refresh_failed", error=str(e))
        await callback.message.edit_text("Health check failed — worker unavailable.")
    await callback.answer()


@router.callback_query(F.data.startswith("tunnel_stop:"))
async def tunnel_stop(callback: CallbackQuery):
    """Stop a running tunnel."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("Stopping tunnel...")

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "stop_tunnel_task", project_id,
            str(callback.message.chat.id),
            str(callback.message.message_id),
        )
    except Exception as e:
        log.error("tunnel.stop_failed", error=str(e))
        await callback.message.edit_text("Failed to stop tunnel.")
    await callback.answer()


@router.callback_query(F.data == "add_repo_manual")
async def add_repo_manual(callback: CallbackQuery):
    """User wants to enter URL manually."""
    await callback.message.edit_text(
        "Send the repository URL:\n\n"
        "Example: /addproject https://github.com/owner/repo"
    )
    await callback.answer()


@router.callback_query(F.data == "add_repo_retry")
async def add_repo_retry(callback: CallbackQuery):
    """Retry fetching GitHub repos."""
    await callback.answer("Retrying...")
    await callback.message.edit_text("Fetching your GitHub repositories...")
    header, keyboard = await _build_repo_keyboard()
    await callback.message.edit_text(header, reply_markup=keyboard)


@router.callback_query(F.data.startswith("confirm_project:"))
async def confirm_project(callback: CallbackQuery):
    """User clicked [Add Project] — save to DB."""
    project_name = callback.data.split(":", 1)[1]

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)

        # Save project to DB
        job = await pool.enqueue_job("confirm_project", project_name)
        project_id = await job.result(timeout=10)

        if isinstance(project_id, dict) and "error" in project_id:
            # Error from confirm_project (expired Redis data, duplicate name, etc.)
            error_msg = project_id.get("message", "Something went wrong.")
            await callback.message.edit_text(
                f"⚠️ {error_msg}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Re-run /addproject", callback_data="menu:addproject")],
                    [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
            )
        elif project_id:
            # Trigger bootstrap — auto-setup Docker, deps, migrations
            await callback.message.edit_text(
                f"Project '{project_name}' added!\n\n"
                f"Setting up Docker environment..."
            )
            await pool.enqueue_job(
                "bootstrap_project",
                project_id,
                str(callback.message.chat.id),
                str(callback.message.message_id),
            )
        else:
            await callback.message.edit_text(
                f"⚠️ Failed to add project '{project_name}'. Please try again.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Re-run /addproject", callback_data="menu:addproject")],
                    [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
            )

    except Exception as e:
        log.error("confirm_project.failed", error=str(e))
        await callback.message.edit_text(
            f"Failed to confirm project '{project_name}'.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Retry", callback_data=f"confirm_project:{project_name}")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_project")],
            ]),
        )
    await callback.answer()


@router.callback_query(F.data == "cancel_project")
async def cancel_project(callback: CallbackQuery):
    await callback.message.edit_text(
        "Project onboarding cancelled.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Project", callback_data="menu:addproject")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ]),
    )
    await callback.answer()


@router.message(Command("adduser"))
async def cmd_add_user(message: Message):
    """Add an allowed user: /adduser <telegram_id> [username]"""
    args = message.text.strip().split()
    if len(args) < 2:
        await message.answer(
            "Usage: /adduser <telegram_id> [username]\n\n"
            "Example: /adduser 123456789 @ahmed\n"
            "Get ID from @userinfobot"
        )
        return

    telegram_id = args[1].strip()
    username = args[2].strip() if len(args) > 2 else None

    async with async_session() as session:
        existing = await session.execute(
            select(User).where(User.chat_provider_uid == telegram_id)
        )
        user = existing.scalar_one_or_none()
        if user:
            user.is_allowed = True
            if username:
                user.username = username
            await session.commit()
            await message.answer(f"User {telegram_id} updated and authorized ✅")
        else:
            user = User(
                chat_provider_type="telegram",
                chat_provider_uid=telegram_id,
                username=username,
                is_allowed=True,
            )
            session.add(user)
            await session.commit()
            await message.answer(f"User {telegram_id} added and authorized ✅")

    log.info("admin.adduser", telegram_id=telegram_id)


@router.message(Command("removeproject"))
async def cmd_remove_project(message: Message):
    """Remove a project with full cleanup: /removeproject <name>"""
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /removeproject <project_name>")
        return

    name = args[1].strip()
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == name))
        project = result.scalar_one_or_none()
        if not project:
            await message.answer(f"Project '{name}' not found.")
            return

    status_msg = await message.answer(f"🗑 Removing {name}...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "remove_project_task", project.id,
            str(message.chat.id), str(status_msg.message_id),
        )
    except Exception as e:
        log.error("removeproject.enqueue_failed", error=str(e))
        await status_msg.edit_text(f"Failed to remove {name} — worker unavailable.")


# ---------------------------------------------------------------------------
# Project lifecycle commands
# ---------------------------------------------------------------------------

async def _find_project_by_name(name: str):
    """Look up a project by name."""
    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.name == name))
        return result.scalar_one_or_none()


@router.message(Command("dockerup"))
async def cmd_docker_up(message: Message):
    """Start Docker containers: /dockerup <project_name>"""
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /dockerup <project_name>")
        return

    name = args[1].strip()
    project = await _find_project_by_name(name)
    if not project:
        await message.answer(f"Project '{name}' not found.")
        return

    status_msg = await message.answer(f"▶️ Starting Docker for {name}...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "docker_up_task", project.id,
            str(message.chat.id), str(status_msg.message_id),
        )
    except Exception as e:
        log.error("dockerup.enqueue_failed", error=str(e))
        await status_msg.edit_text(f"Failed — worker unavailable.")


@router.message(Command("dockerdown"))
async def cmd_docker_down(message: Message):
    """Stop Docker containers: /dockerdown <project_name>"""
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /dockerdown <project_name>")
        return

    name = args[1].strip()
    project = await _find_project_by_name(name)
    if not project:
        await message.answer(f"Project '{name}' not found.")
        return

    status_msg = await message.answer(f"⏹ Stopping Docker for {name}...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "docker_down_task", project.id,
            str(message.chat.id), str(status_msg.message_id),
        )
    except Exception as e:
        log.error("dockerdown.enqueue_failed", error=str(e))
        await status_msg.edit_text(f"Failed — worker unavailable.")


@router.message(Command("bootstrap"))
async def cmd_bootstrap(message: Message):
    """Re-bootstrap a project: /bootstrap <project_name>"""
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /bootstrap <project_name>")
        return

    name = args[1].strip()
    project = await _find_project_by_name(name)
    if not project:
        await message.answer(f"Project '{name}' not found.")
        return

    status_msg = await message.answer(f"🔄 Bootstrapping {name}...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "bootstrap_project", project.id,
            str(message.chat.id), str(status_msg.message_id),
        )
    except Exception as e:
        log.error("bootstrap.enqueue_failed", error=str(e))
        await status_msg.edit_text(f"Failed — worker unavailable.")


# ---------------------------------------------------------------------------
# Inline button callbacks for project lifecycle
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("project_up:"))
async def project_up_callback(callback: CallbackQuery):
    """Start Docker via inline button."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("▶️ Starting Docker...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "docker_up_task", project_id,
            str(callback.message.chat.id), str(callback.message.message_id),
        )
    except Exception as e:
        log.error("project_up.failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()


@router.callback_query(F.data.startswith("project_down:"))
async def project_down_callback(callback: CallbackQuery):
    """Stop Docker via inline button."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("⏹ Stopping Docker...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "docker_down_task", project_id,
            str(callback.message.chat.id), str(callback.message.message_id),
        )
    except Exception as e:
        log.error("project_down.failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()


@router.callback_query(F.data.startswith("project_bootstrap:"))
async def project_bootstrap_callback(callback: CallbackQuery):
    """Re-bootstrap via inline button."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔄 Bootstrapping...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "bootstrap_project", project_id,
            str(callback.message.chat.id), str(callback.message.message_id),
        )
    except Exception as e:
        log.error("project_bootstrap.failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()


@router.callback_query(F.data.startswith("project_unlink:"))
async def project_unlink_callback(callback: CallbackQuery):
    """Unlink project via inline button — stops Docker, marks inactive."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔗 Unlinking project...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "unlink_project_task", project_id,
            str(callback.message.chat.id), str(callback.message.message_id),
        )
    except Exception as e:
        log.error("project_unlink.failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()


@router.callback_query(F.data.startswith("project_relink:"))
async def project_relink_callback(callback: CallbackQuery):
    """Re-link project via inline button — marks active, runs bootstrap."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔗 Re-linking project...")
    try:
        # Mark active first
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project:
                project.status = "active"
                await session.commit()

        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "bootstrap_project", project_id,
            str(callback.message.chat.id), str(callback.message.message_id),
        )
    except Exception as e:
        log.error("project_relink.failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()


@router.callback_query(F.data.startswith("project_remove:"))
async def project_remove_callback(callback: CallbackQuery):
    """Remove project via inline button."""
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🗑 Removing project...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "remove_project_task", project_id,
            str(callback.message.chat.id), str(callback.message.message_id),
        )
    except Exception as e:
        log.error("project_remove.failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()


# ---------------------------------------------------------------------------
# QA Testing
# ---------------------------------------------------------------------------

@router.message(Command("qa"))
async def cmd_qa(message: Message):
    """Run automated QA tests: /qa [smoke|full]"""
    args = message.text.strip().split()
    scope = args[1] if len(args) > 1 else "smoke"
    if scope not in ("smoke", "full"):
        await message.answer("Usage: /qa [smoke|full]")
        return

    status_msg = await message.answer(f"🧪 Starting QA tests ({scope})...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "run_qa_tests",
            str(message.chat.id), str(status_msg.message_id), scope,
        )
    except Exception as e:
        log.error("qa.enqueue_failed", error=str(e))
        await status_msg.edit_text("Failed to start QA — worker unavailable.")


@router.callback_query(F.data.startswith("qa:"))
async def qa_callback(callback: CallbackQuery):
    """Run QA via inline button."""
    scope = callback.data.split(":", 1)[1]
    await callback.message.edit_text(f"🧪 Starting QA tests ({scope})...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "run_qa_tests",
            str(callback.message.chat.id), str(callback.message.message_id), scope,
        )
    except Exception as e:
        log.error("qa.callback_failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()
