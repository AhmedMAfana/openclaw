"""Web chat provider — publishes to Redis for browser consumption via WebSocket."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from openclow.providers.base import ChatProvider
from openclow.providers.registry import register_chat
from openclow.utils.logging import get_logger

if TYPE_CHECKING:
    from openclow.providers.actions import ActionKeyboard

log = get_logger()


@register_chat("web")
class WebChatProvider(ChatProvider):
    """Web chat provider for browser-based UI.

    Instead of making platform API calls (like Telegram/Slack), this provider
    publishes JSON updates to Redis pub/sub. The FastAPI WebSocket handler
    subscribes to these channels and forwards to connected browsers.
    """

    def __init__(self, config: dict):
        from openclow.settings import settings
        self.redis_url = config.get("redis_url", settings.redis_url)

    def _parse_chat_id(self, chat_id: str) -> tuple[int, int]:
        """Extract user_id and session_id from web chat_id.

        Format: "web:{user_id}:{session_id}"
        """
        try:
            parts = chat_id.split(":")
            if len(parts) == 3 and parts[0] == "web":
                return int(parts[1]), int(parts[2])
        except (ValueError, IndexError):
            pass
        # Fallback for malformed chat_id
        log.warning("web_provider.invalid_chat_id", chat_id=chat_id)
        return 0, 0

    async def _publish(self, channel: str, data: dict) -> bool:
        """Publish a message to Redis pub/sub."""
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self.redis_url)
            await r.publish(channel, json.dumps(data))
            await r.aclose()
            return True
        except Exception as e:
            log.warning("web_provider.publish_failed", channel=channel, error=str(e))
            return False

    async def send_message(self, chat_id: str, text: str) -> str:
        """Send a new message (not editing an existing one)."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "msg_new",
            "text": text,
        })
        return "web_msg"

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        is_final: bool = False,
    ) -> None:
        """Update or finalize a message.

        is_final=False: streaming update (text is partial)
        is_final=True: message is complete, mark in DB
        """
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"

        data = {
            "type": "msg_final" if is_final else "msg_update",
            "message_id": message_id,
            "text": text,
        }

        if is_final:
            # Mark message complete in DB
            try:
                from openclow.models.base import async_session
                from openclow.models.web_chat import WebChatMessage
                async with async_session() as session:
                    msg = await session.get(WebChatMessage, int(message_id))
                    if msg:
                        msg.content = text
                        msg.is_complete = True
                        await session.commit()
            except Exception as e:
                log.warning("web_provider.mark_complete_failed", message_id=message_id, error=str(e))

        await self._publish(channel, data)

    async def edit_message_with_actions(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        keyboard: ActionKeyboard | None = None,
        is_final: bool = False,
    ) -> None:
        """Update message with action buttons."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"

        buttons = []
        if keyboard:
            for row in keyboard.rows:
                row_buttons = []
                for btn in row.buttons:
                    row_buttons.append({
                        "label": btn.label,
                        "action_id": btn.action_id,
                        "url": btn.url,
                        "style": btn.style,  # "default", "primary", "danger"
                    })
                buttons.append(row_buttons)

        data = {
            "type": "msg_final" if is_final else "msg_update",
            "message_id": message_id,
            "text": text,
            "buttons": buttons if buttons else None,
        }

        if is_final:
            # Mark message complete in DB
            try:
                from openclow.models.base import async_session
                from openclow.models.web_chat import WebChatMessage
                async with async_session() as session:
                    msg = await session.get(WebChatMessage, int(message_id))
                    if msg:
                        msg.content = text
                        msg.is_complete = True
                        await session.commit()
            except Exception as e:
                log.warning("web_provider.mark_complete_failed", message_id=message_id, error=str(e))

        await self._publish(channel, data)

    async def send_tool_event(
        self,
        chat_id: str,
        message_id: str,
        tool_name: str,
        tool_input: str,
        status: str = "running",
    ) -> None:
        """Publish a tool use event (Read, Edit, docker_exec, etc).

        Status: running | done
        """
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"

        await self._publish(channel, {
            "type": "tool_use",
            "message_id": message_id,
            "tool": tool_name,
            "input": tool_input[:100],  # truncate long inputs
            "status": status,
        })

    # ── No-ops for web (request-driven, not polling) ──

    async def start_bot(self) -> None:
        """No-op for web. Web chat is request-driven, not polling."""
        log.info("web_provider.start_bot_noop")

    async def close(self) -> None:
        """No-op for web."""
        pass
