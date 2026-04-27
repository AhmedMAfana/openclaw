---

description: "Implementation tasks — Admin Instance Management"
---

# Tasks: Admin Instance Management

**Input**: Design documents from `/specs/003-admin-instance-mgmt/`
**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: INCLUDED. The project already maintains `tests/contract/`, `tests/integration/`, `tests/unit/` and Constitution Principle VII mandates end-to-end verification with executable proof. Test tasks are NOT optional for this feature.

**Organization**: Grouped by user story so each one is independently implementable, testable, and shippable.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Maps to user stories from spec.md (US1–US5). Setup, Foundational, and Polish tasks have no story label.
- Every task includes the exact file path(s) it touches.

## Path Conventions

Single-project layout per [plan.md §"Project Structure"](plan.md). Source under `src/openclow/`, tests under `tests/`, alembic migrations under `alembic/versions/`, scripts under `scripts/`. Paths shown are repo-root-relative.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: confirm baseline matches research.md before any change lands.

- [x] T001 Verify the four research-decision substrates exist as documented in [research.md](research.md): `AuditLog` model + `audit_service.log_action()` ([src/openclow/models/audit.py](src/openclow/models/audit.py), [src/openclow/services/audit_service.py](src/openclow/services/audit_service.py)), SSE generator at [src/openclow/api/routes/activity.py](src/openclow/api/routes/activity.py), `activity_log.query()` ([src/openclow/services/activity_log.py](src/openclow/services/activity_log.py)), and `_require_admin` ([src/openclow/api/routes/access.py](src/openclow/api/routes/access.py)). If any drifted, halt and update research.md before proceeding.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: schema, router skeleton, sidebar slot, SSE filter, and shared serializer/auth helpers — all blocking US1.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T002 [P] Add `admin_forced` to the `TerminatedReason` Python enum in [src/openclow/models/instance.py](src/openclow/models/instance.py).
- [x] T003 [P] Create alembic migration `alembic/versions/014_admin_forced_terminated_reason.py` that amends the `ck_instances_terminated_reason` CHECK constraint to include `'admin_forced'` and provides a forward-only down (rejects if rows already use the new value), per [data-model.md §2](data-model.md).
- [x] T004 [P] Extend the existing SSE generator in [src/openclow/api/routes/activity.py](src/openclow/api/routes/activity.py) to accept a `?slug=` query-parameter filter alongside the existing `?type=` filter, dropping non-matching events before emit. Per [contracts/sse-events.md §"Backend filter"](contracts/sse-events.md).
- [x] T005 [P] Create empty router skeleton `src/openclow/api/routes/admin_instances.py` with module imports, a `router = APIRouter(prefix="/api/admin/instances")`, and a `pages_router = APIRouter()` for the two `/settings/instances*` page routes. Each handler in later tasks adds itself to one of these routers.
- [x] T006 [P] Create Pydantic response-schema module `src/openclow/api/schemas/admin_instances.py` with all five derived view shapes from [data-model.md §3](data-model.md): `InstanceListRow`, `InstanceDetail`, `InstanceLogLine`, `InstanceAuditEntry`, `StatusCounts`. Include the field-redaction policy from [data-model.md §4](data-model.md) — `heartbeat_secret` and `db_password` are NOT defined in any schema (cannot be serialized by accident).
- [x] T007 [P] Create shared serialization helper `src/openclow/api/serializers/admin_instance.py` that builds `InstanceListRow` from an `Instance` + joined `User`/`Project`/`ChatSession`/Redis-upstream-state, computes `status_age_seconds`, fills `available_actions`, and routes any text passing through it via `audit_service.redact()`. Used by every list/detail endpoint.
- [x] T008 [P] Add Instances sidebar entry in [src/openclow/api/templates/base.html](src/openclow/api/templates/base.html) under the Management group, between Projects and Users (per [contracts/admin-instances-api.md §"Page routes"](contracts/admin-instances-api.md)). Match existing `<a class="..." href="/settings/instances">` shape used by Projects.
- [x] T009 [P] Add a single SSE-emit helper `_emit_instance_event(payload: dict)` in [src/openclow/services/instance_service.py](src/openclow/services/instance_service.py) that wraps `activity_log.log_event(...)` for instance_status / instance_action / instance_summary events. All later emit sites (T015, T021, T029, T040) call this one helper. Per [contracts/sse-events.md §"Emit sites"](contracts/sse-events.md).
- [x] T010 Wire the new admin_instances `router` and `pages_router` into FastAPI app in [src/openclow/api/main.py](src/openclow/api/main.py). Depends on T005.
- [x] T011 [P] Unit test `tests/unit/test_admin_instance_serializer.py` asserts the serializer drops `heartbeat_secret` and `db_password` from every Instance dict produced (regression guard for [data-model.md §4](data-model.md)). Depends on T007.
- [x] T012 [P] Unit test `tests/unit/test_admin_role_guard.py` asserts every route in `admin_instances.py` returns 403 when called by a non-admin authenticated user, using the existing `_require_admin` pattern. Depends on T005, T010.

