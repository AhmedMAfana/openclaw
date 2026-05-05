# Phase 1 Data Model — Admin Instance Management

**Feature**: 003-admin-instance-mgmt
**Date**: 2026-04-27

The feature is overwhelmingly **read-only over existing models**. There is exactly one schema mutation (an enum value addition) and zero new tables. Everything else is a view-side composition.

---

## 1. Reused entities (no schema changes)

### 1.1 `Instance` — [src/taghdev/models/instance.py:66-123](src/taghdev/models/instance.py#L66-L123)

Source of truth for everything the admin sees in the list and detail views.

| Field | Used by spec FRs | Notes |
|---|---|---|
| `id` (UUID PK) | FR-017 (Reprovision target), FR-018 (Rotate target), FR-019 (Extend target) | Not shown to admin; routes use `slug`. |
| `slug` (unique, `inst-<14 hex>`) | FR-005, FR-008, FR-015, all action endpoints | Stable display identifier; format guaranteed by Constitution Architecture Constraint. |
| `chat_session_id` (FK → `chat_sessions.id`, nullable when chat deleted) | FR-015 (chat link), FR-016 (graceful "deleted" placeholder), FR-017 (Reprovision blocked when null) | Cascade rules already in place. |
| `project_id` (FK → `projects.id`) | FR-005, FR-015, FR-008 (project filter) | Restrict on delete (project deletion does not cascade to instance row). |
| `status` (enum) | FR-005, FR-008, FR-009, FR-010, FR-011, FR-014, FR-017 | Values: `provisioning`, `running`, `idle`, `terminating`, `destroyed`, `failed`. **No new value added.** |
| `compose_project`, `workspace_path`, `session_branch`, `image_digest`, `resource_profile` | FR-015 (detail view) | Diagnostic context. |
| `heartbeat_secret`, `db_password` | — | NEVER returned to admin UI; redacted at the serialization boundary. |
| `created_at`, `started_at`, `last_activity_at`, `expires_at`, `grace_notification_at`, `terminated_at` | FR-005, FR-019 (Extend writes `expires_at`), FR-011 (stuck-state thresholds) | All `TIMESTAMP WITH TIME ZONE`. |
| `terminated_reason` (enum) | FR-013 | **One enum value added (see §2).** |
| `failure_code`, `failure_message` | FR-015, FR-016 | Diagnostic surfacing. |

**No write paths added against this model from the admin UI** — every state mutation goes through `InstanceService` methods (Principle VI: idempotent lifecycle owned by the service).

### 1.2 `AuditLog` — [src/taghdev/models/audit.py:16-62](src/taghdev/models/audit.py#L16-L62)

Reused for every admin-initiated action (FR-024, FR-025).

| Field | Used by | How |
|---|---|---|
| `actor` (str) | All admin actions | Set to the admin user's identifier (e.g. `web_user:<id>` or login). |
| `action` (str) | All admin actions | One of: `force_terminate`, `bulk_force_terminate`, `reprovision`, `rotate_git_token`, `extend_expiry`. |
| `command` (text) | All admin actions | Human-readable command summary; passes through `audit_service.redact()` before storage. |
| `instance_slug` (str, indexed by alembic 013) | FR-025 (per-instance audit query) | Set to the target slug. For bulk: one row per affected slug. |
| `exit_code` | All admin actions | `0` on success, non-zero on error. |
| `output_summary` | All admin actions | Short outcome string (e.g. "teardown enqueued: job_id=…"). |
| `risk_level` ("normal"/"elevated"/"dangerous") | FR-024 metadata | `dangerous` for terminate/reprovision; `elevated` for rotate/extend. |
| `blocked` (bool) | Edge case — concurrent action rejected | `true` when the action was a no-op due to "already terminating". |
| `metadata_` (JSONB) | All admin actions | `{"reason": "admin_forced", "from_status": "...", "to_status": "...", ...}` |
| `created_at` | FR-025 | Server default. |

**No schema change**; no migration for audit.

### 1.3 `User` — [src/taghdev/models/user.py](src/taghdev/models/user.py)

Used only via the existing `_require_admin(user)` guard. Read fields: `id`, `name`, `is_admin`. The list view's "owning user" column dereferences `Instance → ChatSession → User` (existing relationship chain).

