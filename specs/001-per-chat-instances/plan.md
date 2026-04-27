# Implementation Plan: Per-Chat Isolated Instances

**Branch**: `001-per-chat-instances` (tracked on git branch `multi-instance`) | **Date**: 2026-04-23 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-per-chat-instances/spec.md`

## Summary

Replace today's single-shared-environment model with **one isolated development environment per chat**. A chat gets its own docker-compose project (app, web, node, db, redis, cloudflared sidecar), its own named Cloudflare Tunnel and public URL, its own short-lived credentials, and its own long-lived workspace rooted at `/workspaces/inst-<slug>/`. The LLM's role shrinks to a bounded fallback for step failures; provisioning itself is deterministic (`projctl` executes `guide.md` steps). Tasks run *inside* an existing instance via `docker exec` — they no longer create or destroy workspaces. Inactivity of 24 h + 60 min grace window tears the instance down; retention of metadata, audit trail, and the chat's working branch is **chat-lifetime** (see spec Clarifications Q3). Per-user soft cap defaults to 3 concurrent instances (Q2). Preview URLs are **public, unguessable-by-link** (Q1). Upstream outages surface as banners, never auto-teardowns (Q4).

The refactor is scoped to new code paths. Existing `mode="host"` and `mode="docker"` projects continue to run through today's bootstrap flow; a one-line router in `bootstrap.py` selects new vs legacy at task enqueue time.

## Technical Context

**Language/Version**: Python 3.12 async (orchestrator, API, workers, services, MCP servers). `projctl` ships as a separate binary — **decision: Go** for single-static-binary distribution (see research.md §1). No Node/TS in the runtime path.

**Primary Dependencies** (all already in `pyproject.toml` unless noted):
- FastAPI, ARQ, SQLAlchemy 2.x (async), asyncpg, redis.asyncio, pydantic v2
- httpx (async HTTP; for Cloudflare API + GitHub App API) — **timeout is mandatory per Principle IX**
- `claude_agent_sdk` (unchanged) — per-task MCP fleet bound to one instance
- aiogram (Telegram provider), slack-bolt (Slack provider)
- New runtime dependency: **PyJWT** (sign GitHub App JWTs for installation-token mints). Justified in PR description per constitution §Development Workflow.
- `cloudflared` binary (installed in worker image; invoked by `asyncio.create_subprocess_exec`)

**Storage**:
- PostgreSQL (durable source of truth per Principle VI) — new tables `instances`, `instance_tunnels`; new columns `web_chat_sessions.instance_id`, `tasks.instance_id`
- Redis (ephemeral only) — Redis lock `openclow:instance:<slug>` per Principle VI, reuse pattern from `workspace_service.py:36`
- Docker secrets — Cloudflare named-tunnel credential JSONs (`tagh-inst-<slug>-cf`), referenced (not embedded) from `instance_tunnels.credentials_secret`
- Filesystem — per-instance workspaces at `/workspaces/inst-<slug>/`, shared per-project clone cache at `/workspaces/_cache/<project_name>/` (read-only to instances)

**Testing**:
- pytest + pytest-asyncio for unit + service layer
- Integration tests hit a real Postgres + real Redis + real Docker daemon on the CI host (no DB mocks — matches CLAUDE.md "test what you build")
- Cloudflare API stubbed at the HTTP layer using `pytest-httpx` for fast tests; one nightly E2E test against a real CF zone (gated env var)
- `ruff` / `flake8-async` for async correctness (Principle IX enforcement)
- Compose-template lint: a unit test renders every per-instance compose file and asserts no service except `cloudflared` contains `ports:` (Principle V + arch doc §5.5)

**Target Platform**: Linux server (single-host control plane). `docker compose` required on the host; Docker Engine 24+. No Kubernetes in v1.

**Project Type**: Web service — FastAPI `api/` + ARQ `worker/` + chat providers; adds a separate Go CLI (`projctl/`) and a Docker image for `projctl` published at `ghcr.io/<org>/projctl:<ver>`.

**Performance Goals**:
- Warm provision (image + deps cached, branch already cloned): **<2 min end-to-end**, matches SC-002.
- Cold provision (image pull, composer + npm install fresh): **<5 min**, matches SC-002.
- Resume: **<2 min** (SC-004); most of the cost is re-running compose up.
- Hot-reload latency browser-visible: **<3 s** for 95 % of edits (SC-005).
- Reaper cycle: every 5 min; `FOR UPDATE SKIP LOCKED` limit 50 per run (arch doc §6).

**Constraints**:
- **No host port publishing** on any instance service except `cloudflared`'s internal metrics port (Principle V; enforced by compose-template lint).
- **Exactly one active tunnel per instance** (partial unique index on `instance_tunnels(instance_id)` where `status='active'`).
- **Per-user concurrent cap default 3** (FR-030a), operator-configurable via `platform_config` (`category="instance"`, `key="per_user_cap"`).
- **60-min grace window default** after 24 h idle threshold (FR-008), operator-configurable.
- Every external call has an **explicit timeout** (Principle IX).
- No `Bash` / raw `docker` / `host_run_command` exposed to new-mode coding agents (Principle III).

**Scale/Scope**:
- Target 50 concurrent active instances on a single 32 GB VPS (SC-006).
- Cloudflare free tier allows 1,000 named tunnels per account; comfortably above 50.
- DNS records: 2–3 per instance (web, hmr, optional ide); 500 instances = 1,500 records, inside CF limits.
- Disk: ~500 MB per workspace; shared per-project cache amortises vendor/node_modules downloads.
- RAM: ~200 MB idle / ~1 GB active per instance (arch doc §12).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.1.0 has nine principles. The plan is evaluated against each:

| # | Principle | Pass/Fail | Evidence |
|---|-----------|-----------|----------|
| I | Per-Chat Instance Isolation (NON-NEGOTIABLE) | **PASS** | New `instances` table; partial unique on `chat_session_id` for active statuses (arch §3.1). Per-task MCP fleet bound at argv spawn (arch §7.5.1). FR-002, FR-003, FR-019–023. |
| II | Deterministic Execution Over LLM Drift | **PASS** | Provisioning = `projctl` runs declarative `guide.md` steps. LLM is step-failure fallback only, bounded envelope (≤200 stdout + ≤200 stderr, max 3 attempts). FR-024–027. |
| III | No Ambient Authority for Agents (NON-NEGOTIABLE) | **PASS** | `instance_mcp` / `workspace_mcp` / `git_mcp` bound to one instance by argv. No tool accepts an instance/project/workspace identifier. New-mode agents never get `Bash`/`docker`/`host_run_command`. Tests assert rendered MCP manifests carry no "which instance" tools. |
| IV | Credential Scoping & Log Redaction | **PASS** | Per-instance CF creds in Docker secrets; per-instance GitHub App installation tokens (≤1h TTL); DB password generated per instance; heartbeat HMAC generated per instance. Redactor extends `audit_service.py` and runs on BOTH chat-UI and LLM-fallback paths (FR-033). |
| V | Egress-Only Network Surface | **PASS** | Compose-template lint fails build if any non-`cloudflared` service has `ports:`. Ingress only via named tunnel → sidecar → compose-network service-name DNS. FR-031. |
| VI | Durable State, Idempotent Lifecycle | **PASS** | Postgres is source of truth; Redis holds only ephemeral locks. `provision/destroy/rotate/reap` idempotent, documented in research.md §4. Replaces worker-local `_active_processes` dict at `tunnel_service.py:29` per audit finding. |
| VII | Verified Work, No Half-Features | **PASS** | Implementation order (plan §Implementation PR sequence, below) ships each PR as a vertical slice with rollback notes. Legacy code paths untouched until parity E2E passes. |
| VIII | Root-Cause Fixes Over Bypasses (NON-NEGOTIABLE) | **PASS** | No `--no-verify`, no silenced tests, no catch-and-swallow. Plan assumes green CI at every step. |
| IX | Async-Python Correctness | **PASS** | All I/O via `await`. CF API uses `httpx.AsyncClient` with explicit timeout. `cloudflared` invocation uses `asyncio.create_subprocess_exec`. Heartbeat polling and reaper are ARQ jobs. `ruff` / `flake8-async` rules wired into pre-commit. |

**Result**: Constitution Check passes on all nine principles. No entries in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/001-per-chat-instances/
├── plan.md              # This file (/speckit.plan command output)
├── spec.md              # Feature spec + clarifications
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── projctl-stdout.schema.json
│   ├── llm-fallback-envelope.schema.json
│   ├── heartbeat-api.md
│   └── instance-service.md
├── checklists/
│   └── requirements.md  # /speckit.specify quality checklist
└── tasks.md             # /speckit.tasks output — NOT created here
```