**Checkpoint**: foundation ready. Migration applied, router mounted, sidebar entry visible (renders an empty page until US1 lands), SSE accepts `?slug=`, serializer + schemas + emit helper available. US1 can now begin.

---

## Phase 3: User Story 1 — See & force-terminate active instances (Priority: P1) 🎯 MVP

**Goal**: an admin can open the new Instances page, see all platform-wide instances with live-updating status (≤10s staleness), and force-terminate any of them with a confirmation prompt. Underlying compose stack and tunnel torn down within 60s. Audit row written. Idempotent on already-ended instances.

**Independent Test**: per [quickstart.md §P1](quickstart.md). Provision one real instance via the chat frontend, open `/settings/instances`, verify the row appears within 5s, click Force Terminate, confirm, observe row → `terminating` → removed from default view; verify `terminated_reason='admin_forced'` in DB and one `AuditLog` row with `action='force_terminate'`. A repeated click on a `destroyed` instance returns the `blocked: true` no-op envelope and writes a second audit row — no duplicate teardown job enqueued.

### Tests for User Story 1 (write FIRST, ensure they FAIL before implementation)

- [x] T013 [P] [US1] Contract test `tests/contract/test_admin_instances_list.py` covering `GET /api/admin/instances` (default filter, status filter, user_id filter, project_id filter, q substring, sort/dir, pagination, summary block) per [contracts/admin-instances-api.md §1, §2](contracts/admin-instances-api.md).
- [x] T014 [P] [US1] Contract test `tests/contract/test_admin_instances_terminate.py` covering `POST /api/admin/instances/{slug}/terminate` happy path, `confirm:false` rejection, no-op envelope on already-ended status, 404 on unknown slug, 403 on non-admin, per [contracts/admin-instances-api.md §6](contracts/admin-instances-api.md).
- [x] T015 [P] [US1] Integration test `tests/integration/test_admin_force_terminate_flow.py` driving Force Terminate against an in-memory `InstanceService` fake (using `tests/conftest.py::inmemory_service`) and asserting: status transitions to `terminating`, `teardown_instance` job enqueued exactly once, `AuditLog` row with `action='force_terminate'` and `risk_level='dangerous'` and `metadata.reason='admin_forced'`, SSE `instance_action` event emitted.
- [x] T016 [P] [US1] Integration test `tests/integration/test_admin_force_terminate_idempotency.py` asserting that two concurrent Force Terminate calls on the same `running` instance produce: one `teardown_instance` enqueue, one normal audit row, one `blocked=true` audit row, and the second HTTP response is the no-op envelope — per [data-model.md §6](data-model.md).

### Implementation for User Story 1

