"""Debounced notification service.

Platform-agnostic — uses ChatProvider instead of aiogram Bot directly.
Prevents rate limit errors (max 1 edit per interval per message).

NOTE: This service is currently unused. The StatusReporter/LineReporter
pattern in status_reporter.py is the preferred approach. Kept for potential
future use where the reporter pattern is too heavy.
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


class DebouncedNotifier:
    def __init__(self, chat: ChatProvider, interval: float = 3.0):
        self._chat = chat
        self.interval = interval
        self._last_sent: dict[str, float] = {}
        self._pending: dict[str, str] = {}

    async def send(self, chat_id: str, message_id: str, text: str):
        """Send or queue a status update (debounced)."""
        key = f"{chat_id}:{message_id}"
        self._pending[key] = text
        now = time.time()
        if now - self._last_sent.get(key, 0) >= self.interval:
            await self._flush(chat_id, message_id)

    async def _flush(self, chat_id: str, message_id: str):
        """Actually send the pending message."""
        key = f"{chat_id}:{message_id}"
        text = self._pending.pop(key, None)
        if not text:
            return

        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self._chat.edit_message(chat_id, message_id, text)
                self._last_sent[key] = time.time()
                return
            except Exception as e:
                if "message is not modified" not in str(e):
                    log.error("notifier.edit_failed", attempt=attempt, error=str(e))
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))

    async def send_with_actions(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        keyboard: ActionKeyboard | None = None,
    ):
        """Send a message with action buttons (not debounced — immediate)."""
        try:
            await self._chat.edit_message_with_actions(
                chat_id, message_id, text, keyboard,
            )
        except Exception as e:
            log.error("notifier.edit_with_actions_failed", error=str(e))
