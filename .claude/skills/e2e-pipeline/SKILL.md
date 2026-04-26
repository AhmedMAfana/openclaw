---
name: "e2e-pipeline"
description: "Live end-to-end pipeline test for the per-chat-instances feature. Drives a real chat through the entire pipeline via Playwright MCP: chat creation → provision → tunnel → app reachable → workspace edit → HMR → git push → multi-chat isolation → terminate. Captures screenshots + logs + DB state at every phase boundary into artifacts/e2e-<ts>/. When a phase fails, classifies the failure (env/infra/template/code), applies a hot-fix to unblock, then a root-fix in code/template/Dockerfile, then re-runs the phase. Honest about what was actually exercised vs what the UI state machine merely simulated. Run AFTER /pipeline-audit (which gates static contracts) and BEFORE claiming a release-readiness verdict."
argument-hint: "Optional: --phase=<name> to run a single phase. --skip-multi to skip Phase 7 (faster). --keep to skip Phase 9 teardown so you can inspect state."
user-invocable: true
disable-model-invocation: false
---

## User Input

```text
$ARGUMENTS
```

If `$ARGUMENTS` is empty, run all phases. Parse `--phase=<name>`, `--skip-multi`, `--keep` flags.

## Goal

Prove the per-chat-instances pipeline works **end-to-end with a real container, real Cloudflare tunnel, real workspace, real git push** — not just the UI state machine.

This skill is the antidote to the failure mode where I claim "8/8 e2e tests pass" but actually only exercised the chat frontend's banner/card render logic. A green report from this skill means: a real Laravel/Vue scaffold booted in a real container, a real public URL served real HTML, a real file edit triggered a real Vite HMR refresh, a real `git push` landed in the real GitHub repo.

## Operating Constraints

- **Honest scope reporting.** Every phase report says exactly what was exercised AND what wasn't. If Cloudflare creds are missing and we couldn't bring up the tunnel, the phase is "skipped — no creds", not "passed".
- **Forensic capture.** Every phase boundary captures: screenshot, recent logs of relevant services (api, worker, cloudflared if up, the per-instance compose stack), DB rows for the affected instance, `docker ps` snapshot. Lands under `artifacts/e2e-<timestamp>/<phase>/`.
- **Hot-fix + root-fix.** When a phase fails because of infra/config, fix it BOTH ways: a hot patch (env var, restart, exec into container) so we can keep going, AND a root patch (Dockerfile, compose template, code) so the next run doesn't break the same way. Document both in the phase report.
- **No silent retries.** A phase fails → analyze → fix → explicitly re-run that phase with the same artifact-capture. Do NOT loop blindly.
- **Bounded runtime.** First-run cold-build phases can take 10-15 min (Laravel image pull, npm install, etc.). Set a hard ceiling per phase; if exceeded, capture and stop with a clear blocker report.
- **Static gate first.** Always run `/pipeline-audit` before Phase 1. If the static contract suite has any failure at HIGH or above, abort with a pointer at the offending check — there is no point spending 15 min on a live test when the contracts are already broken.

## Layered architecture this exercises

| Layer | What this skill proves |
|---|---|
| Schema + codegen | Implicit — already gated by `/pipeline-audit`. |
| Runtime stream events | Phase 3 onwards exercises real `controller.add_data` paths end-to-end. |
| ARQ job pipeline | Phase 3 enqueues `provision_instance`, Phase 9 enqueues `teardown_instance` — proves the worker registry has them. |
| Compose template | Phase 4-5 proves the Laravel/Vue template renders + boots + exposes only via cloudflared. |
| MCP fleet | Phase 6 calls workspace MCP write, Phase 7 calls git MCP commit/push — proves the LLM-facing tool surface works. |
| Lifecycle | Phase 8-9 proves terminate is idempotent and cleans up Cloudflare + Docker + DB. |

## Phase catalogue

