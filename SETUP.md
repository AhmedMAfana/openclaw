# TAGH DevOps — Developer Setup Guide

## Overview

TAGH DevOps is an AI-powered DevOps orchestration platform. It spins up isolated Docker environments per chat session, manages Cloudflare tunnels, and lets developers interact with their apps via a web chat UI.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy · Redis · PostgreSQL · React (Vite) · Docker · Cloudflare Tunnels

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker | 24+ | with Compose v2 (`docker compose`) |
| Node.js | 18+ | for frontend build only |
| Git | any | |
| Python | 3.12 | only needed to run scripts outside Docker |

---

## 1. Clone the repo

```bash
git clone git@github.com:ami-digital/devops.git
cd devops
```

---

## 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in the required values:

```env
# Database (auto-created by Docker)
DATABASE_URL=postgresql+asyncpg://taghdev:taghdev@postgres:5432/taghdev
REDIS_URL=redis://:taghdev@redis:6379/0
REDIS_PASSWORD=taghdev
POSTGRES_PASSWORD=taghdev

# Security — generate a strong secret:
# openssl rand -hex 32
WEB_CHAT_JWT_SECRET=<your-secret>

# Claude API (for the AI agent)
ANTHROPIC_API_KEY=<your-anthropic-key>

# Cloudflare (for tunnel provisioning — already set for tagh.co.uk)
CF_ACCOUNT_ID=<cloudflare-account-id>
CF_ZONE_ID=<cloudflare-zone-id>
CF_ZONE_DOMAIN=tagh.co.uk
CF_API_TOKEN=<cloudflare-api-token>
```

> **Note:** Cloudflare credentials are already configured for `tagh.co.uk`. Only change them if deploying to a different domain.

---

## 3. Build the frontend

```bash
cd chat_frontend
npm install
npm run build
cd ..
```

This writes to `chat_frontend/dist/` which is volume-mounted into the containers.

---

## 4. Start the stack

```bash
docker compose up --build
```

First run takes ~3 minutes (builds images, runs DB migrations).

**Services started:**

| Service | Port | Description |
|---|---|---|
| `api` | 8000 | FastAPI backend + chat UI |
| `worker` | — | ARQ background job worker |
| `bot` | — | Telegram bot (optional) |
| `postgres` | 5432 | PostgreSQL database |
| `redis` | 6379 | Redis (jobs + pub/sub) |

---

## 5. Create your first admin user

```bash
docker compose exec api python scripts/seed_web_user.py
```

Or create a user manually:

```bash
docker compose exec api python scripts/create_user.py \
  --username admin \
  --password yourpassword \
  --email admin@example.com \
  --admin
```

---

## 6. Access the app

Open **http://localhost:8000** and log in with the credentials from step 5.

---

## 7. Add a project

1. Click **Settings** → **Projects** → **Add Project**
2. Select the GitHub repo
3. Choose mode: `container` (isolated Docker per chat) or `host`
4. Save

---

## Daily development workflow

### Code changes (Python only — no rebuild needed)

```bash
docker compose restart api worker
```

### Frontend changes

```bash
cd chat_frontend && npm run build
# No restart needed — dist/ is volume-mounted
```

### Dependency changes (pyproject.toml or Dockerfile)

```bash
docker compose up api worker --build
```

### View logs

```bash
docker compose logs -f api worker
docker compose logs -f bot
```

### Run DB migrations

```bash
docker compose exec api alembic upgrade head
```

---

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `REDIS_URL` | ✅ | Redis connection string |
| `REDIS_PASSWORD` | ✅ | Redis auth password |
| `POSTGRES_PASSWORD` | ✅ | PostgreSQL password |
| `WEB_CHAT_JWT_SECRET` | ✅ | JWT signing secret (32+ hex chars) |
| `ANTHROPIC_API_KEY` | ✅ | Claude API key for the AI agent |
| `CF_ACCOUNT_ID` | ✅ | Cloudflare account ID |
| `CF_ZONE_ID` | ✅ | Cloudflare zone ID |
| `CF_ZONE_DOMAIN` | ✅ | Base domain (e.g. `tagh.co.uk`) |
| `CF_API_TOKEN` | ✅ | Cloudflare API token (Tunnel + DNS scopes) |
| `TELEGRAM_BOT_TOKEN` | ➖ | Optional — enables Telegram bot |
| `SLACK_BOT_TOKEN` | ➖ | Optional — enables Slack bot |
| `GROQ_API_KEY` | ➖ | Optional — voice transcription |
| `LOG_LEVEL` | ➖ | `INFO` (default) or `DEBUG` |

---

## Project structure

```
.
├── src/taghdev/          # Python backend
│   ├── api/              # FastAPI routes + auth
│   ├── models/           # SQLAlchemy models
│   ├── services/         # Business logic
│   ├── worker/           # ARQ background jobs
│   ├── providers/        # Chat providers (Telegram, Slack, Web)
│   ├── mcp_servers/      # MCP tool servers for the AI agent
│   └── agents/           # LLM agent implementations
├── chat_frontend/        # React + Vite frontend
│   ├── src/
│   └── dist/             # Built output (served by FastAPI)
├── alembic/              # DB migrations
├── scripts/              # Utility scripts
├── tests/                # Test suite
├── docs/                 # Architecture docs
├── specs/                # Feature specs
├── Dockerfile.app        # API + bot image
├── Dockerfile.worker     # Worker image
├── docker-compose.yml    # Main compose file
└── .env                  # Your local config (gitignored)
```

---

## Troubleshooting

**Port 8000 already in use**
```bash
lsof -i :8000 | grep LISTEN
# kill the process or change the port in docker-compose.yml
```

**DB migration errors on first run**
```bash
docker compose exec api alembic upgrade head
```

**Worker not connecting to DB**
```bash
docker compose logs worker | grep "error"
# Usually a DB password mismatch — check .env POSTGRES_PASSWORD
```

**Frontend not updating after build**
```bash
# Hard refresh in browser (Ctrl+Shift+R)
# If still stale: docker compose restart api
```

**Claude agent not responding**
```bash
docker compose logs api | grep "auth\|ANTHROPIC"
# Check ANTHROPIC_API_KEY is set in .env
```

---

## Getting Cloudflare credentials

1. **Account ID** — Cloudflare Dashboard → right sidebar
2. **Zone ID** — Cloudflare Dashboard → select domain → right sidebar
3. **API Token** — Cloudflare Dashboard → Profile → API Tokens → Create Token
   - Scopes needed: `Account > Cloudflare Tunnel > Edit`, `Zone > DNS > Edit`

---

## Production deployment

Use `docker-compose.prod.yml` which disables hot-reload and enables production settings:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```
