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


def _mcp_host() -> dict:
    return {"command": "python", "args": ["-m", "openclow.mcp_servers.host_mcp"]}


def _coder_env_spec(
    mode: str,
    workspace_path: str,
    project_dir: str | None,
    project_name: str,
    app_container: str,
    app_port: int,
) -> tuple[list[str], dict, str]:
    """Return (allowed_tools, mcp_servers, env_hint) for the coder/fix agents,
    switching between Docker- and host-mode project runtimes. `env_hint` is a
    short text block that the system prompt appends to tell the model which
    environment it is operating in."""
    common_tools = [
        "Read", "Write", "Edit", "Glob", "Grep",
        "mcp__git__git_status",
        "mcp__git__git_diff_staged",
        "mcp__git__git_diff_unstaged",
        "mcp__git__git_add",
        "mcp__git__git_log",
    ]
    if mode == "host":
        tools = common_tools + [
            "mcp__host__host_cd",
            "mcp__host__host_git_pull",
            "mcp__host__host_read_install_guide",
            "mcp__host__host_run_command",
            "mcp__host__host_check_port",
            "mcp__host__host_curl",
            "mcp__host__host_process_status",
            "mcp__host__host_tail_log",
            "mcp__host__host_start_app",
            "mcp__host__host_stop_app",
            "mcp__host__host_service_status",
        ]
        mcp_servers = {
            "git": _mcp_git(workspace_path),
            "host": _mcp_host(),
        }
        env_hint = (
            f"HOST ENVIRONMENT (mode=host):\n"
            f"- Project lives at: {project_dir or '(not set)'}\n"
            f"- Project files are mounted into this container at the same path —\n"
            f"  use Read/Edit/Write/Glob/Grep DIRECTLY on files under that path.\n"
            f"  Reserve mcp__host__host_run_command for shell side-effects only\n"
            f"  (npm run build, php artisan ..., composer install).\n"
            f"- App port: {app_port}\n"
            f"- Check HTTP with mcp__host__host_curl(url)\n"
            f"- Tail logs with mcp__host__host_tail_log(path)\n"
            f"- Never use docker_exec — there is no container for this app.\n"
        )
        return tools, mcp_servers, env_hint

    # Docker mode (default)
    app_container_full = f"openclow-{project_name}-{app_container}-1"
    tools = common_tools + [
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_snapshot",
        "mcp__playwright__browser_take_screenshot",
        "mcp__playwright__browser_click",
        "mcp__playwright__browser_fill_form",
        "mcp__docker__list_containers",
        "mcp__docker__container_logs",
        "mcp__docker__docker_exec",
        "mcp__docker__container_health",
        "mcp__docker__restart_container",
        "mcp__docker__tunnel_start",
        "mcp__docker__tunnel_stop",
        "mcp__docker__tunnel_get_url",
    ]
    mcp_servers = {
        "git": _mcp_git(workspace_path),
        "docker": _mcp_docker(),
    }
    env_hint = (
        f"DOCKER ENVIRONMENT:\n"
        f"- App container: {app_container} (docker_exec via MCP)\n"
        f"- Full container: {app_container_full}\n"
        f"- Project compose name: openclow-{project_name}\n"
        f"- App port: {app_port}\n"
    )
    return tools, mcp_servers, env_hint


def _mcp_actions() -> dict:
    return {"command": "python", "args": ["-m", "openclow.mcp_servers.actions_mcp"]}


def _mcp_github() -> dict:
    return {"command": "python", "args": ["-m", "openclow.mcp_servers.github_mcp"]}


