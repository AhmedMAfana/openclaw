# Phase 0 Research — Instance Manifest + Platform Overlay

**Spec**: [spec.md](spec.md) · **Plan**: [plan.md](plan.md)

This file resolves every Technical-Context unknown the plan flagged. Each section follows the Decision / Rationale / Alternatives format.

---

## R1 — Manifest schema validation library

**Decision**: Use **Pydantic v2** (already a transitive dep via FastAPI + `pydantic-settings>=2.5`) for the in-process manifest model, and export a JSONSchema document from it for the contract artifact.

**Rationale**:
- Pydantic v2 is already installed and used elsewhere in the codebase (every settings module + every FastAPI route response model). Adding no new dependency satisfies CLAUDE.md "no new dependencies without justification".
- Pydantic gives typed Python objects downstream — the overlay generator and the auto-detector consume `Manifest`, `Manifest.Spec`, `Manifest.Spec.Ingress` as real classes, so misuse is a type error at the call site rather than a runtime KeyError.
- Pydantic exports a JSONSchema document via `Manifest.model_json_schema()`, which we can pin into [contracts/manifest.schema.json](contracts/manifest.schema.json) for the codegen + fitness-audit pipeline. The contract artifact is the source of truth for downstream consumers; the Pydantic class is the implementation that generates it. They cannot drift because the JSONSchema is regenerated on every `python -m scripts.codegen.gen_manifest_schema` run, gated by the existing pre-commit codegen-freshness hook.
- Pydantic's validation errors are structured (loc, msg, type) — perfect for surfacing field-level diagnostics in the chat ("`spec.ingress.web.port` must be an integer") that SC-006 requires within 5 seconds.

**Alternatives considered**:
- *Plain jsonschema 4.26.0*: works, but produces a less ergonomic Python API (`dict`-typed objects everywhere). The overlay generator becomes `manifest["spec"]["primary_service"]` instead of `manifest.spec.primary_service` — readable but less safe under refactor. Rejected.
- *attrs / dataclasses + manual validation*: more code to maintain, no JSONSchema export, no batteries-included error reporting. Rejected.
- *YAML schema (rx)*: niche, doesn't compose with the existing fitness-check pipeline. Rejected.

---

## R2 — Override port-strip mechanism

**Decision**: The override declares `services.<svc>.ports: !reset []` for every non-cloudflared service in the substrate. The `!reset` YAML tag is a Docker Compose v2.20+ feature that explicitly clears the substrate's list at merge time, suppressing all host-port publishing for that service.

