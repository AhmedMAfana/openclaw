# Feature Specification: Per-Chat Isolated Instances

**Feature Branch**: `multi-instance`
**Created**: 2026-04-23
**Status**: Draft
**Input**: User description: "@docs/architecture/per-chat-instances.md"

## Overview

Today, every chat session on the platform shares one development environment. When two users (or two chats of the same user) work concurrently, their running apps, databases, and LLM agents share the same host, the same ports, and the same credentials. There is no isolation between chats, and environments leak across sessions.

This feature replaces that model with **one isolated development environment per chat**. A chat gets its own private app stack, its own private database, its own public URL for live preview, and its own short-lived credentials. Idle environments are cleaned up automatically so resources are not wasted. Returning to a chat after cleanup transparently spins up a fresh environment and reattaches the user's in-progress code.

## Clarifications

### Session 2026-04-23

- Q: Is the preview URL reachable by anyone who possesses the link, or must the viewer be authenticated? → A: Public — anyone with the link can reach the running app. Privacy relies on URL unguessability, not on platform auth.
- Q: Does the platform enforce a per-user cap on concurrent active environments, or only the platform-wide capacity bound? → A: Operator-configurable soft limit, default 3 per user. Exceeding it returns a distinct "too many active chats — end one to start another" error, separate from FR-030's platform-wide capacity error. No cross-chat reuse of instances.
- Q: What is the retention policy for terminated instances (metadata rows, audit trail, and the chat's working branch)? → A: Chat-lifetime retention. Instance rows, tunnel history, task history, audit logs, and the chat's working branch are retained as long as the owning chat session exists. When the chat is deleted by the user, all five are cascade-deleted. No time-based auto-purge in v1.
- Q: When an upstream dependency (preview URL provider, GitHub) degrades mid-session, does the platform tear down the instance? → A: No. Keep the instance in `running`, show a non-blocking status banner in the chat naming the degraded upstream ("preview URL temporarily unavailable", "git push queued — upstream degraded"), and retry silently in the background. Banner clears on recovery. Transient upstream outages do NOT trip the instance to `failed`.
- Q: What is the default duration of the idle-to-terminate grace window (the buffer between "24 h idle reached" and actual teardown)? → A: 60 minutes. Operator-configurable without a code change. Activity during the window (chat message, in-environment heartbeat signal) cancels the teardown and returns the instance to normal state.
- Followup on Q1 (2026-04-24): entropy floor for the unguessable slug is pinned at 56 bits (14 hex chars) after `/speckit.analyze` found that the original "target 64+" language was unreachable given the 20-char DNS-label cap (`inst-` prefix leaves 15 chars). Applied to FR-018a, data-model §1.1/§1.2, and all contract pattern strings.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Isolated environment per chat (Priority: P1)

A user opens a new chat and asks the assistant to scaffold or modify a project. Behind the scenes, the platform provisions a brand-new, fully isolated development environment dedicated to this chat. The user's running app is reachable only through a private URL tied to this chat. A different user working in a different chat at the same time has their own separate environment — the two cannot see or interfere with each other's running apps, data, or credentials.

**Why this priority**: This is the core promise of the feature. Without per-chat isolation, the rest of the requirements have no meaning. It's also the minimum viable slice — once a chat can reliably get its own private environment, the product is usable even without fancy lifecycle management.

**Independent Test**: Open two concurrent chats (same or different users); run a task that starts the dev server in each; confirm each chat gets a distinct public URL that serves its own running app, and confirm neither chat's URL, credentials, or running processes are visible to the other.

**Acceptance Scenarios**:

1. **Given** a user starts a new chat, **When** they send their first message that requires a running environment, **Then** the system provisions a dedicated environment for that chat and confirms it is ready.
2. **Given** two chats are active at the same time, **When** each runs a task that modifies files, **Then** each chat sees only its own file changes and its own process output.
3. **Given** a chat has an active environment, **When** the user opens the chat's preview URL, **Then** they see the app running inside *their* environment and no other chat's app.
4. **Given** a chat has an active environment, **When** a task in another chat fails or crashes, **Then** this chat's environment is unaffected and remains responsive.

---

### User Story 2 — Automatic cleanup of idle environments (Priority: P1)

A user steps away from a chat for the day. The platform notices there has been no activity for 24 hours and releases the environment's resources. A short grace notice is sent to the chat before teardown so a user who comes back at the last minute is not surprised.

**Why this priority**: Every isolated environment consumes memory, disk, and network quota. Without automatic cleanup the platform cannot sustain more than a handful of chats before exhausting the host. Cleanup is as fundamental to the feature's viability as provisioning itself.

**Independent Test**: Provision an environment, stop all activity, fast-forward the inactivity clock past 24 hours plus the grace window; confirm the environment is torn down, the preview URL stops responding, the chat is notified, and no leftover containers, volumes, or public URLs remain.

**Acceptance Scenarios**:

1. **Given** an environment has had no activity for the configured idle threshold, **When** the grace window starts, **Then** the chat receives a notice that the environment will be torn down soon.
2. **Given** the grace window has elapsed with no new activity, **When** the reaper runs, **Then** the environment is fully destroyed (processes stopped, storage reclaimed, public URL released).
3. **Given** a user sends a new message during the grace window, **When** the message arrives, **Then** teardown is cancelled and the environment returns to normal state without re-provisioning.
4. **Given** the user is not chatting but is actively editing files or running the app inside the preview, **When** that activity happens, **Then** the inactivity clock is reset as if they had sent a chat message.

---

### User Story 3 — Resume a chat after its environment was cleaned up (Priority: P2)

A user returns to a chat two days later. Its environment was torn down after 24 hours of inactivity. On their next message the platform silently provisions a fresh environment and reattaches the in-progress branch of code they were working on, so the user picks up exactly where they left off without manual steps.

**Why this priority**: Without resume, idle cleanup becomes a data-loss event — users will avoid walking away from chats. P2 (not P1) only because the core isolation and cleanup behaviour can ship first and be validated; resume is the polish that makes cleanup acceptable in practice.

**Independent Test**: Provision an environment, make some code changes, let it be torn down by the reaper, then send a new message in the same chat. Confirm a new environment is created automatically, the previous code changes are still present on the chat's working branch, and the new preview URL works.

**Acceptance Scenarios**:

1. **Given** a chat's previous environment was destroyed, **When** the user sends a new message, **Then** a fresh environment is provisioned on demand and the user's in-progress code branch is reattached.
2. **Given** a chat is resumed, **When** the new environment is ready, **Then** the user receives a new preview URL specific to the new environment.
3. **Given** a chat has never had an environment, **When** the user sends their first message requiring one, **Then** provisioning follows the same flow as a resume.

---

### User Story 4 — Live preview through a public URL (Priority: P1)

While working in a chat, the user wants to view the running app in their browser, including live updates as the assistant edits code. The platform gives each chat a unique public URL that serves the chat's own running app, and code changes show up in the browser instantly (hot reload) without manual refresh.

**Why this priority**: The preview URL is how the user actually *sees* that the platform is working. Without it, the isolation in Story 1 is invisible. It is also the piece that most directly demonstrates value to a prospective user.

**Independent Test**: Provision an environment for a chat, open its public URL in a browser, have the assistant modify a visible piece of the app, and confirm the browser reflects the change without a manual refresh. Also confirm the URL stops resolving after the environment is torn down.

**Acceptance Scenarios**:

1. **Given** an environment is ready, **When** the user opens the chat's preview URL, **Then** the app loads and is fully interactive.
2. **Given** the user is viewing the preview, **When** the assistant edits a file in the app's front end, **Then** the browser updates without the user needing to refresh.
3. **Given** an environment is torn down, **When** anyone (including the original user) opens the preview URL afterwards, **Then** the URL no longer resolves to a running app.
4. **Given** the environment is being provisioned, **When** the user opens the URL too early, **Then** they see a clear "starting up" state rather than a confusing error.
5. **Given** the preview URL is public, **When** a third party is given the link by the user, **Then** they can load the app without signing in to the platform.

---

### User Story 5 — Manual "end session" control (Priority: P2)

A user finishes work and wants to release their environment immediately rather than wait for idle cleanup. They issue a terminate command (chat command or UI button). The environment is torn down right away, they get a confirmation, and a new message starts a fresh environment.

**Why this priority**: This is a quality-of-life feature for power users and for anyone cost-conscious about holding resources. It is not on the critical path — idle cleanup already handles the common case — so P2.

**Independent Test**: Provision an environment, issue the terminate command, confirm the environment is destroyed immediately (no grace window), and confirm a subsequent message triggers a fresh provisioning.

**Acceptance Scenarios**:

1. **Given** a user has an active environment, **When** they issue the terminate command, **Then** the environment is destroyed immediately and the user is notified.
2. **Given** a terminate command is in flight, **When** the user sends a new message before teardown completes, **Then** the platform waits for teardown to finish and then provisions a fresh environment rather than reusing the terminating one.

---

### User Story 6 — Clear failure reporting during provisioning (Priority: P2)

A user sends a message that needs an environment, but provisioning fails (for example, dependency install breaks, a container fails to start, or the public URL cannot be registered). The user sees a plain-language error in the chat explaining that the environment could not start, with a visible "retry" action. At least a navigation button back to the main menu is always present — the user is never stranded on a bare error.

**Why this priority**: Provisioning has many moving parts and will fail occasionally. A bad failure experience turns recoverable errors into lost users. This is P2 because the platform can ship with basic error text as long as a retry path exists; richer diagnostics can follow.

**Independent Test**: Force a provisioning step to fail (e.g., by injecting a broken dependency), confirm the user sees a clear error with retry + main-menu buttons, and confirm clicking retry re-attempts provisioning from the failed step without starting over.

**Acceptance Scenarios**:

1. **Given** provisioning fails at any step, **When** the failure is reported to the chat, **Then** the message includes a plain-language reason, a retry control, and a main-menu control.
2. **Given** a provisioning failure occurred, **When** the user clicks retry, **Then** the platform resumes from the failed step if possible rather than discarding successful earlier steps.
3. **Given** the user chooses not to retry, **When** the failed environment is inspected the next day, **Then** it has been cleaned up and is not consuming resources.

---

### User Story 7 — Assistant actions confined to the current chat's environment (Priority: P1)

The user asks the assistant to run commands, edit files, or restart services. Every such action affects only the current chat's environment. The assistant has no way to target another chat's environment — not by naming it, not by path traversal, not through a shell escape, not through a mistyped identifier.

**Why this priority**: Without this guarantee, the infrastructure isolation from Story 1 is undermined by the assistant itself. Security/tenancy is non-negotiable for any multi-user product. P1 alongside Story 1.

**Independent Test**: In a chat bound to environment A, instruct the assistant to perform operations that, if misrouted, would touch environment B (e.g., reference a file path that exists in B, attempt to restart a service by a shared name). Confirm every attempt stays inside A and any escape attempt is refused.

**Acceptance Scenarios**:

1. **Given** the assistant is working in chat A's environment, **When** it tries to read, write, or execute against a path outside that environment's workspace, **Then** the action is refused.
2. **Given** the assistant is working in chat A's environment, **When** it tries to restart or inspect a service by name, **Then** only services inside A's environment are available; names from other environments are not addressable.
3. **Given** the assistant pushes code on behalf of the user, **When** the push occurs, **Then** it targets only the repository bound to the chat's project, regardless of what the assistant constructed.

---

### Edge Cases

- **Reaching platform capacity**: When the host can no longer accept another environment (memory/disk/tunnel limits), what does a new chat's first message see? (Assumption: user is told the platform is at capacity, with a retry-later message. Not silent queuing.)
- **Single user at their per-user cap**: A user with the default 3 concurrent active environments opens a fourth chat. They are told they are at their cap, with links to their currently-active chats so they can end one. No automatic reuse, no silent re-binding.
- **Transient upstream outage mid-session**: The preview URL provider or git upstream has a hiccup while an instance is running. The instance stays running; a chat banner explains which capability is degraded; retries happen silently. The instance is not torn down. The banner clears automatically on recovery.
- **Chat re-used after termination**: Re-opening a chat whose environment was manually terminated on the previous message behaves the same as resume — fresh environment, branch reattached.
- **Activity only from the preview browser**: The user is clicking through the preview URL but not chatting. The idle clock must still reset. (Covered by heartbeat; explicit acceptance scenario above in Story 2.)
- **Activity from raw HTTP requests to the preview URL**: Explicit non-goal for v1 — raw browser requests hitting the preview are not a recognized activity source. Users who are only browsing without interacting with the IDE/chat will eventually time out.
- **Grace window abuse**: User sends one message per 23 hours forever, never actually working. Accepted: activity resets the clock by design; cost tuning (shorter TTL, per-project overrides) is a future decision.
- **Legacy projects created before this feature**: Continue to work under the old shared-environment model with no behavioural change. The new per-chat model applies only to new projects opted into it.
- **Simultaneous termination and new message**: A terminate and a new message arrive within milliseconds of each other. Teardown wins; the new message then provisions a fresh environment rather than racing with teardown.
- **Provisioning partial failure mid-way**: Some resources succeed, others fail. Teardown must be idempotent — retrying cleanup on a half-built environment must succeed without error.
- **Same project opened in two chats**: Two chats of the same project are fully isolated from each other — they do not share a running app, a database, or a preview URL. (Whether they should be able to "see" each other is an open product question, deliberately out of scope for v1.)
- **Preview URL leaked externally**: Because the URL is public (anyone with the link), pasting it into a public channel exposes the running app. This is a user-facing tradeoff, not a bug. Mitigation: URLs are high-entropy and stop resolving when the environment ends; users are informed (in UI copy) that the preview link is shareable-by-link.

## Requirements *(mandatory)*

### Functional Requirements

#### Environment lifecycle

- **FR-001**: The platform MUST provision a dedicated, isolated development environment for each chat that requires one, on demand from the user's first message.
- **FR-002**: The platform MUST bind each environment to exactly one chat at a time; no two chats may share an environment.
- **FR-003**: The platform MUST guarantee that one chat's environment cannot observe, modify, or receive traffic intended for another chat's environment.
- **FR-004**: The platform MUST expose the environment's current lifecycle state to the user via an in-chat system message whenever the state changes. At minimum these five transitions MUST surface: starting, ready, going-to-sleep, ended, failed. Each transition MUST emit exactly one chat message; the message text MUST name the state plainly. UI-badge or other surfaces are out of scope for v1.
- **FR-005**: The platform MUST allow the user to explicitly end the environment at any time via a chat command or UI control; teardown takes effect immediately with no grace window.
- **FR-006**: The platform MUST reclaim all resources tied to an ended environment — running processes, storage, public URL, and any secrets — with no residual footprint on retry or re-provision.

#### Inactivity and resume

- **FR-007**: The platform MUST treat 24 hours without activity as the default idle threshold; this value MUST be configurable by an operator without a code change.
- **FR-008**: The platform MUST warn the chat before an idle environment is torn down, with a grace window during which activity cancels the teardown. The default grace window is **60 minutes** and MUST be operator-configurable without a code change. The warning message MUST name the concrete remaining time ("ending in N minutes") and be updated or suppressed if activity resets the clock.
- **FR-009**: The platform MUST treat every new inbound chat message as activity and reset the idle clock.
- **FR-010**: The platform MUST treat in-environment signals (running dev server, running task, attached user shell) as activity so that a user working silently in the preview is not torn down.
- **FR-011**: The platform MUST NOT treat raw HTTP requests to the preview URL as an activity source in v1.
- **FR-012**: The platform MUST allow a chat whose environment has ended to resume on the next message by provisioning a fresh environment and reattaching the chat's in-progress code branch.
- **FR-013**: The platform MUST preserve the chat's working branch across environment teardowns for the life of the chat, so resume picks up the prior work.
- **FR-013a**: Terminated-instance metadata (instance row, tunnel-history row, linked task rows, audit-log entries) and the chat's working branch MUST be retained for as long as the owning chat session exists. No time-based auto-purge is applied in v1.
- **FR-013b**: Deleting the owning chat MUST cascade-delete all five artifacts listed in FR-013a with no residual record, so the user's "delete this chat" action is the single authoritative way to remove their environment history.
- **FR-013c**: Until the chat is deleted, resume MUST restore the chat's prior working branch regardless of how long ago the previous instance was terminated.

#### Preview URL

- **FR-014**: Each active environment MUST be reachable through a unique public URL that resolves only to that environment. The URL is **openly reachable** — anyone who possesses the link can load the app without authenticating to the platform. Privacy depends on the link being unguessable.
- **FR-015**: Preview URLs MUST support hot reload of front-end changes so the browser reflects code edits without manual refresh.
- **FR-016**: Preview URLs MUST stop resolving to a live app once the environment ends; previously issued URLs must not leak to a different environment in the future.
- **FR-017**: Preview URLs MUST NOT require the user to configure ports, domains, TLS, or networking for their project.
- **FR-018**: The platform MUST inject the environment's public hostname into the project at startup via a documented contract, so project code never needs to concatenate domain names or know its own slug.
- **FR-018a**: Preview URL hostnames MUST be derived from a cryptographically unguessable identifier with **at least 56 bits of entropy** — balancing URL unguessability against the DNS-label length cap of 20 characters for the slug. No identifier derived from the chat ID, user ID, project name, or sequential counter is acceptable in the public hostname.

#### Assistant boundaries

- **FR-019**: When the assistant runs actions on behalf of the user, every action MUST be confined to the current chat's environment. The assistant MUST have no tool, name, or identifier that can address another chat's environment.
- **FR-020**: The assistant MUST be denied file-system access outside the current chat's workspace, even when paths resolve through symbolic links.
- **FR-021**: The assistant MUST be prevented from addressing infrastructure services (e.g., the outbound networking sidecar) that it has no legitimate reason to touch.
- **FR-022**: The assistant MUST be pinned to the chat's working branch for git operations and MUST NOT be able to switch to or destroy unrelated branches.
- **FR-023**: When the assistant pushes code, the push credential MUST be scoped to the specific repository bound to the chat's project and MUST expire on the order of an hour.

#### Failure handling

- **FR-024**: When provisioning fails, the user MUST receive a plain-language error in the chat with at least a retry control and a main-menu control. A bare error message with no path forward is not acceptable.
- **FR-025**: Provisioning MUST be resumable: retrying a failed provision MUST pick up from the failed step when possible, not redo successful earlier steps.
- **FR-026**: An environment that ends in a failed state MUST still release its resources on teardown with the same guarantees as a normally-ended environment.
- **FR-027**: Failures MUST be categorised (at minimum: build/install failed, startup failed, networking failed, health check failed, out of capacity, unknown) so operators can triage without reading raw logs.
- **FR-027a**: Transient degradation of an external dependency (preview-URL provider, git push upstream) while an instance is in `running` MUST NOT transition the instance to `failed` or tear it down. The instance MUST remain `running`, background retries MUST continue, and the chat MUST display a non-blocking banner naming the affected capability (e.g., "preview URL temporarily unavailable", "git push queued — upstream degraded").
- **FR-027b**: The degradation banner MUST clear automatically within one minute of the upstream recovering, without requiring the user to take any action.
- **FR-027c**: If an upstream degradation persists longer than an operator-configurable threshold (default 30 minutes) with zero successful retries, the platform MAY escalate the banner to a "prolonged outage" notice that offers the user explicit retry and terminate controls, but MUST still not auto-tear-down the instance.

#### Concurrency and capacity

- **FR-028**: The platform MUST prevent two tasks in the same chat from running concurrently inside the same environment; a new task MUST wait for the previous one to finish.
- **FR-029**: The platform MUST allow tasks in different chats to run fully in parallel, with no shared mutable state between them.
- **FR-030**: When platform capacity is reached, a new chat's provisioning attempt MUST fail with a clear "at capacity, try again later" error, rather than silently queueing or degrading another chat.
- **FR-030a**: The platform MUST enforce a per-user cap on concurrent active environments. Default value is 3; the cap MUST be operator-configurable without a code change. When a user attempts to start a new environment while at their cap, the attempt MUST fail with a distinct "too many active chats — end one to start another" error that is unambiguously different from FR-030's platform-wide capacity error, and MUST NOT silently queue, reuse, or re-bind an existing environment.
- **FR-030b**: The per-user cap error MUST include navigation controls so the user can see their currently-active chats (to pick one to end) and a main-menu fallback.

#### Security

- **FR-031**: Environments MUST accept inbound traffic only through the sanctioned public URL path. They MUST NOT publish ports on the host.
- **FR-032**: Credentials issued per environment (push token, preview tunnel credentials, database password, heartbeat secret) MUST NOT be baked into images, MUST NOT appear in logs in plain text, and MUST be destroyed when the environment ends.
- **FR-033**: Logs that reach the user or the assistant MUST have secrets redacted before display. The same redactor MUST be used on both paths to prevent one path from leaking what the other hides.

#### Backwards compatibility

- **FR-034**: Projects created under the prior shared-environment model MUST continue to work exactly as before, with no behavioural change, until they are explicitly migrated or retired.
- **FR-035**: The per-chat model MUST be the default for newly created projects.
- **FR-036**: The decision of which model a given chat uses MUST be made from project configuration at runtime, not by duplicating call sites.

### Key Entities *(include if feature involves data)*

- **Instance**: A dedicated development environment tied to exactly one chat. Carries a stable identifier used everywhere (DNS, audit logs, volumes), a current status, timestamps for creation/last-activity/expiry/termination, the branch of code the chat is working on, and (when failed) a structured failure reason.
- **Instance Tunnel**: The public URL(s) and outbound-only networking credentials that make an instance reachable to its user. Exactly one active tunnel per instance. Carries hostnames for the app and for hot-reload traffic, a reference (not the raw contents) to its credentials, and a health status.
- **Chat Session → Instance binding**: Each chat knows which instance it is currently bound to, if any. When an instance ends, the chat's binding is cleared; the next message creates a new instance and re-binds.
- **Task → Instance history**: Every task the assistant runs is recorded against the instance it ran inside, so history per instance is queryable.
- **Credentials (per instance)**: Short-lived tokens scoped to this instance only: push token for git, heartbeat secret for activity reporting, database password for the instance's local database, tunnel credentials for the public URL. All destroyed at teardown.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Two users working concurrently in separate chats NEVER see each other's running apps, files, processes, or credentials. Zero tenancy leaks in a week of automated multi-chat load testing.
- **SC-002**: Time from a user's first message in a new chat to a fully working preview URL is under 2 minutes for a standard project on a warm host, and under 5 minutes on a cold host.
- **SC-003**: After 24 hours of inactivity plus the grace window, 100% of idle environments are torn down with no leftover processes, storage, or publicly reachable URLs.
- **SC-004**: Resuming a chat whose environment was torn down presents the user with a working new environment in under 2 minutes, with their last working-branch state intact.
- **SC-005**: Front-end code edits made by the assistant show up in the user's preview browser within 3 seconds without manual refresh, for at least 95% of edit events.
- **SC-006**: On a single standard host, the platform sustains at least 50 concurrent active environments without provisioning failures attributable to capacity.
- **SC-007**: Provisioning failures always surface to the user with both a retry control and a navigation control present; zero "dead-end" error states reach users.
- **SC-008**: Over any 30-day window, at least 99% of provisioning attempts either succeed or fail with a categorised reason (not "unknown").
- **SC-009**: Zero successful cross-chat operations are recorded by the platform's audit trail in a week of adversarial testing (assistant deliberately prompted to target another chat's environment).
- **SC-010**: No credential material (push tokens, tunnel credentials, database passwords) appears in user-visible or assistant-visible logs, verified by automated scan of log streams.

