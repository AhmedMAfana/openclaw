"""AI-powered logs — Docker MCP fetches raw logs, Groq LLM summarizes."""
import asyncio

from openclow.utils.docker_path import get_docker_env

from openclow.utils.logging import get_logger

log = get_logger()

ANALYSIS_PROMPT = """Analyze these Docker container logs from an TAGH Dev platform.
Extract and categorize into:

1. ERRORS — crashes, exceptions, failed operations (most important)
2. WARNINGS — degraded performance, retries, timeouts
3. KEY EVENTS — service starts, deployments, task completions

Format for Telegram (plain text, no markdown, use emojis):

📋 System Logs

🔴 Errors:
  • container: description (time ago)

⚠️ Warnings:
  • container: description

✅ Key Events:
  • container: description

If no errors, say "No errors detected" under the errors section.
Keep each item to one line, max 60 chars. Max 15 items total.
If logs are healthy with no issues, say so briefly.

RAW LOGS:
{logs}
"""


async def _fetch_container_logs() -> str:
    """Fetch recent logs from all TAGH Dev containers."""
    # Get container names
    _denv = get_docker_env()
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "--format", "{{.Names}}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_denv,
    )
    stdout, _ = await proc.communicate()
    containers = [c.strip() for c in stdout.decode().strip().split("\n") if c.strip()]

    if not containers:
        return "No running containers found."

    all_logs = []
    for name in containers:
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", name, "--tail", "80", "--timestamps",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_denv,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            continue

        # Docker sends app logs to both stdout and stderr
        output = (stdout.decode() + stderr.decode()).strip()
        if output:
            # Take last 40 lines per container to stay within token limits
            lines = output.split("\n")[-40:]
            all_logs.append(f"=== {name} ===\n" + "\n".join(lines))

    return "\n\n".join(all_logs) if all_logs else "No log output from containers."


async def _summarize_with_groq(raw_logs: str) -> str | None:
    """Send raw logs to Groq Llama for AI summary."""
    try:
        from openclow.services.config_service import get_config
        config = await get_config("stt", "provider")
        api_key = config.get("api_key") if config else ""
        if not api_key:
            from openclow.settings import settings
            api_key = settings.groq_api_key
        if not api_key:
            return None

        # Truncate logs to ~6000 chars to stay within token limits
        truncated = raw_logs[:6000]

        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "user", "content": ANALYSIS_PROMPT.format(logs=truncated)},
                    ],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                try:
                    text = data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, TypeError):
                    log.warning("logs_task.groq_malformed_response")
                    return None
                text = text.replace("**", "").replace("```", "").replace("`", "")
                return text
            log.warning("logs_task.groq_failed", status=resp.status_code)
            return None
    except Exception as e:
        log.warning("logs_task.groq_error", error=str(e))
        return None


def _basic_summary(raw_logs: str) -> str:
    """Fallback: basic grep for errors/warnings if Groq unavailable."""
    lines = raw_logs.split("\n")
    errors = [l for l in lines if "error" in l.lower() or "exception" in l.lower()]
    warnings = [l for l in lines if "warning" in l.lower() or "warn" in l.lower()]

    parts = ["📋 System Logs (raw filter)\n"]

    if errors:
        parts.append("🔴 Errors:")
        for e in errors[-5:]:
            parts.append(f"  • {e[-80:]}")
    else:
        parts.append("🔴 No errors detected")

    if warnings:
        parts.append("\n⚠️ Warnings:")
        for w in warnings[-5:]:
            parts.append(f"  • {w[-80:]}")

    parts.append(f"\n📊 Total: {len(lines)} log lines from containers")
    return "\n".join(parts)


async def smart_logs(ctx: dict, chat_id: str, message_id: str, chat_provider_type: str = "telegram"):
    """Worker task: fetch Docker logs, AI-summarize, send to Telegram."""
    from openclow.providers import factory

    chat = await factory.get_chat_by_type(chat_provider_type)
    try:
        await chat.edit_message(chat_id, message_id, "📋 Fetching logs from all containers...")

        raw_logs = await _fetch_container_logs()

        await chat.edit_message(chat_id, message_id, "🤖 Analyzing logs with AI...")

        # Try AI summary, fallback to basic grep
        summary = await _summarize_with_groq(raw_logs)
        if not summary:
            summary = _basic_summary(raw_logs)

        # Send result with buttons
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        kb = ActionKeyboard(rows=[
            ActionRow([ActionButton("🔄 Refresh", "menu:logs")]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await chat.edit_message_with_actions(chat_id, message_id, summary, kb)
        log.info("logs_task.done", summary_length=len(summary))

    except asyncio.CancelledError:
        from openclow.providers.actions import ActionButton, ActionKeyboard, ActionRow
        cancel_kb = ActionKeyboard(rows=[
            ActionRow([ActionButton("🔄 Retry", "menu:logs")]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await chat.edit_message_with_actions(chat_id, message_id, "⏹ Log analysis cancelled.", cancel_kb)
        raise
    except Exception as e:
        log.error("logs_task.failed", error=str(e))
        error_kb = ActionKeyboard(rows=[
            ActionRow([ActionButton("🔄 Retry", "menu:logs")]),
            ActionRow([ActionButton("◀️ Main Menu", "menu:main")]),
        ])
        await chat.edit_message_with_actions(
            chat_id, message_id,
            f"Failed to fetch logs: {str(e)[:200]}",
            error_kb,
        )
