---

description: "Task list for instance-manifest-overlay feature"
---

# Tasks: Project-Owned Instance Manifest with Platform Overlay

**Input**: Design documents from `specs/002-instance-manifest-overlay/`
**Prerequisites**: [plan.md](plan.md) (required), [spec.md](spec.md) (required for user stories), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/)

**Tests**: Test tasks ARE included — the constitution (Principle VII) requires verified work, the spec's acceptance criteria call out specific failure modes that must be tested, and the existing fitness suite is the static sibling of the runtime tests. Both layers ship together in this PR.

**Organization**: Tasks are grouped by user story to enable independent implementation. The MVP is **User Story 1 alone** — every project with a committed `.tagh/instance.yml` provisions correctly. US2/US3/US4 are layered on top in priority order.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: User story this task belongs to (US1, US2, US3, US4) — Setup/Foundational/Polish phases have no story label
- Include exact file paths in descriptions

## Path Conventions

Single Python backend project. `src/taghdev/` is the package root. `tests/` at repository root. `scripts/fitness/` for static checks. `specs/002-instance-manifest-overlay/` for design docs.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Skeleton files + dev-tooling so every later task has somewhere to land.

- [ ] T001 Create empty service-module skeletons: `src/taghdev/services/instance_manifest_service.py`, `src/taghdev/services/instance_inference_service.py`, `src/taghdev/services/instance_overlay_service.py`, `src/taghdev/services/instance_root_service.py`, `src/taghdev/services/github_pr_service.py` (each starts with `from __future__ import annotations` and a module docstring quoting [plan.md §Project Structure](plan.md#project-structure))
- [ ] T002 [P] Create empty test-module skeletons: `tests/unit/test_instance_manifest_service.py`, `tests/unit/test_instance_inference_service.py`, `tests/unit/test_instance_overlay_service.py`, `tests/unit/test_github_pr_service.py`, `tests/contract/test_overlay_compose_layering.py`, `tests/integration/test_provision_with_manifest.py`, `tests/integration/test_provision_without_manifest_proposes.py`
- [ ] T003 [P] Create empty fitness-check skeletons: `scripts/fitness/check_overlay_strips_host_ports.py`, `scripts/fitness/check_no_app_template_shipped.py`, `scripts/fitness/check_overlay_outside_worktree.py`, `scripts/fitness/check_manifest_for_container_projects.py` (each exports `check() -> FitnessResult` with `principles=[...]` per `scripts/fitness/check_*.py` convention; placeholder body returns "not yet implemented" so the suite-runner discovers them but doesn't fail)
- [ ] T004 Add a pytest fixture `synthetic_substrate_compose` in `tests/conftest.py` that produces parametrized project compose-files (Sail-stock, single-service, multi-service-with-worker, with-and-without-host-ports) for the contract test and overlay-strip fitness check to share

**Checkpoint**: All future tasks have a concrete file path to land in.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Pydantic models, schema codegen, root-layout helper, stream-event extension, in-flight bug-fix consolidation. **No user-story implementation can begin until this phase is complete.**

- [ ] T005 Implement Pydantic v2 model `Manifest` in `src/taghdev/services/instance_manifest_service.py` matching [contracts/manifest.schema.json](contracts/manifest.schema.json) — fields per [data-model.md §E1](data-model.md#e1--manifest-project-side); use `model_config = ConfigDict(extra='forbid', frozen=True)`
- [ ] T006 [P] Implement Pydantic v2 model `InferredProposal` in `src/taghdev/services/instance_inference_service.py` per [data-model.md §E2](data-model.md#e2--inferredproposal-in-memory-never-persisted) — fields: `manifest: Manifest`, `confidence: float`, `reasons: list[str]`, `source_signals: list[dict]`
- [ ] T007 Add codegen script `scripts/codegen/gen_manifest_schema.py` that imports `Manifest`, calls `Manifest.model_json_schema()`, and writes the result to `specs/002-instance-manifest-overlay/contracts/manifest.schema.json` (with the existing handwritten file as the comparison baseline — script asserts the generated schema matches; pre-commit codegen-freshness hook runs it)
- [ ] T008 Implement `PerInstanceRoot` helper in `src/taghdev/services/instance_root_service.py` exposing: `root(slug) -> Path`, `worktree(slug) -> Path` (= `<root>/worktree`), `platform_dir(slug) -> Path` (= `<root>/_platform`), `ensure_layout(slug) -> None` (mkdir both subdirs), `remove(slug) -> None` (idempotent rm -rf root). Single source of truth for the per-instance directory convention from [data-model.md §E4](data-model.md#e4--perinstanceroot-platform-side-on-disk)
- [ ] T009 Update `src/taghdev/services/workspace_service.py::reattach_session_branch` to attach the worktree at `<workspace_path>/worktree/` instead of `<workspace_path>/` (parameter rename: callers pass per-instance ROOT, helper computes worktree subdir via `instance_root_service.worktree`). Keeps the pre-existing cache-check + clone-on-empty-cache fix from the e2e debug session.
- [ ] T010 Update `src/taghdev/services/workspace_service.py::prepare` similarly — attach worktrees at the new layout for task-mode chats (legacy host/docker mode is untouched per FR-015)
- [ ] T011 Add new event types to `specs/001-per-chat-instances/contracts/stream_event.schema.json`: `manifest_proposal` (payload: full InferredProposal) and `manifest_pr_opened` (payload: pr_url, pr_number, branch). Each follows the existing event-payload conventions
- [ ] T012 [P] Run codegen for stream events: `python3 scripts/codegen/gen_stream_events.py` → updates `chat_frontend/src/types/instance_events.ts` to include the two new types in the discriminated union (forces `tsc` failure in the frontend if handlers are missing)
- [ ] T013 [P] Update `src/taghdev/services/stream_validator.py::_REQUIRED_BY_TYPE` to include the required keys for `manifest_proposal` (`{instance_slug, manifest, confidence, reasons}`) and `manifest_pr_opened` (`{instance_slug, pr_url, pr_number, branch}`)
- [ ] T014 [P] Add Phase-A unit test `tests/unit/test_instance_root_service.py` (T008's helpers — root layout, idempotent ensure, idempotent remove)

**Checkpoint**: Core types + layout helper + stream-event contract additions are in. User-story phases can run in parallel.

---

## Phase 3: User Story 1 — Onboard with in-repo manifest (Priority: P1) 🎯 MVP

**Goal**: A project that already has `.tagh/instance.yml` in its repo provisions correctly. Chat banner reaches "running", tunnel URL serves real HTML, terminate cleans up.

**Independent Test**: Add `.tagh/instance.yml` to a Sail repo (the canonical fixture is `AhmedMAfana/tagh-fre`). Open a new chat, type a first message. Within 90 seconds the chat shows the live URL and the URL serves the project's actual application HTML. Terminate; confirm `docker ps` is empty for `tagh-inst-*` and `instances.status='destroyed'`.

### Tests for User Story 1

- [ ] T015 [P] [US1] In `tests/unit/test_instance_manifest_service.py`: parametrized happy-path test (well-formed Sail manifest validates), schema-violation tests (apiVersion wrong, kind wrong, missing primary_service, missing ingress.web, env name not UPPER_SNAKE), malformed-YAML test, missing-file test
- [ ] T016 [P] [US1] In `tests/unit/test_instance_overlay_service.py`: cloudflared sidecar present in output, projctl sidecar present, every non-cloudflared service has empty `ports:`, `instance` network exists, environment carries only list-form references (no `KEY: value` mapping form), no secret values present in serialized YAML (regex assert against `/SECRET\|TOKEN\|PASSWORD\|KEY\|AUTH/i` finds nothing in values)
- [ ] T017 [P] [US1] In `tests/contract/test_overlay_compose_layering.py`: feed each fixture from T004 through `instance_overlay_service.generate`, write substrate + override to a temp dir, run `docker compose -f substrate -f override config` (subprocess), parse the merged YAML, assert no service except `cloudflared` has any entry in `ports` (full Principle V verification)
- [ ] T018 [US1] In `tests/integration/test_provision_with_manifest.py`: end-to-end against local Docker daemon — clone a fixture Sail repo with `.tagh/instance.yml`, call `provision_instance`, poll `instances.status` until `running` (90s budget), GET the tunnel URL via `httpx`, assert HTTP 200 + body contains `Laravel`, terminate, assert no `tagh-inst-*` containers remain

### Implementation for User Story 1

- [ ] T019 [US1] Implement `instance_manifest_service.load_and_validate(worktree_path: Path) -> Manifest | ManifestError` in `src/taghdev/services/instance_manifest_service.py` — opens `<worktree>/.tagh/instance.yml`, parses with `asyncio.to_thread(yaml.safe_load, fh)`, validates with the Pydantic model, returns either the Manifest or a structured error (ManifestError fields: `code` ∈ {`missing`, `malformed_yaml`, `schema_violation`, `service_not_found`, `port_collision`}, `field_path`, `human_message`)
- [ ] T020 [US1] Add cross-validation step to `load_and_validate`: parse `<worktree>/<spec.compose>` (the substrate compose), assert `spec.primary_service` exists in `services:`, assert each `spec.ingress.<role>.service` exists in `services:`. On mismatch, return `ManifestError(code=service_not_found, field_path=…)` per spec edge case "Manifest declares a service that isn't in the project's compose"
- [ ] T021 [US1] Implement `instance_overlay_service.generate(substrate: dict, manifest: Manifest, meta: InstanceMeta) -> str` in `src/taghdev/services/instance_overlay_service.py` — pure function returning a YAML string. `InstanceMeta` is a dataclass holding `slug, compose_project, cf_tunnel_id, web_hostname, hmr_hostname, ide_hostname`. Output shape per [contracts/overlay.schema.json](contracts/overlay.schema.json) and [data-model.md §E3](data-model.md#e3--overlay-platform-side-on-disk-under-_platform)
- [ ] T022 [US1] Implement collision-detection in `instance_overlay_service.generate`: if substrate already declares a service named `cloudflared` or `projctl`, raise `OverlayError(code=service_name_collision, conflicting_name=…)`. Per spec edge case "Project's compose declares its own service that collides with a platform-reserved sidecar name"
- [ ] T023 [US1] Implement projctl-config emitter `instance_overlay_service.emit_projctl_config(manifest: Manifest, meta: InstanceMeta) -> str` — YAML string carrying the manifest's `boot[]` list plus the heartbeat URL/secret REFERENCES (not values), targeted at the projctl sidecar's `/projctl/config.yml`
- [ ] T024 [US1] Implement cloudflared-config emitter `instance_overlay_service.emit_cloudflared_config(meta: InstanceMeta) -> str` — extract from existing `setup/compose_templates/laravel-vue/cloudflared.yml` template the parts that are tunnel-id-keyed only; the new emitter takes only the `cf_tunnel_id` and ingress hostnames as input
- [ ] T025 [US1] Refactor `src/taghdev/worker/tasks/instance_tasks.py::provision_instance` — replace the call sequence (TunnelService → render_compose → reattach → compose_up → projctl_up) with the new sequence (TunnelService → root_service.ensure_layout → reattach to `<root>/worktree` → load_and_validate manifest → overlay_service.generate + write to `<root>/_platform/compose.override.yml` + emit_projctl_config to `<root>/_platform/projctl-config.yml` + emit_cloudflared_config to `<root>/_platform/cloudflared.yml` → compose_up with the layered `-f` arguments). Preserve the existing failure-classification (`FailureCode.PROJCTL_UP`, etc.) and the credential-token rotation
- [ ] T026 [US1] Update `src/taghdev/worker/tasks/instance_tasks.py::teardown_instance` — call `compose down` with the same layered `-f` arguments, then `instance_root_service.remove(slug)` (which removes the per-instance root tree atomically). Idempotent on a missing tree
- [ ] T027 [US1] Update `projctl` source so it reads boot commands from `/projctl/config.yml` instead of the prior baked-in `guide.md` path — projctl is Go (per Architecture Constraints); change is a config-source swap in projctl's main.go. Log every command's stdout/stderr to `/var/lib/projctl/boot.log`. Emit `instance_running` event to the orchestrator's heartbeat endpoint after all-success; emit `instance_failed` with structured payload on any non-zero exit
- [ ] T028 [US1] Implement `scripts/fitness/check_overlay_strips_host_ports.py` — load fixtures from T004, run them through `instance_overlay_service.generate`, assert the merged output (via `docker compose -f a -f b config` if available, or YAML-merge in-process otherwise) has zero `ports:` entries on every non-cloudflared service. Map to Constitution Principle V
- [ ] T029 [US1] Seed `.tagh/instance.yml` in the `AhmedMAfana/tagh-fre` GitHub repo via a manual PR (out-of-tree, not a code task in this repo) — content per [quickstart.md §A Step 1](quickstart.md#step-1-add-the-manifest-to-your-repo). This task is the "real" P1 acceptance test gate
- [ ] T030 [US1] Update CLAUDE.md "Per-chat instance mode — quick reference" section to point at the new flow (manifest reader, overlay generator, root layout); preserve the rest of the section's content

**Checkpoint**: A chat against `tagh-fre` with the manifest in its repo provisions to `running`, the tunnel URL serves real HTML, and terminate cleans up. **MVP shipped.**

---

## Phase 4: User Story 2 — First-time onboarding with auto-detect + PR-back (Priority: P2)

**Goal**: A project with NO manifest in its repo gets a manifest proposed in the chat with rationale, and clicking Confirm both provisions AND opens a PR adding the manifest to the project's repo.

**Independent Test**: Take a fresh Sail repo with no `.tagh/` directory. Open a new chat against it. The chat shows a `manifest_proposal` card listing the four reasons + the proposed manifest YAML. Click Confirm. The chat then shows a `manifest_pr_opened` card with the PR URL. Verify (via `gh pr view`) the PR exists, contains exactly `.tagh/instance.yml`, and the chat instance reaches `running`.

### Tests for User Story 2

- [ ] T031 [P] [US2] In `tests/unit/test_instance_inference_service.py`: Sail-stock fixture → confidence ≥ 0.9 + manifest with primary_service derived from compose; non-Sail Laravel fixture → no proposal (confidence 0); empty repo → no proposal; Sail fork with renamed primary service → proposal with the renamed service
- [ ] T032 [P] [US2] In `tests/unit/test_github_pr_service.py`: PR open success path (mock the underlying GitHubProvider — assert branch name `tagh/manifest-init`, manifest committed at `.tagh/instance.yml`, returns PR URL); idempotency path (existing branch detected, returns the existing PR URL via `get_pr_for_branch`); branch-protection-rejection path (mock raises, service surfaces structured error with `failure_code=pr_branch_protection`)
- [ ] T033 [US2] In `tests/integration/test_provision_without_manifest_proposes.py`: end-to-end with a Sail repo whose `.tagh/` is removed before provisioning — assert chat receives `manifest_proposal` event, no compose-up runs, simulated Confirm action triggers compose-up + `manifest_pr_opened` event, project's repo gets a PR

### Implementation for User Story 2

- [ ] T034 [US2] Implement `instance_inference_service.infer_from_worktree(worktree: Path) -> InferredProposal | None` in `src/taghdev/services/instance_inference_service.py` — Sail rules from [research.md §R4](research.md#r4--sail-auto-detection-rules); reads `composer.json`, parses `docker-compose.yml` with `asyncio.to_thread(yaml.safe_load, …)`; returns None when not all four signals match (manual-onboarding fallback per FR-008)
- [ ] T035 [US2] Implement `github_pr_service.open_manifest_pr(repo: str, manifest_yaml: str, pat: str) -> ManifestPRResult` — uses the existing `providers/git/github.py::GitHubProvider`. Logic per [research.md §R3](research.md#r3--pr-open-implementation-path): create branch `tagh/manifest-init` off default branch, commit `.tagh/instance.yml`, push, call `create_pr`. Idempotency: catch "branch exists" from `git push`, fall through to `get_pr_for_branch("tagh/manifest-init")` and return the existing URL. Returns `ManifestPRResult(url, number, branch, was_existing: bool)`
- [ ] T036 [US2] Update `src/taghdev/api/routes/assistant.py` provisioning entry — when a chat triggers provision and `instance_manifest_service.load_and_validate` returns `ManifestError(code=missing)`, call `instance_inference_service.infer_from_worktree`. If a proposal is returned, emit `manifest_proposal` event with redactor-wrapped payload and DO NOT proceed to compose-up. If no proposal returned, emit a structured "manual onboarding required" failure event pointing at the manifest path + quickstart docs (FR-008)
- [ ] T037 [US2] Add `confirm_manifest_proposal` action handler in `src/taghdev/api/routes/assistant.py` — payload includes proposal-id and the user's decision. On Confirm: (a) write manifest to the worktree's `.tagh/instance.yml` for use in this provision only (NOT committed), (b) call `provision_instance` ARQ job, (c) call `github_pr_service.open_manifest_pr` in parallel via `asyncio.gather` (so the live instance doesn't wait on PR-open per FR-019), (d) on PR success emit `manifest_pr_opened`, on PR failure emit a structured warning event but provisioning still proceeds
- [ ] T038 [US2] Add `cancel_manifest_proposal` action handler in `src/taghdev/api/routes/assistant.py` — emits a `manifest_proposal_cancelled` event whose payload contains the proposal YAML as a code-block-ready string (User Story 2 acceptance scenario 3); does NOT provision; chat returns to empty state ready for a fresh first message
- [ ] T039 [US2] Frontend: add a render handler for `manifest_proposal` events in `chat_frontend/src/components/InstanceCard.tsx` (or wherever `instance_*` events render) — card shows the proposed YAML + the rationale list + two buttons (Confirm, "I'll add it myself") that POST the appropriate action handler
- [ ] T040 [US2] Frontend: add a render handler for `manifest_pr_opened` events — card shows "Manifest PR opened: <link to PR>"; clicking the link opens the PR in a new tab. Also handle `manifest_proposal_cancelled` (renders the YAML code-block + a copy-to-clipboard button)
- [ ] T041 [US2] Add an entry to `_REQUIRED_BY_TYPE` in `stream_validator.py` requiring `manifest_proposal` payloads to NEVER carry a value matching the redactor's secret regex (defense in depth — the proposal is by construction secret-free, but the validator asserts it on every emit)

**Checkpoint**: Empty-manifest projects get a proposal in chat. Confirm provisions AND opens a PR. Cancel discards. The PR is reviewable and reversible like any other.

---

## Phase 5: User Story 3 — Local dev unchanged (Priority: P2)

**Goal**: Adding `.tagh/instance.yml` to a project doesn't change anything about local `docker compose up` for the project owner. The TAGH overlay never lands in the project's git status.

**Independent Test**: Clone the project (with the manifest committed). Run `docker compose up` on a developer's laptop. Project boots identically — same services, same ports, same behavior. `git status` shows clean. No `compose.override.yml`, no `_platform/` in the project's working directory.

### Tests for User Story 3

- [ ] T042 [US3] In `tests/integration/test_local_dev_unchanged.py`: clone the test fixture with the manifest, run `docker compose up -d` (project's own compose, no overlay), assert primary service binds the project-declared host port locally, assert `git status` reports clean. Tear down. (Test is environment-dependent — mark with `pytest.mark.requires_docker_daemon`)
- [ ] T043 [US3] [P] In `tests/unit/test_instance_root_service.py` (extends T014): assert `worktree(slug)` and `platform_dir(slug)` return distinct paths; assert no method ever writes outside `<root>` boundary

### Implementation for User Story 3

- [ ] T044 [US3] Implement `scripts/fitness/check_overlay_outside_worktree.py` — read overlay-write call sites in `src/taghdev/services/instance_overlay_service.py` and `src/taghdev/worker/tasks/instance_tasks.py`; assert every write target path resolves through `instance_root_service.platform_dir(slug)` and never through `instance_root_service.worktree(slug)`. Map to Principle V (egress-only by extension — platform files don't pollute the project's repo)
- [ ] T045 [US3] Audit `worker/tasks/instance_tasks.py::provision_instance` for any leftover writes into the worktree subdir (the prior `render_compose` call wrote `_compose.yml`/`_cloudflared.yml`/`_nginx.conf` etc. into the workspace root; under the new layout these all land in `_platform/`). Remove any stragglers found

**Checkpoint**: Project owner's local dev workflow is invariant under TAGH adoption.

---

## Phase 6: User Story 4 — Platform stops shipping project stacks (Priority: P3)

**Goal**: After this PR, the platform repo contains zero framework-specific application stacks. Adding a new framework's project type requires a `.tagh/instance.yml` in that project, not a platform release.

**Independent Test**: After the PR merges, `find src/taghdev/setup -name 'compose.yml' -o -name 'Dockerfile' -o -name '*.dockerfile'` returns nothing inside framework-specific subdirs. `grep -rn 'tagh/laravel-vue-app\|laravel-vue\|compose_templates' src/taghdev/` returns nothing in live code (only the changelog/docs may reference the historical removal).

### Implementation for User Story 4

- [ ] T046 [US4] Delete the directory `src/taghdev/setup/compose_templates/laravel-vue/` entirely (`compose.yml`, `cloudflared.yml`, `nginx.conf`, `vite.config.js`, `guide.md`, `project.yaml`, `.gitkeep`)
- [ ] T047 [US4] Delete the directory `src/taghdev/setup/compose_templates/` once empty (after T046, only `GUIDE_SPEC.md` remains — relocate it to `docs/historical/guide-spec.md` if anything references it, otherwise delete)
- [ ] T048 [US4] Delete `src/taghdev/services/instance_compose_renderer.py` and remove its import from `src/taghdev/worker/tasks/instance_tasks.py` (the render call site is replaced by the overlay generator in T025)
- [ ] T049 [US4] Implement `scripts/fitness/check_no_app_template_shipped.py` — fail if any of these patterns appear under platform-owned directories: a `compose.yml` under `src/taghdev/setup/`, a `Dockerfile` under `src/taghdev/setup/`, an `image: tagh/<framework>-app` reference anywhere in `src/taghdev/`. Map to Principle VIII (root-cause: removing the wrong premise outright)
- [ ] T050 [US4] Sweep CLAUDE.md, docs/, and any scripts for references to `compose_templates`, `tagh/laravel-vue-app`, `_template_dir_for_instance`, `render_compose`, `instance_compose_renderer` — replace each with the corresponding manifest/overlay reference, preserving link targets

**Checkpoint**: Platform repo is debloated of project-specific stacks. New project types onboard via in-repo manifests, not platform releases.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, fitness-check rollout, e2e validation, hot-fix cleanup.

- [ ] T051 Implement `scripts/fitness/check_manifest_for_container_projects.py` — for every active row in `projects` with `mode='container'`, query the project's GitHub default-branch HEAD via `gh api repos/<repo>/contents/.tagh/instance.yml`. PASS if file exists; PASS if file missing AND `instance_inference_service.infer_from_worktree`-equivalent rules match the repo's contents (enough signal for a high-confidence proposal); FAIL otherwise. Map to Principle VII (no-half-features — every onboarded project must be reach-able by the new flow)
- [ ] T052 Wire all four new fitness checks into `scripts/pipeline_fitness.py` discovery (already automatic via `check_*.py` glob — verify) and run `python3 scripts/pipeline_fitness.py --fail-on high`. Must be green
- [ ] T053 [P] Write `docs/setup/MANIFEST.md` — project-owner reference, migrating from quickstart §A. Include the canonical Sail manifest, field reference table, validation error catalogue, and the auto-detect → PR-back UX walkthrough. Link from CLAUDE.md
- [ ] T054 [P] Update `docs/setup/CREDENTIALS.md` — note that the existing GitHub PAT (already required) is now also used for PR-back. No new credential needed
- [ ] T055 Revert the e2e-debug SQL hot-fix: `DELETE FROM platform_config WHERE category='git' AND key='provider.github';` — this row was inserted to unblock the prior flow; the new flow uses `CredentialsService.github_push_token` directly and doesn't need the legacy provider config. Document the revert in the PR description
- [ ] T056 Update `scripts/seed_platform_creds.py` to remove any "git/provider.github" seeding (if added during e2e debug) — the seeder should ONLY write `cloudflare/settings` and `github_app/settings` going forward; the legacy `factory.get_git()` path is removed in T048
- [ ] T057 Run `/e2e-pipeline` end-to-end against `tagh-fre` (after T029 lands the manifest in the test repo). Acceptance: every phase reaches green, including phases 4–9 which were never reached under the prior flow. Capture the artifacts under `artifacts/e2e-<ts>/REPORT.md` and link from this PR's description
- [ ] T058 Update [specs/001-per-chat-instances/plan.md](../001-per-chat-instances/plan.md) — add a "Superseded by 002" note in the section that owned the compose-template subsystem; foundation principles (slug, network, tunnel, MCP fleet) stay untouched
- [ ] T059 PR description discipline (Principle VII): the PR's description states per changed surface "Done and verified" with the verification command (e.g., "T028 → `pytest tests/contract/test_overlay_compose_layering.py`") and lists the e2e regression artifact path

**Final checkpoint**: `pipeline_fitness --fail-on high` green, `/e2e-pipeline` reaches phase 9 green, the platform repo no longer ships any project-specific app stacks, and `tagh-fre` provisions in under 90s with the new flow.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)** — no deps; can start immediately. T002, T003 are [P] — different files, no shared edits.
- **Foundational (Phase 2)** — depends on Phase 1. T006, T012, T013, T014 are [P] within Phase 2 (different files). **BLOCKS all user stories.**
- **User Story 1 (Phase 3 — MVP)** — depends on Phase 2. Tests (T015, T016, T017) are [P]; integration test T018 depends on T025 (the worker refactor). Implementation tasks T019–T028 must complete before the integration test runs green; T029 (seed manifest in test repo) is a gating prerequisite for T057 (e2e regression in Phase 7).
- **User Story 2 (Phase 4)** — depends on Phase 2 + the codegen-driven event handler additions in T037–T040 in the frontend; tests T031, T032 are [P] vs each other and vs US1's tests.
- **User Story 3 (Phase 5)** — depends on Phase 2 + T009 (workspace_service worktree subdir refactor) + T025 (overlay-only writes under `_platform/`). Verification-heavy phase; T044 (fitness check) and T045 (audit) are independent.
- **User Story 4 (Phase 6)** — depends on T025 + T048 (renderer deletion paired with worker call-site replacement). T046–T050 are sequential because they delete cascading references.
- **Polish (Phase 7)** — T051–T056 depend on respective US-phase completion; T057 depends on T029 + all of US1; T058 + T059 are documentation-only and can run last.

### User Story Dependencies

- **US1 → US2**: US2 reuses the overlay generator and the manifest validator from US1. Cannot start until T021 + T025 are merged.
- **US1 → US3**: US3 verifies the layout invariants US1 establishes. T044 + T045 cannot run before T025.
- **US1+US2 → US4**: US4 deletes the legacy renderer; requires the overlay generator (T021) + the worker call-site swap (T025) to be in place first.
- **All US → Polish**: T057 (e2e regression) depends on every story being shippable.

### Within Each User Story

- Tests live alongside implementation in this PR (constitution Principle VII: verified work). The tests are written WITH the implementation, not strictly TDD-first — but each test must FAIL before the corresponding implementation lands and PASS after.
- Models / pure-function services first (T021 generates a YAML string from inputs; can be tested without docker).
- Worker / API call sites second (T025, T036 — wire the services into the live ARQ + FastAPI surfaces).
- Integration tests last (T018, T033 — exercise the full path against a real Docker daemon).

### Parallel Opportunities

- All [P]-marked tasks within a phase can run in parallel.
- All four user stories CAN run in parallel after Phase 2, IF you have multiple developers — but US1 → US2 / US3 / US4 has the dependency chain noted above.
- Tests within a story marked [P] can run in parallel.

---

## Parallel Example: User Story 1

```bash
# Once Phase 2 is done, kick off in parallel:
Task: "T015 — manifest service unit tests in tests/unit/test_instance_manifest_service.py"
Task: "T016 — overlay service unit tests in tests/unit/test_instance_overlay_service.py"
Task: "T017 — contract test in tests/contract/test_overlay_compose_layering.py"
Task: "T028 — fitness check in scripts/fitness/check_overlay_strips_host_ports.py"

# Then, once T021 (overlay generator) lands:
Task: "T025 — refactor provision_instance in src/taghdev/worker/tasks/instance_tasks.py"
Task: "T026 — update teardown_instance in src/taghdev/worker/tasks/instance_tasks.py"

# Then, once T025 lands:
Task: "T018 — integration test in tests/integration/test_provision_with_manifest.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete **Phase 1: Setup** (T001–T004).
2. Complete **Phase 2: Foundational** (T005–T014). Pre-commit codegen-freshness hook ensures the schema files stay in sync.
3. Complete **Phase 3: User Story 1** (T015–T030).
4. **STOP and VALIDATE**: Run `pytest tests/integration/test_provision_with_manifest.py -v` against `tagh-fre` (with manifest seeded via T029). Reaching green here is MVP shipped.

### Incremental Delivery

1. Setup + Foundational → foundation ready (no user-visible change).
2. + US1 → MVP shipped (in-repo manifest → working live URL).
3. + US2 → first-run UX added (auto-detect + PR-back).
4. + US3 → invariant proven (local dev unchanged) — verification only, no behavior change.
5. + US4 → debloating shipped (platform stops shipping project stacks).
6. + Polish → static + live regression green (`pipeline_fitness` + `/e2e-pipeline`).

### Parallel Team Strategy

With multiple devs:

1. Together: complete **Setup + Foundational**.
2. Once Foundational lands:
   - Dev A: US1 (MVP — most surface area, top priority).
   - Dev B: US4 (deletion + new fitness checks — no behavior dependency on US1's worker refactor; CAN start immediately because the deletion of `instance_compose_renderer.py` is gated on T025 in US1, but the fitness-check + sweep work is independent).
   - Dev C: US2 frontend work (T039, T040 — codegen produces the types, frontend handlers can be drafted without the backend implementation present in dev's branch as long as fixtures are available).
3. Once US1 lands: US2 backend (T034–T038) + US3 verification (T042–T045) merge in parallel.
4. Polish runs last; T057 (`/e2e-pipeline`) is the final gate.

---

## Notes

- **All tasks are file-pathed** — every task line names a concrete file the implementer edits.
- **No test deletion** — the constitution (Principle VIII) prohibits making CI green by removing failing tests. Where tests fail because the implementation is missing, the failing test STAYS until the implementation lands.
- **No `--no-verify`, no `# type: ignore`** — fitness, lint, type, and pre-commit signals are root-fixed in the same PR (Principle VIII).
- **Codegen freshness** — T007 + T012 produce generated artifacts; the existing pre-commit hook re-runs codegen and fails if the generated file differs. This catches the "schema bump without regen" class of bug for the new event types.
- **Branch hygiene reminder** — work currently sits on `multi-instance` due to the speckit branch-name guard. Recommended sequence: commit the in-flight bug-fixes (workspace_service.py cache check + instance_tasks.py reorder from the e2e debug session) as their own scope-respecting PR on `multi-instance`; then create branch `002-instance-manifest-overlay` from there for this task list.
- **Stop at any checkpoint** to validate independently. The MVP gate is the most important — phases 4–7 layer on once US1 is provably shipping.
