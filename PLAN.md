# OpenClow — AI-Powered Development Orchestration Platform

## Confirmed Decisions
- **Language**: Python 3.12 (asyncio)
- **Hosting**: Local Docker development first
- **Projects**: 1-2 Laravel+Vue projects initially
- **Chat Platform**: Telegram only (Slack later)
- **Dep Caching**: Cache by default, auto-detect when fresh install needed
- **Claude Auth**: Max $200/month subscription via `CLAUDE_CODE_OAUTH_TOKEN` (NOT API key)
- **Agents**: 2 agents (coder + reviewer) — deployer is direct Python code (no AI needed)
- **Rate Limits**: Effectively none with Max $200 — weekly session reset
- **Task Queue**: arq (native asyncio) instead of Celery (avoids event loop conflicts)

---

## Context

**Problem:** Managing multiple Laravel + Vue projects requires manual coding, branch management, deployments, and PR workflows.

**Solution:** OpenClow — developers send tasks via Telegram, Claude Code agent implements changes, and the system handles the full PR lifecycle automatically.

---

## How to Set Up Prerequisites

### 1. Telegram Bot Token
```bash
# 1. Open Telegram, search for @BotFather
# 2. Send /newbot
# 3. Choose a name: "OpenClow Bot"
# 4. Choose a username: "openclow_bot"
# 5. BotFather gives you a token like: 7123456789:AAF1234567890abcdef
# 6. Put it in .env as TELEGRAM_BOT_TOKEN
```

### 2. Claude Code Login (Max $200 Subscription)
```bash
# You have Claude Max ($200/month) — no real rate limits, weekly session reset
# You already use Docker + SSH + `claude login` — same approach here.

# FIRST TIME ONLY (after docker compose up):
docker exec -it openclow-worker-1 bash
claude login
# → Opens browser → authenticate → done
# Credentials saved to /home/openclow/.claude/ (persistent Docker volume)
# Auto-refreshes weekly. Never need to do this again.

# NO setup-token needed. NO API key needed. Just your normal login.
```

### 3. GitHub Token
```bash
# Go to github.com → Settings → Developer Settings → Fine-grained PAT
# Create token with these permissions on your target repos:
#   - Contents: Read and Write
#   - Pull Requests: Read and Write
#   - Metadata: Read
# Put it in .env as GITHUB_TOKEN
```

### 4. How You Interact With It
```
You do NOT SSH into Docker containers.
You do NOT open a terminal.
You just talk to your Telegram bot:

Phone/Desktop Telegram → @openclow_bot → /task → describe what you want → done

Everything happens inside Docker automatically.
The bot sends you status updates and PR links.
```

---

## Architecture

```
┌──────────────────────── DOCKER COMPOSE NETWORK ─────────────────────────┐
│                                                                          │
│  ┌──────────┐   ┌──────────┐   ┌───────────┐   ┌────────────────────┐  │
│  │ postgres │   │  redis   │   │    bot     │   │       api          │  │
│  │  :5432   │   │  :6379   │   │ (aiogram)  │   │    (FastAPI)       │  │
│  │ task DB  │   │  queue   │   │  polling   │   │     :8000          │  │
│  └────▲─────┘   └────▲─────┘   └─────┬─────┘   └────────────────────┘  │
│       │               │               │                                  │
│       │               │        submits task                              │
│       │               │               ▼                                  │
│  ┌────┴───────────────┴───────────────────────────────────────────────┐  │
│  │                    WORKER CONTAINER (fat image)                     │  │
│  │   Python 3.12 + Node.js 20 + PHP 8.3 + Composer + gh CLI          │  │
│  │                                                                    │  │
│  │   arq picks up task from Redis queue                               │  │
│  │   ┌──────────────────────────────────────────────────────────┐     │  │
│  │   │ ORCHESTRATOR PIPELINE                                    │     │  │
│  │   │                                                          │     │  │
│  │   │ 1. Workspace Manager                                    │     │  │
│  │   │    git worktree add (instant, from cache)                │     │  │
│  │   │    check deps hash → install if changed                  │     │  │
│  │   │                                                          │     │  │
│  │   │ 2. CLAUDE AGENT (single invocation)                     │     │  │
│  │   │    Claude Agent SDK → Anthropic API (internet)           │     │  │
│  │   │    ├── Reads/writes code in /workspaces/task-xxx/        │     │  │
│  │   │    ├── Runs php artisan, npm build, tests (local)        │     │  │
│  │   │    ├── Uses Git MCP for status, diff, commit             │     │  │
│  │   │    └── Self-reviews before finishing                      │     │  │
│  │   │                                                          │     │  │
│  │   │ 3. SEND DIFF PREVIEW to Telegram                        │     │  │
│  │   │    [Create PR] [Discard] buttons                         │     │  │
│  │   │                                                          │     │  │
│  │   │ 4. ON APPROVAL: Direct Python code (no agent needed)    │     │  │
│  │   │    git push → gh pr create → notify user                 │     │  │
│  │   └──────────────────────────────────────────────────────────┘     │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
        │                          │
        ▼                          ▼
  Telegram API               GitHub API
  (send/receive msgs)    (push, PR, merge)
```

