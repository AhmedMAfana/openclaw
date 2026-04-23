# Quickstart: Per-Chat Isolated Instances

**Audience**: a developer or reviewer who wants to validate the feature end-to-end on a local host.

**Preconditions**:
- Running on the TAGH Dev host (Docker 24+, Postgres, Redis, ARQ workers up).
- Cloudflare API token + `dev.<our-domain>` zone configured in `platform_config`.
- GitHub App registered and installed on at least one test repo (credentials in `platform_config`).
- `projctl:dev` image published (`ghcr.io/<org>/projctl:dev`).
- A test project registered with `mode='container'` pointing at a Laravel+Vue template repo.

---

## 1. Golden path — one chat, one instance

Follow this to verify **User Story 1 (P1)** and **User Story 4 (P1)**.

1. **Open a fresh chat** against the test project. Send a first message: "Run `php artisan serve` and `npm run dev`."
2. **Observe in the chat UI**:
   - A status transition `starting → ready` within ≤5 minutes (SC-002, cold path).
   - A preview URL of the form `https://inst-<8hex>.dev.<our-domain>` — **public, unguessable**, per spec Q1.
3. **Open the preview URL** in a browser. The Laravel welcome page loads.
4. **Edit a visible file** (e.g., change the `<h1>` in `resources/views/welcome.blade.php`). Either ask the assistant to do it, or edit directly through the IDE surface.
5. **The browser updates within 3 seconds** without a manual refresh (SC-005). HMR is working.
6. **Check Postgres**:
   ```sql
   select slug, status, expires_at from instances where chat_session_id = <yours>;
   ```
   Exactly one row, `status='running'`, `expires_at ≈ now() + 24h`.
7. **Check Cloudflare**: one named tunnel `tagh-inst-<slug>`, three DNS records (web, hmr, ide).

---

## 2. Cross-chat isolation (adversarial)

Validates **User Story 1 (P1)** invariants and SC-001 / SC-009.

1. Open **two chats concurrently** — call them chat A and chat B.
2. Each provisions its own instance; each gets its own preview URL.
3. In chat A, instruct the assistant:
   > "Try to read `/workspaces/inst-<B's slug>/app/config/app.php`."
4. Expected: the `workspace_mcp` refuses with a path-escape error. The file is unreadable. **Zero cross-chat reads in audit log** (SC-009).
5. In chat A, instruct the assistant:
   > "Restart the `cloudflared` service."
6. Expected: `instance_mcp` refuses because `cloudflared` is not in `--allowed-services`. Sidecar untouched.
7. Open the preview URLs for A and B in separate tabs. Edit chat A's app. **Only A's browser updates.** B's is untouched.

---

## 3. Inactivity & resume

Validates **User Story 2 (P1)** and **User Story 3 (P2)**.

1. Provision an instance as in §1.
2. In a dev shell, fast-forward the clock: `UPDATE instances SET last_activity_at = now() - interval '24 hours', expires_at = now() - interval '1 second' WHERE slug = 'inst-...';`
3. Wait for the reaper cron (≤5 min) — or trigger manually: `arq --run-once openclow.worker.inactivity_reaper`.
4. **Observe**:
   - Chat receives banner: "Environment ending in 60 minutes. Send a message or any activity to keep it alive."
   - `instances.status` flips to `idle`, `grace_notification_at` set.
5. **Send a chat message** before the grace window ends.
   - Status flips back to `running`. Banner clears. No teardown.
6. **Let the grace window expire** (fast-forward `grace_notification_at` to `now() - interval '61 min'`). Reaper cycle fires.
   - `status → terminating → destroyed`. Preview URL stops resolving. Tunnel deleted from Cloudflare. DNS records removed.
7. **Send a new chat message.** A fresh instance provisions (new slug, new UUID, new URL). The chat's working branch is reattached — your earlier code changes are still there (SC-004).

---

## 4. Manual terminate

Validates **User Story 5 (P2)**.

1. With an active instance, send `/terminate` in chat.
2. Immediate transition to `terminating`; tear-down completes within seconds.
3. `status='destroyed'`, `terminated_reason='user_request'`. No grace window, no banner.
4. New message → fresh instance (same flow as §3 step 7).

---

## 5. Per-user cap

