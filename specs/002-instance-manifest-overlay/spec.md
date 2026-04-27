# Feature Specification: Project-Owned Instance Manifest with Platform Overlay

**Feature Branch**: `002-instance-manifest-overlay`
**Created**: 2026-04-26
**Status**: **DEFERRED** — see "Why this is deferred" below.
**Input**: User description: "Refactor per-chat-instances container provisioning so the project owns its own runtime (its own docker-compose.yml + Dockerfile) and the platform owns only the infra overlay (cloudflared sidecar, projctl sidecar, network joining, port stripping, secret injection). Stop shipping platform-owned application stacks; the cloned project is the substrate."

## Why this is deferred (2026-04-26)

This spec was authored mid-`/e2e-pipeline` debug to address what looked like a fundamental architectural problem (the `tagh/laravel-vue-app:latest` image was missing). On closer inspection, the underlying gap was the platform never actually built and published the image that T056 of [specs/001-per-chat-instances/tasks.md](../001-per-chat-instances/tasks.md) called for. Spec 001 still has open tasks (Phase 10 chat UI compat, Phase 11 admin dashboard) and the proper fix was completing T056 inside that milestone plan — not a parallel redesign.

The architectural ideas in this spec (project-owned manifest, platform-owned overlay, `.tagh/instance.yml` + `docker-compose.override.yml`) remain sound. They are appropriate for a future amendment to spec 001 once Phase 10 + 11 are landed and the platform has multiple onboarded projects with diverse compose shapes. Until then:

- T056 is being completed by building a proper `tagh/laravel-vue-app:latest` image (`serversideup/php:8.4-fpm-nginx-alpine` + projctl baked in), inside the existing spec-001 architecture.
- The compose template now also ships meilisearch + mailpit so common Laravel-Sail-shaped projects boot without project-side compose changes.
- The bind-mount path resolution (`WORKSPACE_HOST_DIR` env + `_cloudflared.yml` host path) was added to handle the orchestrator-runs-in-container deployment topology.
- The supporting services + render path are documented in the live `setup/compose_templates/laravel-vue/` directory.

**Do not invoke `/speckit-implement` against this spec.** Pick the work back up only if Phase 10 + 11 of spec 001 are complete and a project-bring-your-own-compose pattern is genuinely needed at that point.

## Clarifications

### Session 2026-04-26

- Q: Where does an auto-detected manifest live after the user clicks Confirm, so subsequent chats against the same project don't re-confirm from scratch? → A: **Nowhere on the platform side.** Confirm performs two atomic actions: it uses the manifest for the current provision AND opens a pull request against the project's repository adding the manifest file. The project's repo is the only source of truth for manifest content. If the PR is merged, future chats read the in-repo manifest via the standard path (FR-002) without re-confirmation. If the PR is not merged, future chats re-show the proposal — which is correct, because the project has not yet adopted the manifest. The platform does not cache manifests in its own database. This moves auto-PR-back from out-of-scope into the core feature.

- Q: When the platform shows an inferred manifest proposal, what does the "Edit" affordance do? → A: **There is no inline manifest editor in chat.** Editing manifest content in the chat would re-create the same two-homes drift problem the Q1 answer just removed. The chat surfaces exactly two actions on a proposal: Confirm-as-shown (provision + open PR-back) or "I'll add it myself" (cancel provision, copy the proposed manifest YAML to the chat as a copy-pasteable block plus a link to the well-known in-repo path documentation, so the user can author it in their normal editor and re-run the chat afterwards). Editing manifest content always happens in the developer's editor against the developer's repo, never in the chat. This keeps the chat UI minimal and keeps the repo as the only authoring surface.

- Q: Where on disk does the platform-generated overlay file live, given FR-013's "alongside the worktree but not in the project's git status"? → A: **Outside the worktree entirely, in a sibling platform-owned directory under the per-instance root.** Concretely the per-instance root layout is `/workspaces/inst-<slug>/{worktree, _platform}` where `worktree/` is the project's git worktree (the bind-mount target for the project's compose) and `_platform/` holds the generated override file plus any other platform-only sidecar configs. Compose-up uses absolute paths — `docker compose -f <worktree>/docker-compose.yml -f <_platform>/override.yml up -d` — so the override doesn't have to share a directory with the substrate. The worktree contains zero platform-generated files; `git status` inside the worktree shows only the project's own changes. Teardown removes both subdirectories independently. This is materially better than the inside-but-git-excluded alternative: it survives `git clean -fdx`, makes the platform/project boundary inspectable, and isolates the platform's state from any worktree corruption caused by agent activity.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Onboard an existing repo and watch the chat boot it (Priority: P1)