### 1.4 `ChatSession` and `Project`

Read-only joins for list-view display fields (owner name, project name) and the detail view's chat/project links. No new columns.

---

## 2. Schema mutation — single enum value addition

### Migration `014_admin_forced_terminated_reason.py`

Add `'admin_forced'` to:
1. The PostgreSQL CHECK constraint `ck_instances_terminated_reason` on `instances.terminated_reason`.
2. The Python `TerminatedReason` enum in [src/taghdev/models/instance.py](src/taghdev/models/instance.py).

**Justification**: FR-013 mandates that admin-initiated terminations are distinguishable from idle-reaper, user-request, project-deleted, chat-deleted, and failure-driven terminations in DB rows and analytics queries. This is the only new persisted vocabulary the feature needs.

**Idempotency / rollback**: standard CHECK-constraint amendment; rollback drops the value (with migration-down also rejecting if any rows already use it — Principle VI: forward-completion only).

**Scope**: This migration was originally enumerated as task T110 in spec 001 Phase 11; 003 absorbs it (see plan.md Phase-11 reconciliation note).

---

## 3. Derived view models (no DB tables)

These are Pydantic response shapes assembled at request time — they do not correspond to database tables.

### 3.1 `InstanceListRow`

Returned by `GET /api/admin/instances`.

```text
{
  slug: str,
  status: "provisioning"|"running"|"idle"|"terminating"|"destroyed"|"failed",
  status_age_seconds: int,           # for FR-011 stuck-state highlight
  user: { id: str, name: str, deleted: bool },
  project: { id: str, name: str, deleted: bool },
  preview_url: str | null,           # null if tunnel not yet up
  created_at: ISO8601,
  last_activity_at: ISO8601 | null,
  expires_at: ISO8601 | null,
  upstream_health: "live" | "degraded" | "unreachable" | null,  # from Redis state
}
```

### 3.2 `InstanceDetail`

Returned by `GET /api/admin/instances/<slug>`. Aggregates:
- All `InstanceListRow` fields plus full Instance row (minus secrets — see §4).
- `transitions: list[StatusTransition]` — derived from existing audit/log substrate; one entry per `provisioning|running|idle|terminating|destroyed|failed` transition with `at: ISO8601` and `note: str`.
- `tunnel: { url, health, last_probe_at, degradation_history: list[DegradationEvent] }` — pulled live from Redis upstream-state at `taghdev:instance_upstream:<slug>:*`.
- `failure: { code: str, message: str } | null` — surfaces `failure_code`+`failure_message` when status is `failed`.
- `chat: { id, deleted: bool, link: str }` — graceful "deleted" placeholder when `chat_session_id` is null.
- `project: { id, name, deleted: bool, link: str }`.
- `available_actions: list[str]` — server-computed allowlist for the UI to render the right buttons (e.g. `["force_terminate"]` for `terminating`, `["force_terminate", "rotate_git_token", "extend_expiry"]` for `running`).

### 3.3 `InstanceLogLine`

Returned by `GET /api/admin/instances/<slug>/logs`.

```text
{
  ts: ISO8601,
  level: "debug"|"info"|"warning"|"error",
  message: str,                     # passed through audit_service.redact()
  context: dict,                    # remaining structlog fields (also redacted)
}
```

Source: `activity_log.query(filters={"instance_slug": slug}, last_n=N)`.

### 3.4 `InstanceAuditEntry`

Returned by `GET /api/admin/instances/<slug>/audit`.

Direct projection of `AuditLog` rows where `instance_slug == slug`, sorted descending by `created_at`. Fields: `actor`, `action`, `command`, `exit_code`, `output_summary`, `risk_level`, `blocked`, `metadata`, `created_at`. Serves FR-025.

### 3.5 `StatusCounts`

Returned by `GET /api/admin/instances/summary`. Used by the list-view header and US5 widget.

```text
{
  running: int,
  idle: int,
  provisioning: int,
  terminating: int,
  failed_24h: int,
  total_active: int,
  capacity: { used: int, cap: int | null },
}
```

---

## 4. Field-level redaction policy

Three classes of fields are handled at the serialization boundary, never at the storage boundary:

