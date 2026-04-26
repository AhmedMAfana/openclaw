# Phase 1 Data Model — Instance Manifest + Platform Overlay

**Spec**: [spec.md](spec.md) · **Plan**: [plan.md](plan.md) · **Research**: [research.md](research.md)

Four entities. Three platform-side, one project-side. None of them is a new SQL table — manifest content lives in the project's repo (per spec FR-018), and the overlay is regenerated on every provision (per spec FR-011), so neither needs durable platform state. The two transient on-disk artifacts (`compose.override.yml`, `projctl-config.yml`) are recreated from inputs at every provision.

---

## E1 — Manifest *(project-side)*

**Lives**: in the project's repo, at `.tagh/instance.yml`.
**Shape**: declarative YAML. Pydantic model in `services/instance_manifest_service.py::Manifest`. JSONSchema export at [contracts/manifest.schema.json](contracts/manifest.schema.json).

**Fields**:

| Path | Type | Required | Description |
|---|---|---|---|
| `apiVersion` | string | yes | Always `tagh/v1` for the v1 schema. Version bump = breaking change. |
| `kind` | string | yes | Always `Instance`. Reserved for future kinds (e.g. `Project`). |
| `spec.compose` | string | yes | Path inside the repo to the substrate compose file. Default `docker-compose.yml`. Must resolve to a regular file when the platform reads it. |
| `spec.primary_service` | string | yes | Name of the service in the substrate compose that receives ingress and runs boot commands. Must match a `services.<name>` key in the substrate compose. |
| `spec.ingress.web.service` | string | yes | Service that serves web traffic. Conventionally same as `primary_service`. |
| `spec.ingress.web.port` | integer (1..65535) | yes | Container port for web ingress. Tunnel routes `${INSTANCE_HOST}:443` → `<service>:<port>`. |
| `spec.ingress.hmr.service` | string | no | Service for HMR (Vite, etc.). Omit if no HMR. |
| `spec.ingress.hmr.port` | integer (1..65535) | no | Container port for HMR. |
| `spec.ingress.ide.service` | string | no | Reserved for future code-server / web-ide ingress. |
| `spec.ingress.ide.port` | integer (1..65535) | no | Reserved. |
| `spec.boot[]` | array of strings | no, default `[]` | Shell commands to run inside `primary_service` after compose-up succeeds and the service is healthy. Empty = no bootstrap. |
| `spec.env.required[]` | array of strings | no, default `[]` | Env-var names that must be set in the container at compose-up time. Platform refuses to provision if any required name is missing from both the project's compose `environment:` and the platform's injection list. |
| `spec.env.inherit[]` | array of strings | no, default `[]` | Env-var names the platform should pass into compose-up's parent process so the project's `environment: - NAME` list-form picks them up. |

**Validation rules** (enforced by the Pydantic model + the manifest service):

- `apiVersion == 'tagh/v1'` strictly.
- `kind == 'Instance'` strictly.
- `spec.primary_service` must be a key in the substrate compose's `services` map (cross-validated when the manifest is loaded against a clone).
- For each declared `ingress.<role>.service`, that service must exist in the substrate compose.
- For each declared `ingress.<role>.port`, that port must appear in the service's `expose` list, `ports` list, or any healthcheck command — the platform is permissive here because Compose port declarations are advisory; final ground truth is whether the cloudflared sidecar can connect after boot.
- Service names must satisfy Compose's identifier regex (`^[a-zA-Z0-9._-]+$`).
- Boot commands have no length cap individually but the overall manifest is bounded by GitHub's PR-body size cap when used in the auto-detect → PR-open flow.

**State transitions**: none. Manifest is read-only at provision time; mutations happen via PRs against the project's repo, outside the platform.

---

## E2 — InferredProposal *(in-memory, never persisted)*

**Lives**: only in the chat-event payload from worker to frontend, and on the user's screen until they Confirm or click "I'll add it myself".
**Shape**: Pydantic model in `services/instance_inference_service.py::InferredProposal`.

**Fields**:

| Path | Type | Description |
|---|---|---|
| `manifest` | E1 Manifest | The proposed manifest, identical in shape to a project-authored one. |
| `confidence` | float (0.0–1.0) | Auto-detector's confidence. Sail-shape match is 0.95. |
| `reasons[]` | array of strings | Human-readable rationale entries, e.g. "Found `composer.json` at repo root." |
| `source_signals[]` | array of objects `{path, signal, value}` | Machine-readable record of what each rule matched, for debug. |

**Lifecycle**:

1. Created by `instance_inference_service.infer_from_worktree(worktree_path) -> InferredProposal | None`.
2. Returned to the orchestrator on the auto-detect path.
3. Embedded in the `manifest_proposal` chat event payload, redactor-wrapped.
4. Discarded after the user's Confirm / Cancel decision. **Not stored anywhere on the platform side.** If the user Cancels, the proposal evaporates. If the user Confirms, its `manifest` field is YAML-serialized into the per-instance worktree's `_platform/projctl-config.yml` for boot AND into a fresh branch in the project's repo for the PR-back; the in-memory proposal object itself goes out of scope at the end of the request.

**State transitions**: none (single-use).

---

## E3 — Overlay *(platform-side, on disk under `_platform/`)*

**Lives**: at `/workspaces/inst-<slug>/_platform/compose.override.yml`. Sibling files: `projctl-config.yml`, `cloudflared.yml`.
**Shape**: docker-compose v3-compatible YAML document. JSONSchema for the SHAPE the generator emits at [contracts/overlay.schema.json](contracts/overlay.schema.json).

**Required content**:

