"""Claude LLM Provider — uses Claude Agent SDK with Max subscription.

Performance-optimized:
- Pinned MCP server versions (no package resolution overhead)
- Explicit tool lists (no wildcards — saves context tokens)
- Model specialization (Sonnet for reviewer/fixes, Opus for coder)
- Effort tuning per agent role
- Minimal MCP servers per role (no unnecessary spawns)
"""
from typing import Any, AsyncIterator

from openclow.providers.base import LLMProvider, ReviewResult
from openclow.providers.registry import register_llm
from openclow.utils.logging import get_logger

log = get_logger()

# ---------------------------------------------------------------------------
# Pinned MCP server versions — eliminates package resolution on every spawn
# ---------------------------------------------------------------------------

MCP_GIT_VERSION = "mcp-server-git==0.6.2"
MCP_PLAYWRIGHT_VERSION = "@playwright/mcp@0.0.28"


def _mcp_git(workspace_path: str) -> dict:
    """Safe Git MCP — wraps git commands to never return non-zero exit codes."""
    return {"command": "python", "args": ["-m", "openclow.mcp_servers.git_mcp", workspace_path]}


def _mcp_playwright() -> dict:
    return {"command": "npx", "args": [MCP_PLAYWRIGHT_VERSION, "--headless"]}


def _mcp_docker() -> dict:
    return {"command": "python", "args": ["-m", "openclow.mcp_servers.docker_mcp"]}


def _mcp_github() -> dict:
    return {"command": "python", "args": ["-m", "openclow.mcp_servers.github_mcp"]}


# ---------------------------------------------------------------------------
# System prompts — front-loaded output format for faster model completion
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """OUTPUT FORMAT (use this exactly):
PLAN:
1. [step]
2. [step]
...
SUMMARY: [one sentence]
FILES: [comma-separated file paths]

---

You are analyzing "{project_name}" ({tech_stack}).
{description}
{agent_system_prompt}

Read the codebase. Create an implementation plan. Do NOT write code."""

CODER_SYSTEM_PROMPT = """You are implementing changes to "{project_name}" ({tech_stack}).
{description}
{agent_system_prompt}

PLAN TO FOLLOW:
{plan}

DOCKER ENVIRONMENT:
- App container: {app_container} (use docker_exec MCP tool to run commands inside it)
- Full container name: {app_container_full}
- Project compose name: openclow-{project_name}
- To run commands in the app: use docker_exec("{app_container_full}", "command")
- To check container status: use list_containers or container_health

BEFORE YOU START:
1. Check if the app container is healthy: container_health("{app_container_full}")
2. If the container is NOT responding or unhealthy:
   - Check logs: container_logs("{app_container_full}")
   - Diagnose and fix the issue (wrong paths, missing processes, config errors)
   - Restart if needed: restart_container("{app_container_full}")
   - Do NOT proceed with coding until the app is responding
3. Only after confirming the app is healthy, proceed with the plan

RULES:
1. Follow the plan step by step
2. After each step: STEP_DONE: [number] - [what you did]
3. Edit files directly, run tests, fix failures
4. Stage changes with git add — do NOT commit/push
5. NEVER run docker compose up/down — containers are managed by the bootstrap system
6. After coding, VERIFY: curl localhost in the container to confirm the app still works
7. If something breaks, FIX IT before finishing — do not leave broken state
8. After ALL steps output:
   DONE_SUMMARY:
   Files modified: [list]
   Tests: [pass/fail]
   Description: [what was done]"""

FIX_PROMPT = """Fix these review issues. Verify fixes, run tests, stage with git add.

{issues}"""

REVIEWER_SYSTEM_PROMPT = """OUTPUT FORMAT:
STATUS: APPROVED
or
STATUS: ISSUES
ISSUE 1: [file] - [problem and fix]

---

Review changes to "{project_name}" ({tech_stack}). Check:
- Security (injection, XSS, mass assignment)
- {tech_stack} best practices
- Error handling, edge cases
- Imports, migrations, tests

READ-ONLY. Do NOT modify files. For small diffs (<10 lines), review in 1-2 turns."""


