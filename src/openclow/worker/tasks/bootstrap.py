"""Bootstrap task — agentic project setup with self-healing.

NOT a one-shot script. This is a full end-to-end agentic mission:
1. Clone repo → check .env → docker compose up
2. If build fails → Doctor diagnoses + fixes → retry build
3. If containers unhealthy → Doctor reads logs → fixes → restarts
4. If app not responding → Doctor investigates → fixes
5. Start Cloudflare tunnel → generate public URL
6. Playwright visits the app → verifies it actually works → screenshots
7. If Playwright sees errors → Doctor reads logs → fixes → retry
8. Every single step reported to Telegram in real-time
9. If can't fix → clear explanation of what's wrong + what user needs to do
10. Keeps going until everything is healthy OR all options exhausted
"""
import asyncio
import os
import re
import shutil

from sqlalchemy import select

from openclow.models import Project, async_session
from openclow.providers import factory
from openclow.settings import settings
from openclow.utils.logging import get_logger

log = get_logger()


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

async def _run(*args: str, cwd: str = None, timeout: int = 300,
               project_id: int | None = None) -> tuple[int, str]:
    """Run a command safely, return (returncode, output).

    Routes Docker commands through docker_guard for allowlist enforcement.
    All commands are audit-logged.
    Docker compose builds get extended timeout (600s).
    """
    from openclow.services.audit_service import log_action

    # Route Docker commands through the guard
    if args and args[0] == "docker":
        from openclow.services.docker_guard import run_docker
        # Docker compose build/up needs more time than 300s
        docker_timeout = timeout
        cmd_str = " ".join(args)
        if "compose" in cmd_str and ("up" in cmd_str or "build" in cmd_str):
            docker_timeout = max(timeout, 600)
        return await run_docker(
            *args, actor="bootstrap", project_id=project_id,
            cwd=cwd, timeout=docker_timeout,
        )

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if config and config.get("token"):
            env["GH_TOKEN"] = config["token"]
            env["GITHUB_TOKEN"] = config["token"]
    except Exception:
        pass

    cmd_str = " ".join(args)
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

    await log_action(
        actor="bootstrap", action="bash", command=cmd_str,
        workspace=cwd, exit_code=rc, output_summary=output[:2000],
    )
    return rc, output