## Assumptions

- **Preview URLs are shareable-by-link**: Clarified in Q1 (2026-04-23). The URL is public; anyone who receives the link can load the app. Auth-gating the preview (via platform session, signed tokens, or per-project toggles) is a possible future enhancement but is NOT in scope for v1.
- **Per-user concurrent instance cap defaults to 3**: Clarified in Q2 (2026-04-23). Operator-configurable. No cross-chat instance reuse or re-binding in v1 — isolation is stronger than efficiency. Per-tier quotas (free/pro/enterprise) are future work.
- **Chat-lifetime retention for instance data**: Clarified in Q3 (2026-04-23). Terminated instance metadata, tunnel history, task history, audit logs, and the chat's working branch are kept until the chat is deleted by the user. No time-based auto-purge. Operator tooling for bulk cleanup, tiered audit-log archival (e.g., 90-day compression), and compliance-driven deletion windows are future work.
- **Idle-grace-window default is 60 minutes**: Clarified in Q5 (2026-04-23). Operator-configurable. Matches the number cited in the architecture doc §6.
- **Scope of v1 is single-host deployment**: The platform runs on one host with a shared orchestrator. Multi-host scale-out is explicitly out of scope for v1, even though the data model does not preclude it.
- **Laravel + Vue is the reference project template**: The first template the feature supports end-to-end is Laravel back end + Vue front end with hot reload. Other stacks are future work.
- **Public URLs are served via a managed edge service**: The platform relies on a hosted edge networking service (Cloudflare Tunnel) for public URLs. Running without it or swapping providers is out of scope for v1.
- **Legacy host-mode and docker-mode projects coexist**: The two prior modes continue to work, via existing code paths, during and after this rollout. Their eventual deprecation is a separate decision cycle.
- **Project git repositories are hosted on GitHub**: Short-lived push credentials rely on a GitHub App installed into the target repositories. Non-GitHub hosting is out of scope for v1.
- **Per-instance in-browser IDE is optional**: The feature reserves a URL slot for an in-browser IDE surface but does not require one to ship; initial release may expose only the running app.
- **Idle threshold is globally 24 hours**: Per-project overrides (shorter or longer) are a follow-up, added the first time a customer asks.
- **Cross-chat sharing of the same project's instance is a non-goal for v1**: Two chats of the same project get fully separate environments. Whether they should eventually be able to share is an open product question deferred to a later release.
- **Raw HTTP traffic to the preview URL is not an activity source in v1**: Users who only browse (never chat, never use the IDE surface, never run the dev server) will eventually time out. If this becomes a real complaint, a lightweight access-log-based activity source can be added later.
- **Automated provisioning budget of ~200 MB idle / ~1 GB active per environment**: Capacity plans assume these back-of-envelope figures for sizing hosts and setting limits. Real usage may move the numbers without invalidating the feature.
