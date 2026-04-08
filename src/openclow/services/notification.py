"""Debounced Telegram notification service.

Prevents Telegram rate limit errors (max 1 edit per 3 seconds per message).
"""
import asyncio
import time

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

from openclow.utils.logging import get_logger

log = get_logger()


class DebouncedNotifier:
    def __init__(self, bot: Bot, interval: float = 3.0):
        self.bot = bot
        self.interval = interval
        self._last_sent: dict[str, float] = {}
        self._pending: dict[str, str] = {}

    async def send(self, task, message: str):
        """Send or queue a status update (debounced)."""
        key = str(task.id)
        self._pending[key] = message
        now = time.time()
        if now - self._last_sent.get(key, 0) >= self.interval:
            await self._flush(task)

    async def _flush(self, task):
        """Actually send the pending message to Telegram."""
        key = str(task.id)
        message = self._pending.pop(key, None)
        if not message or not task.telegram_message_id:
            return

        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.bot.edit_message_text(
                    text=message,
                    chat_id=task.telegram_chat_id,
                    message_id=task.telegram_message_id,
                )
                self._last_sent[key] = time.time()
                return
            except TelegramRetryAfter as e:
                log.warning("telegram.rate_limited", retry_after=e.retry_after, attempt=attempt + 1)
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    log.error("telegram.edit_failed", error=str(e))
                return
        log.error("telegram.flush_failed_after_retries", task_id=key)

    async def flush_all(self, task):
        """Force flush any pending message."""
        await self._flush(task)

    async def send_diff_preview(self, task, diff_summary: str):
        """Send diff preview with Create PR / Discard buttons."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Create PR", callback_data=f"approve:{task.id}"),
                InlineKeyboardButton(text="Discard", callback_data=f"discard:{task.id}"),
            ],
        ])

        text = f"Changes ready!\n\n```\n{diff_summary[:3000]}\n```"

        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=task.telegram_chat_id,
                message_id=task.telegram_message_id,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("telegram.diff_preview_failed", error=str(e))

    async def send_pr_created(self, task, pr_url: str):
        """Send PR link with Merge / Reject buttons."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Merge", callback_data=f"merge:{task.id}"),
                InlineKeyboardButton(text="Reject", callback_data=f"reject:{task.id}"),
            ],
            [
                InlineKeyboardButton(text="View PR", url=pr_url),
            ],
        ])

        text = f"PR created!\n{pr_url}"

        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=task.telegram_chat_id,
                message_id=task.telegram_message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error("telegram.pr_created_failed", error=str(e))
