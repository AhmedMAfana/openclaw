# Phase 1: Data Model

**Date**: 2026-04-23
**Plan**: [plan.md](plan.md)
**Research**: [research.md](research.md)

Two new tables, two column extensions, and one enum addition. All migrations live in a single Alembic revision (`alembic/versions/011_instance_tables.py`) to keep the deploy atomic.

Identifier convention for all new columns: snake_case, per SQLAlchemy 2.x async conventions already in use across [src/taghdev/models/](../../src/taghdev/models/).

---

## 1. New entity: `Instance`

Represents one dedicated development environment bound to exactly one chat.

### 1.1 Fields

| Field | Type | Nullable | Default | Notes |
|-------|------|----------|---------|-------|
| `id` | UUID | No | `gen_random_uuid()` | Primary key. |
| `slug` | text | No | — | Stable short ID, `inst-<14 hex>` (56-bit entropy per FR-018a). Unique. 19 chars total, inside the 20-char DNS-label cap. Generated via `secrets.token_hex(7)` at row creation; never derived from any other identifier. |
| `chat_session_id` | bigint | No | — | FK → `web_chat_sessions.id`. Partial-unique on active statuses (see §1.4). |
| `project_id` | bigint | No | — | FK → `projects.id`. |
| `status` | text (enum) | No | `'provisioning'` | One of: `provisioning`, `running`, `idle`, `terminating`, `destroyed`, `failed`. Enforced by CHECK. |
| `compose_project` | text | No | — | `tagh-inst-<slug>`. Duplicated from slug for operator convenience; never derived from anything else. |
| `workspace_path` | text | No | — | `/workspaces/inst-<slug>/`. Duplicated from slug; simplifies debugging. |
| `session_branch` | text | No | — | Git branch inside the per-project cache repo that this chat is working on. |
| `image_digest` | text | Yes | — | Set at first successful `compose up`; captures the `app` image's exact digest for reproducibility. |
| `resource_profile` | text | No | `'standard'` | `standard` or `large`. Reserved for future tiering. |
| `heartbeat_secret` | text | No | — | Base64-encoded 32-byte random secret; used to authenticate `projctl heartbeat` (see [research.md §8](research.md#8-heartbeat-authentication)). |
| `db_password` | text | No | — | Generated at provision; injected as `DB_PASSWORD` env; destroyed with the instance. |
| `per_user_count_at_provision` | smallint | No | — | Snapshot of the owner's active-instance count at the time of provisioning. Diagnostic only; not authoritative. |
| `created_at` | timestamptz | No | `now()` | Immutable. |
| `started_at` | timestamptz | Yes | — | Set when `status` first reaches `running`. |
| `last_activity_at` | timestamptz | No | `now()` | Bumped by `InstanceService.touch()`. |
| `expires_at` | timestamptz | No | — | Recomputed on every `touch()` as `last_activity_at + idle_ttl`. |
| `grace_notification_at` | timestamptz | Yes | — | Set when the reaper first transitions `running → idle` for this row. Used by reaper to know whether to notify vs terminate. |
| `terminated_at` | timestamptz | Yes | — | Set when `status` reaches `destroyed` or `failed`. |
| `terminated_reason` | text | Yes | — | One of: `idle_24h`, `user_request`, `failed`, `project_deleted`, `chat_deleted`. |
| `failure_code` | text | Yes | — | Closed set when `status='failed'`: `image_build`, `compose_up`, `projctl_up`, `tunnel_provision`, `dns`, `health_check`, `oom`, `storage_full`, `orchestrator_crash`, `unknown`. |
| `failure_message` | text | Yes | — | Redacted (see [research.md §12](research.md#12-test-coverage-gates), redactor gate). |

### 1.2 Constraints

- `UNIQUE (slug)` — DNS / compose / volume naming.
- `UNIQUE (chat_session_id) WHERE status IN ('provisioning', 'running', 'idle', 'terminating')` — partial unique index enforcing **Principle I** at the DB layer. One active instance per chat.
- `CHECK (status IN (...))` — closed enum.
- `CHECK (slug ~ '^inst-[0-9a-f]{14}$')` — format guard; prevents SQL injection into DNS names.
- `FOREIGN KEY (chat_session_id) REFERENCES web_chat_sessions(id) ON DELETE CASCADE` — drives **FR-013b** retention cascade.
- `FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT` — a project with live instances cannot be deleted; operator must terminate instances first.

### 1.3 Indexes

- `idx_instances_status_expires ON (status, expires_at)` — reaper query (see [research.md §11](research.md#11-reaper-activity-source-wiring)).
- `idx_instances_chat ON (chat_session_id)` — chat UI join.
- `idx_instances_project ON (project_id)` — operator listing.

### 1.4 Lifecycle / state transitions

```text
                ┌──────────────┐
                │ provisioning │
                └──────┬───────┘
                       │ compose up OK + projctl up OK + tunnel healthy
                       ▼
       ┌────────────►┌─────────┐
       │ activity    │ running │◄── touch() bumps last_activity_at
       │             └────┬────┘
       │                  │ expires_at ≤ now()
       │                  ▼
       │             ┌─────────┐
       └─────────────│  idle   │  (grace notification sent; containers still up)
                     └────┬────┘
                          │ grace_notification_at + grace_window ≤ now()
                          │ OR user terminates
                          ▼
                     ┌─────────────┐
                     │ terminating │  compose down + tunnel delete + DNS cleanup
                     └─────┬───────┘
                           ▼
                     ┌──────────┐
                     │destroyed │  terminal; row stays for audit; workspace purged
                     └──────────┘

                     ┌────────┐
                     │ failed │◄── any unrecoverable error during provisioning or running
                     └────────┘     (also runs teardown async; no separate terminating step)
```

Invariants (enforced by the partial unique index + service-layer guards):
- Exactly one row per `chat_session_id` with `status ∈ {provisioning, running, idle, terminating}`.
- `terminating` is idempotent — re-running teardown on an already-terminating row is a no-op.
- `destroyed` is terminal. Resume creates a **new** row (new slug, new UUID).

### 1.5 Validation rules

Enforced at the service layer (`InstanceService`):
- `provision(chat_session_id)` rejects when the user is at `per_user_cap` ([research.md §9](research.md#9-per-user-quota-enforcement)).
- `touch(instance_id)` is a no-op if status ∉ `{running, idle}`.
- `terminate(instance_id, reason)` is idempotent; returns existing `terminated_at` if already terminal.

---

## 2. New entity: `InstanceTunnel`

Represents the public networking surface for an instance. Explicit table (not stuffed into `platform_config`) because lifecycle is explicit per [Principle VI](../../.specify/memory/constitution.md) and the audit flagged the worker-local `_active_processes` dict at [tunnel_service.py:29](../../src/taghdev/services/tunnel_service.py#L29) as an orphan risk.

### 2.1 Fields

| Field | Type | Nullable | Default | Notes |
|-------|------|----------|---------|-------|
| `id` | UUID | No | `gen_random_uuid()` | Primary key. |
| `instance_id` | UUID | No | — | FK → `instances.id` ON DELETE CASCADE. |
| `cf_tunnel_id` | text | No | — | Cloudflare named-tunnel UUID from CF API. |
| `cf_tunnel_name` | text | No | — | `tagh-inst-<slug>`. |
| `web_hostname` | text | No | — | `<slug>.dev.<domain>`. |
| `hmr_hostname` | text | No | — | `hmr-<slug>.dev.<domain>`. |
| `ide_hostname` | text | Yes | — | `ide-<slug>.dev.<domain>`. Reserved; may be NULL in v1 (see [research.md §13](research.md#13-open-items-deferred-to-implementation-prs)). |
| `credentials_secret` | text | No | — | Docker-secret name (e.g., `tagh-inst-<slug>-cf`), **not** the credential JSON. Principle IV. |
| `status` | text (enum) | No | `'provisioning'` | One of: `provisioning`, `active`, `rotating`, `destroyed`. |
| `last_health_at` | timestamptz | Yes | — | Last successful `cloudflared --metrics` scrape or Cloudflare API ping. |
| `created_at` | timestamptz | No | `now()` | |
| `destroyed_at` | timestamptz | Yes | — | Set when `status='destroyed'`. |

### 2.2 Constraints

- `UNIQUE (instance_id) WHERE status = 'active'` — **exactly one active tunnel per instance** (Principle VI).
- `UNIQUE (cf_tunnel_name)` — prevents a name collision on retry.
- `CHECK (status IN ('provisioning', 'active', 'rotating', 'destroyed'))`.
- `FOREIGN KEY (instance_id) REFERENCES instances(id) ON DELETE CASCADE`.

### 2.3 Indexes

- `idx_instance_tunnels_instance ON (instance_id)` — join from instances.

### 2.4 Relationship to DNS records

DNS record IDs are **not** stored in the table. Teardown re-queries Cloudflare's `GET /zones/:z/dns_records?content=<tunnel_id>.cfargotunnel.com` to find matching records and deletes them. Rationale: DNS-record state is already authoritative at Cloudflare; duplicating it in Postgres just creates a sync problem.

---

## 3. Extensions to existing tables

### 3.1 `web_chat_sessions`

```sql
ALTER TABLE web_chat_sessions
  ADD COLUMN instance_id UUID NULL REFERENCES instances(id) ON DELETE SET NULL;
```

Back-reference so the chat UI can `JOIN` for status display and the chat handler can resolve the current instance in one query. Not authoritative — `instances.chat_session_id` is the source of truth — but the set-null on delete keeps the chat row valid after a terminated instance is GC'd.

### 3.2 `tasks`

```sql
ALTER TABLE tasks
  ADD COLUMN instance_id UUID NULL REFERENCES instances(id) ON DELETE CASCADE;
```

Per-instance task history. Nullable for legacy-mode (`host`, `docker`) tasks which do not have an instance. New-mode tasks MUST populate it (service-layer guard in `InstanceService`).

### 3.3 `projects`

```sql
ALTER TABLE projects
  ADD CONSTRAINT projects_mode_check
    CHECK (mode IN ('host', 'docker', 'container'));
```

Adds `'container'` as the new default for newly-created projects. Existing rows keep their current value unchanged (backwards-compatibility per **FR-034**).

---

## 4. Relationships overview

```text
User ──◄ WebChatSession ──◄ Instance ──▶ Project
                │                │
                │ (instance_id)  │ (cascade)
                │                │
                └────────────────┤
                                 │
                                 ├──▶ InstanceTunnel (1 active)
                                 │
                                 └──◄ Task (history)

AuditLog (keyed by instance_slug, not FK) — cleaned by service, not cascade.
```

---

## 5. Migration: `alembic/versions/011_instance_tables.py`

One Alembic revision covering everything above:

1. Create `instances` table with all constraints + indexes.
2. Create `instance_tunnels` table with all constraints + indexes.
3. Add `web_chat_sessions.instance_id` (nullable).
4. Add `tasks.instance_id` (nullable).
5. Modify `projects.mode` CHECK constraint to accept `'container'`.

Downgrade path: drops both new tables, drops both new columns, reverts the mode check. Safe because no legacy-mode code reads `instance_id`.

---

## 6. Not-in-scope data-model changes

Documented here so future reviewers don't try to retrofit them into this migration:

- **No per-tier `User.quota` column.** Per-user cap is global via `platform_config`. Tiering is a future product decision (spec Assumptions).
- **No `Instance.idle_ttl_override` column.** Idle TTL is global; per-project override is a future decision (arch §13 item 5).
- **No DNS record storage table.** Cloudflare is authoritative (§2.4).
- **No cross-chat share table.** Cross-chat instance sharing is a deliberate non-goal for v1 (spec Assumptions + arch §13 item 4).
