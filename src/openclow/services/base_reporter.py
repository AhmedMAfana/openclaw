"""Base class for Telegram message reporters with heartbeat and rate-limited editing."""
import asyncio
import time

from openclow.utils.logging import get_logger

log = get_logger()


async def edit_message(chat, chat_id: str, message_id: str, text: str, buttons=None):
    """One-shot Telegram message edit. For progress reporting, use a Reporter."""
    try:
        from aiogram.types import InlineKeyboardMarkup
        bot = chat._get_bot()
        kwargs = dict(
            text=text[:4000],
            chat_id=int(chat_id),
            message_id=int(message_id),
        )
        if buttons:
            kwargs["reply_markup"] = InlineKeyboardMarkup(inline_keyboard=buttons)
        await bot.edit_message_text(**kwargs)
    except Exception as e:
        if "message is not modified" not in str(e):
            log.warning("reporter.edit_failed", error=str(e))


class BaseReporter:
    """Shared foundation for Telegram message reporters.

    Provides: heartbeat loop, rate-limited editing, elapsed timer.
    Subclasses implement ``_build_text()`` to control message content.
    """

    def __init__(
        self,
        chat,
        chat_id: str,
        message_id: str,
        *,
        heartbeat_interval: float = 5.0,
        rate_limit: float = 1.5,
    ):
        self._chat = chat
        self._chat_id = chat_id
        self._message_id = message_id
        self._start_time = time.time()
        self._rate_limit = rate_limit
        self._heartbeat_interval = heartbeat_interval
        self._last_send = 0.0
        self._heartbeat_task: asyncio.Task | None = None
        self._stopped = False

    # -- Lifecycle -------------------------------------------------------------

    async def start(self):
        """Start the background heartbeat that keeps the message alive."""
        if self._heartbeat_interval > 0:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self):
        """Stop the heartbeat."""
        self._stopped = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

    # -- Abstract --------------------------------------------------------------

    def _build_text(self) -> str:
        raise NotImplementedError

    # -- Shared edit machinery -------------------------------------------------

    @property
    def elapsed(self) -> int:
        return int(time.time() - self._start_time)

    async def _render(self, buttons=None):
        now = time.time()
        if now - self._last_send < self._rate_limit:
            return
        await self._force_render(buttons)

    async def _force_render(self, buttons=None):
        self._last_send = time.time()
        text = self._build_text()[:4000]
        try:
            from aiogram.types import InlineKeyboardMarkup
            bot = self._chat._get_bot()
            kwargs = dict(
                text=text,
                chat_id=int(self._chat_id),
                message_id=int(self._message_id),
            )
            if buttons:
                kwargs["reply_markup"] = InlineKeyboardMarkup(inline_keyboard=buttons)
            await bot.edit_message_text(**kwargs)
        except Exception as e:
            if "message is not modified" not in str(e):
                log.warning("reporter.render_failed", error=str(e))

    async def _heartbeat_loop(self):
        try:
            while not self._stopped:
                await asyncio.sleep(self._heartbeat_interval)
                await self._force_render()
        except asyncio.CancelledError:
            pass
