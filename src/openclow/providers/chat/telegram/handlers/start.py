"""Bot commands: /start, /help, /projects, /status, /cancel, /dashboard."""
import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from openclow.models import Task, TaskLog, async_session
from openclow.services import project_service
from openclow.utils.logging import get_logger

router = Router()
log = get_logger()


# ──────────────────────────────────────────────
# Main menu keyboard
# ──────────────────────────────────────────────

def main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="🚀 New Task", callback_data="menu:task"),
            InlineKeyboardButton(text="📂 Projects", callback_data="menu:projects"),
        ],
        [
            InlineKeyboardButton(text="📊 Status", callback_data="menu:status"),
            InlineKeyboardButton(text="❓ Help", callback_data="menu:help"),
        ],
    ]
    if is_admin:
        buttons.insert(1, [
            InlineKeyboardButton(text="📋 Logs", callback_data="menu:logs"),
            InlineKeyboardButton(text="📈 Dashboard", callback_data="menu:dashboard"),
        ])
        buttons.insert(2, [
            InlineKeyboardButton(text="➕ Add Project", callback_data="menu:addproject"),
            InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
    ])


from openclow.utils.messaging import WELCOME_MESSAGE, HELP_MESSAGE

WELCOME_TEXT = WELCOME_MESSAGE.replace("THAG GROUP", "<b>THAG GROUP</b>").replace("/help", "/help 👇")

HELP_TEXT = HELP_MESSAGE


# ──────────────────────────────────────────────
# /start and /help
# ──────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, db_user):
    await message.answer(
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(is_admin=bool(db_user.is_admin)),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        HELP_TEXT,
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Menu callbacks
# ──────────────────────────────────────────────

async def _safe_edit_or_send(callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None):
    """Edit the callback message. If it fails (old/deleted), send a new one."""
    try:
        await callback.message.edit_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
        )
    except Exception:
        # Edit failed — message is old or deleted. Send new message instead.
        await callback.message.answer(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
        )


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, db_user):
    await _safe_edit_or_send(callback, WELCOME_TEXT, main_menu_keyboard(is_admin=bool(db_user.is_admin)), "HTML")
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery):
    await _safe_edit_or_send(callback, HELP_TEXT, back_to_menu_keyboard(), "HTML")
    await callback.answer()


@router.callback_query(F.data == "menu:task")
async def menu_task(callback: CallbackQuery, state: FSMContext):
    """Start task flow directly from menu button."""
    from openclow.providers.chat.telegram.keyboards import project_keyboard
    from openclow.providers.chat.telegram.states import TaskStates

    projects = await project_service.get_all_projects()
    if not projects:
        await _safe_edit_or_send(
            callback,
            "No projects configured yet.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Add Project", callback_data="menu:addproject")],
                [InlineKeyboardButton(text="Main Menu", callback_data="menu:main")],
            ]),
            "HTML",
        )
        await callback.answer()
        return

    await _safe_edit_or_send(
        callback,
        "Select a project:",
        reply_markup=project_keyboard(projects),
    )
    await state.set_state(TaskStates.choosing_project)
    await callback.answer()


@router.callback_query(F.data.startswith("task_for:"))
async def task_for_project(callback: CallbackQuery, state: FSMContext):
    """Start task flow with project pre-selected."""
    from openclow.providers.chat.telegram.states import TaskStates

    project_id = int(callback.data.split(":")[1])

    # Get project name
    project_name = "project"
    try:
        project = await project_service.get_project_by_id(project_id)
        if project:
            project_name = project.name
    except Exception:
        pass

    await state.update_data(project_id=project_id, project_name=project_name)

    await _safe_edit_or_send(
        callback,
        f"📦 Project: <b>{project_name}</b>\n\nDescribe your task:",
        parse_mode="HTML",
    )
    await state.set_state(TaskStates.entering_description)
    await callback.answer()


@router.callback_query(F.data == "menu:addproject")
async def menu_addproject(callback: CallbackQuery):
    """Start addproject flow — fetch repos and show selection keyboard."""
    await callback.message.edit_text("Fetching your GitHub repositories...")
    await callback.answer()

    from openclow.providers.chat.telegram.handlers.admin import _build_repo_keyboard
    header, keyboard = await _build_repo_keyboard()
    await callback.message.edit_text(header, reply_markup=keyboard)


