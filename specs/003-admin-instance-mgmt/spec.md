# Feature Specification: Admin Instance Management

**Feature Branch**: `003-admin-instance-mgmt`
**Created**: 2026-04-27
**Status**: Draft
**Input**: User description: "see our system on 001 its do a nicw work but on the admin dashboard that i cand mange the instances like remove them or what ever as mange — i mean in here see there is nothing to control or do any things connect to hole 001 it must as admin every thing flexable to me to mange"

## Background

Feature 001 (per-chat-instances) introduced isolated per-chat container stacks behind Cloudflare tunnels. Today, instances are created, evolved, and torn down entirely by background workers and the inactivity reaper — but the admin dashboard has **zero surface for inspecting, controlling, or recovering them**. The Management sidebar (Projects, Users, Channels) ends before instances ever appear.

This is operationally untenable: a stuck `provisioning` instance, a runaway long-lived chat, an instance whose tunnel went degraded, or a per-user-cap collision are all invisible to the operator until something else breaks. Phase 11 (T107–T116) of spec 001 sketched a minimal "Force Terminate" tab inside `AccessPanel`; **this spec supersedes that minimal scope** with a first-class Instances admin surface that gives the operator full lifecycle visibility and control.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — See and force-terminate active instances (Priority: P1)

The admin opens the dashboard and clicks **Instances** in the Management sidebar. They land on a page listing every active instance across the platform — slug, owning user, project, status, when it last had activity, when it expires, and the live preview URL. They spot a runaway instance from a user who left a chat open over the weekend, click Force Terminate, confirm the prompt, and watch the row flip to `terminating` then disappear from the active view a minute later.

**Why this priority**: Without this, the platform is uncontrollable in production — a single misbehaving chat can hold a slot indefinitely with no operator recourse short of a database surgery. P1 is the smallest unit of value that makes the feature shipping-worthy.

**Independent Test**: Provision two test instances, log in as admin, verify both appear in the list with correct status; click Force Terminate on one, confirm the dialog, observe the row transition through `terminating` → removed from active view, and the underlying compose stack and tunnel torn down.

**Acceptance Scenarios**:

1. **Given** at least one instance in `running` status exists, **When** an admin opens the Instances page, **Then** the row appears within 5 seconds and shows accurate slug, user, project, status, and preview URL.
2. **Given** an admin clicks Force Terminate on a `running` instance, **When** they confirm the dialog, **Then** the instance transitions to `terminating` and within 60 seconds the underlying compose project and Cloudflare tunnel are removed.
3. **Given** the admin clicks Force Terminate but cancels the confirmation, **When** they dismiss the dialog, **Then** the instance status is unchanged and no teardown job is enqueued.
4. **Given** a non-admin user navigates directly to the Instances URL, **When** the page tries to load, **Then** they receive a 403 and are redirected to the dashboard home.

---

### User Story 2 — Drill into a single instance to diagnose problems (Priority: P2)

An admin clicks a row (or a "Details" action on the row) and a detail view opens showing the full picture for that one instance: every status transition with timestamp (provisioning → running → idle → …), the chat session and project it belongs to, the compose project name, the workspace path, the session branch, the tunnel URL with health state (live / degraded / unreachable), the last failure code/message if any, the recent worker log lines tagged with this slug, and the upstream-degradation history from Redis.

**Why this priority**: Once admins can see and kill instances (P1), the next real pain is "*why* did this one fail?" — without a detail view they have to grep worker logs by slug manually. This is what makes the feature actually replace the operator's current shell-based workflow.

**Independent Test**: Manually break a test project (e.g., bad compose template) so provisioning fails; open the detail view for the failed instance; verify the timeline shows the `provisioning` → `failed` transition with the correct `failure_code` and `failure_message`, and the recent log section shows the worker error lines tagged with that slug.

**Acceptance Scenarios**:

1. **Given** an instance with at least one status transition, **When** admin opens its detail view, **Then** every transition is shown with timestamp in chronological order.
2. **Given** an instance that ended in `failed` status, **When** admin opens its detail view, **Then** the failure code and human-readable message are surfaced prominently (not buried in a JSON blob).
3. **Given** a `running` instance whose tunnel is currently in `degraded` upstream state, **When** admin opens its detail view, **Then** the tunnel section shows `degraded`, the affected capability, and the timestamp of degradation.
4. **Given** an instance whose chat was deleted, **When** admin opens its detail view, **Then** the chat link is shown as "deleted" instead of a broken link, but all other fields remain visible.

---

### User Story 3 — Recover and operate on existing instances (Priority: P3)

