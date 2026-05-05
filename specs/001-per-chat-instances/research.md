# Phase 0 Research: Per-Chat Isolated Instances

**Date**: 2026-04-23
**Plan**: [plan.md](plan.md)
**Spec**: [spec.md](spec.md)

This document resolves every open decision the plan depends on. Spec-level clarifications (Q1–Q5) are already recorded in [spec.md §Clarifications](spec.md#clarifications); they are not re-litigated here. Research covers technology choices, integration patterns, and prior-art adoption calls.

---

## 1. `projctl` implementation language — Go vs Python

**Decision**: **Go**.

**Rationale**:
- `projctl` ships as a single binary baked into arbitrary project images via `COPY --from=ghcr.io/<org>/projctl:<ver>`. Go produces a static binary with zero runtime dependency; Python requires either a PEX/pyoxidizer bundle (heavyweight) or a base image with the right Python version (contradicts the "bake into any project image" goal).
- The constitution (Architecture Constraints) explicitly authorises Go for `projctl` and forbids Node/TS in the runtime path. Go is the only language that simultaneously meets the constraint and the distribution goal.
- The orchestrator stays Python; `projctl` is a process-boundary peer, not a code-shared peer. No cross-language code reuse is required.

**Alternatives considered**:
- **Python + PEX bundle**: viable, but a PEX with the redactor + JSON schema validation weighs >10 MB and still requires `glibc` compatibility. Complexity not justified by code reuse.
- **Python + pyoxidizer**: more compact, but the build pipeline is much slower than `go build` and still couples `projctl` to a specific Python version.
- **Rust**: comparable to Go on deliverable (static binary), but the team has no Rust expertise and the ecosystem for JSON logs, YAML parsing, and subprocess control is less mature than Go's stdlib.

**Implications for plan**:
- New top-level `projctl/` module (Go 1.22+). Not in `src/taghdev/`.
- CI builds and pushes the image on merge to `main` with a `vN.N.N` tag read from `projctl/VERSION`.
- Orchestrator interacts with `projctl` only via `docker exec` + stdout JSON-line parsing. No shared library.

---

## 2. Cloudflare Tunnel API — named tunnels, DNS, rotation

**Decision**: Use Cloudflare's v4 REST API directly via `httpx.AsyncClient`. No SDK.

**API surface used** (all authenticated with one long-lived token in `platform_config` `category="cloudflare"` `key="api_token"`):

| Operation | Endpoint | Purpose |
|-----------|----------|---------|
| Create tunnel | `POST /accounts/:a/cfd_tunnel` | Named tunnel `tagh-inst-<slug>` → returns `tunnel_id` + `tunnel_token` (credential JSON). |
| List tunnels | `GET /accounts/:a/cfd_tunnel?name=tagh-inst-<slug>` | Teardown idempotency: re-find on retry. |
| Delete tunnel | `DELETE /accounts/:a/cfd_tunnel/:id` | Teardown step. |
| Rotate creds | `POST /accounts/:a/cfd_tunnel/:id/token` | Future use; not required for v1. |
| Create DNS record | `POST /zones/:z/dns_records` | CNAME `<slug>.dev.<domain>` → `<tunnel_id>.cfargotunnel.com`. |
| Delete DNS record | `DELETE /zones/:z/dns_records/:id` | Teardown step; record IDs re-queried on teardown (see §4 idempotency). |

**Rationale**:
- `cloudflare-python` exists but is mostly code-gen; the six operations we need are simple and adding a dependency violates constitution §Development Workflow ("no new dep without line-item justification").
- Direct httpx keeps timeout handling explicit (Principle IX).

**Alternatives considered**:
- `cloudflare-python` SDK — rejected for reasons above.
- cloudflared-CLI invocation for tunnel creation — rejected because CLI commands are not fully idempotent and inject shell-quoting risks.

**Constraints documented**:
- All CF API calls use `httpx.AsyncClient` with `timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)`.
- Token scopes required: `Account.Cloudflare Tunnel:Edit`, `Zone.DNS:Edit` on `dev.<our-domain>`. Documented in ops runbook (not in this repo).

---

## 3. GitHub App vs PAT for per-instance push auth

**Decision**: **GitHub App** with per-deployment installation; `CredentialsService.github_push_token(instance_id)` mints an installation token scoped to the one repo bound to the instance's project, TTL = 1 hour (GitHub's maximum).

