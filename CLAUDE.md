# CLAUDE.md — TAGH Dev

## Project

Python 3.12 async project: AI Dev Orchestrator with Telegram/Slack bots, arq workers, FastAPI dashboard.

## Commands

```bash
# Typecheck
python -m py_compile src/openclow/path/to/file.py

# Restart services (code changes only — no rebuild needed)
docker compose restart bot worker

# Rebuild + start (if dependencies or Dockerfile changed)
docker compose up bot worker --build

# Full stack with rebuild
docker compose up --build

# Check logs
docker compose logs -f bot worker
docker compose logs api worker | grep -E "error|Error|tunnel"
```

**📌 Important:** Only use `--build` if you modified `pyproject.toml`, `Dockerfile.worker`, or `Dockerfile.app`. For Python code changes only, use `docker compose restart bot worker` (5x faster).

## CRITICAL RULES — Read Every Time

### 1. NEVER say "done" without verification

Before claiming ANY task is complete, you MUST:

1. **Trace the full path**: user action → handler → service → worker → response back to user
2. **Verify imports exist**: Every function you call must exist in the target module. `grep` for it.
3. **Verify registration**: New handlers/routes/modules must be imported and registered. Check the wiring code.
4. **Run the code if possible**: `python -m py_compile` at minimum. Start the service if you can.
5. **If you CANNOT test it**, say explicitly: "I cannot verify this works. Here's what needs manual testing: [list]"

NEVER claim done based on: "syntax checks pass", "the code looks correct", "all files compile".

### 2. Connect the pieces

When a feature spans multiple files:

- Trace: Does the handler import the right function? Does it pass the right args? Does the service return what the handler expects?
- Check: Is the new module registered in `__init__.py`, `start_bot()`, `register()`, or wherever modules are wired?
- Verify: `grep` for the function name across the codebase to confirm it's called correctly.

### 3. Test what you build

After writing code:
- Run `python -m py_compile` on every changed file
- If it's a handler: trace the action_id/callback_data from button → handler → service → response
- If it's a worker task: verify it's registered in `arq_app.py` functions list
- If it's a model change: check all queries that use that model still work

### 4. No half-features

If you can't finish something end-to-end, don't start it. A feature that's 80% done is 0% useful.

### 5. Report honestly

- "Done and verified" = you tested it
- "Done but unverified" = you wrote it, can't test it, here's what to check
- "Partially done" = here's what works, here's what's left

### 6. Fresh context for complex tasks

If a task requires 3+ files changed, use Plan Mode first. Break into small tasks. Complete and verify each before moving on.

## Architecture Quick Reference

```
src/openclow/
├── api/              # FastAPI dashboard
├── bot/              # Legacy telegram aliases (use providers/ instead)
├── models/           # SQLAlchemy: Project, Task, User, PlatformConfig, TaskLog
├── providers/
│   ├── actions.py    # Platform-agnostic ActionKeyboard/ActionButton
│   ├── base.py       # ChatProvider abstract base
│   ├── factory.py    # Provider singleton factory
│   ├── registry.py   # Provider type registry
│   └── chat/
│       ├── telegram/  # Telegram provider (aiogram)
│       └── slack/     # Slack provider (slack-bolt)
├── services/         # bot_actions, project_service, health_service, ai_chat, etc.
└── worker/
    ├── arq_app.py    # Worker entry + job registry
    └── tasks/        # All async jobs
```

**Data flow**: User → ChatProvider handler → bot_actions service → arq worker task → ChatProvider.edit_message (back to user)

**Key service**: `bot_actions` is the gateway. Handlers call it for all DB queries and job enqueuing. Don't query DB directly from handlers.

## Code Style

- Async everywhere (asyncio, async def, await)
- Type hints on function signatures
- Imports: stdlib → third-party → local, with `from __future__ import annotations`
- No docstrings on obvious functions. Comments only where logic isn't self-evident.
- 2-space indent in templates, 4-space in Python

## Agentic Design