@router.callback_query(F.data == "menu:projects")
async def menu_projects(callback: CallbackQuery):
    projects = await project_service.get_all_projects(include_inactive=True)
    if not projects:
        await callback.message.edit_text(
            "📂 <b>No projects connected</b>\n\n"
            "Use ➕ Add Project to connect a GitHub repo.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Add Project", callback_data="menu:addproject")],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    status_icons = {"active": "🟢", "failed": "🔴", "bootstrapping": "🔄", "inactive": "⚪"}
    buttons = []
    for p in projects:
        icon = status_icons.get(p.status, "❓")
        label = f"{icon} {p.name} — {p.tech_stack or 'N/A'}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"project_detail:{p.id}")])

    buttons.append([InlineKeyboardButton(text="➕ Add Project", callback_data="menu:addproject")])
    buttons.append([InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")])

    await callback.message.edit_text(
        f"📂 <b>Projects</b> ({len(projects)})\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Tap a project to manage:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("project_detail:"))
async def project_detail(callback: CallbackQuery, db_user):
    """Show project detail view with action buttons."""
    project_id = int(callback.data.split(":")[1])
    is_admin = bool(db_user.is_admin) if db_user else False

    async with async_session() as session:
        from sqlalchemy import select
        from openclow.models import Project
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if project:
            session.expunge(project)

    if not project:
        await callback.message.edit_text("Project not found.")
        await callback.answer()
        return

    status = getattr(project, "status", "active")
    status_labels = {
        "active": "🟢 Ready",
        "bootstrapping": "🔄 Bootstrapping...",
        "failed": "🔴 Setup Failed",
        "inactive": "⚪ Unlinked",
    }
    status_label = status_labels.get(status, f"❓ {status}")
    text = (
        f"📦 <b>{project.name}</b> — {status_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 Repo: <code>{project.github_repo}</code>\n"
        f"🔧 Stack: {project.tech_stack or 'N/A'}\n"
        f"🐳 Docker: {'Yes' if project.is_dockerized else 'No'}\n"
    )
    if project.description:
        text += f"📝 {project.description[:100]}\n"

    # Get tunnel URL for Open App button
    tunnel_url = None
    try:
        from openclow.services.tunnel_service import get_tunnel_url
        tunnel_url = await get_tunnel_url(project.name)
    except Exception:
        pass

    if tunnel_url:
        text += f"\n🌐 <code>{tunnel_url}</code>"

    if status == "active":
        buttons = []
        if tunnel_url:
            buttons.append([InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])
        buttons.append([
            InlineKeyboardButton(text="🚀 New Task", callback_data=f"task_for:{project_id}"),
            InlineKeyboardButton(text="💚 Health", callback_data=f"health:{project_id}"),
        ])
        if is_admin:
            buttons.append([
                InlineKeyboardButton(text="🔄 Bootstrap", callback_data=f"project_bootstrap:{project_id}"),
                InlineKeyboardButton(text="📢 Slack Channel", callback_data=f"assign_channel:{project_id}"),
            ])
            if project.is_dockerized:
                buttons.append([
                    InlineKeyboardButton(text="▶️ Docker Up", callback_data=f"project_up:{project_id}"),
                    InlineKeyboardButton(text="⏹ Docker Down", callback_data=f"project_down:{project_id}"),
                ])
            buttons.append([
                InlineKeyboardButton(text="🔗 Unlink", callback_data=f"project_unlink:{project_id}"),
                InlineKeyboardButton(text="🗑 Remove", callback_data=f"project_remove:{project_id}"),
            ])
        buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data="menu:projects")])
    elif status == "bootstrapping":
        buttons = [
            [InlineKeyboardButton(text="🔄 Bootstrap is running...", callback_data="noop")],
            [InlineKeyboardButton(text="◀️ Back", callback_data="menu:projects")],
        ]
    elif status == "failed":
        buttons = []
        if is_admin:
            buttons.append([InlineKeyboardButton(text="🔄 Retry Bootstrap", callback_data=f"project_bootstrap:{project_id}")])
            buttons.append([
                InlineKeyboardButton(text="🔗 Unlink", callback_data=f"project_unlink:{project_id}"),
                InlineKeyboardButton(text="🗑 Remove", callback_data=f"project_remove:{project_id}"),
            ])
        buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data="menu:projects")])
    else:  # inactive
        buttons = []
        if is_admin:
            buttons.append([InlineKeyboardButton(text="🔗 Re-link (Bootstrap)", callback_data=f"project_relink:{project_id}")])
            buttons.append([InlineKeyboardButton(text="🗑 Remove Permanently", callback_data=f"project_remove:{project_id}")])
        buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data="menu:projects")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    """No-op button — just acknowledge the tap."""
    await callback.answer()


