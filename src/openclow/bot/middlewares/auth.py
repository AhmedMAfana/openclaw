"""User allowlist middleware — provider-agnostic user lookup."""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from openclow.models import User, async_session


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if not user:
            return

        # Lookup by provider-agnostic UID
        uid = str(user.id)
        async with async_session() as session:
            result = await session.execute(
                select(User).where(
                    User.chat_provider_type == "telegram",
                    User.chat_provider_uid == uid,
                )
            )
            db_user = result.scalar_one_or_none()

        if not db_user or not db_user.is_allowed:
            if isinstance(event, Message):
                await event.answer(
                    "You are not authorized.\n"
                    f"Your Telegram ID: {user.id}\n"
                    "Ask the admin to add you via the setup wizard."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("Not authorized", show_alert=True)
            return

        data["db_user"] = db_user
        return await handler(event, data)