# ---------------------------------------------------------------------------
# System prompts — front-loaded output format for faster model completion
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """You are a senior developer ANALYZING (not implementing) "{project_name}" ({tech_stack}).

{description}
{agent_system_prompt}

## CRITICAL — read this twice

You have READ-ONLY tools (Read, Glob, Grep). You CANNOT write files, run shell
commands, build, or deploy. You are producing a PROPOSAL the user will review
and Approve before any coding starts.

NEVER write the plan as if work is done. Forbidden words/symbols:
- "shipped", "added", "added the", "implemented", "compiled", "cleared",
  "verified", "fixed", "merged", "pushed", "✓", "✅", "DONE", "completed"
Required tense: future or imperative — "will add", "should create", "create a
new route in routes/api.php that ..."

## Be fast — stop exploring when you have enough

Make AT MOST 5 tool calls. The goal is a plan, not a code review.
Read one example of a similar feature, scan routes once, then plan. Do NOT
recursively grep the whole codebase.

## Output Format

Use Markdown. Exactly this structure, no extras:

## Approach
[1–2 sentences on the strategy you propose, in future tense.]

**Complexity:** [LOW / MEDIUM / HIGH] — [why]

## Plan (proposed steps — nothing has been done yet)
1. [Future tense — "Add ...", "Create ...", "Modify ...". Name the exact file.]
2. ...
[Maximum 8 steps; group related changes if needed.]

## Risks
- **Migration needed:** yes/no
- **New env vars:** list or "none"
- **Post-deploy actions:** what the orchestrator will need to run after coder finishes
- **Breaking changes:** yes/no

## Files to touch
`path/one.php`, `path/two.vue`, ...

## Rules

- Do NOT write code or invent code blocks "for clarity"
- Do NOT pretend any of these steps are already done
- If a similar feature exists, point to it in Approach and reuse the pattern
- Stop and submit your plan as soon as you can answer the question — extra
  exploration delays the user
"""

