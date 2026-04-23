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