@router.callback_query(F.data == "menu:status")
async def menu_status(callback: CallbackQuery):
    active_statuses = ["pending", "preparing", "planning", "coding", "reviewing",
                       "diff_preview", "awaiting_approval", "pushing"]
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.chat_id == str(callback.message.chat.id))
            .where(Task.status.in_(active_statuses))
            .order_by(Task.created_at.desc())
            .limit(5)
        )
        tasks = list(result.scalars().all())

    if not tasks:
        await callback.message.edit_text(
            "📊 <b>No active tasks</b>\n\n"
            "Use 🚀 New Task to submit one.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 New Task", callback_data="menu:task")],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    status_icons = {
        "pending": "⏳", "preparing": "🔧", "planning": "🧠",
        "coding": "💻", "reviewing": "🔍", "diff_preview": "📄",
        "awaiting_approval": "✋", "pushing": "📤",
    }

    lines = []
    for t in tasks:
        icon = status_icons.get(t.status, "❓")
        lines.append(f"{icon} <b>{t.status}</b> — {t.description[:50]}")
        if t.pr_url:
            lines.append(f"   🔗 {t.pr_url}")

    await callback.message.edit_text(
        f"📊 <b>Active Tasks</b> ({len(tasks)})\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n" +
        "\n".join(lines),
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:logs")
async def menu_logs(callback: CallbackQuery):
    """AI-powered logs: fetch from Docker, summarize with Groq Llama."""
    await callback.message.edit_text("📋 Analyzing system logs...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "smart_logs",
            str(callback.message.chat.id),
            str(callback.message.message_id),
        )
    except Exception as e:
        log.error("menu.logs_failed", error=str(e))
        await callback.message.edit_text(
            "📋 Failed to fetch logs — worker unavailable.",
            reply_markup=back_to_menu_keyboard(),
        )
    await callback.answer()


# ──────────────────────────────────────────────
# Dashboard — reads tunnel URL from DB (instant)
# ──────────────────────────────────────────────

@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    await _show_dashboard(message, edit=False)


@router.callback_query(F.data == "menu:dashboard")
async def menu_dashboard(callback: CallbackQuery):
    await callback.answer()
    await _show_dashboard(callback.message, edit=True)


async def _show_dashboard(message: Message, edit: bool = False):
    """Read dashboard tunnel URL from DB (instant, no subprocess)."""
    from openclow.services.tunnel_service import get_tunnel_url

    url = await get_tunnel_url("dozzle")

    if url:
        await _send_dashboard_message(message, url, edit)
    else:
        text = (
            "📈 <b>Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tunnel is starting up. Try again in a moment.\n\n"
            "<i>The worker auto-starts the tunnel on boot.</i>"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:dashboard")],
            [InlineKeyboardButton(text="🔧 Force Start", callback_data="menu:dashboard_refresh")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
        if edit:
            await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


async def _send_dashboard_message(message: Message, url: str, edit: bool = False):
    text = (
        "📈 <b>Live Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Real-time container logs via Dozzle.\n\n"
        f"🔗 <code>{url}</code>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Open Dashboard", url=url)],
        [
            InlineKeyboardButton(text="🔄 New Link", callback_data="menu:dashboard_refresh"),
            InlineKeyboardButton(text="🛑 Stop Tunnel", callback_data="menu:dashboard_stop"),
        ],
        [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
    ])

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "menu:dashboard_refresh")
async def dashboard_refresh(callback: CallbackQuery):
    """Enqueue tunnel refresh on worker, then poll for result."""
    from openclow.worker.arq_app import get_arq_pool

    await callback.answer("Refreshing tunnel...")
    await callback.message.edit_text(
        "🔄 <b>Refreshing tunnel...</b>\n\nThis takes ~10 seconds.",
        parse_mode="HTML",
    )

    try:
        pool = await get_arq_pool()
        job = await pool.enqueue_job("refresh_dashboard_tunnel", "dozzle")
        result = await job.result(timeout=25)

        if result and result.get("ok"):
            await _send_dashboard_message(callback.message, result["url"], edit=True)
        else:
            error = result.get("error", "Unknown error") if result else "Timeout"
            await callback.message.edit_text(
                f"❌ Refresh failed: {error}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:dashboard_refresh")],
                    [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
                parse_mode="HTML",
            )
    except Exception as e:
        log.error("dashboard.refresh_failed", error=str(e))
        await callback.message.edit_text(
            f"❌ Refresh error: {str(e)[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:dashboard_refresh")],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ]),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "menu:dashboard_stop")
