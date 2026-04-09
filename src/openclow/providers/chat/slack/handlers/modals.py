"""Slack modal submission handlers — task creation flow."""
from __future__ import annotations

from openclow.providers.chat.slack.middleware import check_auth
from openclow.services import bot_actions
from openclow.utils.logging import get_logger

log = get_logger()


def register(app):
    """Register modal view submission handlers on the Slack Bolt app."""

    @app.view("task_submit")
    async def handle_task_submit(ack, body, client, view):
        """User submitted the task creation modal."""
        await ack()

        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return

        # Extract values from the modal
        values = view["state"]["values"]
        project_id = int(
            values["project_block"]["project_select"]["selected_option"]["value"]
        )
        description = values["description_block"]["task_description"]["value"].strip()

        if len(description) < 10:
            # Re-open modal with error (Slack validation)
            await ack(response_action="errors", errors={
                "description_block": "Please provide at least 10 characters.",
            })
            return

        channel_id = view.get("private_metadata", "")

        try:
            # Create task in DB
            task = await bot_actions.create_task(
                user_id=db_user.id,
                project_id=project_id,
                description=description,
                chat_id=channel_id,
                chat_provider_type="slack",
            )

            # Send initial status message
            result = await client.chat_postMessage(
                channel=channel_id,
                text="Task submitted! Preparing...",
            )
            message_ts = result["ts"]

            # Dispatch to worker
            job = await bot_actions.enqueue_job("execute_task", str(task.id))

            # Save message ID and job ID
            await bot_actions.update_task_message(task.id, message_ts, job.job_id)

            log.info("slack.task_dispatched", task_id=str(task.id))

        except Exception as e:
            log.error("slack.task_submit_failed", error=str(e))
            if channel_id:
                await client.chat_postMessage(
                    channel=channel_id,
                    text=f"Failed to create task: {str(e)[:200]}",
                )