A platform user already has a Laravel/Vue (or similar) project on GitHub
that runs locally with `docker compose up`. They want to use TAGH so an AI
assistant can iterate on it inside an isolated chat instance, with a public
preview URL, without re-platforming the project.

The user adds a small in-repo file describing which service receives
ingress and which commands to run after boot. They open a new chat, pick
the project, type a first message — and within ~90 seconds get a working
live URL serving their actual app, plus an assistant ready to make changes.

**Why this priority**: This is the entire feature. Today, platform
provisioning fails on every Sail/Compose project because the platform tries
to swap in its own (non-existent) image instead of running the project's
own stack. Without this story working, the per-chat-instances feature is
not usable on real-world projects.

**Independent Test**: Add the manifest file to a stock Laravel Sail repo,
trigger a provision via the chat UI, and confirm the public URL serves
real Laravel HTML within the budget. No platform code changes required to
move the test from one Sail project to another.

**Acceptance Scenarios**:

1. **Given** a project with an in-repo manifest declaring its primary
   service, ingress port, and bootstrap commands, **When** the user sends
   a first message in a new chat tied to that project, **Then** the chat
   shows a "starting environment" banner, the platform boots the project's
   own compose stack augmented only by an infra overlay, and within the
   provisioning budget the chat shows the live URL and the URL serves real
   application HTML (not a placeholder, not a 5xx).

2. **Given** the same project, **When** a second chat is opened against it,
   **Then** that chat boots an independent instance with a different slug
   and different public URL, and the two run side by side without
   interfering.

3. **Given** a running instance, **When** the user terminates the chat,
   **Then** the project's compose stack tears down cleanly, the named
   tunnel is destroyed, no per-instance containers remain, and the chat
   shows a "destroyed" state.

4. **Given** the project's compose lists host port bindings (a normal
   local-dev setup), **When** the platform provisions an instance,
   **Then** no host ports are published — ingress is reachable only via
   the named tunnel.

---

### User Story 2 — First-time onboarding without writing the manifest (Priority: P2)

A platform user wants to try TAGH on an existing project but doesn't want
to write a manifest file before they've even seen the platform work. They
open a chat against the project as-is.

