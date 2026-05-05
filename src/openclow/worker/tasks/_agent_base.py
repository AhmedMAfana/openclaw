"""Shared utilities for agentic LLM worker tasks — DRY base for all agents.

All agent code should use these helpers instead of duplicating tool description
logic, auth error detection, etc. See CLAUDE.md "Agentic Design" section.
"""
from __future__ import annotations

from claude_agent_sdk.types import ToolUseBlock


def describe_tool(block: ToolUseBlock) -> str:
    """Convert a ToolUseBlock into a human-readable one-line description.

    Used by agent_session, orchestrator, and _agent_helper to show live
    progress in Slack/Telegram as the agent works.
    """
    inp = block.input if isinstance(block.input, dict) else {}
    name = block.name

    # File operations
    if name == "Read":
        path = inp.get("file_path", "")
        return f"📖 Reading {path.split('/')[-1]}" if path else "📖 Reading file"
    if name == "Edit":
        path = inp.get("file_path", "")
        return f"✏️ Editing {path.split('/')[-1]}" if path else "✏️ Editing file"
    if name == "Write":
        path = inp.get("file_path", "")
        return f"📝 Writing {path.split('/')[-1]}" if path else "📝 Creating file"
    if name == "Bash":
        cmd = inp.get("command", "")[:50]
        return f"⚡ {cmd}" if cmd else "⚡ Running command"
    if name == "Grep":
        return f"🔍 Searching: {inp.get('pattern', '')[:30]}"
    if name == "Glob":
        return f"📁 Finding: {inp.get('pattern', '')[:30]}"

    # Docker MCP
    if "docker_exec" in name:
        return f"🐳 {inp.get('command', '')[:40]}"
    if "container_logs" in name:
        cn = inp.get("container_name", "")
        short = cn.split("-")[-2] if cn and "-" in cn else cn
        return f"📋 Logs: {short}"
    if "list_containers" in name:
        return "🐳 Checking containers"
    if "container_health" in name:
        return "💚 Health check"
    if "restart_container" in name:
        cn = inp.get("container_name", "")
        short = cn.split("-")[-2] if cn and "-" in cn else "container"
        return f"🔄 Restarting {short}"
    if "compose_up" in name:
        return "🐳 Starting Docker stack"
    if "compose_ps" in name:
        return "🐳 Checking stack status"
    if "tunnel" in name:
        action = name.split("__")[-1].replace("tunnel_", "")
        return f"🌐 Tunnel: {action}"

    # Playwright / Browser
    if "playwright" in name or "browser" in name:
        action = name.split("__")[-1].replace("browser_", "")
        if "navigate" in name:
            url = inp.get("url", "")
            return f"🌐 Opening {url[:40]}" if url else "🌐 Navigating"
        if "screenshot" in name:
            return "📸 Taking screenshot"
        if "click" in name:
            return f"👆 Clicking: {inp.get('element', '')[:30]}"
        if "fill" in name or "type" in name:
            return "⌨️ Typing in form"
        if "snapshot" in name:
            return "🔍 Reading page content"
        return f"🌐 Browser: {action}"

    # Git MCP
    if "git_" in name:
        action = name.split("__")[-1].replace("git_", "")
        return f"📦 Git: {action}"

    # Host MCP (mode="host" projects — apps already running on the VPS host)
    if "host_git_clone" in name:
        return "📥 Cloning repo"
    if "host_git_pull" in name:
        return "📥 git pull"
    if "host_read_install_guide" in name:
        return "📖 Reading install guide"
    if "host_run_command" in name:
        cmd = inp.get("command", "")[:50]
        return f"🖥 {cmd}" if cmd else "🖥 Running command"
    if "host_check_port" in name:
        return f"🔌 Port {inp.get('port', '?')}"
    if "host_curl" in name:
        url = inp.get("url", "")[:40]
        return f"🌐 curl {url}" if url else "🌐 HTTP check"
    if "host_process_status" in name:
        return f"🔎 Process: {inp.get('match', '')[:30]}"
    if "host_tail_log" in name:
        p = inp.get("path", "")
        return f"📜 tail {p.split('/')[-1]}"
    if "host_start_app" in name:
        return "▶️ Starting app"
    if "host_stop_app" in name:
        return "⏹ Stopping app"
    if "host_service_status" in name:
        return f"💡 {inp.get('unit', '')[:30]}"
    if "host_cd" in name:
        return "📂 Entering project dir"

    # Instance / task dispatch tools
    if "remote_trigger" in name or name == "RemoteTrigger":
        return "🚀 Queueing task..."
    if "trigger_task" in name:
        return "🚀 Dispatching task..."
    if name == "Agent":
        return "🤖 Running sub-agent"
    if name == "Skill":
        return "🛠 Invoking skill"
    if "ListMcpResources" in name or "list_resources" in name:
        return "📋 Checking available tools"
    if "get_status" in name or "instance_status" in name:
        return "📊 Checking status"
    if "list_" in name or name.startswith("List"):
        return "📋 Listing resources"
    if "Task" in name and "create" in name.lower():
        return "📝 Creating task"

    # Fallback — strip MCP namespace noise for readability
    readable = name.replace("mcp__", "").replace("__", ": ")
    # CamelCase → spaced words (e.g. ListMcpResourcesTool → List Mcp Resources Tool)
    import re as _re
    readable = _re.sub(r"(?<=[a-z])(?=[A-Z])", " ", readable)
    return f"🔧 {readable}"


# Phrases that unambiguously indicate Claude auth failure. Bare "auth" was
# too loose — it false-positives on debug-to-stderr lines like
# "[API:auth] OAuth token check complete" or "has Authorization header: false"
# that show up whenever stderr is appended to an exception message.
_AUTH_PHRASES = [
    "claudeautherror",
    "401 unauthorized",
    "403 forbidden",
    "authentication required",
    "authentication failed",
    "not authenticated",
    "oauth token expired",
    "token expired",
    "invalid token",
    "credentials missing",
    "please run 'claude login'",
    "please log in",
    "you must be logged in",
]


def is_auth_error(error: Exception) -> bool:
    """Check if an exception is genuinely a Claude authentication error.

    Whitelist specific phrases — substring 'auth' triggers false positives
    on debug logs that happen to mention OAuth, Authorization headers, etc.
    """
    s = str(error).lower()
    return any(p in s for p in _AUTH_PHRASES)