- [x] T017 [US1] Implement `GET /api/admin/instances` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): query Instance + joined User/Project/ChatSession with filters (status set, user_id, project_id, q), sort, pagination; build `InstanceListRow[]` via T007 serializer; attach `summary` block via T018. Auth: `Depends(web_user_dep)` + inline `_require_admin(user)`. Per [contracts/admin-instances-api.md §1](contracts/admin-instances-api.md).
- [x] T018 [US1] Implement `GET /api/admin/instances/summary` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): single counts query grouped by status + last-24h failed count + capacity utilization. Returns `StatusCounts`. Per [contracts/admin-instances-api.md §2](contracts/admin-instances-api.md).
- [x] T019 [US1] Implement `POST /api/admin/instances/{slug}/terminate` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): validate `confirm:true`; load Instance by slug (404); if status ∈ {`terminating`, `destroyed`, `failed`} return no-op envelope + write blocked audit row; else call `InstanceService.terminate(instance_id, reason='admin_forced')`; write success `AuditLog` via `audit_service.log_action(...)`; emit `instance_action` SSE; return success envelope. Per [contracts/admin-instances-api.md §6](contracts/admin-instances-api.md).
- [x] T020 [US1] Add `instance_status` SSE emit calls in [src/openclow/services/instance_service.py](src/openclow/services/instance_service.py) at every method that mutates `Instance.status`. Order: DB commit → call T009 helper → return. Payload shape per [contracts/sse-events.md §"instance_status"](contracts/sse-events.md). Reuses helper from T009.
- [x] T021 [US1] Add `instance_status` SSE emit calls at provision/teardown phase boundaries in [src/openclow/worker/tasks/instance_tasks.py](src/openclow/worker/tasks/instance_tasks.py): provisioning → running, → failed (with failure_code/failure_message), terminating → destroyed. Reuses helper from T009.
- [x] T022 [US1] Add `GET /settings/instances` page route in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py) (on `pages_router`) that renders `templates/settings/instances.html`. Auth: `Depends(web_user_dep)` + `_require_admin`. On 403, redirect to `/settings/` (matching existing settings auth-failure pattern).
- [x] T023 [US1] Create [src/openclow/api/templates/settings/instances.html](src/openclow/api/templates/settings/instances.html) extending `base.html`: header with status counts bound to `summary` block, filter bar (status multi-select, owner/project dropdowns, slug search), table of `InstanceListRow` items with color-coded status chips and a `Force Terminate` row action, JS confirmation dialog, `EventSource('/api/activity/stream?type=instance_status,instance_action,instance_summary')` subscriber that patches rows in place, fallback to full GET refresh on reconnect. Per [contracts/sse-events.md §"List view"](contracts/sse-events.md).
- [x] T024 [US1] Run `python -m py_compile` over every changed `.py` file (Constitution Development Workflow gate); run `docker compose restart bot worker api`; execute [quickstart.md §P1 (steps 1.1–1.5)](quickstart.md) manually; verify all P1 acceptance scenarios in spec.md pass.

**Checkpoint**: US1 fully functional and shipping-worthy. Admin can see and kill instances. Phase 11 of spec 001 (T107–T109, T112) is now subsumed.

---

## Phase 4: User Story 2 — Drill into a single instance (Priority: P2)

**Goal**: clicking a row opens a detail view with full status timeline, tunnel health (live-updating via SSE), failure code/message for failed instances, recent worker log lines tagged with this slug (redacted), and recent audit entries. Graceful handling of deleted chat / deleted project.

**Independent Test**: per [quickstart.md §P2](quickstart.md). Force a provision failure (broken compose template), open the failed instance's detail view, verify timeline shows `provisioning → failed`, failure code/message render prominently, redacted worker logs are visible. Delete the chat and confirm the chat link reads "deleted" while every other field remains.

### Tests for User Story 2

