# TAGH Dev — AI-Powered Development Orchestration Platform

TAGH Dev is an AI-driven development automation platform that orchestrates coding agents to implement features, review code, and manage deployment workflows through chat interfaces (Telegram/Slack).

## Project Overview

TAGH Dev enables teams to delegate development tasks to AI agents through familiar chat platforms. The platform manages the entire development lifecycle:

1. **Task Intake** — Users submit tasks via Telegram or Slack
2. **Planning** — Planner agent analyzes the codebase and creates implementation plans
3. **Approval** — Users review and approve plans before execution
4. **Implementation** — Coder agent implements changes with progress updates
5. **Review** — Reviewer agent checks code quality and security
6. **Delivery** — Changes are pushed to GitHub as pull requests

## Technology Stack

### Core Technologies
- **Python 3.12+** — Main programming language
- **FastAPI** — Web API framework
- **SQLAlchemy 2.0** — Async ORM for database operations
- **PostgreSQL 16** — Primary database (with asyncpg driver)
- **Redis 7** — Task queue and caching
- **ARQ** — Async distributed task queue

### AI & Agent Framework
- **Claude Agent SDK** — LLM agent framework for coding tasks
- **Claude Code CLI** — Command-line AI assistant (inside worker)
- **faster-whisper** — Voice transcription for audio messages

### Chat Providers
- **aiogram 3.4+** — Telegram bot framework
- **slack-bolt** — Slack app framework

### Infrastructure
- **Docker & Docker Compose** — Container orchestration
- **Cloudflared** — Secure tunneling for public URLs
- **Dozzle** — Docker log viewer (observability)

### Development Tools
- **Alembic** — Database migrations
- **pytest** — Testing framework
- **structlog** — Structured logging
- **Pydantic Settings** — Configuration management
- **GitHub CLI (gh)** — GitHub operations

## Architecture

### Service Architecture (Microservices)

```
┌─────────────────────────────────────────────────────────────┐
│                         TAGH Dev                             │
├─────────────┬─────────────┬─────────────┬───────────────────┤
│     Bot     │     API     │   Worker    │   Infrastructure  │
│  (Chat UI)  │  (REST API) │ (Job Queue) │  (Postgres/Redis) │
├─────────────┼─────────────┼─────────────┼───────────────────┤
│ • Telegram  │ • Health    │ • Task      │ • PostgreSQL      │
│ • Slack     │ • Tasks     │   execution │ • Redis           │
│   handlers  │ • Settings  │ • Git ops   │ • Dozzle          │
│ • Auth      │ • Activity  │ • Agents    │                   │
│   middleware│   logs      │ • MCP srvs  │                   │
└─────────────┴─────────────┴─────────────┴───────────────────┘
```

**Services:**
- **bot** — Handles incoming chat messages (Telegram/Slack)
- **api** — FastAPI web service for health checks, settings dashboard, activity logs
- **worker** — ARQ worker that executes background tasks (coding, git operations)
- **postgres** — PostgreSQL database
- **redis** — Redis for task queue and caching
- **dozzle** — Web UI for viewing container logs

### Agent Pipeline

```
User Task
    ↓
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   PLANNER   │ →  │    CODER    │ →  │  REVIEWER   │
│  (Claude)   │    │  (Claude)   │    │  (Claude)   │
│  Read-only  │    │  Write code │    │  Quality    │
└─────────────┘    └─────────────┘    └─────────────┘
      ↓                  ↓                  ↓
   Plan sent        Progress updates    Fix or approve
   for approval                        ↓
                                    PR Created
```

### Provider Pattern

TAGH Dev uses an abstract provider pattern for vendor-agnostic operations:

- **LLMProvider** — Claude (via claude_agent_sdk)
- **ChatProvider** — Telegram (aiogram), Slack (slack-bolt)
- **GitProvider** — GitHub (via gh CLI)

All providers are configured in the database and instantiated via the factory pattern.

## Project Structure