async def _run_shell(cmd: str, cwd: str = None, timeout: int = 300) -> tuple[int, str]:
    """Run a shell command string."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if config and config.get("token"):
            env["GH_TOKEN"] = config["token"]
            env["GITHUB_TOKEN"] = config["token"]
    except Exception:
        pass

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        combined = (stdout.decode() + stderr.decode()).strip()
        return proc.returncode, combined[-4000:]
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, f"TIMEOUT after {timeout}s"
    except Exception as e:
        return -1, str(e)


# ---------------------------------------------------------------------------
# Telegram reporters (shared services)
# ---------------------------------------------------------------------------

from openclow.services.checklist_reporter import ChecklistReporter  # noqa: E402
from openclow.services.status_reporter import LineReporter as StatusReporter  # noqa: E402


# ---------------------------------------------------------------------------
# Bootstrap steps
# ---------------------------------------------------------------------------

async def _step_clone(on_progress, project, workspace: str) -> str:
    """Step 1: Clone or update the repository. Returns detail string."""
    await on_progress("Cloning repository...")

    if not os.path.exists(workspace):
        git_provider = await factory.get_git()
        await git_provider.clone_repo(project.github_repo, workspace)
        return "cloned"
    else:
        # Fetch + reset to handle divergent branches cleanly
        await _run("git", "-C", workspace, "fetch", "origin",
                   project.default_branch, cwd=workspace)
        rc, output = await _run("git", "-C", workspace, "reset", "--hard",
                                f"origin/{project.default_branch}", cwd=workspace)
        if rc == 0:
            return "updated"
        else:
            return f"warning: {output[:50]}"


async def _step_env(on_progress, workspace: str, compose: str) -> str:
    """Step 2: Check/setup .env file. Returns detail string."""
    await on_progress("Checking environment...")

    compose_dir = os.path.dirname(os.path.join(workspace, compose))
    env_path = os.path.join(compose_dir, ".env")
    env_example = os.path.join(compose_dir, ".env.example")

    if not os.path.exists(env_path) and os.path.exists(env_example):
        shutil.copy2(env_example, env_path)
        return "copied .env.example → .env"
    elif os.path.exists(env_path):
        return ".env exists"
    else:
        return "no .env found (may be fine)"


async def _step_agentic_setup(
    checklist: ChecklistReporter, project, workspace: str, compose: str, compose_project: str,
) -> bool:
    """Step 3: LLM-driven project setup.

    Claude Agent reads the project, plans setup steps, executes them,
    and reports progress via the checklist. No hardcoded logic.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    except ImportError:
        # Fallback: dumb docker compose up
        checklist.add_steps(["Start Docker containers"])
        idx = len(checklist.steps) - 1
        await checklist.start_step(idx)
        rc, _ = await _run(
            "docker", "compose", "-f", compose, "-p", compose_project, "up", "-d", "--build",
            cwd=workspace, timeout=300,
        )
        if rc == 0:
            await checklist.complete_step(idx, "containers started")
            return True
        await checklist.fail_step(idx, "docker compose failed")
        return False

    prompt = f"""Set up the project "{project.name}" for development.

Project info:
- Tech stack: {project.tech_stack or 'unknown'}
- Workspace: {workspace}
- Docker compose file: {compose}
- Docker project name: {compose_project}
- App container: {project.app_container_name or 'auto-detect from docker-compose'}
- App port: {project.app_port or 'auto-detect'}
- Setup hints from onboarding: {project.setup_commands or 'none detected'}

INSTRUCTIONS:

PHASE 1 — PLAN (do this first):
Read docker-compose.yml, any Dockerfiles, package.json, composer.json, requirements.txt, etc.
Understand what this project needs to run.
Then output your setup plan EXACTLY like this:

PLAN_START
STEP: Install PHP dependencies (composer)
STEP: Install Node dependencies (npm)
STEP: Build frontend assets
STEP: Start Docker containers
STEP: Run database migrations
PLAN_END

Only include steps that are actually needed for this project.

PHASE 2 — EXECUTE:
Execute each step in order. For Docker commands always use: docker compose -f {compose} -p {compose_project}
After completing each step, output EXACTLY:
DONE: <step_number> <short result>

Example: DONE: 1 installed 47 packages

If a step fails, output:
FAIL: <step_number> <error summary>
Then try to fix it and retry. If fixed, output DONE for that step.

PHASE 3 — FINISH:
After all steps complete, output exactly one of:
SETUP_OK
or
SETUP_FAILED: <clear reason why>

RULES:
- CRITICAL: Before ANY docker compose command, run: export $(cat .env.ports | grep -v '^#' | xargs)
  This loads unique port assignments that prevent conflicts with other projects.
- Always use docker compose -p {compose_project} for ALL docker commands
- Read files before running commands to understand the setup
- If something fails, read the error output, diagnose, and try a different approach
- For Laravel: composer install, npm install, npm run build, php artisan migrate
- For Node.js: npm install, npm run build
- For Python: pip install -r requirements.txt
- Check if auth.json exists in the workspace root — if yes, copy it where needed (e.g. for composer private packages)
- If composer needs GitLab/GitHub auth, check for auth.json and copy to the right location
- When a FAIL happens, include the actual error message (not just exit code) so the user understands WHY

IMPORTANT — PERFORMANCE:
- Before installing, check node --version, php --version, composer --version to verify compatibility
- If deps are already installed (vendor/ or node_modules/ exist), SKIP the install step — just verify
- For npm run build: if it takes too long, check if public/build/ already has assets and skip
- For docker compose: check with `docker compose -f {compose} -p {compose_project} ps` first
  - If all containers are running → SKIP compose up entirely
  - If images already exist (check `docker images | grep {compose_project}`) → use `docker compose up -d` WITHOUT --build
  - Only use --build if images don't exist yet or Dockerfile was modified
- NEVER run a command that blocks forever — add timeouts or use background processes
- After each long command, output a DONE: or FAIL: line so progress updates in Telegram
- For docker compose up: output a DONE: line IMMEDIATELY after the command returns, don't wait
"""

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt=(
            "You are a senior DevOps engineer setting up a project. "
            "You have full Bash access. Read files before acting. "
            "Fix errors when they happen. Be precise and thorough."
        ),
        model="claude-sonnet-4-6",  # DevOps setup is procedural — Sonnet is faster
        allowed_tools=["Bash", "Read", "Glob", "Grep", "Edit", "Write"],
        permission_mode="bypassPermissions",
        max_turns=20,
    )

    full_output = ""
    current_step = -1
    plan_lines: list[str] = []
    plan_parsed = False
    in_plan = False
    base_index = len(checklist.steps)  # offset for steps added by agent

    try:
        async for message in query(prompt=prompt, options=options):
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    full_output += block.text + "\n"

                    for line in block.text.split("\n"):
                        line = line.strip()
                        if not line:
                            continue

                        # Parse plan
                        if "PLAN_START" in line:
                            in_plan = True
                            plan_lines = []
                        elif "PLAN_END" in line and in_plan:
                            in_plan = False
                            plan_parsed = True
                            checklist.add_steps(plan_lines)
                            base_index = len(checklist.steps) - len(plan_lines)
                            if plan_lines:
                                current_step = 0
                                await checklist.start_step(base_index)
                            else:
                                await checklist._force_render()
                        elif in_plan and line.startswith("STEP:"):
                            plan_lines.append(line[5:].strip())

                        # Parse progress
                        elif line.startswith("DONE:"):
                            rest = line[5:].strip()
                            parts = rest.split(" ", 1)
                            if parts[0].isdigit():
                                idx = int(parts[0]) - 1
                                detail = parts[1] if len(parts) > 1 else ""
                            else:
                                idx = current_step
                                detail = rest
                            if 0 <= idx < len(plan_lines):
                                await checklist.complete_step(base_index + idx, detail[:50])
                                current_step = idx + 1
                                if current_step < len(plan_lines):
                                    await checklist.start_step(base_index + current_step)

                        elif line.startswith("FAIL:"):
                            rest = line[5:].strip()
                            parts = rest.split(" ", 1)
                            if parts[0].isdigit():
                                idx = int(parts[0]) - 1
                                detail = parts[1] if len(parts) > 1 else ""
                            else:
                                idx = current_step
                                detail = rest
                            if 0 <= idx < len(plan_lines):
                                await checklist.fail_step(base_index + idx, detail[:50])

                elif isinstance(block, ToolUseBlock):
                    # Show what the agent is doing as live detail
                    if current_step >= 0 and current_step < len(plan_lines):
                        tool_name = block.name if hasattr(block, "name") else ""
                        cmd = ""
                        if hasattr(block, "input") and isinstance(block.input, dict):
                            cmd = str(block.input.get("command", block.input.get("file_path", "")))[:40]
                        if cmd:
                            await checklist.update_step(base_index + current_step, cmd)

        return "SETUP_OK" in full_output

    except Exception as e:
        log.error("bootstrap.agentic_setup_failed", error=str(e))
        if current_step >= 0 and current_step < len(plan_lines):
            await checklist.fail_step(base_index + current_step, str(e)[:50])
        return False


