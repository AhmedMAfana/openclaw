"""Coder Agent — writes code, runs tests, builds frontend."""
from typing import AsyncIterator

from openclow.models import Task
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

CODER_SYSTEM_PROMPT = """You are a senior developer working on "{project_name}" ({tech_stack}).

Project description: {description}
{agent_system_prompt}

RULES:
1. Read existing code first. Follow the project's patterns and conventions.
2. Create migrations with php artisan make:migration. Do NOT run php artisan migrate.
3. Install packages if needed (composer require, npm install).
4. Run npm run build to verify frontend compiles.
5. Run php artisan test to verify tests pass.
6. Stage all changes with git add.
7. Do NOT commit or push — the orchestrator handles that.
8. If tests fail, fix the issues and re-run tests.
"""

FIX_PROMPT = """Fix these review issues. Verify fixes, run tests, stage with git add.

{issues}"""

MCP_GIT_VERSION = "mcp-server-git==0.6.2"


async def run(workspace_path: str, task: Task) -> AsyncIterator:
    """Run the coder agent on the workspace."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    project = task.project
    system_prompt = CODER_SYSTEM_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        description=project.description or "",
        agent_system_prompt=project.agent_system_prompt or "",
    )

    from openclow.providers.llm.claude import _mcp_docker

    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt=system_prompt,
        model="claude-opus-4-6",
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
            "mcp__git__git_status",
            "mcp__git__git_diff_staged",
            "mcp__git__git_diff_unstaged",
            "mcp__git__git_add",
            "mcp__git__git_log",
            # Docker MCP tools — use instead of Bash
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__docker_exec",
            "mcp__docker__container_health",
            "mcp__docker__restart_container",
            "mcp__docker__compose_up",
            "mcp__docker__compose_ps",
        ],
        mcp_servers={
            "git": {
                "command": "uvx",
                "args": [MCP_GIT_VERSION, "--repository", workspace_path],
            },
            "docker": _mcp_docker(),
        },
        permission_mode="bypassPermissions",
        max_turns=settings.claude_coder_max_turns,
        setting_sources=["project"],
    )

    log.info("agent.coder.started", workspace=workspace_path, max_turns=settings.claude_coder_max_turns)

    async for message in query(prompt=task.description, options=options):
        yield message


async def run_fix(workspace_path: str, task: Task, issues: str) -> AsyncIterator:
    """Run the coder agent to fix reviewer issues. Minimal tools, Sonnet for speed."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    project = task.project

    from openclow.providers.llm.claude import _mcp_docker

    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt=(
            f'You are fixing code review issues in "{project.name}" ({project.tech_stack}).\n'
            f"Fix each issue, run tests via docker_exec MCP tool, stage changes with git add. Do NOT commit."
        ),
        model="claude-sonnet-4-6",  # Fixes are straightforward
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
            "mcp__git__git_status",
            "mcp__git__git_diff_staged",
            "mcp__git__git_add",
            # Docker MCP tools — use instead of Bash
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__docker_exec",
            "mcp__docker__container_health",
            "mcp__docker__restart_container",
        ],
        mcp_servers={
            "git": {
                "command": "uvx",
                "args": [MCP_GIT_VERSION, "--repository", workspace_path],
            },
            "docker": _mcp_docker(),
        },
        permission_mode="bypassPermissions",
        max_turns=10,
    )

    prompt = FIX_PROMPT.format(issues=issues)
    log.info("agent.coder.fixing", workspace=workspace_path)

    async for message in query(prompt=prompt, options=options):
        yield message