```
Phase 0  preflight        Static gate + infra readiness check.
Phase 1  pick-project     Find a container-mode project with real GitHub creds.
Phase 2  new-chat         Open chat frontend in Playwright, create chat.
Phase 3  provision        Send first message → watch provision through banner.
Phase 4  app-live         Open the tunnel URL → verify app HTML loads.
Phase 5  workspace-edit   Write to a file via workspace MCP.
Phase 6  hmr              Verify Vite HMR pushed the change to the live app.
Phase 7  git-push         git_commit + git_push via git MCP (skipped if --skip-git).
Phase 8  multi-chat       Open second chat → independent instance + tunnel (skipped if --skip-multi).
Phase 9  terminate        /terminate the test instance(s); verify DB + tunnel + container all gone.
Phase 10 report           Final markdown report with artifact links, screenshots, honest scope notes.
```

## Execution

### Phase 0 — preflight

1. Run `/pipeline-audit` (or shell out to `python scripts/pipeline_fitness.py --fail-on high`). If exit ≠ 0, abort with the audit report — do not proceed to live test.
2. Run `python scripts/e2e/preflight.py`. This script returns JSON with:
   - `services`: are `api`, `worker`, `postgres`, `redis` all healthy in `docker compose ps`?
   - `cloudflare`: is `CLOUDFLARE_API_TOKEN` (or whichever env var the tunnel manager uses) present in the worker?
   - `github`: is at least one project in the DB with `mode='container'` AND a `repo_url` AND seeded credentials?
   - `mcp`: is `playwright-mcp` reachable (`docker exec tagh-devops-worker-1 which playwright-mcp`)?
   - `compose_templates`: does `setup/compose_templates/laravel-vue/compose.yml` exist?
3. Any "no" → BLOCKER. Report which one + suggested fix. Do not proceed.
4. Create `artifacts/e2e-$(date +%Y%m%d-%H%M%S)/` and treat it as `$RUN_DIR` for the rest of the run.

### Phase 1 — pick-project

1. SQL the dashboard for a usable project: `mode='container'`, `repo_url IS NOT NULL`, status `active`. Prefer a project explicitly tagged `e2e` or `test` in its name; fall back to the first match. Do NOT pick a production-named project — abort and ask.
2. Verify the project's GitHub PAT is present in the platform credentials table (or wherever the seeder put it). The `_pick_project` helper in `scripts/e2e/preflight.py` handles this.
3. Capture the chosen project row to `$RUN_DIR/01-pick-project/project.json`.
4. If no usable project exists, BLOCKER: tell the user exactly which dashboard form to fill, with the URL. Do not silently create one — projects carry credentials, the user must own that decision.

### Phase 2 — new-chat

1. `mcp__playwright__browser_navigate` to `http://localhost:8000/chat/`.
2. Take screenshot → `$RUN_DIR/02-new-chat/01-landing.png`.
3. Click "New chat" (or whatever the entry button is — capture a `browser_snapshot` first to see the live DOM).
4. In the project picker, select the project from Phase 1.
5. Take screenshot of the new empty chat → `$RUN_DIR/02-new-chat/02-empty-chat.png`.
6. Read the new thread ID from the URL (`/chat/<id>`). Save it to `$RUN_DIR/02-new-chat/thread_id.txt`.

### Phase 3 — provision

1. Type "hello, please confirm you can see this workspace" in the composer; click send.
2. Within 3 seconds: capture `$RUN_DIR/03-provision/01-banner-provisioning.png` — must show the **provisioning** banner. If it doesn't, this is a real frontend regression — capture browser console logs, fail the phase.
3. Tail logs in parallel (background bash with `run_in_background=true`):
   - `docker compose logs -f api worker | grep -iE "instance|provision|tunnel|error"` → `$RUN_DIR/03-provision/api-worker.log`
   - `docker compose logs -f --tail=0 api | grep -iE "controller.add_data|stream"` → `$RUN_DIR/03-provision/stream-events.log`