class ClaudeAuthError(Exception):
    """Raised when Claude authentication fails."""
    pass


def _check_auth_error(error: Exception) -> None:
    """Check if error is auth-related and raise ClaudeAuthError if so."""
    error_str = str(error).lower()
    auth_keywords = [
        "auth", "unauthorized", "logged in", "credential", 
        "token expired", "not authenticated", "sign in",
        "login", "authentication failed", "invalid token"
    ]
    if any(kw in error_str for kw in auth_keywords):
        raise ClaudeAuthError("Claude authentication required. Please run 'claude login' or click Authenticate.") from error


@register_llm("claude")
class ClaudeProvider(LLMProvider):
    def __init__(self, config: dict):
        self.coder_max_turns = config.get("coder_max_turns", 50)
        self.reviewer_max_turns = config.get("reviewer_max_turns", 20)

    async def run_planner(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        description: str,
        agent_system_prompt: str,
    ) -> str:
        """Read the codebase and create an implementation plan. Returns plan text."""
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        system_prompt = PLANNER_SYSTEM_PROMPT.format(
            project_name=project_name,
            tech_stack=tech_stack or "Unknown",
            description=description or "",
            agent_system_prompt=agent_system_prompt or "",
        )

        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=system_prompt,
            model="claude-sonnet-4-6",  # Sonnet: fast enough for planning
            allowed_tools=["Read", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            max_turns=10,  # Plans shouldn't need 15 turns
            setting_sources=["project"],
        )

        log.info("claude.planner.started", workspace=workspace_path)
        full_output = ""
        try:
            async for message in query(prompt=task_description, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            full_output += block.text
        except Exception as e:
            _check_auth_error(e)
            raise

        log.info("claude.planner.done")
        return full_output

    async def run_coder(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        description: str,
        agent_system_prompt: str,
        max_turns: int,
        plan: str = "",
        on_tool_use: Any | None = None,
        app_container_name: str | None = None,
        app_port: int | None = None,
    ) -> AsyncIterator[Any]:
        from claude_agent_sdk import query, ClaudeAgentOptions

        # Project Docker info
        app_container = app_container_name or "app"
        app_port = app_port or 8000

        app_container_full = f"openclow-{project_name}-{app_container}-1" if app_container else ""
        system_prompt = CODER_SYSTEM_PROMPT.format(
            project_name=project_name,
            tech_stack=tech_stack or "Unknown",
            description=description or "",
            agent_system_prompt=agent_system_prompt or "",
            plan=plan or "No specific plan — implement the task as you see fit.",
            app_container=app_container,
            app_container_full=app_container_full,
            app_port=app_port,
        )

        # Coder: Opus for complex implementation, full MCP suite
        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=system_prompt,
            model="claude-opus-4-6",
            allowed_tools=[
                "Read", "Write", "Edit", "Glob", "Grep",
                # Git: safe wrapper that never crashes SDK
                "mcp__git__git_status",
                "mcp__git__git_diff_staged",
                "mcp__git__git_diff_unstaged",
                "mcp__git__git_add",
                "mcp__git__git_log",
                # Playwright: specific tools, not wildcard
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
                "mcp__playwright__browser_take_screenshot",
                "mcp__playwright__browser_click",
                "mcp__playwright__browser_fill_form",
                # Docker: specific tools
                "mcp__docker__list_containers",
                "mcp__docker__container_logs",
                "mcp__docker__docker_exec",
                "mcp__docker__container_health",
                "mcp__docker__restart_container",
                # Tunnel: manage public URLs
                "mcp__docker__tunnel_start",
                "mcp__docker__tunnel_stop",
                "mcp__docker__tunnel_get_url",
            ],
            mcp_servers={
                "git": _mcp_git(workspace_path),
                "docker": _mcp_docker(),
            },
            permission_mode="bypassPermissions",
            max_turns=max_turns or self.coder_max_turns,
            setting_sources=["project"],
        )

        log.info("claude.coder.started", workspace=workspace_path)
        try:
            async for message in query(prompt=task_description, options=options):
                yield message
        except Exception as e:
            _check_auth_error(e)
            raise

    async def run_coder_fix(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        description: str,
        agent_system_prompt: str,
        issues: str,
        max_turns: int,
        app_container_name: str | None = None,
        app_port: int | None = None,
    ) -> AsyncIterator[Any]:
        """Fix reviewer issues. Minimal tools — no Playwright/Docker/GitHub needed."""
        from claude_agent_sdk import query, ClaudeAgentOptions

        app_container=app_container_name or "app"
        app_container_full = f"openclow-{project_name}-{app_container}-1" if app_container else ""
        # Fix agent: Sonnet is fast enough, minimal tools, fewer turns
        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=(
                f'You are fixing code review issues in "{project_name}" ({tech_stack}).\n'
                f"{description or ''}\n\n"
                f"Project conventions:\n{agent_system_prompt or ''}\n\n"
                f"Docker environment:\n"
                f"- App container: {app_container} (docker_exec via MCP)\n"
                f"- Full container name: {app_container_full}\n"
                f"- Project compose name: openclow-{project_name}\n\n"
                f"Fix each issue, run tests, stage changes with git add. Do NOT commit."
            ),
            model="claude-sonnet-4-6",  # Fixes are straightforward — Sonnet is faster
            allowed_tools=[
                "Read", "Write", "Edit", "Glob", "Grep",
                # Git: safe wrapper
                "mcp__git__git_status",
                "mcp__git__git_diff_staged",
                "mcp__git__git_add",
                # Docker: run commands in containers
                "mcp__docker__list_containers",
                "mcp__docker__container_logs",
                "mcp__docker__docker_exec",
                "mcp__docker__container_health",
                "mcp__docker__restart_container",
            ],
            mcp_servers={
                "git": _mcp_git(workspace_path),
                "docker": _mcp_docker(),
            },
            permission_mode="bypassPermissions",
            max_turns=max_turns or 10,  # Fixes should be quick — 10 turns max
        )

        prompt = FIX_PROMPT.format(issues=issues)
        log.info("claude.coder.fixing", workspace=workspace_path)
        try:
            async for message in query(prompt=prompt, options=options):
                yield message
        except Exception as e:
            _check_auth_error(e)
            raise

    async def run_reviewer(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        max_turns: int,
        description: str = "",
        agent_system_prompt: str = "",
    ) -> ReviewResult:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        system_prompt = REVIEWER_SYSTEM_PROMPT.format(
            project_name=project_name,
            tech_stack=tech_stack or "Unknown",
            description=description or "",
            agent_system_prompt=agent_system_prompt or "",
        )

        # Reviewer: Sonnet for speed, read-only + git diff for review
        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=system_prompt,
            model="claude-sonnet-4-6",  # Review is pattern matching — Sonnet excels
            allowed_tools=[
                "Read", "Glob", "Grep",
                "mcp__git__git_diff_staged",
                "mcp__git__git_diff_unstaged",
                "mcp__git__git_log",
                "mcp__git__git_show",
                "mcp__git__git_status",
            ],
            mcp_servers={
                "git": _mcp_git(workspace_path),
            },
            permission_mode="bypassPermissions",
            max_turns=max_turns or self.reviewer_max_turns,
        )

        log.info("claude.reviewer.started", workspace=workspace_path)
        full_output = ""
        try:
            async for message in query(
                prompt=f"Review the changes made for: {task_description}",
                options=options,
            ):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            full_output += block.text
        except Exception as e:
            _check_auth_error(e)
            raise

        has_issues = "STATUS: ISSUES" in full_output
        issues = ""
        if has_issues:
            parts = full_output.split("STATUS: ISSUES", 1)
            if len(parts) > 1:
                issues = parts[1].strip()

        log.info("claude.reviewer.done", has_issues=has_issues)
        return ReviewResult(has_issues=has_issues, issues=issues, raw_output=full_output)

    def is_tool_use(self, message: Any) -> str | None:
        from claude_agent_sdk.types import AssistantMessage, ToolUseBlock
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    return block.name
        return None

    def is_result(self, message: Any) -> int | None:
        from claude_agent_sdk.types import ResultMessage
        if isinstance(message, ResultMessage):
            return getattr(message, "num_turns", 0)
        return None
