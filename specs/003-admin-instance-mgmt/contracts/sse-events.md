# Contract — SSE Events for Admin Instance Updates

**Feature**: 003-admin-instance-mgmt
**Surface**: Server-Sent Events over the existing `/api/activity/stream` endpoint
**Mechanism**: emit-side calls `activity_log.log_event(<payload>)`; consumer-side subscribes via `EventSource("/api/activity/stream?type=instance_status[&slug=…]")`.

This contract lets the admin list and detail views satisfy FR-006 (≤10s staleness) without polling.

---

## Why this exists

Decision 2 in [research.md](../research.md) chose SSE over polling. The existing stream endpoint at [src/openclow/api/routes/activity.py:48-83](src/openclow/api/routes/activity.py#L48-L83) is the canonical mechanism; this contract pins the **event-type vocabulary** the admin UI listens for and the **emit sites** that must produce them. The fitness suite's `stream_event_contract` check should treat this file as the source of truth for instance-related event types.

---

## Event-type vocabulary

All payloads MUST be JSON with a `type` field at the top level. The admin UI subscribes with `?type=<one of these>` to filter server-side.

### `instance_status`

Emitted on every Instance status transition (provisioning → running → idle → terminating → destroyed/failed) and on initial provision.

```json
{
  "type": "instance_status",
  "slug": "inst-0123456789abcd",
  "status": "running",
  "previous_status": "provisioning",
  "at": "2026-04-27T12:34:56.789Z",
  "user_id": "u_…",
  "project_id": "p_…",
  "preview_url": "https://….trycloudflare.com" | null,
  "failure_code": null,
  "failure_message": null
}
```

`failure_code` / `failure_message` populated only when `status == "failed"`. Used by both the list view (row state update) and the detail view (timeline append).

### `instance_action`

Emitted on every admin-initiated state change (Force Terminate, Reprovision, Rotate Token, Extend Expiry).

```json
{
  "type": "instance_action",
  "slug": "inst-…",
  "action": "force_terminate",
  "actor": "u_admin_…",
  "outcome": "queued" | "blocked" | "ok" | "error",
  "at": "2026-04-27T12:34:56.789Z",
  "audit_id": "…",
  "metadata": { "reason": "admin_forced", ... }
}
```

Allows two admins on the same page to see each other's actions with ≤10s lag (Edge Cases §1; FR-028).

### `instance_upstream`

Emitted on every transition of upstream-degradation state (existing infra at `openclow:instance_upstream:<slug>:<cap>`). The detail view uses this to update the tunnel-health badge live.

```json
{
  "type": "instance_upstream",
  "slug": "inst-…",
  "capability": "preview" | "git" | "exec",
  "health": "live" | "degraded" | "unreachable",
  "at": "2026-04-27T12:34:56.789Z"
}
```

Sourced by extending the existing `tunnel_health_check_cron` job to call `activity_log.log_event(...)` on health-state changes (no new probe; existing 60s cron). Idempotent — duplicate emits at same health state are dropped client-side by comparing to last-seen value.

### `instance_summary`

Emitted at most once per minute (rate-limited at the emit site) when StatusCounts (`running`/`idle`/`provisioning`/`terminating`/`failed_24h`) materially change. Used by the list-view header.

```json
{
  "type": "instance_summary",
  "running": 12,
  "idle": 4,
  "provisioning": 1,
  "terminating": 0,
  "failed_24h": 2,
  "total_active": 17,
  "at": "2026-04-27T12:34:56.789Z"
}
```

Rate limit: at most one emit per 60s per type, debounced server-side. Initial render of the list view fetches `GET /api/admin/instances/summary` synchronously; SSE only delivers updates.

---

## Emit sites (binding)

The fitness suite's `stream_event_contract` check should verify each of these emit sites is wired:

| Event type | Emit site (file:line) | Trigger |
|---|---|---|
| `instance_status` | `src/openclow/services/instance_service.py` (every method that mutates `Instance.status`) | After successful UPDATE commits. |
| `instance_status` | `src/openclow/worker/tasks/instance_tasks.py::provision_instance` | At each phase boundary (provisioning → running, running → failed). |
| `instance_status` | `src/openclow/worker/tasks/instance_tasks.py::teardown_instance` | At terminating → destroyed transition. |
| `instance_action` | `src/openclow/api/routes/admin_instances.py` (new file) | After enqueue/dispatch in each admin action handler. |
| `instance_upstream` | `src/openclow/worker/tasks/instance_tasks.py::tunnel_health_check_cron` | On state-change (not on every probe). |
| `instance_summary` | `src/openclow/services/instance_service.py` (debounced helper) | After any `instance_status` emit, debounced to ≤1/min. |

All emits MUST happen **after** the DB write commits (Principle VI: durable state is the source of truth). Order: `commit → log_event → return`.

---

## Subscriber-side (templates)

### List view (`settings/instances.html`)

```javascript
const stream = new EventSource("/api/activity/stream?type=instance_status,instance_summary");
stream.onmessage = (e) => {
  const evt = JSON.parse(e.data);
  if (evt.type === "instance_status") patchRow(evt);
  else if (evt.type === "instance_summary") patchSummary(evt);
};
```

`patchRow` updates the in-DOM row matching `evt.slug` (or removes/adds it if filter set excludes/includes the new status). On reconnect (browser-default behavior of `EventSource`), the page falls back to a single `GET /api/admin/instances` refresh to re-sync.

### Detail view (`settings/instance_detail.html`)

```javascript
const stream = new EventSource(`/api/activity/stream?type=instance_status,instance_action,instance_upstream&slug=${slug}`);
```

The `slug=` query parameter is honored server-side (filter step in `/api/activity/stream`'s generator) so the client only receives this instance's events.

---

## Backend filter for `slug=`

The existing `/api/activity/stream` accepts `?type=` filters. This contract requires extending it to accept `?slug=` as a secondary filter — server-side, so non-matching events are dropped before the network. **Implementation note**: this is a 5-line change to the generator in [src/openclow/api/routes/activity.py:48-83](src/openclow/api/routes/activity.py#L48-L83). It is bounded by Principle VII (no half-features): the change MUST land in the same PR as the detail-view template that depends on it.

---

## Reconnect & ordering guarantees

- **Reconnect**: `EventSource` automatically reconnects with exponential backoff. The admin UI does NOT attempt to replay missed events from a checkpoint — instead, it issues a full `GET /api/admin/instances` (list view) or `GET /api/admin/instances/{slug}` (detail view) on reconnect to re-sync. This is acceptable because the spec mandates ≤10s staleness, not zero loss.
- **Out-of-order delivery**: SSE is in-order over a single connection; no special handling needed.
- **Duplicate events**: client-side dedupe by `(slug, status, at)` tuple within a 5-second window; server-side debounce for `instance_summary`.

---

## Out-of-scope SSE work

- **No new SSE endpoint** — reuses `/api/activity/stream`.
- **No event replay buffer** — recent-replay is provided by the JSON list endpoint, not SSE.
- **No per-event auth** — the SSE endpoint already gates on `verify_settings_auth` + admin role; once subscribed, all events for that admin's filter pass through.
- **No log-line streaming** — log lines for the detail view are pulled via `GET /api/admin/instances/{slug}/logs` on demand or on a 10s refresh; SSE is for status, not body content.