- [x] T025 [P] [US2] Contract test `tests/contract/test_admin_instance_detail.py` covering `GET /api/admin/instances/{slug}` happy path (returns full `InstanceDetail` aggregate including `transitions`, `tunnel`, `failure`, `chat`, `project`, `available_actions`), 404 on unknown slug, 403 on non-admin, deleted-chat handling (chat block has `deleted:true`). Per [contracts/admin-instances-api.md §3](contracts/admin-instances-api.md).
- [x] T026 [P] [US2] Contract test `tests/contract/test_admin_instance_logs.py` covering `GET /api/admin/instances/{slug}/logs`: `limit` and `level` filters, asserts every returned `message` and every `context` value passed through `audit_service.redact()` (inject a known secret pattern in test fixture log lines and assert it's masked). Per [contracts/admin-instances-api.md §4](contracts/admin-instances-api.md).
- [x] T027 [P] [US2] Contract test `tests/contract/test_admin_instance_audit.py` covering `GET /api/admin/instances/{slug}/audit`: descending sort, `limit` cap, 404, 403. Per [contracts/admin-instances-api.md §5](contracts/admin-instances-api.md).
- [x] T028 [P] [US2] Integration test `tests/integration/test_admin_detail_view.py` driving the full aggregation: seed a `failed` instance with status transitions + AuditLog entries + activity_log lines for the slug + Redis upstream-degradation state; assert the JSON response composes them correctly and `available_actions` is `["force_terminate","reprovision","open_in_chat"]`.

### Implementation for User Story 2

- [x] T029 [US2] Implement `GET /api/admin/instances/{slug}` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): load Instance by slug (404); join chat/project (graceful "deleted" placeholder when nullable FK fails); build status `transitions[]` from existing audit/log substrate; read tunnel `health` + `degradation_history` from Redis at `openclow:instance_upstream:<slug>:*`; surface `failure_code`/`failure_message`; compute `available_actions` from current status; return `InstanceDetail`. Per [data-model.md §3.2](data-model.md).
- [x] T030 [US2] Implement `GET /api/admin/instances/{slug}/logs` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): call `activity_log.query(filters={"instance_slug": slug, "level": level_or_None}, last_n=limit)`; wrap each line's `message` and `context` values through `audit_service.redact()`; return `InstanceLogLine[]`. Per [contracts/admin-instances-api.md §4](contracts/admin-instances-api.md).
- [x] T031 [US2] Implement `GET /api/admin/instances/{slug}/audit` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): call `audit_service.get_recent(instance_slug=slug, limit=limit)` with descending sort; project to `InstanceAuditEntry[]`. Per [contracts/admin-instances-api.md §5](contracts/admin-instances-api.md).
- [x] T032 [US2] Add `instance_upstream` SSE emit at the state-change site in [src/openclow/worker/tasks/instance_tasks.py::tunnel_health_check_cron](src/openclow/worker/tasks/instance_tasks.py): emit only on transitions (not every probe), one emit per (slug, capability) state change. Per [contracts/sse-events.md §"instance_upstream"](contracts/sse-events.md).
- [x] T033 [US2] Add `GET /settings/instances/{slug}` page route in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py) (on `pages_router`) that renders `templates/settings/instance_detail.html`.
- [x] T034 [US2] Create [src/openclow/api/templates/settings/instance_detail.html](src/openclow/api/templates/settings/instance_detail.html) extending `base.html`: header with slug + status badge + owner/project links (graceful "deleted" placeholder), Timeline section (chronological list of transitions), Tunnel section (URL + health badge + degradation history), Failure section (visible when status=`failed`), Recent Worker Logs panel (50 lines, level filter), Recent Audit panel, Available Actions toolbar (initially only Force Terminate and Open Preview/Open in Chat — US3 buttons added later), `EventSource('/api/activity/stream?type=instance_status,instance_action,instance_upstream&slug=<slug>')` subscriber that patches the relevant DOM section in place. Per [contracts/sse-events.md §"Detail view"](contracts/sse-events.md).
- [x] T035 [US2] Run `python -m py_compile` on changed files; restart api+worker; execute [quickstart.md §P2 (steps 2.1–2.3)](quickstart.md) manually; verify all P2 acceptance scenarios in spec.md pass, including deleted-chat graceful handling.

**Checkpoint**: US1 + US2 functional. Admin can diagnose any instance without leaving the dashboard.

---

## Phase 5: User Story 3 — Recover and operate (Priority: P3)

**Goal**: from the detail view, the admin can Reprovision a failed/destroyed instance (rebinds same chat to a new instance), Rotate Git Token on a running instance (≤10s response), Extend Expiry by 1h/4h/24h, and click Open Preview / Open in Chat for quick access.

**Independent Test**: per [quickstart.md §P3](quickstart.md). Reprovision a failed instance and assert chat rebinding + new slug; Rotate Token on a running instance and assert fresh `~/.git-credentials` inside the container within 10s; Extend Expiry +4h and assert `expires_at` advances by exactly 4h.

### Tests for User Story 3

- [x] T036 [P] [US3] Contract test `tests/contract/test_admin_reprovision.py` covering `POST /api/admin/instances/{slug}/reprovision`: happy path returns `{old_slug, new_slug, new_status, audit_id}`; 409 on wrong status; 422 on chat_deleted; 403 on non-admin. Per [contracts/admin-instances-api.md §8](contracts/admin-instances-api.md).
- [x] T037 [P] [US3] Contract test `tests/contract/test_admin_rotate_token.py` covering `POST /api/admin/instances/{slug}/rotate-token`: happy path within 10s budget; 409 on non-running; 502 on upstream GitHub failure with audit `exit_code != 0`. Per [contracts/admin-instances-api.md §9](contracts/admin-instances-api.md).
- [x] T038 [P] [US3] Contract test `tests/contract/test_admin_extend_expiry.py` covering `POST /api/admin/instances/{slug}/extend-expiry`: valid hours {1,4,24}; 400 on invalid hours; 409 on non-running/idle; correctly advances `expires_at` (asserts the `max(now, expires_at) + delta` semantics from data-model.md). Per [contracts/admin-instances-api.md §10](contracts/admin-instances-api.md).
- [x] T039 [P] [US3] Integration test `tests/integration/test_admin_recovery_actions.py` end-to-end: failed instance → Reprovision → new instance + chat rebound + old row preserved; running instance → Rotate Token → fresh token observed; running instance → Extend Expiry → reaper tick does not terminate before new deadline.

