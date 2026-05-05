# Plan: Single Master Claude Agent for Full Bootstrap

## Context

**Problem:** The current bootstrap has ~5 separate Claude agent calls stitched together with Python `if/else` logic. When Docker fails, the fix agent returns `True/False` to Python — **no LLM ever sees the full picture or reasons about what happened**. The decision chain is:

```
Agent 1 (deps) → Python if/else → Agent 2 (docker fix) → Python if/else → Agent 3 (migrations) → ...
```

Each agent is blind to what the others did. Python makes all strategic decisions with dumb conditionals.

**Goal:** One master Claude agent runs the entire setup — from deps to verification. It **sees everything**, **reasons about failures** ("this failed because selenium has no ARM image, I'll swap it"), **decides what to do next**, and **reports its reasoning to the user** in real-time via the Telegram checklist.

```
Master Claude Agent (sees everything, decides everything)
  ├─ reads project structure
  ├─ installs deps → reports what it did
  ├─ docker compose up → if fails, REASONS about why, fixes, retries
  ├─ runs migrations → picks the right framework command
  ├─ verifies app → if fails, reads logs, diagnoses, fixes
  └─ reports EVERY decision back to the user
```

## Architecture

### What the Master Agent Gets

- **MCP Tools:** Docker MCP (compose_up, container_logs, docker_exec, restart_container, container_health), Git MCP (for the workspace)
- **Built-in Tools:** Bash, Read, Write, Edit, Glob, Grep
- **Context:** Full project info (tech stack, docker-compose.yml path, workspace, host architecture, allocated port, compose project name)
- **Model:** `claude-sonnet-4-6` (fast, procedural — this is DevOps, not creative coding)
- **Max turns:** 60 (full pipeline needs room)

### What Python Still Does (Not Agent Work)

These stay in Python because they need system-level access or DB:
1. **Clone repo** — needs git credentials/SSH keys from config
2. **Create .env** — needs config service DB access
3. **Port allocation** — needs `get_port_env_vars()` system coordination
4. **Tunnel creation** — needs cloudflared binary + service
5. **Project status updates** — needs DB writes
6. **Checklist UI** — Python parses agent output markers → updates Telegram

### What the Master Agent Handles (Currently 5 Separate Agents)

**All of these become ONE agent call:**
1. Install dependencies (currently `_step_agentic_setup`)
2. Build frontend (currently Python `npm run build` fallback)
3. Docker compose up + fix failures (currently `_step_docker_up` + `_agent_fix_docker_config`)
4. Database migrations (currently `_step_agent_migrations`)
5. App verification (currently Python `curl` loop)

### Output Protocol (Agent → Python)

```
STATUS: <what the agent is doing>           → checklist.update_step()
DIAGNOSIS: <why something failed>           → checklist.update_step() with ⚠️
ACTION: <what the agent decided to do>      → checklist.update_step() with 🔧
STEP_DONE: <N> <summary>                    → checklist.complete_step(N)
STEP_SKIP: <N> <reason>                     → checklist.skip_step(N)
STEP_FAIL: <N> <error>                      → checklist.fail_step(N)
BOOTSTRAP_COMPLETE                          → success exit
BOOTSTRAP_FAILED: <reason>                  → failure exit
```

## Implementation

### Files to Modify

1. **[bootstrap.py](src/taghdev/worker/tasks/bootstrap.py)** — Major: replace `_step_agentic_setup` + `_step_docker_up` + `_agent_fix_docker_config` + `_step_agent_migrations` + verify logic with single master agent call
2. **[doctor.py](src/taghdev/agents/doctor.py)** — No changes (kept for health_task.py periodic repairs)

### Step 1: Add master agent prompt template (~line 697 area in bootstrap.py)