| Class | Fields | Treatment |
|---|---|---|
| **Always omit** | `Instance.heartbeat_secret`, `Instance.db_password` | Stripped from every admin-facing response. Admin never sees these even with full role. (Principle IV.) |
| **Always redact** | All `InstanceLogLine.message`, all `InstanceAuditEntry.command` | Pass through `audit_service.redact()` (already mandatory per Principle IV). |
| **Conditionally redact** | `Instance.compose_project`, `Instance.workspace_path` | Shown verbatim (these are diagnostic, not secret). |

A unit test asserts the serializer drops `heartbeat_secret` and `db_password` from every admin response shape (regression guard against a future "let me add this field for debugging" refactor).

---

## 5. State transitions (informational; no schema change)

The admin UI observes — and constrains — transitions on the existing state machine. No new states; the transitions remain owned by `InstanceService` and the ARQ jobs.

| From | Admin action | To | Mechanism |
|---|---|---|---|
| `running` / `idle` | Force Terminate | `terminating` → `destroyed` | `InstanceService.terminate(reason='admin_forced')` → `teardown_instance` job. |
| `provisioning` | Force Terminate | `terminating` → `destroyed` | Same; teardown is idempotent over partial-provision state. |
| `terminating` / `destroyed` | Force Terminate | (no change — no-op) | FR-014: action returns "already ended", `AuditLog.blocked=true`. |
| `failed` (with chat) | Reprovision | `provisioning` → `running` (or `failed` again) | `InstanceService.provision(chat_id=instance.chat_session_id)` enqueues `provision_instance`. |
| `destroyed` (with chat) | Reprovision | same as above | same |
| `failed` / `destroyed` (chat deleted) | Reprovision | (blocked by UI) | Edge case — UI removes the button; backend returns 422 if forced. |
| `running` | Rotate Git Token | `running` (no status change) | `InstanceService.rotate_github_token_now(instance_id)` (existing job). |
| `running` / `idle` | Extend Expiry | same | Direct write: `expires_at += delta`; the inactivity reaper re-reads on its next tick. |

All transitions maintain Principle VI: idempotent and resumable. The only newly-introduced *reason* is `admin_forced` (§2); the only newly-introduced *write* outside the service layer is the `expires_at` update for Extend Expiry, which is wrapped in a single-row UPDATE under transaction.

---

## 6. Concurrency & invariants

- **Two-admin race on the same instance**: serialized by row-level lock in `InstanceService.terminate()` (existing); the second call observes `terminating` and returns the no-op path. New `AuditLog` row records `blocked=true`. (Edge Cases §1; FR-028.)
- **Bulk action atomicity**: the bulk endpoint enqueues N independent `terminate()` calls — not a single transaction — to avoid one slow teardown blocking the rest. Each enqueue is its own audit row. (FR-022 cap of 50 keeps tail latency bounded.)
- **Extend Expiry vs. inactivity reaper**: the reaper reads `expires_at` on each 5-min tick; the admin's UPDATE wins if it lands before the next tick, which is the desired semantics. No locking needed (last-write-wins on a monotonic-ish field).
- **Reprovision creates a new Instance row** with a fresh slug, re-binding the same chat to it; the old `failed`/`destroyed` row remains for audit. The chat's `chat_session_id` invariant ("at most one active instance") is maintained because the old instance is non-active. (Principle I.)

---

## 7. Indexes already present

No new indexes required. Existing coverage:
- `instances.slug` UNIQUE — used by every `<slug>`-keyed admin endpoint.
- `instances.chat_session_id` partial UNIQUE on active statuses — Principle I enforcement.
- `instances.status` — used by status filter and counts query.
- `audit_log.instance_slug` — alembic 013, used by `GET /api/admin/instances/<slug>/audit`.
- `audit_log.created_at` — used for descending order.

---

## 8. Out of model

Explicitly NOT added by 003:
- New `admin_action_log` table (rejected — `AuditLog` covers it).
- Any modification to `ChatSession`, `Project`, `User`, `Tunnel`, `Task` schemas.
- New Redis keys (the existing `taghdev:instance_upstream:<slug>:<cap>` keys are read-only-consumed by the detail view).
- New ARQ queues or job registrations (admin actions enqueue *existing* jobs: `teardown_instance`, `provision_instance`, `rotate_github_token`).

This keeps the feature's blast radius minimal and aligned with Principle VI (state owned by the service layer, not the UI).
