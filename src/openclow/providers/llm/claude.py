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
    return {"command": "uvx", "args": [MCP_GIT_VERSION, "--repository", workspace_path]}


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
- App container: docker exec {app_container} [command]
- App URL: http://localhost:{app_port}

RULES:
1. Follow the plan step by step
2. After each step: STEP_DONE: [number] - [what you did]
3. Edit files directly, run tests, fix failures
4. Stage changes with git add — do NOT commit/push
5. After ALL steps output:
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
        async for message in query(prompt=task_description, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text

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
    ) -> AsyncIterator[Any]:
        from claude_agent_sdk import query, ClaudeAgentOptions

        # Project Docker info
        app_container = "app"
        app_port = 8000
        if agent_system_prompt and "APP_CONTAINER:" in agent_system_prompt:
            for line in agent_system_prompt.split("\n"):
                if line.startswith("APP_CONTAINER:"):
                    app_container = line.split(":", 1)[1].strip()
                elif line.startswith("APP_PORT:"):
                    app_port = int(line.split(":", 1)[1].strip())

        system_prompt = CODER_SYSTEM_PROMPT.format(
            project_name=project_name,
            tech_stack=tech_stack or "Unknown",
            description=description or "",
            agent_system_prompt=agent_system_prompt or "",
            plan=plan or "No specific plan — implement the task as you see fit.",
            app_container=app_container,
            app_port=app_port,
        )

        # Coder: Opus for complex implementation, full MCP suite
        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=system_prompt,
            model="claude-opus-4-6",
            allowed_tools=[
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                # Git: only the tools coder actually needs
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
            ],
            mcp_servers={
                "git": _mcp_git(workspace_path),
                "playwright": _mcp_playwright(),
                "docker": _mcp_docker(),
                # GitHub not needed during coding — saves one subprocess
            },
            permission_mode="bypassPermissions",
            max_turns=max_turns or self.coder_max_turns,
            setting_sources=["project"],
        )

        log.info("claude.coder.started", workspace=workspace_path)
        async for message in query(prompt=task_description, options=options):
            yield message

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
    ) -> AsyncIterator[Any]:
        """Fix reviewer issues. Minimal tools — no Playwright/Docker/GitHub needed."""
        from claude_agent_sdk import query, ClaudeAgentOptions

        # Fix agent: Sonnet is fast enough, minimal tools, fewer turns
        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=(
                f'You are fixing code review issues in "{project_name}" ({tech_stack}).\n'
                f"Fix each issue, run tests, stage changes with git add. Do NOT commit."
            ),
            model="claude-sonnet-4-6",  # Fixes are straightforward — Sonnet is faster
            allowed_tools=[
                "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                "mcp__git__git_status",
                "mcp__git__git_diff_staged",
                "mcp__git__git_add",
            ],
            mcp_servers={
                "git": _mcp_git(workspace_path),
                # No Playwright, Docker, GitHub — not needed for code fixes
            },
            permission_mode="bypassPermissions",
            max_turns=max_turns or 10,  # Fixes should be quick — 10 turns max
        )

        prompt = FIX_PROMPT.format(issues=issues)
        log.info("claude.coder.fixing", workspace=workspace_path)
        async for message in query(prompt=prompt, options=options):
            yield message

    async def run_reviewer(
        self,
        workspace_path: str,
        task_description: str,
        project_name: str,
        tech_stack: str,
        max_turns: int,
    ) -> ReviewResult:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        system_prompt = REVIEWER_SYSTEM_PROMPT.format(
            project_name=project_name,
            tech_stack=tech_stack or "Unknown",
        )

        # Reviewer: Sonnet for speed, read-only, git MCP only
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
        async for message in query(
            prompt=f"Review the changes made for: {task_description}",
            options=options,
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text

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
