# Implementation Plan: Project-Owned Instance Manifest with Platform Overlay

**Branch**: `002-instance-manifest-overlay` *(work currently rides on `multi-instance` until cutover; the spec dir + plan are branch-independent)*
**Date**: 2026-04-26
**Spec**: [spec.md](spec.md) · **Clarifications**: [spec.md §Clarifications](spec.md#clarifications) · **Quality checklist**: [checklists/requirements.md](checklists/requirements.md)
**Input**: Feature specification from `specs/002-instance-manifest-overlay/spec.md`

## Summary

Replace the current "platform ships a Laravel/Vue compose template per project type" model with a project-owned instance manifest plus a platform-owned compose overlay. The cloned project's own `docker-compose.yml` becomes the runtime substrate; the platform writes only a sidecar overlay (cloudflared + lifecycle helper + network join + port stripping) into a sibling per-instance platform-only directory and composes the two via the standard layered `docker compose -f a -f b` mechanism. Inferred manifests (auto-detected on first chat for projects with no manifest) are not cached on the platform side — Confirm opens a pull request against the project's repo, making the repo the only source of truth.

## Technical Context

**Language/Version**: Python 3.12 async (constitution Architecture Constraints — orchestrator/API/workers are all asyncio).
**Primary Dependencies**: FastAPI (API), ARQ (worker job runtime), async SQLAlchemy + asyncpg (Postgres), redis.asyncio (ephemeral state), httpx (egress HTTP), aiogram + slack-bolt (chat providers, untouched by this feature), claude_agent_sdk (LLM agent, untouched), PyYAML 6.0.3 (already installed — manifest + compose parsing), jsonschema 4.26.0 (already installed — manifest schema validation).
**Storage**: PostgreSQL holds platform state only (`instances`, `instance_tunnels`, `platform_config` rows). No new tables for manifest content — the project's repo is the only durable home for manifests (per spec.md FR-018).
**Testing**: pytest with `pytest-asyncio` (existing). Unit tests under `tests/unit/`, contract tests under `tests/contract/`, integration tests under `tests/integration/`. Static fitness checks under `scripts/fitness/check_*.py` (existing pattern).
**Target Platform**: Linux server hosting the docker compose orchestrator stack; per-instance containers run on the same docker daemon. macOS dev workstations are supported via Docker Desktop with the same compose layout.
**Project Type**: Single backend service (extends the existing FastAPI + ARQ Python backend). No new frontend code — chat frontend already supports the `instance_*` event family per phase 10 of the prior feature; this plan adds two new event sub-types (`manifest_proposal`, `manifest_pr_opened`) to that same family rather than creating a new event surface.
**Performance Goals**: SC-001 90s end-to-end provision (p95), SC-005 30s teardown (p95), SC-006 5s manifest-validation feedback (p95), SC-007 10s manifest-proposal preview (p95), SC-007a 30s PR-open latency (p95). All measured from chat-message-send.
**Constraints**: Constitution-mandated — Principle V (no host ports outside cloudflared sidecar, regardless of what the project's own compose declares); Principle IV (secrets via env at compose-up only, never on disk); Principle III (no manifest content reaches MCP tool argument schemas); Principle IX (every external call awaited, every httpx client carries a timeout, no orphan `create_task`); Principle VIII (any failing pre-commit / fitness / test signal must be fixed at root, not bypassed).
**Scale/Scope**: Per-user concurrent-instance cap unchanged from spec 001 (FR-014 there). Manifest size cap is the GitHub PR-body size cap (which the platform doesn't approach); compose-file size cap is the docker compose CLI's own limit. No new scale dimension introduced.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Compliance verdict | Evidence |
|---|---|---|
| **I — Per-Chat Instance Isolation** | PASS | Each instance still gets its own compose project, network, slug, named tunnel, workspace. Two-layer cross-tenancy defense (MCP argv-pinning + per-repo PAT scoping) unchanged. The `chat_session_id` partial-unique-on-active-status invariant on `instances` is unchanged. |
| **II — Deterministic Execution Over LLM Drift** | PASS | Manifest is declarative YAML; overlay generator is a pure function `(substrate_compose, manifest, instance_meta) → override_yaml`; auto-detector is rule-based, not LLM-driven. The LLM is still only on the failure-fallback path (existing `projctl explain`), which this feature does not modify. |
| **III — No Ambient Authority for Agents** | PASS | Manifest content stays inside the orchestrator process (workspace_service / instance_overlay_service). It never enters `mcp__instance__*`, `mcp__workspace__*`, or `mcp__git__*` tool argument schemas. Agents continue to receive only the per-instance argv-pinned MCP fleet built in `providers/llm/claude.py::_mcp_*`. The `no_ambient_args` fitness check stays green. |
| **IV — Credential Scoping & Log Redaction** | PASS | Per-instance secrets (DB password, GH PAT, heartbeat HMAC, CF tunnel token) flow via the parent process env into compose-up, exactly as today. The override file written under `_platform/` carries env-var **references** only (`environment: - DB_PASSWORD`), never values — same pattern the prior `compose_templates/laravel-vue/` already used. The audit-service redactor wraps every chat-emitted tool result; this feature emits two new chat events (`manifest_proposal`, `manifest_pr_opened`) which carry no secret values by construction (manifest YAML is project-public, PR URL is intended visibility) so they enter the same redactor pipeline as a no-op. |
| **V — Egress-Only Network Surface** | PASS | The overlay generator's contract: walk the project's compose, drop `ports:` from every service except the platform-owned `cloudflared` sidecar. The new fitness check `check_overlay_strips_host_ports.py` (Phase 1 deliverable) asserts this against synthetic project compose inputs. Project's own compose is unmodified on disk; the strip happens via override-layer empty `ports:` lists which docker compose treats as a replacement during merge. |
| **VI — Durable State, Idempotent Lifecycle** | PASS | The override file is regenerated on every provision (FR-011). No worker holds a long-lived handle to the override path or to compose state — the source of truth is the disk file under `_platform/` plus `instances` row. Re-running provision on a partial-success state converges (clone is idempotent via the bug-fix already landed; overlay-write is idempotent by overwrite; PR-open is idempotent via "is there already an open PR for branch X" check before creating). Teardown removes the per-instance root + named tunnel + DB row — second teardown is a no-op (existing `teardown_instance` already idempotent). |
| **VII — Verified Work, No Half-Features** | PASS | Vertical slice in one PR: manifest reader + manifest validator + overlay generator + auto-detector + Sail-shape heuristic + chat events for proposal/PR-open + PR-open service + per-instance root layout migration + tagh-fre seeding with manifest + 3 new fitness checks + e2e regression. No half-rolled-out feature flag — the new flow is the default the moment the PR lands; the prior `compose_templates/laravel-vue/` directory is removed in the same PR (so no parallel paths to drift). |
| **VIII — Root-Cause Fixes Over Bypasses** | PASS | The prior `setup/compose_templates/laravel-vue/` template referenced an image (`tagh/laravel-vue-app:latest`) that has never been built, captured in artifacts/e2e-20260426-200532/. Rather than papering over that with "build the image", this feature removes the wrong premise (platform shouldn't ship app stacks) and replaces it with the right one (project owns its stack, platform owns the overlay). No `--no-verify`, no test-deletions, no `# type: ignore`. |
| **IX — Async-Python Correctness** | PASS | All new external calls (GitHub PR-open via the existing `GitHubProvider.create_pr`) reuse the existing `httpx.AsyncClient` factories that already carry timeouts (the `timeouts` fitness check already enforces this). Disk I/O for manifest read + overlay write happens inside `worker/tasks/instance_tasks.py` which is already an ARQ async task; reads/writes are wrapped in `asyncio.to_thread` (PyYAML is sync, can't be helped — wrapping is the constitution-mandated pattern). No `asyncio.create_task` without a held reference is introduced. |

**Initial gate result**: All nine principles pass. No Complexity Tracking entries needed.

## Project Structure

### Documentation (this feature)

```text
specs/002-instance-manifest-overlay/
├── plan.md              # This file
├── spec.md              # Feature spec (with Clarifications block)
├── research.md          # Phase 0 output — decisions on: manifest schema, override-port-strip mechanism, PR-open path, per-instance root layout, sail auto-detection rules
├── data-model.md        # Phase 1 — Manifest, InferredProposal, Overlay, PerInstanceRoot
├── quickstart.md        # Phase 1 — how to add the manifest to a project, how to run the e2e regression locally
├── contracts/
│   ├── manifest.schema.json     # JSONSchema v1 for .tagh/instance.yml
│   └── overlay.schema.json      # JSONSchema for the platform-generated override.yml
└── checklists/
    └── requirements.md  # Spec quality checklist (passed in /speckit-clarify)
```

### Source Code (repository root)

```text
src/openclow/
├── services/
│   ├── instance_manifest_service.py      # NEW — load + validate .tagh/instance.yml from a worktree
│   ├── instance_inference_service.py     # NEW — Sail-shape detector → InferredProposal
│   ├── instance_overlay_service.py       # NEW — generate override.yml from (substrate compose, manifest, instance meta)
│   └── github_pr_service.py              # NEW (or extend providers/git/github.py) — open the manifest PR-back via existing PAT path
├── worker/tasks/
│   └── instance_tasks.py                 # MODIFY — provision_instance: replace template-render call with overlay-write; teardown still removes per-instance root
├── api/routes/
│   └── assistant.py                      # MODIFY — emit `manifest_proposal` event when needed; gate compose-up on Confirm; emit `manifest_pr_opened` after PR-open
├── setup/compose_templates/laravel-vue/  # DELETE in same PR (no template-mode rollout window — atomic switch)
└── chat_frontend/src/
    └── (no new code; existing instance_* event handler accepts the two new sub-types via the codegen-driven discriminated union — proves out the schema/codegen contract)

scripts/fitness/
├── check_no_app_template_shipped.py             # NEW — fails if any framework-specific compose template, app image reference, or per-framework user Dockerfile lives under platform-owned dirs
├── check_overlay_strips_host_ports.py           # NEW — synthetic-input check: feed a compose with host ports through the overlay generator, assert ports gone in the merged result
└── check_manifest_for_container_projects.py     # NEW — every active mode='container' project either has .tagh/instance.yml in its repo head OR an inference-supported shape

specs/001-per-chat-instances/contracts/
└── stream_event.schema.json              # MODIFY — add manifest_proposal and manifest_pr_opened to the event-type enum (drives codegen → frontend exhaustiveness)

tests/
├── unit/
│   ├── test_instance_manifest_service.py        # schema-valid, schema-invalid, malformed YAML, missing file
│   ├── test_instance_inference_service.py       # Sail recognition, ambiguous, unrecognizable
│   ├── test_instance_overlay_service.py         # cloudflared sidecar present, lifecycle sidecar present, ports stripped, secrets-as-references-not-values
│   └── test_github_pr_service.py                # PR-open success, PR-already-exists idempotency, branch-protection-rejection surface
├── contract/
│   └── test_overlay_compose_layering.py         # docker compose -f substrate -f override config emits expected merged document (no actual compose-up)
└── integration/
    ├── test_provision_with_manifest.py          # end-to-end: clone tagh-fre, read manifest, write override, compose up (real docker against test daemon)
    └── test_provision_without_manifest_proposes.py  # auto-detect path: no manifest in repo → proposal event emitted, no compose-up until Confirm

tagh-fre repo (out-of-tree, but seeded as part of this feature):
└── .tagh/
    └── instance.yml                              # Sail manifest, committed via the PR-back flow itself OR seeded directly (decision recorded in research.md)
```

**Structure Decision**: Single-project Python backend extension. No new top-level package; this feature is three new service modules + minor edits to two existing worker/API modules + new fitness checks + tests. Frontend gains zero new code because the existing `instance_*` event handler already routes by discriminator, and adding two new sub-types is a contracts/codegen change that the frontend picks up automatically (proves out the layered-architecture pattern in the CLAUDE.md "Architecture-fitness suite" section).

## Per-instance root layout (canonical)

Captured here because Q3 of the clarify session pinned this and it informs every Phase 1 artifact below.

```text
/workspaces/inst-<slug>/
├── worktree/
│   ├── docker-compose.yml          # project's own — UNTOUCHED on disk
│   ├── .tagh/instance.yml          # project's manifest (when present)
│   ├── (everything else from the project's repo)
│   └── .git/                       # the project's git worktree
└── _platform/
    ├── compose.override.yml        # platform-generated override; references absolute paths back into worktree/
    ├── projctl-config.yml          # lifecycle sidecar config (heartbeat URL, HMAC, boot commands inlined)
    └── cloudflared.yml             # tunnel sidecar config (named-tunnel id + ingress rules)
```

Compose-up command shape (run by the worker, not committed anywhere): `docker compose -p <compose_project> -f /workspaces/inst-<slug>/worktree/docker-compose.yml -f /workspaces/inst-<slug>/_platform/compose.override.yml up -d`. Both paths absolute; relative paths inside each file resolve relative to that file's own directory per docker compose semantics — meaning the project's compose continues to reference its own `vendor/laravel/sail/runtimes/8.4/Dockerfile` correctly, and the override's `cloudflared.yml` reference resolves inside `_platform/` correctly. This is the docker compose default behavior, not a workaround.

## Phase 0 — Outline & Research

Captured in [research.md](research.md). Six concrete decision points resolved before Phase 1:

1. **Manifest schema validation library**: jsonschema 4.26.0 (already installed) vs Pydantic v2 (already installed). Pydantic chosen — gives typed Python objects downstream, integrates with FastAPI response models if we later expose manifest debug endpoints, and keeps the schema definition as a Python class rather than a separate JSON file.
2. **Override port-strip mechanism**: docker compose layered-merge replaces the `ports:` list when the override declares an empty list. Researched + documented behavior across compose v2.20+ (which ships with Docker Desktop + the orchestrator's compose plugin).
3. **PR-open implementation path**: reuse `providers/git/github.py::GitHubProvider.create_pr` (already implemented; uses `gh` CLI under the hood). Add a new method `open_manifest_pr` that wraps it with the manifest-specific commit + branch convention.
4. **Sail auto-detection rules**: `composer.json` + `docker-compose.yml` containing `laravel/sail` substring → high confidence. Primary service inferred as the service with a build context (Sail's `laravel.test` is the only one with `build:`). Web port from the build context's exposed port (Sail defaults to 80).
5. **Per-instance root layout migration**: existing live-instance rows have `workspace_path = /workspaces/inst-<slug>/` (not `/workspaces/inst-<slug>/worktree/`). Migration approach: workspace_path semantics change is a one-time cutover; the schema column stays the same (string) but its meaning shifts. Old in-flight instances are terminated via the standard reaper before the cutover (operational rollout note, not code).
6. **Bootstrap-command execution**: reuse the existing `projctl` lifecycle helper which is already a sidecar in every per-instance compose template. New work: have projctl read its boot commands from `/projctl/config.yml` (mounted from `_platform/projctl-config.yml`) instead of a baked-in template.

**Output**: All six items resolved with citations. No NEEDS CLARIFICATION markers remain. Ready for Phase 1.

## Phase 1 — Design & Contracts

**Prerequisites**: research.md complete (✓).

### Data model — [data-model.md](data-model.md)

Four entities, three platform-side and one project-side:

- **Manifest** *(project-side, in `.tagh/instance.yml`)* — apiVersion, kind, spec.compose, spec.primary_service, spec.ingress.{web,hmr,ide}, spec.boot[], spec.env.required[], spec.env.inherit[]. Pydantic model in `services/instance_manifest_service.py`. JSONSchema export at [contracts/manifest.schema.json](contracts/manifest.schema.json).
- **InferredProposal** *(in-memory, never persisted)* — manifest, confidence (0.0–1.0), reasons (list of "we saw X" strings), source_signals (the file paths that triggered each inference). Lives in chat-event payload from worker → frontend; lives nowhere else.
- **Overlay** *(platform-side, on disk under `_platform/compose.override.yml`)* — generated docker-compose-v3-shape document with: `cloudflared` sidecar service, `projctl` sidecar service, project's primary service overridden with `ports: []` and `networks: [instance]`, all other project services overridden with `ports: []`, top-level `networks.instance`, top-level `volumes` for projctl-state + cloudflared-creds. JSONSchema at [contracts/overlay.schema.json](contracts/overlay.schema.json) — describes the SHAPE of what the generator emits (used by `tests/contract/test_overlay_compose_layering.py`).
- **PerInstanceRoot** *(platform-side, on disk under `/workspaces/inst-<slug>/`)* — directory invariant: must contain exactly two children, `worktree/` and `_platform/`. Documented as a precondition for the overlay generator.

### Contracts — [contracts/](contracts/)

- [contracts/manifest.schema.json](contracts/manifest.schema.json) — JSONSchema for project-authored manifest. Required: apiVersion, kind, spec.compose, spec.primary_service, spec.ingress.web. Optional: spec.ingress.{hmr,ide}, spec.boot, spec.env.{required,inherit}. Fast-failed by `instance_manifest_service.load_and_validate()`.
- [contracts/overlay.schema.json](contracts/overlay.schema.json) — JSONSchema for the override.yml shape the generator emits. Tested by the contract test against synthetic project-compose inputs.
- **Existing contract extension**: [specs/001-per-chat-instances/contracts/stream_event.schema.json](../001-per-chat-instances/contracts/stream_event.schema.json) gains two new event types: `manifest_proposal` and `manifest_pr_opened`. Codegen regenerates `chat_frontend/src/types/instance_events.ts` to add these to the discriminated union, which forces the frontend's exhaustive `switch` to add cases (caught by `tsc` if not). This is the layered-architecture story working as intended — adding the events to the schema breaks `tsc` until the frontend handles them.

### Quickstart — [quickstart.md](quickstart.md)

Two flows documented:
1. **For project owners**: how to add `.tagh/instance.yml` to your repo (with the Sail example for tagh-fre).
2. **For platform devs**: how to run `tests/integration/test_provision_with_manifest.py` against a local docker daemon, and how to run the e2e regression after.

### Agent context update

The `<!-- SPECKIT START --> ... <!-- SPECKIT END -->` block in CLAUDE.md (lines 331–334) currently points at spec 001. Updated by this plan to point at spec 002 + this plan + research/data-model/contracts. Spec 001 stays as the foundation reference; this feature is scoped as a 001 follow-up that replaces a specific subsystem (compose-template renderer → overlay generator).

## Constitution Re-Check (Post-Phase-1 Design)

Re-running the gate now that the data model + contracts + project structure are concrete:

| Principle | Phase-1 verdict | What changed since the initial gate |
|---|---|---|
| I | PASS | Per-instance root layout in [data-model.md](data-model.md) preserves the slug → compose-project → network → tunnel → workspace mapping 1:1. No new shared-mutable-state introduced. |
| II | PASS | Auto-detector implementation in [research.md §Sail rules](research.md) is a flat rule list (file presence + dependency-string match). Zero LLM calls in the path. |
| III | PASS | The contract for `manifest_proposal` and `manifest_pr_opened` events confirmed against the no-ambient-args fitness check — these events flow chat-frontend-direction only, never into MCP tool args. |
| IV | PASS | Overlay generator output (per [contracts/overlay.schema.json](contracts/overlay.schema.json)) carries env-var references only. Unit test `test_instance_overlay_service.py::test_no_secret_values` asserts no value matching `/SECRET\|TOKEN\|PASSWORD\|KEY\|AUTH/i` appears in the generated YAML. |
| V | PASS | Overlay generator overrides every non-cloudflared service with `ports: []`. New fitness check `check_overlay_strips_host_ports.py` enforces the invariant against synthetic project-compose inputs (multiple shapes — Sail with single primary, single-service compose, multi-service web+worker, etc.). |
| VI | PASS | Override regeneration on every provision is the implementation of FR-011. PR-open idempotency is implemented as "look up existing PR for branch `tagh/manifest-init`; if present, comment-with-link on the chat instead of opening a new one". |
| VII | PASS | Vertical slice scope confirmed against the project structure tree above — every layer (services, worker, contracts, tests, fitness, frontend handler via codegen, tagh-fre seeding) is in scope of one PR. No half-rolled feature flag. |
| VIII | PASS | Old `setup/compose_templates/laravel-vue/` deleted in the same PR; `instance_compose_renderer.py` reduced to overlay-only or replaced. No silenced tests. The bug-fixes already landed during e2e debugging (workspace_service.py cache check, instance_tasks.py reorder) are preserved in the migration; they were root-cause fixes for the prior path and remain correct under the new path. |
| IX | PASS | All disk I/O paths in the new services confirmed async-correct: `asyncio.to_thread(yaml.safe_load, fh)` for the sync PyYAML calls; httpx.AsyncClient with timeout for PR-open; no orphan `create_task`. The new `timeouts` fitness check stays green. |

**Post-design gate result**: All nine principles still pass. No Complexity Tracking entries needed.

## Complexity Tracking

> No constitution-check violations to track. This section intentionally empty.

## Phase 2 (out of scope for /speckit-plan)

Task generation runs in `/speckit-tasks` next. The plan above is intended to give that command enough structure to produce a dependency-ordered task list without needing to re-derive the architecture.