- **LLM agents decide, never hardcoded regex/if-elif.** Complex task routing, error diagnosis, and repair must go through Claude agents — not scripted logic. The only exception is user-initiated actions (approve/reject buttons map to specific jobs via `_REVIEW_ACTIONS`).
- **No Bash tool in agent configs** — forces agents to use MCP tools (Docker, Git, Playwright) which have graceful error handling and don't crash the SDK.
- **Agent Never Gives Up**: The word "UNFIXABLE" does not exist. When a command fails, read the error, try a completely different approach. When that fails too, try another. Keep going until fixed or out of turns. Never repeat a failed approach.
- **Empty diff = retry**: If the coder agent produces no file changes, auto-retry with a stronger prompt before reporting failure to the user.
- **No Dead Ends**: Every error message MUST include at least a Main Menu button. Slack: use `error_blocks()`. Telegram: use `_error_keyboard()`. Never show bare text errors without navigation buttons.
- **Shared agent utilities**: use `worker/tasks/_agent_base.py` for tool descriptions (`describe_tool`), auth error detection (`is_auth_error`), and common patterns. Don't copy-paste agent boilerplate.
- **MCP factories live in `providers/llm/claude.py`**: `_mcp_docker()`, `_mcp_playwright()`, `_mcp_git()`. Import these, don't inline MCP server configs.

## Architecture-fitness suite

Static, principle-mapped checks that prove the project's intended
architectural properties hold. Run before claiming any feature done
and before any expensive test session:

```bash
python scripts/pipeline_fitness.py            # Markdown report
python scripts/pipeline_fitness.py --json     # machine output
python scripts/pipeline_fitness.py --check stream_event_contract
```

Or via the slash command: `/pipeline-audit`.

### Layered architecture

The suite implements a four-layer contract enforcement strategy
(borrows from Neal Ford's *Building Evolutionary Architectures* —
fitness functions as automated guardrails):

| Layer | Where it lives | What it catches |
|---|---|---|
| **Schema** (source of truth) | `specs/001-per-chat-instances/contracts/*.schema.json` | The contract itself — every event/job/tool name with its full payload shape. Single source of truth. |
| **Codegen** (build-time) | `scripts/codegen/gen_*.py` → `chat_frontend/src/types/*.ts` | TypeScript discriminated unions generated from schema. The frontend's `switch` becomes exhaustive at compile time — adding a new event in the schema breaks `tsc` until every consumer adds a `case`. |
| **Runtime** (emit-time) | `src/openclow/services/stream_validator.py` | Every `controller.add_data` payload validated against the schema. Strict mode raises (dev/test); warn mode logs telemetry (prod). |
| **Static audit** (CI gate) | `scripts/fitness/check_*.py` + `scripts/pipeline_fitness.py` | Cross-checks schema ↔ runtime ↔ codegen ↔ frontend ↔ backend, plus other contract surfaces (ARQ jobs, MCP tool args, compose ports, httpx timeouts, redactor coverage). |

### Today's fitness checks

Each maps to one or more constitution principles (Roman numerals).

| Check | Principle(s) | Asserts |
|---|---|---|
| `stream_event_contract` | VII, VIII | Backend `add_data` events ↔ JSON schema ↔ runtime validator's `_REQUIRED_BY_TYPE` ↔ generated TS types ↔ frontend `parseStream` handlers all align. |
| `arq_job_contract` | VII, VI | Every `enqueue_job("X", ...)` name is in `arq_app._load_functions`. |
| `api_route_contract` | VII | Every frontend `fetch('/api/X')` URL is served by some FastAPI route (path-parameter aware). |
| `mcp_tool_contract` | III, VII | `CONTAINER_MODE_TOOLS` strings ↔ `@mcp.tool()` registrations on the pinned MCP servers. |
| `db_model_drift` | VI, VII | SQLAlchemy `mapped_column` declarations match alembic migration history; optional live `alembic check`. |
| `no_ambient_args` | III | No `@mcp.tool` parameter name contains `instance`/`project`/`workspace`/`container`. |
| `compose_no_host_ports` | V | No service in any per-instance compose template publishes host ports outside `cloudflared`. |
| `redactor_coverage` | IV | Every `tool_result` emit wraps `content` in `redact()`. |
| `timeouts` | IX | Every `httpx.AsyncClient(...)` in `services/` carries a `timeout=` kwarg. |

