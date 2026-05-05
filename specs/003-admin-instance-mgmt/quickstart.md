# Quickstart — Admin Instance Management

**Feature**: 003-admin-instance-mgmt
**Audience**: developer wiring up the implementation, or reviewer checking the slice end-to-end.

This walkthrough assumes the feature is implemented per [plan.md](plan.md). It exercises every user story (P1 → P5) against a running stack so you can verify the slice without reading every file.

---

## Prerequisites

```bash
# 1. Stack is up (database migrated to head, including migration 014)
docker compose up -d
docker compose exec api alembic upgrade head

# 2. You have at least one admin user. The web-chat session for this user must
#    be loaded; copy the auth cookie from your browser into the curl examples
#    below, or paste the SETTINGS_API_KEY into a header.
export ADMIN_COOKIE='session=<paste your admin web session cookie>'

# 3. You have a container-mode project seeded
python3 scripts/seed_platform_creds.py --update-project test-project=you/laravel-test-app
```

---

## P1 — See and force-terminate an instance (US1)

### 1.1 Provision a real instance

Open the chat frontend, start a new chat under `test-project`, and send any message. Wait until the provision card flips to "live preview" (≈30s). Note the chat URL.

### 1.2 Open the new admin section

Navigate to **`http://localhost:8000/settings/instances`**. You should see:

- A new **Instances** item in the Management group of the sidebar (between Projects and Users) — see [contracts/admin-instances-api.md](contracts/admin-instances-api.md) §"Page routes".
- A header with status counts: `running 1 · idle 0 · provisioning 0 · terminating 0 · failed 24h: 0`.
- A row for your new instance: slug `inst-…`, your user, project name `test-project`, status `running` (green chip), preview URL link, "just now" relative time.

```bash
# Same view via JSON
curl -s -H "Cookie: $ADMIN_COOKIE" http://localhost:8000/api/admin/instances | jq .
```

Expected: 1 item, `summary.running == 1`.

### 1.3 Verify SSE updates

Open a second browser tab on the same `/settings/instances` page; in the first tab, open the chat and send another message. The first tab's row "last activity" updates within 10s without refresh. (If it doesn't, check the EventSource console for `instance_status` events; verify [src/taghdev/api/routes/activity.py](src/taghdev/api/routes/activity.py) accepts `?slug=` as documented in [contracts/sse-events.md](contracts/sse-events.md) §"Backend filter".)

### 1.4 Force terminate

Click **Force Terminate** on the row. Confirm the dialog (it should name the slug + owner). Within 5s the row's status changes to `terminating`; within 60s it transitions to `destroyed` and falls out of the default view (FR-009).

Verify the audit row landed:

```bash
curl -s -H "Cookie: $ADMIN_COOKIE" \
  "http://localhost:8000/api/admin/instances/inst-XXXXXXXXXXXXXX/audit" | jq .
```

Expected: one entry with `action: "force_terminate"`, `risk_level: "dangerous"`, `metadata.reason: "admin_forced"`. (Schema per [data-model.md](data-model.md) §1.2.)

Verify the instance row has `terminated_reason = 'admin_forced'`:

```bash
docker compose exec db psql -U app -d taghdev \
  -c "SELECT slug, status, terminated_reason FROM instances ORDER BY created_at DESC LIMIT 1;"
```

### 1.5 Idempotency check (FR-014)

Click Force Terminate again on the (now `destroyed`) row from the filtered "All statuses" view. The response is the no-op envelope:

```json
{ "slug": "inst-…", "status": "destroyed", "blocked": true, "reason": "already_ended" }
```

A second `AuditLog` row exists with `blocked: true`. **No duplicate teardown job is enqueued** — verify with:

```bash
docker compose logs worker --tail=200 | grep teardown_instance
```

Should show one teardown job, not two.

---

## P2 — Drill into a failed instance (US2)

### 2.1 Trigger a failed provision

Edit a project's compose template to inject a syntax error (e.g., `setup/compose_templates/laravel-vue/compose.yml` — change `image: foo` to `imag: foo` temporarily), then create a new chat and send a message. The instance lands in `failed`.

