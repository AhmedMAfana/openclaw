# Repo Audit — Current State Before Per-Chat Instances Refactor

**Date:** 2026-04-22
**Branch:** `multi-instance`
**Scope:** Current host/docker run modes, lifecycle, tunnels, project metadata, auth.
**Purpose:** Ground the per-chat-isolated-instance architecture in what exists today. No new code until this is signed off.

---

## TL;DR

TAGH Dev already has most of the primitives the new design needs, but assembled around a different model: per-**task** ephemeral workspaces on a shared worker, not per-**chat** long-lived instances. Specifically:

- **Tunnel plane exists** ([tunnel_service.py](../../src/taghdev/services/tunnel_service.py), 545 lines) — but it uses Cloudflare **quick tunnels** (anonymous, ephemeral `*.trycloudflare.com`), not account-bound named tunnels with owned hostnames. Account-backed DNS automation is absent.
- **Two run modes coexist** (`docker` and `host`) selected per-project via `Project.mode` ([project.py:27](../../src/taghdev/models/project.py#L27)). Both are wired and live in code — not a plan.
- **No orchestrator process.** Lifecycle (create/run/destroy) is inlined into the ARQ worker tasks, scoped to a single task, not a chat.
- **No per-chat container instance today.** A "chat" maps to a `WebChatSession` row, and its tasks run inside the shared `worker` container using `/workspaces/task-<id>/` git worktrees. The user's app is reached via an ad-hoc cloudflared tunnel pointed at the container's Docker network.
- **No inactivity reaper for chats** — only an idle watchdog inside the bootstrap master-agent ([bootstrap.py:934](../../src/taghdev/worker/tasks/bootstrap.py#L934)) that kills a stuck agent, not an instance.
- **No `guide.md` / `project.yaml` schema** — onboarding is driven by an LLM agent reading the repo and writing arbitrary fields into `Project`.

Net: we keep the tunnel service, workspace caching logic, port allocator, Redis locking, and host-mode MCP tools. We replace: the per-task workspace concept (→ per-chat instance), quick-tunnel generator (→ account-bound named tunnels with managed DNS), and the "agent-decides-bootstrap" flow (→ declarative `guide.md` + CLI, LLM as fallback).

---

## A. Run Modes — Host vs Docker

### The selector

- **Column:** `Project.mode: str` default `"docker"` — [project.py:27](../../src/taghdev/models/project.py#L27).
- **Branch point:** [bootstrap.py:1117](../../src/taghdev/worker/tasks/bootstrap.py#L1117):
  ```python
  if (getattr(project, "mode", "docker") or "docker") == "host":
      return await _bootstrap_project_host(...)
  ```
  The host path lives at [bootstrap.py:1604](../../src/taghdev/worker/tasks/bootstrap.py#L1604) (`_bootstrap_project_host`, ~240 lines).
- **Default resolution** happens in onboarding — reads `host.mode_default` from `PlatformConfig`, falls back to `"docker"`.

### Docker mode (the default path)

Driven by an LLM "master agent" with MCP tools for Docker:

- **Agent prompt:** [bootstrap.py:45–167](../../src/taghdev/worker/tasks/bootstrap.py#L45-L167) — 8-step bootstrap narrative (clone → verify compose → install → build → up → migrate → verify → tunnel).
- **MCP server:** [docker_mcp.py](../../src/taghdev/mcp_servers/docker_mcp.py) exposes `compose_build`, `compose_up`, `compose_down`, `container_logs`, `docker_exec`, etc.
- **Guard rails:** [docker_guard.py](../../src/taghdev/services/docker_guard.py) — allowlist/blocklist for docker subcommands, enforces container-scoped exec.
- **Per-project columns:** `is_dockerized`, `docker_compose_file`, `app_container_name`, `app_port` — [project.py:22–25](../../src/taghdev/models/project.py#L22-L25).
- **Port allocation:** [port_allocator.py](../../src/taghdev/services/port_allocator.py) — each project gets a deterministic 100-port range (`10000 + project_id*100`) with fixed offsets (`+0 app`, `+1 db`, `+6 vite`...). Injected as env vars into the project's compose file. **This is keeper-quality — we'll reuse it for the new instance IPs/hostnames.**
- **Workspace:** `/workspaces/task-<id>/` git worktree per task; `/workspaces/_cache/<project_name>/` shared clone cache — [workspace_service.py:30–80](../../src/taghdev/services/workspace_service.py#L30-L80). Lock files are hashed to detect dependency changes.

### Host mode (newer path, already landed)

User apps run as plain OS processes on the VPS host, not in containers:

- **MCP server:** [host_mcp.py](../../src/taghdev/mcp_servers/host_mcp.py) (242 lines, 13 tools): `host_git_clone`, `host_run_command`, `host_start_app`, `host_stop_app`, `host_check_port`, `host_curl`, `host_process_status`, `host_tail_log`, `host_read_install_guide`, etc.
- **Guard rails:** [host_guard.py](../../src/taghdev/services/host_guard.py) (270 lines) — allowlist (`git`, `npm`, `php`, `composer`, `pm2`, `systemctl`, `curl`, ...), blocklist (`rm -rf /`, writes to `/etc` `/boot` `/root`, shutdown, fork-bombs), cwd sandbox validation, streams output to Redis pub/sub, audits every call.
- **Install-guide discovery:** [host_mcp.py:83–97](../../src/taghdev/mcp_servers/host_mcp.py#L83-L97) — searches for `README.md`, `INSTALL.md`, `SETUP.md`, `CLAUDE.md`, `docs/INSTALL.md`, etc. First match wins, truncated to 8 KB. **This is the closest thing we have today to a `guide.md` convention — but it's freeform prose, not structured.**
- **Per-project columns:** `project_dir`, `install_guide_path`, `start_command`, `stop_command`, `health_url`, `process_manager`, `auto_clone` — [project.py:28–34](../../src/taghdev/models/project.py#L28-L34).
- **Local simulator:** [dev-sandbox/local_vps.py](../../dev-sandbox/local_vps.py) — small FastAPI supervisor that mimics a VPS with multiple sample apps (sim-fastapi, sim-next, sim-laravel). Useful for local testing of host-mode agent code without a real VPS.

### Command matrix

| Operation       | Docker                                                  | Host                                       |
|-----------------|---------------------------------------------------------|--------------------------------------------|
| Clone           | `docker compose run --rm app git clone`                 | `git clone` on host                        |
| Install deps    | `docker compose build` (Dockerfile)                     | `composer install` / `npm install`         |
| Start           | `docker compose up <svc>`                               | `nohup setsid sh -c <start_command>`       |
| Health          | `curl -i http://<container>:<port>/`                    | `curl http://localhost:<port>/`            |
| Logs            | `docker compose logs <svc>`                             | `tail -f <logfile>`                        |

---

## B. Instance Lifecycle

### There is no orchestrator

Lifecycle is glued into the task worker. Every call into the system comes through the ARQ queue as a "task" job; the worker creates a workspace at the start and tears it down at the end.

- **Entry:** [orchestrator.py](../../src/taghdev/worker/tasks/orchestrator.py) (2,547 lines) — `execute_task` picks the route (docker vs host, quick vs plan, chat vs bootstrap) and runs it.
- **Workspace create:** [workspace_service.py](../../src/taghdev/services/workspace_service.py) — `WorkspaceService.prepare(project, task_id)` clones into the per-project cache on first use, then `git worktree add` for the task. Idempotent; guarded by a Redis lock `taghdev:workspace:<project>` (15-min TTL — [workspace_service.py:36](../../src/taghdev/services/workspace_service.py#L36)).
- **Workspace destroy:** same service — `cleanup(task_id)` removes the worktree properly (`git worktree remove`) and leaves the cache intact for reuse.
- **Project state column:** `Project.status` = `active | bootstrapping | failed | inactive` — [project.py:40](../../src/taghdev/models/project.py#L40). Written by bootstrap success/failure. There is no per-chat instance state anywhere.
- **Stuck-lock cleaner:** [maintenance.py](../../src/taghdev/worker/tasks/maintenance.py) — hourly ARQ cron scans Redis for workspace locks older than `max_stuck_minutes` (~4 h) and clears them, demoting project status from `bootstrapping` → `failed`.

### State persistence

- **DB (source of truth):** `Project.status`, `Task.status` ([task.py:11–36](../../src/taghdev/models/task.py)), tunnel records in `platform_config` (`category="tunnel"`, `key=<service_name>`, `value={url,pid,worker_id,ts}`).
- **Redis (ephemeral):** workspace locks, per-session cancel flags ([orchestrator.py:59](../../src/taghdev/worker/tasks/orchestrator.py#L59)), activity log pub/sub.
- **In-process (worker-local):** `tunnel_service._active_processes` dict of cloudflared `asyncio.subprocess.Process` handles — [tunnel_service.py:29](../../src/taghdev/services/tunnel_service.py#L29). **This is a distributed-systems smell: if the worker restarts, the handles die but DB still shows the tunnel URL. Tunnel service tries to detect this by checking `/proc/<pid>/status` ([tunnel_service.py:90–107](../../src/taghdev/services/tunnel_service.py#L90-L107)), but it's process-local health only.**

### Inactivity / idle

- **No whole-instance reaper.**
- **One idle watchdog exists** inside the bootstrap master-agent loop — [bootstrap.py:934–950](../../src/taghdev/worker/tasks/bootstrap.py#L934-L950). It kills a stuck agent after `idle_timeout` seconds (default 1800). It does not tear down containers, tunnels, or workspaces. It is not wired to chat activity.
- **Task watchdog** in orchestrator: different mechanism (20-turn no-diff kill for coder agent stalls). Again, agent-scoped, not instance-scoped.

---

## C. Tunnels / Public URLs

### What exists

[tunnel_service.py](../../src/taghdev/services/tunnel_service.py) (545 lines) — a real, working Cloudflare tunnel manager. This is the single most reusable piece in the codebase for the new design.

- **Mechanism:** `cloudflared tunnel --url <target>` — [tunnel_service.py:113](../../src/taghdev/services/tunnel_service.py#L113). This is Cloudflare **quick tunnel** mode: anonymous, no account, produces `https://<random>.trycloudflare.com`.
- **URL extraction:** regex against cloudflared stderr — [tunnel_service.py:134](../../src/taghdev/services/tunnel_service.py#L134) — matches `https://[a-z0-9-]+\.trycloudflare\.com`.
- **Idempotency:** checks `/proc/<pid>/status` (`cloudflared` in name) before spawning a duplicate — [tunnel_service.py:90–107](../../src/taghdev/services/tunnel_service.py#L90-L107).
- **Rate-limit back-off:** on 429, sets a 10-minute global cooldown — [tunnel_service.py:150](../../src/taghdev/services/tunnel_service.py#L150).
- **Per-service asyncio lock:** prevents concurrent spawns for the same service — [tunnel_service.py:44–48](../../src/taghdev/services/tunnel_service.py#L44-L48).
- **Laravel-specific helpers:** auto-rewrites old trycloudflare URLs in `APP_URL`/`ASSET_URL` after tunnel rotation, injects `config/trustedproxy.php` — [tunnel_service.py:266–325](../../src/taghdev/services/tunnel_service.py#L266-L325). **This logic has to move: in the new design, tunnel hostnames are stable per instance, so most of this rewriting becomes unnecessary.**

### What's missing for the target architecture

1. **Account-bound tunnels.** No `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, or `CLOUDFLARE_ZONE_ID` anywhere in the repo. Checked: [.env.example](../../.env.example) (4 vars, infra only), `src/taghdev/settings.py`, `PlatformConfig` entries. Fresh Cloudflare setup required.
2. **DNS automation.** No CNAME management. Quick tunnel gets a new random URL every restart. For per-chat stable URLs we need named tunnels + DNS record automation (this is what **DockFlare** does — see research task).
3. **Multiple hostnames per instance.** We need both `https://<chat-id>.<our-domain>` (web) and Vite HMR routing. The service is built around one URL per "service_name" — extending it or replacing it is on the table.
4. **WebSocket / HMR handling.** `cloudflared` forwards WS by default, but there's no Vite config template; each project agent currently hand-rolls its `vite.config.js` via LLM. This becomes part of the `guide.md` / compose template story.

### Fallback path for owned domains

[project.py:37–38](../../src/taghdev/models/project.py#L37-L38) — `public_url` + `tunnel_enabled=False` lets a project bypass cloudflared and point at an nginx vhost. Kept for production. In the new per-chat model this is the "production" teardown target; per-chat instances always use tunnels.

---

## D. Project Metadata / Per-Project Config

### The `Project` row

[project.py:9–41](../../src/taghdev/models/project.py#L9-L41) — 22 columns total. Flattened: one row per "project the bot knows about", covering both modes:

| Concern    | Columns |
|------------|---------|
| Identity   | `id`, `name`, `github_repo`, `default_branch`, `description`, `tech_stack` |
| Agent hint | `agent_system_prompt`, `force_fresh_install`, `setup_commands` |
| Docker     | `is_dockerized`, `docker_compose_file`, `app_container_name`, `app_port` |
| Host       | `mode`, `project_dir`, `install_guide_path`, `start_command`, `stop_command`, `health_url`, `process_manager`, `auto_clone` |
| Networking | `public_url`, `tunnel_enabled`, `health_url` |
| Lifecycle  | `status`, `created_at` |

### No structured project descriptor

- **No `guide.md` schema.** The field `install_guide_path` just points to some freeform markdown that the agent reads — not machine-executable.
- **No `project.yaml`, `.devcontainer/`, `nixpacks.toml`, `Procfile`, or equivalent.**
- **`setup_commands` column** is a single text blob of extra shell commands that the agent *may* run during bootstrap. Not structured, not idempotent, no success checks.

This is the biggest gap. The new design introduces `guide.md` + optional `project.yaml` as the contract.

### Chat-to-project binding

- [web_chat.py:9–28](../../src/taghdev/models/web_chat.py) — `WebChatSession` row: `user_id`, `project_id`, `title`, `mode` (`quick`/`plan`), `git_mode` (default `session_branch`), `session_branch_name`, timestamps.
- **One session = one git branch**, not one container. The session branch model is keeper-quality for the new instance; we'll pin the instance's working tree to that branch.

### Repo cloning strategy

- Full clones (no `--depth`) into `/workspaces/_cache/<project_name>/` — [workspace_service.py:30](../../src/taghdev/services/workspace_service.py#L30).
- Per-task isolation via `git worktree add` — fast, hardlinks .git objects, no copy.
- Dependency cache detection via lockfile md5s — [workspace_service.py:65–85](../../src/taghdev/services/workspace_service.py#L65-L85).
- **Keep this pattern for new instances.** It maps cleanly: shared bare repo → per-instance worktree bound to the chat's session branch.

---

## E. Auth / Secrets

### GitHub token

- **Model:** [config.py](../../src/taghdev/models/config.py) — `PlatformConfig` rows (category, key, value JSON).
- **Storage:** `category="git"`, `key="provider.github"`, value `{token, ...}`. Managed through setup wizard (`python -m taghdev.setup`) and admin dashboard.
- **Scope:** **one token, globally shared** across all projects. No per-project override, no per-user mapping, no GitHub App (short-lived installation tokens).
- **Bot identity on commits:** hard-coded in [Dockerfile.worker:62–63](../../Dockerfile.worker#L62-L63) — `taghdev@bot.local` / "TAGH Dev Bot". All PRs attributed to the bot.
- **Retrieval:** `get_provider_config("git")` at task-execute time — [services/github_service.py](../../src/taghdev/services/github_service.py), [tasks/git_ops.py](../../src/taghdev/worker/tasks/git_ops.py).

**Target-architecture implication:** "PR push auth per instance" in the brief means either (a) minting a short-lived installation token from a GitHub App per instance, or (b) attaching the user's PAT if one is on file. Currently we have neither. This is new work.

### Secret hygiene

- **Logs:** [host_guard.py:246](../../src/taghdev/services/host_guard.py) truncates command output at 16 KB. [audit_service.py](../../src/taghdev/services/audit_service.py) caps at 2 KB. Neither redacts — reliance is on not echoing secrets into commands.
- **Env files:** [.env.example](../../.env.example) only contains infra URLs + placeholder passwords. All provider creds live in DB. `.env`, `auth.json`, `tokens.txt` are gitignored.
- **`auth.json`** is bind-mounted read-only into api/worker — [docker-compose.yml:73](../../docker-compose.yml) — for Composer private-repo tokens. Leaks only on filesystem compromise, not in logs.
- **Streaming to UI:** host_guard pipes raw stdout/stderr over Redis pub/sub with no redaction. **Risk for the new design** — if the per-chat instance's app logs include secrets, they'll flow straight to the user's chat. Redaction belongs on the path to the LLM-fallback, too.

---

## F. Docker Setup Today

### Services — [docker-compose.yml](../../docker-compose.yml)

| Service   | Image / Dockerfile        | Exposed ports                  | Role                                      |
|-----------|---------------------------|--------------------------------|-------------------------------------------|
| postgres  | postgres:16-alpine        | none (override exposes 5432)   | App + platform DB                         |
| redis     | redis:7-alpine            | none (override exposes 6379)   | ARQ queue, locks, pub/sub                 |
| bot       | Dockerfile.app            | none                           | Telegram/Slack long-poll bot              |
| api       | Dockerfile.app            | **8000:8000**                  | FastAPI dashboard + web chat              |
| worker    | Dockerfile.worker         | none                           | ARQ worker (fat image, does everything)   |
| dozzle    | amir20/dozzle             | **9999:8080**                  | Docker log viewer                         |
| migrate   | Dockerfile.app (once)     | none                           | `alembic upgrade head`                    |
| setup     | Dockerfile.app (once)     | none                           | Interactive provider-config wizard        |

### Shared volumes

- `postgres_data`, `redis_data` — stateful stores.
- `workspaces` → `/workspaces` in api + worker — where project caches and task worktrees live.
- `claude_auth` → `/home/taghdev/.claude` — persists Claude Code login across restarts.
- `activity_logs` → `/app/logs` — audit log jsonl.
- `/var/run/docker.sock` — bind-mounted into api + worker so they can talk to host Docker. **This gives both containers root-equivalent on the host. Keep this in mind for the new design — the per-chat `cloudflared` sidecar needs to join a Docker network the worker created.**

### Worker image ([Dockerfile.worker](../../Dockerfile.worker), 86 lines)

A single fat image containing everything a project bootstrap might need: Node 20 (via nvm), PHP + Composer, `gh`, `git`, `cloudflared`, Docker CLI, Playwright + Chromium, Claude Code CLI, ffmpeg, jq. ~4 GB, 4 GB memory limit. This is effectively the "sandbox runtime" today — but it's *one shared sandbox for all projects*, not one per chat.

### Dev overrides — [docker-compose.override.yml](../../docker-compose.override.yml)

- Live-mounts `./src` into api/bot/worker for reload.
- Exposes postgres 5432 + redis 6379 for host debugging.
- Adds `host.docker.internal:host-gateway` extra-host — used by host-mode code path to reach the local-VPS simulator when running on macOS.

### Prod overrides — [docker-compose.prod.yml](../../docker-compose.prod.yml)

- API binds to `127.0.0.1:8000`; assumes an external nginx.
- No live-reload mounts. Graceful shutdown bumped to 10 s.

### Local VPS sim — [dev-sandbox/](../../dev-sandbox/)

- `local_vps.py` — FastAPI supervisor simulating a VPS with sample apps for host-mode dev.
- `seed_sim_projects.py` — seeds mode=`host` projects into the DB pointing at sim directories.
- `Makefile.sim` — targets to spin the sim up/down.
- **Keep for now** — useful regression target for host-mode code that's not being deleted. Becomes redundant if we fully deprecate host mode, but that's out of scope for this refactor (brief says "replacing host and docker with per-chat instances" — we'll fold both into one model, but host-mode code may remain as a fallback for existing projects).

### workspaces_dev/

Host dir bind-mounted as `/workspaces` in dev. Contains the cache + worktrees during local dev. Git-ignored.

---

## G. Cloudflare / Domain Story

- **No account-level Cloudflare integration exists.** No API token, account ID, or zone ID in `.env.example`, `settings.py`, or `PlatformConfig` seeds. Grep: zero hits for `CLOUDFLARE_ACCOUNT`, `CLOUDFLARE_ZONE`, `CF_API_TOKEN`.
- **Cloudflare presence** = `cloudflared` CLI baked into `Dockerfile.worker`, invoked anonymously as `cloudflared tunnel --url ...`.
- **Domain assumption:** every tunnel rotation produces a **new** `*.trycloudflare.com` URL. The code has extensive logic to propagate that rotation into running apps ([tunnel_service.py:266–325](../../src/taghdev/services/tunnel_service.py#L266-L325) rewrites `APP_URL`/`ASSET_URL` in `.env`, injects Laravel trusted proxy config).
- **Production escape hatch:** `project.public_url` + `tunnel_enabled=False` for operators who own a domain.

**For the new design we should assume we're starting from scratch on the account-bound side:** procure a Cloudflare account, create a zone for an owned domain (`*.<our-domain>`), mint an API token scoped to DNS + Access, and build named-tunnel provisioning on top. None of this exists today.

---

## Open Questions — Answered

| Q                                                                    | A |
|----------------------------------------------------------------------|---|
| Do we already have an orchestrator process?                          | **No.** Lifecycle is inlined into ARQ worker tasks ([orchestrator.py](../../src/taghdev/worker/tasks/orchestrator.py), [bootstrap.py](../../src/taghdev/worker/tasks/bootstrap.py)). Per-task, not per-chat. Introducing an orchestrator is greenfield work. |
| Where does per-chat state currently live?                            | **Postgres + Redis + filesystem.** `WebChatSession` ([web_chat.py](../../src/taghdev/models/web_chat.py)) + `Task` ([task.py](../../src/taghdev/models/task.py)) rows; Redis for locks/cancel flags; `/workspaces/` for worktrees. No in-memory session store. Adding an `Instance` model alongside `WebChatSession` is clean. |
| Existing Cloudflare account / tunnel?                                | **No account.** Only anonymous quick-tunnels via `cloudflared` CLI. New work: account, zone, API token, named tunnels, DNS automation. |
| How are project repos currently cloned into the runtime?             | **Full clone** (no `--depth`) into per-project cache at `/workspaces/_cache/<name>/`, then **git worktree** per task — [workspace_service.py:30–80](../../src/taghdev/services/workspace_service.py#L30-L80). Dep install cached via lockfile md5s. Keep this pattern, re-scope from "per task" to "per chat instance". |
| Auth story for PRs today                                             | **Single shared GitHub PAT** stored in `PlatformConfig` (`category="git"`, `key="provider.github"`), used for every PR. Bot is always the committer. No GitHub App, no per-user token, no SSH. Per-instance short-lived tokens are new work. |

---

## Keep / Refactor / Delete

### Keep (reuse as-is, or with light changes)

- **[tunnel_service.py](../../src/taghdev/services/tunnel_service.py)** — the spawn/monitor/restart plumbing. Swap the underlying `cloudflared tunnel --url` command for `cloudflared tunnel run <named>` driven by DockFlare-style label reconciliation; keep the DB persistence, per-service locks, rate-limit cooldown, and health-check loop.
- **[workspace_service.py](../../src/taghdev/services/workspace_service.py)** — cache-plus-worktree pattern, dependency hashing, Redis lock. Re-scope the unit from task to instance (each instance gets its own worktree pinned to the chat's session branch).
- **[port_allocator.py](../../src/taghdev/services/port_allocator.py)** — deterministic per-project port ranges. Pattern generalises to "deterministic per-instance subnet / hostname slug".
- **[host_guard.py](../../src/taghdev/services/host_guard.py)** + **[docker_guard.py](../../src/taghdev/services/docker_guard.py)** — allowlist/blocklist + audit-log. Keep the guard model; point it at the instance's Docker network instead of the host.
- **`Project.mode` / `Project.public_url` / `Project.tunnel_enabled`** — the production escape hatch (own domain + nginx) keeps working unchanged.
- **[web_chat.py](../../src/taghdev/models/web_chat.py) session branch model** — keep. Instance ↔ session is 1:1.
- **ARQ + Redis + Postgres** stack. No reason to change.
- **`dev-sandbox/local_vps.py`** — useful for host-mode regression; not on the critical path of the refactor.

### Refactor

- **[orchestrator.py](../../src/taghdev/worker/tasks/orchestrator.py) (2,547 lines)** — split the "instance lifecycle" concerns out of the task orchestrator. Task execution *should* call into an `InstanceService` that already has a running per-chat instance; it should not be doing container setup inline.
- **[bootstrap.py](../../src/taghdev/worker/tasks/bootstrap.py) (1,882 lines, LLM-driven)** — replace the "master agent runs 8 steps via MCP tools" model with `projctl up` running a `guide.md` deterministically. The LLM moves to the fallback path (`projctl explain <error-id>`). This is the single biggest cost saver in the refactor.
- **[tunnel_service.py](../../src/taghdev/services/tunnel_service.py) Laravel URL rewriting ([:266–325](../../src/taghdev/services/tunnel_service.py#L266-L325))** — becomes mostly unnecessary once tunnels are stable per instance; delete or reduce to a one-shot initializer.
- **`Project` model** — the table currently carries both "catalog metadata" (github_repo, tech_stack) and "one running instance state" (app_port, project_dir, status). Split into `Project` (catalog, long-lived) + `Instance` (per chat, short-lived) per the brief.
- **GitHub auth** — move from "one shared PAT" to "short-lived token per instance" (GitHub App installation token or per-user PAT injection). New `git/app` config in `PlatformConfig`.

### Delete (after the refactor lands, not now)

- **Quick-tunnel URL rewriting** in `tunnel_service.py` once named tunnels stabilise hostnames.
- **Host-mode bootstrap MCP agent flow** ([bootstrap.py:1604+](../../src/taghdev/worker/tasks/bootstrap.py#L1604)) — becomes one path (container-based) in the new model. Host-mode can stay as a non-default operator option; its MCP tools remain for existing mode=`host` projects. Only the agent-driven bootstrap flow is removed.
- **`Project.is_dockerized`** — in the new world every instance is containerised, so the flag loses meaning.
- **`setup_commands` text blob** — superseded by `guide.md` steps.
- **`install_guide_path`** — superseded by `guide.md` at a fixed convention location.

### Flag for discussion (don't decide in the audit)

- Do we deprecate host-mode entirely, or keep it as an "advanced" alternative to per-chat containers? Brief says "replacing host and docker", implying both go. But host-mode has landed recently and has a working local sim. Propose: keep host-mode as a legacy path for a release, move per-chat containers to default, deprecate host-mode in a follow-up.
- Worker fat-image vs one-image-per-language — the new per-chat instance needs language runtimes, but we don't want to ship a 4 GB image to every chat. Likely answer: thin base + language-specific sidecars defined by the project (Laravel template: `app` = `php-fpm`, `node` = `node:20`, etc.). This is resolved in the compose template deliverable.

---

## Outstanding before architecture doc

Per the working agreement, these need sign-off before I write `per-chat-instances.md`:

1. **Networking choice** — `cloudflared` sidecar per instance (simpler teardown, more containers) vs one `cloudflared` per host multiplexing by hostname (fewer containers, `ingress:` reconciliation is the hard bit). My lean is **sidecar per instance**, because teardown is `docker compose down` and nothing else; hostname rotation is localised. But DockFlare's model is the shared-multiplexer variant, and if we adopt it we inherit that topology.
2. **CLI name** — `projctl` in the brief. Confirm, or propose alternatives (`taghctl`, `devctl`). Leaning `projctl`.
3. **Keep host-mode as legacy or delete?** (See flag-for-discussion above.)
4. **Stack: TypeScript vs Python for orchestrator + CLI.** Brief says "TypeScript unless existing code says otherwise." The existing orchestrator and all services are Python. Changing to TS introduces a second runtime for no strong reason. Proposal: **Python for the orchestrator, Node/TS only for `projctl` if we adopt Runme or mdrb (both TS).** Confirm.

Once those four are decided, I'll move on to `docs/architecture/per-chat-instances.md` and `docs/research/prior-art.md`.