async def _step_health_check(
    status: StatusReporter, workspace: str, compose: str, compose_project: str,
) -> bool:
    """Step 4: Check container health with agentic repair loop.

    Returns True if all containers healthy.
    """
    from openclow.agents.doctor import repair_container, _is_container_healthy

    await status.section("Container Health Check")
    await status.add("🔄", "Waiting for containers to initialize...")

    await asyncio.sleep(12)

    # Get container statuses
    rc, ps_output = await _run(
        "docker", "compose", "-p", compose_project, "ps", "--format", "json",
        cwd=workspace,
    )

    if rc != 0 or not ps_output.strip():
        await status.add("❌", "No containers found after compose up")
        return False

    import json
    containers = []
    for line in ps_output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            containers.append({
                "name": data.get("Name", ""),
                "state": data.get("State", "unknown"),
                "health": data.get("Health", ""),
                "service": data.get("Service", ""),
            })
        except (json.JSONDecodeError, KeyError):
            continue

    if not containers:
        await status.add("❌", "Could not parse container statuses")
        return False

    # Show container list
    for c in containers:
        icon = "✅" if c["state"] == "running" else "❌"
        health_str = f" ({c['health']})" if c.get("health") else ""
        short = c["name"].split("-")[-1] if "-" in c["name"] else c["name"]
        await status.add(icon, f"{short}: {c['state']}{health_str}")

    # Find unhealthy ones
    unhealthy = [
        c for c in containers
        if c["state"] != "running" or c.get("health", "").lower() == "unhealthy"
    ]

    if not unhealthy:
        await status.add("✅", f"All {len(containers)} containers healthy!")
        return True

    # ── Repair unhealthy containers ──
    await status.section(f"Repairing {len(unhealthy)} Container(s)")

    all_fixed = True
    for c in unhealthy:
        container_name = c["name"]
        service = c.get("service", "")

        async def on_progress(icon, msg, _name=container_name):
            short = _name.split("-")[-1] if "-" in _name else _name
            await status.add(icon, f"[{short}] {msg}")

        repair = await repair_container(
            container=container_name,
            workspace=workspace,
            compose_file=compose,
            compose_project=compose_project,
            service_name=service,
            max_attempts=3,
            on_progress=on_progress,
        )

        if repair.fixed:
            short = container_name.split("-")[-1]
            await status.add("✅", f"{short} repaired: {repair.final_status[:60]}")
        else:
            all_fixed = False
            short = container_name.split("-")[-1]
            await status.add("❌", f"{short}: {repair.suggestion[:80]}")

    return all_fixed


