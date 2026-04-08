"""Chat handler — catch-all for text and voice messages.

Registered LAST so commands (/task, /addproject, etc.) take priority.
Everything not caught by other handlers flows here → Claude AI responds.
Uses the same menu keyboard as /start for a consistent UI.
"""
import asyncio

from aiogram import Router
from aiogram.types import Message

from openclow.services.ai_chat import get_chat_response
from openclow.utils.logging import get_logger

router = Router()
log = get_logger()


@router.message(lambda msg: msg.voice is not None)
async def handle_voice(message: Message, db_user):
    """Voice message → transcribe on worker → AI response."""
    log.info("chat.voice", user=db_user.chat_provider_uid,
             duration=message.voice.duration)

    thinking_msg = await message.answer("Listening...")

    try:
        await asyncio.wait_for(
            _process_voice(message, thinking_msg),
            timeout=90,
        )
    except asyncio.TimeoutError:
        log.error("chat.voice_timeout", user=db_user.chat_provider_uid)
        from openclow.bot.handlers.start import main_menu_keyboard
        await thinking_msg.edit_text(
            "Voice processing timed out. Try typing instead.",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        log.error("chat.voice_failed", error=str(e))
        from openclow.bot.handlers.start import main_menu_keyboard
        await thinking_msg.edit_text(
            "Voice processing failed. Try typing instead.",
            reply_markup=main_menu_keyboard(),
        )


async def _process_voice(message: Message, thinking_msg: Message):
    """Inner voice processing — routes transcription to worker (has whisper model)."""
    import base64

    # Download voice file as bytes
    file = await message.bot.get_file(message.voice.file_id)
    from io import BytesIO
    buf = BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    voice_b64 = base64.b64encode(buf.getvalue()).decode()

    # Send to worker for transcription (worker has whisper model + 4GB RAM)
    from openclow.worker.arq_app import get_arq_pool
    pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
    job = await pool.enqueue_job("transcribe_voice", voice_b64)
    text = await job.result(timeout=60)

    from openclow.bot.handlers.start import main_menu_keyboard

    if not text:
        await thinking_msg.edit_text(
            "Couldn't understand the voice message. Try typing instead.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await thinking_msg.edit_text(f"Heard: \"{text}\"\n\nThinking...")

    # Get AI response (routed through worker which has Claude)
    response = await get_chat_response(
        user_message=text,
        chat_id=str(message.chat.id),
        message_id=str(thinking_msg.message_id),
    )

    clean = response.replace("**", "").replace("``", "").replace("`", "")
    await thinking_msg.edit_text(clean, reply_markup=main_menu_keyboard())


@router.message(lambda msg: msg.text and not msg.text.startswith("/"))
async def handle_text(message: Message, db_user):
    """Regular text message → AI response + same menu as /start."""
    log.info("chat.text", user=db_user.chat_provider_uid,
             text=message.text[:50])

    thinking_msg = await message.answer("Thinking...")

    try:
        response = await get_chat_response(
            user_message=message.text,
            chat_id=str(message.chat.id),
            message_id=str(thinking_msg.message_id),
        )

        # Clean the response — strip markdown that Telegram can't render
        clean = response.replace("**", "").replace("``", "").replace("`", "")

        # Use the same menu keyboard as /start — one consistent UI
        from openclow.bot.handlers.start import main_menu_keyboard
        await thinking_msg.edit_text(
            clean,
            reply_markup=main_menu_keyboard(),
        )

    except Exception as e:
        log.error("chat.text_failed", error=str(e))
        from openclow.bot.handlers.start import main_menu_keyboard
        await thinking_msg.edit_text(
            "Something went wrong. Try the buttons below:",
            reply_markup=main_menu_keyboard(),
        )