### Why 2 Agents + Direct Python (Not 3 Agents)

With Max $200 — no real rate limits, so we CAN use multiple agents.

| Component | Approach | Why |
|-----------|----------|-----|
| **Coder Agent** | Claude Agent SDK, max_turns=50 | Needs AI: reads code, writes code, runs tests |
| **Reviewer Agent** | Claude Agent SDK, max_turns=20 | Needs AI: reviews diff, catches bugs, checks patterns |
| **Deployer** | Direct Python code (NOT an agent) | Does NOT need AI: git push + gh pr create is deterministic code |

Using an AI agent to run `git push` and `gh pr create` is like using a surgeon to flip a light switch. 20 lines of Python do the same thing instantly.

---

## Tech Stack (Verified)

| Component | Technology | Package |
|-----------|-----------|---------|
| Language | Python 3.12 | |
| AI Agent | Claude Agent SDK | `claude-agent-sdk` |
| Auth | Pro/Max OAuth | `CLAUDE_CODE_OAUTH_TOKEN` via `claude setup-token` |
| Task Queue | arq (native asyncio) | `arq` |
| Telegram Bot | aiogram v3 | `aiogram>=3.4` |
| Database | PostgreSQL 16 | `postgres:16-alpine` |
| ORM | SQLAlchemy async | `sqlalchemy[asyncio]`, `asyncpg` |
| Migrations | Alembic | `alembic` |
| Settings | Pydantic Settings | `pydantic-settings` |
| API | FastAPI | `fastapi`, `uvicorn` |
| Git MCP | mcp-server-git | `mcp-server-git` (PyPI) |
| GitHub MCP | github-mcp-server | Go binary from GitHub |
| Custom MCP | MCP SDK | `mcp[cli]` |
| Logging | structlog | `structlog` |
| Containers | Docker Compose | |

### Why arq Instead of Celery

| | Celery | arq |
|---|---|---|
| Async support | Needs `asyncio.run()` bridge (risky) | Native asyncio |
| Setup | Complex (prefork, pools, concurrency) | Simple (Redis only) |
| For long tasks | Needs careful tuning | Works naturally |
| Dependencies | Heavy | Lightweight |
| Monitoring | Flower (extra service) | Built-in job results |

---

## Project Structure

```
openclow/
├── PLAN.md
├── pyproject.toml
├── .env.example
├── .env.bot                            # Bot-only secrets
├── .env.worker                         # Worker-only secrets
├── .gitignore
├── docker-compose.yml
├── docker-compose.override.yml         # Dev hot-reload
├── Dockerfile.app                      # Slim: Python only (bot, api, migrate)
├── Dockerfile.worker                   # Fat: Python + Node + PHP + Composer + gh
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 001_initial_schema.py
├── src/
│   └── openclow/
│       ├── __init__.py
│       ├── settings.py                 # Pydantic Settings
│       ├── models/
│       │   ├── __init__.py
│       │   ├── base.py                 # SQLAlchemy async engine + session
│       │   ├── task.py                 # Task model + status enum
│       │   ├── project.py             # Project model
│       │   └── user.py                # Telegram user allowlist
│       ├── bot/
│       │   ├── __init__.py
│       │   ├── main.py                # Entrypoint: Bot + Dispatcher + polling
│       │   ├── handlers/
│       │   │   ├── __init__.py
│       │   │   ├── start.py           # /start, /help, /projects, /status, /cancel
│       │   │   ├── task.py            # /task FSM flow
│       │   │   └── review.py          # Approve/reject/discard callbacks
│       │   ├── keyboards.py           # Inline keyboard builders
│       │   ├── states.py              # FSM states
│       │   └── middlewares/
│       │       └── auth.py            # User allowlist check
│       ├── api/
│       │   ├── __init__.py
│       │   ├── main.py                # FastAPI app
│       │   └── routes/
│       │       ├── health.py          # GET /health
│       │       └── tasks.py           # GET /tasks/{id}
│       ├── agents/
│       │   ├── __init__.py
│       │   ├── coder.py               # Agent 1: writes code, runs tests
│       │   └── reviewer.py            # Agent 2: reviews diff, catches bugs (read-only)
│       ├── mcp_servers/
│       │   ├── __init__.py
│       │   └── project_info.py        # In-process: get_project_info tool
│       ├── worker/
│       │   ├── __init__.py
│       │   ├── arq_app.py             # arq WorkerSettings
│       │   └── tasks/
│       │       ├── __init__.py
│       │       ├── orchestrator.py    # Main pipeline
│       │       └── git_ops.py         # Clone, push, PR (subprocess)
│       ├── services/
│       │   ├── __init__.py
│       │   ├── github_service.py      # PR create/merge via gh CLI
│       │   ├── project_service.py     # Project CRUD from DB
│       │   ├── notification.py        # Debounced Telegram updates
│       │   └── workspace_service.py   # git worktree + dep cache + locking
│       └── utils/
│           └── logging.py             # structlog JSON setup
├── tests/
│   ├── conftest.py
│   ├── test_worker/
│   └── test_services/
└── scripts/
    ├── seed_projects.py
    └── create_user.py
```