### 2.2 Open the detail view

From `/settings/instances`, switch the status filter to include `failed`. Click the failed row.

You should see:

- Header: slug, status badge (red `failed`), owning user, project links.
- **Timeline** section with chronological transitions, e.g. `provisioning → failed at 12:34:56` (FR-015).
- **Failure** banner with `failure_code` and `failure_message` rendered prominently (FR-015).
- **Tunnel** section either empty (provision didn't reach tunnel-up) or showing `unreachable`.
- **Recent worker logs** panel listing the last ~50 log lines, every one carrying `slug=inst-…` in its context, every secret already redacted (FR-015 + Principle IV).
- **Recent audit** panel.
- **Available actions**: only `Reprovision` and `Open in Chat` (server-computed `available_actions` per [contracts/admin-instances-api.md](contracts/admin-instances-api.md) §3).

### 2.3 Verify deleted-chat handling (FR-016)

Delete the chat (chat frontend "delete chat" button). Reopen the same instance's detail view. The chat link now reads "deleted" but every other field still renders. Reprovision is no longer offered.

---

## P3 — Recover and operate (US3)

### 3.1 Reprovision

Restore the compose template (undo step 2.1). Open the detail view of a `failed` or `destroyed` instance whose chat still exists. Click **Reprovision** → confirm.

```bash
curl -s -X POST -H "Cookie: $ADMIN_COOKIE" -H "Content-Type: application/json" \
  -d '{"confirm": true}' \
  http://localhost:8000/api/admin/instances/inst-XXXXXXXXXXXXXX/reprovision | jq .
```

Expected response:

```json
{
  "old_slug": "inst-OLDOLDOLDOLDOLD",
  "new_slug": "inst-NEWNEWNEWNEWNEW",
  "new_status": "provisioning",
  "audit_id": "..."
}
```

The chat is now bound to the new instance (Principle I one-active-instance-per-chat invariant). The old row remains for audit.

### 3.2 Rotate Git Token

On a `running` instance:

```bash
curl -s -X POST -H "Cookie: $ADMIN_COOKIE" \
  http://localhost:8000/api/admin/instances/inst-XXX/rotate-token | jq .
```

Response (within 10s — FR-018):

```json
{ "slug": "inst-XXX", "rotated_at": "...", "audit_id": "..." }
```

Inside the container, verify the new token is wired:

```bash
docker compose exec <instance-container> cat ~/.git-credentials
```

### 3.3 Extend Expiry

```bash
curl -s -X POST -H "Cookie: $ADMIN_COOKIE" -H "Content-Type: application/json" \
  -d '{"extend_hours": 4}' \
  http://localhost:8000/api/admin/instances/inst-XXX/extend-expiry | jq .
```

Verify `expires_at` advances:

```bash
docker compose exec db psql -U app -d taghdev \
  -c "SELECT slug, expires_at FROM instances WHERE slug='inst-XXX';"
```

The 5-min inactivity reaper will not terminate this instance before the new deadline (Edge Cases §6 of data-model).

---

## P4 — Filter, search, bulk Force Terminate (US4)

### 4.1 Filter

In the URL bar: `/settings/instances?status=failed` shows only failed. `?q=abc` shows only slugs containing `abc` (FR-008). Sort by clicking column headers.

### 4.2 Bulk terminate

Multi-select 3+ rows via the row checkboxes; click **Bulk Force Terminate**. The confirmation dialog shows the count and a sample of slugs (FR-023). Confirm.

```bash
curl -s -X POST -H "Cookie: $ADMIN_COOKIE" -H "Content-Type: application/json" \
  -d '{"slugs": ["inst-aaa", "inst-bbb", "inst-ccc"], "confirm": true}' \
  http://localhost:8000/api/admin/instances/bulk-terminate | jq .
```

Expected: per-slug `outcome` array (`queued` / `already_ended` / `not_found`); one audit row per affected slug; cap at 50 enforced (FR-022).

### 4.3 Cap rejection

```bash
curl -s -X POST -H "Cookie: $ADMIN_COOKIE" -H "Content-Type: application/json" \
  -d '{"slugs": [..51 entries..], "confirm": true}' \
  http://localhost:8000/api/admin/instances/bulk-terminate
```

Returns `422 {"detail": "...", "code": "bulk_cap_exceeded"}`.

---

## P5 — Health overview (US5)

### 5.1 Counts widget

The `/settings/instances` page header shows live counts. Spawn/terminate instances and watch them update via SSE within 10s without refresh.

### 5.2 Recent failures strip

Force-fail a couple of provisions (steps 2.1). The "Recent failures (24h)" strip at the top of the page lists them with click-through links to their detail view.

---

## Authorization smoke test (FR-003, FR-004)

### Non-admin user

Log in as a non-admin web-chat user. Visit `/settings/instances` — expect 403. Hit the JSON API directly:

```bash
curl -s -H "Cookie: <non-admin cookie>" http://localhost:8000/api/admin/instances
```

Expected: `403 {"detail": "Admin only", "code": "admin_only"}`.

### Internal endpoints unaffected

Verify the internal compose-network endpoints still reject from the host:

```bash
curl -s http://localhost:8000/internal/instances/inst-XXX/heartbeat
```

Should return 401/403 (HMAC required) or 404, not 200. Confirms FR-004.

---

## Architecture-fitness gate

Before declaring the slice done, run the static gate:

```bash
python scripts/pipeline_fitness.py
```

Expected: all checks pass, including:

- `api_route_contract` — every fetch URL the new templates emit (`/api/admin/instances`, `/api/admin/instances/<slug>`, `/api/admin/instances/<slug>/logs`, etc.) corresponds to a registered FastAPI route.
- `arq_job_contract` — no new typo'd job names introduced.
- `db_model_drift` — migration 014 matches the `TerminatedReason` enum.
- `redactor_coverage` — new `/logs` endpoint wraps log message + context in `audit_service.redact()`.
- `timeouts` — no new `httpx.AsyncClient(...)` introduced (this feature has no external HTTP calls — all upstream work is in existing ARQ jobs).

Then the live gate:

```bash
# Drives the full chat → provision → admin-terminate path via Playwright MCP
/e2e-pipeline
```

The e2e skill should be extended (or a new "admin-control-plane" phase added) to also click through `/settings/instances` and verify the SSE updates. If the e2e skill does not yet exercise the admin UI, note it in the slice's PR as a follow-up.

---

## Common gotchas

- **SSE staleness**: if the list view doesn't update within 10s, check the browser's Network tab → the SSE connection should be open with `text/event-stream` and receiving `data:` lines. If not, the `?slug=` filter in `/api/activity/stream` may not be merged yet — that is a bounded same-PR change per [contracts/sse-events.md](contracts/sse-events.md) §"Backend filter".
- **Reprovision into the same compose project name**: the new instance has a fresh `compose_project`. If you see "compose project already exists" errors, it means the old teardown didn't fully complete — let it finish, then retry.
- **Bulk terminate is async per slug**: the response returns immediately with `outcome: "queued"`; actual teardown takes ≤60s per instance and runs in parallel via ARQ. Use `/settings/instances` SSE to watch progress.
- **Audit log batching**: `audit_service.log_action()` flushes in batches of 10 ([src/taghdev/services/audit_service.py:119](src/taghdev/services/audit_service.py#L119)). For tests asserting "audit row exists immediately", call the service's flush helper or wait briefly. The admin UI is unaffected because it always queries by slug after refresh.

---

## Sign-off checklist

Before marking the feature "Done and verified" per Principle VII, confirm:

- [ ] All five user stories above produce the expected outputs end-to-end.
- [ ] Migration 014 applied; `terminated_reason='admin_forced'` accepted by DB.
- [ ] `pipeline_fitness.py` clean.
- [ ] Non-admin gets 403 on every admin endpoint.
- [ ] Internal `/internal/instances/*` endpoints unchanged.
- [ ] No `Instance.heartbeat_secret` or `Instance.db_password` appears in any admin API response (grep `curl` outputs from this quickstart).
- [ ] Audit row written for every destructive action exercised here.
- [ ] SSE updates land in ≤10s for every status transition tested.
