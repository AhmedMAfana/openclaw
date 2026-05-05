"""Admin commands: /addproject, /adduser, /removeproject."""
import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from taghdev.models import Project, User, async_session
from taghdev.utils.logging import get_logger

router = Router()
log = get_logger()


async def _admin_only(message_or_callback, db_user) -> bool:
    """Return True if the user is allowed to proceed."""
    if not db_user or not getattr(db_user, "is_admin", False):
        text = "🔒 Admin only. Ask your workspace admin to upgrade you."
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text)
        else:
            await message_or_callback.answer(text, show_alert=True)
        return False
    return True


async def _fetch_github_repos() -> list[dict]:
    """Fetch GitHub repos — tries MCP worker first, falls back to direct API."""

    # 1. Try MCP/worker path (uses gh CLI with token from DB)
    try:
        from taghdev.worker.arq_app import get_arq_pool
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
        from taghdev.services.config_service import get_config
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

    # Map repo (lowercase) -> (project_id, status) for connected repos
    existing_repos = {}  # github_repo_lower -> (project_id, status)
    try:
        async with async_session() as session:
            result = await session.execute(select(Project.id, Project.github_repo, Project.status))
            existing_repos = {r[1].lower(): (r[0], r[2]) for r in result.all() if r[1]}
    except Exception as e:
        log.warning("admin.db_query_failed", error=str(e))

    buttons = []
    if repos_data:
        for repo_info in repos_data:
            repo = repo_info["name"]
            desc = repo_info.get("desc", "")

            if repo.lower() in existing_repos:
                pid, status = existing_repos[repo.lower()]
                if status == "active":
                    buttons.append([InlineKeyboardButton(
                        text=f"✅ {repo}",
                        callback_data=f"project_detail:{pid}",
                    )])
                elif status == "failed":
                    buttons.append([InlineKeyboardButton(
                        text=f"❌ {repo} — retry setup",
                        callback_data=f"project_detail:{pid}",
                    )])
                elif status == "inactive":
                    buttons.append([InlineKeyboardButton(
                        text=f"⚪ {repo} — re-add",
                        callback_data=f"project_relink:{pid}",
                    )])
                else:  # bootstrapping
                    buttons.append([InlineKeyboardButton(
                        text=f"🔄 {repo} — setting up...",
                        callback_data=f"project_detail:{pid}",
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
async def cmd_add_project(message: Message, db_user):
    """Add a new project. Lists GitHub repos or accepts a URL."""
    if not await _admin_only(message, db_user):
        return
    args = message.text.strip().split(maxsplit=1)

    # If URL provided directly, skip listing
    if len(args) >= 2 and ("github.com" in args[1] or "/" in args[1]):
        repo_url = args[1].strip()
        status_msg = await message.answer(f"⏳ Onboarding {repo_url}...")
        try:
            from taghdev.worker.arq_app import get_arq_pool
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
async def add_repo_selected(callback: CallbackQuery, db_user):
    """User selected a repo to add."""
    if not await _admin_only(callback, db_user):
        return
    repo = callback.data.split(":", 1)[1]
    repo_url = f"https://github.com/{repo}"

    await callback.message.edit_text(f"⏳ Onboarding {repo}...")

    try:
        from taghdev.worker.arq_app import get_arq_pool
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
        from taghdev.worker.arq_app import get_arq_pool
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
        from taghdev.worker.arq_app import get_arq_pool
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
        from taghdev.worker.arq_app import get_arq_pool
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
async def confirm_project(callback: CallbackQuery, db_user):
    """User clicked [Add Project] — save to DB."""
    if not await _admin_only(callback, db_user):
        return
    project_name = callback.data.split(":", 1)[1]

    try:
        from taghdev.worker.arq_app import get_arq_pool
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
async def cmd_add_user(message: Message, db_user):
    """Add an allowed user: /adduser <telegram_id> [username]"""
    if not await _admin_only(message, db_user):
        return
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
async def cmd_remove_project(message: Message, db_user):
    """Remove a project with full cleanup: /removeproject <name>"""
    if not await _admin_only(message, db_user):
        return
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
        from taghdev.worker.arq_app import get_arq_pool
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
async def cmd_docker_up(message: Message, db_user):
    """Start Docker containers: /dockerup <project_name>"""
    if not await _admin_only(message, db_user):
        return
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
        from taghdev.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "docker_up_task", project.id,
            str(message.chat.id), str(status_msg.message_id),
        )
    except Exception as e:
        log.error("dockerup.enqueue_failed", error=str(e))
        await status_msg.edit_text(f"Failed — worker unavailable.")


@router.message(Command("dockerdown"))
async def cmd_docker_down(message: Message, db_user):
    """Stop Docker containers: /dockerdown <project_name>"""
    if not await _admin_only(message, db_user):
        return
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
        from taghdev.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "docker_down_task", project.id,
            str(message.chat.id), str(status_msg.message_id),
        )
    except Exception as e:
        log.error("dockerdown.enqueue_failed", error=str(e))
        await status_msg.edit_text(f"Failed — worker unavailable.")


@router.message(Command("bootstrap"))
async def cmd_bootstrap(message: Message, db_user):
    """Re-bootstrap a project: /bootstrap <project_name>"""
    if not await _admin_only(message, db_user):
        return
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
        from taghdev.worker.arq_app import get_arq_pool
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
async def project_up_callback(callback: CallbackQuery, db_user):
    """Start Docker via inline button."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("▶️ Starting Docker...")
    try:
        from taghdev.worker.arq_app import get_arq_pool
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
async def project_down_callback(callback: CallbackQuery, db_user):
    """Confirm before stopping Docker."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "⚠️ <b>Stop Docker containers?</b>\n\n"
        "This will stop all running containers for this project.\n"
        "The app will go offline until you start them again.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏹ Yes, Stop", callback_data=f"confirm_down:{project_id}"),
                InlineKeyboardButton(text="◀️ Cancel", callback_data=f"project_detail:{project_id}"),
            ],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_down:"))