```python
MASTER_BOOTSTRAP_PROMPT = """You are setting up a project from scratch. You have FULL CONTROL.

PROJECT: {project_name}
TECH STACK: {tech_stack}
WORKSPACE: {workspace}
COMPOSE FILE: {compose}
COMPOSE PROJECT: {compose_project}
HOST ARCHITECTURE: {arch}
ALLOCATED PORT: {port}

DOCKER-COMPOSE CONTENTS:
```yaml
{compose_contents}
```

.ENV CONTENTS:
```
{env_contents}
```

YOUR MISSION — execute these steps IN ORDER:

STEP 2 — INSTALL DEPENDENCIES:
- Read the project to understand the package manager (composer, npm, pip, etc.)
- For DOCKERIZED projects: usually SKIP — Docker handles deps at build time
- For non-dockerized: run the install command
- Verify deps exist (vendor/, node_modules/, .venv/, etc.)

STEP 3 — BUILD FRONTEND:
- If package.json exists with a build script, run `npm run build`
- If no frontend or assets already built, SKIP
- For dockerized projects: usually SKIP

STEP 4 — START DOCKER CONTAINERS:
- Use the Docker MCP: call compose_up with the compose file and project name
- If it FAILS — THIS IS CRITICAL:
  * Read the error output carefully
  * DIAGNOSE the root cause (missing env vars? ARM image issue? port conflict? build failure?)
  * Output DIAGNOSIS: <your analysis>
  * FIX IT (edit docker-compose.yml, .env, Dockerfile — whatever is needed)
  * Output ACTION: <what you're fixing>
  * Retry compose_up
  * You get up to 3 fix attempts
- After containers start, verify ALL are running via Docker MCP list_containers

STEP 5 — DATABASE MIGRATIONS:
- Identify the framework from project files (Laravel→artisan, Django→manage.py, Rails→rake, Node→prisma/knex/etc.)
- Find the app container (not mysql/redis/postgres — the actual app)
- Use Docker MCP docker_exec to run migrations inside the container
- Run seeders if they exist
- If DB not ready, wait and retry (up to 3 times)

STEP 6 — VERIFY APP:
- Use Docker MCP docker_exec to curl localhost:<internal_port> inside the app container
- Check for HTTP 200/301/302
- If it fails, read container logs, diagnose, and try to fix
- Report the HTTP status code

RULES:
- Output STATUS: <message> BEFORE every action (so user sees live progress)
- Output DIAGNOSIS: <analysis> when something fails (so user understands WHY)
- Output ACTION: <what you're doing> when fixing something
- Output STEP_DONE: <N> <summary> when a step succeeds
- Output STEP_SKIP: <N> <reason> when a step should be skipped
- Output STEP_FAIL: <N> <error> when a step fails after retries
- Output BOOTSTRAP_COMPLETE when all steps done
- Output BOOTSTRAP_FAILED: <reason> if you cannot continue
- Be FAST — don't over-analyze, act decisively
- Be SURGICAL — only change what's broken
- You CAN modify docker-compose.yml, .env, Dockerfiles — whatever it takes
"""
```

### Step 2: Add `_run_master_agent()` function (new, replaces multiple functions)

```python
async def _run_master_agent(
    checklist: ChecklistReporter, project, workspace: str,
    compose: str, compose_project: str, port: int,
) -> bool:
    """Single master agent handles steps 2-6 of bootstrap."""
    from claude_agent_sdk import query, ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, TextBlock, ToolUseBlock
    import platform

    arch = platform.machine()

    # Read compose + env for agent context
    compose_path = os.path.join(workspace, compose)
    compose_contents = ""
    if os.path.exists(compose_path):
        with open(compose_path) as f:
            compose_contents = f.read()[:4000]

    env_path = os.path.join(workspace, ".env")
    env_contents = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            env_contents = f.read()[:2000]

    prompt = MASTER_BOOTSTRAP_PROMPT.format(
        project_name=project.name,
        tech_stack=project.tech_stack or "Unknown",
        workspace=workspace,
        compose=compose,
        compose_project=compose_project,
        arch=arch,
        port=port,
        compose_contents=compose_contents,
        env_contents=env_contents,
    )

    options = ClaudeAgentOptions(
        cwd=workspace,
        system_prompt=(
            f"Senior DevOps engineer setting up {project.name}. "
            f"Host: {arch}. Be fast, be decisive, fix errors yourself."
        ),
        model="claude-sonnet-4-6",
        allowed_tools=[
            "Bash", "Read", "Write", "Edit", "Glob", "Grep",
            # Docker MCP — the agent's primary interface for containers
            "mcp__docker__compose_up",
            "mcp__docker__compose_ps",
            "mcp__docker__list_containers",
            "mcp__docker__container_logs",
            "mcp__docker__container_health",
            "mcp__docker__docker_exec",
            "mcp__docker__restart_container",
        ],
        mcp_servers={
            "docker": _mcp_docker(),
        },
        permission_mode="bypassPermissions",
        max_turns=60,
    )

    current_step = 2  # Steps 0-1 already done by Python
    success = False

    try:
        async for message in query(prompt=prompt, options=options):
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    for line in block.text.split("\n"):
                        line = line.strip()
                        if not line:
                            continue

                        if line.startswith("STATUS:"):
                            detail = line[7:].strip()[:60]
                            await checklist.update_step(current_step, detail)

                        elif line.startswith("DIAGNOSIS:"):
                            detail = line[10:].strip()[:80]
                            await checklist.update_step(current_step, f"⚠️ {detail}")

                        elif line.startswith("ACTION:"):
                            detail = line[7:].strip()[:60]
                            await checklist.update_step(current_step, f"🔧 {detail}")

                        elif line.startswith("STEP_DONE:"):
                            parts = line[10:].strip().split(" ", 1)
                            step_num = int(parts[0])
                            detail = parts[1] if len(parts) > 1 else ""
                            await checklist.complete_step(step_num, detail[:60])
                            current_step = step_num + 1
                            if current_step <= 6:
                                await checklist.start_step(current_step)

                        elif line.startswith("STEP_SKIP:"):
                            parts = line[10:].strip().split(" ", 1)
                            step_num = int(parts[0])
                            detail = parts[1] if len(parts) > 1 else "skipped"
                            await checklist.skip_step(step_num, detail[:60])
                            current_step = step_num + 1
                            if current_step <= 6:
                                await checklist.start_step(current_step)

                        elif line.startswith("STEP_FAIL:"):
                            parts = line[10:].strip().split(" ", 1)
                            step_num = int(parts[0])
                            detail = parts[1] if len(parts) > 1 else "failed"
                            await checklist.fail_step(step_num, detail[:60])

                        elif "BOOTSTRAP_COMPLETE" in line:
                            success = True

                        elif "BOOTSTRAP_FAILED" in line:
                            success = False

                elif isinstance(block, ToolUseBlock):
                    # Show tool usage in checklist for transparency
                    cmd = ""
                    if hasattr(block, "input") and isinstance(block.input, dict):
                        cmd = str(block.input.get("command",
                                   block.input.get("file_path",
                                   block.name)))[:60]
                    if cmd:
                        await checklist.update_step(current_step, cmd)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("bootstrap.master_agent_failed", error=str(e))
        return False

    return success
```