**Rationale (corrected after empirical test on 2026-04-26)**:
- **Earlier draft of this section was wrong.** I had claimed plain `ports: []` would replace the substrate's list. It does not — Docker Compose's default behavior for `ports` (and other sequence fields like `volumes`, `dns`, `environment`-list-form) is **append**, not replace. An empirical test (`docker compose -f base.yml -f override.yml config` with `override.yml` declaring `ports: []`) confirms the substrate's `ports` are kept verbatim. Without correction, the overlay would have failed to strip host bindings — every instance of the same project would have collided on the substrate's published host ports (e.g., two Sail instances both trying to bind host `:80`).
- **`!reset []`** (a YAML local tag interpreted by Compose's loader since v2.20) explicitly resets the field to an empty list before the override's value is applied. The same empirical test with `ports: !reset []` produces a merged config with the `ports` field gone.
- The orchestrator's worker container ships Docker CLI + the `compose` plugin v2.20+; macOS dev workstations use Docker Desktop which has shipped v2.20+ since release 4.27 (Jan 2024). We pin the minimum compose version in the worker Dockerfile + check it in the new fitness check.
- This approach is **non-invasive**: the project's own `docker-compose.yml` is unchanged on disk, so a project owner running `docker compose up` locally still gets their host-port bindings (User Story 3 SC-003). The strip happens only when the override is layered on top.

**Why port conflicts can't happen even before the strip**:
- Each per-chat instance has its **own compose project name** (`tagh-inst-<slug>`), and each compose project gets its own docker network (`tagh-inst-<slug>_default`). Containers in different networks don't see each other and don't share network namespace.
- Container-internal ports (e.g., nginx listening on `:80` inside the container) are per-namespace and never collide between instances.
- The only conflict surface is **host ports** — bindings in `ports:` like `"8080:80"` that publish container :80 onto the host's :8080. If we don't strip them, two instances on the same host both try to bind host `:8080` and the second fails. The `!reset []` override eliminates that surface entirely.
- Constitution Principle V is the policy: *no host ports outside cloudflared sidecar*. The new fitness check `check_overlay_strips_host_ports.py` is the static enforcer; the contract test `test_overlay_compose_layering.py` is the runtime enforcer.

**Verification artifact**: `tests/contract/test_overlay_compose_layering.py` runs `docker compose -f synthetic-substrate.yml -f generated-override.yml config` and asserts the merged document has no `ports:` entries on any service other than `cloudflared`. The test uses fixtures from T004 covering: Sail-stock multi-service compose, single-service compose with multiple host ports, multi-service with both web + worker bindings.

**Alternatives considered**:
- *Plain `ports: []`*: doesn't actually strip (verified empirically). Rejected.
- *`extends:` instead of multi-file override*: requires copying every service definition into the override, which couples the platform's overlay to the project's compose shape. Rejected.
- *Modify the project's compose file on disk to remove ports before compose-up*: violates User Story 3 (local-dev unchanged) and FR-013 (no project repo modification). Rejected.
- *Run `docker compose` with `--scale <svc>=0` for each substrate service then re-up via override*: convoluted, brittle. Rejected.

**Alternatives considered**:
- *Use `expose:` instead of `ports:` in the project's compose*: requires modifying the project's repo, violates Story 3. Rejected.
- *Don't strip — rely on the orchestrator host's firewall rules to drop host-port traffic*: defense in depth ≠ defense by single mechanism. Constitution Principle V wants the surface itself eliminated. Rejected.
- *Run the project's compose inside a network-namespace that has no host bridge*: same effect, much more complex, fights the Docker default model. Rejected.
- *Render override-with-empty-ports for primary service only, leave other services with their ports*: violates Principle V because non-primary services in a project's compose can also publish host ports (Sail's mysql/redis/mailpit/meilisearch all do). Rejected.

---

## R3 — PR-open implementation path