---

## Database Schema

### `users`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| telegram_id | BIGINT UNIQUE NOT NULL | |
| telegram_username | VARCHAR(255) | |
| is_allowed | BOOLEAN DEFAULT FALSE | |
| created_at | TIMESTAMP | |

### `projects`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| name | VARCHAR(100) UNIQUE NOT NULL | "webapp" |
| github_repo | VARCHAR(255) NOT NULL | "org/repo" |
| default_branch | VARCHAR(100) DEFAULT 'main' | |
| description | TEXT | For agent context |
| tech_stack | VARCHAR(255) | "Laravel 11, Vue 3" |
| agent_system_prompt | TEXT | Extra agent instructions |
| force_fresh_install | BOOLEAN DEFAULT FALSE | |
| setup_commands | TEXT | "cp .env.example .env" |
| created_at | TIMESTAMP | |

### `tasks`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | FK → users | |
| project_id | FK → projects | |
| description | TEXT NOT NULL | |
| status | VARCHAR(50) NOT NULL | See flow below |
| branch_name | VARCHAR(255) | |
| pr_url | VARCHAR(500) | |
| pr_number | INT | |
| arq_job_id | VARCHAR(255) | For cancellation |
| telegram_chat_id | BIGINT NOT NULL | |
| telegram_message_id | BIGINT | Message to edit |
| error_message | TEXT | |
| agent_turns | INT | Turns used |
| duration_seconds | INT | Total duration |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |

**Status flow:**
```
pending → preparing → coding → diff_preview → awaiting_approval
                                    ↓                    ↓
                                 discarded          pushing → merged
                                                         ↓
                                                       failed
                                                       rejected
```

### `task_logs`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| task_id | FK → tasks | |
| agent | VARCHAR(50) | coder/system |
| level | VARCHAR(20) | info/warning/error |
| message | TEXT | |
| metadata | JSONB | Tool calls, diffs, debug info |
| created_at | TIMESTAMP | |

---

## Docker Setup

