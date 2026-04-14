"""Base class for message reporters with heartbeat and rate-limited editing.

Platform-agnostic — uses ChatProvider.edit_message_with_actions() instead
of calling aiogram bot methods directly.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from openclow.utils.logging import get_logger

if TYPE_CHECKING:
    from openclow.providers.actions import ActionKeyboard
    from openclow.providers.base import ChatProvider

log = get_logger()


async def edit_message(
    chat: ChatProvider,
    chat_id: str,
    message_id: str,
    text: str,
    keyboard: ActionKeyboard | None = None,
):
    """One-shot message edit. For progress reporting, use a Reporter."""
    try:
        await chat.edit_message_with_actions(
            chat_id, message_id, text[:4000], keyboard,
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            log.warning("reporter.edit_failed", error=str(e))


class BaseReporter:
    """Shared foundation for message reporters.

    Provides: heartbeat loop, rate-limited editing, elapsed timer.
    Subclasses implement ``_build_text()`` to control message content.
    """

    def __init__(
        self,
        chat: ChatProvider,
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
        self._last_keyboard: ActionKeyboard | None = None

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

    async def _render(self, keyboard: ActionKeyboard | None = None):
        if keyboard is not None:
            self._last_keyboard = keyboard
        now = time.time()
        if now - self._last_send < self._rate_limit:
            return
        await self._force_render(keyboard)

    async def _force_render(self, keyboard: ActionKeyboard | None = None):
        if keyboard is not None:
            self._last_keyboard = keyboard
        self._last_send = time.time()
        text = self._build_text()[:4000]
        kb = keyboard or self._last_keyboard
        # is_final=True when an explicit keyboard is passed (e.g. final buttons)
        # This bypasses Slack's 1s debounce so the final render always lands.
        is_final = keyboard is not None
        try:
            await self._chat.edit_message_with_actions(
                self._chat_id, self._message_id, text, kb,
                is_final=is_final,
            )
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
