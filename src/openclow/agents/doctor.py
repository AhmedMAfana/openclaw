"""Doctor Agent — the agentic repair engine.

This is NOT a one-shot diagnostic. It's a multi-strategy repair loop:
1. Read container logs, compose output, system state
2. Claude diagnoses the root cause
3. Claude attempts a fix (edit Dockerfile, requirements, config, etc.)
4. Verify the fix worked
5. If not → try a different approach
6. Report EVERY step via callback
7. Give up with clear explanation if all strategies exhausted

Used by both bootstrap.py and health_task.py.
"""
import asyncio
import json
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from openclow.utils.logging import get_logger

log = get_logger()

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RepairStep:
    """One step in a repair attempt."""
    action: str          # what was done
    result: str          # outcome
    success: bool        # did it work?


@dataclass
class RepairReport:
    """Full report of a repair session."""
    container: str
    original_error: str
    steps: list[RepairStep] = field(default_factory=list)
    fixed: bool = False
    final_status: str = ""
    suggestion: str = ""   # if not fixed, what user should do


# Type for the progress callback: async fn(icon, message)
ProgressCallback = Callable[[str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

async def _run(*args: str, cwd: str | None = None, timeout: int = 60,
               actor: str = "doctor", project_name: str | None = None) -> tuple[int, str]:
    """Run a command, return (returncode, output). Audits all executions."""
    import os
    from openclow.services.audit_service import log_action

    cmd_str = " ".join(args)

    # Route Docker commands through the guard
    if args and args[0] == "docker":
        from openclow.services.docker_guard import run_docker
        return await run_docker(
            *args, actor=actor, project_name=project_name,
            cwd=cwd, timeout=timeout,
        )

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        combined = (stdout.decode() + stderr.decode()).strip()
        rc = proc.returncode
        output = combined[-4000:]
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        rc, output = -1, f"TIMEOUT after {timeout}s"
    except Exception as e:
        rc, output = -1, str(e)

    # Audit non-Docker commands too
    await log_action(
        actor=actor, action="bash", command=cmd_str,
        workspace=cwd, project_name=project_name,
        exit_code=rc, output_summary=output[:2000],
    )

    return rc, output


async def _get_logs(container: str, tail: int = 50) -> str:
    """Get recent container logs."""
    rc, output = await _run("docker", "logs", container, "--tail", str(tail))
    return output


async def _get_inspect(container: str) -> dict:
    """Get container inspect data."""
    rc, output = await _run("docker", "inspect", container)
    if rc != 0:
        return {}
    try:
        data = json.loads(output)
        return data[0] if data else {}
    except (json.JSONDecodeError, IndexError):
        return {}


async def _is_container_healthy(container: str) -> bool:
    """Check if a container is running and healthy."""
    rc, output = await _run("docker", "inspect", "--format",
                            "{{.State.Status}}:{{.State.Health.Status}}",
                            container)
    if rc != 0:
        return False
    parts = output.strip().split(":")
    status = parts[0] if parts else ""
    health = parts[1] if len(parts) > 1 else ""
    # Running with no health check = OK. Running + healthy = OK.
    return status == "running" and health in ("healthy", "", "<no value>")


# ---------------------------------------------------------------------------
# Claude agent for diagnosis + fix
# ---------------------------------------------------------------------------

DIAGNOSE_PROMPT = """You are a DevOps repair specialist. A Docker container is failing and needs fixing.

Container: {container}
Status: {status} (exit code: {exit_code})
Service: {service_name}
Workspace: {workspace}
Compose file: {compose_file}
Compose project: {compose_project}

Image: {image}
Command: {command}
Working dir: {workdir}
Env vars present (non-secret): {env_summary}

Recent logs (last {log_lines} lines):
```
{logs}
```

{extra_context}

## Task

1. DIAGNOSE: Identify the specific root cause from the logs. Be precise — not "config issue" but "PHP extension `redis` not installed".

2. FIX: Edit workspace files (Dockerfile, requirements.txt, .env, config files, etc.)

3. REBUILD: compose_up(project={compose_project}, compose_file={compose_file}, service={service_name}, build=True)

4. VERIFY: Wait 8s, then container_health("{container}") to confirm recovery

## Fix Strategy by Error Type

- "Module not found" / missing import → check package install, run pip/composer/npm inside container
- "Connection refused" on DB/Redis → dependency container may not be ready; add healthcheck dependency or wait loop
- "Permission denied" → fix ownership via docker_exec chown, or correct the path
- "exec format error" → wrong architecture base image; add platform: linux/amd64
- Package install failure → pin to a working version or remove if non-critical
- Port already allocated → find conflicting container with list_containers, stop it first

End with:
FIXED: [what you changed and why it worked]
or
UNFIXABLE: [specific reason] — User must: [exact action the user needs to take]
"""

RETRY_PROMPT = """Previous repair attempt did not work. The container is still failing.

Container: {container}
Workspace: {workspace}
Compose file: {compose_file}
Compose project: {compose_project}
Service: {service_name}

## Previous Attempts — DO NOT REPEAT THESE

{previous_attempts_with_errors}

## Current Logs (after last fix attempt)

```
{new_logs}
```

## Instructions

1. Read the CURRENT logs — the error may have changed after the last fix
2. Do NOT try any approach listed in Previous Attempts
3. Identify a different root cause or a completely different fix strategy

Alternative approaches when initial fix fails:
- Fixed a missing package but still fails → the real issue may be config, permissions, or an env var
- Fixed config but still fails → verify the process is actually reading that config file
- Rebuild keeps failing on same step → try a different base image or remove the failing dependency

End with FIXED: or UNFIXABLE: [reason] — User must: [exact action]
"""


async def _run_claude_diagnosis(
    prompt: str,
    workspace: str,
    max_turns: int = 12,
    on_progress: ProgressCallback | None = None,
) -> str:
    """Run Claude agent SDK for diagnosis. Returns the full text output.

    Streams live progress to on_progress callback so the user sees
    what the Doctor is doing in real-time.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock, ResultBlock

        from openclow.providers.llm.claude import _mcp_docker

        options = ClaudeAgentOptions(
            cwd=workspace,
            system_prompt=(
                "You are a DevOps repair agent. Fix Docker container issues. "
                "Use the docker MCP tools (docker_exec, container_logs, etc.) instead of Bash. "
                "Be precise and surgical — fix only what's broken."
                " NEVER run 'curl --unix-socket /run/docker.sock' or any raw Docker API call via docker_exec inside a project container — the socket is NOT mounted there and will hang forever. Use ONLY MCP tools for all Docker operations."
            ),
            model="claude-sonnet-4-6",  # Error diagnosis is procedural — Sonnet is faster
            allowed_tools=[
                "Read", "Write", "Edit", "Glob", "Grep",
                # Docker MCP tools — use instead of Bash
                "mcp__docker__list_containers",
                "mcp__docker__container_logs",
                "mcp__docker__container_health",
                "mcp__docker__docker_exec",
                "mcp__docker__restart_container",
                "mcp__docker__compose_up",
                "mcp__docker__compose_ps",
            ],
            mcp_servers={
                "docker": _mcp_docker(),
            },
            permission_mode="bypassPermissions",
            max_turns=max_turns,
        )

        full_output = ""
        turn_count = 0
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                turn_count += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text
                        # Send live progress — show what Doctor is thinking
                        if on_progress:
                            snippet = block.text.strip()[:80]
                            if snippet:
                                await on_progress("🤖", f"[{turn_count}] {snippet}")
                    elif isinstance(block, ToolUseBlock):
                        if on_progress:
                            from openclow.worker.tasks._agent_base import describe_tool
                            await on_progress("🔧", f"[{turn_count}] {describe_tool(block)}")

        return full_output.strip()

    except ImportError:
        # Fallback to Claude CLI if SDK not available
        return await _run_claude_cli(prompt, workspace, max_turns)
    except Exception as e:
        log.error("doctor.claude_failed", error=str(e))
        return f"UNFIXABLE: Claude agent error: {str(e)[:200]}"


async def _run_claude_cli(prompt: str, workspace: str, max_turns: int = 12) -> str:
    """Fallback: run Claude CLI subprocess."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--disallowedTools", "Bash",
        "--allowedTools", "Read,Write,Edit,Glob,Grep,mcp__docker__list_containers,mcp__docker__container_logs,mcp__docker__container_health,mcp__docker__docker_exec,mcp__docker__restart_container,mcp__docker__compose_up,mcp__docker__compose_ps",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
        if proc.returncode == 0:
            data = json.loads(stdout.decode())
            return data.get("result", "")
        return f"UNFIXABLE: CLI error: {stderr.decode()[:300]}"
    except asyncio.TimeoutError:
        proc.kill()
        return "UNFIXABLE: Claude diagnosis timed out"


# ---------------------------------------------------------------------------
# Core repair loop
# ---------------------------------------------------------------------------

async def repair_container(
    container: str,
    workspace: str,
    compose_file: str,
    compose_project: str,
    service_name: str = "",
    max_attempts: int = 3,
    on_progress: ProgressCallback | None = None,
    extra_context: str = "",
) -> RepairReport:
    """Agentic repair loop for a single container.

    Tries up to max_attempts different fix strategies.
    Reports every step via on_progress callback.
    """
    report = RepairReport(container=container, original_error="")

    async def progress(icon: str, msg: str):
        if on_progress:
            await on_progress(icon, msg)

    # If no service name given, guess from container name
    if not service_name:
        # openclow-myproject-app-1 → app
        parts = container.replace(compose_project + "-", "").rsplit("-", 1)
        service_name = parts[0] if parts else container

    # ── Gather initial state ──
    await progress("🔍", f"Reading logs for {container}...")

    logs = await _get_logs(container, tail=80)
    inspect = await _get_inspect(container)

    state = inspect.get("State", {})
    status = state.get("Status", "unknown")
    exit_code = state.get("ExitCode", -1)
    config = inspect.get("Config", {})
    image = config.get("Image", "unknown")
    command = " ".join(config.get("Cmd", []) or config.get("Entrypoint", []) or ["unknown"])
    workdir = config.get("WorkingDir", "/")
    env_vars = config.get("Env", [])
    # Summarize env (hide secrets)
    env_summary = ", ".join(
        v.split("=")[0] for v in (env_vars or [])
        if not any(secret in v.upper() for secret in ["PASSWORD", "SECRET", "TOKEN", "KEY", "API"])
    )[:500]

    report.original_error = logs[-500:] if logs else f"Status: {status}, Exit: {exit_code}"

    await progress("🔍", f"Status: {status} | Exit: {exit_code}")

    # ── Repair attempts ──
    # Each entry: "Attempt N: <summary>\n  Error after this fix:\n<logs>"
    previous_attempts_detail: list[str] = []

    for attempt in range(1, max_attempts + 1):
        await progress("🔧", f"Repair attempt {attempt}/{max_attempts}...")

        if attempt == 1:
            prompt = DIAGNOSE_PROMPT.format(
                container=container,
                status=status,
                exit_code=exit_code,
                logs=logs[-3000:],
                log_lines=min(80, logs.count("\n") + 1),
                image=image,
                command=command,
                workdir=workdir,
                env_summary=env_summary,
                workspace=workspace,
                compose_file=compose_file,
                compose_project=compose_project,
                service_name=service_name,
                extra_context=extra_context,
            )
        else:
            prompt = RETRY_PROMPT.format(
                container=container,
                workspace=workspace,
                compose_file=compose_file,
                compose_project=compose_project,
                service_name=service_name,
                previous_attempts_with_errors="\n".join(f"- {a}" for a in previous_attempts_detail),
                new_logs=logs[-2000:],
            )

        # Run Claude (with live progress to Telegram)
        result = await _run_claude_diagnosis(prompt, workspace, max_turns=12, on_progress=on_progress)

        # Parse result
        is_fixed = "FIXED:" in result.upper()
        is_unfixable = "UNFIXABLE:" in result.upper()

        # Extract the summary line
        summary = ""
        for line in result.split("\n"):
            if line.strip().upper().startswith("FIXED:"):
                summary = line.strip()[6:].strip()
                break
            elif line.strip().upper().startswith("UNFIXABLE:"):
                summary = line.strip()[10:].strip()
                break

        if not summary:
            summary = result[-200:].strip()

        step = RepairStep(
            action=f"Attempt {attempt}: Claude diagnosis + fix",
            result=summary[:200],
            success=False,  # will verify below
        )

        if is_unfixable:
            await progress("🚫", f"Cannot auto-fix: {summary[:100]}")
            step.result = f"UNFIXABLE: {summary}"
            report.steps.append(step)
            report.suggestion = summary
            break

        # Verify the fix
        await progress("🔍", f"Verifying fix...")
        await asyncio.sleep(8)  # give container time to start

        if await _is_container_healthy(container):
            step.success = True
            report.steps.append(step)
            report.fixed = True
            report.final_status = f"Fixed: {summary}"
            await progress("✅", f"Fixed: {summary[:100]}")
            log.info("doctor.fixed", container=container, attempt=attempt, fix=summary[:100])
            return report

        # Not fixed yet — get new logs for next attempt
        await progress("⚠️", f"Attempt {attempt} didn't work. {summary[:60]}")
        report.steps.append(step)
        logs = await _get_logs(container, tail=80)
        # Store attempt summary AND the resulting error so the retry prompt has full context
        previous_attempts_detail.append(
            f"Attempt {attempt}: {summary[:200]}\n  Error after this fix:\n{logs[-400:]}"
        )

    # All attempts exhausted
    if not report.fixed:
        report.final_status = f"Could not auto-repair after {max_attempts} attempts"
        if not report.suggestion:
            report.suggestion = (
                f"Container '{container}' keeps failing. "
                f"Last error: {logs[-200:] if logs else 'unknown'}. "
                f"Check the logs manually: docker logs {container} --tail 100"
            )
        await progress("❌", f"Repair failed after {max_attempts} attempts")
        log.warning("doctor.exhausted", container=container, attempts=max_attempts)

    return report


async def repair_compose_build(
    build_output: str,
    workspace: str,
    compose_file: str,
    compose_project: str,
    max_attempts: int = 2,
    on_progress: ProgressCallback | None = None,
) -> RepairReport:
    """Repair a docker compose build failure.

    Different from container repair — this fixes build-time errors
    (Dockerfile issues, dependency install failures, etc.)
    """
    report = RepairReport(container="build", original_error=build_output[-500:])

    async def progress(icon: str, msg: str):
        if on_progress:
            await on_progress(icon, msg)

    previous_attempts = []

    for attempt in range(1, max_attempts + 1):
        await progress("🔧", f"Fixing build error (attempt {attempt}/{max_attempts})...")

        if attempt == 1:
            prompt = (
                f"Docker compose build failed. Output:\n\n"
                f"```\n{build_output[-3000:]}\n```\n\n"
                f"Workspace: {workspace}\n"
                f"Compose file: {compose_file}\n"
                f"Compose project: {compose_project}\n\n"
                f"Fix the build error. Common causes:\n"
                f"- Package install failure → pin version, use alternative, or remove\n"
                f"- Missing system dependency → add apt-get install\n"
                f"- Wrong base image → use a compatible one\n"
                f"- Syntax error in Dockerfile → fix it\n\n"
                f"After fixing, rebuild: docker compose -f {compose_file} -p {compose_project} up -d --build\n\n"
                f"End with FIXED: or UNFIXABLE:"
            )
        else:
            prompt = (
                f"Previous build fix didn't work. New error:\n\n"
                f"```\n{build_output[-2000:]}\n```\n\n"
                f"Previous attempts:\n" +
                "\n".join(f"- {a}" for a in previous_attempts) +
                f"\n\nTry a DIFFERENT approach. Workspace: {workspace}, Compose: {compose_file}\n"
                f"End with FIXED: or UNFIXABLE:"
            )

        result = await _run_claude_diagnosis(prompt, workspace, max_turns=15, on_progress=on_progress)

        # Parse
        summary = ""
        for line in result.split("\n"):
            upper = line.strip().upper()
            if upper.startswith("FIXED:"):
                summary = line.strip()[6:].strip()
                break
            elif upper.startswith("UNFIXABLE:"):
                summary = line.strip()[10:].strip()
                report.suggestion = summary
                report.steps.append(RepairStep(f"Attempt {attempt}", f"UNFIXABLE: {summary}", False))
                await progress("🚫", f"Cannot auto-fix build: {summary[:100]}")
                return report

        if not summary:
            summary = result[-200:].strip()

        # Verify — try to rebuild
        await progress("🔍", "Verifying build fix...")
        rc, new_output = await _run(
            "docker", "compose", "-f", compose_file, "-p", compose_project,
            "up", "-d", "--build",
            cwd=workspace, timeout=300,
        )

        has_error = "error" in new_output.lower() and "warning" not in new_output.lower()
        has_failed = "failed" in new_output.lower()

        if rc == 0 and not has_error and not has_failed:
            report.fixed = True
            report.final_status = f"Build fixed: {summary}"
            report.steps.append(RepairStep(f"Attempt {attempt}", summary, True))
            await progress("✅", f"Build fixed: {summary[:100]}")
            return report

        # Still broken
        build_output = new_output
        previous_attempts.append(summary[:200])
        report.steps.append(RepairStep(f"Attempt {attempt}", summary, False))
        await progress("⚠️", f"Build still failing after attempt {attempt}")

    report.final_status = f"Build repair failed after {max_attempts} attempts"
    if not report.suggestion:
        report.suggestion = f"Build keeps failing. Last error: {build_output[-300:]}"
    return report