### Step 3: Simplify `bootstrap_project()` main function

Replace lines ~1272-1378 (the complex step-by-step Python logic) with:

```python
# ── Steps 2-6: Master agent handles everything ──
await checklist.start_step(2)
agent_success = await _run_master_agent(
    checklist, project, workspace, compose, compose_project, port,
)

if not agent_success:
    await _bail("Setup failed — check diagnosis above")
    return

# ── Step 7: Create public URL (Python — needs tunnel service) ──
# ... existing tunnel code stays unchanged ...
```

This replaces:
- `_step_agentic_setup()` call + Python validation (lines 1272-1293)
- Build step Python fallback (lines 1296-1324)
- `_step_docker_up()` call + bail (lines 1326-1330)
- `_step_agent_migrations()` call (lines 1332-1334)
- App verify curl loop (lines 1336-1362)

### Step 4: Import Docker MCP helper

Add at top of bootstrap.py:
```python
from taghdev.providers.llm.claude import _mcp_docker
```

### Step 5: Remove dead code

After the master agent handles steps 2-6, these functions become unused in bootstrap.py:
- `_step_agentic_setup()` — absorbed into master agent prompt
- `_agent_fix_docker_config()` — master agent fixes docker itself
- `_step_agent_migrations()` — absorbed into master agent prompt

Keep them if they're used elsewhere, or mark as deprecated.

**Keep `_step_docker_up()` and `_find_app_container()`** — they may still be used by health tasks or other flows.

## What the User Sees (Telegram)

```
Setting up my-laravel-app
━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Clone repository — cloned main
✅ Setup environment — .env created
🔄 Install dependencies — reading composer.json...
🔄 Install dependencies — dockerized project, skipping host install
✅ Install dependencies — skipped (Docker handles deps)
⏭️ Build frontend assets — no build script
🔄 Start Docker containers — running compose up...
⚠️ Start Docker containers — DIAGNOSIS: selenium/standalone-chrome has no ARM64 image
🔧 Start Docker containers — replacing with seleniarm/standalone-chromium
🔄 Start Docker containers — retrying compose up...
✅ Start Docker containers — 4/4 containers running
🔄 Run database migrations — detected Laravel, running artisan migrate
✅ Run database migrations — 23 tables created + seeded
🔄 Verify app — curl localhost:80 inside app container
✅ Verify app — HTTP 200
🔄 Create public URL — starting tunnel...
✅ Create public URL — https://my-app.trycloudflare.com

✅ Project ready!
[Open App] [Health Check] [Main Menu]
```

The key difference: the user now sees **DIAGNOSIS** and **ACTION** lines — Claude's reasoning is visible.

## Verification

1. **Retry Bootstrap** from Telegram on an ARM Mac with a project that has selenium
   - Should see DIAGNOSIS about ARM + ACTION about swapping image
2. Test with a project missing .env vars
   - Should see DIAGNOSIS about missing vars + ACTION adding them
3. Test with a non-dockerized project
   - Should see deps actually installed on host
4. Test timeout: if agent takes too long (>60 turns), Python should catch and bail
5. Test CancelledError: agent should clean up properly
6. Verify doctor.py still works independently for health_task.py periodic repairs
