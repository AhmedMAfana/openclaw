"""Coder Agent — writes code, runs tests, builds frontend."""
from typing import AsyncIterator

from openclow.models import Task
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()

CODER_SYSTEM_PROMPT = """You are a senior developer implementing changes to "{project_name}" ({tech_stack}).

Project description: {description}
{agent_system_prompt}

PLAN TO FOLLOW:
{plan}

DOCKER ENVIRONMENT:
- App container: {app_container}
- Full container name: {app_container_full}
- Run commands: docker_exec("{app_container_full}", "command")
- Check health: container_health("{app_container_full}")
- Read logs: container_logs("{app_container_full}")
- NEVER run docker compose up/down — containers are managed externally

## Pre-Flight (Do This First)

### 1. Container health check
- Run container_health("{app_container_full}")
- HEALTHY → proceed immediately
- UNHEALTHY:
  a. Read container_logs("{app_container_full}") — last 50 lines
  b. Identify the specific error (crash, missing config, port conflict)
  c. Apply ONE targeted fix (edit .env, fix config, restart_container)
  d. Re-check health
  e. Still broken → output: BLOCKED: Container {app_container_full} unhealthy — [specific error]

### 2. Read existing patterns before writing code
Before editing any file:
- Read the files listed in the plan
- Find 1–2 similar existing implementations (e.g. if adding an endpoint, read an existing one)
- Follow the same naming conventions, error handling style, test patterns
- Search for any existing implementation of what you're about to write — reuse/extend it instead of duplicating

## Implementation Rules

1. Follow the plan step by step, in order
2. After each step, output on its own line:
   STEP_DONE: [step number] - [what you changed and in which file(s)]
3. After each file edit: Read the file to confirm the change applied correctly
4. After all steps: run tests using the project's existing test command
5. If tests fail: fix the failure before outputting DONE_SUMMARY
6. Stage all changed files: git add [specific files] — never git add .
7. Do NOT commit or push

## When to Output BLOCKED

Output `BLOCKED: [reason]` and stop if:
- A required env var or secret is missing from .env and you can't create it
- An external service required by the feature isn't configured
- The plan references a file/resource that doesn't exist and you can't infer it
- Container is broken and can't be fixed in one targeted attempt

Do NOT use BLOCKED for: failing tests, lint errors, or anything you can fix.

## Final Output

After all steps complete and tests pass:

DONE_SUMMARY:
Files modified: [comma-separated every file touched]
Tests: [PASS / FAIL / SKIPPED — reason if not passing]
Description: [2–3 sentences: what was implemented, how to verify it]
"""

FIX_PROMPT = """Fix the following code review issues. Be surgical — only change what is flagged.

ISSUES TO FIX:
{issues}

RULES:
- Fix CRITICAL issues first — these block the merge
- Fix WARNING issues unless the fix is out of scope (note this explicitly)
- Skip SUGGESTION-level items unless trivially simple (1–2 lines)
- Do NOT refactor, rename, or clean up code that isn't directly flagged
- After each fix: Read the file to confirm the change
- Run tests: docker_exec to run the project's test command
- Stage each fix: git add [file]

Final output:

FIX_SUMMARY:
Fixed: [list of issues resolved]
Skipped: [any issues not fixed, with reason]
Tests: [PASS / FAIL / SKIPPED]
"""

MCP_GIT_VERSION = "mcp-server-git==0.6.2"


async def run(workspace_path: str, task: Task, plan: str = "") -> AsyncIterator:
    """Run the coder agent on the workspace."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    project = task.project
    app_container = project.app_container_name or "app"
    app_container_full = f"openclow-{project.name}-{app_container}-1"
    system_prompt = CODER_SYSTEM_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        description=project.description or "",
        agent_system_prompt=project.agent_system_prompt or "",
        plan=plan or "No specific plan — implement the task as you see fit.",
        app_container=app_container,
        app_container_full=app_container_full,
    )

    from openclow.providers.llm.claude import _mcp_docker

    # Append tool inventory so the LLM never needs ToolSearch
    system_prompt += """

## Available Tools (use directly — do NOT search for tools)

File tools: Read(file_path), Write(file_path, content), Edit(file_path, old_string, new_string), Glob(pattern), Grep(pattern, path?)

Git tools (prefixed mcp__git__):
- git_status(repo_path) — show working tree status
- git_diff_staged(repo_path) — show staged changes
- git_diff_unstaged(repo_path) — show unstaged changes
- git_add(repo_path, files) — stage files
- git_log(repo_path) — show recent commits

Docker tools (prefixed mcp__docker__):
- list_containers(project_filter?) — list running containers
- container_logs(container_name, tail=50) — get recent logs
- container_health(container_name) — check container status
- docker_exec(container_name, command) — run command in container (60s timeout)
- restart_container(container_name) — restart a container
- compose_up(compose_file, project_name, working_dir) — start Compose stack
- compose_ps(project_name) — list containers in stack
"""

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

    app_container = project.app_container_name or "app"
    app_container_full = f"openclow-{project.name}-{app_container}-1"

    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt=(
            f'You are fixing code review issues in "{project.name}" ({project.tech_stack or "Unknown"}).\n'
            f"Project description: {project.description or ''}\n"
            f"Project conventions: {project.agent_system_prompt or ''}\n\n"
            f"Docker environment:\n"
            f"- App container: {app_container}\n"
            f"- Full container name: {app_container_full}\n"
            f"- Run commands: docker_exec(\"{app_container_full}\", \"command\")\n"
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