**Rationale**:
- Principle IV requires short-lived credentials scoped to the instance; a shared PAT (today's state, audit §Findings) violates this on both axes.
- GitHub App installation tokens are the only GitHub-native mechanism that yields sub-day TTL + per-repo scope.
- Tokens are minted just-in-time (on push), never persisted beyond the container's lifetime. Rotation is automatic (re-mint on expiry).

**Token lifecycle**:
1. Orchestrator holds the App's private key + App ID (in `platform_config`).
2. `CredentialsService.github_push_token(instance_id)` looks up the instance's project → repo → installation ID (memoised), signs a JWT (10-min TTL), exchanges for an installation token (1-hour TTL) scoped to `contents:write` + `pull-requests:write` on that one repo.
3. Token injected into the `app` container's env as `GITHUB_TOKEN`, also written to `~/.git-credentials` via the standard credential helper.
4. `projctl rotate-git-token` runs every 45 min inside the instance, calling the orchestrator's internal `/internal/instances/<slug>/rotate-git-token` endpoint (authenticated with heartbeat HMAC) to receive a fresh token before the old one expires.
5. On teardown, no token persists (container is gone; the installation token expires within an hour regardless).

**Rejected**:
- Per-user OAuth tokens — viable for user-initiated actions but not for the background push path; push happens while the user is not present at a device.
- Shared PAT (status quo) — fails Principle IV on scope and TTL.

---

## 4. Idempotency keys for lifecycle operations

**Decision**: Every lifecycle operation uses the instance `slug` as its idempotency key. On retry, the operation queries DB state + live infra state (CF tunnels by name, Docker containers by compose project) and forward-completes or no-ops — never creates duplicate resources.

**Operation-by-operation recovery paths**:

| Operation | Idempotency key | Re-run behaviour |
|-----------|----------------|------------------|
| `provision(chat_session_id)` | `instances.slug` | Look up existing row; if `status='provisioning'`, resume from the recorded step (see projctl state.json §7). If `status='failed'`, teardown first, then re-create. If `status='running'`, no-op. |
| `destroy(instance_id)` | `instances.slug` | `docker compose down -p tagh-inst-<slug>` is already idempotent. CF tunnel deleted only if still present (list by name → skip if missing). DNS records re-queried and deleted only if still present. Row flipped to `terminated` only after all three succeed. |
| `rotate_credentials(instance_id)` | `instance_tunnels.instance_id` | Old creds revoked only after new creds successfully injected + `cloudflared` reconnects. If mid-rotation crash, reaper re-enters via `status='rotating'`. |
| `reap()` | `instances.id` (per row, `FOR UPDATE SKIP LOCKED`) | Running the reaper 5× in a row is identical to running it once. Grace-window notification is deduped by a per-instance "notified_at" marker. |

**Rationale**:
- Principle VI demands idempotency + durable state. A worker crash mid-provision must not leave orphan tunnels or half-created containers.
- The slug is deterministic (derived once from UUID at row creation) and is already used everywhere (DNS, compose project, volumes, audit logs), so it's the natural idempotency key.

**Partial-success inventory** (drawn from arch doc §5.2 steps):
1. CF tunnel created but DB row not written — reaper finds orphan tunnel via `list tunnels name=tagh-inst-*` → delete.
2. DB row written but CF tunnel missing — provision re-runs, re-creates tunnel.
3. Docker compose started but cloudflared not connected — healthcheck retries; if persistent, transition to `failed` after timeout.
4. Teardown half-done (DNS deleted, CF tunnel still present) — destroy retries only the missing steps.

---

## 5. Adoption-or-build calls for third-party prior art

Per the working agreement (project memory: "Daytona, Cloudflare Sandbox SDK, DockFlare, Runme, and the selfhealing-action pattern are 'adopt or steal, don't rebuild' candidates"), each is evaluated:

| Candidate | Adoption decision | Reasoning |
|-----------|-------------------|-----------|
| **Daytona** (`daytonaio/daytona`) | **Don't adopt** | Daytona is a full workspace-as-a-service platform; adopting it means re-hosting the orchestrator inside Daytona's model. Our orchestrator (`chat_task.py`, `orchestrator.py`) is the control plane; Daytona would compete for that role. Steal the concept of "one workspace = one container stack"; do not take the code. |
| **Cloudflare Sandbox SDK** (`cloudflare/sandbox-sdk`) | **Don't adopt** | Early-stage, targets Cloudflare Workers. Our instances run on our own host; the SDK's execution model (JS Workers) is wrong for Laravel+Vue. |
| **DockFlare** | **Steal pattern** | DockFlare shows the shape of a Docker + Cloudflare Tunnel integration service; useful as a reference for *how* to auto-manage tunnel configs when containers come and go. We are NOT importing it — our compose-per-instance model is stricter than DockFlare's label-based discovery. But the DockFlare README is cited in `docs/research/prior-art.md` for anyone new to the problem. |
| **Runme** (`stateful/runme`) | **Defer** | Runme is a `guide.md`-runner in Go and has good ergonomics. Adopting it means our `projctl up` wraps Runme instead of reimplementing the step runner. **Decision: revisit at projctl PR #2** — if Runme's CLI gives us JSON-line output and success-check semantics cleanly, vendor its binary inside `projctl`. If not, implement natively. Not blocking for the plan. |
| **selfhealing-action** pattern | **Adopt the pattern, not the code** | The pattern ("on failure, build a bounded context envelope and ask an LLM for a structured response") is exactly the LLM fallback in arch §9. Already incorporated into FR-024–027 and the envelope schema in `contracts/llm-fallback-envelope.schema.json`. |

**Every "build from scratch" call** is justified above. Remaining components (`InstanceService`, redactor module, compose renderer) have no credible prior art to adopt — they are project-specific.

---

## 6. Vite HMR over Cloudflare Tunnel — empirical configuration

**Decision**: Use the config snippet from arch doc §5.4 verbatim, with these explicit constraints:

- **Separate hostname for HMR**: `hmr-<slug>.dev.<domain>` routes to `http://node:5173` via the sidecar. Mixing path-based and host-based routing on the same hostname is known to break WebSocket upgrades (upstream cloudflared issue #X, cited in `docs/research/prior-art.md`).
- **`clientPort: 443`** in Vite config — browser always connects over TLS regardless of origin port, because Cloudflare Tunnel terminates TLS at the edge.
- **`noTLSVerify: true` on the HMR ingress** — `cloudflared` speaks plain HTTP to `node:5173` inside the compose network (Vite doesn't serve TLS intra-network). Edge is WSS because Cloudflare wraps it. Safe because the compose network is egress-only.
- **Env-var contract** (FR-018): orchestrator injects `INSTANCE_HOST=<slug>.dev.<domain>` + `INSTANCE_HMR_HOST=hmr-<slug>.dev.<domain>`. Projects must honour these names; concatenating their own hostnames is a bug.

**Verification**: Integration test `test_provision_teardown_e2e.py` opens the HMR URL with a WebSocket client, edits a watched file, asserts the HMR payload arrives within 3 s. SC-005 is the gate.

---

## 7. `projctl` on-disk state for resumability

**Decision**: `/var/lib/projctl/state.json` on a per-instance Docker named volume (`tagh-inst-<slug>-projctl-state`).

**Schema** (documented in `contracts/projctl-stdout.schema.json`):

```json
{
  "guide_version": "<sha256 of guide.md>",
  "steps": {
    "install-php":  { "status": "success", "finished_at": "..." },
    "install-node": { "status": "success", "finished_at": "..." },
    "migrate":      { "status": "failed", "attempt": 2, "last_error": "..." }
  },
  "last_heartbeat_at": "..."
}
```

**Rationale**:
- Resumability is a constitution requirement (VI). A step that succeeded before must not re-run after an orchestrator crash or instance restart.
- State on a named volume survives `docker compose restart` but is destroyed by `docker compose down -v` (intentional — teardown must clear everything).
- Keying steps by name (not by position) tolerates `guide.md` edits mid-flight: if the guide SHA changes, `projctl` treats all steps as unrun and starts over.

**Alternatives rejected**:
- State in Postgres — violates the constraint that `projctl` has no direct DB connection (only orchestrator touches DB). Would require an extra HTTP round-trip on every step.
- State on the workspace volume — risk of conflict with project files; separation of concerns preferred.

---

## 8. Heartbeat authentication

**Decision**: HMAC-SHA256 over the request body, using a per-instance secret generated at provisioning and injected as `HEARTBEAT_SECRET` env var.

**Why HMAC not JWT**:
- HMAC is cheaper to verify and has no token-expiry handling to get wrong.
- Secret rotates every time the instance is re-provisioned (new instance = new secret); there's no long-lived secret to leak.
- Heartbeat traffic is internal (instance → orchestrator's internal endpoint); public JWT discovery is not needed.

**Implementation**:
- Secret is 32 random bytes, base64-encoded, 44 chars.
- Stored on `instances.heartbeat_secret` (add column to migration — noted in data-model.md).
- Orchestrator endpoint verifies HMAC; rejected requests bump a per-instance counter; >10 rejections in 5 min trips an alert (`instance.heartbeat_auth_fail`).

---

## 9. Per-user quota enforcement

**Decision**: Enforce `per_user_cap` at `InstanceService.provision(chat_session_id)` via a counting query:

```sql
SELECT count(*) FROM instances i
  JOIN web_chat_sessions s ON s.id = i.chat_session_id
  WHERE s.user_id = :user_id
    AND i.status IN ('provisioning', 'running', 'idle');
```

If `count >= per_user_cap` (default 3, configurable via `platform_config` `category="instance"` `key="per_user_cap"`), raise `PerUserCapExceeded` with the list of active chats. `chat_task.py` translates this into the error message required by FR-030a/b (distinct from platform-capacity error) with navigation to the user's active chats.

**Race safety**: Wrap provision in a Redis lock `taghdev:user:<user_id>:provision` (held only for the count+insert, not the full compose-up). This prevents two simultaneous first-messages from both succeeding past the cap.

**Alternative rejected**: DB-level advisory lock on `user_id`. Redis is already the ephemeral-lock layer per Principle VI; using it is more consistent than introducing Postgres advisory locks.

---

## 10. Retention cascade (Q3 integration)

**Decision**: A Postgres `ON DELETE CASCADE` on the `instance_id` FK in `web_chat_sessions` and `tasks` handles instance removal when a chat is deleted. The `instance_tunnels.instance_id` FK already cascades per arch §3.2. Audit log entries reference instance by `instance_slug` (a text column), not a FK — they must be cleaned up by a service-level operation, not a cascade.

**Implementation plan** for FR-013b (chat deletion):
- Add `ChatSessionService.delete(chat_session_id)` path (if not present) that, in one transaction: (a) triggers synchronous teardown of any running/idle instance, (b) deletes the chat session row (cascading to FKs), (c) deletes audit log entries with matching `instance_slug`, (d) schedules the per-project cache to be GC'd of this chat's branch via a background ARQ job.

**Why**: FK cascades are reliable for tabular data; audit entries keyed by slug require an explicit step. Both must complete before the "chat deleted" promise is satisfied.

---

## 11. Reaper activity-source wiring

**Decision**: Two write paths bump `instances.last_activity_at` + recompute `expires_at`; one read path (reaper) consumes the result.

**Write paths**:
- Chat handler (`chat_task.py`) calls `InstanceService.touch(instance_id)` on every inbound message. Cheap: one indexed UPDATE.
- `projctl heartbeat` hits `POST /internal/instances/<slug>/heartbeat` every 60 s while the dev server, a running task, or an attached shell is active. Endpoint also calls `InstanceService.touch(instance_id)`.

**Read path**:
- ARQ cron every 5 min. Two-phase query: first transition `running → idle` for rows with `expires_at <= now()` and no `grace_notification_at` (this also emits the chat banner); then transition `idle → terminating` for rows where `grace_notification_at + grace_window <= now()`.

**Why two paths**: FR-009 + FR-010 together. Chat-message activity and in-environment activity are the two SoT sources; both reset the clock. Raw HTTP traffic is explicitly NOT a source (FR-011).

---

## 12. Test coverage gates

**Decision**: The following tests MUST exist and pass before PR 12 (the `bootstrap.py` router flip) lands:

1. **Compose no-ports lint** (`tests/integration/test_compose_no_ports_lint.py`) — renders every per-instance compose and fails on any non-`cloudflared` `ports:` (Principle V enforcement).
2. **MCP manifest no-ambient-identifier** (`tests/unit/test_mcp_manifest.py`) — asserts no tool exposed by `instance_mcp`/`workspace_mcp`/`git_mcp` has an "instance" or "project" or "workspace" identifier in its argument schema (Principle III enforcement).
3. **Provisioning idempotency** (`tests/integration/test_provision_idempotent.py`) — run `provision` N times; assert exactly one tunnel, one compose project, one DB row.
4. **Redactor coverage** (`tests/unit/test_audit_redactor.py`) — asserts the redactor masks bearer tokens, AWS/GCP keys, CF creds, SSH keys, and `.env`-style matches; asserts both chat-UI and LLM-fallback paths call the redactor.
5. **Cross-instance isolation (adversarial)** (`tests/integration/test_agent_isolation.py`) — spawn MCP fleet bound to inst-A; try every known escape (path traversal, service-name forgery, branch switch, repo URL mutation); assert every attempt fails.
6. **HMR E2E** (`tests/integration/test_provision_teardown_e2e.py`) — already listed; SC-005 gate.
7. **Per-user cap** (`tests/integration/test_per_user_cap.py`) — assert 3 concurrent OK, 4th returns distinct error with navigation.

---

## 13. Open items deferred to implementation PRs

Not blockers for Phase 1 design; noted for follow-up per PR:

- **IDE-in-browser surface** (code-server vs Theia vs defer) — arch §13 item 2. Plan reserves DNS slot; decision deferred to a post-v1 PR.
- **Image pre-warming** — arch §13 item 3. Measure SC-002 first; warm if we miss it.
- **Idle-TTL per-project override** — arch §13 item 5. Add when the first customer asks.
- **Runme vendor-or-build** — §5 above. Revisit at projctl PR #2.

All are product/ops decisions, not architectural gates.