4. Poll the DB every 5s (max 6 min): `SELECT slug, status, failure_code FROM instances WHERE chat_session_id = <id> ORDER BY created_at DESC LIMIT 1;`. Save snapshots to `$RUN_DIR/03-provision/db-poll.jsonl`.
5. Success criteria: instance row reaches `status='running'` AND tunnel URL is non-null AND `failure_code IS NULL`.
6. Failure modes (each gets a specific diagnostic):
   - `compose up` failed → grab `docker compose -f <per-instance compose> logs` for the per-instance stack, classify (image pull / port collision / missing env var). Hot-fix: docker prune, retry. Root-fix: bump template, document in `setup/compose_templates/laravel-vue/`.
   - Cloudflare tunnel never came up → grab `cloudflared` logs, check token validity. Hot-fix: rotate token in env. Root-fix: add retry/backoff in `services/instance_service.py::_provision_tunnel`.
   - Stuck in `provisioning` past timeout with no error → backend orchestrator hung. Capture `arq` worker stack via `docker exec worker py-spy dump --pid 1` if possible. Open a finding.

### Phase 4 — app-live

1. Read tunnel URL from the instance row.
2. `mcp__playwright__browser_navigate` to the tunnel URL.
3. Wait for the page to load (`browser_wait_for` text "Laravel" or "Welcome" — adjust to whatever the scaffold ships).
4. Screenshot → `$RUN_DIR/04-app-live/01-app-home.png`.
5. `browser_console_messages` → `$RUN_DIR/04-app-live/console.json`. Any `error` level fails the phase.
6. `browser_network_requests` → `$RUN_DIR/04-app-live/network.json`. Any non-2xx for static assets fails the phase.
7. If the tunnel resolves but the app returns 502/503: the app inside the container isn't ready. Hot-fix: `docker exec` into the container, restart php-fpm or vite. Root-fix: add a healthcheck to the compose template.

### Phase 5 — workspace-edit

1. Identify a target file inside the workspace — start with `resources/js/Pages/Welcome.vue` (Laravel Breeze scaffold) or whatever the template's index page is. Save the choice + original contents to `$RUN_DIR/05-workspace-edit/01-original.txt`.
2. Use the workspace MCP `fs_write` (or whichever tool name is registered — check `mcp_servers/workspace_mcp.py`) to write a marker into the page. The marker MUST be a unique string like `E2E-MARKER-<timestamp>` so we can verify it on the rendered page.
3. Read the file back via `fs_read` and confirm the marker is present. Save to `$RUN_DIR/05-workspace-edit/02-after.txt`.
4. If the workspace MCP isn't reachable from this session, the `mcp_servers/workspace_mcp.py` registration is broken — capture the MCP list (`claude mcp list` if available) and fail with a pointer at the registration code.

### Phase 6 — hmr

1. Wait 3-5 s for Vite HMR to push the change.
2. Re-screenshot the live tunnel URL → `$RUN_DIR/06-hmr/01-after-edit.png`.
3. Use `browser_evaluate` with `document.body.innerText.includes('E2E-MARKER-...')` to confirm. Capture the boolean result.
4. If the marker is NOT in the rendered page:
   - Check Vite logs in the per-instance compose stack: `docker compose -f <per-instance compose> logs vite | tail -50` → `$RUN_DIR/06-hmr/vite.log`.
   - Common issue: Vite host config doesn't accept the cloudflared hostname. Hot-fix: `docker exec` and restart vite with `--host 0.0.0.0`. Root-fix: bump `setup/compose_templates/laravel-vue/vite.config.js` to set `server.allowedHosts: 'all'` or the specific tunnel pattern.

### Phase 7 — git-push (skip if `--skip-git`)

