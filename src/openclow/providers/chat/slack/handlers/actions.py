"""Slack interactive action handlers — button clicks with rich Block Kit UI."""
from __future__ import annotations

import asyncio
import re

from openclow.providers.chat.slack import blocks
from openclow.providers.chat.slack.middleware import check_auth, is_admin_async
from openclow.services import bot_actions
from openclow.utils.logging import get_logger

log = get_logger()


async def _check_task_owner(user_id: str, task_id: str) -> tuple[bool, object | None]:
    """Check auth + task ownership. Returns (ok, task). Admins bypass ownership."""
    ok, db_user = await check_auth(user_id)
    if not ok:
        return False, None
    task = await bot_actions.get_task_by_id(task_id)
    if not task:
        return False, None
    if not db_user.is_admin and task.user_id != db_user.id:
        return False, None
    return True, task


async def _open_task_modal(client, trigger_id: str, channel_id: str, project_id: int | None = None):
    """Open the task creation modal — single entry point for all paths.

    If project_id is provided (task_for: button), use it directly.
    If channel is linked to a project, use the binding.
    Otherwise, show the full modal with project dropdown.
    Returns error string if modal can't be opened, None on success.
    """
    from openclow.services.channel_service import get_channel_project

    # 1. Explicit project (task_for: buttons)
    if project_id is not None:
        project = await bot_actions.get_project_by_id(project_id)
        if not project:
            return "Project not found."
        modal = blocks.build_task_modal_channel_scoped(channel_id, project_id, project.name)
        await client.views_open(trigger_id=trigger_id, view=modal)
        return None

    # 2. Channel linked to a project
    binding = await get_channel_project(channel_id)
    if binding:
        modal = blocks.build_task_modal_channel_scoped(
            channel_id, binding["project_id"], binding["project_name"],
        )
        await client.views_open(trigger_id=trigger_id, view=modal)
        return None

    # 3. No project context — show dropdown
    projects = await bot_actions.get_all_projects()
    if not projects:
        return "No projects configured."

    modal = blocks.build_task_modal(projects, channel_id)
    await client.views_open(trigger_id=trigger_id, view=modal)
    return None


# Actions that map to review_guard + job enqueue
_REVIEW_ACTIONS = {
    "approve_plan": ("execute_plan", ":white_check_mark: Plan approved! Starting implementation..."),
    "approve": ("approve_task", ":outbox_tray: Creating PR..."),
    "discard": ("discard_task", ":wastebasket: Discarding changes..."),
    "merge": ("merge_task", ":white_check_mark: Merging PR..."),
    "reject": ("reject_task", ":x: Rejecting and cleaning up..."),
}


async def _post_or_update(client, channel: str, ts: str | None, text: str, blks: list[dict]):
    """Update existing message or post new one."""
    if ts:
        try:
            await client.chat_update(channel=channel, ts=ts, text=text, blocks=blks)
            return ts
        except Exception:
            pass
    result = await client.chat_postMessage(channel=channel, text=text, blocks=blks)
    return result["ts"]


