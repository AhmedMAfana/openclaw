# Contract — Admin Instances HTTP API

**Feature**: 003-admin-instance-mgmt
**Surface**: HTTP/JSON, served by FastAPI
**Mount**: All routes under `/api/admin/instances`
**Auth**: Every endpoint depends on `web_user_dep` and calls `_require_admin(user)` inline. Non-admin → `403 {"detail": "Admin only"}`. Unauthenticated → `401`.

This contract is the source of truth for the `static_audit::api_route_contract` fitness check.

---

## Common error envelope

All non-2xx responses share this shape:

```json
{ "detail": "<human-readable reason>", "code": "<machine_code>" }
```

Standard codes used in this contract:
- `not_found` — slug does not exist (404).
- `admin_only` — caller is not an admin (403).
- `invalid_status_for_action` — action not allowed in current status (409).
- `bulk_cap_exceeded` — selected count > 50 (422).
- `chat_deleted` — Reprovision attempted on instance whose chat is gone (422).
- `validation_error` — body schema mismatch (400).
- `concurrent_action` — instance already terminating/changing (409, also `blocked=true` in audit).

---

## 1. `GET /api/admin/instances`

**Purpose**: list-view data source (FR-002, FR-005, FR-007, FR-008, FR-009).

**Query parameters** (all optional):

| Param | Type | Default | Notes |
|---|---|---|---|
| `status` | repeated str | active set | Subset of `provisioning,running,idle,terminating,destroyed,failed`. Default = `provisioning,running,idle,terminating` (FR-009). |
| `user_id` | str | — | Owner filter. |
| `project_id` | str | — | Project filter. |
| `q` | str | — | Free-text slug substring (FR-008). Case-insensitive. |
| `sort` | str | `last_activity_at` | One of `slug,status,created_at,last_activity_at,expires_at,user,project`. |
| `dir` | "asc"/"desc" | `desc` | Sort direction. |
| `limit` | int | 100 | Max 500. |
| `offset` | int | 0 | Standard pagination. |

**Response 200**:

```json
{
  "items": [ <InstanceListRow>, ... ],
  "total": 123,
  "summary": <StatusCounts>
}
```

`InstanceListRow` and `StatusCounts` defined in [../data-model.md](../data-model.md) §3.1 and §3.5.

**Errors**: `403`, `400`.

---

## 2. `GET /api/admin/instances/summary`

**Purpose**: standalone counts for the Overview-page widget (US5).

**Response 200**: `StatusCounts` (data-model §3.5).

**Errors**: `403`.

---

## 3. `GET /api/admin/instances/{slug}`

**Purpose**: detail view (FR-015, FR-016).

**Path**: `slug` matches `^inst-[0-9a-f]{14}$` (Constitution architecture constraint).

**Response 200**: `InstanceDetail` (data-model §3.2). Includes `available_actions` so the UI does not duplicate state-machine knowledge client-side.

**Errors**: `403`, `404 not_found`.

---

## 4. `GET /api/admin/instances/{slug}/logs`

**Purpose**: detail view's "recent worker log lines" panel (FR-015).

**Query parameters**:

| Param | Type | Default | Notes |
|---|---|---|---|
| `limit` | int | 50 | Max 500. |
| `level` | str | — | Filter `debug|info|warning|error`. |

**Response 200**:

```json
{ "items": [ <InstanceLogLine>, ... ] }
```

`InstanceLogLine` defined in data-model §3.3. Every `message` and every `context` value passes through `audit_service.redact()` before serialization.

**Errors**: `403`, `404 not_found`.

---

## 5. `GET /api/admin/instances/{slug}/audit`

**Purpose**: detail view's "Recent Audit" tab (FR-025).

**Query parameters**:

| Param | Type | Default |
|---|---|---|
| `limit` | int | 100 |

**Response 200**:

```json
{ "items": [ <InstanceAuditEntry>, ... ] }
```

Sorted descending by `created_at`.

**Errors**: `403`, `404 not_found`.

---

## 6. `POST /api/admin/instances/{slug}/terminate`

**Purpose**: row-level Force Terminate (FR-012, FR-013, FR-014).

**Request body**:

```json
{ "confirm": true, "note": "<optional admin-supplied reason>" }
```

`confirm` MUST be literal `true`; absence or `false` returns `400 validation_error` to defeat accidental clicks via curl.

**Response 200** (action enqueued):

```json
{ "slug": "inst-...", "status": "terminating", "audit_id": "..." }
```

**Response 200 — already-ended (no-op, FR-014)**:

```json
{ "slug": "inst-...", "status": "destroyed", "audit_id": "...", "blocked": true, "reason": "already_ended" }
```

The status is the *current* status; `blocked: true` signals the no-op. UI shows a soft toast, not an error.

**Side effects**:
- `Instance.terminated_reason` set to `admin_forced` (§2 of data-model).
- ARQ `teardown_instance` job enqueued (idempotent — Principle VI).
- One `AuditLog` row written with `action="force_terminate"`, `risk_level="dangerous"`.

**Errors**: `403`, `404 not_found`, `400 validation_error`.

