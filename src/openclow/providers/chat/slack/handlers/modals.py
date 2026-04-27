"""Slack modal submission handlers — task creation, project addition, user management."""
from __future__ import annotations

from openclow.providers.chat.slack import blocks
from openclow.providers.chat.slack.middleware import check_auth
from openclow.services import bot_actions
from openclow.utils.logging import get_logger

log = get_logger()


async def _create_and_dispatch_task(client, db_user, project_id: int, description: str, channel_id: str, skip_planning: bool):
    """Create a task in DB, post status message, and dispatch to worker.

    Shared by both task_submit and task_submit_scoped.
    """
    task = None
    mode = "quick" if skip_planning else "full"
    try:
        log.info("slack.creating_task", user_id=db_user.id, project_id=project_id, channel=channel_id, mode=mode)

        task = await bot_actions.create_task(
            user_id=db_user.id,
            project_id=project_id,
            description=description,
            chat_id=channel_id,
            chat_provider_type="slack",
            git_mode="branch_per_task",
        )

        mode_label = ":zap: Quick" if skip_planning else ":clipboard: Full"
        result = await client.chat_postMessage(
            channel=channel_id,
            text="Task submitted! Preparing...",
            blocks=blocks.loading_blocks(f":rocket: Task submitted ({mode_label}) — preparing..."),
        )

        job = await bot_actions.enqueue_job("execute_task", str(task.id), skip_planning)
        await bot_actions.update_task_message(task.id, result["ts"], job.job_id)

        log.info("slack.task_dispatched", task_id=str(task.id), job_id=job.job_id)

    except Exception as e:
        log.error("slack.task_submit_failed", error=str(e), exc_info=True)
        if task:
            try:
                from openclow.models import async_session, Task
                from sqlalchemy import update
                async with async_session() as session:
                    await session.execute(
                        update(Task)
                        .where(Task.id == task.id)
                        .values(status="failed", error_message=f"Dispatch failed: {str(e)[:200]}")
                    )
                    await session.commit()
            except Exception as mark_err:
                log.error("slack.failed_to_mark_task_failed", error=str(mark_err))
        try:
            await client.chat_postMessage(
                channel=channel_id,
                text=f"Failed to create task: {str(e)[:200]}",
                blocks=blocks.error_blocks(f"Failed to dispatch task: {str(e)[:200]}"),
            )
        except Exception as e2:
            log.error("slack.task_submit_error_notify_failed", error=str(e2))