From the detail view, the admin can take corrective actions beyond Force Terminate: **Reprovision** a `failed` or `destroyed` instance (re-runs the provision flow against the same chat), **Rotate Git Token** on a `running` instance (forces a fresh GitHub token without waiting for the 45-minute cron), **Extend Expiry** to push back the inactivity-reaper deadline by a chosen window (1h / 4h / 24h), and **Open Preview URL** / **Open in Chat** as quick-jump buttons.

**Why this priority**: P3 is the "platform-engineer" power tier — the admin can not only see and kill but also nudge. Operationally important for incidents and demos but not blocking initial value: P1+P2 alone already replace the manual workflow for 80% of cases.

**Independent Test**: Create a `failed` instance, click Reprovision, confirm; verify a fresh provision job is enqueued, the instance row transitions back through `provisioning` → `running`, and the underlying compose stack comes up cleanly.

**Acceptance Scenarios**:

1. **Given** an instance in `failed` status, **When** admin clicks Reprovision and confirms, **Then** a new provision job is enqueued, the status transitions to `provisioning`, and the existing chat is re-bound to the new running instance.
2. **Given** an instance in `running` status, **When** admin clicks Rotate Git Token, **Then** a fresh GitHub installation token is minted within 10 seconds and persisted into the container's git credentials, with success confirmation in the UI.
3. **Given** an instance approaching its `expires_at`, **When** admin clicks Extend Expiry → +4h, **Then** the `expires_at` field advances by 4 hours and the inactivity reaper will not terminate it before that new deadline.
4. **Given** any reachable instance, **When** admin clicks Open Preview URL, **Then** the live tunnel URL opens in a new tab.

---

### User Story 4 — Filter, search, and bulk-operate (Priority: P4)

The Instances page supports filtering by status (any combination of `provisioning` / `running` / `idle` / `terminating` / `failed` / `destroyed`), filtering by owning user, filtering by project, and free-text search by slug. The admin can multi-select rows and perform bulk Force Terminate (capped to a safe maximum per action). A "preset" filter quickly shows commonly-needed slices: *Stuck in provisioning >5 min*, *Failed today*, *Idle approaching expiry*.

**Why this priority**: Helpful at scale (50+ active instances) but not required for early operation when the platform-wide list still fits on one screen.

**Independent Test**: Seed 30 instances across multiple users/projects/statuses; apply a status filter and verify only matching rows show; multi-select 3 `idle` instances, click Bulk Force Terminate, confirm, and verify all 3 transition to `terminating`.

**Acceptance Scenarios**:

1. **Given** 30+ instances exist across statuses, **When** admin checks the `failed` status filter, **Then** only `failed` instances are shown and the count badge updates.
2. **Given** the admin types a partial slug into the search box, **When** they finish typing, **Then** the list filters to instances whose slug contains the substring within 1 second.
3. **Given** the admin selects 5 rows and clicks Bulk Force Terminate, **When** they confirm a single confirmation dialog (with a hard upper bound that prevents accidentally selecting all), **Then** all 5 selected instances are queued for teardown.
4. **Given** a bulk action targets more than the safe maximum, **When** admin attempts to confirm, **Then** the system blocks the action with a clear "select fewer items" message.

---

### User Story 5 — Health overview at a glance (Priority: P5)

The top of the Instances page (or a small dashboard widget on the Overview page) shows live counts: how many are `running`, `idle`, `provisioning`, `terminating`, `failed` in the last 24h, and a "platform capacity utilization" reading (active / cap). A small "Recent failures" strip lists the last 5 failed instances with one-click jump to their detail view.

**Why this priority**: Nice-to-have for operational situational awareness; not required for individual-instance management which is the core ask.

**Independent Test**: Render the Instances page and verify the count badges sum to the total in the underlying DB query, and the recent-failures strip matches the 5 most recent rows with `status = failed`.

**Acceptance Scenarios**:

1. **Given** the underlying DB has known counts per status, **When** admin opens the Instances page, **Then** the header counts match those numbers exactly (within the freshness window of US1).
2. **Given** at least 5 instances failed in the last 24h, **When** admin views the Recent failures strip, **Then** the 5 newest are listed with timestamp + failure code + click-through link.

---

### Edge Cases

- **Two admins act on the same instance simultaneously**: second action gets a "already terminating" message, no double-teardown is enqueued.
- **Instance whose chat session was deleted**: row still appears (chat link shows "deleted"); Force Terminate still works; Reprovision is disabled (no chat to re-bind).
- **Instance stuck in `provisioning` >10 min**: highlighted in the list with an amber state badge; admin can Force Terminate to recover the slot.
- **Instance row exists but compose stack already gone (out-of-band cleanup)**: Force Terminate still completes idempotently and ends in `destroyed`.
- **Admin force-terminates an instance currently being torn down**: action is a no-op with a clear "already terminating" toast.
- **Admin role revoked mid-session**: next action returns 403; the page is no longer accessible on refresh.
- **Network interruption during a destructive action**: action either commits server-side and the UI reconciles on next poll, or returns an error and the instance state is unchanged — never a partial teardown that leaves orphan compose projects.
- **Hundreds of instances**: list loads progressively (pagination or virtualized scroll); filter operations remain responsive.
- **Reprovision on a `destroyed` instance whose project was deleted**: action is blocked with a clear message; admin must re-create the project first.

