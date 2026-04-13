"""Chat handler — catch-all for text and voice messages + agent session callbacks.

Registered LAST so commands (/task, /addproject, etc.) take priority.
Every message goes to the Claude Agent SDK worker task with full MCP tools.
"""
import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from openclow.utils.logging import get_logger

router = Router()
log = get_logger()


# ── Claude Auth callback ──

@router.callback_query(F.data == "claude_auth")
async def claude_auth_callback(callback: CallbackQuery):
    """Start Claude re-authentication flow."""
    await callback.message.edit_text("🔑 Starting authentication...")
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "claude_auth_task",
            str(callback.message.chat.id),
            str(callback.message.message_id),
        )
    except Exception as e:
        log.error("claude_auth.failed", error=str(e))
        await callback.message.edit_text(f"Auth failed: {str(e)[:200]}")
    await callback.answer()


# ── "Talk to Agent" callback — starts agent session with error context ──

@router.callback_query(F.data.startswith("agent_diagnose:"))
async def agent_diagnose_callback(callback: CallbackQuery):
    """Start an agent session scoped to a project. callback_data = agent_diagnose:<project_id>"""
    project_id = callback.data.split(":")[1]

    # Get project name for context
    project_name = "unknown"
    try:
        from openclow.services import project_service
        project = await project_service.get_project_by_id(int(project_id))
        if project:
            project_name = project.name
    except Exception:
        pass

    # Get the current message context (could be error or project detail)
    msg_context = callback.message.text or ""

    await callback.message.edit_text(f"🤖 Connecting to {project_name}...")

    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)

        prompt = (
            f"The user opened an agent session for project '{project_name}'.\n\n"
            f"Current context from the screen:\n{msg_context[:800]}\n\n"
            f"You have full access to this project. The user will type what they need. "
            f"Investigate, diagnose, fix, or do whatever they ask. "
            f"If you need credentials or user decisions, ASK clearly."
        )

        await pool.enqueue_job(
            "agent_session",
            prompt,
            str(callback.message.chat.id),
            str(callback.message.message_id),
            f"project_id:{project_id}",
            "telegram",
            str(callback.from_user.id),
        )
    except Exception as e:
        log.error("agent_diagnose.failed", error=str(e))
        await callback.message.edit_text(f"Agent unavailable: {str(e)[:200]}")
    await callback.answer()


async def _build_project_context(chat_id: int, chat_type: str, db_user) -> str:
    """Build project context string for agent session."""
    from openclow.services.channel_service import get_channel_project
    from openclow.services import bot_actions

    is_dm = chat_type == "private"
    if not is_dm:
        binding = await get_channel_project(str(chat_id), provider_type="telegram")
        if binding:
            return f"project_id:{binding['project_id']}"

    # DM or unlinked group
    if db_user and db_user.default_project_id:
        return f"project_id:{db_user.default_project_id}"

    binding = await get_channel_project(str(chat_id), provider_type="telegram")
    if binding:
        return f"project_id:{binding['project_id']}"

    projects = await bot_actions.get_all_projects()
    if len(projects) == 1:
        return f"project_id:{projects[0].id}"
    return ""


@router.message(lambda msg: msg.voice is not None)
async def handle_voice(message: Message, db_user):
    """Voice message → transcribe on worker → agent session."""
    log.info("chat.voice", user=db_user.chat_provider_uid,
             duration=message.voice.duration)

    thinking_msg = await message.answer("Listening...")

    try:
        await asyncio.wait_for(
            _process_voice(message, thinking_msg, db_user),
            timeout=90,
        )
    except asyncio.TimeoutError:
        log.error("chat.voice_timeout", user=db_user.chat_provider_uid)
        from openclow.providers.chat.telegram.handlers.start import main_menu_keyboard
        await thinking_msg.edit_text(
            "Voice processing timed out. Try typing instead.",
            reply_markup=main_menu_keyboard(is_admin=bool(db_user.is_admin)),
        )
    except Exception as e:
        log.error("chat.voice_failed", error=str(e))
        from openclow.providers.chat.telegram.handlers.start import main_menu_keyboard
        await thinking_msg.edit_text(
            "Voice processing failed. Try typing instead.",
            reply_markup=main_menu_keyboard(is_admin=bool(db_user.is_admin)),
        )


async def _process_voice(message: Message, thinking_msg: Message, db_user):
    """Inner voice processing — transcribe then route to agent session."""
    import base64

    file = await message.bot.get_file(message.voice.file_id)
    from io import BytesIO
    buf = BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    voice_b64 = base64.b64encode(buf.getvalue()).decode()

    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    job = await pool.enqueue_job("transcribe_voice", voice_b64)
    text = await job.result(timeout=60)

    from openclow.providers.chat.telegram.handlers.start import main_menu_keyboard

    if not text:
        await thinking_msg.edit_text(
            "Couldn't understand the voice message. Try typing instead.",
            reply_markup=main_menu_keyboard(is_admin=bool(db_user.is_admin)),
        )
        return

    await thinking_msg.edit_text(f"Heard: \"{text}\"\n\nWorking...")

    project_context = await _build_project_context(message.chat.id, message.chat.type, db_user)

    # Route transcribed text to agent session
    await pool.enqueue_job(
        "agent_session",
        text,
        str(message.chat.id),
        str(thinking_msg.message_id),
        project_context,
        "telegram",
        str(message.from_user.id),
    )


@router.message(lambda msg: msg.text and not msg.text.startswith("/"))
async def handle_text(message: Message, db_user):
    """Every text message → Claude Agent SDK with full MCP tools."""
    log.info("chat.text", user=db_user.chat_provider_uid,
             text=message.text[:50])

    thinking_msg = await message.answer("Working...")

    try:
        project_context = await _build_project_context(message.chat.id, message.chat.type, db_user)
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        await pool.enqueue_job(
            "agent_session",
            message.text,
            str(message.chat.id),
            str(thinking_msg.message_id),
            project_context,
            "telegram",
            str(message.from_user.id),
        )
    except Exception as e:
        log.error("chat.agent_session_failed", error=str(e))
        from openclow.providers.chat.telegram.handlers.start import main_menu_keyboard
        await thinking_msg.edit_text(
            "Agent unavailable. Try again later.",
            reply_markup=main_menu_keyboard(is_admin=bool(db_user.is_admin)),
        )
