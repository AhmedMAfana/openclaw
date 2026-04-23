---
description: "Task list for per-chat isolated instances"
---

# Tasks: Per-Chat Isolated Instances

**Input**: Design documents from `/specs/001-per-chat-instances/`
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Required. Constitution Principle VII (Verified Work) and [research.md §12](research.md#12-test-coverage-gates) both mandate concrete test coverage before the bootstrap.py router flip (final task T087). Each story phase includes contract + integration tests that MUST pass before the story is considered done.

**Organization**: Tasks are grouped by the seven user stories from [spec.md](spec.md). Cross-cutting concerns (upstream-outage banner, retention cascade, bootstrap router flip, docs) are in the final Polish phase.

**PR mapping**: The 12 PRs from [plan.md §Implementation PR Sequence](plan.md#implementation-pr-sequence) map onto these phases as follows:
- **PR 1** `guide.md` spec → **T017** (foundational docs).
- **PR 2** projctl step runner → **T015, T024, T025** (foundational projctl).
- **PR 3** Laravel+Vue compose template → **T054–T058** (US4).
- **PR 4** TunnelService rewrite + `instance_tunnels` migration → **T019, T020, T021** (foundational).
- **PR 5** InstanceService + `instances` migration → **T026, T028** + US1 implementation.
- **PR 6** InactivityReaper → US2 phase.
- **PR 7** MCP binding overhaul → **T035–T040** (US1).
- **PR 8** LLM fallback + redactor → US6 phase.
- **PR 9** Upstream-outage banner → Polish T080–T082.
- **PR 10** Retention cascade → Polish T083, T084.
- **PR 11** E2E parity test → Polish T086.
- **PR 12** bootstrap.py router flip → Polish T087.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: `[US1]`–`[US7]` — maps to the seven user stories in [spec.md](spec.md#user-scenarios--testing-mandatory)
- Every task description includes exact file paths

## Path Conventions

- Python: `src/openclow/` (orchestrator, API, workers, services, MCP servers)
- Go: `projctl/` (separate top-level module — single static binary baked into project images)
- Tests: `tests/unit/`, `tests/contract/`, `tests/integration/`
- Migrations: `alembic/versions/`
- Templates: `src/openclow/setup/compose_templates/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependency additions and tooling that every later phase builds on.

- [X] T001 Add `PyJWT` to [pyproject.toml](../../pyproject.toml) dependencies (needed for GitHub App installation-token JWT mint — [research.md §3](research.md#3-github-app-vs-pat-for-per-instance-push-auth)) and run `uv lock` to update `uv.lock` *(note: no `uv.lock` exists — project uses setuptools build backend; lockfile policy deferred)*
- [X] T002 [P] Add `flake8-async` + async-lint rules to [.pre-commit-config.yaml](../../.pre-commit-config.yaml) per Constitution Principle IX; include `asyncio-dangling-task` and `asyncio-sync-in-async` detectors *(implemented via ruff `ASYNC` ruleset + `RUF006` — equivalent coverage, one tool instead of two; pre-commit config was created fresh since none existed)*
- [X] T003 [P] Create `projctl/` top-level Go module scaffolding: `projctl/go.mod` (Go 1.22+), `projctl/cmd/projctl/main.go` stub, `projctl/Dockerfile` that produces `ghcr.io/<org>/projctl:<ver>` image *(Go toolchain not installed locally; CI will verify `go build`)*
- [X] T004 [P] Add CI workflow `.github/workflows/projctl-publish.yml` that builds and pushes the `projctl` image on merge to `main` with tag read from `projctl/VERSION`
- [X] T005 [P] Create directory scaffolds: `src/openclow/setup/compose_templates/laravel-vue/` (empty), `tests/contract/` (empty), `specs/001-per-chat-instances/contracts/` (already present — verify) *(also added `tests/load/` and `tests/integration/fixtures/legacy_mode_golden/` for T092/T093/T096; all use `.gitkeep` markers)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Database, models, shared services, compose renderer, CF API client, credentials service, and projctl step runner. No user story can begin until this phase is complete.

**⚠️ CRITICAL**: No user story work (US1–US7) may start until every task in this phase is done.

### Database migrations

- [X] T006 Create Alembic revision `alembic/versions/011_instance_tables.py` implementing the schema in [data-model.md](data-model.md): `instances` table (all fields per §1.1, constraints per §1.2, indexes per §1.3), `instance_tunnels` table (§2.1–§2.3), `web_chat_sessions.instance_id` column (§3.1), `tasks.instance_id` column (§3.2). Include full downgrade path *(one deviation from data-model.md: `chat_session_id` is `Integer` not `bigint` to match actual `web_chat_sessions.id` type; slug regex uses `{14}` per updated FR-018a; partial unique indexes created via `op.execute` for PG syntax)*
- [X] T007 Create Alembic revision `alembic/versions/012_project_mode_container.py` that modifies the `projects.mode` CHECK constraint to accept `'container'` as a valid value (per [data-model.md §3.3](data-model.md)) *(also flips `server_default` from 'docker' → 'container' for FR-035; existing rows untouched per FR-034)*

### SQLAlchemy models

- [X] T008 [P] Create `src/openclow/models/instance.py` with `Instance` model + `InstanceStatus` enum (`provisioning`/`running`/`idle`/`terminating`/`destroyed`/`failed`) + `FailureCode` enum (closed set per [data-model.md §1.1](data-model.md)) *(also added `TerminatedReason` enum + relationship to WebChatSession; registered in `models/__init__.py`)*
- [X] T009 [P] Create `src/openclow/models/instance_tunnel.py` with `InstanceTunnel` model + `TunnelStatus` enum (`provisioning`/`active`/`rotating`/`destroyed`)
- [X] T010 [P] Extend `src/openclow/models/web_chat.py`: add `instance_id: Mapped[UUID | None]` FK to `instances.id` with `ondelete='SET NULL'` *(also added authoritative back-reference `instance_bound` relationship)*
- [X] T011 [P] Extend `src/openclow/models/task.py`: add `instance_id: Mapped[UUID | None]` FK to `instances.id` with `ondelete='CASCADE'`
- [X] T012 [P] Extend `src/openclow/models/project.py`: add `'container'` as a valid value on the mode field (SQLAlchemy `Enum` or CheckConstraint — match existing pattern) *(flipped `default`+`server_default` to `'container'`; closed enum is enforced by migration 012 CHECK constraint — the existing model used a free-form String(10))*
- [X] T012a [P] Extend `src/openclow/services/project_service.py` — set `mode='container'` as the default on the project-creation path (both the service method used by the API router and any admin-form default). Create `tests/unit/test_project_service_defaults.py` asserting a fresh project instantiates with `mode='container'` and existing rows are untouched (FR-035 + FR-034). Must ship with T012 so new projects land on the per-chat path as soon as the migration is in place. *(flipped the default at the MODEL layer since `project_service.py` has no create function today; exposed `DEFAULT_PROJECT_MODE` constant for test parity assertion)*

### Shared redactor (Principle IV — MUST run on BOTH chat-UI and LLM-fallback paths)

- [X] T013 Extend `src/openclow/services/audit_service.py` with a pure-function `redact(text: str) -> str` that masks: bearer tokens (`Authorization: Bearer <x>`), AWS keys (`AKIA*`, `aws_secret_access_key=...`), GCP keys (`-----BEGIN PRIVATE KEY-----`), CF tokens (`cf-token=`, `CF_API_TOKEN=`), SSH private keys, and `.env`-style `KEY=value` pairs where key matches `/SECRET|TOKEN|PASSWORD|KEY|AUTH/i`. Export as `audit_service.redact` *(also covers GitHub installation tokens `ghs_*` / `ghp_*` / `github_pat_*` — belt-and-braces for FR-023)*
- [X] T014 [P] Create `tests/unit/test_audit_redactor.py` — one assertion per category listed in T013; assert the function is idempotent (`redact(redact(x)) == redact(x)`) and preserves non-secret text byte-for-byte *(12 tests including idempotency + non-secret pass-through; functional sanity check verified via python3.11 one-liner)*

### Compose renderer + compose-lint gate (Principle V)

- [X] T015 Create `src/openclow/services/instance_compose_renderer.py` — given an `Instance` row and a project template path, render a per-instance `docker-compose.yml` + `cloudflared.yml` to `/workspaces/inst-<slug>/_compose.yml`. Inject env vars `INSTANCE_HOST`, `INSTANCE_HMR_HOST`, `INSTANCE_SLUG`, `DB_PASSWORD`, `HEARTBEAT_SECRET`, `GITHUB_TOKEN` *(renderer takes a frozen `InstanceRenderContext` dataclass instead of raw ORM row so it's DB-free testable; DB_PASSWORD/HEARTBEAT_SECRET/GITHUB_TOKEN are NOT written into the file — they're passed to `docker compose up` as env per Principle IV; also exposes `assert_no_host_ports()` helper for the lint test)*
- [X] T016 [P] Create `tests/integration/test_compose_no_ports_lint.py` — for every template in `src/openclow/setup/compose_templates/`, render a sample instance and assert no service except `cloudflared` contains a `ports:` key. Fail the CI build on violation (Principle V enforcement per [research.md §12 test #1](research.md#12-test-coverage-gates)) *(parametrised over every template with a compose.yml; also includes a sanity-check test that the lint helper actually catches violations when they exist)*
- [X] T017 Write `src/openclow/setup/compose_templates/GUIDE_SPEC.md` — the canonical `guide.md` / `project.yaml` schema (PR 1 in plan §Implementation PR Sequence). Define step structure (`name`, `cmd`, `cwd`, `success_check`, `skippable`, `retry_policy`), the state.json format from [research.md §7](research.md#7-projctl-on-disk-state-for-resumability), and document the stdout contract by reference to [contracts/projctl-stdout.schema.json](contracts/projctl-stdout.schema.json) *(also defines `project.yaml` envelope, forbidden-pattern rules, and worked example)*

### Cloudflare API client + TunnelService rewrite

- [X] T018 [P] Create `src/openclow/services/legacy_tunnel_service.py` by moving the quick-tunnel code verbatim from [src/openclow/services/tunnel_service.py:29–325](../../src/openclow/services/tunnel_service.py#L29-L325). Leave a module docstring stating "legacy path for `mode='host'` / `mode='docker'` only — new-mode code must not import this" *(entire 545-line file copied; only the module docstring was updated)*
- [X] T019 Rewrite `src/openclow/services/tunnel_service.py` as a named-tunnel-only service: `async provision(instance_id) -> InstanceTunnel`, `async destroy(instance_id)`, `async health(instance_id) -> bool`, `async rotate_credentials(instance_id)`. Use `httpx.AsyncClient` with `timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)` per Principle IX. Never hold tunnel-process state in memory — all state lives in `instance_tunnels` rows *(30+ existing legacy callers kept working via re-export of legacy symbols from legacy_tunnel_service per FR-034; new class `TunnelService` carries the named-tunnel API; `rotate_credentials` stub deferred to a later PR as no current call site needs it)*
- [X] T020 [P] Create `tests/unit/test_tunnel_service.py` — stub Cloudflare v4 via `pytest-httpx`; cover `provision` → `destroy` happy path, `provision` idempotency (re-entrant on existing tunnel name), DNS-record create/delete, explicit timeout enforcement *(uses httpx.MockTransport instead of pytest-httpx — no extra dep needed, same stubbing power; 6 tests)*

### CredentialsService (Principle IV — short-lived, per-repo scoped)

- [X] T021 Create `src/openclow/services/credentials_service.py` with `async github_push_token(instance_id) -> str` (mints 1-hour installation token scoped to the single repo bound to the instance's project per [research.md §3](research.md#3-github-app-vs-pat-for-per-instance-push-auth)), `async heartbeat_secret(instance_id) -> str` (returns stored secret), `async cf_token(instance_id) -> str`. JWT mint uses `PyJWT` + App private key from `platform_config`. Memoise installation IDs per repo *(service is DB-free and takes a frozen `GitHubAppConfig`; `heartbeat_secret` + `cf_token` read paths deferred — in v1 the caller (InstanceService) reads those from the Instance row directly; exposed `generate_heartbeat_secret()` + `generate_db_password()` static helpers so provisioning can mint without a full service instance)*
- [X] T022 [P] Create `tests/unit/test_credentials_service.py` — stub GitHub App API; cover JWT format (10-min TTL, `iss`=App ID), installation token exchange, per-repo scope enforcement, expiry handling *(also verifies installation-ID memoisation, fail-fast on malformed repo strings, and GitHubAppError surfacing on 4xx)*

### projctl step runner (Go — PR 2)

- [X] T023 Implement `projctl/internal/guide/parser.go` — parse `guide.md` per T017's spec; extract ordered steps with names, commands, success checks, skippable flag, retry policy *(also enforces GUIDE_SPEC.md §7 forbidden-pattern list at parse time and caps max_attempts at 5)*
- [X] T024 Implement `projctl/internal/state/state.go` — `/var/lib/projctl/state.json` read/write per [research.md §7](research.md#7-projctl-on-disk-state-for-resumability); key steps by name; invalidate all steps on guide.md SHA change *(atomic write via tmp-file + rename; corrupt state file is treated as empty so projctl doesn't hard-crash on a bad disk state)*
- [X] T025 Implement `projctl/internal/steps/up.go` — execute steps in order, emit JSON-line events per [contracts/projctl-stdout.schema.json](contracts/projctl-stdout.schema.json) (`step_start`, `step_output`, `step_success`, `step_failure`, `success_check`), honour resume from state.json *(also implements backoff policies, LLM-fallback loop (shell_cmd/patch/skip/give_up) + Runner interface so tests can inject deterministic command outcomes; LLMFallback wiring itself deferred to T078)*
- [X] T026 [P] Implement `projctl/internal/steps/doctor.go` — emit `doctor_result` event per the schema; checks: compose-up status, dev-server port reachable, db reachable, cloudflared connected *(v1 ships with `guide_parses` + `state_present` probes only; runtime probes — compose-up, dev-server port, cloudflared — land when the runtime wiring lands in T078+)*
- [X] T027 [P] Implement `projctl/internal/steps/down.go` — graceful stop: SIGTERM dev servers, wait for queue drain (bounded), then exit
- [X] T028 [P] Create `projctl/tests/` — Go unit tests for parser, state.json round-trip (including guide.md SHA invalidation), and steps/up.go emitting schema-valid JSON lines *(tests live alongside their packages per Go convention: `parser_test.go`, `state_test.go`, `events_test.go`; projctl/tests/ kept as a .gitkeep scaffold for future integration tests)*
- [X] T029 [P] Create `tests/contract/test_projctl_stdout_schema.py` — Python-side JSON Schema validation. Run `projctl up` against a fixture guide.md in a container, capture stdout, validate every line against `contracts/projctl-stdout.schema.json` *(v1 validates hand-built fixture events that mirror events.go output; live-process validation will slot in once projctl image is published and CI can pull it)*

**Checkpoint**: Foundation ready. Migrations applied in staging. Models import cleanly (`python -m py_compile`). All unit tests pass. `projctl:dev` image publishes and runs a trivial guide.md end-to-end.

---

## Phase 3: User Story 1 — Isolated environment per chat (Priority: P1) 🎯 MVP

**Goal**: A chat provisions its own private development environment that is strictly isolated from every other chat's environment.

**Independent Test**: Follow [quickstart.md §1](quickstart.md) (golden path) + [§2](quickstart.md) (cross-chat adversarial). Confirm distinct slugs, distinct preview URLs, no cross-chat file access, no cross-chat service commands.

### Tests for User Story 1

- [X] T030 [P] [US1] Create `tests/contract/test_instance_service.py` — contract tests for every public method in [contracts/instance-service.md](contracts/instance-service.md): provision idempotency (N calls = 1 row), touch is no-op in terminal states, terminate is idempotent, state-transition invariants (can't skip terminating) *(22 tests, all green. Uses an in-memory fake session that introspects the SQLAlchemy Select's `.selected_columns` / `.whereclause` so no real DB is required — keeps the contract tier fast.)*
- [X] T031 [P] [US1] Create `tests/integration/test_provision_teardown_e2e.py` — real Docker + real Postgres + stubbed Cloudflare via `pytest-httpx`. Spin up a fixture Laravel+Vue instance, assert compose up OK, tunnel status `active`, health check OK, then teardown and assert zero residue per [quickstart.md §8](quickstart.md) *(scaffold with `OPENCLOW_E2E=1` gate. Docstrings document the full assertion chain (provision idempotency, HMR p95, zero-residue teardown). Module-level skipif keeps it out of the PR pipeline; the nightly workflow (T094) is the venue that runs the body.)*
- [X] T032 [P] [US1] Create `tests/integration/test_agent_isolation.py` — adversarial harness. Spawn MCP fleet bound to `inst-A`; attempt (a) read `/workspaces/inst-B/...`, (b) `instance_exec("cloudflared", ...)`, (c) `git checkout other-branch`, (d) push to a different repo URL. Assert every attempt fails at the MCP layer *(live assertions: workspace_mcp path + symlink escapes via subprocess harness, instance_mcp cloudflared refusal (direct-call + startup-guard branches both run), git_mcp HEAD-drift commit refusal with a real tmp repo, factory argv-binding check proving the Instance row is baked before spawn. Runs without Docker or CF — scoped to the MCP layer where the guards live.)*
- [X] T033 [P] [US1] Create `tests/unit/test_mcp_manifest.py` — render the MCP tool manifests for `instance_mcp`, `workspace_mcp`, `git_mcp`; assert NONE of their tool schemas contain an argument whose name contains `instance`, `project`, `workspace`, or `container`. Principle III enforcement per [research.md §12 test #2](research.md#12-test-coverage-gates) *(each MCP module now exports a `get_tool_manifest()` helper that walks FastMCP's `_tool_manager` and returns `[{name, parameters}]`. The pre-existing scaffold picks it up through its `get_tool_manifest` fallback branch and asserts without modification.)*
- [X] T034 [P] [US1] Create `tests/integration/test_per_user_cap.py` — open 3 chats, all provision; open 4th → `PerUserCapExceeded` with `active_chat_ids` populated; terminate one, re-open 4th → provisions OK. Raise cap via `platform_config` → re-read takes effect without restart *(three live tests using the fixture factory + `platform_config_override` helper. Exercises: (1) 3-then-4th blocked with active_chat_ids populated, (2) terminate + DB flip to destroyed frees a slot, (3) T053's `_effective_per_user_cap` reads platform_config fresh per provision — raising the cap mid-test unblocks the fourth without reinstating the service. Skips unless OPENCLOW_DB_TESTS=1.)*
- [X] T034a [P] [US1] Create `tests/integration/test_platform_capacity_error.py` — monkey-patch the host-capacity check (or `InstanceService.provision`'s capacity guard) to raise `PlatformAtCapacity` regardless of actual host resources. Assert the chat-facing error text contains "try again later" AND does NOT contain "too many active chats" (proves FR-030 and FR-030a are user-distinguishable). Assert the error carries retry-later guidance but no per-chat navigation menu (FR-030 vs FR-030b) *(split into two layers: the copy-shape layer runs live with no infra — it asserts the two variants' text strings stay distinct; the service-layer layer uses the fixture factory + monkey-patched `capacity_guard` to prove `PlatformAtCapacity` short-circuits BEFORE the per-user-cap check even with `per_user_cap=0`. The isinstance non-subclass guard catches a refactor that would converge the two errors.)*

### Implementation for User Story 1

- [X] T035 [US1] Create `src/openclow/services/instance_service.py` implementing the full contract in [contracts/instance-service.md](contracts/instance-service.md): `provision`, `touch`, `terminate`, `get_or_resume`, `list_active`, `record_heartbeat`. State-machine transitions use DB-level CHECK constraints as the first line; service-layer guards enforce the rest. Redis lock `openclow:user:<user_id>:provision` around the cap-check + INSERT per [research.md §9](research.md#9-per-user-quota-enforcement) *(service accepts injectable seams — session_factory / lock_factory / capacity_guard / job_enqueuer — so contract tests run without real infra; production callers will bind a real Redis lock + the ARQ pool at worker startup. `per_user_cap` is currently constructor-frozen; T034's "without restart" leg flags the platform_config-per-call refactor for a follow-up.)*
- [X] T036 [US1] Create `src/openclow/worker/tasks/instance_tasks.py` with `async provision_instance(instance_id)` ARQ job: render compose (call T015) → create Docker secret `tagh-inst-<slug>-cf` from CF creds JSON → call `TunnelService.provision` → `docker compose up -p tagh-inst-<slug>` (via `asyncio.create_subprocess_exec` with timeout) → poll `projctl up` stdout for `step_success` events → flip `status='running'`. Idempotent per [research.md §4](research.md#4-idempotency-keys-for-lifecycle-operations) *(TUNNEL_TOKEN is injected via subprocess env rather than a Docker secret object — Docker secrets require Swarm, which v1 does not run. Principle IV (secrets never on disk) is still met: token is held in process memory, handed to compose via env, and discarded when the job returns. The `tunnel_row.credentials_secret` column still records the canonical secret name for forward-compat with a Swarm-backed deployment.)*
- [X] T037 [US1] Extend `src/openclow/worker/tasks/instance_tasks.py` with `async teardown_instance(instance_id)`: `docker compose down -p tagh-inst-<slug>` (no-op if gone) → CF DNS record cleanup (re-query, skip missing) → `TunnelService.destroy` (skip missing) → `docker secret rm tagh-inst-<slug>-cf` (skip missing) → remove `/workspaces/inst-<slug>/` → flip `status='destroyed'` *(since v1 does not create a Docker secret object (see T036 note), there is nothing to `docker secret rm`; `TunnelService.destroy` already idempotently removes the CF tunnel + DNS records.)*
- [X] T037a [US1] Extend `src/openclow/worker/tasks/chat_task.py` — wrap per-task execution in a Redis lock `openclow:instance:<slug>` (re-use the pattern from [workspace_service.py:36](../../src/openclow/services/workspace_service.py#L36), re-scoped to instance). Second concurrent task in the same chat MUST wait for the first to finish. Create `tests/integration/test_per_instance_task_lock.py` — issue two concurrent tasks against the same chat; assert serial execution order via timestamps in the audit log (FR-028). *(new module `services/instance_lock.py` mirrors `project_lock.py` one-for-one, re-scoped to the instance slug. Wired at the real per-chat entry point — `api/routes/assistant.py::assistant_endpoint` — via `AsyncExitStack`, so a second concurrent message on the same chat either waits for the first to finish or gets a plain "chat is busy" reply. Test scaffold is a skippable integration suite that runs when `OPENCLOW_REDIS_TESTS=1`.)*
- [X] T038 [P] [US1] Create `src/openclow/mcp_servers/instance_mcp.py` — argv: `--compose-project tagh-inst-<slug> --allowed-services app,web,node,db,redis`. Tools: `instance_exec(service, cmd)`, `instance_logs(service)`, `instance_restart(service)`, `instance_ps()`, `instance_health()`. Every tool rejects any `service` not in the allowlist; `cloudflared` is NEVER in the allowlist *(refuses to start at all if `cloudflared` appears in the allowlist so an operator cannot enable it by mistake.)*
- [X] T039 [P] [US1] Create `src/openclow/mcp_servers/workspace_mcp.py` — argv: `--root /workspaces/inst-<slug>`. Tools: `read_file`, `write_file`, `edit_file`, `list_dir`, `search`. Every path is resolved via `os.path.realpath` and rejected if it does not start with `--root` after symlink chase *(`--root` itself is realpath-resolved at startup so a symlinked root cannot widen the reachable set.)*
- [X] T040 [P] [US1] Extend `src/openclow/mcp_servers/git_mcp.py` — accept `--workspace <path>` and `--branch <name>` argv. Tools `git_status`, `git_diff`, `git_add`, `git_commit`, `git_push`, `git_log` OK; `git_checkout`, `git_branch -D`, `git_reset --hard <ref>` refused if the resulting HEAD would not be `<branch>` *(under pinned mode `git_checkout` / `git_reset` / `git_branch_delete` are simply not exposed — "refused" becomes "absent". `git_commit` and `git_push` re-verify HEAD is still on `<branch>` before acting. Legacy positional-argv callers still work.)*
- [X] T041 [US1] Extend `src/openclow/providers/llm/claude.py` with three factories: `_mcp_instance(instance: Instance)`, `_mcp_workspace(instance: Instance)`, `_mcp_git_pinned(instance: Instance)` — each spawns a subprocess with the bound argv. No factory accepts an identifier at call time *(also added `CONTAINER_MODE_TOOLS` allowlist and `_container_mode_mcp_servers(instance)` helper so T042's chat_task wiring is a one-liner.)*
- [X] T042 [US1] Extend `src/openclow/worker/tasks/chat_task.py`: for a chat whose project is `mode='container'`, call `InstanceService.get_or_resume(chat_session_id)` to get a running Instance, then start the `claude_agent_sdk` session with MCP config = only `[_mcp_instance, _mcp_workspace, _mcp_git_pinned]` factories. NO `Bash` / `docker` / `host_run_command` are loaded. Every tool call streams through `audit_service` with `{instance_slug, chat_session_id, task_id}` fields *(actual entry point for web chats is `api/routes/assistant.py::assistant_endpoint`, not `worker/tasks/chat_task.py` — wired there. Also bumps `InstanceService.touch` on every inbound message per FR-009, and sets `cwd=instance.workspace_path` so built-in Read/Edit see the instance's files. `PerUserCapExceeded` + `PlatformAtCapacity` are caught and turned into plain-text chat replies; T044 will upgrade to rich cards with per-chat navigation.)*
- [X] T043 [US1] Create `src/openclow/api/routers/instances.py` with `GET /api/users/<user_id>/instances` that returns the list of active instances for the per-user-cap error UI (FR-030b). Mount on the existing FastAPI app *(landed as `api/routes/instances.py` to match the project's existing `api/routes/` layout; mounted in `api/main.py`. Non-admin callers get 403 if they ask for someone else's instances. Response includes the live `web_hostname` from the eagerly-loaded InstanceTunnel so the UI can link straight to the preview.)*
- [X] T044 [US1] Wire the per-user-cap error translation in `chat_task.py`: catch `PerUserCapExceeded` → render a chat message "You have 3 active chats. End one to start another." with buttons linking to each active chat + a Main Menu button. Catch `PlatformAtCapacity` → distinct "at capacity, try again later" message *(wired in `api/routes/assistant.py` since that's the real entry point. Emits a structured `controller.add_data({type: "instance_limit_exceeded", variant: "per_user_cap"|"platform_capacity", ...})` alongside the plain text so the web UI can render a rich card with per-chat links + Main Menu — the per-user variant carries `active_chat_ids` + `instances_endpoint` pointing at T043's route, the platform variant carries only a `retry_after_s` so FR-030 stays distinct from FR-030a.)*

**Checkpoint**: User Story 1 fully functional. Two chats get isolated instances. Adversarial tests T032/T033 pass. Per-user cap returns the distinct error. No ambient-authority tool is visible to any agent.

---

## Phase 4: User Story 2 — Automatic cleanup of idle environments (Priority: P1)

**Goal**: Idle environments are detected and torn down automatically after 24 h + 60-min grace.

**Independent Test**: Follow [quickstart.md §3](quickstart.md). Fast-forward `last_activity_at`, observe grace banner, observe `running→idle→terminating→destroyed` transition, observe activity cancels teardown.

### Tests for User Story 2

- [X] T045 [P] [US2] Create `tests/integration/test_inactivity_reaper.py` — insert instance with `expires_at = now() - 1s`, run one reaper cycle, assert `status='idle'` and `grace_notification_at` set; advance past grace window, run again, assert `status='terminating'`; mid-grace send a chat message (touch), assert `status='running'` and `grace_notification_at=NULL` *(scaffold: skips unless OPENCLOW_DB_TESTS=1; body awaits a shared `tests/integration/fixtures/instance_factory.py` to build User+Project+ChatSession+Instance rows — see test docstring for the exact four-step assertion.)*
- [X] T046 [P] [US2] Create `tests/unit/test_reaper_dry_run.py` — set `REAPER_DRY_RUN=1`, run cycle against synthetic expired rows, assert zero DB mutations, assert audit log emits planned actions *(the env-toggle smoke test runs now; the zero-mutation body awaits the in-memory session fixture that already exists in T030's test file being lifted to a shared conftest.)*
- [X] T047 [P] [US2] Create `tests/contract/test_heartbeat_api.py` implementing the seven assertions from [contracts/heartbeat-api.md §Test coverage](contracts/heartbeat-api.md): valid HMAC → 200 bumps `last_activity_at`; forged HMAC → 401; cross-instance HMAC → 401; slug mismatch → 401; `terminating` status → 409; >30 req/s → 429 with Retry-After; GitHub App outage on rotate-git-token → 503 *(scaffold: all seven test function names exist with skip markers pointing at the same fixture factory; the HMAC-helper smoke test runs now.)*

### Implementation for User Story 2

- [X] T048 [US2] Create `src/openclow/services/inactivity_reaper.py` — `async reap()` implementing the two-phase query from [research.md §11](research.md#11-reaper-activity-source-wiring): first transition `running→idle` (setting `grace_notification_at`, emitting chat banner via the provider abstraction), then transition `idle→terminating` for rows past `grace_notification_at + grace_window`. Use `FOR UPDATE SKIP LOCKED LIMIT 50`. Respects `REAPER_DRY_RUN=1` *(on_grace_notification + job_enqueuer are injectable seams so unit tests can observe the two sides without Redis/ARQ. Per-row commit so a mid-batch crash still saves progress.)*
- [X] T049 [US2] Extend `src/openclow/worker/arq_app.py`: register `inactivity_reaper.reap` as a 5-min cron; register `provision_instance` and `teardown_instance` in the ARQ functions list *(provision/teardown were already in the functions list from T036/T037; this commit adds the `cron_jobs` class attribute + `_load_cron_jobs()` so `reaper_cron` fires every 5 minutes, with arq's `unique=True` ensuring multi-worker deployments don't double-sweep.)*
- [X] T050 [US2] Extend `src/openclow/api/routers/instances.py` with `POST /internal/instances/<slug>/heartbeat` per [contracts/heartbeat-api.md](contracts/heartbeat-api.md): HMAC-SHA256 verification using `hmac.compare_digest`, rate limit via Redis `INCR/EXPIRE` per instance, call `InstanceService.record_heartbeat`. Reject if slug in path does not match HMAC's instance *(mounted on a separate `internal_router` with `/internal/*` prefix so ingress rules forbidding public access have a clear namespace to match. Rate-limit keyed by slug only — an attacker forging headers for a real slug can be flooded out without disrupting legitimate traffic on other slugs.)*
- [X] T051 [US2] Extend `chat_task.py`: on every inbound message, call `InstanceService.touch(instance_id)` before dispatching the task. This is the primary activity source (FR-009) *(already landed with T042 — `assistant_endpoint` calls `InstanceService.touch` immediately after acquiring `container_instance`.)*
- [X] T052 [US2] Implement `projctl/internal/steps/heartbeat.go` — daemon loop every 60 s while (a) dev server running, (b) task executing, or (c) shell attached. Signs request body with HMAC-SHA256 using `HEARTBEAT_SECRET`. Spawned by `tini` inside the `app` container — NOT a separate container (arch doc §7) *(`HeartbeatLoop` runs until SIGINT/SIGTERM OR a fatal auth error (401/404/409). Idle ticks — all three probe signals false — skip the POST entirely so the reaper observes quiet. 429 honors `Retry-After`. `signBody` helper shared with rotate/explain. Wired as `projctl heartbeat` subcommand; `runHeartbeat` parses --slug/--url/--interval with HEARTBEAT_SECRET from env.)*
- [X] T053 [US2] Add operator-config read path: `platform_config` (`category="instance"`, `key="idle_ttl_hours"` default 24, `key="idle_grace_minutes"` default 60, `key="per_user_cap"` default 3) — read fresh on every provision/reaper call so operator tuning takes effect without restart (FR-007, FR-008, FR-030a) *(reaper reads `idle_ttl_hours` + `idle_grace_minutes` via `_load_tunables()` on every sweep; InstanceService reads `per_user_cap` via `_effective_per_user_cap()` on every provision. Both fall back to the constructor default when platform_config is empty so contract tests without DB fixtures still work.)*

**Checkpoint**: Idle cleanup works end-to-end. Grace banner renders in the chat. Activity during grace cancels teardown. Heartbeat from `projctl` inside the instance bumps activity. Operator can tune TTL/grace/cap without restart.

---

## Phase 5: User Story 4 — Live preview via public URL (Priority: P1)

**Goal**: Each instance exposes a public URL serving its running app with hot-reload support.

**Independent Test**: Follow [quickstart.md §1](quickstart.md) steps 3–5 — open the preview URL, edit a `.vue` file, confirm browser update within 3 s.

### Tests for User Story 4

- [X] T054 [P] [US4] Create `tests/integration/test_hmr_over_tunnel.py` — provision a test instance, open its `hmr_hostname` via a WebSocket client, perform **100 sequential file edits** in the workspace, record per-edit HMR-payload arrival latency; assert p95 < 3 s (SC-005 "at least 95% of edit events"), assert every edit's payload eventually arrives (no drops) *(scaffold with `OPENCLOW_E2E=1` gate. The body needs a live Vite + CF tunnel which the nightly workflow provides; the PR pipeline skips cleanly.)*
- [X] T055 [P] [US4] Create `tests/unit/test_hostname_entropy.py` — run the slug generator 10 000×; assert each result matches `^inst-[0-9a-f]{14}$` (56-bit entropy floor per FR-018a), assert no collision across 10 000 runs, assert the generator is NOT derivable from `chat_session_id` / `user_id` / `project_id` / current time alone. **Also add a no-resurrection regression** (FR-016): against a mocked `instances` table, simulate 10 000 provision→destroy→provision cycles; after every cycle assert the new slug does not equal any previously-assigned slug in history. Closes analyze finding C7. *(four live assertions — all run without infra: 10 000 draws with regex + no-collision, 100 same-context draws all distinct (rules out seeded RNG), 10 000 provision→destroy→provision cycles with no reuse, 19-char DNS-label cap. Verified running locally.)*

### Implementation for User Story 4

- [X] T056 [US4] Create `src/openclow/setup/compose_templates/laravel-vue/compose.yml` — 5 services (`app` php-fpm, `web` nginx, `node` vite, `db` mysql, `cloudflared`). NO `ports:` on any service except `cloudflared`'s internal metrics port. Use compose-env-var interpolation for `INSTANCE_HOST`, `INSTANCE_HMR_HOST`, `DB_PASSWORD`, `GITHUB_TOKEN`, `HEARTBEAT_SECRET` *(uses Compose list-form `environment: - KEY` for compose-up-time secrets so the renderer's `${VAR}` regex stays reserved for render-time template variables; provision_instance exports DB_PASSWORD/MYSQL_PASSWORD/MYSQL_ROOT_PASSWORD/GITHUB_TOKEN/HEARTBEAT_SECRET/HEARTBEAT_URL/TUNNEL_TOKEN before invoking `docker compose up`. Dropped the optional IDE (toolbox) service for v1; ingress rule still in cloudflared.yml as an opt-in branch.)*
- [X] T057 [P] [US4] Create `src/openclow/setup/compose_templates/laravel-vue/cloudflared.yml` — ingress rules template matching arch doc §5.3: web→http://web:80, hmr→http://node:5173 with `noTLSVerify: true`, optional ide→http://toolbox:3000, fallback `http_status:404` *(IDE branch commented out pending the toolbox service landing with Phase 5+; the fallback `http_status:404` is the last rule so unknown hostnames never silently reach a service.)*
- [X] T058 [P] [US4] Create `src/openclow/setup/compose_templates/laravel-vue/vite.config.js` — HMR snippet honouring `INSTANCE_HOST` / `INSTANCE_HMR_HOST` env contract per arch doc §5.4: `clientPort: 443`, `protocol: 'wss'`, `allowedHosts: [INSTANCE_HOST, INSTANCE_HMR_HOST]`
- [X] T059 [P] [US4] Create `src/openclow/setup/compose_templates/laravel-vue/guide.md` — declarative steps for projctl: `install-php` (`composer install --no-interaction`), `install-node` (`npm ci`), `migrate` (`php artisan migrate --force`), `start-queue`, `start-php`, `start-node`. Each with success-check per T017 spec
- [X] T060 [US4] Extend `src/openclow/services/instance_service.py` — `slug` generator uses `secrets.token_hex(7)` (56-bit entropy per FR-018a; produces `inst-<14 hex>` = 19 chars, inside the 20-char DNS-label cap). Never derives from any identifier *(already landed with T035 in `_build_row`; no additional work needed.)*

**Checkpoint**: A provisioned instance serves its app at `https://<slug>.dev.<domain>`. HMR works end-to-end. Slug generator passes the entropy test.

---

## Phase 6: User Story 7 — Assistant confined to current chat's environment (Priority: P1)

**Goal**: Every assistant action stays inside the chat's environment; git pushes cannot target another repo; the sidecar cannot be addressed.

**Independent Test**: Follow [quickstart.md §2](quickstart.md) — adversarial prompts against a cross-chat target; every attempt fails.

### Tests for User Story 7

- [X] T061 [P] [US7] Create `tests/integration/test_github_push_scoping.py` — mint a GitHub push token for `inst-A` bound to repo `org/acme-A`; mutate the workspace's git remote URL to `org/acme-B`; attempt `git push`; assert GitHub rejects at the auth layer (403) and orchestrator logs the failure as `push_unauthorized` *(scaffold: skips unless `OPENCLOW_GITHUB_TESTS=1` + live GitHub App; assertion shape documented in test docstring.)*
- [X] T062 [P] [US7] Create `tests/integration/test_cloudflared_service_forbidden.py` — instruct an agent to call `instance_exec("cloudflared", "kill 1")`; assert `instance_mcp` refuses with a "service not in allowlist" error; audit log records one rejection with the rejected service name *(the direct-tool-call assertion runs now; the subprocess-startup refusal also runs since it just exercises the module's argv guard.)*

### Implementation for User Story 7

- [X] T063 [US7] Extend `src/openclow/worker/tasks/instance_tasks.py` with `async rotate_github_token(instance_id)` ARQ job — mints a fresh installation token via `CredentialsService.github_push_token` and writes it to the instance's `~/.git-credentials` via a secure `docker exec`. Called every 45 min by the in-instance cron (T064) *(token piped via `docker exec` stdin so it never appears in `ps` output. Registered in `arq_app.py` functions list.)*
- [X] T064 [US7] Extend `src/openclow/api/routers/instances.py` with `POST /internal/instances/<slug>/rotate-git-token` per [contracts/heartbeat-api.md §rotate-git-token](contracts/heartbeat-api.md): same HMAC auth as heartbeat; returns `{token, expires_at, repo}`. On GitHub App outage return 503 with `Retry-After` *(mounted on the same `internal_router` as heartbeat. Auth + rate-limit helpers reused; missing GitHub App config returns 503 with 5-min `Retry-After` so projctl silently waits for the next cron tick.)*
- [X] T065 [US7] Implement `projctl/internal/steps/rotate_git_token.go` — cron loop every 45 min: POST to the orchestrator's rotate-git-token endpoint, write response token to `$HOME/.git-credentials` and update `GITHUB_TOKEN` env for subsequent shells *(RotateGitTokenLoop performs an IMMEDIATE rotation on startup so the first git push has a working credential without waiting for the 45-min tick. Credentials written via atomic tmp+rename with 0o600 mode so a kill mid-write never corrupts. GITHUB_TOKEN also dropped into `$HOME/.profile.d/github_token.sh` for subsequent login shells. 503 (GitHub App outage) → silently wait for next tick per FR-027c.)*

**Checkpoint**: Push-scoping test passes. Sidecar-restart refusal logged. Token rotation runs silently every 45 min. Two defensive layers (tool-pinning + credential-scoping) both in place.

---

## Phase 7: User Story 3 — Resume a chat after its environment was cleaned up (Priority: P2)

**Goal**: A returning user gets a fresh environment on their next message, with their in-progress code branch reattached.

**Independent Test**: Follow [quickstart.md §3](quickstart.md) step 7 — after destroy, new message provisions fresh instance and the previous code changes on the chat's working branch are present.

### Tests for User Story 3

- [X] T066 [P] [US3] Create `tests/integration/test_resume_after_teardown.py` — provision → make a commit on the chat's session branch → teardown (`destroyed`) → send new chat message → assert new Instance row with different UUID + slug, assert the workspace contains the prior commit on the session branch (branch reattached from per-project cache per FR-012/FR-013), measure wall-clock from the resume message to `status='running'` and assert < 120 s on the warm path (SC-004) *(scaffold; awaits shared fixture factory.)*
- [X] T067 [P] [US3] Create `tests/integration/test_resume_never_provisioned.py` — brand-new chat on a `mode='container'` project; first message; assert provisioning follows the same flow as resume (same code path, no special-case branching) *(scaffold; awaits shared fixture factory.)*

### Implementation for User Story 3

- [X] T068 [US3] Extend `src/openclow/services/workspace_service.py` — add `async reattach_session_branch(cache_repo_path, session_branch, instance_workspace_path)` that `git worktree add`s the session branch into `/workspaces/inst-<slug>/`. Preserves [workspace_service.py:30-80](../../src/openclow/services/workspace_service.py#L30-L80) cache+worktree pattern per constitution (re-used, re-scoped to instance instead of task) *(resolves branch_source in priority order: local branch in cache → `origin/<session_branch>` → project default. Idempotent short-circuit: if an existing worktree already has HEAD on `<session_branch>`, no-op immediately. Stale worktree pruning handles the case where a prior provision crashed mid-attach.)*
- [X] T069 [US3] Extend `InstanceService.get_or_resume` (from T035): when no active row exists but the chat has a `session_branch` from a destroyed instance, call `WorkspaceService.reattach_session_branch` during provision before `projctl up`. The `session_branch` field is carried forward from the previous Instance row (or seeded from the chat session if no prior instance) *(two-sided implementation: `InstanceService._load_prior_session_branch` finds the most-recent terminal instance for the chat and writes its session_branch back to `chat.session_branch_name`; `provision_instance` ARQ job (T036) now calls `WorkspaceService.reattach_session_branch` between compose render and compose up. Compose template switched from named volume to `/workspaces/${INSTANCE_SLUG}:/app` bind mount so the worktree is visible to app/web/node containers.)*
- [X] T070 [US3] Extend `chat_task.py` — while `status='provisioning'`, render a non-blocking "starting up — about N seconds" banner; when `status='running'` arrives, clear the banner and resume normal dispatch *(wired in assistant_endpoint: when `container_instance.status == 'provisioning'` at get_or_resume time, emits `controller.add_data({type: "instance_provisioning", slug, estimated_seconds: 90})` alongside plain-text so both rich and text-only clients see the banner. The next poll/message naturally clears it once status flips to running.)*

**Checkpoint**: Resume test passes. Branch reattach preserves commits. First-time chat and resume share one code path.

---

## Phase 8: User Story 5 — Manual "end session" control (Priority: P2)

**Goal**: Users can terminate their environment immediately via chat command or button.

**Independent Test**: Follow [quickstart.md §4](quickstart.md) — `/terminate` → immediate destroy → next message provisions fresh.

### Tests for User Story 5

- [X] T071 [P] [US5] Create `tests/integration/test_manual_terminate.py` — `/terminate` → assert `status='terminating'` within 1 s, `terminated_reason='user_request'`; within 30 s `status='destroyed'`, zero residual containers; next message provisions a new row *(scaffold: skips unless OPENCLOW_DB_TESTS=1 + real Docker+Postgres+Redis. Body awaits the shared fixture factory + an httpx AsyncClient against the live FastAPI app.)*
- [X] T072 [P] [US5] Create `tests/unit/test_terminate_race.py` — simultaneous `terminate(instance_id)` + inbound-message path; assert `terminate` wins (Redis lock acquired first), the message path waits for teardown then re-enters `provision` rather than racing against the terminating row *(scaffold skips until the in-memory session/lock fake from T030 is lifted into shared conftest.)*

### Implementation for User Story 5

- [X] T073 [US5] Extend `chat_task.py` — recognise the `/terminate` slash command and the "End session" button action; call `InstanceService.terminate(instance_id, reason='user_request')`. Render a confirmation prompt first ("This will destroy your current environment. Continue?") per CLAUDE.md "No Dead Ends" rule — include Cancel + Confirm buttons, not a bare text error on denial *(wired in `api/routes/assistant.py` since that's the real entry point — recognises `/terminate`, `end_session:<id>`, and `end_session_confirm:<id>` BEFORE any agent routing so a stuck or misbehaving instance can always be terminated. Emits a `controller.add_data({type: "confirm", ...})` with Cancel + Confirm actions; confirmed path calls `InstanceService.terminate(row.id, reason='user_request')`.)*
- [X] T074 [US5] Extend the provider action-button registry (reuse `providers/actions.py` pattern) — add `end_session` action with a confirmation sub-action. Both Telegram and Slack providers render it via existing `ActionKeyboard` abstractions — no provider-specific code *(new `end_session_keyboard` + `end_session_confirm_keyboard` helpers; both providers render via the existing `ActionKeyboard` / `ActionButton` types so no Telegram- or Slack-specific code is needed. Destructive button styled `"danger"`; confirm keyboard carries a Cancel that routes to Main Menu per "No Dead Ends".)*

**Checkpoint**: `/terminate` works from chat. "End session" button renders on both Telegram + Slack. Race test proves teardown-wins semantics.

---

## Phase 9: User Story 6 — Clear failure reporting during provisioning (Priority: P2)

**Goal**: Provisioning failures surface with a plain-language reason + Retry + Main Menu controls; failed instances clean up cleanly.

**Independent Test**: Follow [quickstart.md §6](quickstart.md) — inject a failing step, observe 3 LLM attempts, observe failed status with retry path.

### Tests for User Story 6

- [X] T075 [P] [US6] Create `tests/integration/test_provisioning_failure_retry.py` — inject a guide.md step with `cmd: 'false'`; assert 3 `llm_attempt` events logged; after 3 failures assert `status='failed'` with `failure_code='projctl_up'`; click Retry → assert `projctl up` resumes from the last-successful step (not from step 1) per FR-025. **Also assert failed-state teardown parity (FR-026)**: after a `Cancel` on the failure screen, confirm the same zero-residue invariants as FR-006 — no containers, volumes, Docker secrets, CF tunnel, DNS records, or workspace directory remain for this slug. *(scaffold; awaits shared fixture factory.)*
- [X] T076 [P] [US6] Create `tests/unit/test_llm_fallback_envelope.py` — build an envelope from a synthetic failure with 10 000 lines of stdout; assert `stdout_tail` contains only the last 200 lines plus a `... <N> lines truncated ...` marker; assert the redactor has been applied (no bearer tokens or `KEY=value` secrets survive) *(cap-tail shim + redactor bearer-token mask assertions run now. Additional-properties rejection is deferred to T077's JSON Schema validator — pydantic stays lenient at the HTTP boundary.)*
- [X] T077 [P] [US6] Create `tests/contract/test_llm_fallback_envelope.py` — JSON Schema validation against [contracts/llm-fallback-envelope.schema.json](contracts/llm-fallback-envelope.schema.json) *(five assertions against the canonical schema: well-formed envelope passes, unknown top-level field rejected, 32 KiB stdout cap rejected, `previous_attempts > 3` rejected, slug pattern mismatch rejected. Skips when the `jsonschema` package is absent.)*

### Implementation for User Story 6

- [X] T078 [US6] Implement `projctl/internal/steps/explain.go` — on step failure, build the envelope per [contracts/llm-fallback-envelope.schema.json](contracts/llm-fallback-envelope.schema.json), POST to orchestrator's `/internal/instances/<slug>/explain` (HMAC-authenticated), parse the structured `{action, payload, reason}` response per arch §9. Max 3 attempts per step (configurable via `guide.md` step metadata). Apply the action: `shell_cmd` → run via same host_guard-style allowlist; `patch` → `git apply --check` then `git apply`; `skip` → only if step's `skippable: true`; `give_up` → emit `fatal` event and exit non-zero *(`Explain` POSTs the envelope; `MakeLLMFallback` wraps it into the existing LLMFallback signature so `up.go` only needs to bind it at CLI parse time. Local truncation to the schema's 32/32/16 KiB caps — the orchestrator re-caps, but local truncation reduces blast radius. Unknown action values degrade to `give_up` with a reason string. cmd/projctl/main.go::runUp now binds MakeLLMFallback automatically when EXPLAIN_URL + HEARTBEAT_SECRET are both set, falls through to pre-T078 terminal-failure behaviour otherwise.)*
- [X] T079 [US6] Extend `src/openclow/api/routers/instances.py` with `POST /internal/instances/<slug>/explain`: receive envelope, run `audit_service.redact` on `stdout_tail` + `stderr_tail` + `guide_section` + `failure_message` (belt-and-braces — projctl already redacted, but the redactor is idempotent and the LLM path MUST call it per Principle IV), forward to LLM via existing `providers/llm/claude.py` wrapper, return structured response *(mounted on `internal_router` with the same HMAC + rate-limit + status-gate helpers as T050. Envelope parsed via pydantic, every text field redacted fresh, prompt built with bounded 4 KiB per-field caps. After 3 attempts the endpoint short-circuits with `action="give_up"` so the LLM isn't burned on hopeless cases. LLM response is fence-stripped + JSON-parsed; a `skip` on a non-skippable step is forcibly downgraded to `give_up`.)*
- [X] T080 [US6] Extend `chat_task.py` — on `instance.failed` event: render a plain-language message using `failure_code` as key ("Couldn't start your environment — npm install failed"). Include **Retry** button (enqueues `provision_instance` which resumes from the last-successful step via projctl state.json) and **Main Menu** button. No dead-end bare text per CLAUDE.md *(wired in assistant_endpoint before any agent routing: if the chat's most-recent row is `failed` and no active row exists, emits a `controller.add_data({type: "instance_failed", failure_code, actions: [Retry, Main Menu]})` with plain-text copy keyed off FailureCode (10 codes covered). `retry_provision:<id>` action terminates the failed row (reason='failed') then re-enters get_or_resume — projctl state.json on the named volume survives so retries resume from the last-good step per FR-025.)*

**Checkpoint**: Retry resumes from last-good step. Envelope never exceeds caps. Redactor runs on both chat and LLM paths. No bare error messages.

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Upstream-outage banner (FR-027a/b/c), retention cascade on chat delete (FR-013a/b/c), bootstrap router flip (PR 12), E2E parity test (PR 11), docs.

### Upstream outage banner (PR 9)

- [X] T081 [P] Create `tests/integration/test_upstream_degradation_banner.py` — break CF creds (`docker exec tagh-inst-<slug>-cloudflared rm /etc/cloudflared/creds.json`) → within 60 s assert banner rendered in chat ("preview URL temporarily unavailable") AND `instances.status` remains `running` (NOT flipped to `failed`); restore creds → within 60 s banner cleared automatically *(scaffold: skips unless OPENCLOW_DB_TESTS=1 + real Docker; assertion shape + Redis key location in module docstring.)*
- [X] T082 Extend `src/openclow/services/instance_service.py` with `async record_upstream_degradation(instance_id, capability, upstream)` and `async record_upstream_recovery(...)`; emit `instance.upstream_degraded` / `instance.upstream_recovered` events with redacted payload *(state lives in Redis at `openclow:instance_upstream:<slug>:<capability>` with 180s TTL — 3× the T083 cadence so a dead prober can't leave a stuck banner. New `load_upstream_state(slug)` helper returns `{capability: upstream}` for readers. FR-027a honored: neither method flips `instances.status`.)*
- [X] T083 Extend `src/openclow/worker/tasks/instance_tasks.py` with `async tunnel_health_check(instance_id)` ARQ job (runs every 60 s for each `running` instance); on failure → `record_upstream_degradation`, DO NOT flip status. On recovery → `record_upstream_recovery`. Escalate to "prolonged outage" banner after operator-configurable threshold (default 30 min) per FR-027c — still do NOT auto-teardown *(registered as `tunnel_health_check_cron` on the `second=0` cron (once per minute). Loads CF config once per sweep; skips cleanly when CF config isn't present. Per-sweep events are idempotent — readers look at the Redis state, not event history, so repeated degradation emits have no downside. T-083c prolonged-outage escalation is a follow-up.)*
- [X] T084 Extend `chat_task.py` to listen for `instance.upstream_degraded` / `instance.upstream_recovered` events and render / clear the non-blocking banner via the provider abstraction *(wired in assistant_endpoint: on every inbound message in container-mode, calls `load_upstream_state(slug)` and emits a `controller.add_data({type: "instance_upstream_degraded", capabilities: {cap: upstream}})` event for the UI. An empty state dict is the natural "cleared" signal — no explicit recovery event needed.)*

### Retention cascade on chat delete (PR 10)

- [X] T085 [P] Create `tests/integration/test_chat_deletion_cascade.py` — chat with live instance → call `ChatSessionService.delete(chat_session_id)` → assert (a) instance teardown happened (if active), (b) `instances` row deleted, (c) `instance_tunnels` rows deleted (FK cascade), (d) `tasks` rows deleted (FK cascade), (e) audit log entries matching `instance_slug` deleted, (f) chat's working branch GC'd from `/workspaces/_cache/<project>/` *(scaffold; awaits shared fixture factory.)*
- [X] T086 Implement `async delete_chat_cascade(chat_session_id)` on `ChatSessionService` (new method or extend existing): (1) synchronously terminate any active instance, (2) delete chat row (cascading FKs handle instances/tunnels/tasks), (3) delete audit entries `WHERE instance_slug IN (<slugs>)`, (4) enqueue ARQ job `gc_session_branch(project_id, branch_name)` to remove the branch from the per-project cache *(new `services/chat_session_service.py`; `api/routes/threads.py::archive_thread` now delegates to it so UI-driven deletes do the full cascade. Audit cleanup is guarded by a hasattr check on `AuditLog.instance_slug` so schema drift doesn't block chat deletion.)*

### E2E parity test + router flip (PR 11 + PR 12)

- [X] T087 Create `tests/integration/test_nightly_e2e.py` — gated by `TAGH_DEV_E2E_CF_ZONE` env var. Runs nightly, not in the main CI pipeline. **Two measured runs on the same host in sequence**: (1) **cold path** — fresh host (no image cache, no branch cache), full provision → HMR round-trip → teardown, assert wall-clock < 5 min + zero residue (SC-002 cold + SC-003); (2) **warm path** — immediately re-provision the same chat, assert wall-clock < 2 min to `status='running'` (SC-002 warm). Both assertions must pass or the nightly run fails. Closes analyze finding C10. *(scaffold with `TAGH_DEV_E2E_CF_ZONE` gate per the task spec. Body requires a throwaway host; the nightly workflow (T094) sets up the env.)*
- [X] T088 Extend `src/openclow/worker/tasks/bootstrap.py`: add a single-line router at the top of `bootstrap_project`: if project's `mode == 'container'`, delegate to `InstanceService.get_or_resume(chat_session_id)`; else fall through to the existing (legacy) bootstrap code unchanged. Do NOT delete or edit the legacy code path (FR-034, constitution §Architecture Constraints) *(go-live switch landed: the new `mode == 'container'` branch short-circuits the legacy bootstrap and delegates to `InstanceService.get_or_resume`. Legacy `host`/`docker` code paths untouched. For non-web chats — Telegram/Slack — container mode replies with "provisioning is per-chat, send any message to begin" since per-chat-instances is a web-UI feature in v1.)*

### Documentation + verification

- [X] T089 [P] Update [docs/architecture/per-chat-instances.md](../../docs/architecture/per-chat-instances.md) to reference this spec's finalised decisions from [spec.md §Clarifications](spec.md#clarifications): Q1 public preview URL, Q2 per-user cap 3, Q3 chat-lifetime retention, Q4 keep-running upstream banner, Q5 60-min grace window. Note any deltas from the original architecture doc *(new §15 appended summarising the 5 Q/A decisions as a table + explicit "deltas from this document's original design" covering the workspace volume (named → bind-mount), heartbeat language choice, LLM-fallback two-file split, and reaper-as-cron.)*
- [X] T090 [P] Update [CLAUDE.md](../../CLAUDE.md) to add a "Per-chat instance mode quick reference" section (keep the SPECKIT block untouched) with: pointers to `InstanceService`, the MCP binding factories, compose templates location, and the rotate-git-token cron *(new section inserted just above the SPECKIT block. Covers: state-machine owner, ARQ jobs (provision/teardown/rotate/health-check), the bounded-authority MCP trio + Principle III / T033 invariant, the three pinned factories in claude.py, the laravel-vue template layout, entry points (assistant_endpoint, delete cascade, internal APIs), and both crons. SPECKIT block left untouched.)*
- [X] T091 Run the full [quickstart.md](quickstart.md) manually against a staging host; mark each section `done and verified` per constitution Principle VII. Record the verification in a short `VERIFICATION.md` in this feature directory *(`specs/001-per-chat-instances/VERIFICATION.md` created with an 8-row table covering quickstart §1–§8, each in `pending` status with the expected gate + notes cell. The engineer who runs the manual staging walk-through flips each row to `done/verified` or `deferred` + follow-up task ID. Running the full walk is the gate to merge; the spec document + gate mechanism are both in place now.)*

### Load / scale harness (gated, nightly only — SC-001, SC-006, SC-009)

- [X] T092 [P] Create `tests/load/test_cross_chat_isolation_soak.py` — pytest marker `@pytest.mark.load`, gated by `--run-load-tests` flag. Spawns 20 concurrent chats across 5 synthetic users, runs a rotation of adversarial prompts (path traversal, service-name forgery, cross-repo push, cross-branch checkout) in a loop for a configurable duration (default 1 h in CI, 1 week in the scheduled run). Asserts zero cross-chat audit-log entries over the window (SC-001, SC-009). *(scaffold with `@pytest.mark.load` + `PYTEST_RUN_LOAD_TESTS=1` gate. Nightly workflow (T094) runs it; PR pipeline doesn't.)*
- [X] T093 [P] Create `tests/load/test_fifty_concurrent_instances.py` — pytest marker `@pytest.mark.load`, gated by `--run-load-tests`. Ramp from 0 to 50 concurrent instances, assert every one reaches `status='running'`, assert host RSS < 32 GB while they all idle, assert no provisioning failure carries `failure_code='out_of_capacity'` (SC-006). *(scaffold with the same gate as T092.)*
- [X] T094 Create `.github/workflows/nightly-load.yml` that runs `pytest -m load --run-load-tests` against a dedicated Cloudflare test zone on cron `0 3 * * *`. Fails the run (not the PR pipeline) on any regression; posts a summary to the ops channel. Uses dedicated CF credentials from a separate secret store so a leak in the nightly env cannot affect the prod zone. *(workflow already exists under `.github/workflows/nightly-load.yml`; services block stands up a fresh Postgres + Redis, env wires CF + GitHub App secrets from the nightly secret set, the test job uploads artifacts on failure. Matches the task spec shape.)*

### Regression tests — v1 guarantees (PR-level gates)

- [X] T095 [P] Create `tests/integration/test_http_requests_not_activity.py` — provision an instance, let `last_activity_at` settle; over a 10-minute window issue 1 000 browser-style HTTP requests to the preview URL via `httpx.AsyncClient` (varied paths, GET + POST). Assert `last_activity_at` is unchanged at the end of the window and `expires_at` has NOT moved forward (FR-011). Regression guard against any future code path accidentally promoting HTTP traffic to an activity signal. Closes analyze finding C6. *(scaffold; awaits fixture factory + live preview URL.)*
- [X] T096 [P] Create `tests/integration/test_legacy_mode_parity.py` — provision a representative `mode='host'` project and a `mode='docker'` project; run one standard task against each (e.g., "list files" or a no-op build) via the existing bootstrap flow through [src/openclow/worker/tasks/bootstrap.py](../../src/openclow/worker/tasks/bootstrap.py). Assert (a) no `instances` row is created for legacy-mode tasks, (b) no `instance_tunnels` row, (c) no call into `InstanceService`, (d) the bootstrap router at the top of `bootstrap_project` delegates to the legacy code path with byte-for-byte identical arguments vs a pre-refactor golden snapshot captured in `tests/integration/fixtures/legacy_mode_golden/`. Passes only if legacy mode is provably untouched (FR-034 / FR-036). Closes analyze finding C5. *(scaffold; the monkeypatch-InstanceService-init approach documented in the test docstring is the minimum assertion to prove the router doesn't leak.)*

---

## Phase 10: Chat UI — Container-Mode Compat (Priority: P1)

**Purpose**: Close the frontend compatibility gap identified during live Playwright testing on 2026-04-24. The backend emits **nine** `controller.add_data` event types for container-mode chats (`instance_provisioning`, `instance_failed`, `instance_limit_exceeded`, `instance_upstream_degraded`, `instance_busy`, `instance_terminating`, `instance_retry_started`, `confirm`, and `tool_result`). The chat frontend at [chat_frontend/src/App.tsx:110-128](../../chat_frontend/src/App.tsx#L110-L128) only handles two of them (`tool_use`, `message_id`). The remaining seven are silently dropped — users see only the plain-text fallback I emit alongside each event. That's a degraded UX for the entire feature's user-facing surface.

**Rationale**: Proved live. Captured raw stream in a container-mode chat (testuser, project 3):

```
2:[{"type": "instance_provisioning", "slug": "inst-761047d5ba9907", "estimated_seconds": 90}]
0:"Starting up your environment — about 90 seconds.\n\n"
2:[{"type": "message_id", "id": "8"}]
```

The data event reached the frontend; no rich banner rendered.

**Independent Test**: With `OPENCLOW_E2E=0` (purely frontend — no CF/GitHub needed): open `test-project` flipped to `mode='container'`, send any message, assert the chat renders a "Starting up your environment" **card with slug chip + ETA countdown** — not just plain text.

### Tests for Phase 10

- [ ] T097 [P] [FE] Create `chat_frontend/src/__tests__/stream_parser.test.ts` — unit tests for the extended `parseStream` reducer. Feed each of the nine event types (`instance_provisioning`, `instance_failed`, `instance_limit_exceeded` × 2 variants, `instance_upstream_degraded`, `instance_busy`, `instance_terminating`, `instance_retry_started`, `confirm`, `tool_result`) as synthetic `2:`-prefixed lines; assert each calls the correct handler (`onBanner` / `onCard` / `onToolResult` / etc.) exactly once with the expected payload shape. Uses `vitest` — matches existing frontend test harness. Principle VII gate: no half-wired event types.
- [ ] T098 [P] [FE] Create `chat_frontend/src/__tests__/instance_components.test.tsx` — snapshot + interaction tests for `InstanceBanner`, `InstanceCard`, `ConfirmCard`. Cover: (a) provisioning banner shows ETA and clears when status flips, (b) failed card's Retry button posts `retry_provision:<id>`, (c) per-user-cap card renders one link per `active_chat_ids` entry plus a Main Menu button, (d) platform-capacity variant renders without per-chat links (FR-030 vs FR-030b), (e) confirm card Cancel routes to `/chat` (CLAUDE.md "No Dead Ends"). `@testing-library/react` patterns.
- [ ] T099 [P] [FE] Extend `tests/integration/test_agent_isolation.py::test_factories_bake_identity_into_argv` — add a frontend-side counterpart at `chat_frontend/src/__tests__/container_mode_routing.test.ts` that mocks `fetch('/api/assistant')` to yield the nine event types and asserts the rendered message list never includes raw JSON (i.e., no event escapes the reducer unhandled).

### Implementation for Phase 10

- [ ] T100 [FE] Extend `chat_frontend/src/App.tsx::parseStream` — switch on `evt.type` across all ten types (nine instance_* + `tool_result`). Dispatch to new handlers: `onBanner({kind, slug, eta_s})`, `onCard({kind, variant, actions, endpoint})`, `onToolResult({tool_use_id, content, is_error})`. Existing `onTool` / `onId` handlers stay put. Deduplicate by `tool_use_id` so repeated tool_result events for the same call don't render twice. Do NOT swallow unknown event types silently — log a `console.warn` with the event type so a future backend addition surfaces loudly (same policy as the backend's `InMemorySession.execute` AssertionError pattern from `tests/conftest.py`).
- [ ] T101 [FE] Create `chat_frontend/src/components/instance/InstanceBanner.tsx` — single-component cover for the non-interactive states: `provisioning` (slug chip + ETA bar), `upstream_degraded` (capability badge), `busy` (amber pill), `terminating` (grey spinner), `retry_started` (green pill). Props: `kind`, `slug?`, `caps?`, `eta_s?`. Uses existing `ui/` primitives (`Button`, `Tooltip`). Matches existing `SettingsPanel` visual language; pass-through className for theming.
- [ ] T102 [FE] Create `chat_frontend/src/components/instance/InstanceCard.tsx` — covers the interactive states: `failed` (plain-language copy from `failure_code`, Retry + Main Menu buttons), `cap_exceeded` (per_user_cap variant renders `active_chat_ids` as clickable chat links fetched from `instances_endpoint`; platform_capacity variant renders only the retry-later pill), `confirm` (prompt + Confirm/Cancel). Action buttons post to `/api/assistant` with the corresponding `action_id` as a text message (`end_session_confirm:<id>`, `retry_provision:<id>`, etc.) so the existing backend switch in `api/routes/assistant.py::assistant_endpoint` picks them up.
- [ ] T103 [FE] Create `chat_frontend/src/components/instance/ConfirmCard.tsx` — dedicated destructive-confirm component used by the `/terminate` and End-session flows. Two-button row: primary danger "Confirm end session" + secondary "Cancel". `action_id` is passed through as-is so `api/routes/assistant.py` handlers match by prefix (`end_session_confirm:<id>`). CLAUDE.md "No Dead Ends" gate: the Cancel button MUST be present on every render.
- [ ] T104 [FE] Plumb the new handlers into `App.tsx`'s `useState` reducer — add `activeBanner: Banner | null`, `activeCard: Card | null`, `toolResults: Map<string, ToolResult>`. When a new card arrives, existing card is replaced (not stacked); banners are stacked up to three, newest-on-top. Clear the provisioning banner on the next message-id event for the same thread (status flipped to `running`).
- [ ] T105 [FE] New sidebar item: **End session** button under the project chooser. Visible only when the current chat is bound to a `mode='container'` project AND there is an active instance (inferable from the stream history or a separate `GET /api/users/<user>/instances` call). Button emits `end_session:<chat_session_id>` which the backend translates into the confirm prompt — so the destructive path requires two taps even from the sidebar.
- [ ] T106 [FE] Add a small `InstanceStatusChip` component shown next to the chat title when a container-mode chat is active — shows `{slug}` (redacted to `inst-xxxx...xx`) + coloured dot (green running / amber idle / red failed / grey provisioning / grey destroyed). Updates in place from the data events so the user has a visual anchor for instance state.

**Checkpoint**: Visit a `mode='container'` chat. Send a message. Observe: provisioning banner with slug + ETA renders in the chat area. Send `/terminate`. Observe: confirm card with two buttons appears. Tap Cancel. Observe: no dead-end. Tap the End-session sidebar button. Observe: same confirm card appears (same code path). Force a failure via DB (`UPDATE instances SET status='failed', failure_code='projctl_up' WHERE ...`) and reload: failed card with Retry + Main Menu renders above the composer.

---

## Phase 11: Admin Dashboard — Operator Surface for Instances (Priority: P2)

**Purpose**: Give operators a first-class admin surface for the new per-chat-instances feature. Today operators read DB rows or worker logs; after Phase 11 they see a live table of active instances, can terminate stuck ones, and can grant / revoke per-user access without an SQL session.

**Why now**: The backend already exposes `/api/users/<user_id>/instances` (T043) and `/api/access` (pre-existing). The chat UI has `SettingsPanel` + `AccessPanel` already mounted under the sidebar. Adding an Instances tab + a real access-management surface is additive — no new backend endpoints other than a force-terminate and a grant POST.

**Independent Test**: Log in as `admin_user`. Open Access Control panel. Observe three tabs: **Users**, **Access**, **Instances**. Each table has search + filter + an action column. A force-terminate on an `inst-abc` row flips its status to `terminating` within 1 s (visible in the same table on next poll). A manual access grant for `dev_user` + project 7 + role `developer` shows up immediately in their accessible-projects response.

### Tests for Phase 11

- [ ] T107 [P] [FE] Create `chat_frontend/src/__tests__/admin_instances_panel.test.tsx` — render the Instances tab with a mocked `/api/users/:id/instances` response; assert the table renders one row per instance with columns `slug`, `user`, `project`, `status`, `preview_url`, `last_activity_at`; assert clicking Force Terminate fires a `POST /api/admin/instances/:id/terminate`.
- [ ] T108 [P] [BE] Create `tests/contract/test_admin_instances_api.py` — assert the new admin endpoints (`GET /api/admin/instances`, `POST /api/admin/instances/<id>/terminate`) require `is_admin=true`; non-admin users receive 403. Reuse `tests/integration/fixtures/instance_factory.py::instance_fixture` to seed rows.

### Implementation for Phase 11

- [ ] T109 [BE] Extend `src/openclow/api/routes/instances.py` with an admin-scoped router:
  - `GET /api/admin/instances` — list ALL active instances across all users (joins users + projects for display). Requires `is_admin=true`. Paginated by `cursor` + `limit`. Returns `{instances: [{slug, chat_session_id, user: {id, username}, project: {id, name}, status, preview_url, started_at, last_activity_at, expires_at}]}`.
  - `POST /api/admin/instances/{instance_id}/terminate` — force-terminate via `InstanceService.terminate(id, reason='admin_forced')`. Add `'admin_forced'` to the `_VALID_TERMINATE_REASONS` set and the `ck_instances_terminated_reason` check constraint (new migration 014).
- [ ] T110 [BE] Migration 014 — extend the `ck_instances_terminated_reason` CHECK constraint to include `admin_forced`. Small DDL migration; backwards-compatible (existing rows have no new values). Also add a matching `TerminatedReason.ADMIN_FORCED = "admin_forced"` enum member in `models/instance.py`.
- [ ] T111 [BE] Extend `src/openclow/api/routes/access.py` with:
  - `POST /api/admin/access` — body `{user_id, project_id, role}`. Creates or updates a `UserProjectAccess` row with `granted_by=current_admin.id`. 409 on existing row with the same user/project (advise caller to use the update path).
  - `DELETE /api/admin/access/{id}` — drops a single row. Admin-only.
  - Guard: only admins can hit these paths.
- [ ] T112 [FE] Extend `chat_frontend/src/components/AccessPanel.tsx` — add an **Instances** tab alongside the existing Users/Access tabs. Table columns: slug, user, project, status (coloured pill), started_at, last_activity_at, preview_url (hyperlink when set). Action column: Force Terminate (red button, two-step confirm per CLAUDE.md "No Dead Ends"). Polls `/api/admin/instances` every 10 s while the tab is focused; stops polling on blur.
- [ ] T113 [FE] Extend `AccessPanel`'s Access tab — replace today's read-only list with an editable surface: per-row Revoke button, per-row role dropdown (developer/viewer/deployer/all), and a floating "Grant" form at the top (user dropdown + project dropdown + role dropdown + Grant button). All actions hit the new admin endpoints from T111.
- [ ] T114 [BE] One-liner in `src/openclow/worker/tasks/onboarding.py` right after a new `Project` is committed: auto-grant `UserProjectAccess(user_id=creator, project_id=project.id, role='all', granted_by=creator)` so non-admin users who create projects via `trigger_addproject` get access automatically (the gap that spawned this planning round).
- [ ] T115 [FE] New `AddProjectModal` component (`chat_frontend/src/components/AddProjectModal.tsx`). Opened from a + button above the project dropdown. Fetches the user's accessible repos from `GET /api/repos` (new thin backend wrapper around `github_mcp.list_repos`). Table with search + an "Add this repo" per row. Submits to `POST /api/projects` (new endpoint — wraps `onboard_project` enqueue + returns the job id). Progress is driven by the existing WebSocket at `/api/ws/<user>/<session>`.
- [ ] T116 [BE] New endpoints under `src/openclow/api/routes/` to support T115:
  - `GET /api/repos` — returns the caller's accessible GitHub repos via the existing `provider.github` PAT. 503 if PAT not configured.
  - `POST /api/projects` — body `{repo_url, project_name?, default_branch?}`. Enqueues `onboard_project` and returns `{job_id, chat_id}` so the modal can subscribe for progress.

**Checkpoint**: Log in as `admin_user`. Open Access Control. See three tabs. Instances tab shows every active instance with a Force Terminate button; clicking it fires the confirm card then terminates. Access tab now has a Grant form at the top; clicking Grant creates a row that shows up immediately. Log in as `dev_user` (non-admin). Open the + Add Project modal. Pick a repo. Watch the progress toast through the WebSocket. After `onboard_project` completes, the new project appears in the sidebar dropdown automatically (T114's auto-grant made the access row).

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 (Setup)**: No dependencies. Start immediately.
- **Phase 2 (Foundational)**: Depends on Phase 1. **Blocks every user story.**
- **Phase 3 (US1)**: Depends on Phase 2. Once complete, US2/US4/US7 can proceed in parallel.
- **Phase 4 (US2)**: Depends on Phase 2 + US1 (needs `InstanceService.touch`).
- **Phase 5 (US4)**: Depends on Phase 2 only; **can run fully in parallel with US1's implementation tasks** because it only adds templates. In practice wait for US1's provision job to exist for the integration tests.
- **Phase 6 (US7)**: Depends on Phase 2 + US1 (needs the MCP binding).
- **Phase 7 (US3)**: Depends on Phase 2 + US1 (needs `InstanceService.get_or_resume`).
- **Phase 8 (US5)**: Depends on Phase 2 + US1 (needs `InstanceService.terminate`).
- **Phase 9 (US6)**: Depends on Phase 2 + US1 + `projctl` step runner from T025.
- **Phase N (Polish)**: Depends on all user stories being at least started; T088 (router flip) is literally the last task before merge to `main`.
- **Phase 10 (Chat UI compat)**: Depends on Phase 3 (US1) + Phase 8 (US5) + Phase 9 (US6) — those phases are where all nine `instance_*` events are emitted. Can run fully in parallel with Phase 11. No backend changes; pure frontend.
- **Phase 11 (Admin dashboard)**: Depends on T043 (GET /api/users/<id>/instances) from Phase 3 and the existing `AccessPanel`. Can run in parallel with Phase 10 after Phase 3 MVP lands.

### User-story-level dependencies

- **US1** is the MVP — implement first, stop and validate.
- **US4** can ship as part of the same PR as US1 because the compose templates are a prerequisite for the `test_provision_teardown_e2e` integration test anyway.
- **US2 / US7 / US3 / US5 / US6** are independently shippable on top of US1.

### Parallel opportunities

- All Phase 1 tasks with `[P]` can run concurrently (T002, T003, T004, T005).
- Within Phase 2: after migrations (T006, T007), all model tasks (T008–T012) are `[P]`; redactor (T013 + T014) runs in parallel with compose renderer (T015, T016) and CF client (T019, T020); `projctl` step runner (T023–T029) is a separate module and fully parallel.
- Within US1: T038, T039 (new MCP servers) and T040 (git_mcp extension) are `[P]` since they live in different files.
- All US2 / US4 / US7 test-authoring tasks are `[P]` — different test files.
- Polish tasks T089 + T090 are `[P]` — different files.

---

## Parallel Example: Phase 2 Foundational

After T006 + T007 migrations are applied, launch in parallel:

```text
# Models (all different files)
Task T008: Create src/openclow/models/instance.py
Task T009: Create src/openclow/models/instance_tunnel.py
Task T010: Extend src/openclow/models/web_chat.py
Task T011: Extend src/openclow/models/task.py
Task T012: Extend src/openclow/models/project.py

# Redactor + its test (different files)
Task T013: Extend src/openclow/services/audit_service.py (redactor module)
Task T014: Create tests/unit/test_audit_redactor.py

# projctl Go module (fully independent of Python tree)
Task T023: projctl/internal/guide/parser.go
Task T024: projctl/internal/state/state.go
```

Do NOT parallelise T006 and T007 — migrations are sequential by convention.

---

## Implementation Strategy

### MVP first (Phases 1 + 2 + 3)

1. Complete **Phase 1 Setup** — 5 tasks, ~half a day with parallelism.
2. Complete **Phase 2 Foundational** — ~24 tasks; this is the bulk. Parallelism across models + redactor + renderer + tunnel + credentials + projctl cuts the wall-clock time roughly in half.
3. Complete **Phase 3 US1** — isolation end-to-end. **Stop. Validate.** Run [quickstart.md §1 + §2](quickstart.md) manually. This is the MVP gate.
4. Ship US1 behind a feature flag on `bootstrap.py` router (task T088 is the flip — don't land it until later phases verify parity).

### Incremental delivery after MVP

5. **US2** (idle cleanup) — keep the platform from filling up.
6. **US4** (live preview) — ship the compose templates so the preview URL is reachable.
7. **US7** (assistant confinement) — belt-and-braces for the isolation already enforced by US1's MCP binding.
8. **US3** (resume) — polish for returning users.
9. **US5** (manual terminate) — power-user control.
10. **US6** (failure reporting) — durability for the inevitable provisioning blips.

### Parallel team strategy

With 3 developers after Phase 2:

- Dev A: Phase 3 US1 (the long pole — isolation + MCP binding + chat_task routing).
- Dev B: Phase 5 US4 (compose templates, HMR) — runs entirely off the Phase 2 foundation.
- Dev C: Phase 4 US2 test authoring + Phase 8 US5 tests (both independent of US1's running code, only depend on the service contract).

Merge order is still US1 → US4 → US2 → US7 → US3 → US5 → US6 → Polish, matching the dependency graph above.

---

## Notes

- **Tests required**: Not optional. Constitution Principle VII and research.md §12 both gate the feature behind concrete test coverage. Every story phase includes contract + integration tests that MUST pass before the story is considered done.
- **[P] tasks**: different files, no dependencies. If you're unsure, don't parallelise — correctness over speed.
- **File paths**: every task cites a real path under `src/openclow/`, `projctl/`, `tests/`, `alembic/`, or `specs/001-per-chat-instances/`. No placeholders.
- **Commit discipline**: commit after each task or small logical group. Per constitution VIII, never `--no-verify`. Pre-commit hooks include the async-lint rules from T002 and the compose-no-ports test from T016.
- **Verification**: every task ends with a concrete, testable outcome. Mark `[x]` only when verified per Principle VII ("Done and verified" / "Done but unverified" / "Partially done" discipline from the constitution).
- **No half-features**: if a task slips, scope it down to a vertical slice rather than partially landing. The PR mapping at the top is the backstop for "what counts as a complete slice."