### Implementation for User Story 3

- [x] T040 [US3] Add `InstanceService.extend_expiry(instance_id: UUID, hours: int) -> datetime` in [src/openclow/services/instance_service.py](src/openclow/services/instance_service.py): single-row UPDATE `expires_at = max(now, expires_at) + timedelta(hours=hours)`; return new value; emit `instance_status` event (status unchanged, but `expires_at` changed — payload includes new `expires_at`). Per [data-model.md §5](data-model.md).
- [x] T041 [US3] Verify `InstanceService.rotate_github_token_sync(instance_id: UUID)` exists; if absent, add it as a synchronous wrapper around the existing `rotate_github_token` ARQ job logic that mints + injects a token within the request timeout (≤10s). Reuse the existing job's helpers; do NOT duplicate token-minting logic. File: [src/openclow/services/instance_service.py](src/openclow/services/instance_service.py).
- [x] T042 [US3] Implement `POST /api/admin/instances/{slug}/reprovision` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): gate on status ∈ {`failed`, `destroyed`}; gate on chat exists (else 422 `chat_deleted`); call `InstanceService.provision(chat_id=instance.chat_session_id)`; write AuditLog; return `{old_slug, new_slug, new_status, audit_id}`. Per [contracts/admin-instances-api.md §8](contracts/admin-instances-api.md).
- [x] T043 [US3] Implement `POST /api/admin/instances/{slug}/rotate-token` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): gate on status == `running`; call `InstanceService.rotate_github_token_sync(instance.id)`; write AuditLog with `risk_level='elevated'`; return `{slug, rotated_at, audit_id}`. On upstream GitHub error, return 502 + audit row with `exit_code != 0`. Per [contracts/admin-instances-api.md §9](contracts/admin-instances-api.md).
- [x] T044 [US3] Implement `POST /api/admin/instances/{slug}/extend-expiry` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): validate `extend_hours` ∈ {1,4,24} (else 400); gate on status ∈ {`running`, `idle`} (else 409); call `InstanceService.extend_expiry(...)`; write AuditLog with `risk_level='elevated'` and `metadata={"hours": …, "old_expires_at": …, "new_expires_at": …}`; return `{slug, new_expires_at, audit_id}`. Per [contracts/admin-instances-api.md §10](contracts/admin-instances-api.md).
- [x] T045 [US3] Add Reprovision / Rotate Git Token / Extend Expiry (with 1h/4h/24h selector) / Open Preview URL / Open in Chat buttons to [src/openclow/api/templates/settings/instance_detail.html](src/openclow/api/templates/settings/instance_detail.html). Each button's visibility driven by the `available_actions` array returned by T029. Each destructive action shows a confirmation dialog before POSTing.
- [x] T046 [US3] Run `python -m py_compile`; restart api+worker; execute [quickstart.md §P3 (steps 3.1–3.3)](quickstart.md) manually; verify all P3 acceptance scenarios in spec.md pass.

**Checkpoint**: US1 + US2 + US3 functional. Admin has full single-instance lifecycle control.

---

## Phase 6: User Story 4 — Filter, search, bulk Force Terminate (Priority: P4)