The platform inspects the cloned repo, recognizes a known framework shape
(e.g. Laravel + Sail), and proposes a manifest in the chat with a clear
rationale ("we saw `composer.json` and `laravel/sail` in your compose, so
the primary service is `laravel.test` on port 80"). The user has two
choices: **Confirm** the proposal as shown (which both starts provisioning
AND opens a pull request against the project's repository adding the
manifest file), or **"I'll add it myself"** (which cancels provisioning,
shows the proposed manifest as a copy-pasteable block, and points at the
well-known in-repo path so the user can author the manifest in their
editor and re-open the chat afterwards). The chat is not a manifest
editor; manifest content is always authored in the developer's repo.

**Why this priority**: Reduces onboarding friction for first-time users.
Optional for the core flow (P1 covers the case where the manifest already
exists), so it can ship in a follow-up if scope pressure demands.

**Independent Test**: Open a new chat against a project that has no
manifest. Confirm the platform shows a proposed manifest with rationale
and does not provision until the user confirms. Confirm rejecting the
proposal aborts provisioning cleanly with no orphaned infra.

**Acceptance Scenarios**:

1. **Given** a project with no in-repo manifest, **When** the user starts
   a new chat against it, **Then** the chat shows a proposed manifest
   with the rationale for each inferred field plus exactly two actions:
   **Confirm** (proceed with this manifest and open a PR adding it to
   the repo) and **"I'll add it myself"** (cancel and copy the proposed
   manifest YAML to chat for the user to author in their editor).

2. **Given** a proposed manifest displayed in the chat, **When** the user
   clicks Confirm, **Then** provisioning proceeds using the proposed
   manifest exactly as shown AND a pull request is opened against the
   project's repository adding the manifest file at the well-known path,
   with a link to the PR shown in the chat.

3. **Given** a proposed manifest displayed in the chat, **When** the user
   clicks "I'll add it myself", **Then** no instance is created, no
   tunnel is allocated, no pull request is opened, the chat shows the
   proposed manifest YAML as a copy-pasteable block plus a pointer to
   the well-known in-repo path, and the chat returns to a state where
   re-sending the first message will re-trigger detection.

5. **Given** an inferred manifest was previously confirmed but the
   resulting pull request has not been merged, **When** the same project
   owner (or another user with access) starts a new chat against the
   same project, **Then** the chat re-shows the manifest proposal — the
   project has not yet adopted the manifest, so the platform does not
   silently treat any cached value as authoritative.

6. **Given** a project where a previously-opened TAGH manifest pull
   request has been merged into the default branch, **When** any user
   starts a new chat against the project, **Then** the chat skips the
   proposal step entirely and provisions directly from the in-repo
   manifest.

4. **Given** a project whose shape doesn't match any built-in heuristic,
   **When** the user starts a new chat, **Then** the chat tells the user a
   manifest could not be inferred and points them at the manifest
   documentation, rather than proceeding with low-confidence guesses.

---

### User Story 3 — Project owner keeps local dev unchanged (Priority: P2)

The project owner runs the same repo locally with their existing tooling
(`docker compose up`, etc.) and expects nothing about the local workflow
to change after they add the TAGH manifest.

**Why this priority**: Protects the contract that TAGH is additive, not
intrusive. If onboarding TAGH breaks local dev, project owners will not
adopt it.

**Independent Test**: After adding the manifest to the repo, run
`docker compose up` in a clean local checkout and verify the project
boots exactly as before — same services, same ports, same behavior.
Verify no TAGH-only files (overlay, sidecars) need to exist in the repo.

**Acceptance Scenarios**:

1. **Given** a project with a TAGH manifest committed, **When** the owner
   runs the project's own boot command locally, **Then** the project
   boots identically to a checkout without the manifest — no missing-file
   errors, no service-name conflicts, no port changes.

2. **Given** a project with a TAGH manifest committed, **When** the owner
   inspects the repo, **Then** the only TAGH-specific artifact in the
   repo is the manifest file itself; no platform-generated files
   (overlay, secrets, generated configs) are committed or expected to
   be in `.gitignore`. Inside a live instance's worktree, `git status`
   on the worktree never reports platform-generated files because they
   live in a sibling platform-owned directory, not inside the worktree.

---

### User Story 4 — Platform stops shipping project stacks (Priority: P3)

A platform maintainer adds support for a new project type (e.g. a Django
repo). With this feature, they don't need to build a Django-specific
container image, write a Django-specific compose template, or maintain
those artifacts going forward — the project supplies its own stack.

**Why this priority**: Operational sustainability. Each shipped project
template is a maintenance liability; eliminating that category removes a
category of work.

**Independent Test**: After this feature lands, confirm the codebase
contains no platform-owned application container images, no
framework-specific compose templates, and no per-framework user-app
Dockerfiles shipped under platform-owned directories.

**Acceptance Scenarios**:

1. **Given** the platform repo, **When** a maintainer searches for shipped
   application stacks, **Then** they find none — the only platform-owned
   compose artifact is the overlay generator and its sidecar definitions.

2. **Given** a new framework appears in the wild, **When** a project of
   that framework is onboarded with a manifest, **Then** no platform code
   change is required for it to work — the manifest declares everything
   the platform needs to know.

---

### Edge Cases

- **Missing manifest, no recognized shape**: When the cloned repo doesn't
  match any auto-detection heuristic, the chat must surface a clear
  actionable message (point at manifest docs) rather than proceed with a
  low-confidence guess or silently fail at compose-up.
- **Manifest declares a service that isn't in the project's compose**:
  The platform must refuse to provision before any container starts,
  surface the discrepancy ("primary_service `laravel.test` not found in
  your compose"), and not allocate a tunnel.
- **Manifest declares a port that the primary service doesn't expose**:
  Same — refuse early with a specific error, no tunnel allocated.
- **Project's compose references images that need authentication**: The
  platform forwards the docker pull error to the chat as a structured
  failure with retry, and does not silently fall back to a different
  image.
- **Project's compose binds host ports**: The overlay strips those
  bindings at provision time; the project's own compose file is not
  modified. Local-dev binds remain intact for the project owner.
- **Bootstrap command fails (e.g. `composer install` errors)**:
  Provisioning is marked failed with the failing command surfaced in the
  chat. The instance is not left in a half-booted state — either the
  boot completes or teardown runs.
- **Manifest is malformed YAML or violates schema**: Refuse to provision
  with a clear "manifest is invalid: <reason>" message in the chat. No
  tunnel allocated, no compose-up attempted.
- **Two chats open against the same project simultaneously while the
  manifest is being inferred**: Auto-detection proposes the same manifest
  to both, but each chat confirms independently. Two confirmed instances
  boot side by side without sharing state.
- **Project's compose declares its own service that collides with a
  platform-reserved sidecar name**: The overlay generator must detect
  the collision and refuse to provision rather than overwriting the
  project's service definition.
- **Old projects already onboarded with the prior platform-template
  flow**: Those projects must continue to provision during the rollout
  window (feature flag default), but show a deprecation indicator that
  points the owner at the manifest path.
- **Manifest pull request fails to open on Confirm**: Provisioning for
  the current chat still proceeds with the proposed manifest, but the
  chat surfaces the PR-back failure with actionable detail (e.g.
  "credentials lack repo write access — add the manifest manually at
  `.tagh/instance.yml`"). The next chat will re-show the proposal
  unless the manifest is added to the repo by some other means.
- **Manifest pull request is opened but never merged**: Subsequent
  chats re-show the manifest proposal. The platform does NOT treat the
  unmerged PR as a confirmation. This is intentional: the merge is the
  signal that the project has adopted TAGH.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The platform MUST treat the cloned project's own compose
  stack as the runtime substrate for container-mode chats. The platform
  MUST NOT ship, build, or pull a platform-owned application image as
  the primary service.

- **FR-002**: The platform MUST read a project-owned manifest at a
  well-known in-repo path describing: the substrate compose file, the
  primary service that receives ingress, the ingress ports (web, optional
  HMR, optional IDE), the post-boot commands to run inside the primary
  service, the env variables the project requires, and the env variables
  the project expects to inherit at compose-up time.

- **FR-003**: The platform MUST generate a per-instance overlay file
  containing only platform-owned concerns (tunnel sidecar, lifecycle
  sidecar, instance-scoped network attachment, host-port stripping for
  non-tunnel services) and MUST compose it with the project's substrate
  via the standard layered compose mechanism. The platform MUST NOT
  modify the project's own compose file on disk.

- **FR-004**: The platform MUST inject per-instance secrets (database
  password, Git credentials, lifecycle HMAC secret, tunnel credentials)
  into the compose-up environment at boot time, and MUST NOT write any
  secret value into the overlay file or any other on-disk artifact in
  the workspace.

- **FR-005**: The platform MUST refuse to provision and surface a clear
  error in the chat when the manifest is missing without an
  auto-detected proposal, malformed, references a non-existent service
  or port, or collides with platform-reserved service names. No tunnel,
  network, or container resource may be allocated when manifest
  validation fails.

- **FR-006**: When no manifest is present in a repo, the platform MUST
  attempt rule-based auto-detection from project-shape signals (file
  presence, dependency declarations, compose-file contents) and MUST
  surface the proposed manifest plus the rationale for each inferred
  field to the user in the chat before provisioning starts.

- **FR-007**: Auto-detection MUST NOT silently provision based on
  inferred values. The user MUST confirm the proposed manifest in the
  chat before any container resource is allocated. The chat MUST
  surface exactly two actions on a proposal: Confirm (provision +
  PR-back) and "I'll add it myself" (cancel + show proposed YAML for
  the user to author against their repo). The chat MUST NOT offer an
  inline manifest editor — manifest content is always authored in the
  developer's repo.

- **FR-008**: When auto-detection cannot reach a usable manifest with
  reasonable confidence, the platform MUST surface a manual-onboarding
  message (point at the manifest path and documentation) rather than
  proceeding with a low-confidence guess.

- **FR-009**: The platform MUST run the manifest's post-boot commands
  inside the primary service after compose-up succeeds and before
  declaring the instance running. A bootstrap-command failure MUST
  surface in the chat as a structured failure with the failing command
  and its output (subject to credential redaction).

- **FR-010**: After provisioning, the public ingress URL MUST be reachable
  only through the per-instance named tunnel. No service in the booted
  stack — other than the tunnel sidecar's internal metrics, where
  applicable — may publish a host port. This MUST hold true regardless
  of what the project's own compose declares.

- **FR-011**: The platform MUST regenerate the overlay file on every
  provision (it is not stored as durable state). Re-running provision
  on a partial-success state MUST converge to the same end-state as a
  first-time provision, without duplicating tunnels, networks, or
  container resources.

- **FR-012**: Termination MUST tear down both the project's substrate
  and the platform's overlay (using a single layered teardown), destroy
  the named tunnel, remove the workspace, and mark the instance row
  destroyed. Termination MUST be idempotent: a second termination on an
  already-destroyed instance MUST succeed as a no-op.

- **FR-013**: The platform MUST NOT modify the project's repository on
  disk except via the workspace tools used by the chat agent. The
  per-instance root MUST be split into two sibling subdirectories: a
  worktree subdirectory holding the project's git checkout (and only
  that), and a platform-owned subdirectory holding the generated
  overlay file and any other platform-only artifacts. The compose-up
  invocation MUST reference both via absolute paths. The worktree
  subdirectory MUST contain zero platform-generated files, so
  `git status` inside it never reflects platform state.

- **FR-014**: The platform MUST support running multiple independent
  chats against the same project simultaneously, each with its own slug,
  network, tunnel, and worktree. Two such chats MUST NOT share any
  mutable state (volumes, networks, tunnels).

- **FR-015**: For projects already onboarded under the prior
  platform-template flow, the existing provision path MUST remain
  available during the rollout window. Switching a project to the new
  flow MUST be safe to attempt — an unrecoverable state means refuse to
  provision, not partial migration of an existing instance.

- **FR-016**: Once the new flow is the default, the platform MUST stop
  shipping framework-specific application stacks (compose templates,
  app images, Dockerfiles for user applications). Greenfield project
  scaffolding (creating a new repo with a starter manifest from a
  template) is a separate user-facing capability and is out of scope
  for this feature.

- **FR-017**: When a user confirms an inferred manifest proposal, the
  platform MUST, in the same atomic action: (a) start provisioning with
  that manifest, AND (b) open a pull request against the project's
  default branch adding the manifest file at the well-known in-repo
  path. The PR title, body, and target branch are platform-determined
  but MUST be reviewable and reversible by the project owner like any
  normal PR.

- **FR-018**: The platform MUST NOT cache, persist, or otherwise retain
  inferred manifest content in its own data stores between chats.
  Provisioning data already stored on the per-instance row (slug,
  network, tunnel, workspace path) is unchanged; manifest content
  itself lives only in (a) the in-repo file when present and (b) the
  pull request opened on Confirm. Future chats either read from the
  in-repo file or re-run auto-detection; they do not read cached
  manifest content from the platform's database.

- **FR-019**: When the platform fails to open the manifest pull request
  on Confirm (network error, insufficient credentials, branch
  protection rejects the push), the chat MUST surface the failure with
  enough context for the user to retry or to add the manifest manually.
  Provisioning for the current chat MUST still proceed using the
  proposed manifest — the PR-back failure is a follow-up problem, not
  a provision-blocker for the current chat.

### Key Entities *(include if feature involves data)*

- **Instance Manifest**: A project-owned declarative document committed
  in the project's repository. Identifies which service in the project's
  compose stack receives platform-managed ingress, which ports map to
  web and optional HMR/IDE roles, which commands to run after the stack
  is up, which env vars the project requires, and which env vars the
  platform should inherit at compose-up time. Versioned via an
  `apiVersion` field so future schema changes are explicit.

- **Inferred Manifest Proposal**: A short-lived value produced by the
  auto-detector when no in-repo manifest exists. Carries the proposed
  manifest, the confidence level, and the rationale for each inferred
  field. Surfaced in the chat for user confirmation. On Confirm, its
  content moves into a pull request against the project's repository;
  the platform does not retain a copy. On Cancel, it is discarded.

- **Instance Overlay**: A platform-generated file rendered into the
  per-instance platform-owned subdirectory at provision time (sibling
  to, not inside, the project's worktree). Carries only platform-owned
  concerns (tunnel sidecar, lifecycle sidecar, instance-scoped network,
  port stripping). Composed with the project's substrate at compose-up
  time via absolute paths. Removed on teardown; regenerated on every
  provision; never carries secret values; never appears in the
  project's git status.

- **Per-Instance Runtime**: The live instance's actual container set,
  composed from the project's substrate and the platform's overlay.
  Bound by slug, network name, tunnel id, and workspace path. Owns no
  durable state outside the project's own volumes; everything platform-
  owned lives in the per-instance database row.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An onboarded project's first chat produces a publicly
  reachable live URL within 90 seconds of the user's first message, and
  the URL serves the project's actual application HTML (not a placeholder,
  not a 5xx).

- **SC-002**: Onboarding a new project to TAGH requires only changes
  inside the project's repository (adding the manifest file). It
  requires no platform code change, no platform release, and no
  platform-side configuration entry per project.

- **SC-003**: A project owner can run the onboarded project locally with
  the same tooling and same boot command they used before adopting TAGH.
  No commit-time files added by the platform appear in the project's
  repo, and no local-dev workflow change is required.

- **SC-004**: An external observer scanning host ports on the
  orchestrator finds no application service exposed — all ingress
  arrives via the per-instance named tunnel. This holds even when the
  project's own compose file binds host ports for local-dev convenience.

- **SC-005**: When a chat is terminated, within 30 seconds all of the
  following are true: no per-instance containers run on the host, the
  named tunnel for that instance is destroyed, the workspace directory
  is removed, and the instance database row is marked destroyed.

- **SC-006**: When a manifest is malformed, the user receives a clear
  diagnostic message in the chat that names the invalid field and the
  reason within 5 seconds of sending the first message — and no tunnel,
  network, or container is allocated.

- **SC-007**: When the platform receives a chat for a project with no
  manifest, the user sees a proposed manifest with rationale within 10
  seconds; the user's Confirm or Cancel decision is the gate before any
  container resource is allocated.

- **SC-007a**: After the user clicks Confirm on an inferred manifest
  proposal, a pull request adding the manifest file to the project's
  repository is opened within 30 seconds; the chat displays the PR
  link. Provisioning of the current chat does not block on the PR open
  (a PR-open failure surfaces in chat without blocking the live
  instance).

- **SC-007b**: Once a manifest pull request is merged, no further
  inferred-manifest proposals appear for chats against that project.
  The platform reads the in-repo manifest on the next provision and
  proceeds without prompting.

- **SC-008**: Two chats opened against the same project boot independent
  instances. They run side-by-side without state contamination —
  destroying one does not affect the other, and bootstrap commands run
  in each don't interfere with the other's state.

- **SC-009**: After the rollout completes, the platform repository
  contains no shipped application container images, no
  framework-specific compose templates, and no framework-specific
  user-application Dockerfiles under platform-owned directories. A
  code-search for the prior template path returns no live references.

- **SC-010**: Onboarding a second framework (e.g. moving from a Sail
  project to a Next.js project) requires only adding a manifest to that
  project's repository — no platform code, no platform release, no
  platform configuration change.

## Assumptions

- The cloned project is expected to ship its own compose file (or
  equivalent layered compose set) that successfully boots the project
  locally. Projects that don't ship a usable compose stack are out of
  scope for this feature; greenfield-project scaffolding is a separate
  workstream.

- The cloned project either ships its own Dockerfile(s) or references
  off-the-shelf images its compose can pull. The platform does not
  provide application base images.

- The project owner is willing to add a small in-repo manifest file
  (committed in their own repo) as the explicit declaration of where
  platform-managed ingress lands. Manifests committed by the project
  owner are the source of truth; auto-detection is a convenience for
  first-time onboarding only.

- Auto-detection ships with rule-based heuristics for the most common
  shapes encountered today (notably stock Laravel Sail). Other framework
  heuristics (e.g. Next.js, Rails) are deferred follow-ups; their
  absence does not block this feature, because the manual-onboarding
  path covers them.

- The existing per-instance lifecycle primitives (tunnel sidecar
  pattern, lifecycle helper, named-tunnel allocator, workspace worktree
  pattern, instance database schema) are reused as-is. This feature
  changes how the application stack is composed and booted, not how the
  platform's own infra primitives work.

- Secret-redaction in chat-visible logs is already in place via the
  existing audit-service redactor; this feature does not introduce new
  secret types or new visibility surfaces.

- A short rollout window with a feature flag is acceptable so the prior
  platform-template flow can remain available during cutover. Once the
  new flow is default and validated against the canonical test project,
  the prior flow is deleted in a follow-up commit.

- Project-owner education (manifest documentation, examples) is a
  prerequisite for general adoption but is treated as documentation
  work outside this feature's scope. The feature ships with at least
  one worked example committed to the canonical test project.
