"""Slack slash command handlers — Employee mode.

Slack is for team members, not admins. Admin commands (addproject, docker,
bootstrap, logs, settings) are managed via Telegram or the Settings Dashboard.

Note: Slack reserves /status, /help, /join, /leave, /me, etc.
We prefix with /oc- (OpenClow) to avoid conflicts.
"""
from __future__ import annotations

from openclow.providers.chat.slack import blocks
from openclow.providers.chat.slack.handlers.actions import _open_task_modal
from openclow.providers.chat.slack.middleware import check_auth, is_admin_async
from openclow.services import bot_actions
from openclow.utils.logging import get_logger

log = get_logger()


async def _ack_if_unauthorized(ack, body, client) -> bool:
    """Check auth and send ephemeral denial. Returns True if unauthorized."""
    await ack()
    user_id = body["user_id"]
    ok, db_user = await check_auth(user_id)
    if not ok:
        await client.chat_postEphemeral(
            channel=body["channel_id"],
            user=user_id,
            text="You are not authorized. Ask an admin to add your Slack ID.",
            blocks=blocks.error_blocks("You are not authorized. Ask an admin to add your Slack ID."),
        )
        return True
    return False


def register(app):
    """Register slash command handlers on the Slack Bolt app."""

    # ── /oc-task ─────────────────────────────────────────────

    @app.command("/oc-task")
    async def handle_task(ack, body, client):
        if await _ack_if_unauthorized(ack, body, client):
            return

        channel_id = body["channel_id"]
        err = await _open_task_modal(client, body["trigger_id"], channel_id)
        if err:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=body["user_id"],
                text=err,
                blocks=blocks.project_list_blocks([]) if "No projects" in err else blocks.error_blocks(err),
            )

    # ── /oc-status ───────────────────────────────────────────

    @app.command("/oc-status")
    async def handle_status(ack, body, client):
        if await _ack_if_unauthorized(ack, body, client):
            return

        tasks = await bot_actions.get_active_tasks(body["channel_id"], user_id=body["user_id"])
        blks = blocks.status_blocks(tasks or [])
        await client.chat_postMessage(
            channel=body["channel_id"],
            text=f"Active Tasks ({len(tasks or [])})",
            blocks=blks,
        )

    # ── /oc-projects ─────────────────────────────────────────

    @app.command("/oc-projects")
    async def handle_projects(ack, body, client):
        if await _ack_if_unauthorized(ack, body, client):
            return

        channel_id = body["channel_id"]
        projects = await bot_actions.get_all_projects()

        # In a linked channel, show only the linked project
        from openclow.services.channel_service import get_channel_project
        binding = await get_channel_project(channel_id)
        if binding and projects:
            projects = [p for p in projects if p.id == binding["project_id"]]

        blks = blocks.project_list_blocks(projects or [])
        await client.chat_postMessage(
            channel=channel_id,
            text=f"Projects ({len(projects or [])})",
            blocks=blks,
        )

    # ── /oc-help ─────────────────────────────────────────────

    @app.command("/oc-help")
    async def handle_help(ack, body, client):
        await ack()
        blks = blocks.help_blocks()
        await client.chat_postMessage(
            channel=body["channel_id"],
            text="OpenClow Commands",
            blocks=blks,
        )

    # ── Admin gate ────────────────────────────────────────────

    _ADMIN_LOCKED_MSG = ":lock: This is an admin command. Use `/oc-dev` to unlock developer mode."

    async def _admin_gate(ack, body, client) -> bool:
        """Check admin status. Returns True if blocked (not an admin)."""
        await ack()
        if await is_admin_async(body["user_id"]):
            return False  # allowed
        await client.chat_postEphemeral(
            channel=body["channel_id"], user=body["user_id"],
            text=_ADMIN_LOCKED_MSG,
        )
        return True  # blocked

    # ── /oc-addproject ───────────────────────────────────────

    @app.command("/oc-addproject")
    async def handle_addproject(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        # Dev mode active — show the welcome menu with admin buttons
        from openclow.services.channel_service import get_channel_project
        binding = await get_channel_project(body["channel_id"])
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
            dev_mode=True
        )
        await client.chat_postMessage(
            channel=body["channel_id"], text="Use the menu buttons below.",
            blocks=blks,
        )

    # ── /oc-logs ─────────────────────────────────────────────

    @app.command("/oc-logs")
    async def handle_logs(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        msg = await client.chat_postMessage(
            channel=body["channel_id"],
            text="Analyzing logs...",
            blocks=blocks.loading_blocks(":clipboard: Analyzing system logs..."),
        )
        try:
            await bot_actions.enqueue_job("smart_logs", body["channel_id"], msg["ts"], "slack")
        except Exception as e:
            log.error("cmd.logs_failed", error=str(e))
            await client.chat_update(
                channel=body["channel_id"], ts=msg["ts"],
                text="Failed",
                blocks=blocks.error_blocks("Failed to fetch logs — worker unavailable."),
            )

    # ── /oc-dashboard ────────────────────────────────────────

    @app.command("/oc-dashboard")
    async def handle_dashboard(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        from openclow.services.tunnel_service import get_tunnel_url
        url = await get_tunnel_url("dozzle")
        blks = blocks.dashboard_blocks(url) if url else blocks.dashboard_retry_blocks()
        await client.chat_postMessage(
            channel=body["channel_id"], text="Dashboard", blocks=blks,
        )

    # ── /oc-settings ─────────────────────────────────────────

    @app.command("/oc-settings")
    async def handle_settings(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        from openclow.services.tunnel_service import get_tunnel_url
        url = await get_tunnel_url("settings")
        if url:
            blks = blocks.settings_blocks(f"{url}/settings", f"{url}/settings/wizard")
        else:
            blks = blocks.settings_retry_blocks()
        await client.chat_postMessage(
            channel=body["channel_id"], text="Settings", blocks=blks,
        )

    # ── /oc-cancel ───────────────────────────────────────────

    @app.command("/oc-cancel")
    async def handle_cancel(ack, body, client):
        if await _ack_if_unauthorized(ack, body, client):
            return

        task = await bot_actions.cancel_latest_task(body["channel_id"], user_id=body["user_id"])
        if not task:
            await client.chat_postEphemeral(
                channel=body["channel_id"],
                user=body["user_id"],
                text="No cancellable tasks found.",
                blocks=blocks.terminal_blocks("No cancellable tasks found."),
            )
            return

        desc = task.description[:60] if task.description else "Unknown"
        blks = blocks.terminal_blocks(f":octagonal_sign: Task cancelled: {desc}")
        await client.chat_postMessage(
            channel=body["channel_id"],
            text=f"Task cancelled: {desc}",
            blocks=blks,
        )

    # ── /oc-adduser ──────────────────────────────────────────

    @app.command("/oc-adduser")
    async def handle_adduser(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        await client.chat_postEphemeral(
            channel=body["channel_id"], user=body["user_id"],
            text="Use the Settings Dashboard to manage users.",
        )

    # ── /oc-removeproject ────────────────────────────────────

    @app.command("/oc-removeproject")
    async def handle_removeproject(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        await client.chat_postEphemeral(
            channel=body["channel_id"], user=body["user_id"],
            text="Use the Settings Dashboard to remove projects.",
        )

    # ── /oc-dockerup ─────────────────────────────────────────

    @app.command("/oc-dockerup")
    async def handle_dockerup(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        await client.chat_postEphemeral(
            channel=body["channel_id"], user=body["user_id"],
            text="Use the Settings Dashboard or Telegram for Docker operations.",
        )

    # ── /oc-dockerdown ───────────────────────────────────────

    @app.command("/oc-dockerdown")
    async def handle_dockerdown(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        await client.chat_postEphemeral(
            channel=body["channel_id"], user=body["user_id"],
            text="Use the Settings Dashboard or Telegram for Docker operations.",
        )

    # ── /oc-bootstrap ────────────────────────────────────────

    @app.command("/oc-bootstrap")
    async def handle_bootstrap(ack, body, client):
        if await _admin_gate(ack, body, client):
            return
        await client.chat_postEphemeral(
            channel=body["channel_id"], user=body["user_id"],
            text="Use the Settings Dashboard or Telegram for bootstrap operations.",
        )

    # ── /oc-qa ───────────────────────────────────────────────

    @app.command("/oc-qa")
    async def handle_qa(ack, body, client):
        """Run automated QA tests: /oc-qa [smoke|full]"""
        await ack()
        user_id = body["user_id"]
        ok, _ = await check_auth(user_id)
        if not ok:
            await client.chat_postEphemeral(
                channel=body["channel_id"],
                user=user_id,
                text="You are not authorized.",
            )
            return

        text = (body.get("text") or "").strip()
        scope = text if text in ("smoke", "full") else "smoke"

        msg = await client.chat_postMessage(
            channel=body["channel_id"],
            text=f":test_tube: Starting QA tests ({scope})...",
            blocks=blocks.loading_blocks(f":test_tube: Starting QA tests ({scope})..."),
        )
        try:
            await bot_actions.enqueue_job(
                "run_qa_tests",
                body["channel_id"], msg["ts"], scope, "slack",
            )
        except Exception as e:
            log.error("qa.enqueue_failed", error=str(e))
            await client.chat_update(
                channel=body["channel_id"], ts=msg["ts"],
                text="Failed to start QA.",
                blocks=blocks.error_blocks("Failed to start QA — worker unavailable."),
            )

    # ── /oc-dev ──────────────────────────────────────────────

    @app.command("/oc-dev")
    async def handle_dev(ack, body, client):
        """Open dev mode password modal, or extend if already active."""
        if await _ack_if_unauthorized(ack, body, client):
            return

        user_id = body["user_id"]

        if await is_admin_async(user_id):
            # Already in dev mode — extend the session
            from openclow.providers.chat.slack.middleware import grant_dev_mode, DEV_SESSION_TTL
            grant_dev_mode(user_id)
            await client.chat_postEphemeral(
                channel=body["channel_id"],
                user=user_id,
                text=f"Dev mode extended for another {DEV_SESSION_TTL // 60} minutes.",
                blocks=blocks.dev_mode_status_blocks(True, DEV_SESSION_TTL // 60),
            )
            return

        modal = blocks.build_dev_modal()
        await client.views_open(trigger_id=body["trigger_id"], view=modal)

    # ── /oc-devoff ───────────────────────────────────────────

    @app.command("/oc-devoff")
    async def handle_devoff(ack, body, client):
        """Deactivate dev mode."""
        await ack()
        from openclow.providers.chat.slack.middleware import revoke_dev_mode
        revoke_dev_mode(body["user_id"])
        await client.chat_postEphemeral(
            channel=body["channel_id"],
            user=body["user_id"],
            text="Developer mode deactivated.",
            blocks=blocks.dev_mode_status_blocks(False),
        )
