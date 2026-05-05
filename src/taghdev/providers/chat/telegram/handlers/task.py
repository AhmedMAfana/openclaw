"""Task submission handler — FSM flow for /task command."""
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from taghdev.providers.chat.telegram.keyboards import confirm_keyboard, project_keyboard
from taghdev.providers.chat.telegram.states import TaskStates
from taghdev.models import Project, Task, async_session
from taghdev.services import project_service
from taghdev.utils.logging import get_logger

router = Router()
log = get_logger()


@router.message(Command("task"))
async def cmd_task(message: Message, state: FSMContext):
    """Start the task submission flow."""
    projects = await project_service.get_all_projects()
    if not projects:
        await message.answer("No projects configured. Run seed_projects.py first.")
        return

    await message.answer(
        "Select a project:",
        reply_markup=project_keyboard(projects),
    )
    await state.set_state(TaskStates.choosing_project)


@router.callback_query(TaskStates.choosing_project, F.data.startswith("project:"))
async def project_chosen(callback: CallbackQuery, state: FSMContext):
    """User selected a project."""
    project_id = int(callback.data.split(":")[1])
    project = await project_service.get_project_by_id(project_id)

    if not project:
        await callback.answer("Project not found", show_alert=True)
        return

    await state.update_data(project_id=project_id, project_name=project.name)
    await callback.message.edit_text(
        f"Project: {project.name}\n\nDescribe your task:"
    )
    await state.set_state(TaskStates.entering_description)
    await callback.answer()


@router.message(TaskStates.entering_description)
async def description_entered(message: Message, state: FSMContext):
    """User typed the task description."""
    if not message.text:
        await message.answer("Please send a text description.")
        return
    description = message.text.strip()
    if len(description) < 10:
        await message.answer("Please provide a more detailed description (at least 10 characters).")
        return

    data = await state.get_data()
    await state.update_data(description=description)

    # Get project details for richer confirmation
    project = await project_service.get_project_by_id(data["project_id"])

    # Get tunnel URL
    tunnel_url = None
    try:
        from taghdev.services.tunnel_service import get_tunnel_url
        tunnel_url = await get_tunnel_url(project.name) if project else None
    except Exception:
        pass

    confirm_text = (
        f"📦 <b>{project.name if project else data['project_name']}</b>\n"
        f"🔧 {project.tech_stack if project and project.tech_stack else 'N/A'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Task:</b> {description}\n"
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = []
    if tunnel_url:
        buttons.append([InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])
    buttons.append([
        InlineKeyboardButton(text="⚡ Quick", callback_data="submit_quick"),
        InlineKeyboardButton(text="📋 Full", callback_data="submit"),
    ])
    buttons.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel"),
    ])

    await message.answer(
        confirm_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await state.set_state(TaskStates.confirming)


@router.callback_query(TaskStates.confirming, F.data == "submit_quick")
async def task_submitted_quick(callback: CallbackQuery, state: FSMContext, db_user):
    """Quick task — skip planning, go straight to coding."""
    await _create_and_dispatch_task(callback, state, db_user, skip_planning=True)


@router.callback_query(TaskStates.confirming, F.data == "submit")
async def task_submitted(callback: CallbackQuery, state: FSMContext, db_user):
    """Full task — plan first, then code."""
    await _create_and_dispatch_task(callback, state, db_user, skip_planning=False)


async def _create_and_dispatch_task(callback: CallbackQuery, state: FSMContext, db_user, skip_planning: bool = False):
    """User confirmed — create task and dispatch to worker."""
    data = await state.get_data()
    if not data or not data.get("project_id"):
        # Already processed (double-click) — ignore
        await callback.answer("Already submitted!", show_alert=False)
        return
    await state.clear()

    # Create task in DB
    task_id = uuid.uuid4()
    async with async_session() as session:
        task = Task(
            id=task_id,
            user_id=db_user.id,
            project_id=data["project_id"],
            description=data["description"],
            status="pending",
            chat_id=str(callback.message.chat.id),
            chat_provider_type="telegram",
            git_mode="branch_per_task",
        )
        session.add(task)
        await session.commit()

    # Send initial status message (we'll edit this message with updates)
    from taghdev.utils.messaging import task_submitted_message
    status_msg = await callback.message.edit_text(
        task_submitted_message(data["description"]),
        parse_mode="HTML"
    )

    # Dispatch to arq worker
    try:
        from taghdev.worker.arq_app import get_arq_pool
        import asyncio as _asyncio
        pool = await _asyncio.wait_for(get_arq_pool(), timeout=5)
        job = await pool.enqueue_job("execute_task", str(task_id), skip_planning)

        # Save message ID and job ID
        async with async_session() as session:
            result = await session.execute(
                select(Task).where(Task.id == task_id)
            )
            task = result.scalar_one()
            task.chat_message_id = str(status_msg.message_id)
            task.arq_job_id = job.job_id
            await session.commit()

        log.info("task.dispatched", task_id=str(task_id), project=data.get("project_name", "unknown"))
    except Exception as e:
        log.error("task.dispatch_failed", task_id=str(task_id), error=str(e))
        from taghdev.providers.chat.telegram.handlers.start import back_to_menu_keyboard
        from taghdev.utils.messaging import ErrorMessages
        
        # Update task status to failed
        async with async_session() as session:
            result = await session.execute(
                select(Task).where(Task.id == task_id)
            )
            task = result.scalar_one()
            task.status = "failed"
            task.error_message = f"Failed to dispatch: {str(e)[:200]}"
            await session.commit()
        
        await callback.message.edit_text(
            ErrorMessages.WORKER_UNAVAILABLE,
            reply_markup=back_to_menu_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    
    await callback.answer()


@router.callback_query(TaskStates.choosing_project, F.data == "cancel")
@router.callback_query(TaskStates.confirming, F.data == "cancel")
async def task_cancelled(callback: CallbackQuery, state: FSMContext):
    """User cancelled task submission."""
    await state.clear()
    from taghdev.providers.chat.telegram.handlers.start import back_to_menu_keyboard
    await callback.message.edit_text("Task cancelled.", reply_markup=back_to_menu_keyboard())
    await callback.answer()


@router.message(TaskStates.choosing_project)
async def choosing_project_text(message: Message):
    """User sent text while choosing a project — remind to use buttons."""
    await message.answer("Please tap a project button above, or tap Cancel.")