### Dockerfile.app (slim — for bot, api, migrate)

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY src/ ./src/
COPY alembic.ini ./alembic/
COPY alembic/ ./alembic/
COPY scripts/ ./scripts/
RUN useradd -m openclow && chown -R openclow:openclow /app
USER openclow
CMD ["python", "-m", "openclow.bot.main"]
```

### Dockerfile.worker (fat — full dev environment)

```dockerfile
FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget ca-certificates gnupg openssh-client jq unzip \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# PHP 8.3 + Composer
RUN curl -fsSL https://packages.sury.org/php/apt.gpg \
      | tee /etc/apt/keyrings/sury-php.gpg > /dev/null \
    && echo "deb [signed-by=/etc/apt/keyrings/sury-php.gpg] https://packages.sury.org/php/ bookworm main" \
      | tee /etc/apt/sources.list.d/sury-php.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends \
       php8.3-cli php8.3-mbstring php8.3-xml php8.3-curl \
       php8.3-zip php8.3-mysql php8.3-pgsql php8.3-sqlite3 \
       php8.3-bcmath php8.3-intl php8.3-gd \
    && rm -rf /var/lib/apt/lists/*
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

# GitHub MCP Server binary
RUN curl -fsSL https://github.com/github/github-mcp-server/releases/latest/download/github-mcp-server-linux-amd64.tar.gz \
    | tar xz -C /usr/local/bin/ github-mcp-server

# Git identity
RUN git config --system user.email "openclow@bot.local" \
    && git config --system user.name "OpenClow Bot"

# Python app
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY src/ ./src/

# Workspace + Claude home
RUN mkdir -p /workspaces \
    && useradd -m -s /bin/bash openclow \
    && mkdir -p /home/openclow/.claude \
    && chown -R openclow:openclow /app /workspaces /home/openclow
USER openclow

CMD ["arq", "openclow.worker.arq_app.WorkerSettings"]
```

### docker-compose.yml

```yaml
x-app: &app
  build:
    context: .
    dockerfile: Dockerfile.app
  env_file: .env.common
  restart: unless-stopped
  depends_on:
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy

services:
  # ======== Infrastructure ========
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: openclow
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-openclow}
      POSTGRES_DB: openclow
    volumes:
      - postgres_data:/var/lib/postgresql/data
    # ports NOT exposed (dev override adds them)
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U openclow"]
      interval: 5s
      timeout: 3s
      retries: 5

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --requirepass ${REDIS_PASSWORD:-openclow}
    volumes:
      - redis_data:/data
    # ports NOT exposed
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD:-openclow}", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  # ======== Application ========
  bot:
    <<: *app
    env_file:
      - .env.common
      - .env.bot
    command: python -m openclow.bot.main
    deploy:
      resources:
        limits:
          memory: 512M
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import os,time; s=os.stat('/tmp/bot_health'); exit(0 if time.time()-s.st_mtime < 30 else 1)\""]
      interval: 30s
      timeout: 5s
      retries: 3

  api:
    <<: *app
    command: uvicorn openclow.api.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    deploy:
      resources:
        limits:
          memory: 512M
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 15s
      timeout: 5s
      retries: 3

  worker:
    build:
      context: .
      dockerfile: Dockerfile.worker
    env_file:
      - .env.common
      - .env.worker
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - workspaces:/workspaces
      - claude_auth:/home/openclow/.claude   # persists claude login
    deploy:
      resources:
        limits:
          memory: 4G
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import redis; r=redis.from_url('${REDIS_URL}'); r.ping()\""]
      interval: 60s
      timeout: 10s
      retries: 3

  # ======== Run once ========
  migrate:
    <<: *app
    command: alembic upgrade head
    restart: "no"

volumes:
  postgres_data:
  redis_data:
  workspaces:
  claude_auth:    # persists claude login credentials
```

### docker-compose.override.yml (Dev)

```yaml
services:
  bot:
    command: watchfiles "python -m openclow.bot.main" src/
    volumes:
      - ./src:/app/src:cached
    environment:
      - LOG_LEVEL=DEBUG

  api:
    command: uvicorn openclow.api.main:app --host 0.0.0.0 --port 8000 --reload
    volumes:
      - ./src:/app/src:cached

  worker:
    volumes:
      - ./src:/app/src:cached
      - ./workspaces_dev:/workspaces
    environment:
      - LOG_LEVEL=DEBUG

  postgres:
    ports:
      - "5432:5432"

  redis:
    ports:
      - "6379:6379"
```

### Environment Files (Split for Security)

**.env.common** (shared by all services)
```bash
DATABASE_URL=postgresql+asyncpg://openclow:openclow@postgres:5432/openclow
REDIS_URL=redis://:openclow@redis:6379/0
REDIS_PASSWORD=openclow
LOG_LEVEL=INFO
```

**.env.bot** (bot only)
```bash
TELEGRAM_BOT_TOKEN=7123456789:AAF1234567890abcdef
```

**.env.worker** (worker only)
```bash
TELEGRAM_BOT_TOKEN=7123456789:AAF1234567890abcdef
# Claude auth: NOT needed here — uses `claude login` inside container
# Credentials persisted via claude_auth Docker volume
GITHUB_TOKEN=github_pat_your-fine-grained-pat
GH_TOKEN=${GITHUB_TOKEN}
WORKSPACE_BASE_PATH=/workspaces
CLAUDE_CODER_MAX_TURNS=50
CLAUDE_REVIEWER_MAX_TURNS=20
```

---

## Orchestrator Pipeline (Corrected)

```python
# worker/tasks/orchestrator.py

async def execute_task(ctx: dict, task_id: str):
    """arq task — native async, no event loop bridging needed."""
    task = await get_task(task_id)
    log = structlog.get_logger().bind(task_id=task_id, project=task.project.name)
    notifier = DebouncedNotifier(interval=3.0)  # max 1 Telegram edit per 3 sec

    # Acquire per-project lock (prevent concurrent tasks on same project)
    lock = await acquire_project_lock(task.project_id, ttl=900)
    if not lock:
        await notifier.send(task, "Queued — another task on this project is running...")
        raise Retry(defer=30)  # arq retry after 30 sec

    try:
        # ── Step 1: Prepare workspace (git worktree, fast) ──
        await update_status(task, "preparing")
        await notifier.send(task, "Preparing workspace...")
        workspace = await workspace_service.prepare(task.project, task.id)
        await log_to_db(task, "system", "info", "Workspace ready", {
            "cache_hit": workspace.from_cache,
            "deps_updated": workspace.deps_changed,
        })

        # ── Step 2: Run Claude Agent (single invocation) ──
        await update_status(task, "coding")
        await notifier.send(task, "Agent working...")
        
        turn_count = 0
        last_diff_size = 0
        stall_count = 0

        async for message in agents.coder.run(workspace.path, task):
            turn_count += 1
            
            # Stream progress (debounced)
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        await notifier.send(task, f"Agent: {block.name}...")
            
            # Stall detection: every 10 turns, check if diff grew
            if turn_count % 10 == 0:
                diff_size = await git_ops.diff_size(workspace.path)
                if diff_size == last_diff_size:
                    stall_count += 1
                    if stall_count >= 2:  # 20 turns with no progress
                        raise AgentStalled("Agent made no progress for 20 turns")
                else:
                    stall_count = 0
                    last_diff_size = diff_size
            
            if isinstance(message, ResultMessage):
                await update_task(task, agent_turns=message.num_turns)

        # ── Step 3: Run Reviewer Agent ──
        await update_status(task, "reviewing")
        await notifier.send(task, "Reviewing changes...")
        
        review_result = await agents.reviewer.run(workspace.path, task)
        
        if review_result.has_issues:
            # Send back to coder to fix (max 2 retries)
            for retry in range(2):
                await notifier.send(task, f"Fixing review issues (attempt {retry+1})...")
                await agents.coder.run_fix(workspace.path, task, review_result.issues)
                review_result = await agents.reviewer.run(workspace.path, task)
                if not review_result.has_issues:
                    break

        # ── Step 4: Send diff preview to Telegram ──
        await update_status(task, "diff_preview")
        diff_summary = await git_ops.diff_stat(workspace.path)
        await notifier.send_diff_preview(task, diff_summary)
        # Bot sends: "Changes ready:\n{diff_summary}\n[Create PR] [Discard]"
        # User decision handled by review handler

        # Workspace stays alive until user approves/discards (TTL: 1 hour)
        await log_to_db(task, "system", "info", f"Diff preview sent. Turns: {turn_count}")

    except Exception as e:
        log.error("task.failed", error=str(e))
        await update_task(task, status="failed", error_message=str(e))
        await notifier.send(task, f"Failed: {str(e)}")
        # Capture debug info
        await log_to_db(task, "system", "error", str(e), {
            "git_status": await git_ops.status(workspace.path),
            "git_diff": await git_ops.diff(workspace.path),
        })
        await workspace_service.cleanup(task.id)
    finally:
        await release_project_lock(task.project_id)


async def approve_task(task_id: str):
    """Called when user clicks [Create PR] — direct Python, no agent."""
    task = await get_task(task_id)
    workspace = workspace_service.get_path(task.id)

    await update_status(task, "pushing")
    await git_ops.push(workspace, task.branch_name)

    pr_url = await github_service.create_pr(
        repo=task.project.github_repo,
        branch=task.branch_name,
        base=task.project.default_branch,
        title=f"[OpenClow] {task.description[:60]}",
        body=generate_pr_body(task),
    )

    await update_task(task, status="awaiting_approval", pr_url=pr_url)
    await notification.send_pr_created(task, pr_url)
    # Bot sends: "PR created! {url}\n[Merge] [Reject]"


async def merge_task(task_id: str):
    """Called when user clicks [Merge]."""
    task = await get_task(task_id)
    await github_service.merge_pr(task.project.github_repo, task.pr_number)
    await update_task(task, status="merged")
    await notification.send(task, "Merged successfully!")
    await workspace_service.cleanup(task.id)


async def reject_task(task_id: str):
    """Called when user clicks [Reject]."""
    task = await get_task(task_id)
    await github_service.close_pr(task.project.github_repo, task.pr_number)
    await git_ops.delete_remote_branch(task.project.github_repo, task.branch_name)
    await update_task(task, status="rejected")
    await notification.send(task, "Task rejected. PR closed.")
    await workspace_service.cleanup(task.id)
```

---

## Agent Configurations

### Agent 1: Coder (`agents/coder.py`)

```python
CODER_SYSTEM_PROMPT = """You are a senior developer working on "{project_name}" ({tech_stack}).

Project description: {description}
{agent_system_prompt}

RULES:
1. Read existing code first. Follow the project's patterns and conventions.
2. Create migrations with php artisan make:migration. Do NOT run php artisan migrate.
3. Install packages if needed (composer require, npm install).
4. Run npm run build to verify frontend compiles.
5. Run php artisan test to verify tests pass.
6. Stage all changes with git add.
7. Do NOT commit or push — the orchestrator handles that.
"""

async def run(workspace_path: str, task) -> AsyncIterator:
    from claude_agent_sdk import query, ClaudeAgentOptions

    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt=CODER_SYSTEM_PROMPT.format(...),
        allowed_tools=[
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
            "mcp__git__*",
            "mcp__project-info__*",
        ],
        mcp_servers={
            "git": {
                "command": "uvx",
                "args": ["mcp-server-git", "--repository", workspace_path],
            },
            "project-info": project_info_server,  # in-process MCP
        },
        permission_mode="bypassPermissions",
        max_turns=50,  # generous — Max $200 has no real rate limits
        setting_sources=["project"],  # reads CLAUDE.md from the project repo
    )

    async for message in query(prompt=task.description, options=options):
        yield message
```

### Agent 2: Reviewer (`agents/reviewer.py`)

```python
REVIEWER_SYSTEM_PROMPT = """You are a senior code reviewer for "{project_name}" ({tech_stack}).

Review the changes made for the task described below. Check for:
1. Security: SQL injection, XSS, CSRF, mass assignment vulnerabilities
2. Laravel best practices: proper validation, middleware, form requests
3. Vue best practices: reactivity, component structure, prop validation
4. Missing error handling or edge cases
5. Broken imports or unused code
6. Code style consistency with existing codebase
7. Missing or incorrect migrations
8. Test coverage — are the changes tested?

Output your review as:
- APPROVED: if changes look good
- ISSUES: list each issue with file path, line, and what to fix
"""

async def run(workspace_path: str, task) -> AsyncIterator:
    from claude_agent_sdk import query, ClaudeAgentOptions

    options = ClaudeAgentOptions(
        cwd=workspace_path,
        system_prompt=REVIEWER_SYSTEM_PROMPT.format(...),
        allowed_tools=[
            "Read", "Glob", "Grep",               # READ-ONLY — no Write/Edit/Bash
            "mcp__git__git_diff_staged",
            "mcp__git__git_diff_unstaged",
            "mcp__git__git_log",
            "mcp__git__git_show",
        ],
        mcp_servers={
            "git": {
                "command": "uvx",
                "args": ["mcp-server-git", "--repository", workspace_path],
            },
        },
        permission_mode="bypassPermissions",
        max_turns=20,
    )

    async for message in query(
        prompt=f"Review changes for: {task.description}",
        options=options,
    ):
        yield message
```

---

## Workspace Manager (git worktree, fast)

```python
# services/workspace_service.py

class WorkspaceService:
    async def prepare(self, project, task_id: str) -> Workspace:
        cache_path = f"/workspaces/_cache/{project.name}"
        work_path = f"/workspaces/task-{str(task_id)[:8]}"

        if os.path.exists(cache_path):
            # Update cache (fast: just git fetch)
            await run(f"git -C {cache_path} fetch origin {project.default_branch}")
            await run(f"git -C {cache_path} reset --hard origin/{project.default_branch}")

            # Create worktree (instant — hardlinks .git objects)
            await run(f"git -C {cache_path} worktree add {work_path}")

            # Symlink deps from cache (fast, agent usually doesn't modify these)
            deps_changed = await self._check_deps(cache_path, work_path)
            if not deps_changed and not project.force_fresh_install:
                if os.path.exists(f"{cache_path}/vendor"):
                    os.symlink(f"{cache_path}/vendor", f"{work_path}/vendor")
                if os.path.exists(f"{cache_path}/node_modules"):
                    os.symlink(f"{cache_path}/node_modules", f"{work_path}/node_modules")
            else:
                await self._install_deps(work_path)
                await self._update_cache_deps(cache_path, work_path)
        else:
            # First time: full clone + install
            await run(f"git clone {project.github_repo} {cache_path}")
            await self._install_deps(cache_path)
            await self._save_dep_hashes(cache_path)
            await run(f"git -C {cache_path} worktree add {work_path}")
            os.symlink(f"{cache_path}/vendor", f"{work_path}/vendor")
            os.symlink(f"{cache_path}/node_modules", f"{work_path}/node_modules")

        # Run project-specific setup
        if project.setup_commands:
            for cmd in project.setup_commands.split("\n"):
                await run(cmd, cwd=work_path)

        return Workspace(path=work_path, from_cache=True, deps_changed=deps_changed)

    async def cleanup(self, task_id: str):
        work_path = f"/workspaces/task-{str(task_id)[:8]}"
        # Remove worktree properly (not just rm -rf)
        cache_dirs = glob("/workspaces/_cache/*/")
        for cache in cache_dirs:
            await run(f"git -C {cache} worktree remove {work_path} --force",
                      ignore_errors=True)
        if os.path.exists(work_path):
            shutil.rmtree(work_path)
