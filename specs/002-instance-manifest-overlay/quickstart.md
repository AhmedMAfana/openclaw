# Quickstart — Instance Manifest + Platform Overlay

**Spec**: [spec.md](spec.md) · **Plan**: [plan.md](plan.md) · **Research**: [research.md](research.md) · **Data model**: [data-model.md](data-model.md)

Two flows. Skip to the one you need.

---

## A — Project owners: onboarding your repo to TAGH

You have a GitHub repo that runs locally with `docker compose up`. You want an isolated TAGH chat instance to be able to boot it, give you a public preview URL, and let the assistant edit the code.

### Step 1: Add the manifest to your repo

Create `.tagh/instance.yml` in your repo. For a stock Laravel Sail project (this is the canonical example):

```yaml
apiVersion: tagh/v1
kind: Instance
spec:
  compose: docker-compose.yml
  primary_service: laravel.test
  ingress:
    web:
      service: laravel.test
      port: 80
    hmr:
      service: laravel.test
      port: 5173
  boot:
    - composer install --no-interaction --prefer-dist
    - php artisan key:generate --force
    - php artisan migrate --force
    - npm install
    - npm run dev -- --host 0.0.0.0
  env:
    required:
      - APP_KEY
      - DB_PASSWORD
    inherit:
      - GITHUB_TOKEN
      - HEARTBEAT_SECRET
      - HEARTBEAT_URL
      - CF_TUNNEL_TOKEN
```

Adjust `primary_service`, the ingress port(s), and the boot commands to match your project. Required fields: `apiVersion`, `kind`, `spec.compose`, `spec.primary_service`, `spec.ingress.web`. Everything else is optional.

### Step 2: Commit and push

```bash
git add .tagh/instance.yml
git commit -m "chore: add TAGH instance manifest"
git push
```

That's the only repo change. Local `docker compose up` continues to work exactly as before — the manifest is platform metadata, not a runtime config the project itself reads.

### Step 3: Open a chat in TAGH

Pick the project, type your first message, watch the live URL come up.

### Don't want to write the manifest yourself?

Open a chat against the project as-is. If TAGH recognizes your project's shape (currently: Laravel Sail), it'll propose a manifest in the chat with the rationale for each field. Two actions:

- **Confirm** — TAGH provisions with the proposed manifest AND opens a pull request against your repo adding the manifest file. You review the PR like any other.
- **I'll add it myself** — TAGH cancels provisioning and shows you the proposed YAML to paste into your editor. Add it to your repo, push, re-open the chat.

If TAGH doesn't recognize your project shape, the chat tells you and points at this quickstart. There's no third "auto-detect with low confidence" path — silent guesses cause more trouble than they save.

### What stays in your repo

Just `.tagh/instance.yml`. Nothing else committed by TAGH. The platform writes a `compose.override.yml` and other sidecar configs, but those live in a platform-owned directory **outside** your worktree — `git status` inside your project never reflects platform state.

### What runs locally vs in TAGH

| | Local (your laptop) | TAGH instance |
|---|---|---|
| Boot command | `docker compose up` | `docker compose -f your.yml -f platform/override.yml up -d` |
| Public URL | none (or a dev tunnel you set up yourself) | per-instance `https://inst-<slug>.apps.tagh.co.uk` |
| Host ports | bound per your compose | none (Principle V — all traffic via the named tunnel) |
| Bootstrap commands | you run them manually | TAGH runs the manifest's `spec.boot[]` automatically |

---

## B — Platform devs: running the implementation locally

Prerequisites:

- Docker Desktop (or any Docker daemon with the compose plugin) running.
- The TAGH platform stack up: `docker compose up` from the repo root.
- A test GitHub repo with `.tagh/instance.yml` already committed (the canonical one is `AhmedMAfana/tagh-fre`; the manifest is added in this feature's PR).
- Cloudflare + GitHub PAT credentials seeded in `platform_config` (see [docs/setup/CREDENTIALS.md](../../docs/setup/CREDENTIALS.md) and `scripts/seed_platform_creds.py`).

### Run the unit tests for this feature

```bash
docker compose exec worker pytest tests/unit/test_instance_manifest_service.py -v
docker compose exec worker pytest tests/unit/test_instance_inference_service.py -v
docker compose exec worker pytest tests/unit/test_instance_overlay_service.py -v
docker compose exec worker pytest tests/unit/test_github_pr_service.py -v
```

### Run the contract test

The contract test feeds synthetic project compose-files through the overlay generator and asserts the merged document via `docker compose -f a -f b config`:

```bash
docker compose exec worker pytest tests/contract/test_overlay_compose_layering.py -v
```

### Run the integration test (real docker boot)

```bash
docker compose exec worker pytest tests/integration/test_provision_with_manifest.py -v
docker compose exec worker pytest tests/integration/test_provision_without_manifest_proposes.py -v
```

These spin up real per-instance containers against the local Docker daemon. They're slow (~60s each) but catch real issues that mocked unit tests can't.

### Run the static gate (fitness audit)

```bash
python3 scripts/pipeline_fitness.py --fail-on high
```

After this feature lands, the suite includes three new checks:

- `no_app_template_shipped` — fails if any framework-specific compose template, app image reference, or per-framework user Dockerfile lives under platform-owned directories.
- `overlay_strips_host_ports` — feeds synthetic project compose-files through the overlay generator and asserts every non-cloudflared service has empty `ports:` in the merged result.
- `manifest_for_container_projects` — for every active `mode='container'` project, asserts either the project's GitHub default branch HEAD has `.tagh/instance.yml` OR the project shape is auto-detectable by the inference service (Sail rules in v1).

### Run the live e2e regression

```bash
# From a Claude Code session in this repo:
/e2e-pipeline
```

This is the same skill that caught the original `tagh/laravel-vue-app:latest` blocker. Re-running it after this feature should reach phase 9 (terminate) green for the first time. See [.claude/skills/e2e-pipeline/](../../.claude/skills/e2e-pipeline/) for what each phase does.

### Inspecting a live instance

```bash
# What slug does this chat have?
docker compose exec postgres psql -U taghdev -d taghdev -c \
  "SELECT slug, status FROM instances WHERE chat_session_id = <CHAT_ID>;"

# What does the per-instance root look like?
docker compose exec worker ls -la /workspaces/inst-<slug>/

# What did the overlay generator emit?
docker compose exec worker cat /workspaces/inst-<slug>/_platform/compose.override.yml

# What's the merged compose document docker actually sees?
docker compose exec worker docker compose \
  -p tagh-inst-<slug> \
  -f /workspaces/inst-<slug>/worktree/docker-compose.yml \
  -f /workspaces/inst-<slug>/_platform/compose.override.yml \
  config

# What did projctl run?
docker compose exec worker docker logs tagh-inst-<slug>-projctl-1 | tail -100
```

---

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| Chat shows "manifest at `.tagh/instance.yml` is invalid: …" within 5 seconds | Manifest YAML doesn't validate against [contracts/manifest.schema.json](contracts/manifest.schema.json) | Read the field path in the message; fix in your repo; push; resend the chat message. |
| Chat shows "primary_service `X` not found in your compose" | Manifest's `spec.primary_service` doesn't match a service in `spec.compose` | Either rename your service or update the manifest's `primary_service`. |
| Chat shows a manifest proposal for a project that already has `.tagh/instance.yml` in its repo | The manifest exists on a non-default branch or wasn't pushed | Confirm it's on the default branch and `git push`. |
| Provisioning succeeds but the public URL returns 502 | Primary service's healthcheck never went green or the boot commands hung | Check `docker logs tagh-inst-<slug>-projctl-1` — projctl's stdout shows which boot command stalled. |
| `docker compose -f a -f b config` shows `ports:` on a non-cloudflared service | Overlay generator regression | Run `pytest tests/contract/test_overlay_compose_layering.py -v` — the contract test should reproduce. File a bug. |