### Adding a new fitness function

When a new contract surface appears in the system, drop a file
under `scripts/fitness/check_<name>.py` exporting `check() ->
FitnessResult`. The runner discovers it. Map it to constitution
principle(s) via the `principles=[...]` field. Keep each check ≤200
lines, ≤2 s offline, deterministic.

### Pre-commit wiring

`.pre-commit-config.yaml::pipeline-fitness` runs the suite with
`--fail-on critical` — only hard violations block commits, while
HIGH-severity drift is surfaced in the report (so a Phase 10
work-in-progress branch isn't gated by frontend handlers that are
deliberately scoped for a follow-up commit). Codegen freshness is
its own hook so a schema bump without a regen also fails locally.

### What this suite is NOT

Not a runtime monitor. Not a replacement for tests. Not a security
scanner. It catches "A calls B but B doesn't know about it" — not
"A calls B and B does the wrong thing". For behavioural correctness,
the test suites under `tests/` (unit, contract, integration, load)
are still the gate.

## Live end-to-end pipeline test

Where `/pipeline-audit` is the static gate, **`/e2e-pipeline`** is the
live gate. It drives a real chat through the full per-chat-instances
pipeline via Playwright MCP and proves the system actually works —
not just that the UI state machine renders the right banners.

```bash
# What it does, phase by phase:
#   0. preflight        — runs /pipeline-audit + scripts/e2e/preflight.py
#   1. pick-project     — picks a container-mode project from the DB
#   2. new-chat         — opens chat frontend, creates a chat
#   3. provision        — sends first message, watches provision land
#   4. app-live         — opens the tunnel URL, verifies real HTML loads
#   5. workspace-edit   — writes a marker file via workspace MCP
#   6. hmr              — verifies Vite HMR pushed the change to the live app
#   7. git-push         — commits + pushes via git MCP
#   8. multi-chat       — opens a second chat, verifies isolation
#   9. terminate        — /terminate, verifies DB + tunnel + container all gone
#   10. report          — writes REPORT.md with screenshots + log excerpts
```

Forensic capture: every phase boundary writes `services.txt`, `ps.txt`,
`api.log`, `worker.log`, `instance.log`, `instance.json`, `redis-keys.txt`
under `artifacts/e2e-<timestamp>/<phase>/` (gitignored). When a phase
fails, the skill classifies the failure, applies a hot-fix to keep
going, then a root-fix in code/template/Dockerfile, then re-runs the
phase. Both fixes are documented in the final report.

Helper scripts:
- `scripts/e2e/preflight.py` — JSON readiness probe (services up,
  cloudflare/github_app credentials in `platform_config`, container-mode
  project exists, playwright-mcp reachable, compose template present,
  fitness audit clean).
- `scripts/e2e/capture.sh` — phase artifact dump.

When to invoke: before cutting a release tag, after any change to
`instance_service.py`, `worker/tasks/instance_tasks.py`, the compose
templates, the MCP fleet, or the chat frontend's instance handlers.

## Playwright MCP — Claude Code setup

Playwright MCP runs **inside the worker container** (the host Mac has no
Node). The worker image bakes `@playwright/mcp` + Chromium at build
time (Dockerfile.worker lines 52-60) and publishes a stable binary at
`/usr/local/bin/playwright-mcp`. Two consequences:

1. **Worker must be running** for Claude Code to reach Playwright. If
   `docker ps` doesn't show `tagh-devops-worker-1`, the MCP will be
   listed as "Failed" in the Claude Code MCP dialog. Start the worker
   first: `docker compose up -d worker`.
2. **Don't use `npx @playwright/mcp@latest`** in the Claude Code
   registration — every launch triggers a registry check that races
   against Claude Code's short handshake timeout. Use the stable
   binary path.

To (re)register the MCP at user scope on a fresh machine:

```bash
claude mcp add --scope user playwright -- \
  docker exec -i tagh-devops-worker-1 /usr/local/bin/playwright-mcp --headless
```

Then restart Claude Code. Verify with `claude mcp list`; Playwright
should show green, not "Failed".