```

---

## Debounced Telegram Notifications

```python
# services/notification.py

class DebouncedNotifier:
    """Prevents Telegram rate limit errors (max 1 edit per 3 seconds)."""

    def __init__(self, bot: Bot, interval: float = 3.0):
        self.bot = bot
        self.interval = interval
        self.last_sent: dict[str, float] = {}
        self.pending: dict[str, str] = {}

    async def send(self, task, message: str):
        key = str(task.id)
        self.pending[key] = message
        now = time.time()
        if now - self.last_sent.get(key, 0) >= self.interval:
            await self._flush(task)

    async def _flush(self, task):
        message = self.pending.pop(str(task.id), None)
        if message and task.telegram_message_id:
            try:
                await self.bot.edit_message_text(
                    text=message,
                    chat_id=task.telegram_chat_id,
                    message_id=task.telegram_message_id,
                )
                self.last_sent[str(task.id)] = time.time()
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
```

---

## Telegram Bot — FSM with Redis Storage

```python
# bot/main.py

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

async def main():
    bot = Bot(token=settings.telegram_bot_token)

    # FSM state survives bot restarts
    storage = RedisStorage.from_url(settings.redis_url + "/2")
    dp = Dispatcher(storage=storage)

    # Register handlers
    dp.include_router(start.router)
    dp.include_router(task.router)
    dp.include_router(review.router)

    # Heartbeat for Docker health check
    async def heartbeat():
        while True:
            Path("/tmp/bot_health").write_text(str(time.time()))
            await asyncio.sleep(10)
    asyncio.create_task(heartbeat())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)