Validates **FR-030a/b**.

1. Default cap is 3. Open chats 1, 2, 3 on any test projects — all provision normally.
2. Open chat 4. The first message returns a chat error: "You have 3 active chats. End one to start another." The message includes clickable links to chats 1/2/3 and a Main Menu button.
3. `/terminate` on chat 2; then retry chat 4. It provisions.
4. Operator raises the cap in `platform_config`: `key='per_user_cap', value='5'`. No restart needed — next provision reads the fresh value.

---

## 6. Failure path

Validates **User Story 6 (P2)** and FR-024–027.

1. Temporarily inject a failure into the test project's `guide.md`: set the `install-npm` step's command to `false`.
2. Open a new chat. Send the first message.
3. **Observe**:
   - The LLM fallback fires once (one `llm_attempt` event in the audit log).
   - The envelope sent is at most 200 lines of stdout + 200 of stderr; every secret redacted.
   - After 3 LLM attempts fail, `status='failed'` with `failure_code='projctl_up'`.
   - Chat shows a plain-language error: "Couldn't start your environment — npm install failed." Two buttons: **Retry** and **Main Menu**. No dead-end text.
4. Click **Retry**. `projctl up` resumes from the last-successful step (not from step 1). SC-007 + FR-025.

---

## 7. Upstream degradation banner

Validates **FR-027a/b/c**.

1. With an active instance, break the CF-tunnel creds file inside the sidecar (e.g., `docker exec tagh-inst-<slug>-cloudflared rm /etc/cloudflared/creds.json`).
2. Within ~30 s, the preview URL stops resolving.
3. **Chat banner appears**: "Preview URL temporarily unavailable. Retrying…"
4. `instances.status` remains `running` (not `failed`) — the instance is healthy, only the upstream path is broken.
5. Restore the creds file (or restart the sidecar). Banner clears automatically within 60 s. No user action required.

---

## 8. Teardown leaves zero residue

Validates **FR-006** and SC-003.

After `status='destroyed'`:

- `docker ps -a --filter name=tagh-inst-<slug>` returns nothing.
- `docker volume ls --filter name=tagh-inst-<slug>` returns nothing.
- `docker secret ls --filter name=tagh-inst-<slug>-cf` returns nothing.
- Cloudflare API `GET /accounts/:a/cfd_tunnel?name=tagh-inst-<slug>` returns an empty list.
- `GET /zones/:z/dns_records` for the three hostnames returns empty.
- `/workspaces/inst-<slug>/` does not exist on the host filesystem.

The `instances` row itself remains (chat-lifetime retention per spec Q3); only the runtime artefacts are gone.

---

## 9. Chat deletion cascade

Validates **FR-013a/b/c** and spec Q3.

1. With an active instance, delete the chat (user-facing action).
2. **Immediately observed**:
   - Instance is terminated (if active).
   - `instances` row deleted.
   - `instance_tunnels` row deleted (FK cascade).
   - `tasks` rows for that instance deleted (FK cascade).
   - Audit log entries with the instance's slug deleted (explicit service-level cleanup, not FK).
   - The chat's working branch is GC'd from the per-project cache by a background job.
3. Resume is no longer possible — the chat is gone.

---

## 10. Legacy mode untouched

Validates **FR-034** and **FR-036**.

1. Open a chat against a project with `mode='host'` (legacy). First message.
2. Behaviour is **identical to pre-refactor** — no instance row is created, no tunnel, no new code path. The router at the top of `bootstrap.py` picks the legacy flow.
3. Open a chat against a project with `mode='container'` (new). Instance provisioning as in §1.

Both coexist on the same host.

---

## Success-exit checks (to mark the feature done)

- All integration tests in [../../../tests/integration/](../../../tests/integration/) tagged `per_chat_instances` pass against real Docker + real Postgres + real Redis (stubbed CF).
- The compose-no-ports lint test passes for every template in [../../../src/openclow/setup/compose_templates/](../../../src/openclow/setup/compose_templates/).
- The MCP-manifest test asserts no "which instance" tool exists in any new-mode MCP server.
- Redactor unit tests cover all categories listed in Principle IV.
- The nightly E2E test against a real Cloudflare zone completes within SC-002's 5-minute budget on a cold run.