## Requirements *(mandatory)*

### Functional Requirements

#### Sidebar & navigation
- **FR-001**: A new **Instances** item MUST appear in the Management group of the admin sidebar, alongside Projects / Users / Channels.
- **FR-002**: Clicking Instances MUST load a page that lists all instances visible to the admin, regardless of which user or chat owns them.

#### Authorization
- **FR-003**: Every Instances page and every instance-management API endpoint MUST require the admin role; non-admins MUST receive 403 with no data leakage in the error body.
- **FR-004**: The internal heartbeat / token-rotation / explain endpoints (compose-network only) MUST remain unaffected and MUST NOT be reachable through any new admin-facing route.

#### List view (US1, US4, US5)
- **FR-005**: The list view MUST display, for each instance: slug, owning user (name + identifier), project name, status (color-coded), created-at (relative + absolute on hover), last-activity-at, expires-at, preview URL (clickable when present).
- **FR-006**: The list view MUST update without manual refresh; staleness MUST NOT exceed 10 seconds for any visible field.
- **FR-007**: The list view MUST support sorting by any displayed column.
- **FR-008**: The list view MUST support filtering by status (multi-select), owning user, project, and free-text slug search.
- **FR-009**: The list view MUST default to showing active instances (`provisioning`, `running`, `idle`, `terminating`); ended instances (`destroyed`, `failed`) MUST be hidden by default but accessible via filter.
- **FR-010**: The list view MUST surface platform-wide status counts in a header (running / idle / provisioning / terminating / failed-24h) that match the filtered or unfiltered totals.
- **FR-011**: Stuck-state instances (`provisioning` for >10 min, `terminating` for >5 min) MUST be visually flagged in the list.

#### Row actions (US1)
- **FR-012**: Every row MUST expose a **Force Terminate** action; clicking it MUST present a confirmation dialog naming the instance and its owner before any teardown is enqueued.
- **FR-013**: Force Terminate MUST mark the instance with the `admin_forced` termination reason (a new value added to the existing termination-reason enum).
- **FR-014**: Force Terminate on an instance already in `terminating` / `destroyed` MUST be a no-op with a clear "already ended" message and MUST NOT enqueue a duplicate teardown.

#### Detail view (US2)
- **FR-015**: Each instance MUST be openable in a detail view that shows: full status timeline (every transition with timestamp), chat session link, project link, compose project name, workspace path, session branch, image digest, resource profile, tunnel URL with current upstream health state, recent upstream-degradation history, last failure code and message (if any), and the recent worker log lines tagged with this instance's slug.
- **FR-016**: The detail view MUST gracefully handle missing references (deleted chat, deleted project) by showing a "deleted" placeholder rather than a broken link, while keeping all other fields visible.

#### Detail-view actions (US3)
- **FR-017**: The detail view MUST expose **Reprovision** for instances in `failed` or `destroyed` status when the bound chat still exists; the action MUST enqueue a fresh provision and re-bind the same chat to the resulting instance.
- **FR-018**: The detail view MUST expose **Rotate Git Token** for `running` instances; the action MUST mint and inject a fresh GitHub installation token within 10 seconds and report success/failure to the admin.
- **FR-019**: The detail view MUST expose **Extend Expiry** with options +1h / +4h / +24h; the action MUST advance `expires_at` and prevent the inactivity reaper from terminating the instance before the new deadline.
- **FR-020**: The detail view MUST expose **Open Preview URL** and **Open in Chat** quick-links.

#### Bulk operations (US4)
- **FR-021**: The list view MUST support multi-row selection and a **Bulk Force Terminate** action over the selected rows.
- **FR-022**: Bulk Force Terminate MUST be capped at a safe maximum per action (no more than 50 selected); attempts beyond the cap MUST be blocked client-side with a clear message.
- **FR-023**: Bulk Force Terminate MUST present a single confirmation that explicitly lists the count of instances and a sample of slugs/owners before execution.