```

---

## Telegram Flow (Complete)

```
User: /task
Bot:  Select project:
      [webapp] [admin-panel]

User: taps [webapp]
Bot:  Describe your task:

User: "Add email notification when order is placed"
Bot:  Confirm task:
      Project: webapp
      Task: Add email notification when order is placed
      [Submit] [Cancel]

User: taps [Submit]
Bot:  Task submitted! Preparing workspace...
      (message keeps updating):
      → Preparing workspace...
      → Agent working...
      → Agent: Read (app/Models/Order.php)...
      → Agent: Write (app/Notifications/OrderPlaced.php)...
      → Agent: Bash (php artisan test)...

Bot:  Changes ready!
      5 files changed, +127 -3
      + app/Notifications/OrderPlaced.php (new)
      + app/Listeners/SendOrderNotification.php (new)
      ~ app/Providers/EventServiceProvider.php
      ~ config/mail.php
      ~ routes/web.php
      [Create PR] [Discard]

User: taps [Create PR]
Bot:  PR #42 created!
      github.com/org/webapp/pull/42
      [Merge] [Reject] [View PR]

User: reviews on GitHub, taps [Merge]
Bot:  Merged! PR #42 is live.
```

### Cancel Flow
```
User: /cancel
Bot:  Running tasks:
      [Task #a1b2: "Add email notification" — coding...] [Cancel]

User: taps [Cancel]
Bot:  Task cancelled. Workspace cleaned up.
```

---

## Self-Service Commands (via Telegram)

### Project Onboarding — `/addproject <repo_url>`

```
User: /addproject https://github.com/ahmedMAfani/trade-bot

OPENCLOW:
  1. Clones the repo (temporary)
  2. Launches Onboarding Agent (Claude, read-only) that:
     - Reads docker-compose.yml → detects services, ports
     - Reads Dockerfile → detects base image, framework
     - Reads package.json / composer.json / requirements.txt → detects tech stack
     - Reads CLAUDE.md if exists → gets project conventions
     - Reads README.md → gets project description
  3. Sends to Telegram:
     "Project analyzed!
      Name: trade-bot
      Tech: Python, FastAPI, PostgreSQL, Redis
      App container: aurora-api
      Port: 8000
      Docker: infra/docker/docker-compose.yml
      [Add Project] [Cancel]"
  4. User taps [Add Project] → saved to DB
  5. Starts Docker stack → sends live link
  6. Ready for /task
```

### User Management — `/adduser <telegram_id>`
```
Admin: /adduser 123456789 @username
Bot: "User 123456789 (@username) added and authorized."
```

### Onboarding Agent — analyzes repos automatically
```python
ONBOARDING_SYSTEM_PROMPT = """Analyze this repository and extract:
1. Find docker-compose.yml (check root, infra/, docker/, etc.)
2. Identify the main app service name and port
3. Detect tech stack from files (package.json, composer.json, requirements.txt, etc.)
4. Read README.md for project description
5. Read CLAUDE.md for conventions (if exists)

Output in this EXACT format:
PROJECT_NAME: <derived from repo>
TECH_STACK: <comma-separated>
DOCKER_COMPOSE: <path to docker-compose.yml>
APP_CONTAINER: <service name>
APP_PORT: <port number>
DESCRIPTION: <one line>
SETUP_COMMANDS: <if needed, one per line>
"""
```

## Implementation Order

### Step 0: Validate (Before Writing Code)
- [ ] Run `claude setup-token` on your Mac, save the token
- [ ] Test Claude Agent SDK with OAuth token in a Python script
- [ ] Test arq basic task with asyncio
- [ ] Test git worktree on a sample repo

### Step 1: Project Skeleton
- [ ] `pyproject.toml`, `.gitignore`, `.env.*` files
- [ ] `Dockerfile.app`, `Dockerfile.worker`
- [ ] `docker-compose.yml`, `docker-compose.override.yml`
- [ ] `src/openclow/settings.py`
- [ ] Verify: `docker compose build` succeeds

### Step 2: Database
- [ ] Models: `base.py`, `user.py`, `project.py`, `task.py`
- [ ] Alembic setup + migration
- [ ] `scripts/seed_projects.py`, `scripts/create_user.py`
- [ ] Verify: `docker compose run migrate` works

### Step 3: Worker Foundation
- [ ] `worker/arq_app.py` — arq settings
- [ ] `worker/tasks/git_ops.py` — clone, branch, push, PR
- [ ] `services/workspace_service.py` — git worktree + cache
- [ ] `services/notification.py` — debounced Telegram updates
- [ ] `utils/logging.py` — structlog setup
- [ ] Verify: manual test of git ops

### Step 4: Agent Integration
- [ ] `agents/coder.py` — system prompt + Claude Agent SDK
- [ ] `mcp_servers/project_info.py` — in-process MCP tool
- [ ] `worker/tasks/orchestrator.py` — full pipeline
- [ ] Verify: run agent on test repo, check it makes changes

### Step 5: Telegram Bot
- [ ] `bot/main.py` — dispatcher, Redis FSM storage, heartbeat
- [ ] `bot/middlewares/auth.py`
- [ ] `bot/states.py`, `bot/keyboards.py`
- [ ] `bot/handlers/start.py` — /start, /help, /projects, /status, /cancel
- [ ] `bot/handlers/task.py` — /task FSM flow
- [ ] `bot/handlers/review.py` — approve/reject/discard/merge
- [ ] Verify: full E2E on Telegram

### Step 6: API + Polish
- [ ] `api/main.py` + health + tasks routes
- [ ] Error handling in all handlers
- [ ] Write to `task_logs` at every step
- [ ] Periodic workspace cleanup task
- [ ] Startup OAuth token validation
- [ ] Verify: `docker compose up` — everything works E2E

---

## Weaknesses Addressed (Audit Results)

| Issue | Severity | Fix |
|-------|----------|-----|
| Wrong auth (API key vs OAuth) | CRITICAL | Use `CLAUDE_CODE_OAUTH_TOKEN` |
| 3 agents (deployer wasted) | HIGH | 2 agents (coder+reviewer) + direct Python for PR |
| Celery asyncio conflicts | CRITICAL | Switch to arq (native async) |
| cp -r is 30+ sec for large repos | HIGH | git worktree add (instant) |
| Telegram rate limits on edits | HIGH | Debounced notifier (3 sec) |
| No diff preview before PR | HIGH | Send diff, wait for approval |
| No task cancellation | HIGH | /cancel command + arq job abort |
| No per-project concurrency lock | HIGH | Redis lock per project |
| FSM state lost on restart | MEDIUM | Redis-backed FSM storage |
| Single fat image for all services | MEDIUM | Split Dockerfile.app + Dockerfile.worker |
| All secrets in all containers | MEDIUM | Split .env files per service |
| No structured logging | MEDIUM | structlog with JSON + task_id |
| Ports exposed unnecessarily | MEDIUM | Only in dev override |
| Agent stall/infinite loop | MEDIUM | Stall detector (10 turns no diff = kill) |
| No task retry for transient errors | MEDIUM | arq built-in retry |
| No disk cleanup | MEDIUM | Periodic + startup cleanup |
| task_logs never written | MEDIUM | Write at every status change |
