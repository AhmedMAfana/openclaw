"""Slack chat provider — uses slack-bolt async with Socket Mode, rich Block Kit UI."""
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


@register_chat("slack")
class SlackProvider(ChatProvider):
    def __init__(self, config: dict):
        # Validate required config keys
        required = ["bot_token", "app_token", "signing_secret"]
        missing = [k for k in required if not config.get(k)]
        if missing:
            raise ValueError(
                f"Slack provider missing required config: {', '.join(missing)}. "
                f"Configure via Settings Dashboard → Chat → Slack."
            )
        
        self.bot_token = config["bot_token"]
        self.app_token = config["app_token"]
        self.signing_secret = config["signing_secret"]
        self._app = None
        self._client = None
        self._socket_handler = None
        self._debounce_last: dict[str, float] = {}
        self._debounce_interval = 1.0
        
        log.info("slack.provider_initialized", 
                 bot_token_prefix=self.bot_token[:10] if self.bot_token else None,
                 app_token_prefix=self.app_token[:10] if self.app_token else None)

    def _get_client(self):
        if self._client is None:
            from slack_sdk.web.async_client import AsyncWebClient
            self._client = AsyncWebClient(token=self.bot_token)
        return self._client

    # ── Core message methods ──────────────────────────────────

    async def send_message(self, chat_id: str, text: str) -> str:
        client = self._get_client()
        result = await client.chat_postMessage(channel=chat_id, text=text[:4000])
        return result["ts"]

    async def edit_message(self, chat_id: str, message_id: str, text: str, is_final: bool = False) -> None:
        client = self._get_client()

        # Debounce — but always deliver terminal/final updates
        key = f"{chat_id}:{message_id}"
        now = time.time()
        if not is_final:
            if now - self._debounce_last.get(key, 0) < self._debounce_interval:
                return
        self._debounce_last[key] = now

        try:
            await client.chat_update(channel=chat_id, ts=message_id, text=text[:4000])
        except Exception as e:
            if "message_not_found" not in str(e):
                log.warning("slack.edit_failed", error=str(e))

    async def edit_message_blocks(
        self, chat_id: str, message_id: str, blocks: list[dict], is_final: bool = False
    ) -> None:
        """Update a message with pre-built Block Kit blocks (no text→block conversion)."""
        client = self._get_client()

        key = f"{chat_id}:{message_id}"
        now = time.time()
        if not is_final:
            if now - self._debounce_last.get(key, 0) < self._debounce_interval:
                return
        self._debounce_last[key] = now

        try:
            await client.chat_update(
                channel=chat_id, ts=message_id,
                text="OpenClow", blocks=blocks,
            )
        except Exception as e:
            if "message_not_found" not in str(e):
                log.warning("slack.edit_blocks_failed", error=str(e))

    # ── Action keyboard methods ───────────────────────────────

    @staticmethod
    def _build_blocks(text: str, keyboard: ActionKeyboard | None = None) -> list[dict]:
        from openclow.providers.chat.slack.blocks import build_message_blocks, status_update_blocks, translate_keyboard
        rich = status_update_blocks(text)
        if rich is not None:
            rich.extend(translate_keyboard(keyboard))
            return rich
        # Always return standard message blocks so layout is consistent
        return build_message_blocks(text, keyboard)

    async def send_message_with_actions(
        self,
        chat_id: str,
        text: str,
        keyboard: ActionKeyboard | None = None,
        parse_mode: str | None = None,
    ) -> str:
        client = self._get_client()
        blocks = self._build_blocks(text, keyboard)
        result = await client.chat_postMessage(
            channel=chat_id,
            text=text[:4000],
            blocks=blocks,
        )
        return result["ts"]

    async def edit_message_with_actions(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        keyboard: ActionKeyboard | None = None,
        parse_mode: str | None = None,
        is_final: bool = False,
    ) -> None:
        client = self._get_client()
        blks = self._build_blocks(text, keyboard)

        key = f"{chat_id}:{message_id}"
        now = time.time()
        if not is_final:
            if now - self._debounce_last.get(key, 0) < self._debounce_interval:
                return
        self._debounce_last[key] = now

        try:
            await client.chat_update(
                channel=chat_id,
                ts=message_id,
                text=text[:4000],
                blocks=blks,
            )
        except Exception as e:
            if "message_not_found" not in str(e):
                log.warning("slack.edit_with_actions_failed", error=str(e))

    # ── Task-specific message methods (rich Block Kit) ────────

    async def send_plan_preview(
        self, chat_id: str, message_id: str, task_id: str, plan: str
    ) -> None:
        from openclow.providers.chat.slack.blocks import plan_preview_blocks
        blks = plan_preview_blocks(plan, task_id)
        await self._update_or_post(chat_id, message_id, "Implementation Plan", blks)

    async def send_progress(
        self, chat_id: str, message_id: str, step: str, total_steps: int, current_step: int
    ) -> None:
        from openclow.providers.chat.slack.blocks import progress_blocks
        blks = progress_blocks(step, current_step, total_steps)
        client = self._get_client()

        try:
            await client.chat_update(
                channel=chat_id, ts=message_id,
                text=f"Implementing [{current_step}/{total_steps}] {step}",
                blocks=blks,
            )
        except Exception as e:
            if "message_not_found" not in str(e):
                log.warning("slack.progress_failed", error=str(e))

    async def send_summary(
        self, chat_id: str, message_id: str, task_id: str, summary: str, diff_summary: str
    ) -> None:
        from openclow.providers.chat.slack.blocks import summary_blocks
        blks = summary_blocks(summary, diff_summary, task_id)
        await self._update_or_post(chat_id, message_id, "Implementation Complete", blks)

    async def send_diff_preview(
        self, chat_id: str, message_id: str, task_id: str, diff_summary: str
    ) -> None:
        from openclow.providers.chat.slack.blocks import diff_preview_blocks
        blks = diff_preview_blocks(diff_summary, task_id)
        await self._update_or_post(chat_id, message_id, "Changes Ready", blks)

    async def send_pr_created(
        self, chat_id: str, message_id: str, task_id: str, pr_url: str
    ) -> None:
        from openclow.providers.chat.slack.blocks import pr_created_blocks
        blks = pr_created_blocks(pr_url, task_id)
        await self._update_or_post(chat_id, message_id, f"PR created! {pr_url}", blks)

    async def send_error(self, chat_id: str, message_id: str | None, text: str) -> None:
        from openclow.providers.chat.slack.blocks import error_blocks
        blks = error_blocks(text)
        if message_id:
            await self._update_or_post(chat_id, message_id, f"Error: {text[:500]}", blks)
        else:
            client = self._get_client()
            await client.chat_postMessage(
                channel=chat_id, text=f"Error: {text[:500]}", blocks=blks,
            )

    async def send_terminal_message(self, chat_id: str, message_id: str | None, text: str) -> None:
        from openclow.providers.chat.slack.blocks import terminal_blocks
        blks = terminal_blocks(text)
        if message_id:
            await self._update_or_post(chat_id, message_id, text[:4000], blks)
        else:
            client = self._get_client()
            await client.chat_postMessage(
                channel=chat_id, text=text[:4000], blocks=blks,
            )

    # ── Home Tab ──────────────────────────────────────────────

    async def publish_home_tab(self, user_id: str) -> None:
        """Publish/refresh the App Home Tab for a user."""
        client = self._get_client()
        from openclow.providers.chat.slack.handlers.home import publish_home
        await publish_home(client, user_id)

    # ── Internal helpers ──────────────────────────────────────

    async def _update_or_post(
        self, chat_id: str, message_id: str, fallback_text: str, blks: list[dict]
    ) -> None:
        """Try to update a message; fall back to posting a new one."""
        client = self._get_client()
        try:
            await client.chat_update(
                channel=chat_id, ts=message_id,
                text=fallback_text[:4000], blocks=blks,
            )
        except Exception as e:
            if "message_not_found" in str(e):
                log.debug("slack.message_not_found_fallback", chat_id=chat_id)
            else:
                log.warning("slack.update_failed_fallback", error=str(e))
            try:
                await client.chat_postMessage(
                    channel=chat_id, text=fallback_text[:4000], blocks=blks,
                )
            except Exception as e2:
                log.error("slack.post_fallback_failed", error=str(e2))

    # ── Bot lifecycle ─────────────────────────────────────────

    async def start_bot(self) -> None:
        """Start the Slack bot with Socket Mode."""
        import signal
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        app = AsyncApp(
            token=self.bot_token,
            signing_secret=self.signing_secret,
        )
        self._app = app

        # Register all handlers (order matters — catch-all last)
        from openclow.providers.chat.slack.handlers import commands, actions, events, modals, home
        commands.register(app)
        modals.register(app)
        home.register(app)
        actions.register(app)   # Must be after modals (catch-all last)
        events.register(app)

        # Heartbeat for Docker health check
        async def heartbeat():
            while True:
                Path("/tmp/bot_health").write_text(str(time.time()))
                await asyncio.sleep(10)
        asyncio.create_task(heartbeat())

        log.info("slack.socket_mode_starting")
        handler = AsyncSocketModeHandler(app, self.app_token)
        self._socket_handler = handler

        # Graceful shutdown on SIGINT/SIGTERM so watchfiles restart
        # doesn't leave a zombie Socket Mode connection on Slack's side.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self._shutdown()))

        await handler.start_async()

    async def _shutdown(self) -> None:
        """Close Socket Mode connection quickly so Slack frees the session."""
        log.info("slack.shutting_down")
        try:
            if self._socket_handler:
                await asyncio.wait_for(self._socket_handler.close_async(), timeout=3)
        except Exception:
            pass
        self._socket_handler = None

    async def close(self) -> None:
        await self._shutdown()
        if self._client:
            await self._client.close()
            self._client = None