**Decision**: Reuse `src/taghdev/providers/git/github.py::GitHubProvider.create_pr` (existing — implements the Provider abstraction's `create_pr(repo, branch, base, title, body)` via the `gh` CLI under the hood). Add a thin adapter `services/github_pr_service.py::open_manifest_pr(repo, manifest_yaml)` that:

1. Generates a deterministic branch name `tagh/manifest-init`.
2. In the per-instance worktree, creates that branch off the project's default branch, writes `.tagh/instance.yml` with the inferred manifest content, commits with message `chore: add TAGH instance manifest`, pushes the branch (using the existing per-instance PAT).
3. Calls `create_pr(repo, "tagh/manifest-init", default_branch, title, body)` to open the PR.
4. Returns the PR URL for the chat event.

**Rationale**:
- The `GitHubProvider` abstraction is the existing extension point for any project-authoring action. Adding a new wrapper method follows the existing pattern (`open_manifest_pr` sits next to `create_pr`, `merge_pr`, `close_pr`).
- The `gh` CLI is already installed in the worker image (we already use it for `gh pr view`, `gh api`, `gh pr merge` per `git/github.py`). No new tool.
- Branch name `tagh/manifest-init` is project-deterministic, not chat-deterministic. This is the idempotency lever: subsequent chats against the same project that hit the auto-detect path will try to push the same branch, which will fast-fail "already exists"; the wrapper catches that, calls `get_pr_for_branch` to find the existing open PR, and emits `manifest_pr_opened` with that URL instead of attempting a duplicate. Idempotent per Principle VI.
- The PAT used is the per-instance installation token minted at provision time (already implemented in `services/credentials_service.py::github_push_token`), so the PR is opened with the same authority as agent-side commits — nothing new to audit.

**Alternatives considered**:
- *Use the GitHub REST API directly via httpx*: more explicit control, but the project already standardized on `gh` CLI for github writes. Rejecting for "no new alternative paths" hygiene; revisit later if `gh` CLI proves a bottleneck.
- *Open the PR from inside the per-instance container via the agent's git MCP*: violates Principle III (no ambient args; the manifest-init action is platform-decided, not agent-decided). Rejected.
- *Don't open a PR, just commit-and-push to the default branch*: silently mutates the project's default branch. Rejected — every change to the project must be reviewable.

---

## R4 — Sail auto-detection rules

**Decision**: Sail is recognized by the conjunction:
1. `composer.json` exists at repo root, AND
2. `composer.json` declares `"laravel/sail"` as a dependency (any version, look in `require-dev` or `require`), AND
3. `docker-compose.yml` exists at repo root, AND
4. `docker-compose.yml` (parsed) contains exactly one service whose `build.context` references `vendor/laravel/sail/runtimes/`.

When all four match, the inferred manifest is:
- `spec.compose: docker-compose.yml`
- `spec.primary_service: <the service name from rule 4 above>`  (Sail's stock name is `laravel.test`; we read it from the file rather than hardcoding it)
- `spec.ingress.web: { service: <primary>, port: 80 }`
- `spec.ingress.hmr: { service: <primary>, port: 5173 }` *(only if the primary service's `ports:` list contains `5173` — Sail's default exposes it as `${VITE_PORT:-5173}:${VITE_PORT:-5173}`)*
- `spec.boot:` `["composer install --no-interaction --prefer-dist", "php artisan key:generate --force", "php artisan migrate --force", "npm install", "npm run dev -- --host 0.0.0.0"]`
- `spec.env.required: ["APP_KEY", "DB_PASSWORD"]`
- `spec.env.inherit: ["GITHUB_TOKEN", "HEARTBEAT_SECRET", "HEARTBEAT_URL", "CF_TUNNEL_TOKEN"]`

**Confidence: 0.95** when all four match. **Confidence: 0.0** otherwise (no proposal — the chat surfaces the manual-onboarding message per FR-008).

**Reasons** included in the InferredProposal payload:
- "Found `composer.json` at repo root."
- "Found `laravel/sail` in `composer.json` `require-dev`."
- "Found `docker-compose.yml` at repo root."
- "Service `<name>` has `build.context: vendor/laravel/sail/runtimes/<version>` — Sail convention."

**Rationale**:
- The four signals are all unambiguous and locally checkable (no GitHub API calls). They eliminate false positives from non-Sail Laravel projects (which wouldn't declare `laravel/sail`) and from non-Laravel PHP projects (which wouldn't have `composer.json` with `laravel/sail`).
- Reading the primary service NAME from the parsed compose (rather than hardcoding `laravel.test`) handles forks of Sail that rename the service.
- Boot command list mirrors the standard Sail dev workflow. The platform owner (project author) can override post-Confirm by editing the manifest in their repo.
- Confidence threshold of 0.95 (not 1.0) acknowledges that even with all four signals matching, edge-case Sail forks may have non-default ports or non-default boot. Auto-detect is a *proposal*, not a determination — the user reviews before Confirm.

**Alternatives considered**:
- *Hardcode `primary_service: laravel.test`*: fails on forks. Rejected.
- *Skip the `composer.json` check and rely on compose-file parsing only*: would also match any Laravel-Sail-templated docker setup pasted into a non-Laravel repo (rare but possible). The `composer.json` check is cheap and improves precision. Kept.
- *Run `gh api` to fetch the project's package.json/composer.json from the GitHub default branch*: introduces network dependency for what is a local-disk operation. Rejected.

---

## R5 — Per-instance root layout migration

**Decision**: The per-instance directory tree changes from `/workspaces/inst-<slug>/{...project files...}` (current) to `/workspaces/inst-<slug>/{worktree/, _platform/}` (new). The `instances.workspace_path` column **stays as `/workspaces/inst-<slug>/` (the per-instance root)**; consumers learn that the **worktree** is at `<workspace_path>/worktree/` and the **platform-owned** files are at `<workspace_path>/_platform/`. No DB schema change.

The cutover is operational, not migrational:
1. Old in-flight instances (`status IN ('provisioning','running','idle','terminating','grace_pending')`) are reaped via the existing `inactivity_reaper` cron with the cap shortened to 5 minutes for the cutover window. Operators take the cutover off-hours.
2. The new code path is the only path. There is no two-mode rendering — the prior `compose_templates/laravel-vue/` directory and `instance_compose_renderer.py` template-render path are deleted in the same PR.
3. After cutover, `instances` table has only rows produced by the new path, all of which use the new layout.

**Rationale**:
- Avoiding a DB column rename is the cheapest way to honor the change. Consumers (agent MCP server, workspace_service) gain a new convention (worktree is `${workspace_path}/worktree/`) and update accordingly.
- An operational cutover (drain old instances, switch code, accept new) is simpler and safer than running two layouts side-by-side via a feature flag. Spec FR-015 explicitly anticipates this — "switching a project to the new flow MUST be safe to attempt" (an unrecoverable old state means refuse to provision, not partial-migrate).
- No durable state is lost: the project's git history lives in the project's GitHub repo, not in the per-instance directory; per-instance volumes are ephemeral by design (Principle I).

**Alternatives considered**:
- *Add a new column `instances.platform_dir` and a new column `instances.worktree_dir` and migrate all rows*: extra schema churn for a one-time cutover. Rejected.
- *Run two layouts in parallel via a per-project feature flag*: violates Principle VII no-half-features. Rejected.
- *Symlink-based bridge ('worktree' is a symlink at the old `workspace_path`, '_platform' lives next to it)*: clever, but introduces a layer of indirection that bind-mounts traverse inconsistently across docker storage drivers. Rejected.

---

## R6 — Bootstrap-command execution mechanism

**Decision**: The existing `projctl` lifecycle helper runs as a sidecar in every per-instance compose stack already (per the prior compose-templates flow's `projctl up` step). It will be repurposed to read its boot-commands list from `/projctl/config.yml` (a bind-mount of `_platform/projctl-config.yml`) instead of from a baked-in `guide.md`. The overlay generator emits this `projctl-config.yml` from the manifest's `spec.boot` array.

Flow:
1. Worker provisions per-instance root, writes manifest into `_platform/projctl-config.yml`.
2. Compose-up brings the project's services + the `projctl` sidecar.
3. `projctl` waits for the primary service's healthcheck (or, if absent, polls TCP on the manifest's `ingress.web.port`) for up to 60 seconds.
4. Once healthy, `projctl` execs each boot command into the primary service container in order, streaming stdout/stderr to a per-instance log file.
5. On any boot-command non-zero exit, projctl emits a `instance_failed` event with `failure_code: bootstrap` and the failing command + last 200 lines of output (subject to redactor).
6. On all-success, projctl emits `instance_running` and the orchestrator flips `instances.status='running'`.

**Rationale**:
- Reuses an existing sidecar — no new platform-runtime component.
- Keeps boot logic out of the project's compose (the project owner declares boot in the manifest, the platform executes it; the project's compose stays purely about services).
- Healthcheck-then-boot is the canonical PaaS pattern (Render, Fly all do equivalent). Avoids the "container is running but the app inside isn't ready" race that bites on Sail (php-fpm boots faster than Vite).
- The 60-second wait is generous for typical Sail boot; manifest may override per project by adding a healthcheck to its primary service that projctl waits on.

**Alternatives considered**:
- *Run boot commands as a separate ephemeral docker container*: more orchestration; adds another network attach. Rejected.
- *Bake boot logic into the project's compose via `command:` override in the override.yml*: makes the boot logic invisible to the project owner reading their own compose. Rejected.
- *Have the agent (via MCP exec tool) run the boot commands as its first action*: violates Principle II (LLM on fallback only) — boot is a deterministic step, not an agent decision. Rejected.

---

## Open items deferred to Phase 2 (task generation)

None. Every Technical-Context item is resolved. The plan + this research are the input contract for `/speckit-tasks`.

---

## Citations & references

- [docs/architecture/per-chat-instances.md](../../docs/architecture/per-chat-instances.md) — original audit + per-chat-instances arch doc; this feature is a follow-up that replaces the compose-template subsystem inside that arch.
- [.specify/memory/constitution.md](../../.specify/memory/constitution.md) — the nine principles checked against in plan.md.
- [src/taghdev/providers/git/github.py](../../src/taghdev/providers/git/github.py) — existing GitHubProvider; reused for PR-open.
- [src/taghdev/services/credentials_service.py](../../src/taghdev/services/credentials_service.py) — existing per-instance PAT minting; reused for PR auth.
- [scripts/fitness/](../../scripts/fitness/) — existing fitness-check directory; three new checks added under it.
- [artifacts/e2e-20260426-200532/](../../artifacts/e2e-20260426-200532/) — empirical evidence that the prior flow is broken (the `tagh/laravel-vue-app:latest` image gap that motivated this feature).
