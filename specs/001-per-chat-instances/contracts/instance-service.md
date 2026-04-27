# Contract: `InstanceService`

**Audience**: callers within the orchestrator — chat handlers in [src/openclow/worker/tasks/chat_task.py](../../../src/openclow/worker/tasks/chat_task.py), the inactivity reaper, and the FastAPI routes that back the admin UI.

**Location**: [src/openclow/services/instance_service.py](../../../src/openclow/services/instance_service.py) (new file).

**Invariants enforced** (every public method):
- **Principle I** — one chat binds to exactly one active instance at a time.
- **Principle VI** — every call is idempotent; partial-success state is forward-completable.
- **Principle IX** — every call is `async`; every external I/O carries an explicit timeout.

---

## Method surface

Types shown are Python 3.12 annotations. Actual signatures go in the implementation file.

### `async provision(chat_session_id: int) -> Instance`

Begin provisioning a fresh instance for a chat that has none. Idempotent — if a row already exists in `provisioning` / `running` / `idle`, it is returned unchanged.

- **Pre-conditions**:
  - `web_chat_sessions.id == chat_session_id` exists.
  - Its project has `mode='container'`.
  - The owning user is below `per_user_cap` (see [research.md §9](../research.md#9-per-user-quota-enforcement)). Otherwise raises `PerUserCapExceeded`.
  - Platform is below host capacity. Otherwise raises `PlatformAtCapacity`.
- **Side effects** (in order; each checkpointable):
  1. `INSERT instances` row with `status='provisioning'` (held under Redis lock `openclow:user:<user_id>:provision`).
  2. Enqueue ARQ job `provision_instance(instance_id)`. The job itself is responsible for compose render, tunnel provision, compose up, projctl up, and status transition to `running`.
  3. Return the row immediately — callers observe lifecycle via `get_or_resume` or UI polling.
- **Errors**:
  - `PerUserCapExceeded(active_chat_ids: list[int])` — FR-030a.
  - `PlatformAtCapacity()` — FR-030.
  - `ChatNotFound(chat_session_id)` — defensive; caller is expected to have validated.
  - `ProjectNotContainerMode(project_id)` — caller routed wrongly; a bug.

### `async get_or_resume(chat_session_id: int) -> Instance`

Return the chat's current active instance, or provision a new one if the previous was destroyed. This is the primary entry point from `chat_task.py`.

- **Behaviour**:
  - Active row present (`provisioning` / `running` / `idle`) → return as-is. If `idle`, also call `touch()` and return (the chat is coming back; cancel any grace-window teardown).
  - Only `destroyed` / `failed` rows exist → call `provision(chat_session_id)`.
- **Errors**: same as `provision`.

### `async touch(instance_id: UUID) -> None`

Bump activity. Indexed update; called on every inbound chat message and on every `projctl heartbeat`.

- **Pre-conditions**: instance exists.
- **Side effects**:
  - If status ∈ {`running`, `idle`}: UPDATE `last_activity_at = now()`, `expires_at = now() + idle_ttl`, clear `grace_notification_at`. If status was `idle`, transition back to `running` and clear the chat's grace banner.
  - Otherwise: no-op.
- **Errors**: `InstanceNotFound(instance_id)`.

### `async terminate(instance_id: UUID, *, reason: str) -> None`

User-initiated or system-initiated teardown. Immediate, no grace window.

- **Pre-conditions**: `reason ∈ {'user_request', 'idle_24h', 'failed', 'project_deleted', 'chat_deleted'}`.
- **Side effects**:
  1. Transition `status → terminating`, set `terminated_reason`.
  2. Enqueue ARQ job `teardown_instance(instance_id)` (itself idempotent: see [research.md §4](../research.md#4-idempotency-keys-for-lifecycle-operations)).
  3. On job completion: transition `status → destroyed`, set `terminated_at`.
- **Idempotent**: calling `terminate` on an already-`terminating` / `destroyed` row is a no-op and returns the existing `terminated_at`.
- **Errors**: `InstanceNotFound`.

### `async list_active(*, user_id: int | None = None) -> list[Instance]`

Return active instances (statuses ∈ `{provisioning, running, idle, terminating}`). Used by the per-user-cap error UI (FR-030b) and operator dashboards.

- `user_id=None` → all users (admin surface; permission-checked by caller).
- `user_id=<int>` → only that user's instances.

### `async record_heartbeat(slug: str, signals: HeartbeatSignals) -> HeartbeatAck`

Called by the FastAPI heartbeat route (see [heartbeat-api.md](heartbeat-api.md)). Separate from `touch()` because it also records the `signals` for operator diagnostics and rate-limits per slug.

---

## Events emitted

All go through the existing event bus into `audit_service`:

| Event | When | Payload (redacted) |
|-------|------|--------------------|
| `instance.provisioning` | `provision()` enters DB | `{instance_slug, chat_session_id, user_id, project_id}` |
| `instance.running` | compose+tunnel+projctl OK | `{instance_slug, image_digest, startup_duration_s}` |
| `instance.idle` | reaper transition | `{instance_slug, reason="expired_ttl"}` |
| `instance.grace_notified` | grace warning sent to chat | `{instance_slug, grace_expires_at}` |
| `instance.terminating` | `terminate()` called | `{instance_slug, reason}` |
| `instance.destroyed` | teardown job complete | `{instance_slug, reason, lifetime_s}` |
| `instance.failed` | any failure path | `{instance_slug, failure_code, failure_message}` |
| `instance.upstream_degraded` | FR-027a | `{instance_slug, capability, upstream}` |
| `instance.upstream_recovered` | FR-027b | `{instance_slug, capability, upstream}` |

Every event carries `instance_slug` so post-hoc auditors can assert Principle I invariants.

---

## Errors (public)

Exceptions exported from `instance_service`:

```text
class InstanceServiceError(Exception): ...
class InstanceNotFound(InstanceServiceError): ...
class ChatNotFound(InstanceServiceError): ...
class ProjectNotContainerMode(InstanceServiceError): ...
class PerUserCapExceeded(InstanceServiceError):
    active_chat_ids: list[int]
class PlatformAtCapacity(InstanceServiceError): ...
```

Callers (specifically `chat_task.py`) are responsible for translating these into user-visible chat messages with navigation controls, per FR-024 and FR-030b.

---

## Concurrency rules

- `provision(chat_session_id)` holds the user-scoped Redis lock `openclow:user:<user_id>:provision` only across the cap check + INSERT — not across the compose up (that's in an ARQ job).
- `touch(instance_id)` is lockless — the UPDATE is inherently atomic.
- `terminate(instance_id, ...)` holds the instance-scoped Redis lock `openclow:instance:<slug>` to prevent concurrent terminate vs provision on the same chat.
- The reaper holds `FOR UPDATE SKIP LOCKED` on its query so multiple reaper replicas can coexist (v1 has one, but the design does not preclude more).

---

## What `InstanceService` does NOT do

- Compose rendering → `InstanceComposeRenderer`.
- CF API → `TunnelService`.
- GitHub App tokens → `CredentialsService`.
- Redaction → `audit_service`.
- Direct `docker exec` / `docker compose` calls → `instance_tasks` (ARQ job).

This keeps `InstanceService` as the state-machine owner only; concrete infra calls live elsewhere and are separately testable.