```
taghdev/
├── agents/                 # AI agent implementations
│   ├── coder.py           # Coder agent (implements tasks)
│   ├── reviewer.py        # Reviewer agent (code review)
│   ├── doctor.py          # Diagnostic agent
│   ├── bootstrap.py       # Project bootstrap agent
│   └── onboarding.py      # Project onboarding
├── api/                   # FastAPI web service
│   ├── main.py           # API entry point
│   ├── routes/           # API routes (health, tasks, settings)
│   ├── pages.py          # HTML pages (settings dashboard)
│   └── schemas/          # Pydantic schemas
├── bot/                   # Telegram bot (legacy, redirects to providers)
│   ├── main.py           # Bot entry point
│   ├── handlers/         # Message handlers
│   └── middlewares/      # Auth middleware
├── models/               # SQLAlchemy database models
│   ├── base.py          # Base model, engine, session
│   ├── user.py          # User model
│   ├── project.py       # Project model
│   ├── task.py          # Task and TaskLog models
│   ├── config.py        # Platform config model
│   └── audit.py         # Audit log model
├── providers/            # Provider abstractions and implementations
│   ├── base.py          # Abstract base classes (LLM, Chat, Git)
│   ├── factory.py       # Provider factory (singleton pattern)
│   ├── registry.py      # Provider registry
│   ├── llm/claude.py    # Claude LLM provider
│   ├── chat/telegram/   # Telegram provider
│   ├── chat/slack/      # Slack provider
│   └── git/github.py    # GitHub provider
├── services/            # Business logic services
│   ├── config_service.py    # Provider config management
│   ├── workspace_service.py # Git workspace management
│   ├── tunnel_service.py    # Cloudflare tunnel management
│   └── ...
├── worker/              # ARQ background worker
│   ├── arq_app.py      # Worker configuration
│   └── tasks/          # Task implementations
│       ├── orchestrator.py  # Main task orchestration
│       ├── bootstrap.py     # Project bootstrap
│       ├── git_ops.py       # Git operations
│       └── ...
├── mcp_servers/         # MCP (Model Context Protocol) servers
│   ├── project_info.py      # Project info MCP server
│   ├── github_mcp.py        # GitHub MCP server
│   └── docker_mcp.py        # Docker MCP server
├── setup/               # Interactive setup wizard
│   └── __main__.py     # python -m taghdev.setup
└── utils/               # Utilities
    └── logging.py       # Structured logging
```

## Build and Run Commands

### Initial Setup

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env with your secrets

# 2. Run setup wizard to configure providers
docker compose run --rm setup

# 3. Build images
docker compose build

# 4. Run migrations
docker compose run --rm migrate

# 5. Start services
docker compose up -d

# 6. Authenticate Claude (one-time)
docker exec -it taghdev-worker-1 claude login
```

### Development Commands

```bash
# Start all services
docker compose up -d

# View logs
docker compose logs -f [service]

# Restart a service
docker compose restart [bot|api|worker]

# Run migrations
docker compose run --rm migrate

# Create new migration
docker compose run --rm migrate alembic revision -m "description"

# Access database
docker compose exec postgres psql -U taghdev -d taghdev

# Access Redis
docker compose exec redis redis-cli -a taghdev
```

### Setup Wizard

```bash
# Interactive configuration
docker compose run --rm setup

# Or run directly
python -m taghdev.setup
```

## Testing Instructions

### Run Tests

```bash
# Run all tests
docker compose exec worker pytest

# Run with verbose output
docker compose exec worker pytest -v

# Run specific test file
docker compose exec worker pytest tests/test_audit_fixes.py

# Run specific test class
docker compose exec worker pytest tests/test_audit_fixes.py::TestCommandInjectionFixes
```

### Test Structure

Tests are located in `tests/` directory:

- **test_audit_fixes.py** — Security and critical fix verification tests
  - Command injection prevention
  - Settings validation
  - Claude provider fixes
  - Workspace locking
  - Factory singleton pattern
  - Notification fixes
  - Activity log fixes
  - Logging configuration
  - Review handler fixes
  - Tunnel service fixes
  - ORM session fixes
  - Dead code cleanup

## Code Style Guidelines

### Python Style

- **PEP 8** compliance required
- **Type hints** strongly encouraged (Python 3.12+ features)
- **Docstrings** for all public functions and classes
- **Max line length** — 100 characters preferred

### Naming Conventions

```python
# Modules: lowercase_with_underscores
# Classes: PascalCase
# Functions/Variables: snake_case
# Constants: UPPER_SNAKE_CASE
# Private: _leading_underscore

class WorkspaceService:
    DEFAULT_TIMEOUT = 300
    
    async def prepare_workspace(self, project_id: int) -> str:
        _temp_dir = "/tmp/workspace"
        ...
```

### Async Patterns

- Use `async/await` for all I/O operations
- Prefer `asyncio.create_subprocess_exec` over `subprocess` (security)
- Use `async_session` from SQLAlchemy for database operations
- Always close resources properly (use context managers)

### Security Requirements

**CRITICAL: Never use shell=True or create_subprocess_shell with user input**

```python
# ✅ CORRECT: Use exec with argument list
await asyncio.create_subprocess_exec(
    "git", "clone", repo_url, dest_path,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)