### Source Code (repository root)

```text
src/openclow/
├── api/                              # FastAPI dashboard — unchanged topology
│   └── routers/
│       └── instances.py              # NEW: /internal/instances/<slug>/heartbeat, /instances list
├── models/
│   ├── instance.py                   # NEW: Instance + InstanceStatus enum
│   ├── instance_tunnel.py            # NEW: InstanceTunnel + TunnelStatus enum
│   ├── web_chat.py                   # EXTEND: add instance_id FK (nullable)
│   ├── task.py                       # EXTEND: add instance_id FK (nullable)
│   └── project.py                    # EXTEND: add mode='container' enum value
├── services/
│   ├── instance_service.py           # NEW: provision/touch/terminate/get_or_resume/list_active
│   ├── tunnel_service.py             # REWRITE: CF named-tunnel provisioning; legacy quick-tunnel moved to legacy_tunnel_service.py
│   ├── legacy_tunnel_service.py      # NEW (holds old quick-tunnel code from current tunnel_service.py)
│   ├── credentials_service.py        # NEW: github_push_token(), heartbeat_secret(), cf_token()
│   ├── inactivity_reaper.py          # NEW: ARQ cron job
│   ├── audit_service.py              # EXTEND: redactor module usable on chat + LLM paths
│   └── instance_compose_renderer.py  # NEW: renders per-instance docker-compose.yml + cloudflared config
├── worker/
│   ├── arq_app.py                    # EXTEND: register instance_tasks + reaper cron
│   └── tasks/
│       ├── instance_tasks.py         # NEW: provision_instance, teardown_instance, rotate_github_token
│       ├── chat_task.py              # EXTEND: route mode='container' → InstanceService.get_or_resume
│       └── bootstrap.py              # EXTEND: one-line router at top; legacy path untouched below
├── mcp_servers/
│   ├── instance_mcp.py               # NEW: exec/logs/restart/ps/health bound to one compose project
│   ├── workspace_mcp.py              # NEW: read/write/edit/list/search bound to one workspace root
│   ├── git_mcp.py                    # EXTEND: accept --workspace + --branch; reject branch-leaving ops
│   └── docker_mcp.py                 # KEEP but legacy-only (host/docker modes); not loaded for container mode
├── providers/llm/
│   └── claude.py                     # EXTEND: _mcp_instance(), _mcp_workspace(), _mcp_git_pinned() factories
└── setup/
    └── compose_templates/
        └── laravel-vue/              # NEW: compose.yml + cloudflared.yml + vite.config.js snippet + projctl

projctl/                              # NEW top-level Go project (separate image)
├── go.mod
├── cmd/projctl/
│   └── main.go
├── internal/
│   ├── steps/                        # up, doctor, down, step --retry, explain, heartbeat
│   ├── guide/                        # guide.md parser + success-check runner
│   └── state/                        # /var/lib/projctl/state.json on instance-persistent volume
├── Dockerfile                        # published as ghcr.io/<org>/projctl:<ver>
└── tests/

alembic/versions/
├── 011_instance_tables.py            # NEW: instances, instance_tunnels, extensions
└── 012_project_mode_container.py     # NEW: allow mode='container' on projects

tests/
├── unit/
│   ├── test_instance_service.py
│   ├── test_tunnel_service.py
│   ├── test_credentials_service.py
│   ├── test_audit_redactor.py
│   └── test_compose_renderer.py
├── contract/
│   ├── test_projctl_stdout_schema.py
│   ├── test_llm_fallback_envelope.py
│   └── test_heartbeat_api.py
└── integration/
    ├── test_provision_teardown_e2e.py     # real Docker + real Postgres + stubbed CF
    ├── test_inactivity_reaper.py
    ├── test_per_user_cap.py
    ├── test_agent_isolation.py            # adversarial: prove an agent in A can't touch B
    └── test_compose_no_ports_lint.py
```

