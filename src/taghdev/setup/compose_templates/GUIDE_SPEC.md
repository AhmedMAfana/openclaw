# `guide.md` + `project.yaml` — authoring contract

**Status**: v1.
**Consumers**: [projctl](../../../../projctl/) (Go), [instance_compose_renderer](../../services/instance_compose_renderer.py) (Python).
**See also**: [specs/001-per-chat-instances/contracts/projctl-stdout.schema.json](../../../../specs/001-per-chat-instances/contracts/projctl-stdout.schema.json), [specs/001-per-chat-instances/contracts/llm-fallback-envelope.schema.json](../../../../specs/001-per-chat-instances/contracts/llm-fallback-envelope.schema.json).

This spec tells project authors what to put in their `guide.md` and `project.yaml` so that `projctl up` can execute their setup deterministically, step by step, with resumable state and bounded LLM fallback on failure.

---

## 1. `project.yaml`

One file per project template. Machine-readable metadata that the orchestrator reads at provisioning time.

```yaml
# project.yaml — example for the Laravel + Vue template
name: laravel-vue
description: "Laravel 11 backend + Vue 3 + Vite front end with HMR."
stack: [laravel, vue, vite, mysql]

# Which service in the compose file is the "app" (php-fpm etc.) — the one
# projctl exec runs its steps against. Must match a service name in compose.yml.
app_service: app

# Which service carries the front-end dev server (for HMR env injection).
dev_service: node

# Resource profile; matches Instance.resource_profile (standard | large).
resource_profile: standard

# guide.md lives next to this file unless overridden.
guide: guide.md

# Optional: extra env vars the template needs. These MUST NOT include secrets —
# secrets arrive via the orchestrator's env-injection, not the template.
env:
  APP_ENV: local
  NODE_ENV: development
```

**Validation**:
- `app_service` MUST match a service name in the template's `compose.yml`.
- `resource_profile` MUST be `standard` or `large`.
- `env` keys MUST NOT match `/SECRET|TOKEN|PASSWORD|KEY|AUTH/i` — use the orchestrator's secret-injection path instead. Renderer refuses to emit a template that tries to embed secrets via `env`.

---

## 2. `guide.md` — step format

`guide.md` is ordinary Markdown with a strict structure. Each `##` heading is a **step**. `projctl up` runs steps in document order; state is keyed by heading slug so edits to the guide invalidate just the changed steps.

### Required shape per step

````markdown
## install-php

```projctl
cmd: composer install --no-interaction --prefer-dist
cwd: /app
success_check: composer show -i | grep -q laravel/framework
skippable: false
retry_policy: exponential_backoff
max_attempts: 3
```

Short human-readable description of what this step does. `projctl explain`
includes this prose verbatim when asking the LLM for help on failure.
````

### Field semantics

| Field | Required | Type | Default | Meaning |
|-------|----------|------|---------|---------|
| `cmd` | yes | string | — | Shell command. Runs under `/bin/sh -c`. Treated as a single logical unit; chain with `&&` for multi-command steps. |
| `cwd` | no | string | `/app` | Working directory inside the container. |
| `success_check` | yes | string | — | Command that MUST exit 0 to declare the step successful even if `cmd` exited 0. This is the belt on top of the exit-code braces. |
| `skippable` | no | bool | `false` | If `true`, the LLM fallback may return `action: skip`. If `false`, skip is never allowed. |
| `retry_policy` | no | string | `none` | `none` \| `fixed_delay` \| `exponential_backoff`. |
| `max_attempts` | no | int | `1` | How many times `projctl up` retries `cmd` before invoking the LLM fallback. Capped at 5. |
| `timeout_seconds` | no | int | `300` | Per-attempt wall-clock timeout. Mandatory per Constitution Principle IX; defaults always exist. |

### Step naming

- Heading MUST be `## <kebab-case-name>` — one header token, no spaces, no emoji.
- Heading slug is the step's stable identifier across edits. Rename = new step.
- Duplicate slugs in one guide are a parse error.

---

## 3. `projctl up` execution model

1. Parse `guide.md`. Any parse error → emit `fatal` event and exit non-zero.
2. Compute SHA-256 of `guide.md` contents. Compare against `state.json`. If different, invalidate all recorded step outcomes (they belonged to a different guide).
3. For each step in document order:
   - If `state.json` already has `status=success` for this step, skip. Emit no event.
   - Otherwise emit `step_start`.
   - Run `cmd` with `timeout_seconds`.
   - If exit code 0, run `success_check`; emit `success_check` event.
   - On both OK → emit `step_success`, persist to state.json, continue.
   - On any failure → emit `step_failure`. If `attempt < max_attempts`, retry per `retry_policy`. Else invoke `projctl explain` (LLM fallback) up to 3 times. If still failing, emit `fatal` and exit.
