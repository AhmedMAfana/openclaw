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

    # Fallback
    return f"🔧 {name.replace('mcp__', '').replace('__', ': ')}"


_AUTH_KEYWORDS = [
    "auth", "unauthorized", "logged in", "credential",
    "token expired", "not authenticated", "sign in",
    "login", "authentication failed", "invalid token",
]


def is_auth_error(error: Exception) -> bool:
    """Check if an exception is a Claude authentication error."""
    return any(kw in str(error).lower() for kw in _AUTH_KEYWORDS)