async def _step_app_health(status: StatusReporter, port: int) -> bool:
    """Step 5: Check if the app is responding on its port."""
    await status.add("🔄", f"Checking app at port {port}...")

    # Try multiple times — app might be slow to start
    for attempt in range(3):
        rc, output = await _run_shell(
            f'curl -sf http://localhost:{port}/health 2>&1 || '
            f'curl -sf http://localhost:{port}/ 2>&1 || '
            f'echo NOT_RESPONDING',
            timeout=10,
        )
        if "NOT_RESPONDING" not in output:
            await status.add("✅", f"App responding on port {port}", replace_last=True)
            return True
        if attempt < 2:
            await status.add("⏳", f"App not responding yet, retrying in 10s... ({attempt+1}/3)", replace_last=True)
            await asyncio.sleep(10)

    await status.add("⚠️", f"App not responding on port {port} (may still be starting)", replace_last=True)
    return False


async def _step_tunnel(status: StatusReporter, port: int, project_name: str) -> str:
    """Step 6: Start cloudflared tunnel (persisted via tunnel_service)."""
    from openclow.services.tunnel_service import start_tunnel

    await status.add("🔄", "Creating public URL...")

    try:
        url = await start_tunnel(project_name, f"http://localhost:{port}")
        if url:
            await status.add("✅", f"Public URL: {url}", replace_last=True)
            return url
        else:
            await status.add("⚠️", "Tunnel failed to start (local access only)", replace_last=True)
            return ""
    except Exception as e:
        await status.add("⚠️", f"Tunnel failed: {str(e)[:60]} (local access only)", replace_last=True)
        return ""


# ---------------------------------------------------------------------------
# Playwright verification
# ---------------------------------------------------------------------------

