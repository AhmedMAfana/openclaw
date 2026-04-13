"""Telegram chat provider — uses aiogram v3."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openclow.providers.base import ChatProvider
from openclow.providers.registry import register_chat
from openclow.utils.logging import get_logger

if TYPE_CHECKING:
    from openclow.providers.actions import ActionKeyboard

log = get_logger()


@register_chat("telegram")
class TelegramProvider(ChatProvider):
    def __init__(self, config: dict):
        self.token = config["token"]
        from openclow.settings import settings
        self.redis_url = config.get("redis_url", settings.redis_url)
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

    # ── Platform-agnostic action keyboard methods ──────────────

    @staticmethod
    def _translate_keyboard(keyboard: ActionKeyboard | None):
        """Convert ActionKeyboard → aiogram InlineKeyboardMarkup."""
        if keyboard is None:
            return None
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = []
        for row in keyboard.rows:
            buttons = []
            for btn in row.buttons:
                if btn.url:
                    buttons.append(InlineKeyboardButton(text=btn.label, url=btn.url))
                else:
                    buttons.append(InlineKeyboardButton(
                        text=btn.label, callback_data=btn.action_id,
                    ))
            rows.append(buttons)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def send_message_with_actions(
        self,
        chat_id: str,
        text: str,
        keyboard: ActionKeyboard | None = None,
        parse_mode: str | None = None,
    ) -> str:
        bot = self._get_bot()
        kwargs: dict[str, Any] = dict(chat_id=int(chat_id), text=text[:4000])
        markup = self._translate_keyboard(keyboard)
        if markup:
            kwargs["reply_markup"] = markup
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        msg = await bot.send_message(**kwargs)
        return str(msg.message_id)

    async def edit_message_with_actions(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        keyboard: ActionKeyboard | None = None,
        parse_mode: str | None = None,
    ) -> None:
        bot = self._get_bot()
        kwargs: dict[str, Any] = dict(
            text=text[:4000],
            chat_id=int(chat_id),
            message_id=int(message_id),
        )
        markup = self._translate_keyboard(keyboard)
        if markup:
            kwargs["reply_markup"] = markup
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        try:
            await bot.edit_message_text(**kwargs)
        except Exception as e:
            if "message is not modified" not in str(e):
                log.warning("telegram.edit_with_actions_failed", error=str(e))

    async def _get_tunnel_url_for_task(self, task_id: str) -> str | None:
        """Look up the tunnel URL for a task's project."""
        try:
            from openclow.services.tunnel_service import get_tunnel_url
            from openclow.models import Task, async_session
            from sqlalchemy import select

            async with async_session() as session:
                result = await session.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    await session.refresh(task, ["project"])
                    if task.project:
                        return await get_tunnel_url(task.project.name)
        except Exception:
            pass
        return None

    async def send_plan_preview(
        self, chat_id: str, message_id: str, task_id: str, plan: str
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from openclow.utils.messaging import plan_preview_message

        bot = self._get_bot()
        tunnel_url = await self._get_tunnel_url_for_task(task_id)

        # Use professional messaging template
        text = plan_preview_message(plan, estimated_minutes=5)

        buttons = []
        if tunnel_url:
            buttons.append([InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])
        buttons.append([
            InlineKeyboardButton(text="✅ Approve & Start", callback_data=f"approve_plan:{task_id}"),
            InlineKeyboardButton(text="❌ Request Changes", callback_data=f"discard:{task_id}"),
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await bot.edit_message_text(
                text=text, chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard, parse_mode="HTML",
            )
        except Exception as e:
            log.error("telegram.plan_preview_failed", error=str(e))
            try:
                await bot.send_message(
                    chat_id=int(chat_id), text=text[:4000], reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception:
                pass

    async def send_progress(
        self, chat_id: str, message_id: str, step: str, total_steps: int, current_step: int
    ) -> None:
        from openclow.utils.messaging import progress_message
        
        # Calculate elapsed time from message tracking if available
        elapsed = 0  # Could be enhanced to track actual elapsed time
        text = progress_message(step, current_step, total_steps, elapsed)
        
        await self.edit_message(chat_id, message_id, text)

    async def send_summary(
        self, chat_id: str, message_id: str, task_id: str, summary: str, diff_summary: str
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from openclow.utils.messaging import task_complete_message

        bot = self._get_bot()
        tunnel_url = await self._get_tunnel_url_for_task(task_id)

        buttons = []
        if tunnel_url:
            buttons.append([InlineKeyboardButton(text="🌐 Review Live", url=tunnel_url)])
        buttons.append([
            InlineKeyboardButton(text="✅ Create PR", callback_data=f"approve:{task_id}"),
            InlineKeyboardButton(text="🗑️ Discard", callback_data=f"discard:{task_id}"),
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Parse diff_summary to extract stats
        files_modified = diff_summary.count(" | ") if diff_summary else 0
        lines_added = diff_summary.count("+") if diff_summary else 0
        lines_removed = diff_summary.count("-") if diff_summary else 0
        
        text = task_complete_message(
            summary=summary,
            files_modified=max(1, files_modified),
            lines_added=lines_added,
            lines_removed=lines_removed,
            duration_seconds=0,  # Could be passed from orchestrator
            tunnel_url=tunnel_url,
        )
        
        try:
            await bot.edit_message_text(
                text=text, chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard, parse_mode="HTML",
            )
        except Exception as e:
            log.error("telegram.summary_failed", error=str(e))
            # Fallback without HTML
            await bot.edit_message_text(
                text=text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", ""),
                chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard,
            )

    async def send_diff_preview(
        self, chat_id: str, message_id: str, task_id: str, diff_summary: str
    ) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        bot = self._get_bot()
        tunnel_url = await self._get_tunnel_url_for_task(task_id)

        buttons = []
        if tunnel_url:
            buttons.append([InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])
        buttons.append([
            InlineKeyboardButton(text="Create PR", callback_data=f"approve:{task_id}"),
            InlineKeyboardButton(text="Discard", callback_data=f"discard:{task_id}"),
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

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
        from openclow.utils.messaging import pr_created_message

        bot = self._get_bot()
        
        # Extract PR number from URL
        pr_number = 0
        try:
            pr_number = int(pr_url.split("/")[-1])
        except (ValueError, IndexError):
            pass
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Merge PR", callback_data=f"merge:{task_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{task_id}"),
            ],
            [InlineKeyboardButton(text="🔗 View on GitHub", url=pr_url)],
        ])

        text = pr_created_message(pr_url, pr_number or 1)

        try:
            await bot.edit_message_text(
                text=text,
                chat_id=int(chat_id), message_id=int(message_id),
                reply_markup=keyboard, parse_mode="HTML",
            )
        except Exception as e:
            log.error("telegram.pr_created_failed", error=str(e))
            try:
                await bot.send_message(
                    chat_id=int(chat_id),
                    text=text,
                    reply_markup=keyboard, parse_mode="HTML",
                )
            except Exception:
                pass

    async def send_error(self, chat_id: str, message_id: str | None, text: str) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from openclow.utils.messaging import ErrorMessages
        
        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Main Menu", callback_data="menu:main")],
            [InlineKeyboardButton(text="💬 Chat with Agent", callback_data="menu:help")],
        ])
        
        # Use improved error messages for common errors
        error_lower = text.lower()
        if "worker" in error_lower and ("unavailable" in error_lower or "failed" in error_lower):
            message = ErrorMessages.WORKER_UNAVAILABLE
        elif "no changes" in error_lower or "agent made no" in error_lower:
            message = ErrorMessages.AGENT_NO_CHANGES
        elif "timeout" in error_lower:
            message = ErrorMessages.TIMEOUT
        elif "not found" in error_lower and "project" in error_lower:
            message = ErrorMessages.PROJECT_NOT_FOUND
        elif "busy" in error_lower or "lock" in error_lower:
            message = ErrorMessages.PROJECT_BUSY.format(holder_info=text)
        else:
            # Generic error with original message
            message = ErrorMessages.GENERIC_ERROR.format(ref="ERR_001")
            message = message.replace("We apologize for the inconvenience.", f"\n<b>Details:</b> {text[:200]}")
        
        if message_id:
            try:
                await bot.edit_message_text(
                    text=message,
                    chat_id=int(chat_id), message_id=int(message_id),
                    reply_markup=keyboard, parse_mode="HTML",
                )
                return
            except Exception:
                pass
        await bot.send_message(chat_id=int(chat_id), text=message, reply_markup=keyboard, parse_mode="HTML")

    async def send_terminal_message(self, chat_id: str, message_id: str | None, text: str) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        bot = self._get_bot()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 New Task", callback_data="menu:task"),
                InlineKeyboardButton(text="📂 Projects", callback_data="menu:projects"),
            ],
            [InlineKeyboardButton(text="📋 Main Menu", callback_data="menu:main")],
        ])
        
        # Add context to terminal messages
        if "cancel" in text.lower():
            formatted_text = f"✅ Task Cancelled\n\n{text}"
        else:
            formatted_text = text
            
        if message_id:
            try:
                await bot.edit_message_text(
                    text=formatted_text, chat_id=int(chat_id), message_id=int(message_id),
                    reply_markup=keyboard, parse_mode="HTML",
                )
                return
            except Exception:
                pass
        await bot.send_message(chat_id=int(chat_id), text=formatted_text, reply_markup=keyboard, parse_mode="HTML")

    async def start_bot(self) -> None:
        """Start the Telegram bot with polling."""
        from aiogram import Dispatcher
        from aiogram.fsm.storage.redis import RedisStorage
        from openclow.providers.chat.telegram.handlers import start, task, review, admin, chat
        from openclow.providers.chat.telegram.middlewares.auth import AuthMiddleware

        bot = self._get_bot()
        # Use DB 2 for FSM storage — replace the DB number in the URL
        import re
        fsm_url = re.sub(r'/\d+$', '/2', self.redis_url)
        storage = RedisStorage.from_url(fsm_url)
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