def register(app):
    """Register button action handlers on the Slack Bolt app."""

    # ── Claude Auth ──────────────────────────────────────────

    @app.action("claude_auth")
    async def handle_claude_auth(ack, body, client):
        """Start Claude re-authentication flow."""
        await ack()
        user_id = body["user"]["id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            return

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text="Starting authentication...",
            blocks=blocks.loading_blocks(":key: Starting Claude authentication..."),
        )

        try:
            from openclow.worker.arq_app import get_arq_pool
            pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
            await pool.enqueue_job(
                "claude_auth_task",
                channel, ts, "slack",
            )
        except Exception as e:
            log.error("slack.claude_auth_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Auth failed",
                blocks=blocks.error_blocks(f"Failed to start authentication: {str(e)[:200]}"),
            )

    # ── Review Actions (approve/merge/reject/discard) ────────

    @app.action(re.compile(r"^(approve_plan|approve|discard|merge|reject):"))
    async def handle_review_action(ack, body, client):
        await ack()

        # Get value or fall back to action_id
        action = body["actions"][0]
        action_value = action.get("value") or action.get("action_id")
        if not action_value:
            log.error("review_action.no_value", action=action)
            return
        action_name, task_id = action_value.split(":", 1)

        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return

        review_info = _REVIEW_ACTIONS.get(action_name)
        if not review_info:
            return

        _, progress_text = review_info
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        guard_ok, error_msg = await bot_actions.review_guard(
            action_name, task_id, user_id=user_id, is_admin=db_user.is_admin,
        )
        if not guard_ok:
            await client.chat_update(
                channel=channel, ts=ts,
                text=f"Cannot proceed: {error_msg}",
                blocks=blocks.error_blocks(f"Cannot proceed: {error_msg}"),
            )
            return

        await client.chat_update(
            channel=channel, ts=ts,
            text=progress_text,
            blocks=blocks.loading_blocks(progress_text),
        )

    # ── Main Menu ────────────────────────────────────────────

    @app.action("menu:main")
    async def handle_menu_main(ack, body, client):
        await ack()
        channel = body.get("channel", {}).get("id") or body.get("channel_id")
        ts = body.get("message", {}).get("ts")
        user_id = body["user"]["id"]
        if not channel:
            # Called from Home Tab — open DM instead
            try:
                dm = await client.conversations_open(users=[user_id])
                channel = dm["channel"]["id"]
            except Exception as e:
                log.error("menu_main.dm_open_failed", user_id=user_id, error=str(e))
                return
        # Show linked project name and health in welcome if channel is bound
        from openclow.services.channel_service import get_channel_project
        binding = await get_channel_project(channel) if channel else None
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
            dev_mode=await is_admin_async(user_id)
        )
        await _post_or_update(client, channel, ts, "THAG GROUP Menu", blks)

    @app.action("menu:cancel")
    async def handle_menu_cancel(ack, body, client):
        await ack()
        user_id = body["user"]["id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            return

        channel = body.get("channel", {}).get("id") or body.get("channel_id")
        ts = body.get("message", {}).get("ts")
        task = await bot_actions.cancel_latest_task(channel, user_id=user_id)
        if not task:
            blks = blocks.terminal_blocks("No cancellable tasks found.")
        else:
            desc = task.description[:60] if task.description else "Unknown"
            blks = blocks.terminal_blocks(f":octagonal_sign: Task cancelled: {desc}")
        await _post_or_update(client, channel, ts, "Cancel", blks)

    @app.action("menu:task")
    async def handle_menu_task(ack, body, client):
        await ack()
        user_id = body["user"]["id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            return

        channel_id = body["channel"]["id"]
        err = await _open_task_modal(client, body["trigger_id"], channel_id)
        if err:
            await client.chat_postMessage(
                channel=channel_id, text=err,
                blocks=blocks.project_list_blocks([]) if "No projects" in err else blocks.error_blocks(err),
            )

    @app.action(re.compile(r"^task_for:"))
    async def handle_task_for(ack, body, client):
        """Open task modal for a known project — no dropdown needed."""
        await ack()
        user_id = body["user"]["id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            return

        project_id = int(body["actions"][0]["value"].split(":", 1)[1])
        channel_id = body["channel"]["id"]
        err = await _open_task_modal(client, body["trigger_id"], channel_id, project_id=project_id)
        if err:
            await client.chat_postMessage(
                channel=channel_id, text=err, blocks=blocks.error_blocks(err),
            )

    @app.action("menu:projects")
    async def handle_menu_projects(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        # In a linked channel, show only the linked project
        from openclow.services.channel_service import get_channel_project
        binding = await get_channel_project(channel)
        projects = await bot_actions.get_all_projects()
        if binding and projects:
            projects = [p for p in projects if p.id == binding["project_id"]]

        blks = blocks.project_list_blocks(projects or [])
        await _post_or_update(client, channel, ts, "Projects", blks)

    @app.action("menu:status")
    async def handle_menu_status(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        user_id = body["user"]["id"]
        tasks = await bot_actions.get_active_tasks(channel, user_id=user_id)
        blks = blocks.status_blocks(tasks or [])
        await _post_or_update(client, channel, ts, "Status", blks)

    @app.action("menu:help")
    async def handle_menu_help(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        blks = blocks.help_blocks()
        await _post_or_update(client, channel, ts, "Help", blks)

    @app.action("menu:logs")
    async def handle_menu_logs(ack, body, client):
        await ack()
        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return
        if not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        await client.chat_update(
            channel=channel, ts=ts,
            text="Analyzing logs...",
            blocks=blocks.loading_blocks(":clipboard: Analyzing system logs..."),
        )
        try:
            await bot_actions.enqueue_job("smart_logs", channel, ts, "slack")
        except Exception as e:
            log.error("menu.logs_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Failed",
                blocks=blocks.error_blocks("Failed to fetch logs — worker unavailable."),
            )

    # ── Dashboard ────────────────────────────────────────────

    @app.action("menu:dashboard")
    async def handle_menu_dashboard(ack, body, client):
        await ack()
        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return
        if not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        from openclow.services.tunnel_service import get_tunnel_url
        url = await get_tunnel_url("dozzle")
        blks = blocks.dashboard_blocks(url) if url else blocks.dashboard_retry_blocks()
        await _post_or_update(client, channel, ts, "Dashboard", blks)

    @app.action("menu:dashboard_refresh")
    async def handle_dashboard_refresh(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text="Refreshing tunnel...",
            blocks=blocks.loading_blocks(":arrows_counterclockwise: Refreshing tunnel... (~10 seconds)"),
        )

        try:
            job = await bot_actions.enqueue_job("refresh_dashboard_tunnel", "dozzle")
            result = await job.result(timeout=25)
            if result and result.get("ok"):
                blks = blocks.dashboard_blocks(result["url"])
                await client.chat_update(channel=channel, ts=ts, text="Dashboard", blocks=blks)
            else:
                error = result.get("error", "Unknown error") if result else "Timeout"
                await client.chat_update(
                    channel=channel, ts=ts,
                    text=f"Refresh failed: {error}",
                    blocks=blocks.error_blocks(f"Dashboard refresh failed: {error}"),
                )
        except Exception as e:
            log.error("dashboard.refresh_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Refresh error",
                blocks=blocks.error_blocks(f"Dashboard refresh error: {str(e)[:200]}"),
            )

    @app.action("menu:dashboard_stop")
    async def handle_dashboard_stop(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        try:
            await bot_actions.enqueue_job("stop_dashboard_tunnel", "dozzle")
        except Exception as e:
            log.error("dashboard.stop_failed", error=str(e))

        await client.chat_update(
            channel=channel, ts=ts,
            text="Dashboard stopped",
            blocks=blocks.dashboard_stopped_blocks(),
        )

    # ── Settings ─────────────────────────────────────────────

    @app.action("menu:settings")
    async def handle_menu_settings(ack, body, client):
        await ack()
        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return
        if not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        from openclow.services.tunnel_service import get_tunnel_url
        url = await get_tunnel_url("settings")
        if url:
            settings_url = f"{url}/settings"
            wizard_url = f"{url}/settings/wizard"
            blks = blocks.settings_blocks(settings_url, wizard_url)
        else:
            blks = blocks.settings_retry_blocks()
        await _post_or_update(client, channel, ts, "Settings", blks)

    @app.action("menu:settings_refresh")
    async def handle_settings_refresh(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text="Refreshing settings tunnel...",
            blocks=blocks.loading_blocks(":arrows_counterclockwise: Refreshing settings tunnel... (~10 seconds)"),
        )

        try:
            job = await bot_actions.enqueue_job("refresh_dashboard_tunnel", "settings")
            result = await job.result(timeout=25)
            if result and result.get("ok"):
                url = result["url"]
                settings_url = f"{url}/settings"
                wizard_url = f"{url}/settings/wizard"
                blks = blocks.settings_blocks(settings_url, wizard_url)
                await client.chat_update(channel=channel, ts=ts, text="Settings", blocks=blks)
            else:
                error = result.get("error", "Unknown error") if result else "Timeout"
                await client.chat_update(
                    channel=channel, ts=ts,
                    text=f"Refresh failed: {error}",
                    blocks=blocks.error_blocks(f"Settings refresh failed: {error}"),
                )
        except Exception as e:
            log.error("settings.refresh_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Refresh error",
                blocks=blocks.error_blocks(f"Settings refresh error: {str(e)[:200]}"),
            )

    @app.action("menu:settings_stop")
    async def handle_settings_stop(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        try:
            await bot_actions.enqueue_job("stop_dashboard_tunnel", "settings")
        except Exception as e:
            log.error("settings.stop_failed", error=str(e))

        await client.chat_update(
            channel=channel, ts=ts,
            text="Settings stopped",
            blocks=blocks.settings_stopped_blocks(),
        )

    # ── Add Project ──────────────────────────────────────────

    @app.action("menu:addproject")
    async def handle_menu_addproject(ack, body, client):
        await ack()
        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return
        if not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text="Fetching repos...",
            blocks=blocks.loading_blocks("Fetching your GitHub repositories..."),
        )

        try:
            repos = await bot_actions.fetch_github_repos()
        except Exception:
            repos = []

        from openclow.services import project_service
        all_projects = await project_service.get_all_projects()
        existing_map = {}
        for p in (all_projects or []):
            if p.github_repo:
                existing_map[p.github_repo.lower()] = p.id

        blks = blocks.repo_list_blocks(repos or [], existing_map)
        await client.chat_update(
            channel=channel, ts=ts,
            text="Add Project",
            blocks=blks,
        )

    @app.action(re.compile(r"^add_repo:"))
    async def handle_add_repo(ack, body, client):
        await ack()
        action_value = body["actions"][0]["value"]
        repo = action_value.split(":", 1)[1]
        repo_url = f"https://github.com/{repo}"

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text=f"Onboarding {repo}...",
            blocks=blocks.loading_blocks(f":hourglass_flowing_sand: Onboarding `{repo}`..."),
        )

        try:
            await bot_actions.enqueue_job("onboard_project", repo_url, channel, ts, "slack")
        except Exception as e:
            log.error("addproject.enqueue_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text=f"Failed to onboard {repo}",
                blocks=blocks.error_blocks(f"Failed to start onboarding for `{repo}`."),
            )

    @app.action("add_repo_manual")
    async def handle_add_repo_manual(ack, body, client):
        await ack()
        # Post hint to use /oc-addproject with URL
        channel = body["channel"]["id"]
        await client.chat_postEphemeral(
            channel=channel,
            user=body["user"]["id"],
            text="Use `/oc-addproject https://github.com/owner/repo` to add a repo by URL.",
        )

    @app.action("add_repo_retry")
    async def handle_add_repo_retry(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text="Retrying...",
            blocks=blocks.loading_blocks("Fetching your GitHub repositories..."),
        )

        try:
            repos = await bot_actions.fetch_github_repos()
        except Exception:
            repos = []

        from openclow.services import project_service
        all_projects = await project_service.get_all_projects()
        existing_map = {}
        for p in (all_projects or []):
            if p.github_repo:
                existing_map[p.github_repo.lower()] = p.id

        blks = blocks.repo_list_blocks(repos or [], existing_map)
        await client.chat_update(channel=channel, ts=ts, text="Add Project", blocks=blks)

    @app.action(re.compile(r"^confirm_project:"))
    async def handle_confirm_project(ack, body, client):
        await ack()
        action_value = body["actions"][0]["value"]
        project_name = action_value.split(":", 1)[1]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        try:
            job = await bot_actions.enqueue_job("confirm_project", project_name)
            project_id = await job.result(timeout=10)

            if isinstance(project_id, dict) and "error" in project_id:
                error_msg = project_id.get("message", "Something went wrong.")
                await client.chat_update(
                    channel=channel, ts=ts,
                    text=f"Error: {error_msg}",
                    blocks=blocks.error_blocks(error_msg),
                )
            elif project_id:
                await client.chat_update(
                    channel=channel, ts=ts,
                    text=f"Setting up {project_name}...",
                    blocks=blocks.loading_blocks(f"Project '{project_name}' added! Setting up Docker environment..."),
                )
                await bot_actions.enqueue_job("bootstrap_project", project_id, channel, ts, "slack")
                # Auto-link channel to the newly added project
                try:
                    from openclow.services.channel_service import set_channel_project
                    await set_channel_project(channel, project_id, project_name)
                except Exception as e:
                    log.warning("slack.auto_link_failed", channel=channel, project=project_name, error=str(e))
            else:
                await client.chat_update(
                    channel=channel, ts=ts,
                    text="Failed",
                    blocks=blocks.error_blocks(f"Failed to add project '{project_name}'. Please try again."),
                )
        except Exception as e:
            log.error("confirm_project.failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Failed",
                blocks=blocks.error_blocks(f"Failed to confirm project '{project_name}'."),
            )

    @app.action("cancel_project")
    async def handle_cancel_project(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        blks = blocks.terminal_blocks("Project onboarding cancelled.")
        await client.chat_update(channel=channel, ts=ts, text="Cancelled", blocks=blks)

    # ── Project Detail ───────────────────────────────────────

    @app.action(re.compile(r"^project_detail:"))
    async def handle_project_detail(ack, body, client):
        await ack()
        action_value = body["actions"][0]["value"]
        project_id = int(action_value.split(":", 1)[1])

        project = await bot_actions.get_project_by_id(project_id)
        if not project:
            channel = body["channel"]["id"]
            ts = body["message"]["ts"]
            await client.chat_update(
                channel=channel, ts=ts,
                text="Project not found",
                blocks=blocks.error_blocks("Project not found."),
            )
            return

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        # Fetch tunnel URL with health check — only show "Open App" if tunnel is alive
        tunnel_url = None
        try:
            from openclow.services.tunnel_service import get_tunnel_url, check_tunnel_health
            t_url = await get_tunnel_url(project.name)
            if t_url and await check_tunnel_health(project.name):
                tunnel_url = t_url
        except Exception:
            pass

        blks = blocks.project_detail_blocks(project, tunnel_url=tunnel_url)
        await _post_or_update(client, channel, ts, f"Project: {project.name}", blks)

    # ── Destructive Action Confirmations ───────────────────────

    _DESTRUCTIVE_ACTIONS = {"project_down", "project_unlink", "project_remove"}

    _CONFIRM_WARNINGS = {
        "project_down": ":warning: *Stop Docker containers?*\nThis will shut down the dev environment for this project.",
        "project_unlink": ":warning: *Unlink this project?*\nThe Docker environment will be removed but the DB record will remain.",
        "project_remove": ":warning: *Remove this project entirely?*\nThis will delete the project record and all associated data. This cannot be undone.",
    }

    # ── Project Lifecycle Actions ────────────────────────────

    @app.action(re.compile(r"^(health|project_bootstrap|project_up|project_down|project_unlink|project_remove|project_relink):"))
    async def handle_project_action(ack, body, client):
        await ack()

        action_value = body["actions"][0]["value"]
        action_name, project_id_str = action_value.split(":", 1)

        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return
        if not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        project_id = int(project_id_str)

        # Destructive actions require confirmation first
        if action_name in _DESTRUCTIVE_ACTIONS:
            confirm_action = action_name.replace("project_", "confirm_")
            warning_text = _CONFIRM_WARNINGS.get(action_name, ":warning: Are you sure?")
            confirm_blocks = [
                blocks.section_block(warning_text),
                blocks.actions_block([
                    blocks.button_element(
                        "Confirm",
                        f"{confirm_action}:{project_id}",
                        value=f"{confirm_action}:{project_id}",
                        style="danger",
                    ),
                    blocks.button_element(
                        "Cancel",
                        f"project_detail:{project_id}",
                        value=f"project_detail:{project_id}",
                    ),
                ]),
            ]
            await client.chat_update(
                channel=channel, ts=ts,
                text=warning_text,
                blocks=confirm_blocks,
            )
            return

        job_map = {
            "health": "check_project_health",
            "project_bootstrap": "bootstrap_project",
            "project_up": "docker_up_task",
            "project_relink": "bootstrap_project",
        }

        progress_map = {
            "health": ":mag: Running health check...",
            "project_bootstrap": ":arrows_counterclockwise: Bootstrapping...",
            "project_up": ":arrow_forward: Starting Docker...",
            "project_relink": ":link: Re-linking project...",
        }

        job_name = job_map.get(action_name)
        if not job_name:
            return

        progress_text = progress_map.get(action_name, "Processing...")
        await client.chat_update(
            channel=channel, ts=ts,
            text=progress_text,
            blocks=blocks.loading_blocks(progress_text),
        )

        # For relink, mark active first
        if action_name == "project_relink":
            try:
                from openclow.models import Project, async_session
                from sqlalchemy import select
                async with async_session() as session:
                    result = await session.execute(select(Project).where(Project.id == project_id))
                    project = result.scalar_one_or_none()
                    if project:
                        project.status = "bootstrapping"
                        await session.commit()
            except Exception as e:
                log.warning("project_relink.status_update_failed", error=str(e))

        try:
            await bot_actions.enqueue_job(job_name, project_id, channel, ts, "slack")
        except Exception as e:
            log.error(f"slack.{action_name}_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text=f"Failed: {str(e)[:200]}",
                blocks=blocks.error_blocks(f"Failed to run {action_name.replace('_', ' ')}: {str(e)[:200]}"),
            )

    # ── Confirmed Destructive Actions ────────────────────────

    @app.action(re.compile(r"^(confirm_down|confirm_unlink|confirm_remove):"))
    async def handle_confirmed_destructive(ack, body, client):
        await ack()

        action_value = body["actions"][0]["value"]
        action_name, project_id_str = action_value.split(":", 1)

        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return
        if not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        project_id = int(project_id_str)

        confirm_job_map = {
            "confirm_down": ("docker_down_task", ":stop_button: Stopping Docker..."),
            "confirm_unlink": ("unlink_project_task", ":link: Unlinking project..."),
            "confirm_remove": ("remove_project_task", ":wastebasket: Removing project..."),
        }

        job_name, progress_text = confirm_job_map[action_name]
        await client.chat_update(
            channel=channel, ts=ts,
            text=progress_text,
            blocks=blocks.loading_blocks(progress_text),
        )

        try:
            await bot_actions.enqueue_job(job_name, project_id, channel, ts, "slack")
        except Exception as e:
            log.error(f"slack.{action_name}_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text=f"Failed: {str(e)[:200]}",
                blocks=blocks.error_blocks(f"Failed: {str(e)[:200]}"),
            )

    # ── Agent Diagnose ───────────────────────────────────────

    @app.action(re.compile(r"^agent_diagnose:"))
    async def handle_agent_diagnose(ack, body, client):
        await ack()

        action_value = body["actions"][0]["value"]
        project_id_str = action_value.split(":", 1)[1]

        user_id = body["user"]["id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            return

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        project_id = int(project_id_str)

        await client.chat_update(
            channel=channel, ts=ts,
            text="Agent diagnosing...",
            blocks=blocks.loading_blocks(":brain: Agent is analyzing the error..."),
        )

        try:
            error_context = f"Diagnose issues for project {project_id}"
            user_id = body["user"]["id"]
            await bot_actions.enqueue_job(
                "agent_session", error_context, channel, ts, "", "slack", user_id,
            )
        except Exception as e:
            log.error("agent_diagnose.failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Failed",
                blocks=blocks.error_blocks(f"Agent session failed: {str(e)[:200]}"),
            )

    @app.action(re.compile(r"^health_ref:"))
    async def handle_health_refresh(ack, body, client):
        await ack()
        action_value = body["actions"][0]["value"]
        project_id = int(action_value.split(":", 1)[1])
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text="Refreshing health check...",
            blocks=blocks.loading_blocks(":mag: Refreshing health check..."),
        )

        try:
            await bot_actions.enqueue_job("check_project_health", project_id, channel, ts, "slack")
        except Exception as e:
            log.error("health.refresh_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Failed",
                blocks=blocks.error_blocks("Health check failed — worker unavailable."),
            )

    # ── Tunnel Stop ──────────────────────────────────────────

    @app.action(re.compile(r"^tunnel_stop:"))
    async def handle_tunnel_stop(ack, body, client):
        await ack()
        action_value = body["actions"][0]["value"]
        project_id = int(action_value.split(":", 1)[1])
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text="Stopping tunnel...",
            blocks=blocks.loading_blocks("Stopping tunnel..."),
        )
        try:
            await bot_actions.enqueue_job("stop_tunnel_task", project_id, channel, ts, "slack")
        except Exception as e:
            log.error("tunnel.stop_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Failed", blocks=blocks.error_blocks("Failed to stop tunnel."),
            )

    # ── QA Tests ─────────────────────────────────────────────

    @app.action(re.compile(r"^qa:"))
    async def handle_qa_action(ack, body, client):
        await ack()
        action_value = body["actions"][0]["value"]
        scope = action_value.split(":", 1)[1]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        await client.chat_update(
            channel=channel, ts=ts,
            text=f"Starting QA ({scope})...",
            blocks=blocks.loading_blocks(f":test_tube: Starting QA tests ({scope})..."),
        )
        try:
            await bot_actions.enqueue_job("run_qa_tests", channel, ts, scope, "slack")
        except Exception as e:
            log.error("qa.action_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Failed", blocks=blocks.error_blocks("Failed to start QA — worker unavailable."),
            )

    # ── Task Management Actions ────────────────────────────────

    @app.action(re.compile(r"^task_view:"))
    async def handle_task_view(ack, body, client):
        """View task details and Claude session activity."""
        await ack()
        try:
            action_value = body["actions"][0]["value"]
            task_id = action_value.split(":", 1)[1]
            channel = body["channel"]["id"]
            ts = body["message"]["ts"]

            ok, task = await _check_task_owner(body["user"]["id"], task_id)
            if not ok or not task:
                await client.chat_update(
                    channel=channel, ts=ts,
                    text="Task not found", blocks=blocks.error_blocks("Task not found or access denied.")
                )
                return

            # Show task details with real-time status
            title = f"{task.description[:50]}"
            status_icon = blocks.STATUS_ICONS.get(task.status, ":question:")
            details = (
                f"{status_icon} *{task.status.replace('_', ' ').title()}*\n\n"
                f"*Task:* {task.description}\n"
                f"*Status:* {task.status}\n"
                f"*Progress:* {task.agent_turns or 0} agent turns\n"
            )
            if task.error_message:
                details += f"*Error:* {task.error_message}\n"
            if task.pr_url:
                details += f"*PR:* <{task.pr_url}|View PR>\n"

            # Build action buttons based on task status
            action_buttons = [
                blocks.button_element("🔄 Refresh", f"task_view:{task_id}", value=f"task_view:{task_id}"),
            ]

            # Add approval buttons based on task status
            if task.status == "diff_preview":
                action_buttons.append(blocks.button_element("✅ Approve", f"approve:{task_id}", value=f"approve:{task_id}", style="primary"))
                action_buttons.append(blocks.button_element("❌ Discard", f"discard:{task_id}", value=f"discard:{task_id}", style="danger"))
            elif task.status == "awaiting_approval":
                action_buttons.append(blocks.button_element("✅ Merge", f"merge:{task_id}", value=f"merge:{task_id}", style="primary"))
                action_buttons.append(blocks.button_element("❌ Reject", f"reject:{task_id}", value=f"reject:{task_id}", style="danger"))
            elif task.status == "plan_review":
                action_buttons.append(blocks.button_element("✅ Approve Plan", f"approve_plan:{task_id}", value=f"approve_plan:{task_id}", style="primary"))
                action_buttons.append(blocks.button_element("❌ Discard", f"discard:{task_id}", value=f"discard:{task_id}", style="danger"))

            # Add pause/cancel for cancellable statuses
            if task.status in ("pending", "preparing", "planning", "coding", "reviewing"):
                action_buttons.append(blocks.button_element("⏸️ Pause", f"task_pause:{task_id}", value=f"task_pause:{task_id}"))
            action_buttons.append(blocks.button_element("❌ Cancel", f"task_cancel:{task_id}", value=f"task_cancel:{task_id}", style="danger"))
            action_buttons.append(blocks.button_element("◀️ Back", "menu:status", value="menu:status"))

            await client.chat_update(
                channel=channel, ts=ts,
                text=title,
                blocks=[
                    blocks.section_block(details),
                    blocks.actions_block(action_buttons)
                ]
            )
            log.info("task_view.shown", task_id=task_id)
        except Exception as e:
            import traceback
            log.error("task_view_failed", error=str(e), traceback=traceback.format_exc())
            try:
                await client.chat_update(
                    channel=body["channel"]["id"], ts=body["message"]["ts"],
                    text="Error", blocks=blocks.error_blocks(f"Could not load task: {str(e)[:100]}")
                )
            except Exception:
                pass

    @app.action(re.compile(r"^task_pause:"))
    async def handle_task_pause(ack, body, client):
        """Pause a running task."""
        await ack()
        action_value = body["actions"][0]["value"]
        task_id = action_value.split(":", 1)[1]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        ok, task = await _check_task_owner(body["user"]["id"], task_id)
        if not ok:
            return

        try:
            await client.chat_update(
                channel=channel, ts=ts,
                text="⏸️ Paused", blocks=blocks.loading_blocks("Task paused. Resume it anytime."),
            )
            # TODO: Implement task pause in orchestrator/worker
            log.info("task.paused", task_id=task_id)
        except Exception as e:
            log.error("task_pause_failed", error=str(e))

    @app.action(re.compile(r"^task_cancel:"))
    async def handle_task_cancel(ack, body, client):
        """Cancel a task immediately."""
        await ack()
        action_value = body["actions"][0]["value"]
        task_id = action_value.split(":", 1)[1]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        ok, task = await _check_task_owner(body["user"]["id"], task_id)
        if not ok:
            return

        try:
            from openclow.models import Task, async_session
            from sqlalchemy import update
            import uuid
            async with async_session() as session:
                await session.execute(
                    update(Task)
                    .where(Task.id == uuid.UUID(task_id))
                    .values(status="cancelled")
                )
                await session.commit()

            await client.chat_update(
                channel=channel, ts=ts,
                text="❌ Cancelled",
                blocks=[
                    blocks.section_block(":white_check_mark: *Task Cancelled*\n\nThe task has been stopped."),
                    blocks.actions_block([
                        blocks.button_element("📊 View Tasks", "menu:status", value="menu:status"),
                        blocks.button_element("🚀 New Task", "menu:task", value="menu:task", style="primary"),
                        blocks.button_element("◀️ Menu", "menu:main", value="menu:main"),
                    ])
                ]
            )
            log.info("task.cancelled", task_id=task_id)
        except Exception as e:
            log.error("task_cancel_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Error", blocks=blocks.error_blocks(f"Could not cancel task: {str(e)[:100]}")
            )

    # ── Overflow Menus ────────────────────────────────────────

    @app.action(re.compile(r"^menu:overflow"))
    async def handle_overflow(ack, body, client):
        """Route overflow menu selections to the matching menu: handler."""
        await ack()
        selected = body["actions"][0].get("selected_option", {}).get("value", "")
        if not selected.startswith("menu:"):
            return

        channel = body.get("channel", {}).get("id") or body.get("channel_id")
        ts = body.get("message", {}).get("ts")
        if not channel:
            return

        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return

        # Route to the appropriate handler logic
        if selected in {"menu:logs", "menu:dashboard", "menu:settings"} and not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=channel, user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return
        if selected == "menu:logs":
            await client.chat_update(
                channel=channel, ts=ts,
                text="Analyzing logs...",
                blocks=blocks.loading_blocks(":clipboard: Analyzing system logs..."),
            )
            try:
                await bot_actions.enqueue_job("smart_logs", channel, ts, "slack")
            except Exception as e:
                log.error("overflow.logs_failed", error=str(e))
                await client.chat_update(
                    channel=channel, ts=ts, text="Failed",
                    blocks=blocks.error_blocks("Failed to fetch logs — worker unavailable."),
                )
        elif selected == "menu:dashboard":
            from openclow.services.tunnel_service import get_tunnel_url
            url = await get_tunnel_url("dozzle")
            blks = blocks.dashboard_blocks(url) if url else blocks.dashboard_retry_blocks()
            await _post_or_update(client, channel, ts, "Dashboard", blks)
        elif selected == "menu:settings":
            from openclow.services.tunnel_service import get_tunnel_url
            url = await get_tunnel_url("settings")
            if url:
                blks = blocks.settings_blocks(f"{url}/settings", f"{url}/settings/wizard")
            else:
                blks = blocks.settings_retry_blocks()
            await _post_or_update(client, channel, ts, "Settings", blks)
        elif selected == "menu:help":
            blks = blocks.help_blocks()
            await _post_or_update(client, channel, ts, "Help", blks)
        else:
            # Get project context if channel is bound
            binding = await bot_actions.get_channel_binding(channel, "slack")
            project_id = binding.get("project_id") if binding else None
            project = None
            tunnel_url = None
            if project_id:
                try:
                    project = await bot_actions.get_project_by_id(project_id)
                    if project:
                        from openclow.services.tunnel_service import get_tunnel_url, check_tunnel_health
                        t_url = await get_tunnel_url(project.name)
                        if t_url and await check_tunnel_health(project.name):
                            tunnel_url = t_url
                except Exception:
                    pass

            blks = blocks.welcome_blocks(
                project_name=project.name if project else None,
                project_id=project_id,
                tunnel_url=tunnel_url,
            )
            await _post_or_update(client, channel, ts, "Menu", blks)

    @app.action(re.compile(r"^project:overflow"))
    async def handle_project_overflow(ack, body, client):
        """Route project detail overflow selections."""
        await ack()
        selected = body["actions"][0].get("selected_option", {}).get("value", "")
        if not selected or ":" not in selected:
            return

        user_id = body["user"]["id"]
        ok, db_user = await check_auth(user_id)
        if not ok:
            return
        if not db_user.is_admin:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
            )
            return

        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        # Parse action_name:project_id from value
        action_name, project_id_str = selected.split(":", 1)
        project_id = int(project_id_str)

        # Destructive actions require confirmation first
        if action_name in _DESTRUCTIVE_ACTIONS:
            confirm_action = action_name.replace("project_", "confirm_")
            warning_text = _CONFIRM_WARNINGS.get(action_name, ":warning: Are you sure?")
            confirm_blks = [
                blocks.section_block(warning_text),
                blocks.actions_block([
                    blocks.button_element(
                        "Confirm",
                        f"{confirm_action}:{project_id}",
                        value=f"{confirm_action}:{project_id}",
                        style="danger",
                    ),
                    blocks.button_element(
                        "Cancel",
                        f"project_detail:{project_id}",
                        value=f"project_detail:{project_id}",
                    ),
                ]),
            ]
            await client.chat_update(
                channel=channel, ts=ts,
                text=warning_text,
                blocks=confirm_blks,
            )
            return

        job_map = {
            "project_bootstrap": "bootstrap_project",
            "project_up": "docker_up_task",
            "project_relink": "bootstrap_project",
        }
        progress_map = {
            "project_bootstrap": ":arrows_counterclockwise: Bootstrapping...",
            "project_up": ":arrow_forward: Starting Docker...",
            "project_relink": ":link: Re-linking project...",
        }

        job_name = job_map.get(action_name)
        if not job_name:
            return

        progress_text = progress_map.get(action_name, "Processing...")
        await client.chat_update(
            channel=channel, ts=ts, text=progress_text,
            blocks=blocks.loading_blocks(progress_text),
        )

        if action_name == "project_relink":
            try:
                from openclow.models import Project, async_session
                from sqlalchemy import select
                async with async_session() as session:
                    result = await session.execute(select(Project).where(Project.id == project_id))
                    project = result.scalar_one_or_none()
                    if project:
                        project.status = "bootstrapping"
                        await session.commit()
            except Exception as e:
                log.warning("project_relink.status_update_failed", error=str(e))

        try:
            await bot_actions.enqueue_job(job_name, project_id, channel, ts, "slack")
        except Exception as e:
            log.error(f"slack.overflow_{action_name}_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts, text=f"Failed: {str(e)[:200]}",
                blocks=blocks.error_blocks(f"Failed: {str(e)[:200]}"),
            )

    # ── Channel-Project Binding ──────────────────────────────

    @app.action(re.compile(r"^channel_bind:"))
    async def handle_channel_bind(ack, body, client):
        """Link a Slack channel to a project."""
        await ack()
        action_value = body["actions"][0]["value"]
        # Value format: "channel_bind:{project_id}:{project_name}"
        _, remainder = action_value.split(":", 1)  # strip "channel_bind:" prefix
        parts = remainder.split(":", 1)
        project_id = int(parts[0])
        project_name = parts[1] if len(parts) > 1 else "unknown"
        channel = body["channel"]["id"]

        from openclow.services.channel_service import set_channel_project
        await set_channel_project(channel, project_id, project_name)

        await client.chat_update(
            channel=channel,
            ts=body["message"]["ts"],
            text=f"Channel linked to {project_name}",
            blocks=[
                blocks.section_block(
                    f"✅ Channel linked to *{project_name}*\n\n"
                    f"All messages and tasks in this channel will now be scoped to this project."
                ),
                blocks.actions_block([
                    blocks.button_element("🔗 Unlink", f"project_unlink:{project_id}", value=f"project_unlink:{project_id}"),
                    blocks.button_element("Menu", "menu:main", value="menu:main"),
                ]),
            ],
        )

        # Try to update channel topic
        try:
            await client.conversations_setTopic(channel=channel, topic=f"🤖 THAG GROUP: {project_name}")
        except Exception:
            pass

    # ── DM Project Selector ─────────────────────────────────

    @app.action(re.compile(r"^dm_project_select:"))
    async def handle_dm_project_select(ack, body, client):
        await ack()
        action_value = body["actions"][0]["value"]
        _, project_id_str, pending_text = action_value.split(":", 2)
        project_id = int(project_id_str)
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        user_id = body["user"]["id"]

        # Cache selection
        await bot_actions.set_dm_project("slack", user_id, project_id)

        # Save as default project for this user
        from openclow.models.user import User
        from openclow.models.base import async_session
        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.chat_provider_uid == user_id, User.chat_provider_type == "slack")
            )
            db_user = result.scalar_one_or_none()
            if db_user:
                db_user.default_project_id = project_id
                await session.commit()

        # Get project name
        project = await bot_actions.get_project_by_id(project_id)
        project_name = project.name if project else "Project"

        await client.chat_update(
            channel=channel, ts=ts,
            text=f"Chatting in context of {project_name}",
            blocks=blocks.loading_blocks(f"🎯 Using *{project_name}* — processing your request..."),
        )

        try:
            await bot_actions.enqueue_job(
                "agent_session", pending_text, channel, ts, f"project_id:{project_id}", "slack", user_id,
            )
        except Exception as e:
            log.error("slack.dm_project_select_failed", error=str(e))
            await client.chat_update(
                channel=channel, ts=ts,
                text="Something went wrong.",
                blocks=blocks.error_blocks("Something went wrong. Try again."),
            )

    # ── No-op ────────────────────────────────────────────────

    @app.action("noop")
    async def handle_noop(ack):
        await ack()

    # URL buttons — Slack fires actions for these even though they just open links
    @app.action(re.compile(r"^(open_dashboard|open_settings|open_wizard|view_pr:)"))
    async def handle_url_buttons(ack):
        await ack()

    # Open App — the SMART entry point. Fixes everything needed to get a working URL.
    @app.action(re.compile(r"^open_app:"))
    async def handle_open_app(ack, body, client):
        await ack()
        try:
            action_value = body["actions"][0]["value"]
            project_id = int(action_value.split(":", 1)[1])
            channel = body["channel"]["id"]
            ts = body["message"]["ts"]

            from openclow.models import Project, async_session
            from sqlalchemy import select as sa_select
            async with async_session() as session:
                result = await session.execute(sa_select(Project).where(Project.id == project_id))
                project = result.scalar_one_or_none()
                if project:
                    session.expunge(project)

            if not project:
                await client.chat_update(channel=channel, ts=ts, text="Project not found",
                                         blocks=blocks.error_blocks("Project not found"))
                return

            await client.chat_update(channel=channel, ts=ts, text="Opening app...",
                                     blocks=[blocks.section_block(f"🔍 Checking *{project.name}*...")])

            # Step 1: Check if tunnel URL exists and app responds
            from openclow.services.tunnel_service import get_tunnel_url, check_tunnel_health, start_tunnel, stop_tunnel
            import httpx

            url = await get_tunnel_url(project.name)
            app_alive = False

            if url:
                # Probe the URL
                try:
                    async with httpx.AsyncClient(timeout=5, follow_redirects=True, verify=False) as http:
                        resp = await http.get(url)
                        app_alive = resp.status_code < 502
                except Exception:
                    pass

            if app_alive:
                # Everything works — show URL immediately
                _show_url = url
            else:
                # Something's broken — fix it inline
                await client.chat_update(channel=channel, ts=ts, text="Fixing...",
                                         blocks=[blocks.section_block(f"🔧 Fixing tunnel for *{project.name}*...")])

                # Kill old zombie tunnel
                await stop_tunnel(project.name)

                # Find the container IP
                compose_project = f"openclow-{project.name}"
                target = None
                try:
                    from openclow.worker.tasks.bootstrap import _get_tunnel_target
                    target = await _get_tunnel_target(
                        compose_project, f"/workspaces/_cache/{project.name}", project.id)
                except Exception:
                    pass

                if not target:
                    from openclow.services.port_allocator import get_app_port
                    target = f"http://localhost:{get_app_port(project.id)}"

                # Detect host_header from .env (for Laravel virtual hosts)
                import os
                host_header = None
                env_path = f"/workspaces/_cache/{project.name}/.env"
                if os.path.exists(env_path):
                    try:
                        with open(env_path) as f:
                            for line in f:
                                if line.strip().startswith("APP_URL="):
                                    from urllib.parse import urlparse
                                    app_url = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                                    parsed = urlparse(app_url)
                                    if parsed.hostname and ".trycloudflare.com" not in parsed.hostname \
                                            and parsed.hostname not in ("localhost", "127.0.0.1"):
                                        host_header = parsed.hostname
                                    break
                    except Exception:
                        pass

                # Start fresh tunnel
                new_url = await start_tunnel(project.name, target, host_header=host_header)

                if new_url:
                    # Update APP_URL in .env so app uses new tunnel
                    if os.path.exists(env_path):
                        try:
                            with open(env_path) as f:
                                content = f.read()
                            import re as _re
                            content = _re.sub(r'APP_URL=.*', f'APP_URL={new_url}', content)
                            with open(env_path, 'w') as f:
                                f.write(content)
                            # Clear Laravel config cache
                            from openclow.services.docker_guard import run_docker
                            container = f"{compose_project}-{project.app_container_name or 'laravel.test'}-1"
                            await run_docker("docker", "exec", container, "php", "artisan", "config:clear",
                                             actor="open_app", timeout=10)
                            await run_docker("docker", "exec", container, "php", "artisan", "cache:clear",
                                             actor="open_app", timeout=10)
                        except Exception:
                            pass
                    _show_url = new_url
                else:
                    # Tunnel failed — fall back to health check agent
                    await client.chat_update(
                        channel=channel, ts=ts, text="Checking app...",
                        blocks=blocks.agent_thinking_blocks("⚠️ Tunnel failed — running full health check..."),
                    )
                    await bot_actions.enqueue_job("check_project_health", project_id, channel, ts, "slack")
                    return

            # Show the working URL
            from openclow.providers.actions import ActionButton, project_nav_keyboard
            kb = project_nav_keyboard(
                project_id,
                ActionButton("🌐 Open", "open_link", url=_show_url),
                ActionButton("💚 Health", f"health:{project_id}"),
            )
            text = f"✅ *{project.name}* is running\n🔗 {_show_url}"
            blks = [blocks.section_block(text)] + blocks.translate_keyboard(kb)
            await client.chat_update(channel=channel, ts=ts, text=text, blocks=blks)

        except Exception as e:
            log.error("slack.open_app_failed", error=str(e))
            await client.chat_update(
                channel=body["channel"]["id"], ts=body["message"]["ts"],
                text="Error", blocks=blocks.error_blocks(f"Failed: {str(e)[:100]}")
            )

    # ── Home Tab Actions (prefixed with home:) ───────────────
    # These are identical to menu: actions but triggered from the Home Tab
    # where there's no channel context. We post to the user's DM.

    @app.action(re.compile(r"^home:"))
    async def handle_home_action(ack, body, client):
        await ack()
        action = body["actions"][0]
        action_id = action["action_id"]

        # Overflow menus send selected_option instead of action_id
        if action.get("type") == "overflow" and action.get("selected_option"):
            menu_action = action["selected_option"]["value"]
            if not menu_action.startswith("home:"):
                menu_action = f"menu:{menu_action.split(':', 1)[-1]}" if ":" in menu_action else menu_action
        else:
            menu_action = action_id.replace("home:", "menu:", 1)

        user_id = body["user"]["id"]

        ok, db_user = await check_auth(user_id)
        if not ok:
            return

        # Admin gate for restricted home tab actions
        if menu_action in {"menu:logs", "menu:dashboard", "menu:settings", "menu:addproject"} and not db_user.is_admin:
            await client.chat_postMessage(
                channel=channel,
                text=":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode.",
                blocks=blocks.error_blocks(":lock: This is an admin feature. Use `/oc-dev` to unlock developer mode."),
            )
            return

        # Open DM with user for posting results
        try:
            dm = await client.conversations_open(users=[user_id])
            channel = dm["channel"]["id"]
        except Exception as e:
            log.error("home.dm_open_failed", user_id=user_id, error=str(e))
            return

        if menu_action == "menu:task":
            err = await _open_task_modal(client, body["trigger_id"], channel)
            if err:
                await client.chat_postMessage(channel=channel, text=err, blocks=blocks.project_list_blocks([]))
        elif menu_action == "menu:projects":
            projects = await bot_actions.get_all_projects()
            await client.chat_postMessage(channel=channel, text="Projects", blocks=blocks.project_list_blocks(projects or []))
        elif menu_action == "menu:status":
            tasks = await bot_actions.get_all_active_tasks(limit=10, user_id=user_id)
            await client.chat_postMessage(channel=channel, text="Status", blocks=blocks.status_blocks(tasks or []))
        elif menu_action == "menu:help":
            await client.chat_postMessage(channel=channel, text="Help", blocks=blocks.help_blocks())
        elif menu_action == "menu:logs":
            msg = await client.chat_postMessage(channel=channel, text="Analyzing logs...", blocks=blocks.loading_blocks(":clipboard: Analyzing system logs..."))
            try:
                await bot_actions.enqueue_job("smart_logs", channel, msg["ts"], "slack")
            except Exception as e:
                log.error("home.logs_failed", error=str(e))
        elif menu_action == "menu:dashboard":
            from openclow.services.tunnel_service import get_tunnel_url
            url = await get_tunnel_url("dozzle")
            blks = blocks.dashboard_blocks(url) if url else blocks.dashboard_retry_blocks()
            await client.chat_postMessage(channel=channel, text="Dashboard", blocks=blks)
        elif menu_action == "menu:settings":
            from openclow.services.tunnel_service import get_tunnel_url
            url = await get_tunnel_url("settings")
            if url:
                blks = blocks.settings_blocks(f"{url}/settings", f"{url}/settings/wizard")
            else:
                blks = blocks.settings_retry_blocks()
            await client.chat_postMessage(channel=channel, text="Settings", blocks=blks)
        elif menu_action == "menu:addproject":
            # Use message-based flow (not modal) to avoid trigger_id expiration
            # since fetch_github_repos() can take 15+ seconds
            msg = await client.chat_postMessage(
                channel=channel, text="Fetching repos...",
                blocks=blocks.loading_blocks("Fetching your GitHub repositories..."),
            )
            try:
                repos = await bot_actions.fetch_github_repos()
            except Exception:
                repos = []
            from openclow.services import project_service
            all_projects = await project_service.get_all_projects()
            existing_map = {p.github_repo.lower(): p.id for p in (all_projects or []) if p.github_repo}
            blks = blocks.repo_list_blocks(repos or [], existing_map)
            await client.chat_update(channel=channel, ts=msg["ts"], text="Add Project", blocks=blks)
        else:
            # Show welcome with user's default project context if available
            project_id = db_user.default_project_id if db_user else None
            project = None
            tunnel_url = None
            if project_id:
                try:
                    project = await bot_actions.get_project_by_id(project_id)
                    if project:
                        from openclow.services.tunnel_service import get_tunnel_url, check_tunnel_health
                        t_url = await get_tunnel_url(project.name)
                        if t_url and await check_tunnel_health(project.name):
                            tunnel_url = t_url
                except Exception:
                    pass

            await client.chat_postMessage(
                channel=channel, text="Menu",
                blocks=blocks.welcome_blocks(
                    project_name=project.name if project else None,
                    project_id=project_id,
                    tunnel_url=tunnel_url,
                )
            )

    # ── Cancel Session ──────────────────────────────────────────

    @app.action("cancel_session")
    async def handle_cancel_session(ack, body, client):
        """Cancel an in-progress LLM session."""
        await ack()
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        try:
            await client.chat_update(
                channel=channel, ts=ts,
                text="Cancelled",
                blocks=blocks.error_blocks(":stop_sign: Request cancelled."),
            )
        except Exception as e:
            log.error("cancel_session.failed", error=str(e))

    # ── Cancel Repair Agent ─────────────────────────────────

    @app.action(re.compile(r"^cancel_repair:"))
    async def handle_cancel_repair(ack, body, client):
        """Cancel an in-progress repair agent via Redis flag."""
        await ack()
        action_value = body["actions"][0].get("value") or body["actions"][0].get("action_id", "")
        parts = action_value.split(":", 2)
        if len(parts) >= 3:
            chat_id, message_id = parts[1], parts[2]
        else:
            chat_id = body["channel"]["id"]
            message_id = body["message"]["ts"]

        try:
            from openclow.worker.tasks._agent_helper import set_cancel_flag
            await set_cancel_flag(chat_id, message_id)

            channel = body["channel"]["id"]
            ts = body["message"]["ts"]
            await client.chat_update(
                channel=channel, ts=ts,
                text="Cancelling...",
                blocks=[blocks.section_block("⏹ *Cancelling...* Agent will stop after current tool call.")],
            )
        except Exception as e:
            log.error("cancel_repair.failed", error=str(e))

    # ── Catch-all (must be LAST) ─────────────────────────────

    @app.action(re.compile(r".*"))
    async def handle_unknown(ack, body):
        await ack()
        log.debug("slack.unhandled_action", action=body.get("actions", [{}])[0].get("action_id"))