4. On successful completion of every step, emit nothing further and exit 0.

---

## 4. Interaction with `projctl explain`

When a step fails beyond its retries:

1. Build the envelope per [llm-fallback-envelope.schema.json](../../../../specs/001-per-chat-instances/contracts/llm-fallback-envelope.schema.json):
   - `stdout_tail` / `stderr_tail` — last 200 lines each, redacted via the shared `audit_service.redact` on the orchestrator side (the `/internal/instances/<slug>/explain` endpoint runs the redactor a second time belt-and-braces; projctl SHOULD also redact locally if it has access to the redactor binary).
   - `guide_section` — the raw Markdown of this step, from the `## name` line through the next `##` or EOF.
   - `previous_attempts` — increments with each LLM ask. Cap at 3.
2. POST to `/internal/instances/<slug>/explain`.
3. Parse `{action, payload, reason}`:
   - `shell_cmd` → run `payload` as a shell command, then re-run the step's `cmd`.
   - `patch` → `git apply --check <payload>`; if clean, `git apply`; then re-run `cmd`.
   - `skip` → only accepted if `skippable: true`; emit `step_success` marked `skipped: true`.
   - `give_up` → emit `fatal`, exit non-zero. Orchestrator flips instance to `failure_code=projctl_up`.

---

## 5. State file (`/var/lib/projctl/state.json`)

```json
{
  "guide_version": "<sha256 of guide.md>",
  "steps": {
    "install-php":  { "status": "success", "finished_at": "2026-04-23T14:22:01Z" },
    "install-node": { "status": "success", "finished_at": "2026-04-23T14:23:07Z" },
    "migrate":      { "status": "failed",  "attempt": 2, "last_error": "..." }
  },
  "last_heartbeat_at": "2026-04-23T14:30:00Z"
}
```

- Lives on a named Docker volume `tagh-inst-<slug>-projctl-state`. Survives container restart; destroyed by `compose down -v`.
- `guide_version` mismatch invalidates ALL step entries — the guide changed, start over.
- projctl never reads DB directly. The orchestrator is the only DB writer.

---

## 6. Example — minimal Laravel+Vue `guide.md`

````markdown
## install-php

```projctl
cmd: composer install --no-interaction --prefer-dist
cwd: /app
success_check: test -d /app/vendor
skippable: false
max_attempts: 3
retry_policy: exponential_backoff
timeout_seconds: 300
```

Installs PHP dependencies via Composer. Required before `artisan migrate`.

## install-node

```projctl
cmd: npm ci
cwd: /app
success_check: test -d /app/node_modules
skippable: false
max_attempts: 2
timeout_seconds: 600
```

Installs front-end deps for Vite.

## migrate

```projctl
cmd: php artisan migrate --force
cwd: /app
success_check: php artisan migrate:status | grep -q "Ran"
skippable: false
max_attempts: 2
timeout_seconds: 60
```

Runs DB migrations against the per-instance MySQL.

## start-queue

```projctl
cmd: php artisan queue:work --daemon &
cwd: /app
success_check: pgrep -f "queue:work" > /dev/null
skippable: true
max_attempts: 1
timeout_seconds: 10
```

Starts the Laravel queue worker. Skippable because some projects don't use queues.

## start-node

```projctl
cmd: npm run dev &
cwd: /app
success_check: curl -sf http://localhost:5173 > /dev/null
skippable: false
max_attempts: 2
timeout_seconds: 60
```

Starts Vite dev server. Must stay up for HMR to work (see spec §5.4).
````

---

## 7. Forbidden patterns

These patterns are rejected at parse time with a `fatal` event:

- **Secrets in `cmd`**: any literal matching `/SECRET|TOKEN|PASSWORD|KEY|AUTH/i` followed by `=` in the command text. Secrets must come from env vars injected by the orchestrator.
- **`docker`, `docker compose`, or `kubectl` in `cmd`**: instances do NOT have Docker socket access. If a project needs these, that's a template-level concern, not a step.
- **`sudo` / `su -`**: containers run as their configured user; privilege escalation is not supported.
- **Unbounded `tail -f` / long-running commands without backgrounding**: use `&` for daemons (queue workers, dev servers); interactive foreground commands block `projctl up` forever.
- **Steps without `success_check`**: every step must be verifiable. "Assume it worked" is not a success criterion.

---

## 8. Versioning

This spec version: **1**. A future breaking change to step format will bump this to **2** with a migration note. `project.yaml` MAY add a `guide_spec_version: 1` field when a project opts into stricter validation; absent is treated as `1`.
