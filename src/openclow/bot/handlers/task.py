"""Task submission handler — FSM flow for /task command."""
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from openclow.bot.keyboards import confirm_keyboard, project_keyboard
from openclow.bot.states import TaskStates
from openclow.models import Project, Task, async_session
from openclow.services import project_service
from openclow.utils.logging import get_logger

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
    description = message.text.strip()
    if len(description) < 10:
        await message.answer("Please provide a more detailed description (at least 10 characters).")
        return

    data = await state.get_data()
    await state.update_data(description=description)

    await message.answer(
        f"Confirm task:\n\n"
        f"Project: {data['project_name']}\n"
        f"Task: {description}\n",
        reply_markup=confirm_keyboard(),
    )
    await state.set_state(TaskStates.confirming)


@router.callback_query(TaskStates.confirming, F.data == "submit")
async def task_submitted(callback: CallbackQuery, state: FSMContext, db_user):
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
        )
        session.add(task)
        await session.commit()

    # Send initial status message (we'll edit this message with updates)
    status_msg = await callback.message.edit_text("Task submitted! Preparing...")

    # Dispatch to arq worker
    from openclow.worker.arq_app import get_arq_pool
    pool = await get_arq_pool()
    job = await pool.enqueue_job("execute_task", str(task_id))

    # Save message ID and job ID in a single session
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one()
        task.chat_message_id = str(status_msg.message_id)
        task.arq_job_id = job.job_id
        await session.commit()

    log.info("task.dispatched", task_id=str(task_id), project=data["project_name"])
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def task_cancelled(callback: CallbackQuery, state: FSMContext):
    """User cancelled task submission."""
    await state.clear()
    from openclow.bot.handlers.start import back_to_menu_keyboard
    await callback.message.edit_text("Task cancelled.", reply_markup=back_to_menu_keyboard())
    await callback.answer()
