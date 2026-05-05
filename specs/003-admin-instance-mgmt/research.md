# Phase 0 Research — Admin Instance Management

**Feature**: 003-admin-instance-mgmt
**Date**: 2026-04-27

The four open questions raised by the spec's checklist all resolved to **reuse existing infrastructure** rather than build new substrates. No `[NEEDS CLARIFICATION]` remains.

---

## Decision 1 — Audit log substrate

**Decision**: Reuse the existing `AuditLog` table and `audit_service.log_action()` helper for every admin-initiated state change (FR-024, FR-025).

**Why**: The substrate already exists end-to-end. The model is `AuditLog` ([src/taghdev/models/audit.py:16-62](src/taghdev/models/audit.py#L16-L62)) with the columns the spec needs (`actor`, `action`, `command`, `instance_slug`, `risk_level`, `metadata_`, `created_at`). The service is [src/taghdev/services/audit_service.py:154-282](src/taghdev/services/audit_service.py#L154-L282) — `log_action()` writes rows with batching, `redact()` masks secrets in command output, `get_recent()` reads them back. A migration ([alembic/versions/013_audit_instance_slug.py](alembic/versions/013_audit_instance_slug.py)) already added the indexed `instance_slug` column, so per-slug audit queries are O(log n).

**Alternatives rejected**:
- *New `admin_actions` table*: extra schema for no marginal benefit; the existing table's columns cover every field the spec mandates.
- *Append-only JSONL file*: would break the "audit readable from detail view" requirement (FR-025) since we'd need a new query layer.

**Implication for plan**: The detail view's "Recent Audit" tab calls `audit_service.get_recent(instance_slug=slug, limit=N)`. No new model, no new migration for audit. The only new schema work is adding `admin_forced` to the `TerminatedReason` enum (already enumerated in spec 001 Phase 11 as task T110, which 003 absorbs).

---

## Decision 2 — Live-update mechanism

**Decision**: Use Server-Sent Events (SSE) over the existing `/api/activity/stream` endpoint to push instance state-change events to the admin UI; consumers subscribe via `EventSource` in the page templates.

**Why**: The pattern is already in production. The endpoint is [src/taghdev/api/routes/activity.py:48-83](src/taghdev/api/routes/activity.py#L48-L83) returning `StreamingResponse` with `text/event-stream`, and [src/taghdev/api/templates/settings/chat.html](src/taghdev/api/templates/settings/chat.html) shows the consumer pattern (`new EventSource('/api/activity/stream?type=...')`). The Projects page uses HTMX delete only — there is **no polling pattern in any settings page today**, so introducing polling would be a fresh divergence rather than alignment.

**Alternatives rejected**:
- *HTMX `hx-trigger="every 5s"` polling*: works but adds load and lags ≤5s on every transition. Existing SSE plumbing is cheaper and freshness-bounded by emit time, comfortably under the spec's 10s budget (FR-006).
- *WebSockets*: bidirectional channel for a unidirectional read-model is overkill; no other admin page uses WS.

**Implication for plan**: Provisioning, terminating, and reaper jobs that already mutate Instance status must call `activity_log.log_event(...)` with a typed event payload (e.g. `{"type": "instance_status", "slug": "...", "status": "..."}`). The list view subscribes with `?type=instance_status`; the detail view subscribes with `?type=instance_status&slug=...`. Initial render is server-rendered HTML; SSE only patches deltas.

**Async correctness (Principle IX)**: Existing `/api/activity/stream` already follows the async-generator pattern. New emit sites in `instance_tasks.py` already use structlog over an async path — no new blocking calls.

---

## Decision 3 — Worker log retrieval by slug

**Decision**: The detail view's "Recent worker log lines for this instance" panel queries `activity_log.query(filters={"instance_slug": slug}, last_n=N)` — no new instrumentation needed.

**Why**: Per-slug filtering is already feasible because:
1. Workers bind `slug` as a structlog context var ([src/taghdev/utils/logging.py:12-28](src/taghdev/utils/logging.py#L12-L28) configures `merge_contextvars`), so every log line emitted under a worker job carries `slug` as a structured field — not buried in the message text.
2. `instance_tasks.py` consistently passes `slug=slug` ([src/taghdev/worker/tasks/instance_tasks.py:113,204,212,371,…](src/taghdev/worker/tasks/instance_tasks.py#L113)), so all relevant log lines are tagged.
3. Logs are persisted as JSONL on disk at the `LOG_FILE` path ([src/taghdev/services/activity_log.py:23](src/taghdev/services/activity_log.py#L23)); the `query()` helper at [src/taghdev/services/activity_log.py:241](src/taghdev/services/activity_log.py#L241) supports field-equality filters and `last_n` truncation out of the box.

**Alternatives rejected**:
- *Live `docker logs` of the instance container*: heavier infra, not the worker's view of the lifecycle. Container stdout streaming is explicitly deferred per the spec's Assumptions.
- *New `worker_logs` table indexed by slug*: would duplicate what the JSONL already provides at much higher write cost.

**Implication for plan**: New endpoint `GET /api/admin/instances/<slug>/logs?limit=N` thinly wraps `activity_log.query()`. No write-path changes to workers. If a future audit reveals a hot path that does not bind `slug`, fix it in that file under a Principle IV/VI ticket — out of scope for 003.

**Redaction (Principle IV)**: The JSONL file already contains the unredacted log lines. The new `/logs` endpoint MUST pass results through `audit_service.redact()` before serialization — same module the constitution mandates. This is a one-line wrap, not a new redactor.

---

## Decision 4 — Admin role enforcement

**Decision**: New admin instance endpoints use `Depends(web_user_dep)` to authenticate and call `_require_admin(user)` inline to enforce the admin role — exactly the shape used by every endpoint in `access.py`.

**Why**: `_require_admin` ([src/taghdev/api/routes/access.py:19-22](src/taghdev/api/routes/access.py#L19-L22)) is a 4-line guard that takes a `User` and either returns it or raises `HTTPException(403, "Admin only")`. It is already used in [access.py:44,54,71,97,121,145,184,201](src/taghdev/api/routes/access.py#L44) — the canonical admin-route pattern. `verify_settings_auth` ([src/taghdev/api/auth.py:19-52](src/taghdev/api/auth.py#L19-L52)) checks SETTINGS_API_KEY / settings cookie / Authorization header / admin JWT — it gates *who can reach the settings UI at all*, not *who is an admin*. They are layered, not equivalent: `verify_settings_auth` may pass for a non-admin web-chat session, but `_require_admin` would still reject them.

**Pattern to follow** (drop-in, already in production at access.py:42-44):

```python
@router.get("/admin/instances")
async def list_admin_instances(user: User = Depends(web_user_dep)):
    _require_admin(user)
    return await ...
```

**Alternatives rejected**:
- *Route-level `Depends(_require_admin)` decorator*: would require adapting `_require_admin` to take its `User` via a nested `Depends(web_user_dep)` — more indirection, no benefit.
- *Stack with `verify_settings_auth`*: overlapping guards lead to ambiguous 401-vs-403 semantics. Spec mandates 403 for non-admin access (FR-003) — `_require_admin` already returns exactly that.

**Implication for plan**: All new admin instance endpoints live in a new router file (e.g., `src/taghdev/api/routes/admin_instances.py`) with `web_user_dep` + `_require_admin` on every handler. No new auth code is written; no settings-cookie logic is touched.

---

## Cross-cutting confirmations

- **Idempotency (Principle VI)**: `InstanceService.terminate()` is already idempotent on `terminating`/`destroyed` ([src/taghdev/services/instance_service.py:267-736](src/taghdev/services/instance_service.py)). Force Terminate, Bulk Force Terminate, Reprovision all delegate to it — no duplicate teardowns even under concurrent admin clicks.
- **No agent surface (Principle III)**: New admin endpoints carry instance identifiers in the path (`/api/admin/instances/<slug>/...`). This does **not** violate Principle III, which constrains MCP tool argument schemas exposed to coding agents — not authenticated HTTP routes used by humans.
- **Async correctness (Principle IX)**: Every new endpoint is `async def`; every DB call uses async SQLAlchemy; every `audit_service.log_action()` already returns awaitably; no new external HTTP calls (Cloudflare/GitHub) — those happen inside ARQ jobs, which the new endpoints only enqueue.
- **No new dependencies**: Reuses FastAPI, SQLAlchemy[asyncio], Jinja2, ARQ, Redis, structlog, and the existing in-template HTMX + EventSource. Constitution's "no new dep without justification" rule is satisfied vacuously.

---

## Summary table

| # | Question | Decision | Net new code |
|---|---|---|---|
| 1 | Audit substrate | Reuse `AuditLog` + `audit_service.log_action()` | None (table & service exist) |
| 2 | Live updates | SSE via existing `/api/activity/stream` | New event emit calls in `instance_tasks.py`; new EventSource subscriber in templates |
| 3 | Worker logs by slug | `activity_log.query(filters={"instance_slug": slug})` + `audit_service.redact()` | One thin `GET /api/admin/instances/<slug>/logs` endpoint |
| 4 | Admin role enforcement | `Depends(web_user_dep)` + inline `_require_admin(user)` | None (pattern already in access.py) |

All four decisions converge on the same shape: **003 is a thin presentation + orchestration layer over substrates that already exist**. No new tables (except the enum value addition for `admin_forced`), no new services, no new dependencies.
