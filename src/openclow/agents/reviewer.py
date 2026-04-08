"""Reviewer Agent — reviews code changes for quality and security (read-only)."""
from dataclasses import dataclass
from typing import AsyncIterator

from openclow.models import Task
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

REVIEWER_SYSTEM_PROMPT = """You are a senior code reviewer for "{project_name}" ({tech_stack}).

Review the changes made for the task described below. Check for:
1. Security: SQL injection, XSS, CSRF, mass assignment vulnerabilities
2. Laravel best practices: proper validation, middleware, form requests
3. Vue best practices: reactivity, component structure, prop validation
4. Missing error handling or edge cases
5. Broken imports or unused code
6. Code style consistency with existing codebase
7. Missing or incorrect migrations
8. Test coverage — are the changes tested?

IMPORTANT: You are READ-ONLY. Do NOT modify any files. Only analyze and report.

Output your review in this EXACT format:

STATUS: APPROVED
(if everything looks good)

OR

STATUS: ISSUES
ISSUE 1: [file path] - [description of the problem and how to fix it]
ISSUE 2: [file path] - [description of the problem and how to fix it]
...
"""


@dataclass
class ReviewResult:
    has_issues: bool
    issues: str
    raw_output: str


async def run(workspace_path: str, task: Task) -> ReviewResult:
    """Run the reviewer agent. Returns ReviewResult."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    project = task.project
    system_prompt = REVIEWER_SYSTEM_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
    )

    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt=system_prompt,
        model="claude-sonnet-4-6",  # Review is pattern matching — Sonnet is faster
        allowed_tools=[
            "Read", "Glob", "Grep",
            "mcp__git__git_diff_staged",
            "mcp__git__git_diff_unstaged",
            "mcp__git__git_log",
            "mcp__git__git_show",
            "mcp__git__git_status",
        ],
        mcp_servers={
            "git": {
                "command": "uvx",
                "args": ["mcp-server-git==0.6.2", "--repository", workspace_path],
            },
        },
        permission_mode="bypassPermissions",
        max_turns=settings.claude_reviewer_max_turns,
    )

    log.info("agent.reviewer.started", workspace=workspace_path)

    full_output = ""
    async for message in query(
        prompt=f"Review the changes made for: {task.description}",
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    full_output += block.text

    # Parse the review result
    has_issues = "STATUS: ISSUES" in full_output
    issues = ""
    if has_issues:
        # Extract everything after "STATUS: ISSUES"
        parts = full_output.split("STATUS: ISSUES", 1)
        if len(parts) > 1:
            issues = parts[1].strip()

    log.info("agent.reviewer.done", has_issues=has_issues)
    return ReviewResult(has_issues=has_issues, issues=issues, raw_output=full_output)