async def dashboard_stop(callback: CallbackQuery):
    """Enqueue tunnel stop on worker."""
    from openclow.worker.arq_app import get_arq_pool

    await callback.answer("Stopping tunnel...")
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("stop_dashboard_tunnel", "dozzle")
    except Exception as e:
        log.error("dashboard.stop_failed", error=str(e))

    await callback.message.edit_text(
        "🛑 <b>Dashboard tunnel stopped</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📈 Restart Dashboard", callback_data="menu:dashboard_refresh")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ]),
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Settings — opens web dashboard via tunnel
# ──────────────────────────────────────────────

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    await _show_settings(message, edit=False)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(callback: CallbackQuery):
    await callback.answer()
    await _show_settings(callback.message, edit=True)


async def _show_settings(message: Message, edit: bool = False):
    """Show settings dashboard link (tunnel URL from DB)."""
    from openclow.services.tunnel_service import get_tunnel_url

    url = await get_tunnel_url("settings")

    if url:
        settings_url = f"{url}/settings"
        wizard_url = f"{url}/settings/wizard"
        text = (
            "⚙️ <b>Settings Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Configure providers, manage projects,\n"
            "and test connections from the web UI.\n\n"
            f"🔗 <code>{settings_url}</code>"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Open Settings", url=settings_url)],
            [InlineKeyboardButton(text="🧙 Setup Wizard", url=wizard_url)],
            [
                InlineKeyboardButton(text="🔄 New Link", callback_data="menu:settings_refresh"),
                InlineKeyboardButton(text="🛑 Stop Tunnel", callback_data="menu:settings_stop"),
            ],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
    else:
        text = (
            "⚙️ <b>Settings Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tunnel is starting up. Try again in a moment.\n\n"
            "<i>The worker auto-starts the tunnel on boot.</i>"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:settings")],
            [InlineKeyboardButton(text="🔧 Force Start", callback_data="menu:settings_refresh")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])

    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "menu:settings_refresh")
async def settings_refresh(callback: CallbackQuery):
    """Refresh settings tunnel."""
    from openclow.worker.arq_app import get_arq_pool

    await callback.answer("Refreshing settings tunnel...")
    await callback.message.edit_text(
        "🔄 <b>Refreshing settings tunnel...</b>\n\nThis takes ~10 seconds.",
        parse_mode="HTML",
    )

    try:
        pool = await get_arq_pool()
        job = await pool.enqueue_job("refresh_dashboard_tunnel", "settings")
        result = await job.result(timeout=25)

        if result and result.get("ok"):
            url = result["url"]
            settings_url = f"{url}/settings"
            text = (
                "⚙️ <b>Settings Dashboard</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔗 <code>{settings_url}</code>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Open Settings", url=settings_url)],
                [
                    InlineKeyboardButton(text="🔄 New Link", callback_data="menu:settings_refresh"),
                    InlineKeyboardButton(text="🛑 Stop Tunnel", callback_data="menu:settings_stop"),
                ],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            error = result.get("error", "Unknown error") if result else "Timeout"
            await callback.message.edit_text(
                f"❌ Refresh failed: {error}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:settings_refresh")],
                    [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
                ]),
                parse_mode="HTML",
            )
    except Exception as e:
        log.error("settings.refresh_failed", error=str(e))
        await callback.message.edit_text(
            f"❌ Refresh error: {str(e)[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Retry", callback_data="menu:settings_refresh")],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ]),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "menu:settings_stop")