1. Use the git MCP `git_status` to confirm the marker file shows as modified.
2. `git_commit` with message `e2e: HMR marker (auto-cleanup at phase 9)`.
3. `git_push` to the project's branch (do NOT push to main — confirm the branch is the per-chat ephemeral branch first).
4. Verify via `gh api` (host has `gh` installed) that the commit landed.
5. If the push fails on auth → the per-instance git token didn't get rotated into `~/.git-credentials`. Hot-fix: re-run `rotate_github_token` job manually. Root-fix: investigate why bootstrap skipped the rotate step.

### Phase 8 — multi-chat (skip if `--skip-multi`)

1. Open a second chat in the SAME browser context (new tab via `browser_tabs`).
2. Trigger a provision (same as Phase 3) for the same project.
3. Verify both instances coexist:
   - Different slugs in the DB.
   - Different tunnel URLs.
   - Both `status='running'`.
4. Screenshot both apps side-by-side — `$RUN_DIR/08-multi-chat/01-tabs.png`.
5. If the second chat reuses the first's instance, the `get_or_resume` logic is wrong — capture the lookup query and reference `services/instance_service.py::get_or_resume`.
6. If the second chat fails with `instance_limit_exceeded` despite the user being under cap, the cap accounting is wrong — capture the `_count_active_instances` query.

### Phase 9 — terminate (skip if `--keep`)

1. In each chat: type `/terminate`, click the **Confirm end_session** button on the card.
2. Verify within 30 s:
   - DB row `status='terminated'`, `terminated_at IS NOT NULL`.
   - `docker ps` no longer shows the per-instance containers.
   - Cloudflare tunnel deleted (or at least named-tunnel cleanup queued — depends on impl).
3. Screenshot the final main-menu state → `$RUN_DIR/09-terminate/01-after-terminate.png`.

### Phase 10 — report

Write the final report to `$RUN_DIR/REPORT.md`. Structure:

```
# E2E pipeline run — <timestamp>

## Verdict
PASS | PASS-WITH-CAVEATS | FAIL

## Scope
What ran for real:
- [ ] Real container provisioned via compose
- [ ] Real Cloudflare tunnel resolved
- [ ] Real app HTML loaded over the tunnel
- [ ] Real file edit + Vite HMR observed in the browser
- [ ] Real git push landed in GitHub
- [ ] Real multi-chat isolation verified
- [ ] Real terminate cleaned up infra

What was skipped + why.

## Per-phase results
(Phase 0...9 with pass/fail, artifact paths, screenshots, log excerpts)

## Bugs caught + fixed live
(For each: phase, symptom, root cause, hot-fix, root-fix commit ref)

## Bugs caught + NOT fixed
(With recommended next action.)

## Artifacts
$RUN_DIR/
```

Print the path to the report at the end so the user can open it.

## Honest reporting rules

- A phase that didn't actually run a real container is NOT a pass. It's "skipped — environment couldn't support it".
- A screenshot of the chat frontend showing a "provisioning" banner is NOT proof of provision. The DB row reaching `status='running'` AND the tunnel URL serving real bytes IS proof.
- Hot-fixes that aren't followed by root-fixes get logged as **technical debt** in the final report — the next run will hit the same issue. Don't paper over.
- If you ran out of turns mid-phase, the verdict is **FAIL — incomplete**, not "passed up to phase X".

## When to invoke

- Before cutting a release tag.
- After any change to: `instance_service.py`, `worker/tasks/instance_tasks.py`, `setup/compose_templates/`, `mcp_servers/`, `providers/llm/claude.py::_mcp_*`, the chat frontend's instance-event handlers.
- As the **definitive** sign-off on the per-chat-instances feature, paired with `/pipeline-audit` (static gate) and the unit/contract test suites under `tests/`.

## What this skill is NOT

- Not a load test (Phase 8 only opens 2 chats; load goes via `tests/load/`).
- Not a security test (no privilege-boundary fuzzing — that's a separate skill if needed).
- Not a UI snapshot regression (no pixel-diff; we only assert presence of marker text + non-error console).
- Not a replacement for `/pipeline-audit` — it RUNS audit first as a gate.
