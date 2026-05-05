# Implementation Plan: Admin Instance Management

**Branch**: `003-admin-instance-mgmt` | **Date**: 2026-04-27 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/003-admin-instance-mgmt/spec.md`

## Summary

Add a first-class **Instances** section to the admin dashboard that gives operators full lifecycle visibility and control over the per-chat container instances introduced by feature 001. Five prioritized user stories: (P1) see and Force-Terminate active instances; (P2) drill into one instance to diagnose problems; (P3) recover/operate (Reprovision, Rotate Git Token, Extend Expiry); (P4) filter, search, and bulk operations capped at 50; (P5) health overview widget.

The technical approach (resolved in [research.md](research.md)) is **thin presentation + orchestration over substrates that already exist**:
- Read/write through existing `InstanceService` methods (idempotent state machine, Principle VI).
- Audit via existing `AuditLog` table — already has indexed `instance_slug` (alembic 013).
- Live updates via existing SSE endpoint `/api/activity/stream` (no polling pattern in settings today).
- Worker logs via existing `activity_log.query()` over JSONL (workers already bind `slug` as structlog context).
- Authorization via existing `_require_admin(user)` guard.

**Net new code**: one new router file (`api/routes/admin_instances.py`), two server-rendered templates (`settings/instances.html`, `settings/instance_detail.html`), one sidebar entry (`templates/base.html`), one alembic migration (014, adds `admin_forced` to `TerminatedReason` enum), event-emit calls at existing Instance state-change sites, and a `?slug=` filter on the existing `/api/activity/stream` generator. No new tables, no new dependencies.

This plan **subsumes Phase 11 of spec 001 (T107–T116)** — that phase enumerated only a minimal "Force Terminate inside `AccessPanel`" tab. Tasks T107–T112 are absorbed; T113–T116 (Access UI items unrelated to instances) remain as part of spec 001.

## Technical Context

**Language/Version**: Python 3.12 async (orchestrator, API, workers — Constitution Architecture Constraint). Server-rendered admin templates use Jinja2 + Tailwind CSS + HTMX + vanilla `EventSource` JS (continuity with existing `settings/*.html` pages — see [src/taghdev/api/templates/settings/projects.html](src/taghdev/api/templates/settings/projects.html), [src/taghdev/api/templates/settings/chat.html](src/taghdev/api/templates/settings/chat.html)).
**Primary Dependencies**: FastAPI, SQLAlchemy[asyncio], Jinja2, ARQ, Redis, structlog. All already in the dependency set; no new deps.
**Storage**: Postgres (existing `instances`, `chat_sessions`, `projects`, `users`, `audit_log` tables); Redis (read-only consumption of existing `taghdev:instance_upstream:<slug>:<cap>` keys); JSONL on disk (read-only consumption via `activity_log.query()`).
**Testing**: pytest (existing test suite — `tests/contract/`, `tests/integration/`, `tests/unit/`). No new test framework. New tests added per the conventions of feature 001.
**Target Platform**: Linux server (Docker compose stack); modern web browsers (Chrome/Firefox/Safari current) for the admin UI.
**Project Type**: Web service (FastAPI backend + server-rendered admin pages; no SPA).
**Performance Goals**:
- List view returns within 500ms for ≤500 active instances (SC-006).
- Status freshness ≤10s end-to-end via SSE (FR-006, SC-004).
- Force Terminate teardown completes ≤60s (SC-002).
- Rotate Git Token returns ≤10s (FR-018).
- Bulk terminate handles 50 selections without UI lag.
**Constraints**:
- MUST reuse existing `InstanceService` methods — no parallel state machine (Principle VI).
- MUST NOT expose `/internal/instances/*` endpoints to the admin UI (FR-004; data-model.md §1.1 redaction policy).
- MUST NOT return `heartbeat_secret` or `db_password` in any admin response (Principle IV).
- All admin responses & log lines pass through `audit_service.redact()` before serialization (Principle IV).
- All new endpoints `async def` with no blocking I/O; no new external HTTP calls (Principle IX).
**Scale/Scope**:
- ~500 active instances at peak (SC-006).
- ~5 admin users.
- 11 new HTTP endpoints (10 JSON + 2 page routes — see [contracts/admin-instances-api.md](contracts/admin-instances-api.md)).
- 4 new SSE event types (`instance_status`, `instance_action`, `instance_upstream`, `instance_summary` — see [contracts/sse-events.md](contracts/sse-events.md)).
- 2 new templates, 1 sidebar edit, 1 migration, 1 router file.

No NEEDS CLARIFICATION remain — all four open questions from the spec's checklist resolved in research.md.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.1.0 — nine principles. This feature is a control-plane UI on top of substrates that already enforce the principles, so most checks are PASS-by-reuse rather than PASS-by-new-design.

| # | Principle | Verdict | Evidence |
|---|---|---|---|
| I | Per-Chat Instance Isolation | **PASS** | No new shared state across chats. All admin actions delegate to `InstanceService`, which already enforces the partial-unique `chat_session_id` constraint (data-model.md §6, Reprovision invariant). |
| II | Deterministic Execution Over LLM Drift | **PASS by N/A** | Pure CRUD/orchestration — no LLM in the admin code path. No new agentic surface. |
| III | No Ambient Authority for Agents | **PASS** | Admin endpoints are HTTP routes for *humans*, not MCP tools for *coding agents*. Path parameter `<slug>` is allowed under this principle (the principle restricts MCP tool argument schemas). No new MCP tool added. |
| IV | Credential Scoping & Log Redaction | **PASS** (binding) | Detail-view logs and audit `command` output pass through existing `audit_service.redact()` (data-model.md §4; quickstart.md sign-off checklist). `heartbeat_secret`/`db_password` always omitted from responses. Unit-test guard added per data-model.md §4. |
| V | Egress-Only Network Surface | **PASS by N/A** | No new instance-side service; admin UI is on the existing single `api` deployment. No new `ports:` entries in any compose template. |
| VI | Durable State, Idempotent Lifecycle | **PASS** (binding) | All state mutations route through existing idempotent `InstanceService` methods. Force Terminate on `terminating`/`destroyed` is a no-op (FR-014, contracts §6 "blocked: true" envelope). Bulk path enqueues N independent idempotent calls. |
| VII | Verified Work, No Half-Features | **PASS** (binding) | Each User Story is an Independent Test slice. The `?slug=` filter on `/api/activity/stream` (contracts/sse-events.md §"Backend filter") MUST land in the same PR as the detail-view template that depends on it. Quickstart.md provides the end-to-end verification path. |
| VIII | Root-Cause Fixes Over Bypasses | **PASS by N/A** | New-feature work; no existing checks bypassed. |
| IX | Async-Python Correctness | **PASS** | All new endpoints `async def` with `await` over async SQLAlchemy. No new `httpx.AsyncClient` instances (no external HTTP). No `time.sleep` / `requests.get` introduced. SSE generator already follows the async-generator pattern. |

**Gate result: PASS — no Complexity Tracking entries required.**

Re-check after Phase 1 design: still PASS. The contracts and data-model added during Phase 1 do not introduce any constraint violations; they pin the binding obligations (redaction at serialization, idempotent delegation, no internal-route exposure) into the artifacts so reviewers can mechanically verify.

## Project Structure

### Documentation (this feature)

```text
specs/003-admin-instance-mgmt/
├── plan.md              # This file
├── research.md          # Phase 0 — four-decision resolution
├── data-model.md        # Phase 1 — entity reuse + InstanceListRow/Detail/etc.
├── quickstart.md        # Phase 1 — end-to-end walkthrough per US
├── contracts/
│   ├── admin-instances-api.md   # 11 HTTP endpoints + page routes + auth
│   └── sse-events.md            # 4 event types + emit sites + subscriber pattern
├── checklists/
│   └── requirements.md  # From /speckit.specify
├── spec.md              # The spec
└── tasks.md             # Phase 2 output (/speckit.tasks — not created yet)
```

### Source Code (repository root)

```text
src/taghdev/
├── api/
│   ├── routes/
│   │   ├── admin_instances.py         # NEW — 11 admin endpoints (JSON) + 2 page routes
│   │   ├── activity.py                # MODIFIED — add `?slug=` filter to existing SSE generator
│   │   ├── access.py                  # UNCHANGED — _require_admin reused as-is
│   │   └── instances.py               # UNCHANGED — internal/* routes untouched
│   ├── templates/
│   │   ├── base.html                  # MODIFIED — sidebar gets <Instances> entry between Projects and Users
│   │   └── settings/
│   │       ├── instances.html         # NEW — list view (header counts, filters, table, bulk actions)
│   │       └── instance_detail.html   # NEW — detail view (timeline, tunnel, logs, audit, actions)
│   └── main.py                        # MODIFIED — include admin_instances router
├── services/
│   ├── instance_service.py            # MODIFIED — emit instance_status SSE event after each commit;
│   │                                  #   add rotate_github_token_now() if not present;
│   │                                  #   add extend_expiry() (single-row UPDATE)
│   ├── audit_service.py               # UNCHANGED — log_action() and redact() reused as-is
│   └── activity_log.py                # UNCHANGED — query() and log_event() reused as-is
├── worker/
│   └── tasks/
│       └── instance_tasks.py          # MODIFIED — emit SSE events at phase boundaries
│                                      #   (provisioning→running, →failed, →destroyed, upstream change)
├── models/
│   └── instance.py                    # MODIFIED — add `admin_forced` to TerminatedReason enum
└── alembic/
    └── versions/
        └── 014_admin_forced_terminated_reason.py   # NEW — single CHECK-constraint amendment

tests/
├── contract/
│   └── test_admin_instances_api.py    # NEW — assert all endpoints in contracts/admin-instances-api.md
├── integration/
│   ├── test_admin_force_terminate_flow.py     # NEW — US1 end-to-end against in-memory service
│   ├── test_admin_detail_view.py              # NEW — US2 timeline + logs + audit aggregation
│   ├── test_admin_recovery_actions.py         # NEW — US3 reprovision, rotate, extend
│   └── test_admin_bulk_terminate.py           # NEW — US4 bulk path + 50-cap rejection
└── unit/
    ├── test_admin_serialization_redaction.py  # NEW — assert heartbeat_secret/db_password never serialized
    └── test_admin_role_guard.py               # NEW — non-admin gets 403 on every endpoint

scripts/
└── fitness/
    └── check_admin_instances_endpoints.py     # NEW — extend existing api_route_contract check to cover
                                               #   the new template fetch URLs (no new fitness check file
                                               #   strictly required if api_route_contract already auto-discovers
                                               #   templates; verified during /speckit.tasks)
```

**Structure Decision**: Single-project layout — admin work fits inside the existing `src/taghdev/api/` (router + templates) and `src/taghdev/services/` (existing InstanceService extensions). No new top-level project, no new package, no separate frontend repo. This matches the constitution's "single-tenant control plane" architecture constraint and the audit's keep-as-is verdict on the FastAPI + ARQ + Postgres stack.

The new `api/routes/admin_instances.py` router is the only new module file; everything else is either a template (UI), a single-line enum addition (model), or method additions on existing services.

## Complexity Tracking

> No constitution violations to justify. Table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| *(none)* | — | — |
