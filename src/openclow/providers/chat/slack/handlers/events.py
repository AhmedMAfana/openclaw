"""Slack event handlers — messages, app mentions, with rich Block Kit responses."""
from __future__ import annotations

from openclow.providers.chat.slack import blocks
from openclow.providers.chat.slack.middleware import check_auth
from openclow.services import bot_actions
from openclow.utils.logging import get_logger

log = get_logger()


def register(app):
    """Register event handlers on the Slack Bolt app."""

    @app.event("app_mention")
    async def handle_mention(event, client):
        """Respond when the bot is @mentioned with rich blocks."""
        user_id = event.get("user", "")
        if not user_id:
            log.error("slack.event_missing_user_id", event_type="app_mention", channel=event["channel"])
            return
        ok, _ = await check_auth(user_id)
        if not ok:
            from openclow.providers.chat.slack import blocks as _blocks
            await client.chat_postEphemeral(
                channel=event["channel"],
                user=user_id,
                text="You are not authorized.",
                blocks=_blocks.error_blocks("You are not authorized. Ask an admin to add your Slack ID."),
            )
            return

        text = event.get("text", "")
        # Strip the bot mention from the text
        import re
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        thread_ts = event.get("thread_ts") or event.get("ts")

        if not text:
            from openclow.services.channel_service import get_channel_project
            from openclow.providers.chat.slack.middleware import is_dev_mode
            binding = await get_channel_project(event["channel"])
            project_name = binding["project_name"] if binding else None
            project_id = binding["project_id"] if binding else None
            tunnel_url = None
            if project_id and project_name:
                try:
                    from openclow.services.tunnel_service import get_tunnel_url, check_tunnel_health
                    t_url = await get_tunnel_url(project_name)
                    if t_url and await check_tunnel_health(project_name):
                        tunnel_url = t_url
                except Exception:
                    pass
            blks = blocks.welcome_blocks(
                project_name=project_name,
                project_id=project_id,
                tunnel_url=tunnel_url,
                dev_mode=is_dev_mode(user_id)
            )
            await client.chat_postMessage(
                channel=event["channel"],
                text="Use /oc-task to submit a task, or /oc-help for commands.",
                blocks=blks,
                thread_ts=thread_ts,
            )
            return

        # Show thinking indicator
        thinking = await client.chat_postMessage(
            channel=event["channel"],
            text="Processing your request...",
            blocks=blocks.agent_thinking_blocks(text),
            thread_ts=thread_ts,
        )

        try:
            # Check if channel is linked to a project
            from openclow.services.channel_service import get_channel_project
            binding = await get_channel_project(event["channel"])
            project_context = f"project_id:{binding['project_id']}" if binding else ""

            await bot_actions.enqueue_job(
                "agent_session", text, event["channel"], thinking["ts"], project_context, "slack", user_id,
            )
        except Exception as e:
            log.error("slack.mention_failed", error=str(e))
            await client.chat_update(
                channel=event["channel"],
                ts=thinking["ts"],
                text="Something went wrong.",
                blocks=blocks.error_blocks("Something went wrong. Try `/oc-help` for available commands."),
            )

    @app.event("message")
    async def handle_message(event, client):
        """Handle messages — DMs always, channels only if linked to a project."""
        # Ignore bot messages, edits, and subtypes to avoid loops
        if event.get("bot_id") or event.get("subtype"):
            return

        channel_type = event.get("channel_type", "")
        channel = event["channel"]
        is_dm = channel_type == "im"
        user_id = event.get("user", "")
        if not user_id:
            log.error("slack.event_missing_user_id", event_type="message", channel=channel)
            return

        # For channels/groups: only respond if the channel is linked to a project
        if not is_dm:
            from openclow.services.channel_service import get_channel_project
            binding = await get_channel_project(channel)
            if not binding:
                await client.chat_postEphemeral(
                    channel=channel,
                    user=user_id,
                    blocks=blocks.build_unlinked_channel_prompt(channel),
                )
                return
        ok, _ = await check_auth(user_id)
        if not ok:
            return

        text = event.get("text", "")
        if not text or text.startswith("/"):
            return

        # Skip messages that are @mentions (handled by handle_mention)
        import re
        if re.search(r"<@[A-Z0-9]+>", text):
            return

        thread_ts = event.get("thread_ts") or event.get("ts") if not is_dm else None

        # Show thinking indicator
        thinking = await client.chat_postMessage(
            channel=channel,
            text="Processing your request...",
            blocks=blocks.agent_thinking_blocks(text),
            **({"thread_ts": thread_ts} if thread_ts else {}),
        )

        try:
            if is_dm:
                from openclow.services.channel_service import get_channel_project
                binding = await get_channel_project(channel)
                if not binding:
                    # Check for cached DM project selection
                    cached_pid = await bot_actions.get_dm_project("slack", user_id)
                    if cached_pid:
                        binding = {"project_id": cached_pid}
                    else:
                        # Check user's default project
                        from openclow.models.user import User
                        from openclow.models.base import async_session
                        from sqlalchemy import select
                        async with async_session() as session:
                            result = await session.execute(
                                select(User).where(User.chat_provider_uid == user_id, User.chat_provider_type == "slack")
                            )
                            db_user = result.scalar_one_or_none()
                        if db_user and db_user.default_project_id:
                            binding = {"project_id": db_user.default_project_id}
                        else:
                            projects = await bot_actions.get_all_projects()
                            if len(projects) == 1:
                                binding = {"project_id": projects[0].id}
                            elif len(projects) > 1:
                                await client.chat_update(
                                    channel=channel,
                                    ts=thinking["ts"],
                                    text="Select a project",
                                    blocks=blocks.build_dm_project_selector(projects, text),
                                )
                                return
            project_context = f"project_id:{binding['project_id']}" if binding else ""

            await bot_actions.enqueue_job(
                "agent_session", text, channel, thinking["ts"], project_context, "slack", user_id,
            )
        except Exception as e:
            log.error("slack.message_failed", error=str(e))
            await client.chat_update(
                channel=channel,
                ts=thinking["ts"],
                text="Something went wrong.",
                blocks=blocks.error_blocks("Something went wrong. Try the buttons below."),
            )
