"""Chat AI task — agentic chat with MCP tools for project management.

The chat agent uses Claude Agent SDK with Actions MCP,
so it can actually execute commands: unlink projects, start Docker,
trigger bootstrap, etc. Not just talk — DO things.
"""
import asyncio
import json

from taghdev.utils.logging import get_logger

log = get_logger()

CHAT_SYSTEM_PROMPT = """You are THAG GROUP specialist's AI assistant. Senior DevOps and development expert.
You respond via Telegram chat and can EXECUTE actions on projects.

FORMATTING (CRITICAL — follow exactly):
- NO markdown whatsoever. No asterisks, no backticks, no code blocks.
- Plain text ONLY.
- Use emojis for visual clarity (one per section max).
- Keep replies under 80 words.
- Use line breaks for readability.
- Be warm, human, professional.

YOU CAN DO THINGS:
- Use list_projects to see connected projects
- Use unlink_project to disconnect a project (keeps data, stops Docker)
- Use remove_project to permanently delete a project
- Use relink_project to reconnect an unlinked project (runs full bootstrap)
- Use docker_up / docker_down to start/stop containers
- Use bootstrap to run full setup (clone, docker, health, tunnel, verify)
- Use trigger_task to create a development task
- Use trigger_addproject to onboard a new GitHub repo
- Use system_status to check system health

When the user asks to do something, USE THE TOOLS. Don't just tell them to tap buttons.
For example:
- "unlink trade-bot" → call unlink_project
- "start my project" → call docker_up or bootstrap
- "remove uk-post-map" → call remove_project
- "add this repo github.com/..." → call trigger_addproject

IMPORTANT: The chat_id for tool calls is: {chat_id}
The message_id for tool calls is: {message_id}

CONTEXT:
{context}
"""


async def chat_response(ctx: dict, user_message: str, chat_id: str, message_id: str, context: str) -> str:
    """Generate an AI chat response using Claude Agent SDK with Actions MCP."""
    system = CHAT_SYSTEM_PROMPT.format(context=context, chat_id=chat_id, message_id=message_id)
    prompt = f"User says: {user_message}"

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        options = ClaudeAgentOptions(
            system_prompt=system,
            model="claude-sonnet-4-6",  # Chat responses — Sonnet is fast + cheap
            allowed_tools=[
                "mcp__actions__list_projects",
                "mcp__actions__list_tasks",
                "mcp__actions__system_status",
                "mcp__actions__trigger_task",
                "mcp__actions__trigger_addproject",
                "mcp__actions__unlink_project",
                "mcp__actions__remove_project",
                "mcp__actions__docker_up",
                "mcp__actions__docker_down",
                "mcp__actions__bootstrap",
            ],
            mcp_servers={
                "actions": {
                    "command": "python",
                    "args": ["-m", "taghdev.mcp_servers.actions_mcp"],
                },
            },
            permission_mode="bypassPermissions",
            max_turns=5,
        )

        full_output = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text

        result = full_output.strip()
        if result:
            result = result.replace("**", "").replace("```", "").replace("`", "")
            log.info("chat_task.success", length=len(result), used_sdk=True)
            return result

    except ImportError:
        log.warning("chat_task.sdk_unavailable, falling back to CLI")
    except Exception as e:
        log.error("chat_task.sdk_failed", error=str(e))

    # Fallback: plain Claude CLI (no tools)
    try:
        fallback_system = system.split("YOU CAN DO THINGS:")[0] + "CONTEXT:\n" + context
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--system-prompt", fallback_system,
            "--output-format", "json",
            "--max-turns", "1",
            "--disallowedTools", "Bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return "Hey! I'm a bit slow right now. Try tapping one of the buttons below."

        if proc.returncode == 0:
            try:
                data = json.loads(stdout.decode())
            except json.JSONDecodeError:
                log.error("chat_task.json_parse_failed", stdout=stdout.decode()[:200])
                data = {}
            result = data.get("result", "").strip()
            if result:
                result = result.replace("**", "").replace("```", "").replace("`", "")
                log.info("chat_task.success", length=len(result), used_sdk=False)
                return result

    except Exception as e:
        log.error("chat_task.fallback_failed", error=str(e))

    return (
        "Hey! I'm THAG GROUP specialist, your dev assistant.\n\n"
        "Tap the buttons below to get started."
    )
