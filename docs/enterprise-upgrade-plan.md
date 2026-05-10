# Enterprise-Level System Upgrade Plan

## Context

The system is a production-grade AI Dev Orchestrator (Python 3.12 + FastAPI + ARQ + Redis + PostgreSQL + Docker per-chat instances). The codebase audit revealed that while the core architecture is solid, it's missing the telemetry, reliability patterns, security hardening, and horizontal-scale readiness that separate a "senior app" from an enterprise platform. The user is targeting multi-server horizontal scaling. This plan addresses all four pillars in prioritized order: Performance → Observability → Reliability → Security.

---

## Phase 1: Performance (Quick Wins — No New Infrastructure)

**Goal:** Drop tail latency 40-60% and saturate the current server properly before scaling out.

### 1.1 `uvloop` — Event Loop Replacement
- Add `uvloop>=0.21` to `pyproject.toml`
- Set in `src/taghdev/worker/arq_app.py` — ARQ reads `WorkerSettings.loop` 
- Set in `Dockerfile.worker` / uvicorn startup command: `--loop uvloop`
- Expected: 40-60% reduction in tail latency for all async I/O

### 1.2 Uvicorn Worker Count
- Current: `api` container runs 1 uvicorn process
- Fix `docker-compose.prod.yml` api command: `--workers 9` (4-core target: `2*4+1`)
- Each worker is a separate process with its own DB connection pool
- File: `docker-compose.prod.yml`

### 1.3 SQLAlchemy Engine Tuning
- File: `src/taghdev/models/base.py`
- Current: `pool_size=5, max_overflow=10` — too small for multi-worker
- Fix:
  ```python
  pool_size=20,
  max_overflow=10,
  pool_timeout=30,
  pool_recycle=3600,
  pool_pre_ping=True,  # validates connections after idle
  ```
- For multi-server: add PgBouncer as a compose service in transaction pooling mode

### 1.4 PgBouncer (Multi-Server Prep)
- New compose service: `pgbouncer` (image: `bitnami/pgbouncer:1`)
- API + worker connect to PgBouncer (port 6432), not Postgres directly
- Allows N worker nodes to share a bounded connection pool to Postgres
- Config: `pool_mode=transaction`, `max_client_conn=500`, `default_pool_size=25`
- Files to add: `pgbouncer.ini`, `userlist.txt`, update `docker-compose.yml`

### 1.5 ARQ Worker Scaling
- File: `src/taghdev/worker/arq_app.py`
- Add `max_jobs=20` to `WorkerSettings` (currently unset, defaults low)
- Add `job_timeout=600` (10 min hard ceiling on any single job)
- Document: run multiple `worker` replicas for horizontal scale — each picks up from same Redis queue

---

## Phase 2: Observability (New Compose Services)

**Goal:** Full visibility into LLM cost, latency, job queue health, and errors — zero blind spots.

### 2.1 Prometheus + Grafana Stack
New compose services in `docker-compose.yml`:
- `prometheus` (prom/prometheus:v2.53) — scrapes all metrics endpoints
- `grafana` (grafana/grafana:11) — dashboards + alerting
- `redis_exporter` (oliver006/redis_exporter) — Redis metrics
- `postgres_exporter` (prometheuscommunity/postgres-exporter) — DB metrics

FastAPI instrumentation:
- Add `prometheus-fastapi-instrumentator>=7.0` to `pyproject.toml`
- Wire in `src/taghdev/api/main.py`:
  ```python
  from prometheus_fastapi_instrumentator import Instrumentator
  Instrumentator().instrument(app).expose(app)
  ```
- Exposes `/metrics` endpoint — Prometheus scrapes it every 15s

Custom ARQ metrics:
- Add `prometheus_client` counters/histograms in `src/taghdev/worker/arq_app.py`
- Track: jobs enqueued, jobs completed, jobs failed, job duration by type

### 2.2 Langfuse (LLM Tracing)
Self-hosted Langfuse v3 as compose services (requires 6 containers):
- `langfuse-web`, `langfuse-worker`, `clickhouse`, `minio`, `langfuse-redis`, `langfuse-postgres`
- OR use Langfuse Cloud (zero infra) with `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` env vars

Instrument `src/taghdev/providers/llm/claude.py`:
```python
from langfuse.decorators import observe, langfuse_context

@observe(as_type="generation")
async def _run_claude_agent(...):
    langfuse_context.update_current_observation(
        model="claude-sonnet-4-6",
        input=messages,
        metadata={"chat_id": chat_id, "project": project_name}
    )
```

Every Claude agent call gets: input/output tokens, cost, TTFT, cache hit rate, trace waterfall.

