# TAGH Dev — Implementation Plan & Architecture Audit

## Table of Contents
1. [Current State](#current-state)
2. [What's Built](#whats-built)
3. [What's Missing](#whats-missing)
4. [Agent System Architecture](#agent-system-architecture)
5. [MCP Architecture](#mcp-architecture)
6. [Chat AI Assistant](#chat-ai-assistant)
7. [Voice Support](#voice-support)
8. [Self-Service Project Onboarding](#self-service-project-onboarding)
9. [Docker Integration](#docker-integration)
10. [Error Correction Pipeline](#error-correction-pipeline)
11. [Provider System](#provider-system)
12. [Security Model](#security-model)
13. [Audit — Weaknesses & Fixes](#audit--weaknesses--fixes)
14. [Implementation Order](#implementation-order)

---

## Current State

### What's Running
```
✅ PostgreSQL         — healthy, task DB
✅ Redis              — healthy, task queue + FSM storage
✅ Bot (Telegram)     — healthy, polling, responds to commands
✅ API (FastAPI)      — healthy, port 8000
✅ Worker (arq)       — healthy, 7 functions registered
✅ Claude Code        — logged in, Max $200 subscription
✅ User authorized    — Telegram ID 7671632701
```

### What's NOT Working
```
❌ Chat AI — bot ignores regular text/voice messages (command-only)
❌ Voice messages — no transcription service
❌ /addproject — onboarding agent not fully tested
❌ Docker MCP — built but not integrated into agent invocations
❌ GitHub MCP — built but not integrated
❌ Actions MCP — not built yet
❌ Task pipeline — not tested end-to-end
```

---

## What's Built

### Files Created (60+ files)
```
src/taghdev/
├── settings.py                        ✅ bootstrap config from .env
├── models/
│   ├── base.py                        ✅ SQLAlchemy async engine
│   ├── config.py                      ✅ PlatformConfig (provider settings in DB)
│   ├── project.py                     ✅ Project model (Docker fields included)
│   ├── task.py                        ✅ Task + TaskLog models
│   └── user.py                        ✅ Provider-agnostic user model
├── providers/
│   ├── base.py                        ✅ Abstract: LLMProvider, ChatProvider, GitProvider
│   ├── registry.py                    ✅ Pluggable provider registration
│   ├── factory.py                     ✅ Cached provider instantiation from DB
│   ├── llm/claude.py                  ✅ Claude provider (planner + coder + reviewer)
│   ├── chat/telegram/__init__.py      ✅ Telegram provider (polling, debounced notifs)
│   └── git/github.py                  ✅ GitHub provider (gh CLI)
├── agents/
│   ├── coder.py                       ✅ Legacy wrapper (logic moved to claude provider)
│   ├── reviewer.py                    ✅ Legacy wrapper
│   ├── onboarding.py                  ✅ Auto-detect project config from repo
│   └── doctor.py                      ✅ Diagnose/fix Docker container issues
├── mcp_servers/
│   ├── project_info.py                ✅ In-process: get_project_info
│   ├── docker_mcp.py                  ✅ Docker: containers, logs, exec, compose
│   ├── github_mcp.py                  ✅ GitHub: list repos, check access
│   └── actions_mcp.py                 ❌ NOT BUILT — triggers tasks from chat
├── bot/
│   ├── main.py                        ✅ Entrypoint (delegates to chat provider)
│   ├── handlers/start.py              ✅ /start, /help, /projects, /status, /cancel
│   ├── handlers/task.py               ✅ /task FSM flow
│   ├── handlers/review.py             ✅ Approve/reject/merge/discard callbacks
│   ├── handlers/admin.py              ✅ /addproject, /adduser, /removeproject
│   ├── handlers/chat.py               ❌ NOT BUILT — catch-all AI text + voice
│   ├── keyboards.py                   ✅ Inline keyboard builders
│   ├── states.py                      ✅ FSM states
│   └── middlewares/auth.py            ✅ User allowlist middleware
├── worker/
│   ├── arq_app.py                     ✅ arq worker settings
│   └── tasks/
│       ├── orchestrator.py            ✅ Full pipeline (plan → code → review → PR)
│       ├── git_ops.py                 ✅ Clone, branch, push, PR (subprocess)
│       └── onboarding.py             ✅ Project onboarding task
├── services/
│   ├── config_service.py              ✅ Read/write platform config from DB
│   ├── github_service.py              ✅ PR create/merge (legacy, replaced by provider)
│   ├── project_service.py             ✅ Project CRUD
│   ├── notification.py                ✅ Debounced Telegram notifications
│   ├── workspace_service.py           ✅ Git worktree + dep caching
│   ├── docker_service.py              ✅ Docker container management
│   ├── transcription.py              ❌ NOT BUILT — faster-whisper
│   └── ai_chat.py                    ❌ NOT BUILT — Claude chat responses
└── setup/
    └── __main__.py                    ✅ Interactive setup wizard
```

---

## What's Missing

### Priority 1 — Must Build Now
| Feature | Files | Description |
|---------|-------|-------------|
| **Actions MCP** | `mcp_servers/actions_mcp.py` | Lets chat agent trigger tasks, add projects, check status |
| **Chat Handler** | `bot/handlers/chat.py` | Catch-all for text + voice messages → Claude AI response |
| **AI Chat Service** | `services/ai_chat.py` | Claude subprocess wrapper for chat responses |
| **Transcription** | `services/transcription.py` | faster-whisper voice → text |
| **Dockerfile update** | `Dockerfile.app` | Add ffmpeg + faster-whisper |

### Priority 2 — Test & Fix
| Feature | Files | Description |
|---------|-------|-------------|
| Full task pipeline E2E | orchestrator.py | Test: /task → plan → approve → code → review → PR |
| Project onboarding E2E | onboarding.py | Test: /addproject → clone → analyze → add |
| Claude credentials persist | docker-compose.yml | Verify volume mount survives restarts |
| Docker project startup | workspace_service.py | Test: docker compose up for user's project |

---

## Agent System Architecture

### 6 Agents — Each With Specific Role and Access

```
┌─── PLANNER ─────────────────────────────────────────────────┐
│  ROLE: Analyze codebase, create implementation plan          │
│  ACCESS: Read, Glob, Grep (read-only)                        │
│  MCPs: none (just reads files)                               │
│  OUTPUT: Plan text → sent to Telegram for approval           │
│  TRIGGER: When task is submitted                             │
└──────────────────────────────────────────────────────────────┘

┌─── CODER ───────────────────────────────────────────────────┐
│  ROLE: Implement the approved plan step-by-step              │
│  ACCESS: Read, Write, Edit, Bash, Glob, Grep (FULL)         │
│  MCPs: Git, Docker, Playwright, GitHub                       │
│  OUTPUT: Code changes + STEP_DONE markers + DONE_SUMMARY     │
│  TRIGGER: When user approves the plan                        │
└──────────────────────────────────────────────────────────────┘

┌─── REVIEWER ────────────────────────────────────────────────┐
│  ROLE: Review code for quality, security, conventions        │
│  ACCESS: Read, Glob, Grep (READ-ONLY)                        │
│  MCPs: Git (diff/status only)                                │
│  OUTPUT: APPROVED or ISSUES list → back to Coder if issues   │
│  TRIGGER: After Coder finishes                               │
└──────────────────────────────────────────────────────────────┘

┌─── CHAT ────────────────────────────────────────────────────┐
│  ROLE: Conversational AI assistant                           │
│  ACCESS: Read (via MCPs only)                                │
│  MCPs: Actions, Docker, GitHub, Project-Info                 │
│  OUTPUT: Text response + optional action triggers            │
│  TRIGGER: Any text/voice message not caught by commands      │
└──────────────────────────────────────────────────────────────┘

┌─── ONBOARDING ──────────────────────────────────────────────┐
│  ROLE: Analyze new repos, detect Docker setup, tech stack    │
│  ACCESS: Read, Glob, Grep (read-only on cloned repo)         │
│  MCPs: Docker, GitHub                                        │
│  OUTPUT: PROJECT_CONFIG struct → sent to user for approval   │
│  TRIGGER: /addproject command                                │
└──────────────────────────────────────────────────────────────┘

┌─── DOCTOR ──────────────────────────────────────────────────┐
│  ROLE: Diagnose and fix Docker container failures            │
│  ACCESS: Read, Write, Edit, Bash (FULL)                      │
│  MCPs: Docker                                                │
│  OUTPUT: FIXED or UNFIXABLE                                  │
│  TRIGGER: When containers are unhealthy during task setup    │
└──────────────────────────────────────────────────────────────┘
```

---

## MCP Architecture

### 6 MCP Servers — All Connected to Agents

```
┌─── EXISTING (install only) ─────────────────────────────────┐
│                                                              │
│  Git MCP (mcp-server-git, PyPI)                              │
│  ├── git_status, git_diff_staged, git_diff_unstaged          │
│  ├── git_add, git_commit, git_create_branch, git_checkout    │
│  ├── git_log, git_show, git_branch, git_reset                │
│  └── Used by: Coder, Reviewer                                │
│                                                              │
│  Playwright MCP (@playwright/mcp, npm)                       │
│  ├── browser_navigate, browser_click, browser_fill_form      │
│  ├── browser_snapshot, browser_take_screenshot                │
│  ├── browser_evaluate, browser_press_key                     │
│  └── Used by: Coder                                          │
│                                                              │
└──────────────────────────────────────────────────────────────┘

┌─── CUSTOM (we built) ──────────────────────────────────────┐
│                                                              │
│  Docker MCP (taghdev.mcp_servers.docker_mcp)                │
│  ├── list_containers, container_logs, container_health       │
│  ├── restart_container, docker_exec                          │
│  ├── compose_up, compose_down, compose_ps                    │
│  └── Used by: Coder, Chat, Onboarding, Doctor                │
│                                                              │
│  GitHub MCP (taghdev.mcp_servers.github_mcp)                │
│  ├── list_repos, repo_info, list_branches, list_prs          │
│  ├── check_repo_access                                       │
│  └── Used by: Chat, Onboarding                               │
│                                                              │
│  Project Info MCP (taghdev.mcp_servers.project_info)        │
│  ├── get_project_info, get_coding_conventions                │
│  └── Used by: Coder, Chat                                    │
│                                                              │
│  Actions MCP (taghdev.mcp_servers.actions_mcp) ❌ NOT BUILT │
│  ├── trigger_task, trigger_addproject                         │
│  ├── list_projects, list_tasks, system_status                │
│  └── Used by: Chat (enables natural language → actions)      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### Which Agent Gets Which MCPs

| Agent | Git | Playwright | Docker | GitHub | Project | Actions |
|-------|-----|------------|--------|--------|---------|---------|
| Planner | — | — | — | — | — | — |
| Coder | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Reviewer | ✅ (read) | — | — | — | — | — |
| Chat | — | — | ✅ | ✅ | ✅ | ✅ |
| Onboarding | — | — | ✅ | ✅ | — | — |
| Doctor | — | — | ✅ | — | — | — |

---

## Chat AI Assistant

### Architecture
```
User sends text/voice on Telegram
        │
        ▼
Command handlers try FIRST (/task, /addproject, etc.)
        │
        ▼ (no command matched)
Chat handler (catch-all, registered LAST)
        │
        ├── If voice: faster-whisper transcribes → text
        │
        ▼
Claude Chat Agent (via Claude Agent SDK)
├── System prompt: TAGH Dev mission, available actions, personality
├── Context injected: connected projects, active tasks, system health
├── MCPs: Actions, Docker, GitHub, Project-Info
│
├── User: "hello"
│   → Claude: "Hey! I'm TAGH Dev, your dev assistant. Send /task or
│              just tell me what you need."
│
├── User: "what's running?"
│   → Claude uses Docker MCP → checks containers → responds with status
│
├── User: "fix the login bug on trade-bot"
│   → Claude uses Actions MCP → trigger_task("trade-bot", "fix login bug")
│   → "Task created! I'll analyze the code and send you a plan."
│
├── User: "add my new repo github.com/me/new-app"
│   → Claude uses Actions MCP → trigger_addproject(url)
│   → "Cloning and analyzing... I'll send you the config shortly."
│
└── User sends voice: "I need to add authentication"
    → faster-whisper: "I need to add authentication"
    → Claude: "Which project? Here are your connected projects: [trade-bot]"
```

### System Prompt
```
You are TAGH Dev's AI assistant — a humble, senior-level DevOps and
development expert. You help developers manage their projects through
natural conversation.

PERSONALITY:
- Concise and professional
- Proactive — suggest actions when the user's intent is clear
- Honest about limitations
- Never overwhelming with information

CAPABILITIES (via MCP tools):
- Create development tasks (trigger_task)
- Add projects from GitHub (trigger_addproject)
- List projects and tasks (list_projects, list_tasks)
- Check system health (system_status)
- Check Docker container status (Docker MCP)
- Browse GitHub repos (GitHub MCP)

CONTEXT:
Connected projects: {projects}
Active tasks: {active_tasks}
System: {system_health}

RULES:
- If user describes work to do → ask which project, then trigger_task
- If user asks about status → use system_status or list_tasks
- If user mentions a repo URL → suggest trigger_addproject
- For simple questions → respond directly without tools
- Keep responses SHORT — this is Telegram, not email
```

### Files to Build
```
src/taghdev/mcp_servers/actions_mcp.py    — trigger_task, list_projects, etc.
src/taghdev/services/ai_chat.py           — wraps Claude for chat responses
src/taghdev/services/transcription.py     — faster-whisper voice → text
src/taghdev/bot/handlers/chat.py          — catch-all text + voice handler
```

---

## Voice Support

### Technology: faster-whisper
```
Package: faster-whisper (pip)
Model: tiny (75MB download, ~1 sec per message)
Runs: inside bot container (local, no internet, free forever)
Dependency: ffmpeg (converts Telegram .ogg → .wav)
```

### Flow
```
1. Telegram sends voice message as .ogg file
2. Bot downloads .ogg via Telegram API
3. ffmpeg converts .ogg → .wav (subprocess)
4. faster-whisper transcribes .wav → text (local AI model)
5. Transcribed text → Chat Agent (same as text messages)
6. Claude responds → bot sends text reply
7. Cleanup temp files
```

### Dockerfile.app Changes
```dockerfile
# Add ffmpeg for audio conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

### pyproject.toml Changes
```toml
dependencies = [
    ...existing...
    "faster-whisper>=1.0",
]
```

---

## Self-Service Project Onboarding

### /addproject Flow
```
USER: /addproject

BOT fetches GitHub repos (via gh CLI):
  [ahmedMAfani/trade-bot] ← from GitHub API
  [ahmedMAfani/webapp]
  ─────────────────────
  [trade-bot ✅ already connected]
  [webapp — not connected]
  ─────────────────────
  [Enter URL manually]

USER taps [webapp]

BOT: "Cloning and analyzing..."

ONBOARDING AGENT (Claude, read-only):
  - Reads docker-compose.yml → detects services, ports
  - Reads Dockerfile → detects framework
  - Reads requirements.txt/package.json → detects tech stack
  - Reads README.md → gets description

BOT: "Project analyzed!
      Name: webapp
      Tech: Laravel 11, Vue 3, Tailwind
      Docker: Yes (docker-compose.yml)
      Container: app (port 8000)
      [Add Project] [Cancel]"

USER taps [Add Project]

BOT: "Project 'webapp' added! Ready for /task."
```

### How It Works Internally
1. `/addproject` handler lists repos from GitHub (gh CLI)
2. Shows already-connected projects with ✅ status badge
3. On selection: enqueues `onboard_project` arq task
4. Worker clones repo to temp dir
5. Onboarding Agent (Claude) analyzes the repo structure
6. Agent outputs structured config (PROJECT_CONFIG_START/END)
7. Config stored temporarily in Redis (1 hour TTL)
8. User sees config preview with [Add Project] button
9. On confirm: saved to `projects` DB table
10. Temp clone cleaned up

---

## Docker Integration

### How User's Projects Run
```
User's projects ARE Docker-based. When a task runs:

1. Workspace Manager clones the project
2. docker compose up the PROJECT'S stack (via Docker socket)
   ├── project-app    (PHP/Python/Node)
   ├── project-db     (MySQL/Postgres)
   └── project-redis  (Redis)
3. Claude edits files (mounted into containers)
4. Claude runs commands: docker exec project-app php artisan test
5. Playwright tests: http://localhost:<port>
6. After task: docker compose down (cleanup)
```

### Worker Container Has Docker Socket
```yaml
worker:
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - workspaces:/workspaces
    - claude_auth:/home/taghdev/.claude
```

### Project Model Fields
```
is_dockerized: bool          — whether project uses Docker
docker_compose_file: str     — path to docker-compose.yml (e.g., "infra/docker/docker-compose.yml")
app_container_name: str      — main app container (e.g., "aurora-api")
app_port: int                — app port (e.g., 8000)
```

---

## Error Correction Pipeline

### 3 Layers of Error Handling
```
LAYER 1: Self-correction (within Coder Agent)
  Claude writes code → runs tests → FAILS
  → Claude reads error → fixes → re-runs tests
  → Automatic, up to max_turns (50)
  → Example: missing import → adds import → tests pass

LAYER 2: Reviewer Agent catches remaining issues
  Coder finishes → Reviewer reads diff
  → "ISSUES: missing null check on line 42"
  → Sent back to Coder → fixes → re-reviewed
  → Up to 2 retry loops

LAYER 3: User approval
  All code done → user sees summary + diff on Telegram
  → [Create PR] or [Discard]
  → Nothing touches GitHub without user approval
```

### Docker Error Handling (Doctor Agent)
```
Project containers start → one is unhealthy
  → Doctor Agent reads logs
  → Diagnoses: "missing .env file"
  → Fixes: copies .env.example → .env
  → Restarts container
  → Verifies: healthy ✅
  → Continues with task
```

---

## Provider System

### Architecture
```
TAGH Dev is provider-agnostic. The core engine never imports
aiogram, claude_agent_sdk, or gh CLI directly.

Everything goes through abstract providers:

LLMProvider (abstract)
  └── ClaudeProvider (concrete) — Claude Agent SDK
  └── OpenAIProvider (future)

ChatProvider (abstract)
  └── TelegramProvider (concrete) — aiogram v3
  └── SlackProvider (future)

GitProvider (abstract)
  └── GitHubProvider (concrete) — gh CLI
  └── GitLabProvider (future)
```

### Configuration
```
Provider config stored in platform_config DB table (NOT .env files):

| category | key      | value                                    |
|----------|----------|------------------------------------------|
| llm      | provider | {"type": "claude", "coder_max_turns": 50}|
| chat     | provider | {"type": "telegram", "token": "xxx"}     |
| git      | provider | {"type": "github", "token": "xxx"}       |

Setup via: docker compose run setup (interactive wizard)
Or via Telegram: /adduser, /addproject
```

### Adding a New Provider
```
To add Slack support:
1. Create src/taghdev/providers/chat/slack.py
2. Implement ChatProvider abstract class
3. Decorate with @register_chat("slack")
4. Done — system auto-discovers it

Same pattern for OpenAI, GitLab, etc.
```

---

## Security Model

| Agent | Read files | Write files | Bash | Docker | Push to GitHub |
|-------|-----------|------------|------|--------|---------------|
| Planner | ✅ | ❌ | ❌ | ❌ | ❌ |
| Coder | ✅ | ✅ | ✅ | ✅ (exec) | ❌ |
| Reviewer | ✅ | ❌ | ❌ | ❌ | ❌ |
| Chat | via MCP | ❌ | ❌ | via MCP | ❌ |
| Doctor | ✅ | ✅ | ✅ | ✅ | ❌ |
| Orchestrator | ✅ | git only | ✅ | ✅ | ✅ (after approval) |

### Key Security Rules
- User must be in allowlist (checked every request)
- Claude credentials persist via Docker volume (auto-restore on restart)
- GitHub PAT stored in DB (platform_config), not .env
- No code pushed to GitHub without user approval (2 gates: diff preview + PR merge)
- Reviewer agent is READ-ONLY (cannot modify files)
- Workspace isolation: each task gets its own directory
- Project Docker containers isolated per task (unique compose project name)

---

## Audit — Weaknesses & Fixes

### Critical (Fixed)
| Issue | Fix |
|-------|-----|
| Used ANTHROPIC_API_KEY (wrong) | Use claude login + Docker volume for Max subscription |
| Celery asyncio conflicts | Switched to arq (native async) |
| Claude credentials lost on restart | Entrypoint script + claude_auth volume |

### High (Fixed)
| Issue | Fix |
|-------|-----|
| 3 agents wasteful | 2 agents (coder+reviewer) + direct Python for PR |
| cp -r slow for large repos | git worktree add (instant) |
| Telegram rate limits | Debounced notifier (3 sec interval) |
| No diff preview before PR | Plan → approve → code → review → summary → PR |
| Single fat image for all | Split Dockerfile.app (slim) + Dockerfile.worker (fat) |

### High (Still Open)
| Issue | Fix Needed |
|-------|-----------|
| Bot ignores text/voice | Build chat handler + transcription |
| No AI conversational layer | Build Chat Agent with Actions MCP |
| Task pipeline untested E2E | Need full end-to-end test |
| Project onboarding untested | Need /addproject E2E test |

### Medium (Open)
| Issue | Fix Needed |
|-------|-----------|
| No conversation memory | Store last N messages per user in Redis |
| No task cancellation tested | Test /cancel with running arq job |
| No disk cleanup cron | Add periodic workspace cleanup task |
| No web dashboard | Future: React/Vue admin panel |

---

## Implementation Order

### NOW — Priority 1 (Chat AI Module)
```
1. Create actions_mcp.py        — trigger_task, list_projects, system_status
2. Create transcription.py      — faster-whisper wrapper
3. Create ai_chat.py            — Claude chat agent invocation
4. Create chat.py handler       — catch-all text + voice handler
5. Update Dockerfile.app        — add ffmpeg + faster-whisper
6. Register chat handler LAST   — in telegram provider
7. Rebuild + test               — "hello" → AI response, voice → transcribe → AI
```

### NEXT — Priority 2 (E2E Testing)
```
8. Test /addproject E2E         — with trade-bot repo
9. Test /task E2E               — plan → approve → code → review → PR
10. Test Docker project startup  — docker compose up inside workspace
11. Test Claude credentials persist — restart worker, verify logged in
```

### LATER — Priority 3 (Polish)
```
12. Conversation memory          — Redis-backed last N messages
13. Task cancellation testing    — /cancel with running jobs
14. Workspace cleanup cron       — periodic stale workspace removal
15. Error monitoring             — structured logs + alerts
16. Web dashboard                — future
```