**Structure Decision**: Extend the existing single Python package (`src/openclow/`) rather than introducing a new service; the feature is a topology change to the existing orchestrator, not a new service boundary. The one exception is `projctl/`, which ships as a separate Go module so it can be baked into arbitrary project images via `COPY --from=ghcr.io/<org>/projctl:<ver>`. Tests mirror the existing `tests/unit` + `tests/integration` split and add a `tests/contract` directory for schema contracts (projctl stdout, LLM envelope, heartbeat API).

## Implementation PR Sequence

Directly follows the constitution's "Verified Work" rule: each PR ships as a complete vertical slice with rollback notes. Sequence mirrors arch doc §14 and the architecture-doc memory note.

1. **`guide.md` spec + `project.yaml` schema** (docs only; unblocks projctl).
2. **`projctl` step runner** + JSON-line stdout contract + success-check semantics (new Go project; published Docker image; not yet used by the orchestrator).
3. **Laravel+Vue compose template** (`setup/compose_templates/laravel-vue/`) — rendered offline, linted for no-ports.
4. **TunnelService rewrite** + `instance_tunnels` migration + Cloudflare API client (httpx, explicit timeouts, per Principle IX). Legacy quick-tunnel code moved verbatim to `legacy_tunnel_service.py`.
5. **InstanceService** + `instances` migration + state machine + per-user cap enforcement (FR-030a/b).
6. **InactivityReaper** + ARQ cron wiring + dry-run mode (FR-007/008).
7. **MCP binding overhaul** — `instance_mcp`, `workspace_mcp`, extended `git_mcp`; MCP manifest assertions for "no ambient identifier" (Principle III).
8. **LLM fallback + redactor** + envelope schema (FR-024–027; arch §9).
9. **Upstream-outage banner policy** (FR-027a/b/c) — heartbeat + health integration.
10. **Retention cascade on chat delete** (FR-013a/b/c) — wire to existing chat-deletion path.
11. **E2E parity test**: spin Laravel+Vue instance, verify HMR via tunnel, cross-chat isolation, teardown leaves zero residue.
12. **bootstrap.py router**: route `mode='container'` → InstanceService; legacy modes unchanged.

Each PR is independently rollback-able (a failed PR 5 does not break PR 4's TunnelService — the router in PR 12 is what actually consumes the new path).

## Complexity Tracking

> Empty — Constitution Check passes on all nine principles with no violations.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| *(none)*  | *(none)*   | *(none)*                             |