```yaml
services:
  cloudflared:        # NEW — added by overlay
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate --config /etc/cloudflared/config.yml run
    volumes:
      - <abs path to>_platform/cloudflared.yml:/etc/cloudflared/config.yml:ro
      - cloudflared-creds:/etc/cloudflared/creds
    environment:
      - TUNNEL_TOKEN
    networks: [instance]

  projctl:            # NEW — added by overlay
    image: tagh/projctl:latest    # platform-owned tiny image (already exists)
    restart: unless-stopped
    volumes:
      - <abs path to>_platform/projctl-config.yml:/projctl/config.yml:ro
      - projctl-state:/var/lib/projctl
    environment:
      - HEARTBEAT_SECRET
      - HEARTBEAT_URL
      - GITHUB_TOKEN     # only the inherit-listed names from the manifest
    networks: [instance]
    depends_on:
      <primary_service>:
        condition: service_healthy   # or service_started fallback

  <primary_service>:  # OVERRIDE on top of substrate
    ports: []                         # strip host-port binding (Principle V)
    networks: [instance, <substrate networks>]   # additionally join instance net

  <other_substrate_service_1>:    # OVERRIDE — strip ports for every other service
    ports: []
    networks: [instance, <substrate networks>]

  # ... repeated for every service in the substrate compose ...

networks:
  instance:
    driver: bridge

volumes:
  cloudflared-creds:
  projctl-state:
```

**Generator contract** (`instance_overlay_service.generate(substrate, manifest, instance_meta) -> str`):

- **Inputs**: parsed substrate compose (PyYAML), validated Manifest, and an `InstanceMeta` record (slug, compose_project, cf_tunnel_id, web_hostname).
- **Output**: a YAML string ready to write to `_platform/compose.override.yml`.
- **Invariants** (enforced by the unit tests + the new fitness check):
  - Every service from the substrate appears with `ports: []` in the override.
  - The `cloudflared` service exists in the override but is NOT in the substrate (collision detection raises early — see Edge Cases in spec).
  - The `projctl` service exists in the override but is NOT in the substrate.
  - Every `services.<svc>.environment` list-form entry referenced from the manifest's `env.inherit` is a list-form `- NAME` reference, never `KEY: value` — secrets flow via the parent process env at compose-up time. (Principle IV.)
  - The `networks.instance` top-level definition is added if not present in the substrate.
  - The `volumes` top-level definition gains `cloudflared-creds` and `projctl-state` if not present.

**State transitions**: none. Recreated on every provision; deleted on teardown.

---

## E4 — PerInstanceRoot *(platform-side, on disk)*

**Lives**: directory at `/workspaces/inst-<slug>/`.
**Shape**: filesystem directory invariant.

**Tree invariant**:

```text
/workspaces/inst-<slug>/
├── worktree/        # exactly one — the project's git worktree
└── _platform/       # exactly one — platform-owned files
```

**Cardinality**: exactly one root per `instances` row keyed by slug. The `instances.workspace_path` column holds the root path (e.g. `/workspaces/inst-bd8526f03c1160/`). Consumers compute the worktree path as `<workspace_path>/worktree/` and the platform path as `<workspace_path>/_platform/`.

**Lifecycle**:

| Phase | What happens |
|---|---|
| `provision_instance` step 1 | `mkdir -p <root>/{worktree,_platform}` |
| `provision_instance` step 2 (clone/worktree) | Project worktree attached at `<root>/worktree/` via the existing `WorkspaceService.reattach_session_branch` |
| `provision_instance` step 3 (overlay) | `compose.override.yml`, `projctl-config.yml`, `cloudflared.yml` written into `<root>/_platform/` |
| `provision_instance` step 4 (compose up) | `docker compose -p <project> -f <root>/worktree/docker-compose.yml -f <root>/_platform/compose.override.yml up -d` |
| `provision_instance` step 5 (boot) | `projctl` sidecar reads `<root>/_platform/projctl-config.yml`, runs boot commands inside `primary_service` |
| `teardown_instance` | `docker compose down`; then `rm -rf <root>` (both subtrees disappear together) |

**Invariant the new fitness check enforces**: The `worktree/` subtree never contains a platform-generated file. `git status` inside the worktree never reflects platform state. (See [check_overlay_outside_worktree.py] sketch in [plan.md](plan.md#project-structure).)

---

## Cross-entity invariants

- **No two homes for manifest content** (Q1 of clarify session): the project's repo holds the canonical `.tagh/instance.yml`; the in-process Pydantic Manifest object exists during a single provision; the InferredProposal is single-use; the overlay's `projctl-config.yml` carries only the *boot commands list* extracted from the manifest, not the full manifest. Three places, no duplication, single source of truth.
- **No platform secrets in any on-disk artifact** (Principle IV): manifest is project-public; overlay carries env-var references; projctl-config carries the heartbeat URL and HMAC secret REFERENCE (not value) — actual values arrive via the parent process env at compose-up time.
- **No host ports in any merged compose document** (Principle V): substrate may declare them; overlay always strips them; merged document (verified by the contract test) has zero `ports:` entries on non-cloudflared services.
- **One PR-open per project** (R3 idempotency): `tagh/manifest-init` branch name is project-deterministic; concurrent attempts fast-fail "branch already exists" and the wrapper resolves to the existing PR.

---

## Migration path

The `instances.workspace_path` column semantic shift (per-instance root, was project worktree) is the only schema-adjacent change. Per [research.md §R5](research.md), this is an operational cutover, not a data migration: old rows are reaped before the new code path lands. No `alembic` migration is needed — the column type and name are unchanged, only its meaning at the consumer side.
