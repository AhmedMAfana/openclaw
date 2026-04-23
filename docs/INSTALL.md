# Install & Run (Dev)

Bring the orchestrator up from scratch on a fresh machine and wire the
`tagh-fre` seed repo for per-chat-instances (`mode='container'`) tests.

---

## 1. Prerequisites

- Docker Engine 24+ (Docker Desktop on Mac works) with `docker compose` plugin.
- `gh` CLI, authenticated against the GitHub account that owns
  `AhmedMAfana/tagh-fre` (private).
- `git`.
- Python 3.12 is **not** required on the host — everything runs in
  containers. Host Python is optional for fast contract-test iteration.

## 2. Clone

```bash
git clone https://github.com/AhmedMAfana/openclaw.git tagh-devops
cd tagh-devops
cp .env.example .env   # if present; otherwise create .env per §3 below
```

## 3. `.env`

At minimum the following keys must be set before `docker compose up`:

```ini
# Database — matches docker-compose.yml defaults
POSTGRES_PASSWORD=openclow
DATABASE_URL=postgresql+asyncpg://openclow:openclow@postgres:5432/openclow

# Redis — matches docker-compose.yml defaults
REDIS_PASSWORD=openclow
REDIS_URL=redis://:openclow@redis:6379/0

# Web-chat auth (any 64-char hex; the api container will mint one on
# first boot if WEB_CHAT_JWT_SECRET is empty)
WEB_CHAT_JWT_SECRET=

# Workspace root used by both host and worker
WORKSPACE_BASE_PATH=/workspaces
```

Cloudflare / GitHub App credentials are **not** required until T036+
(tunnel + instance provisioning). Skip them for T030/T034 work.

## 4. Bring up the stack

```bash
docker compose up --build -d postgres redis
docker compose run --rm migrate        # applies alembic migrations incl. 011 + 012
docker compose up -d api worker bot
```

Check health:

```bash
docker compose ps
docker compose logs -f worker api | grep -Ei "error|ready|started"
```

## 5. Seed the dev DB

```bash
# One-off test user for the web chat (admin)
docker compose run --rm api python -m scripts.seed_web_user
# default credentials: testuser / testpass123

# Per-chat-instances fixture project — clones AhmedMAfana/tagh-fre on provision
docker compose run --rm api python -m scripts.seed_tagh_fre
```

Verify:

```bash
docker compose exec postgres psql -U openclow -c \
  "SELECT id, name, github_repo, mode FROM projects WHERE name='tagh-fre';"
```

## 6. Run tests

### Contract tier (no DB, ~0.5 s)

```bash
docker compose run --rm worker bash -c \
  "pip install -q pytest pytest-asyncio && \
   python -m pytest tests/contract/test_instance_service.py -v"
```

Expected: **22 passed**. This proves the `InstanceService` state machine
in isolation — no Postgres / Redis / ARQ required.

### Integration tier (Postgres + Redis required)

T034 / T034a are DB-backed. Until their real-DB fixtures land, the
modules skip cleanly. When they activate:

```bash
docker compose run --rm \
  -e OPENCLOW_DB_TESTS=1 \
  worker python -m pytest tests/integration/test_per_user_cap.py tests/integration/test_platform_capacity_error.py -v
```

### E2E (T031 — real Docker + compose + cloudflared)

Requires T036 (`provision_instance`) and T037 (`teardown_instance`) ARQ
jobs, plus Cloudflare API credentials (stubbed in test) or a live zone
(nightly only). Until then the module skips.

When active:

```bash
docker compose run --rm \
  -e OPENCLOW_E2E=1 \
  worker python -m pytest tests/integration/test_provision_teardown_e2e.py -v
```

## 7. Where to run what

| Test tier         | Mac (Docker Desktop) | Server `…113`       |
|-------------------|----------------------|---------------------|
| Contract (22)     | ✓ fast, primary      | ✓                   |
| Integration (T034, T034a) | ✓ Postgres+Redis via compose | ✓ same |
| E2E (T031)        | ✓ works but flaky on cloudflared/network | ✓ **preferred** — prod-like |

The provision ARQ job launches *nested* `docker compose up -p tagh-inst-<slug>`
per instance, and cloudflared behaves closest to prod on Linux. Mac Docker
Desktop can run it but expect occasional DNS/network weirdness.

## 8. Handy commands

```bash
# Code changes only — no rebuild
docker compose restart worker api bot

# Dependency or Dockerfile changes — rebuild
docker compose up -d --build worker api bot

# Tail the worker + api
docker compose logs -f worker api

# Drop & reapply migrations (nuke DB)
docker compose down -v postgres
docker compose up -d postgres
docker compose run --rm migrate

# List per-instance compose projects (after T036 lands)
docker compose ls -a | grep tagh-inst-
```

## 9. Troubleshooting

- **`alembic` fails on migration 011** — the `instances` table FKs
  require `web_chat_sessions` and `projects` from earlier migrations.
  If those are missing the migrate container exits with a constraint
  error; run `docker compose down -v postgres` and start clean.
- **`ModuleNotFoundError: httpx`** — you're running tests on host
  Python; either `pip install -e .[dev]` locally or run inside the
  `worker` container (preferred).
- **Postgres port collision (5432)** — Mac often has another Postgres
  listening. Either stop it or add a `ports: - "55432:5432"` override
  and set `DATABASE_URL` host to `localhost:55432`.

## 10. Pointers

- Active spec: [`specs/001-per-chat-instances/spec.md`](../specs/001-per-chat-instances/spec.md)
- Task list: [`specs/001-per-chat-instances/tasks.md`](../specs/001-per-chat-instances/tasks.md)
- Contracts: [`specs/001-per-chat-instances/contracts/`](../specs/001-per-chat-instances/contracts/)
- Architecture notes: [`docs/architecture/`](architecture/)
- Prior-art research: [`docs/research/`](research/)