CODER_SYSTEM_PROMPT = """You are a senior developer implementing changes to "{project_name}" ({tech_stack}).

{description}
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
- Search for any existing implementation of what you're about to write — reuse/extend it

## Implementation Rules

1. Follow the plan step by step, in order
2. After each step, output on its own line:
   STEP_DONE: [step number] - [what you changed and in which file(s)]
3. After each file edit: Read the file to confirm the change applied correctly
4. After all steps: run tests using the project's existing test command
5. If tests fail: fix the failure before outputting DONE_SUMMARY
6. Stage all changed files: git add [specific files] — never git add .
7. Do NOT commit or push

## DO NOT run frontend builds

Do NOT run `npm run build`, `npx vite build`, or any frontend asset compilation.
The orchestrator runs the build automatically after you finish coding.
Focus only on editing source files and verifying they are correct.

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


def _make_stderr_collector(label: str) -> tuple[list[str], Any]:
    """Return (buffer, callback) pair for collecting Claude SDK subprocess
    stderr. The SDK normally drops stderr unless we set this callback —
    without it, exit-code-1 crashes show 'Check stderr output for details'
    with no detail. We keep the last 80 lines and log each at WARNING.
    """
    from collections import deque
    buf: deque[str] = deque(maxlen=80)
    def _cb(line: str) -> None:
        buf.append(line)
        log.warning("claude.stderr", agent=label, line=line[:500])
    # Return the deque cast to list so callers can read the buffer at any time.
    # Pyright/mypy may complain about variance but it works at runtime.
    return buf, _cb  # type: ignore[return-value]


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

        stderr_buf, stderr_cb = _make_stderr_collector("planner")
        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=system_prompt,
            model="claude-sonnet-4-6",  # Sonnet: fast enough for planning
            allowed_tools=["Read", "Glob", "Grep"],
            permission_mode="bypassPermissions",
            max_turns=5,  # Tight cap — plans should be a few targeted reads, not exhaustive archaeology
            setting_sources=["project"],
            stderr=stderr_cb,
            extra_args={"debug-to-stderr": None},
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
            tail = "\n".join(list(stderr_buf)[-20:])
            if tail:
                raise RuntimeError(f"planner crashed: {e}\n--- last stderr ---\n{tail}") from e
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
        mode: str = "docker",
        project_dir: str | None = None,
    ) -> AsyncIterator[Any]:
        from claude_agent_sdk import query, ClaudeAgentOptions

        # Project runtime info (Docker container OR host process)
        app_container = app_container_name or "app"
        app_port = app_port or 8000

        app_container_full = f"openclow-{project_name}-{app_container}-1" if app_container else ""
        tools, mcp_servers, env_hint = _coder_env_spec(
            mode, workspace_path, project_dir, project_name, app_container, app_port,
        )
        system_prompt = CODER_SYSTEM_PROMPT.format(
            project_name=project_name,
            tech_stack=tech_stack or "Unknown",
            description=description or "",
            agent_system_prompt=agent_system_prompt or "",
            plan=plan or "No specific plan — implement the task as you see fit.",
            app_container=app_container,
            app_container_full=app_container_full,
            app_port=app_port,
        ) + "\n\n" + env_hint

        # Coder: Opus for complex implementation, full MCP suite
        stderr_buf, stderr_cb = _make_stderr_collector("coder")
        options = ClaudeAgentOptions(
            cwd=project_dir if mode == "host" and project_dir else workspace_path,
            system_prompt=system_prompt,
            model="claude-opus-4-6",
            allowed_tools=tools,
            mcp_servers=mcp_servers,
            permission_mode="bypassPermissions",
            max_turns=max_turns or self.coder_max_turns,
            setting_sources=["project"],
            include_partial_messages=True,
            stderr=stderr_cb,
            extra_args={"debug-to-stderr": None},  # so silent-exit crashes still leave a trail
        )

        log.info("claude.coder.started", workspace=workspace_path)
        try:
            async for message in query(prompt=task_description, options=options):
                yield message
        except Exception as e:
            _check_auth_error(e)
            tail = "\n".join(list(stderr_buf)[-20:])
            if tail:
                raise RuntimeError(f"coder crashed: {e}\n--- last stderr ---\n{tail}") from e
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
        mode: str = "docker",
        project_dir: str | None = None,
    ) -> AsyncIterator[Any]:
        """Fix reviewer issues. Minimal tools — no Playwright/Docker/GitHub needed."""
        from claude_agent_sdk import query, ClaudeAgentOptions

        app_container = app_container_name or "app"
        app_port = app_port or 8000
        tools, mcp_servers, env_hint = _coder_env_spec(
            mode, workspace_path, project_dir, project_name, app_container, app_port,
        )
        # Fix agent: Sonnet is fast enough, fewer turns
        stderr_buf, stderr_cb = _make_stderr_collector("coder_fix")
        options = ClaudeAgentOptions(
            cwd=project_dir if mode == "host" and project_dir else workspace_path,
            system_prompt=(
                f'You are fixing code review issues in "{project_name}" ({tech_stack}).\n'
                f"{description or ''}\n\n"
                f"Project conventions:\n{agent_system_prompt or ''}\n\n"
                f"{env_hint}\n"
                f"Fix each issue, run tests, stage changes with git add. Do NOT commit."
            ),
            model="claude-sonnet-4-6",
            allowed_tools=tools,
            mcp_servers=mcp_servers,
            permission_mode="bypassPermissions",
            max_turns=max_turns or 10,
            include_partial_messages=True,
            stderr=stderr_cb,
            extra_args={"debug-to-stderr": None},  # so silent-exit crashes still leave a trail
        )

        prompt = FIX_PROMPT.format(issues=issues)
        log.info("claude.coder.fixing", workspace=workspace_path)
        try:
            async for message in query(prompt=prompt, options=options):
                yield message
        except Exception as e:
            _check_auth_error(e)
            tail = "\n".join(list(stderr_buf)[-20:])
            if tail:
                raise RuntimeError(f"coder_fix crashed: {e}\n--- last stderr ---\n{tail}") from e
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
        on_stream: "Callable | None" = None,
    ) -> AsyncIterator[Any]:
        """Run reviewer agent — yields SDK messages for streaming.

        The orchestrator collects output via _run_agent_with_streaming and
        parses ReviewResult from the collected text.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions

        system_prompt = REVIEWER_SYSTEM_PROMPT.format(
            project_name=project_name,
            tech_stack=tech_stack or "Unknown",
            description=description or "",
            agent_system_prompt=agent_system_prompt or "",
        )

        # Reviewer: Sonnet for speed, read-only + git diff for review
        stderr_buf, stderr_cb = _make_stderr_collector("reviewer")
        options = ClaudeAgentOptions(
            cwd=workspace_path,
            system_prompt=system_prompt,
            model="claude-sonnet-4-6",
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
            include_partial_messages=True,
            stderr=stderr_cb,
            extra_args={"debug-to-stderr": None},  # so silent-exit crashes still leave a trail
        )

        log.info("claude.reviewer.started", workspace=workspace_path)
        try:
            async for message in query(
                prompt=f"Review the changes made for: {task_description}",
                options=options,
            ):
                yield message
        except Exception as e:
            _check_auth_error(e)
            tail = "\n".join(list(stderr_buf)[-20:])
            if tail:
                raise RuntimeError(f"reviewer crashed: {e}\n--- last stderr ---\n{tail}") from e
            raise

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