The worker-entrypoint.sh prints a loud warning at container startup if
the `playwright-mcp` binary or `PLAYWRIGHT_BROWSERS_PATH` is missing,
so a future Dockerfile regression doesn't silently break the MCP.

## Per-chat instance mode — quick reference (T090)

Every NEW web chat now runs in **container mode** by default: the chat
gets its own isolated Docker stack + Cloudflare named tunnel, and the
assistant agent sees ONLY a scoped MCP fleet bound to that one chat.
Legacy host/docker modes still work unchanged — the router in
`worker/tasks/bootstrap.py` branches on `project.mode`.

**Key code locations**
- `services/instance_service.py` — state-machine owner (`provision`,
  `get_or_resume`, `touch`, `terminate`, `record_upstream_*`). Every
  method injectable at every I/O seam; tests wire fakes via
  `tests/conftest.py::inmemory_service`.
- `worker/tasks/instance_tasks.py` — ARQ jobs: `provision_instance`
  (compose render → CF tunnel → GH token → compose up → projctl up),
  `teardown_instance` (idempotent subtract), `rotate_github_token`
  (fresh token → docker exec into `~/.git-credentials`),
  `tunnel_health_check_cron` (per-minute CF probe — FR-027a: never
  flips status to `failed`).
- `mcp_servers/instance_mcp.py` + `workspace_mcp.py` + `git_mcp.py` —
  the bounded-authority trio. Every tool argument-name is free of
  `instance`/`project`/`workspace`/`container` (Principle III / T033
  enforced).
- `providers/llm/claude.py` — pinned factories:
  `_mcp_instance(instance)`, `_mcp_workspace(instance)`,
  `_mcp_git_pinned(instance)`. Each takes the Instance row (not a
  loose identifier) so an LLM can't substitute a slug at call time.
  `CONTAINER_MODE_TOOLS` is the tool allowlist for container-mode
  chats.
- `setup/compose_templates/laravel-vue/` — v1's shipped template
  (compose.yml, cloudflared.yml, vite.config.js, guide.md,
  project.yaml, nginx.conf). No `ports:` outside `cloudflared`
  (Principle V). Runtime secrets come via `environment: - KEY`
  list-form so the renderer's `${VAR}` interpolation stays reserved
  for render-time variables.

**Entry points**
- Web chat: `api/routes/assistant.py::assistant_endpoint`. Wires
  `get_or_resume`, `touch`, the scoped MCP fleet, `/terminate`,
  `end_session_confirm:<id>`, `retry_provision:<id>`, and the
  provisioning / upstream-degraded / failure banners.
- Chat delete: `api/routes/threads.py::archive_thread` →
  `services/chat_session_service.py::delete_chat_cascade` (terminate
  + FK cascade + audit GC + branch GC).
- Internal APIs (compose-network only):
  `POST /internal/instances/<slug>/heartbeat` (projctl cadence 60s),
  `POST /internal/instances/<slug>/rotate-git-token` (projctl cadence
  45min), `POST /internal/instances/<slug>/explain` (projctl LLM
  fallback, arch §9).

**Crons**
- `inactivity_reaper` — every 5 min. Two-phase: running → idle +
  grace banner, idle → terminating after grace window.
- `tunnel_health_check` — every 60 s. CF tunnel probe; Redis-backed
  degradation state at `openclow:instance_upstream:<slug>:<cap>`
  with 180s TTL.

**Docs**: full spec lives in [specs/001-per-chat-instances/](specs/001-per-chat-instances/);
contract docs in [contracts/](specs/001-per-chat-instances/contracts/).

<!-- SPECKIT START -->
Active feature plan: [specs/001-per-chat-instances/plan.md](specs/001-per-chat-instances/plan.md)
Spec: [specs/001-per-chat-instances/spec.md](specs/001-per-chat-instances/spec.md) · Research: [research.md](specs/001-per-chat-instances/research.md) · Data model: [data-model.md](specs/001-per-chat-instances/data-model.md) · Contracts: [contracts/](specs/001-per-chat-instances/contracts/)
<!-- SPECKIT END -->
