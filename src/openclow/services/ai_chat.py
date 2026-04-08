"""AI Chat Service — Groq Llama (fast chat) + Claude CLI (heavy tasks fallback).

Chat responses use Groq Llama API directly from the bot (~1s).
Coding/review tasks still use Claude Code CLI on the worker.
"""
import asyncio
import json

from openclow.utils.logging import get_logger

log = get_logger()

CHAT_SYSTEM_PROMPT = """You are OpenClow's AI assistant — a senior-level DevOps and development expert.
You help developers manage their projects through natural conversation on Telegram.

FORMATTING (CRITICAL):
- NO markdown. No asterisks, no backticks, no code blocks.
- Plain text ONLY. Use emojis sparingly for visual clarity.
- Keep replies under 80 words unless asked for detail.
- Use line breaks for readability.
- Never write slash commands — the UI has buttons for actions.
- Be warm, professional, concise.

YOUR CAPABILITIES:
- Create development tasks (bug fixes, features, refactors)
- Add/manage/unlink/remove GitHub projects
- Check system status (Docker, DB, Redis)
- Health check running projects
- Diagnose setup failures and explain why things broke
- View active tasks and PRs

RULES:
- If user asks WHY something failed → check the context for recent errors and explain clearly
- If user asks about setup/bootstrap failure → look at audit logs in context, explain the root cause
- If user wants dev work → tell them to tap "New Task" button
- If user asks about status → summarize from context below
- For greetings → be warm, mention their projects
- NEVER say "I can't help with that" — always try to answer from context
- NEVER make up information — use the context below

CONTEXT:
{context}
"""


async def _build_context() -> str:
    """Build context string with projects, tasks, and recent failures."""
    parts = []

    try:
        from openclow.services import project_service
        projects = await project_service.get_all_projects(include_inactive=True)
        if projects:
            for p in projects:
                status = getattr(p, "status", "active")
                parts.append(f"Project: {p.name} ({p.tech_stack or 'N/A'}) — {status}")
        else:
            parts.append("No projects connected yet.")
    except Exception:
        parts.append("Projects: unable to fetch")

    try:
        from openclow.models import Task, async_session
        from sqlalchemy import select
        async with async_session() as session:
            # Active tasks
            result = await session.execute(
                select(Task)
                .where(Task.status.in_(["pending", "coding", "reviewing", "awaiting_approval"]))
                .limit(5)
            )
            active = result.scalars().all()
            if active:
                task_list = "; ".join(f"{t.status}: {t.description[:40]}" for t in active)
                parts.append(f"Active tasks: {task_list}")
            else:
                parts.append("No active tasks.")

            # Recent failed tasks (last 3)
            result = await session.execute(
                select(Task)
                .where(Task.status == "failed")
                .order_by(Task.created_at.desc())
                .limit(3)
            )
            failed = result.scalars().all()
            if failed:
                for t in failed:
                    err = t.error_message[:150] if t.error_message else "unknown error"
                    parts.append(f"Recent failure: {t.description[:40]} — {err}")
    except Exception:
        parts.append("Tasks: unable to fetch")

    # Recent audit logs for bootstrap failures
    try:
        from openclow.models import async_session
        from sqlalchemy import select, text
        async with async_session() as session:
            result = await session.execute(
                text("""
                    SELECT actor, command, exit_code, output_summary, created_at
                    FROM audit_logs
                    WHERE actor IN ('bootstrap', 'lifecycle') AND exit_code != 0
                    ORDER BY created_at DESC LIMIT 3
                """)
            )
            rows = result.fetchall()
            if rows:
                parts.append("\nRecent bootstrap/setup errors:")
                for r in rows:
                    cmd = (r[1] or "")[:60]
                    output = (r[3] or "")[:100]
                    parts.append(f"  [{r[0]}] {cmd} → exit {r[2]}: {output}")
    except Exception:
        pass

    return "\n".join(parts)


async def _get_groq_key() -> str:
    """Get Groq API key from DB first, then env fallback."""
    try:
        from openclow.services.config_service import get_config
        config = await get_config("stt", "provider")
        if config and config.get("api_key"):
            return config["api_key"]
    except Exception:
        pass
    from openclow.settings import settings
    return settings.groq_api_key


async def _chat_groq(user_message: str, context: str) -> str | None:
    """Fast chat via Groq Llama API (~1s). Returns response or None."""
    api_key = await _get_groq_key()
    if not api_key:
        return None

    system = CHAT_SYSTEM_PROMPT.format(context=context)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    "max_tokens": 300,
                    "temperature": 0.7,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                # Clean any markdown Llama might add
                text = text.replace("**", "").replace("```", "").replace("`", "")
                log.info("ai_chat.groq_ok", length=len(text), model="llama-3.3-70b")
                return text
            log.warning("ai_chat.groq_http_error", status=resp.status_code, body=resp.text[:200])
            return None
    except Exception as e:
        log.warning("ai_chat.groq_error", error=str(e))
        return None


async def _chat_claude_worker(user_message: str, chat_id: str, message_id: str, context: str) -> str | None:
    """Fallback: route chat through worker → Claude CLI (~8s)."""
    try:
        from openclow.worker.arq_app import get_arq_pool
        pool = await asyncio.wait_for(get_arq_pool(), timeout=5)
        job = await pool.enqueue_job("chat_response", user_message, chat_id, message_id, context)
        result = await job.result(timeout=30)
        return result if result else None
    except Exception as e:
        log.warning("ai_chat.claude_fallback_error", error=str(e))
        return None


async def get_chat_response(user_message: str, chat_id: str, message_id: str = "") -> str:
    """Get AI chat response. Groq Llama first (~1s), Claude CLI fallback (~8s)."""
    context = await _build_context()

    # 1. Groq Llama — fast, free
    result = await _chat_groq(user_message, context)
    if result:
        return result

    # 2. Claude CLI via worker — slower but more capable
    log.info("ai_chat.fallback_to_claude")
    result = await _chat_claude_worker(user_message, chat_id, message_id, context)
    if result:
        return result

    # 3. Static fallback
    return (
        "Hey! I'm OpenClow, your dev assistant.\n\n"
        "Tap the buttons below to get started."
    )
