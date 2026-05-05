"""Claude authentication task — lets users re-authenticate from chat."""
from __future__ import annotations

import asyncio
import json
import os
import pty
import re
import time

from taghdev.providers import factory
from taghdev.utils.logging import get_logger

log = get_logger()

_URL_RE = re.compile(r"(https://(?:claude\.com|claude\.ai)\S+)")


def _find_listen_port(pid: int) -> int | None:
    """Return the TCP port the given PID is LISTEN-ing on (via /proc), or None."""
    proc_inodes: set[str] = set()
    try:
        for fd_name in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd_name}")
                if "socket" in target:
                    proc_inodes.add(target.split("[")[1].rstrip("]"))
            except OSError:
                pass
    except OSError:
        return None

    try:
        with open(f"/proc/{pid}/net/tcp6") as f:
            for line in f:
                parts = line.split()
                # state 0A = LISTEN
                if len(parts) > 9 and parts[3] == "0A" and parts[9] in proc_inodes:
                    port_hex = parts[1].split(":")[-1]
                    return int(port_hex, 16)
    except OSError:
        pass
    return None


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
            m = _URL_RE.search(text)
            if m:
                url = m.group(1)
                break
        except asyncio.TimeoutError:
            continue

    if not url:
        try:
            stderr_data = await asyncio.wait_for(proc.stderr.read(4000), timeout=3)
            m = _URL_RE.search(stderr_data.decode())
            if m:
                url = m.group(1)
        except Exception:
            pass

    if not url:
        proc.kill()
        from taghdev.providers.actions import ActionButton, ActionKeyboard, ActionRow
        await chat.edit_message_with_actions(
            chat_id, message_id,
            "Failed to get auth URL. Tap to try again.",
            ActionKeyboard(rows=[
                ActionRow([ActionButton("🔑 Try Again", "claude_auth", style="primary")]),
                ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
            ]),
        )
        return

    from taghdev.providers.actions import ActionButton, ActionKeyboard, ActionRow
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
                m = _URL_RE.search(text)
                if m:
                    url = m.group(1)
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


