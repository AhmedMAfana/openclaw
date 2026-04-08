"""Telegram chat provider — uses aiogram v3."""
import asyncio
import time
from pathlib import Path
from typing import Any

from openclow.providers.base import ChatProvider
from openclow.providers.registry import register_chat
from openclow.utils.logging import get_logger

log = get_logger()


@register_chat("telegram")
class TelegramProvider(ChatProvider):
    def __init__(self, config: dict):
        self.token = config["token"]
        self.redis_url = config.get("redis_url", "redis://redis:6379/0")
        self._bot = None
        self._debounce_last: dict[str, float] = {}
        self._debounce_interval = 3.0

    def _get_bot(self):
        if self._bot is None:
            from aiogram import Bot
            self._bot = Bot(token=self.token)
        return self._bot

    async def send_message(self, chat_id: str, text: str) -> str:
        bot = self._get_bot()
        msg = await bot.send_message(chat_id=int(chat_id), text=text)
        return str(msg.message_id)

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        bot = self._get_bot()

        # Debounce: max 1 edit per 3 seconds per message
        key = f"{chat_id}:{message_id}"
        now = time.time()
        if now - self._debounce_last.get(key, 0) < self._debounce_interval:
            return  # skip, too frequent
        self._debounce_last[key] = now

        try:
            await bot.edit_message_text(
                text=text,
                chat_id=int(chat_id),
                message_id=int(message_id),
            )
        except Exception as e:
            if "message is not modified" not in str(e):
                log.warning("telegram.edit_failed", error=str(e))

    async def send_plan_preview(
        self, chat_id: str, message_id: str, task_id: str, plan: str
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Approve Plan", callback_data=f"approve_plan:{task_id}"),
                InlineKeyboardButton(text="Reject", callback_data=f"discard:{task_id}"),
            ],
        ])

        text = f"Here's my implementation plan:\n\n{plan[:3500]}\n\nApprove to start coding?"
        try:
            await bot.edit_message_text(
                text=text, chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error("telegram.plan_preview_failed", error=str(e))
            # Fallback: send new message so user can still act
            try:
                await bot.send_message(
                    chat_id=int(chat_id), text=text[:4000], reply_markup=keyboard,
                )
            except Exception:
                pass

    async def send_progress(
        self, chat_id: str, message_id: str, step: str, total_steps: int, current_step: int
    ) -> None:
        bar = "█" * current_step + "░" * (total_steps - current_step)
        text = f"Implementing [{current_step}/{total_steps}]\n{bar}\n\n{step}"
        await self.edit_message(chat_id, message_id, text)

    async def send_summary(
        self, chat_id: str, message_id: str, task_id: str, summary: str, diff_summary: str
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Create PR", callback_data=f"approve:{task_id}"),
                InlineKeyboardButton(text="Discard", callback_data=f"discard:{task_id}"),
            ],
        ])

        text = f"Done!\n\n{summary[:2000]}\n\nChanges:\n```\n{diff_summary[:1000]}\n```"
        try:
            await bot.edit_message_text(
                text=text, chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception as e:
            log.error("telegram.summary_failed", error=str(e))
            # Fallback without markdown
            await bot.edit_message_text(
                text=f"Done!\n\n{summary[:2000]}\n\nChanges:\n{diff_summary[:1000]}",
                chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard,
            )

    async def send_diff_preview(
        self, chat_id: str, message_id: str, task_id: str, diff_summary: str
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Create PR", callback_data=f"approve:{task_id}"),
                InlineKeyboardButton(text="Discard", callback_data=f"discard:{task_id}"),
            ],
        ])

        text = f"Changes ready!\n\n```\n{diff_summary[:3000]}\n```"
        try:
            await bot.edit_message_text(
                text=text, chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception as e:
            log.error("telegram.diff_preview_failed", error=str(e))
            try:
                await bot.send_message(
                    chat_id=int(chat_id),
                    text=f"Changes ready!\n\n{diff_summary[:3000]}",
                    reply_markup=keyboard,
                )
            except Exception:
                pass

    async def send_pr_created(
        self, chat_id: str, message_id: str, task_id: str, pr_url: str
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Merge", callback_data=f"merge:{task_id}"),
                InlineKeyboardButton(text="Reject", callback_data=f"reject:{task_id}"),
            ],
            [InlineKeyboardButton(text="View PR", url=pr_url)],
        ])

        try:
            await bot.edit_message_text(
                text=f"PR created!\n{pr_url}",
                chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard,
            )
        except Exception as e:
            log.error("telegram.pr_created_failed", error=str(e))
            try:
                await bot.send_message(
                    chat_id=int(chat_id),
                    text=f"PR created!\n{pr_url}",
                    reply_markup=keyboard,
                )
            except Exception:
                pass

    async def send_error(self, chat_id: str, message_id: str | None, text: str) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Main Menu", callback_data="menu:main")],
        ])
        if message_id:
            try:
                await bot.edit_message_text(
                    text=f"Error: {text[:500]}",
                    chat_id=int(chat_id), message_id=int(message_id),
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass
        await bot.send_message(chat_id=int(chat_id), text=f"Error: {text[:500]}", reply_markup=keyboard)

    async def send_terminal_message(self, chat_id: str, message_id: str | None, text: str) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="New Task", callback_data="menu:task"),
                InlineKeyboardButton(text="Projects", callback_data="menu:projects"),
            ],
            [InlineKeyboardButton(text="Main Menu", callback_data="menu:main")],
        ])
        if message_id:
            try:
                await bot.edit_message_text(
                    text=text, chat_id=int(chat_id), message_id=int(message_id),
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass
        await bot.send_message(chat_id=int(chat_id), text=text, reply_markup=keyboard)

    async def start_bot(self) -> None:
        """Start the Telegram bot with polling."""
        from aiogram import Dispatcher
        from aiogram.fsm.storage.redis import RedisStorage
        from openclow.bot.handlers import start, task, review, admin, chat
        from openclow.bot.middlewares.auth import AuthMiddleware

        bot = self._get_bot()
        storage = RedisStorage.from_url(self.redis_url + "/2")
        dp = Dispatcher(storage=storage)

        dp.message.middleware(AuthMiddleware())
        dp.callback_query.middleware(AuthMiddleware())

        dp.include_router(start.router)
        dp.include_router(task.router)
        dp.include_router(review.router)
        dp.include_router(admin.router)
        dp.include_router(chat.router)  # LAST — catch-all for text + voice

        # Heartbeat for Docker health check
        async def heartbeat():
            while True:
                Path("/tmp/bot_health").write_text(str(time.time()))
                await asyncio.sleep(10)
        asyncio.create_task(heartbeat())

        await bot.delete_webhook(drop_pending_updates=True)
        log.info("telegram.polling_started")
        await dp.start_polling(bot)

    async def close(self) -> None:
        if self._bot:
            await self._bot.session.close()
            self._bot = None