### 2.3 Structured Logging → Loki-Ready
- File: `src/taghdev/utils/logging.py`
- Current: structlog with ConsoleRenderer in debug, JSONRenderer in prod
- Add consistent fields to every log line: `chat_id`, `instance_slug`, `project_id`, `job_name`
- Add `request_id` middleware on FastAPI that generates a UUID per request and binds it to structlog context
- In docker-compose.prod.yml: add Loki log driver OR promtail sidecar to ship activity.jsonl + container logs to Grafana Loki

### 2.4 Sentry Error Tracking
- Add `sentry-sdk[fastapi,arq]>=2.0` to `pyproject.toml`
- Init in `src/taghdev/api/main.py` and `src/taghdev/worker/arq_app.py`
- Every unhandled exception gets captured with full context (user, chat_id, job_name)
- `SENTRY_DSN` env var (free tier covers small teams)

---

## Phase 3: Reliability

**Goal:** The system degrades gracefully under any failure — no cascading failures, no silent job loss.

### 3.1 Circuit Breakers on External APIs
- Add `pyresilience>=1.0` or `circuitbreaker>=2.0` to `pyproject.toml`
- Wrap in `src/taghdev/services/tunnel_service.py` (Cloudflare API calls)
- Wrap in `src/taghdev/services/credentials_service.py` (GitHub API calls)
- Pattern:
  ```python
  @circuit(failure_threshold=5, recovery_timeout=60)
  async def create_dns_record(...): ...
  ```
- When circuit opens: return structured error immediately, queue retry job instead of hanging

### 3.2 ARQ Job Idempotency Guards
- File: `src/taghdev/worker/arq_app.py` + all job handlers
- Add Redis `SET NX` deduplication at job entry:
  ```python
  key = f"taghdev:job_dedup:{job_name}:{idempotency_key}"
  if not await redis.set(key, "1", nx=True, ex=3600):
      return  # already processed
  ```
- Critical for: `provision_instance`, `teardown_instance`, `rotate_github_token`
- Prevents duplicate provisioning on worker restart / network split

### 3.3 Dead Letter Queue (DLQ)
- File: `src/taghdev/worker/arq_app.py`
- Add `on_job_error` hook to ARQ `WorkerSettings`
- On permanent failure (max retries exceeded): write to `taghdev:dlq` Redis sorted set with timestamp
- Add `/api/admin/dlq` endpoint to view + manually retry DLQ entries
- Wire DLQ count as a Prometheus gauge (alert when > 0)

### 3.4 Graceful Shutdown with Job Draining
- File: `src/taghdev/worker/arq_app.py`
- Add `on_shutdown` hook that waits for in-flight jobs to complete (up to 30s)
- Register SIGTERM handler that sets a flag → job runners check flag and don't pick up new jobs
- docker-compose stop_grace_period: 45s (longer than drain window)

### 3.5 Job Timeout Enforcement
- File: `src/taghdev/worker/arq_app.py`
- Set per-job-type timeouts in the job registration:
  ```python
  {"function": provision_instance, "timeout": 300},   # 5min
  {"function": execute_task, "timeout": 600},          # 10min
  {"function": chat_response, "timeout": 120},         # 2min
  ```
- Jobs that exceed timeout are cancelled and written to DLQ

### 3.6 SSE Reconnection + Event Sequencing
- File: `src/taghdev/api/routes/assistant.py`
- Add `id:` field to every SSE event (monotonic counter per chat, stored in Redis `INCR`)
- Add 15s heartbeat: `": keepalive\n\n"` to prevent Cloudflare 100s timeout
- On reconnect with `Last-Event-ID`: replay missed events from Redis sorted set (TTL 5min)

---

## Phase 4: Security Hardening

**Goal:** Per-user containers cannot escape, public API cannot be DoS'd, secrets are managed safely.

### 4.1 Rate Limiting on All API Endpoints
- Add `slowapi>=0.1.9` (FastAPI-native rate limiter backed by Redis) to `pyproject.toml`
- File: `src/taghdev/api/main.py`
- Per-IP limits: `60/minute` for public endpoints, `300/minute` for authenticated users
- Per-user limits: `10/minute` for LLM-heavy endpoints (`/api/chat/send`)
- Specific limits: `/chat/login` → `5/minute` (brute force protection)

### 4.2 Security Headers Middleware
- File: `src/taghdev/api/main.py`
- Add `secure>=1.0.1` package or write custom middleware:
  ```
  Strict-Transport-Security: max-age=31536000
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Content-Security-Policy: default-src 'self'
  X-Request-ID: <uuid>    ← also binds to structlog context
  ```

### 4.3 CORS Configuration
- File: `src/taghdev/api/main.py`
- Current: likely permissive defaults
- Fix: explicit allowed origins from `CORS_ALLOWED_ORIGINS` env var
- Separate config for dev (localhost) vs prod (your actual domain)

### 4.4 Container Security (Per-User Instances)
- File: `src/taghdev/services/instance_compose_renderer.py`
- Add to every generated compose template:
  ```yaml
  security_opt:
    - no-new-privileges:true
    - seccomp:/etc/docker/seccomp/default.json
  read_only: false   # too restrictive for app containers, but...
  cap_drop:
    - ALL
  cap_add:
    - NET_BIND_SERVICE
  ```