**Goal**: list view supports rich filters, free-text slug search (already in US1's GET), preset-filter shortcuts, multi-row selection, and Bulk Force Terminate capped at 50 with explicit confirmation.

**Independent Test**: per [quickstart.md §P4](quickstart.md). Seed 30+ instances; apply status filter and verify only matching rows render; multi-select 5; click Bulk Force Terminate; confirm; verify all 5 transition to `terminating` and one audit row written per slug. Attempt 51-slug bulk and assert 422 `bulk_cap_exceeded`.

### Tests for User Story 4

- [x] T047 [P] [US4] Contract test `tests/contract/test_admin_bulk_terminate.py` covering `POST /api/admin/instances/bulk-terminate`: 1–50 slugs happy path with mixed `outcome` values (`queued`/`already_ended`/`not_found`), `confirm:false` rejection, 422 on >50 slugs, 403 on non-admin. Per [contracts/admin-instances-api.md §7](contracts/admin-instances-api.md).
- [x] T048 [P] [US4] Integration test `tests/integration/test_admin_bulk_terminate.py` end-to-end: seed 5 mixed-status instances, bulk-terminate, assert per-slug outcomes, assert one `AuditLog` row per affected slug with `action='bulk_force_terminate'` and `metadata.bulk_size=5`, assert no enqueues for `already_ended`/`not_found` slugs.

### Implementation for User Story 4

- [x] T049 [US4] Implement `POST /api/admin/instances/bulk-terminate` handler in [src/openclow/api/routes/admin_instances.py](src/openclow/api/routes/admin_instances.py): validate `confirm:true` and `len(slugs)` ∈ [1, 50] (else 422 `bulk_cap_exceeded`); for each slug iterate the same logic as T019 (skip-if-already-ended path, enqueue otherwise, write per-slug AuditLog); return per-slug `outcome` array; never partial-fail. Per [contracts/admin-instances-api.md §7](contracts/admin-instances-api.md) + [data-model.md §6](data-model.md).
- [x] T050 [US4] Add multi-row selection (checkboxes), bulk action toolbar (visible when ≥1 selected), and Bulk Force Terminate button + confirmation dialog (lists count + sample of slugs/owners) to [src/openclow/api/templates/settings/instances.html](src/openclow/api/templates/settings/instances.html). Client-side hard cap at 50 selected rows; "select all on page" never auto-selects beyond 50. Per FR-022, FR-023.
- [x] T051 [US4] Add preset-filter chips ("Stuck in provisioning >5 min", "Failed today", "Idle approaching expiry") to [src/openclow/api/templates/settings/instances.html](src/openclow/api/templates/settings/instances.html). Each chip applies a fixed combination of `status=`, `created_at<`, `expires_at<` query params on the existing list endpoint.
- [x] T052 [US4] Run `python -m py_compile`; restart api+worker; execute [quickstart.md §P4 (steps 4.1–4.3)](quickstart.md) manually; verify all P4 acceptance scenarios in spec.md pass.

**Checkpoint**: scale-tier features in. Admin can operate at 50–500 active instances comfortably.

---

## Phase 7: User Story 5 — Health overview at a glance (Priority: P5)

**Goal**: header counts on the Instances page emit live `instance_summary` SSE events (debounced ≤1/min), and a Recent Failures (24h) strip lists the 5 most recent failed instances with click-through. (Optional: small instance-counts widget on the Overview settings page.)

**Independent Test**: per [quickstart.md §P5](quickstart.md). Spawn/terminate instances and verify the header counts update via SSE within 10s without page refresh; force two failures and verify the Recent Failures strip lists them with timestamps + failure codes + click-through links.

### Tests for User Story 5

- [x] T053 [P] [US5] Contract test `tests/contract/test_admin_instance_summary_sse.py` asserting the `instance_summary` payload shape per [contracts/sse-events.md §"instance_summary"](contracts/sse-events.md): all five count fields + `total_active` + `at`. Tests via the in-memory event bus, not over a real SSE connection.
- [x] T054 [P] [US5] Integration test `tests/integration/test_admin_recent_failures.py`: seed 7 failed instances at different timestamps over 24h; assert the recent-failures payload returns exactly the 5 newest in descending time order.

### Implementation for User Story 5

- [x] T055 [US5] Add a debounced `_emit_instance_summary()` helper in [src/openclow/services/instance_service.py](src/openclow/services/instance_service.py) (rate-limit ≤1 emit per 60s, server-side; debounce key = a single in-process timestamp). Call it from every `instance_status` emit site (T020, T021). Per [contracts/sse-events.md §"instance_summary"](contracts/sse-events.md).
- [x] T056 [US5] Add Recent Failures (24h) strip at the top of [src/openclow/api/templates/settings/instances.html](src/openclow/api/templates/settings/instances.html). Pulls from a new compact endpoint `GET /api/admin/instances?status=failed&limit=5&sort=created_at&dir=desc` (no new endpoint needed — reuses T017). Each entry: timestamp + slug + failure_code, clickable into `/settings/instances/<slug>`.
- [x] T057 [US5] *(Optional)* Add small instance-counts widget to [src/openclow/api/templates/settings/overview.html](src/openclow/api/templates/settings/overview.html) by fetching `/api/admin/instances/summary` once on page load and subscribing to `instance_summary` SSE for updates. Skip this task if Overview page architecture doesn't accommodate it cleanly — the in-page header from T056 satisfies SC-001/SC-007 alone.
- [x] T058 [US5] Run `python -m py_compile`; restart api+worker; execute [quickstart.md §P5 (steps 5.1–5.2)](quickstart.md) manually; verify all P5 acceptance scenarios in spec.md pass.

**Checkpoint**: full feature set in. All five user stories independently functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: gates, doc reconciliation, and the final "Done and verified" sign-off.

- [x] T059 [P] Run `python scripts/pipeline_fitness.py` — assert all checks pass, especially `api_route_contract` (every fetch URL the new templates emit corresponds to a registered FastAPI route) and `redactor_coverage` (the new `/logs` and `/audit` endpoints route content through `audit_service.redact()`). If `api_route_contract` doesn't auto-discover the new template URLs, extend the check via `scripts/fitness/check_admin_instances_endpoints.py` per [plan.md §"Project Structure"](plan.md).
- [x] T060 [P] Reconcile spec 001 Phase 11 in [specs/001-per-chat-instances/tasks.md](specs/001-per-chat-instances/tasks.md): mark T107–T112 as **subsumed by spec 003** with a one-line cross-reference; T113–T116 (Access UI tasks unrelated to instances) remain owned by spec 001.
- [x] T061 [P] Update CLAUDE.md "Per-chat instance mode — quick reference" section ([CLAUDE.md:266-329](CLAUDE.md#L266-L329)) with one paragraph pointing to the new `/settings/instances` admin section + audit query path. Keep edit minimal — the SPECKIT block already points at spec 003's plan.
- [x] T062 Extend the e2e-pipeline skill (or document as follow-up): add an `admin-control-plane` phase that drives `/settings/instances` via Playwright MCP, force-terminates, and verifies the SSE update lands in the second tab. If extending now is out of scope, file a follow-up issue and link it in this task's PR description.
- [x] T063 [P] Lint clean: run `ruff check` (or project's existing linter) on all new files (`src/openclow/api/routes/admin_instances.py`, `src/openclow/api/schemas/admin_instances.py`, `src/openclow/api/serializers/admin_instance.py`, `tests/contract/test_admin_*.py`, `tests/integration/test_admin_*.py`, `tests/unit/test_admin_*.py`).
- [x] T064 Execute the full [quickstart.md §"Sign-off checklist"](quickstart.md). Tick every item. PR description states **"Done and verified"** per Constitution Principle VII, with the verification commands inlined.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately.
- **Foundational (Phase 2)**: depends on Setup. **Blocks all user stories.** Internal parallelism: T002–T009 are all [P] (different files). T010 depends on T005. T011 depends on T007. T012 depends on T005 + T010.
- **User Story 1 (Phase 3)**: depends on Phase 2. Independent of US2–US5.
- **User Story 2 (Phase 4)**: depends on Phase 2. **Soft dependency** on US1 page route (the detail-view link is reachable from the list view) but the `/api/admin/instances/{slug}` JSON endpoint is independently usable. Can land in parallel with US1 after Phase 2.
- **User Story 3 (Phase 5)**: depends on Phase 2. **Soft dependency** on US2 detail view template (US3 adds buttons to it). The action endpoints alone are independently usable. Can develop in parallel with US2 if T034's template skeleton is created early.
- **User Story 4 (Phase 6)**: depends on Phase 2 + US1's list-view template (T023). Bulk endpoint and bulk UI both build on US1.
- **User Story 5 (Phase 7)**: depends on Phase 2 + US1's list-view template (T023). The summary endpoint already lands in US1 (T018); US5 adds SSE debouncing + the Recent Failures strip.
- **Polish (Phase 8)**: depends on whichever user stories are being shipped in this round.

### Within Each User Story

- Tests written first; verify they FAIL before implementation lands. (Constitution Principle VII: no half-features → tests are part of the slice, not a follow-up.)
- Schema/Pydantic shapes (in [src/openclow/api/schemas/admin_instances.py](src/openclow/api/schemas/admin_instances.py) — already laid down in T006) → handler implementation → template wiring.
- Service-layer changes (`InstanceService.extend_expiry`, etc.) before the handlers that call them.
- Manual quickstart-section run is the last task of every user story phase.

### Parallel Opportunities

- **Phase 2**: T002, T003, T004, T005, T006, T007, T008, T009 all [P] — different files, no interdependencies. T011 + T012 [P] after their dependencies clear.
- **Phase 3 (US1)**: T013, T014, T015, T016 all [P] — independent test files. T017 + T018 share the same router file (sequential in that file). T020 + T021 different files (in parallel). T023 (template) independent of T017–T021 once T006 + T007 done.
- **Phase 4 (US2)**: T025, T026, T027, T028 all [P] (independent test files). T029, T030, T031 share the router file (sequential). T032 + T034 [P] (different files).
- **Phase 5 (US3)**: T036, T037, T038, T039 all [P]. T040 + T041 share `instance_service.py` (sequential). T042, T043, T044 share router (sequential). T045 (template) [P] with the others once endpoints exist.
- **Phase 6 (US4)**: T047 + T048 [P]. T049 (router), T050 + T051 (template) sequential within their files.
- **Phase 7 (US5)**: T053 + T054 [P]. T055 (service), T056 + T057 (templates) — all in different files, all [P].
- **Phase 8**: T059, T060, T061, T063 all [P]. T062, T064 sequential.

### User Story Independence

The five stories are scoped so that any subset 1..N can ship as an MVP increment. After Phase 2:
- **Ship US1 alone** = MVP. The kill-switch and visibility are in production; admin can operate 80% of incidents.
- **Add US2** = full diagnostic capability without leaving the dashboard.
- **Add US3** = full single-instance recovery.
- **Add US4** = scale tier (operate at 50–500 instances).
- **Add US5** = situational awareness widget.

---

## Parallel Example: User Story 1

```bash
# All US1 tests can be authored in parallel (different files):
Task T013: tests/contract/test_admin_instances_list.py
Task T014: tests/contract/test_admin_instances_terminate.py
Task T015: tests/integration/test_admin_force_terminate_flow.py
Task T016: tests/integration/test_admin_force_terminate_idempotency.py

# After tests fail, implement endpoints + service emits + template in parallel:
Task T017+T018+T019: src/openclow/api/routes/admin_instances.py     (sequential — same file)
Task T020:           src/openclow/services/instance_service.py
Task T021:           src/openclow/worker/tasks/instance_tasks.py    (parallel with T020)
Task T023:           src/openclow/api/templates/settings/instances.html   (parallel with T017–T021)
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1: T001 (single verification step).
2. Phase 2: T002–T012 (foundational, parallelize aggressively).
3. Phase 3: T013–T024 (US1 — tests first, then handlers, then template, then quickstart §P1).
4. **STOP and VALIDATE**: run [quickstart.md §P1](quickstart.md) end-to-end + non-admin smoke test + idempotency check.
5. Ship as MVP. Admin can now see and kill any instance from the dashboard.

### Incremental Delivery (recommended cadence)

1. Setup + Foundational → foundation ready.
2. **+ US1** → Test, deploy. **MVP shipped.** (T001–T024)
3. **+ US2** → Test, deploy. Diagnostic tier in production. (T025–T035)
4. **+ US3** → Test, deploy. Recovery tier in production. (T036–T046)
5. **+ US4** → Test, deploy. Scale tier in production. (T047–T052)
6. **+ US5** → Test, deploy. Situational-awareness in production. (T053–T058)
7. **Polish** → Fitness gate, doc reconciliation, sign-off. (T059–T064)

### Parallel Team Strategy

After Phase 2 lands:
- Developer A: US1 (T013–T024).
- Developer B: US2 (T025–T035) — soft-coupled via T034 template stub.
- Developer C: US3 (T036–T046) — picks up after T034 lands.
- Developer D: US4 + US5 (T047–T058) — picks up after T023 lands.

The Pydantic schemas and serializer (T006, T007) are the load-bearing shared artifacts; once they land in Phase 2, developers can compose against them without coordination.

---

## Notes

- **[P] tasks** = different files, no dependencies on incomplete tasks.
- **[Story] label** maps each task to a single user story for traceability and lets us PR per slice.
- **Constitution gates**: every PR runs `python -m py_compile` (Development Workflow gate); the final PR runs `pipeline_fitness.py` and the quickstart sign-off (T059, T064).
- **Same-PR principle**: per Principle VII, the `?slug=` SSE filter (T004) MUST land in the same PR as the detail-view template (T034) — they are co-dependent. Either land together in one PR or land T004 first as foundational.
- **Verify tests fail before implementing**: standard TDD discipline; commit failing tests, watch CI red, then commit the implementation that turns it green.
- **Commit per task or per logical group**: small focused commits per the project's existing pattern.
- **Stop at any checkpoint** to validate the slice; the next slice can pick up from the same foundational base.
- **Avoid**: vague tasks ("add admin features"), same-file parallelism (sequential within a file), cross-story dependencies that break independence.