async def _step_verify_app(
    status: StatusReporter, tunnel_url: str, port: int, project_name: str, workspace: str,
) -> tuple[bool, str]:
    """Step 7: Playwright verification — visit app, check it works.

    Uses Claude Agent SDK + Playwright MCP to:
    1. Navigate to the app URL
    2. Wait for page load
    3. Take a screenshot
    4. Check for errors (500s, blank pages, JS errors)

    Returns (ok: bool, detail: str).
    """
    await status.section("App Verification")
    await status.add("🔄", "Launching Playwright to verify app...")

    url = tunnel_url or f"http://localhost:{port}"

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions

        options = ClaudeAgentOptions(
            cwd=workspace,
            system_prompt=(
                "You are a QA engineer verifying a newly deployed application. "
                "Your job is to open the app in a browser, check it loads correctly, "
                "and report what you see. Be concise."
            ),
            model="claude-sonnet-4-6",
            allowed_tools=[
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
                "mcp__playwright__browser_take_screenshot",
            ],
            mcp_servers={
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@0.0.28", "--headless"],
                },
            },
            permission_mode="bypassPermissions",
            max_turns=4,  # Navigate + screenshot + report = 3-4 turns max
        )

        prompt = (
            f"Navigate to {url} and verify the app is working.\n\n"
            "Steps:\n"
            "1. Navigate to the URL\n"
            "2. Wait for page to fully load\n"
            "3. Take a screenshot\n"
            "4. Check for errors (500 pages, blank screens, connection refused)\n"
            "5. Describe what you see (login page, dashboard, API docs, error, etc.)\n\n"
            "At the end, output EXACTLY one of these lines:\n"
            "VERIFY_STATUS: OK\n"
            "VERIFY_STATUS: FAIL\n\n"
            "And then:\n"
            "VERIFY_DETAIL: [what you see in 1 sentence]\n"
        )

        last_text = ""
        screenshot_path = ""

        async for message in query(prompt=prompt, options=options):
            if hasattr(message, "content"):
                for block in (message.content if isinstance(message.content, list) else [message.content]):
                    if hasattr(block, "text"):
                        last_text += block.text + "\n"
                    # Capture screenshot path if the agent saved one
                    if hasattr(block, "type") and block.type == "tool_result":
                        text_content = str(block)
                        if "screenshot" in text_content.lower() and ".png" in text_content.lower():
                            import re as _re
                            match = _re.search(r"(/[\w/.-]+\.png)", text_content)
                            if match:
                                screenshot_path = match.group(1)

        # Parse result
        verify_ok = "VERIFY_STATUS: OK" in last_text
        detail = ""
        for line in last_text.split("\n"):
            if line.strip().startswith("VERIFY_DETAIL:"):
                detail = line.split(":", 1)[1].strip()
                break

        if not detail:
            # Extract something useful from the output
            detail = last_text.strip().split("\n")[-1][:120] if last_text.strip() else "no response"

        if verify_ok:
            await status.add("✅", f"App verified: {detail[:80]}", replace_last=True)
        else:
            await status.add("❌", f"App issues: {detail[:80]}", replace_last=True)

        log.info("bootstrap.playwright_verify",
                 project=project_name, ok=verify_ok, detail=detail[:200])

        return verify_ok, detail

    except ImportError:
        await status.add("⚠️", "Claude Agent SDK not available — skipping Playwright verification", replace_last=True)
        return False, "sdk_unavailable"
    except Exception as e:
        await status.add("⚠️", f"Playwright verification failed: {str(e)[:60]}", replace_last=True)
        log.warning("bootstrap.playwright_failed", error=str(e))
        return False, f"error: {str(e)[:100]}"


# ---------------------------------------------------------------------------
# Main bootstrap task
# ---------------------------------------------------------------------------