- File: `Dockerfile.worker` — ensure the worker itself runs as non-root user `taghdev`
- For gVisor: install `runsc` on host, configure Docker daemon runtime, add `runtime: runsc` to per-instance compose templates

### 4.5 API Endpoint Audit + Auth Gaps
- Add `X-Request-ID` to all responses (tracing)
- Verify all `/api/` routes have explicit auth dependency (grep for unprotected routes)
- Add `Content-Security-Policy` nonce for inline scripts in the React app build

---

## Phase 5: Multi-Server Readiness

**Goal:** Any service can run N replicas without split-brain or data races.

### 5.1 Shared Workspace Volume
- Current: `/workspaces` bind-mount on single host
- For multi-server: replace with NFS mount or Ceph RBD shared across all nodes
- OR: move workspace IO to per-instance containers only (workspaces live inside the instance container, synced via git push)

### 5.2 Redis Sentinel (HA)
- Add Redis Sentinel setup to `docker-compose.prod.yml`
- 1 primary + 2 replicas + 3 sentinel processes
- ARQ and the app connect via Sentinel-aware URL: `redis+sentinel://...`

### 5.3 API Sticky Sessions for SSE
- SSE connections must hit the same API instance for the duration of a stream
- Fix: Nginx/Traefik upstream with `ip_hash` sticky sessions OR move SSE pub/sub fully to Redis (any API node can serve any stream via Redis Pub/Sub)
- Recommended: Redis Pub/Sub fan-out (already partially in place) — complete the migration

---

## Critical Files to Modify

| File | Changes |
|---|---|
| `pyproject.toml` | Add: uvloop, prometheus-fastapi-instrumentator, sentry-sdk, slowapi, pyresilience |
| `src/taghdev/api/main.py` | Prometheus instrumentation, Sentry init, CORS, security headers, rate limiter, request-id middleware |
| `src/taghdev/models/base.py` | SQLAlchemy pool tuning (pool_size=20, pool_pre_ping=True, pool_recycle=3600) |
| `src/taghdev/worker/arq_app.py` | uvloop, max_jobs=20, job_timeout, on_shutdown drain, DLQ hook, idempotency guard pattern |
| `src/taghdev/providers/llm/claude.py` | Langfuse `@observe` decorator on agent calls |
| `src/taghdev/services/tunnel_service.py` | Circuit breaker on Cloudflare API calls |
| `src/taghdev/services/credentials_service.py` | Circuit breaker on GitHub API calls |
| `src/taghdev/api/routes/assistant.py` | SSE event IDs, heartbeat, Last-Event-ID replay |
| `src/taghdev/services/instance_compose_renderer.py` | Security options in generated compose templates |
| `docker-compose.yml` | Add prometheus, grafana, redis_exporter, postgres_exporter, pgbouncer services |
| `docker-compose.prod.yml` | Uvicorn workers=9, log drivers, stop_grace_period |
| `Dockerfile.worker` | uvloop in requirements, non-root user verification |

---

## Verification

1. **Performance:** `ab -n 1000 -c 50 http://localhost:8000/health` — compare p99 before/after uvloop + worker count
2. **Observability:** Open Grafana at :3000, confirm `/metrics` endpoint returns ARQ + FastAPI metrics; trigger a Claude call and see trace in Langfuse
3. **Reliability:** Kill the worker mid-job → confirm in-flight job completes or lands in DLQ; send duplicate provision request → confirm idempotency guard fires
4. **Security:** Run `curl -s http://localhost:8000/api/chat -X POST` 61 times in a loop → confirm 429 on 61st; inspect response headers for HSTS + X-Frame-Options
5. **Fitness suite:** `python scripts/pipeline_fitness.py` — must pass all checks after changes
6. **Type check:** `python -m py_compile` on every modified Python file

---

## Suggested Implementation Order

1. Phase 1.1-1.3 (uvloop + pool tuning) — 30 min, zero risk
2. Phase 2.4 (Sentry) — 30 min, immediate error visibility  
3. Phase 2.1 (Prometheus + Grafana) — 2 hrs, adds dashboards
4. Phase 3.2 (idempotency guards) — 1 hr, critical for multi-server
5. Phase 3.1 (circuit breakers) — 2 hrs
6. Phase 3.3 (DLQ) — 2 hrs
7. Phase 4.1-4.3 (rate limiting + headers) — 2 hrs
8. Phase 2.2 (Langfuse) — 3 hrs
9. Phase 3.4-3.6 (graceful shutdown + SSE) — 3 hrs
10. Phase 4.4 (container security) — 4 hrs
11. Phase 1.4 + Phase 5 (PgBouncer + Redis HA) — last, needs infra changes