async def confirm_down_callback(callback: CallbackQuery, db_user):
    """Actually stop Docker after confirmation."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("⏹ Stopping Docker...")
    try:
        from taghdev.worker.arq_app import get_arq_pool
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
async def project_bootstrap_callback(callback: CallbackQuery, db_user):
    """Re-bootstrap via inline button."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔄 Bootstrapping...")
    try:
        from taghdev.worker.arq_app import get_arq_pool
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
async def project_unlink_callback(callback: CallbackQuery, db_user):
    """Confirm before unlinking project."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "⚠️ <b>Unlink this project?</b>\n\n"
        "This will:\n"
        "• Stop all Docker containers\n"
        "• Mark the project as inactive\n"
        "• Remove the tunnel\n\n"
        "You can re-link it later from Add Project.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔗 Yes, Unlink", callback_data=f"confirm_unlink:{project_id}"),
                InlineKeyboardButton(text="◀️ Cancel", callback_data=f"project_detail:{project_id}"),
            ],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_unlink:"))
async def confirm_unlink_callback(callback: CallbackQuery, db_user):
    """Actually unlink after confirmation."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔗 Unlinking project...")
    try:
        from taghdev.worker.arq_app import get_arq_pool
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
async def project_relink_callback(callback: CallbackQuery, db_user):
    """Re-link project via inline button — marks active, runs bootstrap."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🔗 Re-linking project...")
    try:
        # Mark active first
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project:
                project.status = "bootstrapping"
                await session.commit()

        from taghdev.worker.arq_app import get_arq_pool
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
async def project_remove_callback(callback: CallbackQuery, db_user):
    """Confirm before removing project."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "🚨 <b>Remove this project permanently?</b>\n\n"
        "This will:\n"
        "• Stop and remove all Docker containers\n"
        "• Delete the project from the database\n"
        "• Remove the workspace and tunnel\n\n"
        "<b>This cannot be undone.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Yes, Remove", callback_data=f"confirm_remove:{project_id}"),
                InlineKeyboardButton(text="◀️ Cancel", callback_data=f"project_detail:{project_id}"),
            ],
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_remove:"))
async def confirm_remove_callback(callback: CallbackQuery, db_user):
    """Actually remove after confirmation."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])
    await callback.message.edit_text("🗑 Removing project...")
    try:
        from taghdev.worker.arq_app import get_arq_pool
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
async def cmd_qa(message: Message, db_user: User | None = None):
    """Run automated QA tests: /qa [smoke|full]"""
    if not await _admin_only(message, db_user):
        return

    args = message.text.strip().split()
    scope = args[1] if len(args) > 1 else "smoke"
    if scope not in ("smoke", "full"):
        await message.answer("Usage: /qa [smoke|full]")
        return

    status_msg = await message.answer(f"🧪 Starting QA tests ({scope})...")
    try:
        from taghdev.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "run_qa_tests",
            str(message.chat.id), str(status_msg.message_id), scope,
        )
    except Exception as e:
        log.error("qa.enqueue_failed", error=str(e))
        await status_msg.edit_text("Failed to start QA — worker unavailable.")


@router.callback_query(F.data.startswith("assign_channel:"))
async def assign_channel_callback(callback: CallbackQuery, db_user):
    """Show Slack channels to assign to a project."""
    if not await _admin_only(callback, db_user):
        return
    project_id = int(callback.data.split(":")[1])

    await callback.message.edit_text("Loading Slack channels...")

    try:
        from taghdev.services.config_service import get_provider_config, get_config
        _, slack_config = await get_provider_config("chat")
        if not slack_config.get("bot_token"):
            slack_config = await get_config("chat", "provider.slack") or {}

        bot_token = slack_config.get("bot_token")
        if not bot_token:
            await callback.message.edit_text("Slack not configured. Set up Slack in Settings first.")
            await callback.answer()
            return

        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://slack.com/api/conversations.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"types": "public_channel,private_channel", "limit": 20, "exclude_archived": "true"},
            )
            data = resp.json()

        if not data.get("ok"):
            await callback.message.edit_text(f"Slack API error: {data.get('error', 'unknown')}")
            await callback.answer()
            return

        channels = data.get("channels", [])
        if not channels:
            await callback.message.edit_text("No Slack channels found.")
            await callback.answer()
            return

        from taghdev.services.channel_service import get_all_channel_bindings
        bindings = await get_all_channel_bindings()
        bound_channels = {b["channel_id"]: b["project_name"] for b in bindings}

        buttons = []
        for ch in channels:
            ch_id = ch["id"]
            ch_name = ch.get("name", ch_id)
            if ch_id in bound_channels:
                label = f"✅ #{ch_name} → {bound_channels[ch_id]}"
            else:
                label = f"#{ch_name}"
            buttons.append([InlineKeyboardButton(
                text=label[:40],
                callback_data=f"bind_channel:{project_id}:{ch_id}:{ch_name}",
            )])

        buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data=f"project_detail:{project_id}")])

        await callback.message.edit_text(
            "📢 Select a Slack channel to link to this project:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception as e:
        log.error("assign_channel.failed", error=str(e))
        await callback.message.edit_text(f"Failed to load channels: {str(e)[:200]}")
    await callback.answer()


@router.callback_query(F.data.startswith("bind_channel:"))
async def bind_channel_callback(callback: CallbackQuery, db_user):
    """Link a Slack channel to a project."""
    if not await _admin_only(callback, db_user):
        return
    parts = callback.data.split(":")
    project_id = int(parts[1])
    channel_id = parts[2]
    channel_name = parts[3] if len(parts) > 3 else channel_id

    from taghdev.services import project_service
    project = await project_service.get_project_by_id(project_id)
    project_name = project.name if project else "unknown"

    from taghdev.services.channel_service import set_channel_project
    await set_channel_project(channel_id, project_id, project_name)

    await callback.message.edit_text(
        f"✅ #{channel_name} linked to {project_name}\n\n"
        f"Messages in #{channel_name} will now be scoped to this project.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Back to Project", callback_data=f"project_detail:{project_id}")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ]),
    )

    try:
        from taghdev.services.config_service import get_config
        slack_config = await get_config("chat", "provider.slack") or {}
        bot_token = slack_config.get("bot_token")
        if bot_token:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://slack.com/api/conversations.setTopic",
                    headers={"Authorization": f"Bearer {bot_token}"},
                    json={"channel": channel_id, "topic": f"🤖 THAG GROUP: {project_name}"},
                )
    except Exception:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("qa:"))
async def qa_callback(callback: CallbackQuery):
    """Run QA via inline button."""
    scope = callback.data.split(":", 1)[1]
    await callback.message.edit_text(f"🧪 Starting QA tests ({scope})...")
    try:
        from taghdev.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "run_qa_tests",
            str(callback.message.chat.id), str(callback.message.message_id), scope,
        )
    except Exception as e:
        log.error("qa.callback_failed", error=str(e))
        await callback.message.edit_text("Failed — worker unavailable.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Dev mode password
# ---------------------------------------------------------------------------

@router.message(Command("setdevpw"))
async def cmd_set_dev_password(message: Message, db_user):
    """Set the Slack dev mode password: /setdevpw <password>"""
    if not await _admin_only(message, db_user):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Usage: `/setdevpw <password>`\n"
            "Sets the password for Slack `/oc-dev` developer mode.\n"
            "Send `/setdevpw off` to disable.",
            parse_mode="Markdown",
        )
        return

    password = parts[1].strip()
    from taghdev.services import config_service

    if password.lower() == "off":
        await config_service.set_config("system", "dev_password", {"value": ""})
        await message.answer("Dev mode password cleared — `/oc-dev` is now disabled.")
    else:
        await config_service.set_config("system", "dev_password", {"value": password})
        await message.answer("Dev mode password set. Slack users can now use `/oc-dev` to unlock admin commands.")

    # Delete the command message (contains the password in plaintext)
    try:
        await message.delete()
    except Exception:
        pass
