"""Reviewer Agent — reviews code changes for quality and security (read-only)."""
from dataclasses import dataclass
from typing import AsyncIterator

from taghdev.models import Task
from taghdev.settings import settings
from taghdev.utils.logging import get_logger

log = get_logger()

REVIEWER_SYSTEM_PROMPT = """You are a senior code reviewer for "{project_name}" ({tech_stack}).

{description}
{agent_system_prompt}

READ-ONLY. Do NOT modify any files.

## Review Workflow

1. git_diff_staged — read every line of the diff first
2. For each changed file: Read the full function/class context (not just the changed lines)
3. Grep for related code that may be affected but wasn't changed

## What to Check

### CRITICAL (blocks merge — must fix)
- SQL injection via raw query string interpolation
- XSS: unescaped user input rendered in templates
- Mass assignment without validation/allowlist
- New routes/endpoints not protected by the correct auth middleware
- Hardcoded secrets, API keys, or passwords in the diff
- Logic errors that will cause incorrect results or exceptions in normal use

### WARNING (should fix before merge)
- Missing database migration for a new column or table
- New config values absent from .env.example
- N+1 query pattern: querying per item in a loop over a collection
- Missing database index on a column used in WHERE/JOIN/ORDER BY
- Missing error handling in I/O operations (file reads, HTTP calls, DB queries)
- Plan steps that appear incomplete or unimplemented — compare diff against the original task
- Happy-path works but primary failure cases unhandled

### SUGGESTION (optional improvements)
- Code style inconsistency with the surrounding codebase
- Unnecessary complexity where a simpler approach exists
- Dead code or unused imports introduced by the diff

## Output Format

STATUS: APPROVED

Or:

STATUS: ISSUES
CRITICAL 1: [file:line] — [problem] — [exact fix required]
WARNING 1: [file:line] — [problem] — [recommended fix]
SUGGESTION 1: [file:line] — [optional improvement]

Rules:
- Be specific: "UserController.py:42 — no authorization check before user.delete()" not "missing auth"
- Omit categories where no issues are found
- SUGGESTION lines are informational only — the fixer won't act on them
"""


@dataclass
class ReviewResult:
    has_issues: bool
    issues: str
    raw_output: str
    has_blocking: bool = False  # True if CRITICAL or WARNING issues found (not just SUGGESTION)


async def run(workspace_path: str, task: Task) -> ReviewResult:
    """Run the reviewer agent. Returns ReviewResult."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    project = task.project
    system_prompt = REVIEWER_SYSTEM_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        description=project.description or "",
        agent_system_prompt=project.agent_system_prompt or "",
    )

    # Append tool inventory so LLM never needs ToolSearch
    system_prompt += """

## Available Tools (use directly — do NOT search for tools)

File tools: Read(file_path), Glob(pattern), Grep(pattern, path?)

Git tools (prefixed mcp__git__):
- git_diff_staged(repo_path) — show staged changes
- git_diff_unstaged(repo_path) — show unstaged changes
- git_log(repo_path) — show recent commits
- git_show(repo_path, revision) — show a specific commit
- git_status(repo_path) — show working tree status
"""

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

    # has_blocking: CRITICAL or WARNING found (not just SUGGESTION-only)
    # Used by the orchestrator to decide whether to run the fix loop
    has_blocking = has_issues and ("CRITICAL" in full_output or "WARNING" in full_output)

    log.info("agent.reviewer.done", has_issues=has_issues, has_blocking=has_blocking)
    return ReviewResult(has_issues=has_issues, issues=issues, raw_output=full_output, has_blocking=has_blocking)