# ❌ WRONG: Shell injection vulnerability
await asyncio.create_subprocess_shell(
    f"git clone {repo_url} {dest_path}",  # DANGEROUS!
)
```

### Logging

Use structured logging via `get_logger()`:

```python
from taghdev.utils.logging import get_logger

log = get_logger()

# Structured logging with context
log.info("task.started", task_id=str(task.id), project=project.name)
log.error("git.clone_failed", repo=repo_url, error=str(e))
```

### Database Patterns

```python
# Use async_session context manager
from taghdev.models import async_session

async def get_project(project_id: int) -> Project | None:
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.id == project_id)
        )
        return result.scalar_one_or_none()

# Eager load relationships to avoid N+1
from sqlalchemy.orm import selectinload

result = await session.execute(
    select(Task)
    .options(selectinload(Task.project), selectinload(Task.user))
    .where(Task.id == task_id)
)
```

## Security Considerations

### Command Injection Prevention

All subprocess calls must use `create_subprocess_exec` with argument lists, never shell strings. This is enforced by tests in `test_audit_fixes.py`.

### Secrets Management

- Secrets are stored in **database** (provider configs), not environment variables
- `.env` only contains infrastructure settings (database URLs)
- Never log secrets or API tokens

### User Authentication

- Users are authenticated via chat provider UID (Telegram user ID, Slack user ID)
- `is_allowed` flag controls access
- Middleware blocks unauthorized users

### Provider Isolation

- Tasks track their originating provider (`chat_provider_type`)
- Tasks from mismatched providers are rejected (prevents cross-platform confusion)

### Docker Security

- Worker runs as non-root user (`taghdev`)
- Worker has access to host Docker socket (for project containers)
- Each project runs in isolated Docker Compose network

## Database Migrations

### Creating Migrations

```bash
# Auto-generate migration from model changes
docker compose run --rm migrate alembic revision --autogenerate -m "description"

# Create empty migration
docker compose run --rm migrate alembic revision -m "description"
```

### Migration Files

Located in `alembic/versions/`:

- `001_initial_schema.py` — Initial database schema
- `002_audit_logs.py` — Audit logging
- `003_project_status.py` — Project status field
- `004_task_chat_provider_type.py` — Provider tracking

### Schema Overview

**Tables:**
- `platform_config` — Provider configurations (LLM, chat, git)
- `users` — Authorized users
- `projects` — Managed projects
- `tasks` — Development tasks
- `task_logs` — Task execution logs
- `audit_logs` — Security audit logs

## MCP Servers

Model Context Protocol (MCP) servers provide tools to Claude agents:

- **project_info** — Get project details and coding conventions
- **github_mcp** — GitHub operations (via MCP server)
- **docker_mcp** — Docker container operations
- **actions_mcp** — Action dispatching

## Common Development Tasks

### Adding a New Provider

1. Create provider class in appropriate subdirectory
2. Inherit from base class (`LLMProvider`, `ChatProvider`, `GitProvider`)
3. Register in `providers/registry.py`
4. Import in `providers/factory.py` (with try/except for optional deps)
5. Add tests

### Adding a New Task

1. Implement async function in `worker/tasks/`
2. Add to `arq_app.py` `_load_functions()` list
3. Enqueue via `await enqueue_job("function_name", args)`

### Adding API Endpoints

1. Create route in `api/routes/`
2. Include router in `api/main.py`
3. Add Pydantic schemas in `api/schemas/` if needed

## Troubleshooting

### Worker Not Processing Tasks

```bash
# Check worker health
docker compose exec worker python -c "import redis; r=redis.from_url('redis://:taghdev@redis:6379/0'); print(r.ping())"

# Restart worker
docker compose restart worker
```

### Database Connection Issues

```bash
# Verify postgres is healthy
docker compose ps postgres

# Check connection
docker compose exec api python -c "from taghdev.models import engine; print(engine.url)"
```

### Claude Authentication

```bash
# Check auth status
docker exec -it taghdev-worker-1 claude auth status

# Re-authenticate
docker exec -it taghdev-worker-1 claude login
```

## Additional Resources

- `docs/AGENT_SYSTEM.md` — Agent responsibility map
- `docs/IMPLEMENTATION_PLAN.md` — Implementation history
- `docs/AUDIT_FIX_PLAN.md` — Security audit fixes
- `docs/ACTIVITY_LOG_PORTABLE_GUIDE.md` — Activity logging guide