async def claude_auth_login_web(ctx: dict) -> dict:
    """Long-running web auth flow.

    The CLI opens a local HTTP server on a random port to receive the OAuth
    callback.  The browser is supposed to hit http://localhost:PORT/callback
    but in Docker the container's localhost != the user's browser localhost.
    We discover the port, store it in Redis, and let the API endpoint forward
    the code on behalf of the user's browser.

    Flow:
    1. Spawn `claude auth login` (PTY stdin so CLI behaves interactively)
    2. Extract OAuth URL from stdout; discover local callback port via /proc
    3. Publish URL + port + state to Redis
    4. Wait up to 3 min for the CLI to exit (it exits once it receives the callback)
    5. Verify with `claude auth status`
    """
    import redis.asyncio as aioredis
    from taghdev.settings import settings

    SESSION = "claude_auth:web"
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.delete(
        f"{SESSION}:url", f"{SESSION}:port", f"{SESSION}:state",
        f"{SESSION}:code", f"{SESSION}:status",
    )

    master_fd, slave_fd = pty.openpty()

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "login",
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        os.close(master_fd)
        os.close(slave_fd)
        await r.setex(f"{SESSION}:status", 300, f"error:{str(e)[:200]}")
        return {"status": "error", "message": str(e)[:200]}

    os.close(slave_fd)

    # Extract OAuth URL from stdout (first 15 s)
    url = None
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
            if not line:
                break
            text = line.decode().strip()
            m = _URL_RE.search(text)
            if m:
                url = m.group(1)
                break
        except asyncio.TimeoutError:
            continue

    if not url:
        try:
            stderr_data = await asyncio.wait_for(proc.stderr.read(4000), timeout=3)
            m = _URL_RE.search(stderr_data.decode())
            if m:
                url = m.group(1)
        except Exception:
            pass

    if not url:
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        await r.setex(f"{SESSION}:status", 300, "error:No URL")
        return {"status": "error", "message": "Failed to get auth URL"}

    # Extract state from OAuth URL
    state_match = re.search(r"state=([^&\s]+)", url)
    state = state_match.group(1) if state_match else ""

    # Give the CLI a moment to open its local HTTP server, then discover the port
    await asyncio.sleep(0.5)
    port = _find_listen_port(proc.pid)
    log.info("auth.web_session_ready", port=port, has_state=bool(state))

    # Publish to Redis so the API can return them to the frontend
    await r.setex(f"{SESSION}:url", 300, url)
    if port:
        await r.setex(f"{SESSION}:port", 300, str(port))
    if state:
        await r.setex(f"{SESSION}:state", 300, state)

    # Drain stdout/stderr continuously to prevent pipe-buffer deadlock
    captured = {"out": b"", "err": b""}

    async def _drain(stream, key):
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), timeout=5)
                    if not chunk:
                        break
                    captured[key] += chunk
                except asyncio.TimeoutError:
                    continue
        except Exception:
            pass

    drain_tasks = [
        asyncio.create_task(_drain(proc.stdout, "out")),
        asyncio.create_task(_drain(proc.stderr, "err")),
    ]

    # Wait for the user to submit the auth code (up to 3 min)
    code = None
    deadline = time.time() + 180
    while time.time() < deadline:
        code = await r.get(f"{SESSION}:code")
        if code:
            break
        await asyncio.sleep(1)

    if not code:
        for t in drain_tasks:
            t.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        await r.setex(f"{SESSION}:status", 60, "error:Timed out waiting for code")
        return {"status": "timeout", "message": "Sign-in timed out (3 min) — try again"}

    # Forward the code to the CLI's local callback server (must run from
    # within this container — the API container can't reach [::1] here)
    if port:
        import httpx as _httpx
        cb_url = f"http://[::1]:{port}/callback?code={code}&state={state}"
        try:
            async with _httpx.AsyncClient() as client:
                resp = await client.get(cb_url, follow_redirects=False, timeout=10)
            log.info("auth.web_callback_sent", status=resp.status_code, port=port)
        except Exception as e:
            log.error("auth.web_callback_failed", error=str(e)[:200], port=port)
            for t in drain_tasks:
                t.cancel()
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
            await r.setex(f"{SESSION}:status", 60, f"error:{str(e)[:200]}")
            return {"status": "error", "message": f"Callback failed: {str(e)[:200]}"}
    else:
        log.warning("auth.web_no_port", msg="Port not found; CLI may not receive code")

    # Wait for CLI to process the callback and exit
    try:
        await asyncio.wait_for(proc.wait(), timeout=60)
    except asyncio.TimeoutError:
        for t in drain_tasks:
            t.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        log.error("auth.web_code_timeout",
                  stdout=captured["out"][-500:].decode(errors="replace"),
                  stderr=captured["err"][-500:].decode(errors="replace"))
        await r.setex(f"{SESSION}:status", 60, "error:CLI hung after callback")
        return {"status": "error", "message": "CLI hung after callback"}

    for t in drain_tasks:
        t.cancel()
    try:
        os.close(master_fd)
    except OSError:
        pass

    log.info("auth.web_cli_exited",
             rc=proc.returncode,
             stdout=captured["out"][-500:].decode(errors="replace"),
             stderr=captured["err"][-500:].decode(errors="replace"))

    # Verify auth succeeded
    try:
        status_proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(status_proc.communicate(), timeout=10)
        status = json.loads(out.decode())
        if status.get("loggedIn"):
            await r.setex(f"{SESSION}:status", 60, "ok")
            log.info("auth.web_login_success", method=status.get("authMethod"))
            return {"status": "ok", "loggedIn": True}
        else:
            await r.setex(f"{SESSION}:status", 60, "error:Not authenticated")
            return {"status": "error", "message": "Authentication failed — try again"}
    except Exception as e:
        await r.setex(f"{SESSION}:status", 60, f"error:{str(e)[:200]}")
        return {"status": "error", "message": str(e)[:200]}
