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
        """Send a new message (not editing an existing one).

        Creates a DB row so the message_id can be used for subsequent edits.
        Returns the numeric DB message id as string, or 'web_msg' on failure.
        """
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"

        # Create DB row so the worker can edit it later via edit_message(is_final=True)
        # Use a non-empty placeholder so the message survives the frontend filter
        # (.filter(m => m.role !== "assistant" || m.content.trim() !== ""))
        # on page refresh — the real content arrives via send_progress_card heartbeat.
        _stored_content = text if text.strip() else "__LOADING__"
        msg_id = "web_msg"
        if user_id and session_id:
            try:
                from openclow.models.base import async_session
                from openclow.models.web_chat import WebChatMessage
                async with async_session() as db:
                    msg = WebChatMessage(
                        session_id=session_id,
                        user_id=user_id,
                        role="assistant",
                        content=_stored_content,
                        is_complete=False,
                    )
                    db.add(msg)
                    await db.commit()
                    await db.refresh(msg)
                    msg_id = str(msg.id)
            except Exception as e:
                log.warning("web_provider.send_message_db_failed", error=str(e))

        await self._publish(channel, {
            "type": "msg_new",
            "message_id": msg_id,
            "text": text,
        })
        return msg_id

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

        if is_final and message_id and str(message_id).isdigit():
            # Mark message complete in DB (only when we have a real numeric DB id)
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
        """Web: buttons stripped — web is conversational, no button clicks needed."""
        await self.edit_message(chat_id, message_id, text, is_final=is_final)

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

    async def send_tool_output(
        self,
        chat_id: str,
        message_id: str,
        tool_name: str,
        chunk: str,
        final: bool = False,
    ) -> None:
        """Stream stdout/stderr from a long-running tool call (host_run_command,
        etc.) to the user's web chat panel. The frontend appends these under the
        preceding tool_use bubble so the user sees the real output live."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "tool_output",
            "message_id": message_id,
            "tool": tool_name,
            "chunk": (chunk or "")[:4096],
            "final": bool(final),
        })

    async def send_message_with_actions(
        self,
        chat_id: str,
        text: str,
        keyboard: "ActionKeyboard | None" = None,
        parse_mode: str | None = None,
    ) -> str:
        """Web: buttons stripped — web is conversational, no button clicks needed."""
        return await self.send_message(chat_id, text)

    async def send_agent_token(self, chat_id: str, message_id: str, text: str) -> None:
        """Stream a raw agent text block to the frontend (no DB persist — ephemeral).

        Called once per agent turn (TextBlock), not per character. The frontend
        accumulates tokens into a scrolling log inside the progress card.
        """
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "agent_token",
            "message_id": message_id,
            "text": text,
        })

    async def send_progress_card(
        self, chat_id: str, message_id: str, card: dict
    ) -> None:
        """Publish a structured progress card event (web-only).
        Callers check hasattr(chat, 'send_progress_card') before calling."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "progress_card",
            "message_id": message_id,
            "card": card,
        })
        # Persist to DB so the card survives page refresh.
        # Also embed session_id into the stored card so the Stop button works after refresh.
        if message_id and str(message_id).isdigit():
            try:
                from openclow.models.base import async_session
                from openclow.models.web_chat import WebChatMessage
                # Inject session_id so frontend can render the Stop button after page refresh
                card_to_store = dict(card)
                if "session_id" not in card_to_store and session_id:
                    card_to_store["session_id"] = str(session_id)
                content = f"__PROGRESS_CARD__{json.dumps(card_to_store)}"
                async with async_session() as db:
                    msg = await db.get(WebChatMessage, int(message_id))
                    if msg:
                        msg.content = content
                        # Always sync is_complete with overall_status so a heartbeat
                        # overwrite after a cancel resets is_complete to False, keeping
                        # the card cancellable again.
                        msg.is_complete = card.get("overall_status") in ("done", "failed")
                        await db.commit()
            except Exception as e:
                log.warning("web_provider.progress_card_persist_failed", message_id=message_id, error=str(e))

    async def send_plan_preview(
        self, chat_id: str, message_id: str, task_id: str, plan: str
    ) -> None:
        """Web: publish plan_preview event with task_id so UI can show Approve/Reject buttons."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "plan_preview",
            "message_id": message_id,
            "task_id": task_id,
            "text": plan,
        })

    async def send_progress(
        self, chat_id: str, message_id: str, step: str, total_steps: int, current_step: int
    ) -> None:
        """Send progress update."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "msg_update",
            "message_id": message_id,
            "text": f"[{current_step}/{total_steps}] {step}",
        })

    async def send_summary(
        self, chat_id: str, message_id: str, task_id: str, summary: str, diff_summary: str
    ) -> None:
        """Web: send completion summary as a NEW message — do NOT overwrite the progress card."""
        text = f"{summary}\n\n**Changes:**\n{diff_summary}"
        await self.send_message(chat_id, text)

    async def send_diff_preview(
        self, chat_id: str, message_id: str, task_id: str, diff_summary: str
    ) -> None:
        """Web: send diff as plain text — no approve/reject buttons needed."""
        await self.edit_message(chat_id, message_id, diff_summary, is_final=False)

    async def send_pr_created(
        self, chat_id: str, message_id: str, task_id: str, pr_url: str
    ) -> None:
        """Send PR created notification."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "msg_final",
            "message_id": message_id,
            "text": f"✅ PR created: {pr_url}",
            "pr_url": pr_url,
        })

    async def send_error(self, chat_id: str, message_id: str | None, text: str) -> None:
        """Send error message."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "msg_error",
            "message_id": message_id,
            "text": f"❌ {text}",
        })

    async def send_terminal_message(self, chat_id: str, message_id: str | None, text: str) -> None:
        """Send a terminal-state message."""
        user_id, session_id = self._parse_chat_id(chat_id)
        channel = f"wc:{user_id}:{session_id}"
        await self._publish(channel, {
            "type": "msg_final",
            "message_id": message_id,
            "text": text,
        })

    def _keyboard_to_buttons(self, keyboard: "ActionKeyboard | None") -> list | None:
        """Convert ActionKeyboard to JSON-serializable button rows."""
        if not keyboard:
            return None
        buttons = []
        for row in keyboard.rows:
            row_buttons = []
            for btn in row.buttons:
                row_buttons.append({
                    "label": btn.label,
                    "action_id": btn.action_id,
                    "url": btn.url,
                    "style": btn.style,
                })
            buttons.append(row_buttons)
        return buttons or None

    # ── No-ops for web (request-driven, not polling) ──

    async def start_bot(self) -> None:
        """No-op for web. Web chat is request-driven, not polling."""
        log.info("web_provider.start_bot_noop")

    async def close(self) -> None:
        """No-op for web."""
        pass