#### Audit (cross-cutting)
- **FR-024**: Every admin-initiated state change (Force Terminate, Bulk Force Terminate, Reprovision, Rotate Git Token, Extend Expiry) MUST be recorded in an audit log capturing: admin user identifier, action name, target instance identifier, timestamp, parameters, and outcome (success / error).
- **FR-025**: The audit log MUST be readable from the detail view of each instance (or from a filterable platform-wide audit view), so admins can answer "who terminated this?".

#### Read-only safety
- **FR-026**: This spec MUST NOT introduce a way for admins to **provision** an instance on behalf of a chat they don't own. (Provisioning-on-behalf is explicitly out of scope; an admin who needs to test must use their own chat.)

#### Resilience
- **FR-027**: A failed admin action (network error, server error) MUST leave the instance in its prior state and report the error clearly; partial state transitions are forbidden.
- **FR-028**: Concurrent admin actions on the same instance MUST be serialized server-side; the second action MUST receive a clear "instance is already in <state>" response.

### Key Entities

- **Instance** *(reused from spec 001)*: identified by slug, has status, owning chat session, project, timestamps (`created_at`, `started_at`, `last_activity_at`, `expires_at`, `terminated_at`), failure code/message, and termination reason. The new termination reason `admin_forced` is added by this spec.
- **Admin Action Audit Entry** *(new)*: immutable record of one admin-initiated action — admin user identifier, action name (e.g. `force_terminate`, `reprovision`, `rotate_git_token`, `extend_expiry`, `bulk_force_terminate`), target instance identifier(s), parameters, timestamp, outcome.
- **Instance Detail Aggregate** *(view-only composition)*: Instance row + status-transition timeline + tunnel-health snapshot + recent worker log lines + recent upstream degradation events + failure context + bound chat / project metadata.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An admin can locate any specific active instance — by slug, owning user, or project — within 30 seconds of opening the dashboard.
- **SC-002**: An admin can force-terminate a runaway instance (sidebar → list → row → confirm) in 5 clicks or fewer, with the underlying compose stack and tunnel torn down within 60 seconds.
- **SC-003**: An admin can diagnose the root cause of a failed provision (read failure code, message, and recent worker log lines) without leaving the dashboard, in under 2 minutes.
- **SC-004**: Status fields visible to the admin reflect actual platform state with staleness no greater than 10 seconds.
- **SC-005**: 100% of admin-initiated destructive or recovery actions are recorded in the audit log; no untracked actions exist.
- **SC-006**: The Instances list remains responsive (filter, sort, scroll without UI lag) with at least 500 instances loaded.
- **SC-007**: Zero double-teardowns occur when two admins act on the same instance simultaneously; the second action is a clean no-op.
- **SC-008**: After this feature ships, operator interventions that previously required shelling into the host or running database queries are reduced by at least 90% (measured by operator self-report or absence of `docker compose down` / SQL-update activity in operator command history during incidents).

## Assumptions

- **Builds on spec 001**: the Instance model, InstanceService, ARQ jobs (`provision_instance`, `teardown_instance`, `rotate_github_token`), and inactivity-reaper cron all exist and remain the canonical owners of the underlying state machine. This feature is a control-plane UI on top of them, not a re-implementation.
- **Supersedes Phase 11 of spec 001 (T107–T116)**: that phase enumerated only a minimal table + Force Terminate inside `AccessPanel`. This spec rolls those tasks into a richer first-class admin section; the narrower Phase 11 work should be either dropped or repurposed as the P1 slice of this feature.
- **Admin role definition**: "admin" is the same role enforced by the existing `_require_admin(user)` guard used by the access-control routes today. No new role is introduced.
- **Frontend stack continuity**: the existing admin dashboard is server-rendered Jinja2 + Tailwind + HTMX. This spec is technology-agnostic about the page mechanics, but the implementation should align with the existing pattern unless a clear reason emerges to deviate.
- **Audit log substrate**: a persistent audit-log table either already exists or will be added as part of this work. The spec mandates the behavior; the storage mechanism is left to the implementation plan.
- **Worker log retrieval by slug**: workers already include the instance slug in their log context, making per-instance log filtering feasible without new instrumentation. If not, log-tagging plumbing is a sub-requirement of US2.
- **Container stdout is out of scope (v1)**: the detail view shows worker log lines tagged with the slug, not live container stdout. Live container log streaming can come later as a P5+ enhancement.
- **Provision-on-behalf is explicitly out of scope**: admins manage existing instances; they do not start new ones for chats they don't own (FR-026).
- **Capacity / quota changes are out of scope**: the per-user cap and platform-wide capacity remain managed elsewhere; this spec only surfaces them as read-only context (e.g. utilization in the header) where useful.
- **No bulk reprovision in v1**: bulk operations are limited to Force Terminate. Reprovision / Rotate Token / Extend Expiry are single-instance only — bulk versions would compound risk and are deferred.