async def settings_stop(callback: CallbackQuery):
    """Stop settings tunnel."""
    from openclow.worker.arq_app import get_arq_pool

    await callback.answer("Stopping settings tunnel...")
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("stop_dashboard_tunnel", "settings")
    except Exception as e:
        log.error("settings.stop_failed", error=str(e))

    await callback.message.edit_text(
        "🛑 <b>Settings tunnel stopped</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Restart Settings", callback_data="menu:settings_refresh")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ]),
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Legacy text commands (still work directly)
# ──────────────────────────────────────────────

@router.message(Command("projects"))
async def cmd_projects(message: Message):
    projects = await project_service.get_all_projects(include_inactive=True)
    if not projects:
        await message.answer(
            "No projects configured.\n\nUse /addproject to connect a GitHub repo."
        )
        return

    status_icons = {"active": "🟢", "failed": "🔴", "bootstrapping": "🔄", "inactive": "⚪"}
    buttons = []
    for p in projects:
        icon = status_icons.get(p.status, "❓")
        label = f"{icon} {p.name} — {p.tech_stack or 'N/A'}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"project_detail:{p.id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")])

    await message.answer(
        f"📂 <b>Projects</b> ({len(projects)})\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Tap a project to manage:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    active_statuses = ["pending", "preparing", "planning", "coding", "reviewing",
                       "diff_preview", "awaiting_approval", "pushing"]
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.chat_id == str(message.chat.id))
            .where(Task.status.in_(active_statuses))
            .order_by(Task.created_at.desc())
            .limit(5)
        )
        tasks = list(result.scalars().all())

    if not tasks:
        await message.answer("No active tasks.")
        return

    status_icons = {
        "pending": "⏳", "preparing": "🔧", "planning": "🧠",
        "coding": "💻", "reviewing": "🔍", "diff_preview": "📄",
        "awaiting_approval": "✋", "pushing": "📤",
    }

    lines = []
    for t in tasks:
        icon = status_icons.get(t.status, "❓")
        lines.append(f"{icon} <b>{t.status}</b> — {t.description[:50]}")
        if t.pr_url:
            lines.append(f"   🔗 {t.pr_url}")

    await message.answer(
        f"📊 <b>Active Tasks</b> ({len(tasks)})\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n" +
        "\n".join(lines),
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("logs"))
async def cmd_logs(message: Message):
    """AI-powered logs: fetch from Docker, summarize with Groq Llama."""
    status_msg = await message.answer("📋 Analyzing system logs...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "smart_logs",
            str(message.chat.id),
            str(status_msg.message_id),
        )
    except Exception as e:
        log.error("cmd_logs.failed", error=str(e))
        await status_msg.edit_text("Failed to fetch logs — worker unavailable.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    """Cancel a running task."""
    cancellable = ["pending", "preparing", "coding", "reviewing"]
    async with async_session() as session:
        result = await session.execute(
            select(Task)
            .where(Task.chat_id == str(message.chat.id))
            .where(Task.status.in_(cancellable))
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()

    if not task:
        await message.answer("No cancellable tasks found.")
        return

    # Abort the arq job
    if task.arq_job_id:
        try:
            from openclow.worker.arq_app import get_arq_pool
            pool = await get_arq_pool()
            await pool.abort_job(task.arq_job_id)
        except Exception as e:
            log.warning("cancel.abort_failed", error=str(e))

    from sqlalchemy import update
    async with async_session() as session:
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(status="failed", error_message="Cancelled by user")
        )
        await session.commit()

    await message.answer(
        f"Task cancelled: {task.description[:60]}",
        reply_markup=back_to_menu_keyboard(),
    )