---

## 7. `POST /api/admin/instances/bulk-terminate`

**Purpose**: bulk Force Terminate (FR-021, FR-022, FR-023).

**Request body**:

```json
{
  "slugs": ["inst-...", "inst-...", ...],
  "confirm": true
}
```

- `slugs` MUST be 1–50 entries; >50 returns `422 bulk_cap_exceeded`.
- `confirm` MUST be `true`.

**Response 200**:

```json
{
  "results": [
    { "slug": "inst-...", "outcome": "queued", "audit_id": "..." },
    { "slug": "inst-...", "outcome": "already_ended", "audit_id": "...", "blocked": true },
    { "slug": "inst-...", "outcome": "not_found" }
  ]
}
```

Per-slug outcomes; the endpoint never partial-fails — every slug gets a result. Each successful enqueue writes its own `AuditLog` row with `action="bulk_force_terminate"` and `metadata.bulk_size=<N>`.

**Errors**: `403`, `400 validation_error`, `422 bulk_cap_exceeded`.

---

## 8. `POST /api/admin/instances/{slug}/reprovision`

**Purpose**: detail-view Reprovision (FR-017).

**Preconditions**:
- Current `status` ∈ {`failed`, `destroyed`}; else `409 invalid_status_for_action`.
- `chat_session_id` is non-null and chat exists; else `422 chat_deleted`.

**Request body**:

```json
{ "confirm": true }
```

**Response 200**:

```json
{ "old_slug": "inst-...", "new_slug": "inst-...", "new_status": "provisioning", "audit_id": "..." }
```

A fresh `Instance` row is created and bound to the same chat (data-model §6 invariant). One `AuditLog` row with `action="reprovision"`, `risk_level="dangerous"`, `metadata={"old_slug": "...", "new_slug": "..."}`.

**Errors**: `403`, `404`, `409`, `422`.

---

## 9. `POST /api/admin/instances/{slug}/rotate-token`

**Purpose**: detail-view Rotate Git Token (FR-018).

**Preconditions**:
- Current `status == "running"`; else `409 invalid_status_for_action`.

**Request body**: `{}` (no parameters).

**Response 200** (within 10s — FR-018):

```json
{ "slug": "inst-...", "rotated_at": "2026-04-27T12:34:56Z", "audit_id": "..." }
```

**Side effects**: synchronous call to `InstanceService.rotate_github_token_now(instance_id)` (existing). One `AuditLog` row with `action="rotate_git_token"`, `risk_level="elevated"`.

**Errors**: `403`, `404`, `409`. On upstream GitHub failure: `502 {"detail": "...", "code": "rotate_failed"}` with audit `exit_code != 0`.

---

## 10. `POST /api/admin/instances/{slug}/extend-expiry`

**Purpose**: detail-view Extend Expiry (FR-019).

**Preconditions**:
- Current `status` ∈ {`running`, `idle`}; else `409 invalid_status_for_action`.

**Request body**:

```json
{ "extend_hours": 4 }
```

`extend_hours` MUST be one of `1`, `4`, `24`. Other values → `400 validation_error`.

**Response 200**:

```json
{ "slug": "inst-...", "new_expires_at": "2026-04-28T12:34:56Z", "audit_id": "..." }
```

**Side effects**: `Instance.expires_at = max(now, expires_at) + delta` (single-row UPDATE under transaction). One `AuditLog` row with `action="extend_expiry"`, `risk_level="elevated"`, `metadata={"hours": 4, "old_expires_at": "...", "new_expires_at": "..."}`.

**Errors**: `403`, `404`, `409`, `400`.

---

## 11. `GET /api/admin/instances/audit` *(platform-wide audit view, optional in MVP)*

**Purpose**: platform-wide filterable audit (FR-025 alternative path).

**Query parameters**:

| Param | Type | Default |
|---|---|---|
| `actor` | str | — |
| `action` | repeated str | — |
| `since` | ISO8601 | 24h ago |
| `limit` | int | 200 |

**Response 200**: `{ "items": [ <InstanceAuditEntry>, ... ] }`.

**Errors**: `403`.

This endpoint is OPTIONAL for the P1 slice. The per-instance `/audit` endpoint (§5) satisfies FR-025's mandatory case.

---

## Page routes (server-rendered)

These accompany the JSON API for the templates side. They use the same `web_user_dep` + `_require_admin` guard.

| Method | Path | Template |
|---|---|---|
| `GET` | `/settings/instances` | `settings/instances.html` (list) |
| `GET` | `/settings/instances/{slug}` | `settings/instance_detail.html` (detail) |

Sidebar entry added in `templates/base.html` between Projects and Users (FR-001).

---

## Internal routes (NOT touched by 003)

The internal heartbeat / token-rotation / explain endpoints under `/internal/instances/*` ([src/taghdev/api/routes/instances.py:202-630](src/taghdev/api/routes/instances.py#L202-L630)) remain compose-network only. **003 introduces no new routes under `/internal/*` and no admin route reads or proxies `heartbeat_secret`** (FR-004; data-model §4 redaction policy).
