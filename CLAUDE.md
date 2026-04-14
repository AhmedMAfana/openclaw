# CLAUDE.md — OpenClow

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