def register(app):
    """Register modal view submission handlers on the Slack Bolt app."""

    # ── Task Submit (with project dropdown) ──────────────────

    @app.view("task_submit")
    async def handle_task_submit(ack, body, client, view):
        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            await ack(response_action="errors", errors={
                "description_block": "You are not authorized to create tasks.",
            })
            return

        values = view["state"]["values"]

        # Validate project selection
        project_select = values.get("project_block", {}).get("project_select", {})
        if not project_select or not project_select.get("selected_option"):
            await ack(response_action="errors", errors={
                "project_block": "Please select a project.",
            })
            return

        try:
            project_id = int(project_select["selected_option"]["value"])
        except (ValueError, KeyError) as e:
            log.error("slack.task_submit_invalid_project", error=str(e))
            await ack(response_action="errors", errors={
                "project_block": "Invalid project selected.",
            })
            return

        description = values.get("description_block", {}).get("task_description", {}).get("value", "").strip()
        if len(description) < 10:
            await ack(response_action="errors", errors={
                "description_block": "Please provide at least 10 characters.",
            })
            return

        mode_select = values.get("mode_block", {}).get("task_mode", {})
        skip_planning = (mode_select.get("selected_option") or {}).get("value", "quick") == "quick"

        channel_id = view.get("private_metadata", "")
        if not channel_id:
            log.error("slack.task_submit_no_channel", user_id=user_id)
            await ack(response_action="errors", errors={
                "description_block": "Internal error: No channel context. Please try again.",
            })
            return

        await ack()
        await _create_and_dispatch_task(client, db_user, project_id, description, channel_id, skip_planning)

    # ── Channel-Scoped Task Submit (linked channel / known project) ──

    @app.view("task_submit_scoped")
    async def handle_task_submit_scoped(ack, body, client, view):
        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            await ack(response_action="errors", errors={
                "description_block": "You are not authorized to create tasks.",
            })
            return

        # Parse metadata: "channel_id:project_id"
        metadata = view.get("private_metadata", "")
        parts = metadata.rsplit(":", 1)
        if len(parts) != 2:
            await ack(response_action="errors", errors={
                "description_block": "Internal error: bad metadata. Try again.",
            })
            return

        channel_id, project_id_str = parts
        try:
            project_id = int(project_id_str)
        except ValueError:
            await ack(response_action="errors", errors={
                "description_block": "Internal error: invalid project. Try again.",
            })
            return

        values = view["state"]["values"]
        description = values.get("description_block", {}).get("task_description", {}).get("value", "").strip()
        if len(description) < 10:
            await ack(response_action="errors", errors={
                "description_block": "Please provide at least 10 characters.",
            })
            return

        mode_select = values.get("mode_block", {}).get("task_mode", {})
        skip_planning = (mode_select.get("selected_option") or {}).get("value", "quick") == "quick"

        await ack()
        await _create_and_dispatch_task(client, db_user, project_id, description, channel_id, skip_planning)

    # ── Add Project Submit ───────────────────────────────────

    @app.view("addproject_submit")
    async def handle_addproject_submit(ack, body, client, view):
        """User submitted the add project modal."""
        user_id = body["user"]["id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            await ack(response_action="errors", errors={
                "manual_url_block": "You are not authorized to add projects.",
            })
            return

        values = view["state"]["values"]
        channel_id = view.get("private_metadata", "")
        
        if not channel_id:
            log.error("slack.addproject_submit_no_channel", user_id=user_id)
            await ack(response_action="errors", errors={
                "manual_url_block": "Internal error: No channel context. Please try again.",
            })
            return

        # Check radio button selection first
        repo_url = None
        repo_select = values.get("repo_select_block", {}).get("repo_select", {})
        if repo_select and repo_select.get("selected_option"):
            repo_name = repo_select["selected_option"]["value"]
            repo_url = f"https://github.com/{repo_name}"

        # Check manual URL input (takes priority if both filled)
        manual_url = values.get("manual_url_block", {}).get("manual_url", {}).get("value", "").strip()
        if manual_url:
            repo_url = manual_url

        if not repo_url:
            await ack(response_action="errors", errors={
                "manual_url_block": "Please select a repository or enter a URL.",
            })
            return
        
        # Basic URL validation
        if not (repo_url.startswith("https://github.com/") or repo_url.startswith("http://github.com/") or "/" in repo_url):
            await ack(response_action="errors", errors={
                "manual_url_block": "Please enter a valid GitHub URL (e.g., https://github.com/owner/repo).",
            })
            return

        # Acknowledge immediately
        await ack()

        try:
            log.info("slack.addproject_submit", repo=repo_url, channel=channel_id, user_id=user_id)
            
            msg = await client.chat_postMessage(
                channel=channel_id,
                text=f"Onboarding {repo_url}...",
                blocks=blocks.loading_blocks(f":hourglass_flowing_sand: Onboarding `{repo_url}`..."),
            )
            
            job = await bot_actions.enqueue_job(
                "onboard_project", repo_url, channel_id, msg["ts"], "slack",
            )
            
            log.info("slack.addproject_dispatched", repo=repo_url, job_id=job.job_id)
        except Exception as e:
            log.error("slack.addproject_submit_failed", error=str(e), exc_info=True)
            if channel_id:
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=f"Failed to onboard: {str(e)[:200]}",
                        blocks=blocks.error_blocks(f"Failed to start onboarding: {str(e)[:200]}"),
                    )
                except Exception as e2:
                    log.error("slack.addproject_error_notify_failed", error=str(e2))

    # ── Add User Submit ──────────────────────────────────────

    @app.view("adduser_submit")
    async def handle_adduser_submit(ack, body, client, view):
        """User submitted the add user modal."""
        submitter_id = body["user"]["id"]
        ok, _ = await check_auth(submitter_id)
        if not ok:
            await ack(response_action="errors", errors={
                "user_select_block": "You are not authorized to add users.",
            })
            return

        values = view["state"]["values"]
        channel_id = view.get("private_metadata", "")
        
        # Validate user selection
        user_select = values.get("user_select_block", {}).get("user_select", {})
        if not user_select or not user_select.get("selected_user"):
            await ack(response_action="errors", errors={
                "user_select_block": "Please select a user to add.",
            })
            return
        
        slack_user_id = user_select["selected_user"]
        
        # Prevent adding yourself (redundant but safe)
        if slack_user_id == submitter_id:
            log.info("slack.adduser_self_add_attempt", submitter_id=submitter_id)
        
        username = (values.get("username_block", {}).get("username_input", {}).get("value") or "").strip()

        # Acknowledge immediately
        await ack()

        try:
            from openclow.models import User, async_session
            from sqlalchemy import select

            async with async_session() as session:
                existing = await session.execute(
                    select(User).where(User.chat_provider_uid == slack_user_id)
                )
                user = existing.scalar_one_or_none()
                if user:
                    user.is_allowed = True
                    if username:
                        user.username = username
                    await session.commit()
                    status_text = f"User <@{slack_user_id}> updated and authorized :white_check_mark:"
                    log.info("admin.updateuser_slack", slack_id=slack_user_id, username=username)
                else:
                    # Look up the user's display name if no username provided
                    if not username:
                        try:
                            info = await client.users_info(user=slack_user_id)
                            username = info["user"].get("real_name") or info["user"].get("name")
                        except Exception as e:
                            log.warning("slack.user_info_lookup_failed", error=str(e))
                            username = f"slack_{slack_user_id[:8]}"

                    user = User(
                        chat_provider_type="slack",
                        chat_provider_uid=slack_user_id,
                        username=username,
                        is_allowed=True,
                    )
                    session.add(user)
                    await session.commit()
                    status_text = f"User <@{slack_user_id}> ({username}) added and authorized :white_check_mark:"
                    log.info("admin.adduser_slack", slack_id=slack_user_id, username=username)

            if channel_id:
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=status_text,
                        blocks=[blocks.section_block(status_text)],
                    )
                except Exception as e:
                    log.error("slack.adduser_notify_failed", error=str(e))

        except Exception as e:
            log.error("slack.adduser_failed", error=str(e), exc_info=True)
            if channel_id:
                try:
                    await client.chat_postMessage(
                        channel=channel_id,
                        text=f"Failed to add user: {str(e)[:200]}",
                        blocks=blocks.error_blocks(f"Failed to add user: {str(e)[:200]}"),
                    )
                except Exception as e2:
                    log.error("slack.adduser_error_notify_failed", error=str(e2))

    # ── Dev Mode Unlock ─────────────────────────────────────

    @app.view("dev_unlock")
    async def handle_dev_unlock(ack, body, client, view):
        """Validate dev password and grant dev mode."""
        user_id = body["user"]["id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            await ack(response_action="errors", errors={
                "password_block": "You are not authorized.",
            })
            return

        values = view["state"]["values"]
        password = values.get("password_block", {}).get("dev_password", {}).get("value", "").strip()

        if not password:
            await ack(response_action="errors", errors={
                "password_block": "Please enter the developer password.",
            })
            return

        # Check password against stored config
        from openclow.services import config_service
        stored = await config_service.get_config("system", "dev_password")
        expected = stored.get("value", "") if stored else ""

        if not expected:
            await ack(response_action="errors", errors={
                "password_block": "No dev password configured. Ask an admin to set it.",
            })
            return

        import secrets
        if not secrets.compare_digest(password, expected):
            await ack(response_action="errors", errors={
                "password_block": "Incorrect password.",
            })
            return

        await ack()

        # Persist admin flag on successful dev unlock
        if not db_user.is_admin:
            db_user.is_admin = True
            from openclow.models import async_session
            async with async_session() as session:
                session.add(db_user)
                await session.commit()

        from openclow.providers.chat.slack.middleware import grant_dev_mode, DEV_SESSION_TTL
        grant_dev_mode(user_id)

        log.info("slack.dev_mode_unlocked", user_id=user_id)

        # Find a channel to send the confirmation to (DM to the user)
        try:
            dm = await client.conversations_open(users=[user_id])
            dm_channel = dm["channel"]["id"]
            await client.chat_postMessage(
                channel=dm_channel,
                text=f"Developer mode unlocked for {DEV_SESSION_TTL // 60} minutes.",
                blocks=blocks.dev_mode_status_blocks(True, DEV_SESSION_TTL // 60),
            )
        except Exception as e:
            log.warning("slack.dev_mode_dm_failed", error=str(e))
