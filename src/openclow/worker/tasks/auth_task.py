"""Claude authentication task — lets users re-authenticate from chat."""
from __future__ import annotations

import asyncio
import json
import re
import time

from openclow.providers import factory
from openclow.utils.logging import get_logger

log = get_logger()


async def claude_auth_task(ctx: dict, chat_id: str, message_id: str,
                           chat_provider_type: str = "telegram"):
    """Run claude auth login, capture URL, send to user, wait for completion."""
    try:
        chat = await factory.get_chat_by_type(chat_provider_type)
    except Exception:
        chat = await factory.get_chat()

    await chat.edit_message(chat_id, message_id, "🔑 Starting authentication...")

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "login",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        await chat.edit_message(chat_id, message_id, f"Failed to start auth: {str(e)[:200]}")
        return

    # Read output for the auth URL
    url = None
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
            if not line:
                break
            text = line.decode().strip()
            match = re.search(r"(https://claude\.com\S+)", text)
            if match:
                url = match.group(1)
                break
        except asyncio.TimeoutError:
            continue

    if not url:
        try:
            stderr_data = await asyncio.wait_for(proc.stderr.read(4000), timeout=3)
            match = re.search(r"(https://claude\.com\S+)", stderr_data.decode())
            if match:
                url = match.group(1)
        except Exception:
            pass

    if not url:
        proc.kill()
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        await chat.edit_message_with_actions(
            chat_id, message_id,
            "Failed to get auth URL. Tap to try again.",
            ActionKeyboard(rows=[
                ActionRow([ActionButton("🔑 Try Again", "claude_auth", style="primary")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ]),
        )
        return

    from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
    kb = ActionKeyboard(rows=[
        ActionRow([ActionButton("🔑 Open Auth Page", "open_auth", url=url)]),
        ActionRow([ActionButton("◀️ Cancel", "menu:main")]),
    ])
    await chat.edit_message_with_actions(
        chat_id, message_id,
        "🔑 Authenticate Claude\n\n"
        "Tap the button to sign in.\n"
        "After signing in, come back — it confirms automatically.",
        kb,
    )

    # Wait for login to complete
    try:
        await asyncio.wait_for(proc.wait(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await chat.edit_message_with_actions(
            chat_id, message_id,
            "Auth timed out (3 min). Tap to try again.",
            ActionKeyboard(rows=[
                ActionRow([ActionButton("🔑 Try Again", "claude_auth")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ]),
        )
        return

    # Check if login succeeded
    try:
        status_proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await status_proc.communicate()
        status = json.loads(out.decode())

        if status.get("loggedIn"):
            method = status.get("authMethod", "unknown")
            await chat.edit_message_with_actions(
                chat_id, message_id,
                f"✅ Claude authenticated!\n\n"
                f"Method: {method}\n"
                f"Agent is ready.",
                ActionKeyboard(rows=[
                    ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
                ]),
            )
            log.info("auth.login_success", method=method)
        else:
            await chat.edit_message_with_actions(
                chat_id, message_id,
                "Auth did not complete. Try again.",
                ActionKeyboard(rows=[
                    ActionRow([ActionButton("🔑 Try Again", "claude_auth")]),
                    ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
                ]),
            )
    except Exception as e:
        log.error("auth.status_check_failed", error=str(e))
        await chat.edit_message(chat_id, message_id, f"Auth check failed: {str(e)[:200]}")


async def claude_auth_check(ctx: dict) -> dict:
    """Check Claude auth status — called from API via arq."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return json.loads(out.decode())
    except Exception as e:
        return {"loggedIn": False, "error": str(e)[:200]}


async def claude_auth_get_url(ctx: dict) -> dict:
    """Start claude login and return the OAuth URL — called from API via arq."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "login",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        url = None
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
                if not line:
                    break
                text = line.decode().strip()
                match = re.search(r"(https://claude\.com\S+)", text)
                if match:
                    url = match.group(1)
                    break
            except asyncio.TimeoutError:
                continue

        if url:
            return {"status": "pending", "url": url}
        else:
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            return {"status": "error", "message": "Failed to get auth URL"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}
