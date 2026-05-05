"""Slack App Home Tab — main dashboard view when user clicks the bot in sidebar."""
from __future__ import annotations

from taghdev.providers.chat.slack.middleware import check_auth
from taghdev.services import bot_actions
from taghdev.utils.logging import get_logger

log = get_logger()


def register(app):
    """Register Home Tab event handler on the Slack Bolt app."""

    @app.event("app_home_opened")
    async def handle_home_opened(event, client):
        user_id = event.get("user", "")
        ok, _ = await check_auth(user_id)
        if not ok:
            # Show unauthorized home tab
            from taghdev.providers.chat.slack.blocks import header_block, section_block
            await client.views_publish(
                user_id=user_id,
                view={
                    "type": "home",
                    "blocks": [
                        header_block(":zap: THAG GROUP"),
                        section_block(
                            "You are not authorized to use this app.\n\n"
                            f"Your Slack ID: `{user_id}`\n"
                            "Ask an admin to add you."
                        ),
                    ],
                },
            )
            return

        await publish_home(client, user_id)


async def publish_home(client, user_id: str) -> None:
    """Publish (or refresh) the App Home Tab for a user."""
    from taghdev.providers.chat.slack.blocks import home_tab_blocks

    try:
        projects = await bot_actions.get_all_projects()
        # Home Tab shows all active tasks across channels
        tasks = await bot_actions.get_all_active_tasks(limit=5)
    except Exception as e:
        log.error("home.fetch_data_failed", error=str(e))
        projects = []
        tasks = []

    blocks = home_tab_blocks(projects=projects, tasks=tasks)

    try:
        await client.views_publish(
            user_id=user_id,
            view={"type": "home", "blocks": blocks},
        )
    except Exception as e:
        log.error("home.publish_failed", error=str(e))