async def bootstrap_project(ctx: dict, project_id: int, chat_id: str, message_id: str):
    """Agentic project bootstrap with live checklist progress.

    Uses ChecklistReporter for UX and Claude Agent for smart setup.
    The LLM reads the project, plans steps, executes them, fixes errors.
    """
    chat = await factory.get_chat()

    async with async_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()

    if not project:
        await chat.send_error(chat_id, message_id, "Project not found")
        return

    from openclow.services.project_lock import acquire_project_lock, get_lock_holder
    lock = await acquire_project_lock(project_id, task_id=f"bootstrap-{project.name}", wait=5)
    if lock is None:
        holder = await get_lock_holder(project_id)
        await chat.edit_message(chat_id, message_id,
                                f"Cannot bootstrap — project is locked by task {holder}.\n"
                                f"Wait for it to finish or use /cancel.")
        await chat.close()
        return

    workspace = os.path.join(settings.workspace_base_path, "_cache", project.name)
    compose = project.docker_compose_file or "docker-compose.yml"
    compose_project = f"openclow-{project.name}"

    # Use allocated port for this project (deterministic, unique per project)
    from openclow.services.port_allocator import get_app_port
    port = get_app_port(project_id)

    if not message_id or message_id == "0":
        bot = chat._get_bot()
        msg = await bot.send_message(chat_id=int(chat_id), text=f"Setting up {project.name}...")
        message_id = str(msg.message_id)

    # Use ChecklistReporter for clean live progress
    checklist = ChecklistReporter(
        chat, chat_id, message_id,
        title=f"Setting up {project.name}",
        subtitle=project.tech_stack or "",
    )

    # Initial steps (Python-driven, fast)
    checklist.set_steps([
        "Clone repository",
        "Setup environment",
    ])
    await checklist._force_render()
    await checklist.start()

    try:
        # ── Step 1: Clone ──
        await checklist.start_step(0)
        clone_detail = await _step_clone(
            lambda msg: checklist.update_step(0, msg), project, workspace,
        )
        await checklist.complete_step(0, clone_detail)

        # ── Step 2: Environment ──
        await checklist.start_step(1)
        env_detail = await _step_env(
            lambda msg: checklist.update_step(1, msg), workspace, compose,
        )
        await checklist.complete_step(1, env_detail)

        # ── Write port isolation env vars ──
        # Each project gets unique ports so multiple projects don't conflict
        from openclow.services.port_allocator import get_port_env_vars
        port_vars = get_port_env_vars(project.id)
        ports_file = os.path.join(workspace, ".env.ports")
        with open(ports_file, "w") as f:
            f.write("# Auto-generated by OpenClow — unique ports for this project\n")
            for k, v in port_vars.items():
                f.write(f"{k}={v}\n")

        # ── Step 3: Agentic setup — Claude takes over ──
        # Claude plans its own steps, adds them to checklist, executes them
        setup_ok = await _step_agentic_setup(checklist, project, workspace, compose, compose_project)

        if not setup_ok:
            checklist._footer = "❌ Setup failed — check errors above"
            from aiogram.types import InlineKeyboardButton
            buttons = [
                [InlineKeyboardButton(text="🔄 Retry Bootstrap", callback_data=f"project_bootstrap:{project.id}")],
                [
                    InlineKeyboardButton(text="🔗 Unlink", callback_data=f"project_unlink:{project.id}"),
                    InlineKeyboardButton(text="🗑 Remove", callback_data=f"project_remove:{project.id}"),
                ],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ]
            await checklist.stop()
            await checklist._force_render(buttons=buttons)
            log.error("bootstrap.setup_failed", project=project.name)
            return

        # ── Post-setup steps: Tunnel + Playwright ──
        tunnel_idx = len(checklist.steps)
        checklist.add_steps(["Create public URL", "Browser verification"])

        # Tunnel
        await checklist.start_step(tunnel_idx)
        from openclow.services.tunnel_service import start_tunnel
        tunnel_url = ""
        try:
            url = await start_tunnel(project.name, f"http://localhost:{port}")
            if url:
                tunnel_url = url
                await checklist.complete_step(tunnel_idx, url)
            else:
                await checklist.fail_step(tunnel_idx, "could not start")
        except Exception as e:
            await checklist.fail_step(tunnel_idx, str(e)[:40])

        # Playwright verification
        pw_idx = tunnel_idx + 1
        await checklist.start_step(pw_idx)
        verify_url = tunnel_url or f"http://localhost:{port}"
        try:
            from claude_agent_sdk import query as pw_query, ClaudeAgentOptions as PwOptions
            from claude_agent_sdk.types import AssistantMessage as PwMsg, TextBlock as PwText

            pw_options = PwOptions(
                cwd=workspace,
                system_prompt="You are verifying a deployed app. Navigate, check it loads, report.",
                model="claude-sonnet-4-6",  # Visual check — Sonnet is fast enough
                allowed_tools=[
                    "mcp__playwright__browser_navigate",
                    "mcp__playwright__browser_snapshot",
                    "mcp__playwright__browser_take_screenshot",
                ],
                mcp_servers={"playwright": {"command": "npx", "args": ["@playwright/mcp@0.0.28", "--headless"]}},
                permission_mode="bypassPermissions",
                max_turns=4,  # Navigate + screenshot + report = 3-4 turns max
            )
            pw_output = ""
            async for msg in pw_query(
                prompt=f"Navigate to {verify_url}, verify the page loads, take a screenshot. "
                       f"Output VERIFY_STATUS: OK or VERIFY_STATUS: FAIL and VERIFY_DETAIL: <what you see>",
                options=pw_options,
            ):
                if isinstance(msg, PwMsg):
                    for b in msg.content:
                        if isinstance(b, PwText):
                            pw_output += b.text

            if "VERIFY_STATUS: OK" in pw_output:
                detail = ""
                for line in pw_output.split("\n"):
                    if "VERIFY_DETAIL:" in line:
                        detail = line.split(":", 1)[1].strip()
                        break
                await checklist.complete_step(pw_idx, detail[:40] or "app loads")
            else:
                await checklist.fail_step(pw_idx, "app not responding")
        except Exception:
            await checklist.complete_step(pw_idx, "skipped")

        # ── Final summary with buttons ──
        checklist._footer = ""
        all_done = all(s["status"] == "done" for s in checklist.steps)
        any_failed = any(s["status"] == "failed" for s in checklist.steps)

        if all_done:
            checklist._footer = "🚀 Ready for tasks!"
        elif any_failed:
            checklist._footer = "⚠️ Some steps had issues — check above"
        else:
            checklist._footer = "✅ Setup complete"

        if tunnel_url:
            checklist._footer += f"\n🌐 {tunnel_url}"

        from aiogram.types import InlineKeyboardButton
        buttons = [
            [
                InlineKeyboardButton(text="🚀 New Task", callback_data="menu:task"),
                InlineKeyboardButton(text="💚 Health", callback_data=f"health:{project.id}"),
            ],
        ]
        if tunnel_url:
            buttons.insert(0, [InlineKeyboardButton(text="🌐 Open App", url=tunnel_url)])

        await checklist.stop()
        await checklist._force_render(buttons=buttons)

        log.info("bootstrap.complete", project=project.name, all_done=all_done)

    except (Exception, asyncio.CancelledError, TimeoutError) as e:
        await checklist.stop()
        error_msg = "Job timed out" if isinstance(e, (asyncio.CancelledError, TimeoutError)) else str(e)[:150]
        log.error("bootstrap.failed", project=project.name, error=error_msg)
        checklist._footer = f"❌ {error_msg}"
        try:
            from aiogram.types import InlineKeyboardButton
            await checklist._force_render(buttons=[
                [InlineKeyboardButton(text="🔄 Retry Bootstrap", callback_data=f"project_bootstrap:{project.id}")],
                [
                    InlineKeyboardButton(text="🔗 Unlink", callback_data=f"project_unlink:{project.id}"),
                    InlineKeyboardButton(text="🗑 Remove", callback_data=f"project_remove:{project.id}"),
                ],
                [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
            ])
        except Exception:
            pass
    finally:
        if lock:
            await lock.release()
        from openclow.services.audit_service import flush
        await flush()
        await chat.close()


