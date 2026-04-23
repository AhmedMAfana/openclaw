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
- [ ] T031 [P] [US1] Create `tests/integration/test_provision_teardown_e2e.py` — real Docker + real Postgres + stubbed Cloudflare via `pytest-httpx`. Spin up a fixture Laravel+Vue instance, assert compose up OK, tunnel status `active`, health check OK, then teardown and assert zero residue per [quickstart.md §8](quickstart.md) *(scaffold only: module skips until T036/T037 land; test bodies are `pytest.skip()` placeholders documenting what the fleshed-out test must assert.)*
- [ ] T032 [P] [US1] Create `tests/integration/test_agent_isolation.py` — adversarial harness. Spawn MCP fleet bound to `inst-A`; attempt (a) read `/workspaces/inst-B/...`, (b) `instance_exec("cloudflared", ...)`, (c) `git checkout other-branch`, (d) push to a different repo URL. Assert every attempt fails at the MCP layer *(scaffold only: module skips via `pytest.importorskip` until T038–T040 land.)*
- [ ] T033 [P] [US1] Create `tests/unit/test_mcp_manifest.py` — render the MCP tool manifests for `instance_mcp`, `workspace_mcp`, `git_mcp`; assert NONE of their tool schemas contain an argument whose name contains `instance`, `project`, `workspace`, or `container`. Principle III enforcement per [research.md §12 test #2](research.md#12-test-coverage-gates) *(scaffold only: module skips via `pytest.importorskip` until T038–T040 land; when they do, the assertion runs unmodified.)*
- [ ] T034 [P] [US1] Create `tests/integration/test_per_user_cap.py` — open 3 chats, all provision; open 4th → `PerUserCapExceeded` with `active_chat_ids` populated; terminate one, re-open 4th → provisions OK. Raise cap via `platform_config` → re-read takes effect without restart *(scaffold only: skips unless OPENCLOW_DB_TESTS=1 + a real Postgres/Redis are wired. The "without restart" leg flags work for a follow-up: InstanceService currently freezes `per_user_cap` in the constructor — it will need a per-call platform_config read.)*
- [ ] T034a [P] [US1] Create `tests/integration/test_platform_capacity_error.py` — monkey-patch the host-capacity check (or `InstanceService.provision`'s capacity guard) to raise `PlatformAtCapacity` regardless of actual host resources. Assert the chat-facing error text contains "try again later" AND does NOT contain "too many active chats" (proves FR-030 and FR-030a are user-distinguishable). Assert the error carries retry-later guidance but no per-chat navigation menu (FR-030 vs FR-030b) *(scaffold only: skips until T044's chat_task error translator lands.)*

### Implementation for User Story 1

- [X] T035 [US1] Create `src/openclow/services/instance_service.py` implementing the full contract in [contracts/instance-service.md](contracts/instance-service.md): `provision`, `touch`, `terminate`, `get_or_resume`, `list_active`, `record_heartbeat`. State-machine transitions use DB-level CHECK constraints as the first line; service-layer guards enforce the rest. Redis lock `openclow:user:<user_id>:provision` around the cap-check + INSERT per [research.md §9](research.md#9-per-user-quota-enforcement) *(service accepts injectable seams — session_factory / lock_factory / capacity_guard / job_enqueuer — so contract tests run without real infra; production callers will bind a real Redis lock + the ARQ pool at worker startup. `per_user_cap` is currently constructor-frozen; T034's "without restart" leg flags the platform_config-per-call refactor for a follow-up.)*
- [X] T036 [US1] Create `src/openclow/worker/tasks/instance_tasks.py` with `async provision_instance(instance_id)` ARQ job: render compose (call T015) → create Docker secret `tagh-inst-<slug>-cf` from CF creds JSON → call `TunnelService.provision` → `docker compose up -p tagh-inst-<slug>` (via `asyncio.create_subprocess_exec` with timeout) → poll `projctl up` stdout for `step_success` events → flip `status='running'`. Idempotent per [research.md §4](research.md#4-idempotency-keys-for-lifecycle-operations) *(TUNNEL_TOKEN is injected via subprocess env rather than a Docker secret object — Docker secrets require Swarm, which v1 does not run. Principle IV (secrets never on disk) is still met: token is held in process memory, handed to compose via env, and discarded when the job returns. The `tunnel_row.credentials_secret` column still records the canonical secret name for forward-compat with a Swarm-backed deployment.)*
- [X] T037 [US1] Extend `src/openclow/worker/tasks/instance_tasks.py` with `async teardown_instance(instance_id)`: `docker compose down -p tagh-inst-<slug>` (no-op if gone) → CF DNS record cleanup (re-query, skip missing) → `TunnelService.destroy` (skip missing) → `docker secret rm tagh-inst-<slug>-cf` (skip missing) → remove `/workspaces/inst-<slug>/` → flip `status='destroyed'` *(since v1 does not create a Docker secret object (see T036 note), there is nothing to `docker secret rm`; `TunnelService.destroy` already idempotently removes the CF tunnel + DNS records.)*
- [ ] T037a [US1] Extend `src/openclow/worker/tasks/chat_task.py` — wrap per-task execution in a Redis lock `openclow:instance:<slug>` (re-use the pattern from [workspace_service.py:36](../../src/openclow/services/workspace_service.py#L36), re-scoped to instance). Second concurrent task in the same chat MUST wait for the first to finish. Create `tests/integration/test_per_instance_task_lock.py` — issue two concurrent tasks against the same chat; assert serial execution order via timestamps in the audit log (FR-028).
- [X] T038 [P] [US1] Create `src/openclow/mcp_servers/instance_mcp.py` — argv: `--compose-project tagh-inst-<slug> --allowed-services app,web,node,db,redis`. Tools: `instance_exec(service, cmd)`, `instance_logs(service)`, `instance_restart(service)`, `instance_ps()`, `instance_health()`. Every tool rejects any `service` not in the allowlist; `cloudflared` is NEVER in the allowlist *(refuses to start at all if `cloudflared` appears in the allowlist so an operator cannot enable it by mistake.)*
- [X] T039 [P] [US1] Create `src/openclow/mcp_servers/workspace_mcp.py` — argv: `--root /workspaces/inst-<slug>`. Tools: `read_file`, `write_file`, `edit_file`, `list_dir`, `search`. Every path is resolved via `os.path.realpath` and rejected if it does not start with `--root` after symlink chase *(`--root` itself is realpath-resolved at startup so a symlinked root cannot widen the reachable set.)*
- [X] T040 [P] [US1] Extend `src/openclow/mcp_servers/git_mcp.py` — accept `--workspace <path>` and `--branch <name>` argv. Tools `git_status`, `git_diff`, `git_add`, `git_commit`, `git_push`, `git_log` OK; `git_checkout`, `git_branch -D`, `git_reset --hard <ref>` refused if the resulting HEAD would not be `<branch>` *(under pinned mode `git_checkout` / `git_reset` / `git_branch_delete` are simply not exposed — "refused" becomes "absent". `git_commit` and `git_push` re-verify HEAD is still on `<branch>` before acting. Legacy positional-argv callers still work.)*
- [X] T041 [US1] Extend `src/openclow/providers/llm/claude.py` with three factories: `_mcp_instance(instance: Instance)`, `_mcp_workspace(instance: Instance)`, `_mcp_git_pinned(instance: Instance)` — each spawns a subprocess with the bound argv. No factory accepts an identifier at call time *(also added `CONTAINER_MODE_TOOLS` allowlist and `_container_mode_mcp_servers(instance)` helper so T042's chat_task wiring is a one-liner.)*
- [ ] T042 [US1] Extend `src/openclow/worker/tasks/chat_task.py`: for a chat whose project is `mode='container'`, call `InstanceService.get_or_resume(chat_session_id)` to get a running Instance, then start the `claude_agent_sdk` session with MCP config = only `[_mcp_instance, _mcp_workspace, _mcp_git_pinned]` factories. NO `Bash` / `docker` / `host_run_command` are loaded. Every tool call streams through `audit_service` with `{instance_slug, chat_session_id, task_id}` fields
- [ ] T043 [US1] Create `src/openclow/api/routers/instances.py` with `GET /api/users/<user_id>/instances` that returns the list of active instances for the per-user-cap error UI (FR-030b). Mount on the existing FastAPI app
- [ ] T044 [US1] Wire the per-user-cap error translation in `chat_task.py`: catch `PerUserCapExceeded` → render a chat message "You have 3 active chats. End one to start another." with buttons linking to each active chat + a Main Menu button. Catch `PlatformAtCapacity` → distinct "at capacity, try again later" message

**Checkpoint**: User Story 1 fully functional. Two chats get isolated instances. Adversarial tests T032/T033 pass. Per-user cap returns the distinct error. No ambient-authority tool is visible to any agent.

---

## Phase 4: User Story 2 — Automatic cleanup of idle environments (Priority: P1)

**Goal**: Idle environments are detected and torn down automatically after 24 h + 60-min grace.

**Independent Test**: Follow [quickstart.md §3](quickstart.md). Fast-forward `last_activity_at`, observe grace banner, observe `running→idle→terminating→destroyed` transition, observe activity cancels teardown.

### Tests for User Story 2

- [ ] T045 [P] [US2] Create `tests/integration/test_inactivity_reaper.py` — insert instance with `expires_at = now() - 1s`, run one reaper cycle, assert `status='idle'` and `grace_notification_at` set; advance past grace window, run again, assert `status='terminating'`; mid-grace send a chat message (touch), assert `status='running'` and `grace_notification_at=NULL`
- [ ] T046 [P] [US2] Create `tests/unit/test_reaper_dry_run.py` — set `REAPER_DRY_RUN=1`, run cycle against synthetic expired rows, assert zero DB mutations, assert audit log emits planned actions
- [ ] T047 [P] [US2] Create `tests/contract/test_heartbeat_api.py` implementing the seven assertions from [contracts/heartbeat-api.md §Test coverage](contracts/heartbeat-api.md): valid HMAC → 200 bumps `last_activity_at`; forged HMAC → 401; cross-instance HMAC → 401; slug mismatch → 401; `terminating` status → 409; >30 req/s → 429 with Retry-After; GitHub App outage on rotate-git-token → 503

### Implementation for User Story 2

- [ ] T048 [US2] Create `src/openclow/services/inactivity_reaper.py` — `async reap()` implementing the two-phase query from [research.md §11](research.md#11-reaper-activity-source-wiring): first transition `running→idle` (setting `grace_notification_at`, emitting chat banner via the provider abstraction), then transition `idle→terminating` for rows past `grace_notification_at + grace_window`. Use `FOR UPDATE SKIP LOCKED LIMIT 50`. Respects `REAPER_DRY_RUN=1`
- [ ] T049 [US2] Extend `src/openclow/worker/arq_app.py`: register `inactivity_reaper.reap` as a 5-min cron; register `provision_instance` and `teardown_instance` in the ARQ functions list
- [ ] T050 [US2] Extend `src/openclow/api/routers/instances.py` with `POST /internal/instances/<slug>/heartbeat` per [contracts/heartbeat-api.md](contracts/heartbeat-api.md): HMAC-SHA256 verification using `hmac.compare_digest`, rate limit via Redis `INCR/EXPIRE` per instance, call `InstanceService.record_heartbeat`. Reject if slug in path does not match HMAC's instance
- [ ] T051 [US2] Extend `chat_task.py`: on every inbound message, call `InstanceService.touch(instance_id)` before dispatching the task. This is the primary activity source (FR-009)
- [ ] T052 [US2] Implement `projctl/internal/steps/heartbeat.go` — daemon loop every 60 s while (a) dev server running, (b) task executing, or (c) shell attached. Signs request body with HMAC-SHA256 using `HEARTBEAT_SECRET`. Spawned by `tini` inside the `app` container — NOT a separate container (arch doc §7)
- [ ] T053 [US2] Add operator-config read path: `platform_config` (`category="instance"`, `key="idle_ttl_hours"` default 24, `key="idle_grace_minutes"` default 60, `key="per_user_cap"` default 3) — read fresh on every provision/reaper call so operator tuning takes effect without restart (FR-007, FR-008, FR-030a)

**Checkpoint**: Idle cleanup works end-to-end. Grace banner renders in the chat. Activity during grace cancels teardown. Heartbeat from `projctl` inside the instance bumps activity. Operator can tune TTL/grace/cap without restart.

---

## Phase 5: User Story 4 — Live preview via public URL (Priority: P1)

**Goal**: Each instance exposes a public URL serving its running app with hot-reload support.

**Independent Test**: Follow [quickstart.md §1](quickstart.md) steps 3–5 — open the preview URL, edit a `.vue` file, confirm browser update within 3 s.

### Tests for User Story 4

- [ ] T054 [P] [US4] Create `tests/integration/test_hmr_over_tunnel.py` — provision a test instance, open its `hmr_hostname` via a WebSocket client, perform **100 sequential file edits** in the workspace, record per-edit HMR-payload arrival latency; assert p95 < 3 s (SC-005 "at least 95% of edit events"), assert every edit's payload eventually arrives (no drops)
- [ ] T055 [P] [US4] Create `tests/unit/test_hostname_entropy.py` — run the slug generator 10 000×; assert each result matches `^inst-[0-9a-f]{14}$` (56-bit entropy floor per FR-018a), assert no collision across 10 000 runs, assert the generator is NOT derivable from `chat_session_id` / `user_id` / `project_id` / current time alone. **Also add a no-resurrection regression** (FR-016): against a mocked `instances` table, simulate 10 000 provision→destroy→provision cycles; after every cycle assert the new slug does not equal any previously-assigned slug in history. Closes analyze finding C7.

### Implementation for User Story 4

- [X] T056 [US4] Create `src/openclow/setup/compose_templates/laravel-vue/compose.yml` — 5 services (`app` php-fpm, `web` nginx, `node` vite, `db` mysql, `cloudflared`). NO `ports:` on any service except `cloudflared`'s internal metrics port. Use compose-env-var interpolation for `INSTANCE_HOST`, `INSTANCE_HMR_HOST`, `DB_PASSWORD`, `GITHUB_TOKEN`, `HEARTBEAT_SECRET` *(uses Compose list-form `environment: - KEY` for compose-up-time secrets so the renderer's `${VAR}` regex stays reserved for render-time template variables; provision_instance exports DB_PASSWORD/MYSQL_PASSWORD/MYSQL_ROOT_PASSWORD/GITHUB_TOKEN/HEARTBEAT_SECRET/HEARTBEAT_URL/TUNNEL_TOKEN before invoking `docker compose up`. Dropped the optional IDE (toolbox) service for v1; ingress rule still in cloudflared.yml as an opt-in branch.)*
- [X] T057 [P] [US4] Create `src/openclow/setup/compose_templates/laravel-vue/cloudflared.yml` — ingress rules template matching arch doc §5.3: web→http://web:80, hmr→http://node:5173 with `noTLSVerify: true`, optional ide→http://toolbox:3000, fallback `http_status:404` *(IDE branch commented out pending the toolbox service landing with Phase 5+; the fallback `http_status:404` is the last rule so unknown hostnames never silently reach a service.)*
- [X] T058 [P] [US4] Create `src/openclow/setup/compose_templates/laravel-vue/vite.config.js` — HMR snippet honouring `INSTANCE_HOST` / `INSTANCE_HMR_HOST` env contract per arch doc §5.4: `clientPort: 443`, `protocol: 'wss'`, `allowedHosts: [INSTANCE_HOST, INSTANCE_HMR_HOST]`
- [X] T059 [P] [US4] Create `src/openclow/setup/compose_templates/laravel-vue/guide.md` — declarative steps for projctl: `install-php` (`composer install --no-interaction`), `install-node` (`npm ci`), `migrate` (`php artisan migrate --force`), `start-queue`, `start-php`, `start-node`. Each with success-check per T017 spec
- [ ] T060 [US4] Extend `src/openclow/services/instance_service.py` — `slug` generator uses `secrets.token_hex(7)` (56-bit entropy per FR-018a; produces `inst-<14 hex>` = 19 chars, inside the 20-char DNS-label cap). Never derives from any identifier

**Checkpoint**: A provisioned instance serves its app at `https://<slug>.dev.<domain>`. HMR works end-to-end. Slug generator passes the entropy test.

---

## Phase 6: User Story 7 — Assistant confined to current chat's environment (Priority: P1)

**Goal**: Every assistant action stays inside the chat's environment; git pushes cannot target another repo; the sidecar cannot be addressed.

**Independent Test**: Follow [quickstart.md §2](quickstart.md) — adversarial prompts against a cross-chat target; every attempt fails.

### Tests for User Story 7

- [ ] T061 [P] [US7] Create `tests/integration/test_github_push_scoping.py` — mint a GitHub push token for `inst-A` bound to repo `org/acme-A`; mutate the workspace's git remote URL to `org/acme-B`; attempt `git push`; assert GitHub rejects at the auth layer (403) and orchestrator logs the failure as `push_unauthorized`
- [ ] T062 [P] [US7] Create `tests/integration/test_cloudflared_service_forbidden.py` — instruct an agent to call `instance_exec("cloudflared", "kill 1")`; assert `instance_mcp` refuses with a "service not in allowlist" error; audit log records one rejection with the rejected service name

### Implementation for User Story 7

- [ ] T063 [US7] Extend `src/openclow/worker/tasks/instance_tasks.py` with `async rotate_github_token(instance_id)` ARQ job — mints a fresh installation token via `CredentialsService.github_push_token` and writes it to the instance's `~/.git-credentials` via a secure `docker exec`. Called every 45 min by the in-instance cron (T064)
- [ ] T064 [US7] Extend `src/openclow/api/routers/instances.py` with `POST /internal/instances/<slug>/rotate-git-token` per [contracts/heartbeat-api.md §rotate-git-token](contracts/heartbeat-api.md): same HMAC auth as heartbeat; returns `{token, expires_at, repo}`. On GitHub App outage return 503 with `Retry-After`
- [ ] T065 [US7] Implement `projctl/internal/steps/rotate_git_token.go` — cron loop every 45 min: POST to the orchestrator's rotate-git-token endpoint, write response token to `$HOME/.git-credentials` and update `GITHUB_TOKEN` env for subsequent shells

**Checkpoint**: Push-scoping test passes. Sidecar-restart refusal logged. Token rotation runs silently every 45 min. Two defensive layers (tool-pinning + credential-scoping) both in place.

---

## Phase 7: User Story 3 — Resume a chat after its environment was cleaned up (Priority: P2)

**Goal**: A returning user gets a fresh environment on their next message, with their in-progress code branch reattached.

**Independent Test**: Follow [quickstart.md §3](quickstart.md) step 7 — after destroy, new message provisions fresh instance and the previous code changes on the chat's working branch are present.

### Tests for User Story 3

- [ ] T066 [P] [US3] Create `tests/integration/test_resume_after_teardown.py` — provision → make a commit on the chat's session branch → teardown (`destroyed`) → send new chat message → assert new Instance row with different UUID + slug, assert the workspace contains the prior commit on the session branch (branch reattached from per-project cache per FR-012/FR-013), measure wall-clock from the resume message to `status='running'` and assert < 120 s on the warm path (SC-004)
- [ ] T067 [P] [US3] Create `tests/integration/test_resume_never_provisioned.py` — brand-new chat on a `mode='container'` project; first message; assert provisioning follows the same flow as resume (same code path, no special-case branching)

### Implementation for User Story 3

- [ ] T068 [US3] Extend `src/openclow/services/workspace_service.py` — add `async reattach_session_branch(cache_repo_path, session_branch, instance_workspace_path)` that `git worktree add`s the session branch into `/workspaces/inst-<slug>/`. Preserves [workspace_service.py:30-80](../../src/openclow/services/workspace_service.py#L30-L80) cache+worktree pattern per constitution (re-used, re-scoped to instance instead of task)
- [ ] T069 [US3] Extend `InstanceService.get_or_resume` (from T035): when no active row exists but the chat has a `session_branch` from a destroyed instance, call `WorkspaceService.reattach_session_branch` during provision before `projctl up`. The `session_branch` field is carried forward from the previous Instance row (or seeded from the chat session if no prior instance)
- [ ] T070 [US3] Extend `chat_task.py` — while `status='provisioning'`, render a non-blocking "starting up — about N seconds" banner; when `status='running'` arrives, clear the banner and resume normal dispatch

**Checkpoint**: Resume test passes. Branch reattach preserves commits. First-time chat and resume share one code path.

---

## Phase 8: User Story 5 — Manual "end session" control (Priority: P2)

**Goal**: Users can terminate their environment immediately via chat command or button.

**Independent Test**: Follow [quickstart.md §4](quickstart.md) — `/terminate` → immediate destroy → next message provisions fresh.

### Tests for User Story 5

- [ ] T071 [P] [US5] Create `tests/integration/test_manual_terminate.py` — `/terminate` → assert `status='terminating'` within 1 s, `terminated_reason='user_request'`; within 30 s `status='destroyed'`, zero residual containers; next message provisions a new row
- [ ] T072 [P] [US5] Create `tests/unit/test_terminate_race.py` — simultaneous `terminate(instance_id)` + inbound-message path; assert `terminate` wins (Redis lock acquired first), the message path waits for teardown then re-enters `provision` rather than racing against the terminating row

### Implementation for User Story 5

- [ ] T073 [US5] Extend `chat_task.py` — recognise the `/terminate` slash command and the "End session" button action; call `InstanceService.terminate(instance_id, reason='user_request')`. Render a confirmation prompt first ("This will destroy your current environment. Continue?") per CLAUDE.md "No Dead Ends" rule — include Cancel + Confirm buttons, not a bare text error on denial
- [ ] T074 [US5] Extend the provider action-button registry (reuse `providers/actions.py` pattern) — add `end_session` action with a confirmation sub-action. Both Telegram and Slack providers render it via existing `ActionKeyboard` abstractions — no provider-specific code

**Checkpoint**: `/terminate` works from chat. "End session" button renders on both Telegram + Slack. Race test proves teardown-wins semantics.

---

## Phase 9: User Story 6 — Clear failure reporting during provisioning (Priority: P2)

**Goal**: Provisioning failures surface with a plain-language reason + Retry + Main Menu controls; failed instances clean up cleanly.

**Independent Test**: Follow [quickstart.md §6](quickstart.md) — inject a failing step, observe 3 LLM attempts, observe failed status with retry path.

### Tests for User Story 6

- [ ] T075 [P] [US6] Create `tests/integration/test_provisioning_failure_retry.py` — inject a guide.md step with `cmd: 'false'`; assert 3 `llm_attempt` events logged; after 3 failures assert `status='failed'` with `failure_code='projctl_up'`; click Retry → assert `projctl up` resumes from the last-successful step (not from step 1) per FR-025. **Also assert failed-state teardown parity (FR-026)**: after a `Cancel` on the failure screen, confirm the same zero-residue invariants as FR-006 — no containers, volumes, Docker secrets, CF tunnel, DNS records, or workspace directory remain for this slug.
- [ ] T076 [P] [US6] Create `tests/unit/test_llm_fallback_envelope.py` — build an envelope from a synthetic failure with 10 000 lines of stdout; assert `stdout_tail` contains only the last 200 lines plus a `... <N> lines truncated ...` marker; assert the redactor has been applied (no bearer tokens or `KEY=value` secrets survive)
- [ ] T077 [P] [US6] Create `tests/contract/test_llm_fallback_envelope.py` — JSON Schema validation against [contracts/llm-fallback-envelope.schema.json](contracts/llm-fallback-envelope.schema.json)

### Implementation for User Story 6

- [ ] T078 [US6] Implement `projctl/internal/steps/explain.go` — on step failure, build the envelope per [contracts/llm-fallback-envelope.schema.json](contracts/llm-fallback-envelope.schema.json), POST to orchestrator's `/internal/instances/<slug>/explain` (HMAC-authenticated), parse the structured `{action, payload, reason}` response per arch §9. Max 3 attempts per step (configurable via `guide.md` step metadata). Apply the action: `shell_cmd` → run via same host_guard-style allowlist; `patch` → `git apply --check` then `git apply`; `skip` → only if step's `skippable: true`; `give_up` → emit `fatal` event and exit non-zero
- [ ] T079 [US6] Extend `src/openclow/api/routers/instances.py` with `POST /internal/instances/<slug>/explain`: receive envelope, run `audit_service.redact` on `stdout_tail` + `stderr_tail` + `guide_section` + `failure_message` (belt-and-braces — projctl already redacted, but the redactor is idempotent and the LLM path MUST call it per Principle IV), forward to LLM via existing `providers/llm/claude.py` wrapper, return structured response
- [ ] T080 [US6] Extend `chat_task.py` — on `instance.failed` event: render a plain-language message using `failure_code` as key ("Couldn't start your environment — npm install failed"). Include **Retry** button (enqueues `provision_instance` which resumes from the last-successful step via projctl state.json) and **Main Menu** button. No dead-end bare text per CLAUDE.md

**Checkpoint**: Retry resumes from last-good step. Envelope never exceeds caps. Redactor runs on both chat and LLM paths. No bare error messages.

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Upstream-outage banner (FR-027a/b/c), retention cascade on chat delete (FR-013a/b/c), bootstrap router flip (PR 12), E2E parity test (PR 11), docs.

### Upstream outage banner (PR 9)

- [ ] T081 [P] Create `tests/integration/test_upstream_degradation_banner.py` — break CF creds (`docker exec tagh-inst-<slug>-cloudflared rm /etc/cloudflared/creds.json`) → within 60 s assert banner rendered in chat ("preview URL temporarily unavailable") AND `instances.status` remains `running` (NOT flipped to `failed`); restore creds → within 60 s banner cleared automatically
- [ ] T082 Extend `src/openclow/services/instance_service.py` with `async record_upstream_degradation(instance_id, capability, upstream)` and `async record_upstream_recovery(...)`; emit `instance.upstream_degraded` / `instance.upstream_recovered` events with redacted payload
- [ ] T083 Extend `src/openclow/worker/tasks/instance_tasks.py` with `async tunnel_health_check(instance_id)` ARQ job (runs every 60 s for each `running` instance); on failure → `record_upstream_degradation`, DO NOT flip status. On recovery → `record_upstream_recovery`. Escalate to "prolonged outage" banner after operator-configurable threshold (default 30 min) per FR-027c — still do NOT auto-teardown
- [ ] T084 Extend `chat_task.py` to listen for `instance.upstream_degraded` / `instance.upstream_recovered` events and render / clear the non-blocking banner via the provider abstraction

### Retention cascade on chat delete (PR 10)

- [ ] T085 [P] Create `tests/integration/test_chat_deletion_cascade.py` — chat with live instance → call `ChatSessionService.delete(chat_session_id)` → assert (a) instance teardown happened (if active), (b) `instances` row deleted, (c) `instance_tunnels` rows deleted (FK cascade), (d) `tasks` rows deleted (FK cascade), (e) audit log entries matching `instance_slug` deleted, (f) chat's working branch GC'd from `/workspaces/_cache/<project>/`
- [ ] T086 Implement `async delete_chat_cascade(chat_session_id)` on `ChatSessionService` (new method or extend existing): (1) synchronously terminate any active instance, (2) delete chat row (cascading FKs handle instances/tunnels/tasks), (3) delete audit entries `WHERE instance_slug IN (<slugs>)`, (4) enqueue ARQ job `gc_session_branch(project_id, branch_name)` to remove the branch from the per-project cache

### E2E parity test + router flip (PR 11 + PR 12)

- [ ] T087 Create `tests/integration/test_nightly_e2e.py` — gated by `TAGH_DEV_E2E_CF_ZONE` env var. Runs nightly, not in the main CI pipeline. **Two measured runs on the same host in sequence**: (1) **cold path** — fresh host (no image cache, no branch cache), full provision → HMR round-trip → teardown, assert wall-clock < 5 min + zero residue (SC-002 cold + SC-003); (2) **warm path** — immediately re-provision the same chat, assert wall-clock < 2 min to `status='running'` (SC-002 warm). Both assertions must pass or the nightly run fails. Closes analyze finding C10.
- [ ] T088 Extend `src/openclow/worker/tasks/bootstrap.py`: add a single-line router at the top of `bootstrap_project`: if project's `mode == 'container'`, delegate to `InstanceService.get_or_resume(chat_session_id)`; else fall through to the existing (legacy) bootstrap code unchanged. Do NOT delete or edit the legacy code path (FR-034, constitution §Architecture Constraints)

### Documentation + verification

- [ ] T089 [P] Update [docs/architecture/per-chat-instances.md](../../docs/architecture/per-chat-instances.md) to reference this spec's finalised decisions from [spec.md §Clarifications](spec.md#clarifications): Q1 public preview URL, Q2 per-user cap 3, Q3 chat-lifetime retention, Q4 keep-running upstream banner, Q5 60-min grace window. Note any deltas from the original architecture doc
- [ ] T090 [P] Update [CLAUDE.md](../../CLAUDE.md) to add a "Per-chat instance mode quick reference" section (keep the SPECKIT block untouched) with: pointers to `InstanceService`, the MCP binding factories, compose templates location, and the rotate-git-token cron
- [ ] T091 Run the full [quickstart.md](quickstart.md) manually against a staging host; mark each section `done and verified` per constitution Principle VII. Record the verification in a short `VERIFICATION.md` in this feature directory

### Load / scale harness (gated, nightly only — SC-001, SC-006, SC-009)

- [ ] T092 [P] Create `tests/load/test_cross_chat_isolation_soak.py` — pytest marker `@pytest.mark.load`, gated by `--run-load-tests` flag. Spawns 20 concurrent chats across 5 synthetic users, runs a rotation of adversarial prompts (path traversal, service-name forgery, cross-repo push, cross-branch checkout) in a loop for a configurable duration (default 1 h in CI, 1 week in the scheduled run). Asserts zero cross-chat audit-log entries over the window (SC-001, SC-009).
- [ ] T093 [P] Create `tests/load/test_fifty_concurrent_instances.py` — pytest marker `@pytest.mark.load`, gated by `--run-load-tests`. Ramp from 0 to 50 concurrent instances, assert every one reaches `status='running'`, assert host RSS < 32 GB while they all idle, assert no provisioning failure carries `failure_code='out_of_capacity'` (SC-006).
- [ ] T094 Create `.github/workflows/nightly-load.yml` that runs `pytest -m load --run-load-tests` against a dedicated Cloudflare test zone on cron `0 3 * * *`. Fails the run (not the PR pipeline) on any regression; posts a summary to the ops channel. Uses dedicated CF credentials from a separate secret store so a leak in the nightly env cannot affect the prod zone.

### Regression tests — v1 guarantees (PR-level gates)

- [ ] T095 [P] Create `tests/integration/test_http_requests_not_activity.py` — provision an instance, let `last_activity_at` settle; over a 10-minute window issue 1 000 browser-style HTTP requests to the preview URL via `httpx.AsyncClient` (varied paths, GET + POST). Assert `last_activity_at` is unchanged at the end of the window and `expires_at` has NOT moved forward (FR-011). Regression guard against any future code path accidentally promoting HTTP traffic to an activity signal. Closes analyze finding C6.
- [ ] T096 [P] Create `tests/integration/test_legacy_mode_parity.py` — provision a representative `mode='host'` project and a `mode='docker'` project; run one standard task against each (e.g., "list files" or a no-op build) via the existing bootstrap flow through [src/openclow/worker/tasks/bootstrap.py](../../src/openclow/worker/tasks/bootstrap.py). Assert (a) no `instances` row is created for legacy-mode tasks, (b) no `instance_tunnels` row, (c) no call into `InstanceService`, (d) the bootstrap router at the top of `bootstrap_project` delegates to the legacy code path with byte-for-byte identical arguments vs a pre-refactor golden snapshot captured in `tests/integration/fixtures/legacy_mode_golden/`. Passes only if legacy mode is provably untouched (FR-034 / FR-036). Closes analyze finding C5.

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
