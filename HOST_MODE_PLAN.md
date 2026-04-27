# Host-Mode Migration + Local VPS Simulation

## Context

TAGH Dev currently manages user projects by cloning them into `/workspaces/_cache/{project_name}` inside its own Docker containers, then running `docker compose build`/`up` to spin them up as managed stacks. The product owner has decided this is the wrong deployment model for their Digital Ocean VPS: the TAGH Dev orchestrator keeps running under Docker, but **user apps are already running on the VPS host** as plain directories (e.g. `/srv/projects/<repo>`, `/opt/apps/<repo>`, or `~/projects/<repo>` — chosen per deploy). Each user project has its own `README`/`INSTALL`/`CLAUDE.md` install guide; the add-project agent should follow that guide rather than invent a Docker flow.

We also need a **local simulation** so developers can build and QA the new flow without needing a real VPS.

### User decisions (locked-in)
- **Sibling migration**, not a replace: Docker mode stays for existing projects; host mode is a parallel code path gated by `project.mode = "docker" | "host"`.
- **Projects base dir is flexible**, configured from the dashboard settings and saved to DB. No hardcoded `/srv/projects`.
- **Agent auto-clones** the repo into the configured base dir on first setup, then `git pull`s on subsequent bootstraps.
- **Simulation**: user apps live **at the same filesystem level as the TAGH Dev repo** (e.g. `../sim-fastapi/`, `../sim-next/`), and a small FastAPI-based local server (our "local-VPS") exposes them to the browser like the VPS would.

### Goal
One new sibling code path (`mode="host"`) reusing everything that works in Docker mode — streaming, retry loops, repair cards, tunnel service — plus a self-contained local simulation that exercises the exact same code path.

---

## 1. Data model changes

**Edit** [src/openclow/models/project.py:9-29](src/openclow/models/project.py#L9-L29) — add host-mode columns (all nullable/defaulted so existing Docker rows are unaffected):

```python
mode:               Mapped[str] = mapped_column(String(10), default="docker", server_default="docker")
project_dir:        Mapped[str | None] = mapped_column(String(500))   # absolute path resolved from base + name
install_guide_path: Mapped[str | None] = mapped_column(String(255))   # e.g. README.md, INSTALL.md, CLAUDE.md
start_command:      Mapped[str | None] = mapped_column(String(500))
stop_command:       Mapped[str | None] = mapped_column(String(500))
health_url:         Mapped[str | None] = mapped_column(String(255))   # default http://localhost:<app_port>/
process_manager:    Mapped[str | None] = mapped_column(String(50))    # pm2 | systemd | supervisor | manual
auto_clone:         Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
```

Keep `is_dockerized`, `docker_compose_file`, `app_container_name`, `app_port`, `setup_commands` unchanged — host projects leave Docker fields NULL, reuse `app_port` + `setup_commands`.

**New** Alembic migration: `alembic/versions/NNN_host_mode.py` — adds columns with server defaults, no data backfill.

---

## 2. Configuration (dashboard setting, persisted to DB)

Projects base dir is **not an env var** — it's a dashboard setting saved in the existing `PlatformConfig` table so it can be changed per-deployment without a redeploy.

**Edit** [src/openclow/services/settings_service.py](src/openclow/services/settings_service.py) — add keys:
- `host.projects_base` — absolute path; default value computed at runtime: `../` relative to the TAGH Dev repo (simulation) OR `/srv/projects` (production).
- `host.mode_default` — `"docker"` | `"host"` (global default applied when onboarding a new project; can be overridden per-project).
- `host.auto_clone_default` — bool.

**Edit** React admin settings panel ([chat_frontend/src/components/settings/SettingsProjects.tsx](chat_frontend/src/components/settings/SettingsProjects.tsx)) — add a "Host Mode" section with these three fields. Existing `/settings/config/{category}` endpoints already persist and read them.

**Env fallback** (bootstrap safety — read by settings.py only on cold start if DB empty): `HOST_PROJECTS_BASE`, `PROJECT_MODE_DEFAULT`. No new feature flag beyond `project.mode`.

---

## 3. New MCP server: `host_mcp`

**New file** `src/openclow/mcp_servers/host_mcp.py` — modeled on [src/openclow/mcp_servers/docker_mcp.py](src/openclow/mcp_servers/docker_mcp.py). Tools:

| Tool | Signature | Purpose |
|------|-----------|---------|
| `host_cd` | `(project_dir)` | Verify dir exists, return canonical path + `git status` |
| `host_git_clone` | `(repo_url, project_dir)` | First-time clone into base dir |
| `host_git_pull` | `(project_dir, branch="main")` | `git -C <dir> fetch && reset --hard origin/<branch>` |
| `host_read_install_guide` | `(project_dir)` | Read README.md / INSTALL.md / CLAUDE.md / SETUP.md (first found), truncated to ~8KB |
| `host_run_command` | `(project_dir, command, timeout=120)` | Shell command inside project_dir; enforced by `host_guard` allowlist; streams stdout/stderr chunks to Redis pub/sub channel |
| `host_check_port` | `(port)` | `ss`/`lsof` — returns "LISTEN pid cmd" or "FREE" |
| `host_curl` | `(url, timeout=10)` | Real HTTP hit — returns `HTTP <code>\n<first 1KB body>` |
| `host_process_status` | `(match)` | `ps -eo pid,cmd \| grep <match>` |
| `host_tail_log` | `(path, lines=100)` | Tail log file — path must be under `project_dir` or a whitelisted journald unit |
| `host_start_app` | `(project_dir, start_command)` | Detached spawn (`nohup` + `setsid`); pid + first 2s output |
| `host_stop_app` | `(project_dir, stop_command="")` | Run stop command or fall back to `pm2 stop` / `systemctl stop` based on `process_manager` |
| `host_service_status` | `(unit)` | `systemctl status` / `pm2 status` auto-detect |

**New file** `src/openclow/services/host_guard.py` — mirrors [src/openclow/services/docker_guard.py](src/openclow/services/docker_guard.py):
- Allowlist: `git`, `npm`, `yarn`, `pnpm`, `pip`, `pip3`, `python`, `python3`, `composer`, `php`, `node`, `bundle`, `rails`, `go`, `mvn`, `gradle`, `make`, `pm2`, `systemctl`, `journalctl`, `curl`, `wget`, `ls`, `cat`, `head`, `tail`, `grep`, `find`, `ps`, `ss`, `lsof`, `pwd`, `which`, `test`.
- Blocklist: `rm -rf /`, fork-bombs, `dd if=`, `mkfs`, `shutdown`, `reboot`, `sudo su`, subshell-to-shell pipes, writes under `/etc/` `/boot/` `/root/`.
- Confines writes to under `settings.host_projects_base` (realpath-resolved).
- Uses the existing [audit_service](src/openclow/services/audit_service.py) `log_action` / `log_blocked`.

**Edit** [src/openclow/providers/llm/claude.py:35-44](src/openclow/providers/llm/claude.py#L35-L44) — add factory:
```python
def _mcp_host() -> dict:
    return {"command": "python", "args": ["-m", "openclow.mcp_servers.host_mcp"]}
```

**Edit** [src/openclow/worker/tasks/_agent_base.py:11-83](src/openclow/worker/tasks/_agent_base.py#L11-L83) — extend `describe_tool` for `host_*` names (📥 git pull, 📖 install guide, 🖥 run, 🌐 curl, 📜 logs, ▶️ start, ⏹ stop).

---

## 4. Onboarding flow (host mode)

**Edit** [src/openclow/agents/onboarding.py](src/openclow/agents/onboarding.py) — add `HOST_ONBOARDING_PROMPT` + `analyze_repo_host()` alongside existing Docker versions. Extend `ProjectConfig` + `parse_config` with new fields (start_command, stop_command, process_manager, health_url, install_guide_path).

**Edit** [src/openclow/worker/tasks/onboarding.py:19-181](src/openclow/worker/tasks/onboarding.py#L19-L181) — top-level branch:

```
if mode == "host":
    project_dir = os.path.join(settings_service.get("host.projects_base"), project_name)
    if not os.path.isdir(project_dir):
        if auto_clone:
            await _host_clone(repo_url, project_dir)       # uses host_git_clone
        else:
            return error("Project dir not found; clone manually or enable auto_clone")
    await _host_git_pull(project_dir)
    config = await analyze_repo_host(project_dir, on_progress=...)
else:
    # existing Docker path unchanged
```

`confirm_project` in [src/openclow/api/routes/actions.py:97-162](src/openclow/api/routes/actions.py#L97-L162) stores the new fields on the Project row.

---

## 5. Bootstrap flow (host mode)

**Edit** [src/openclow/worker/tasks/bootstrap.py](src/openclow/worker/tasks/bootstrap.py) — top-of-function branch in `bootstrap_project`:
```
if project.mode == "host":
    return await _bootstrap_project_host(ctx, project, chat, chat_id, message_id)
# else: existing Docker path unchanged
```

**New function** `_bootstrap_project_host` in the same file — 6-step checklist replacing Docker's 8-step:

```
1. Verify/clone project_dir                  (host_cd / host_git_clone)
2. git pull                                  (host_git_pull)
3. Read install guide                        (host_read_install_guide)
4. Run setup commands                        (agent + host_run_command loop — MCP-first, LLM-fallback)
5. Ensure app is running                     (host_process_status → host_start_app if down)
6. Health check + tunnel + Playwright verify (host_curl → existing tunnel_service.start_tunnel → Playwright MCP)
```

**New prompt** `HOST_MASTER_BOOTSTRAP_PROMPT` (sibling to the existing [MASTER_BOOTSTRAP_PROMPT lines 45-167](src/openclow/worker/tasks/bootstrap.py#L45-L167)). Same structure, same STATUS/DIAGNOSIS/ACTION/STEP_DONE/BOOTSTRAP_COMPLETE vocabulary. Rules:
- MCP-first: always try `host_*` tools before reasoning from scratch.
- Never give up: 3 concretely-different approaches before surfacing blocker to user.
- Stream narration before + after every tool call.

**Reuse** the existing `_run_master_agent` ([src/openclow/worker/tasks/bootstrap.py:775-900](src/openclow/worker/tasks/bootstrap.py#L775-L900)) with `prompt_override=HOST_MASTER_BOOTSTRAP_PROMPT.format(...)`, `allowed_tools=["mcp__host__*", "Read", "Glob", "Grep"]`, `mcp_servers={"host": _mcp_host()}`. Idle watchdog, retry, heartbeat, cancel machinery all work as-is.

Skip `_preflight` entirely for host mode (it's Docker-container cleanup). Host preflight: confirm `project_dir` exists (or auto-clone) and `app_port` is free or held by our known PID.

After STEP 6, reuse the existing tunnel block unchanged — `tunnel_target = f"http://localhost:{project.app_port}"`.

---

## 6. Shared project-exec abstraction

**New file** `src/openclow/services/project_exec.py`:

```python
async def execute_in_project(project, command: str, timeout: int = 60) -> tuple[int, str]:
    if project.mode == "host":
        return await host_guard.run_host(command, cwd=project.project_dir, timeout=timeout,
                                         actor="task", project_id=project.id)
    container = f"openclow-{project.name}-{project.app_container_name or 'app'}-1"
    return await docker_guard.run_docker("docker", "exec", container, "sh", "-c", command,
                                         actor="task", project_id=project.id, timeout=timeout)
```

**Edit** every deterministic `docker_exec` call in [src/openclow/worker/tasks/orchestrator.py:299-485](src/openclow/worker/tasks/orchestrator.py#L299-L485) (`_run_frontend_build`, `_run_lightweight_deploy`) + [src/openclow/worker/tasks/health_task.py](src/openclow/worker/tasks/health_task.py) HTTP probe → route through `execute_in_project`.

**Edit** [src/openclow/providers/llm/claude.py](src/openclow/providers/llm/claude.py) — extract `_tools_and_mcp_for(project)` that returns `(allowed_tools, mcp_servers)` based on `project.mode`. Used by `run_coder`, `run_coder_fix`, `run_reviewer`, health_guard. Agent system prompts gain a conditional `HOST ENVIRONMENT: ...` section when mode=host.

---

## 7. Streaming enhancements

Current gap: `ToolUseBlock` announcements stream, but tool **output** (stdout from `host_run_command`) does not.

**Edit** [src/openclow/providers/chat/web/__init__.py:146-227](src/openclow/providers/chat/web/__init__.py#L146-L227) — add `send_tool_output(chat_id, message_id, tool_name, chunk, final=False)` publishing a `"tool_output"` event to `wc:{user_id}:{session_id}`.

**Wire** in `host_run_command`: the MCP tool reads `stream_hook` from env (published by the orchestrator) and writes incremental stdout chunks to that Redis channel directly. No callback plumbing.

**Edit** the streaming helper in [src/openclow/worker/tasks/agent_session.py:590-740](src/openclow/worker/tasks/agent_session.py#L590-L740) + [orchestrator `_run_agent_with_streaming`](src/openclow/worker/tasks/orchestrator.py#L176-L276) — new `StreamEvent` branch for `mcp_tool_result` → `send_tool_output(final=True)`.

**Edit** [chat_frontend/src/App.tsx:86-132](chat_frontend/src/App.tsx#L86-L132) `readStream()` + thread component — handle `tool_output` event: render as scrolling panel attached to its parent `tool_use`.

---

## 8. Senior DevOps + Chat Support Engineer persona

**Edit** [src/openclow/api/routes/assistant.py:246-355](src/openclow/api/routes/assistant.py#L246-L355) `_build_system_prompt()` — replace the current TAGH block with three paragraphs:

> You are a Senior DevOps Engineer and AI Chat Support Engineer for TAGH Dev. You run the infrastructure and you talk to the user at the same time.
>
> As a DevOps engineer you own the outcome. User apps live on this host under the configured projects base dir. You have tools to pull code, install deps, start services, read logs, and verify health over HTTP. You try the MCP tool first — `host_cd`, `host_git_pull`, `host_read_install_guide`, `host_run_command`, `host_curl`, `host_process_status`, `host_tail_log`, `host_start_app`. You never invent tools. Only when an MCP tool returns a clearly unresolvable error that you've investigated do you fall back to reasoning and a different tool.
>
> As a chat support engineer you explain what you're doing in plain English in real time. Before each tool call: one sentence about what you're about to do. After each tool returns: 2-3 sentences on what actually happened, based on the exact tool result. Never promise background work; never say "queued"; never hand off to a progress card.
>
> **Never give up.** "Unfixable" does not exist. If a command fails, read the output, form a hypothesis, try a different approach. If the app won't start, tail the logs, find the real error, fix it, restart. Keep trying different concrete approaches until the app is healthy or you've documented three specific attempts that each failed for a specific reason. "The tool kept failing" is not a reason — understand *why* and fix that first.

**Edit** [src/openclow/worker/tasks/_agent_helper.py:14-76](src/openclow/worker/tasks/_agent_helper.py#L14-L76) — the existing persona already says "senior DevOps engineer" and "NEVER GIVE UP"; extract `_build_system_prompt(mode: str)` that swaps the "Available Tools" + "Critical Architecture" sections for host mode. The NEVER GIVE UP clause (line 52) stays verbatim.

---

## 9. Local VPS simulation (Python-based local server)

Developers run user apps **at the same filesystem level as the TAGH Dev repo** — `../sim-fastapi/`, `../sim-next/`, `../sim-laravel/` — and a tiny FastAPI "local-VPS" supervisor exposes them to the browser, mimicking what Digital Ocean + tunnel will do in production.

**New directory** `dev-sandbox/` in the openclow repo (tracked; a dev convenience, not deployed):

```
dev-sandbox/
├── Makefile.sim              # included from root Makefile
├── local_vps.py              # FastAPI "local VPS" server + supervisor (one file)
├── local_vps.yml             # process list (sample apps, ports, cmds, health urls)
├── seed_sim_projects.py      # inserts sim-* rows into projects table
├── logs/                     # per-app log files (git-ignored)
├── env/                      # per-app .env files (git-ignored)
├── secrets.example.env       # tracked template
└── README.md                 # how to run
```

User app skeletons live **outside** the openclow repo at the same level:
```
<parent-dir>/
├── openclow/                 # this repo
├── sim-fastapi/              # Python 3.12 + FastAPI, port 8101
├── sim-next/                 # Next.js 14, port 8102
└── sim-laravel/              # Laravel 11, port 8103
```

Each sample app has a real README install guide (the one the agent will read):
- **sim-fastapi**: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && uvicorn app.main:app --port 8101`
- **sim-next**: `npm install && npm run build && PORT=8102 npm start`
- **sim-laravel**: `composer install && cp .env.example .env && php artisan key:generate && php artisan migrate --force && php artisan serve --port=8103`

### `dev-sandbox/local_vps.py` (one stdlib+FastAPI file, ~300 LOC)

Two roles in one process:

**a) Supervisor**: reads `local_vps.yml`, spawns each app with `subprocess.Popen(start_new_session=True)`, tees stdout/stderr into `logs/<name>.log`, health-polls every 5s via `httpx`, maintains `registry.json` (`{name, pid, pgid, port, status, health_url, log_path}`).

**b) FastAPI "local VPS" HTTP server** on **port 8120** exposing:
- `GET /apps` — list registered sample apps (name, port, status, health)
- `GET /apps/{name}` — redirect to `http://localhost:<port>/` for browser access (like a tunnel does in prod)
- `POST /apps/{name}/start|stop|restart` — admin controls
- `GET /apps/{name}/logs?tail=200` — log viewer
- `GET /apps/{name}/health` — proxied health check

This is our **local-server-behavior analog of the VPS + cloudflared tunnel**. The web chat/agent calls the `host_*` MCP tools exactly as it would in production; the tools resolve `project_dir` to `../sim-<name>/` on the developer's disk.

### Make targets (root Makefile includes `dev-sandbox/Makefile.sim`)

```
make sim-install   # one-time: clone starter apps as siblings, run their install steps
make sim-up        # start local_vps.py (supervisor + FastAPI admin)
make sim-down      # stop all
make sim-status    # curl :8120/apps | jq
make sim-seed      # run seed_sim_projects.py (inserts DB rows, mode="host")
make sim-logs NAME=sim-next
make sim-doctor    # check deps (python/node/php/composer), ports 8101-8103,8120 free
```

### `PROJECT_MODE` wiring — the ONE conditional

`host_mcp.py` resolves the base dir from DB settings; the only sim-vs-prod difference is the base dir value + how the orchestrator reaches the app:

| | simulate | host (production) |
|---|---|---|
| `host.projects_base` setting | `../` (sibling to openclow) | `/srv/projects` or configured |
| HTTP reachability from Docker | `host.docker.internal:<port>` | `localhost:<port>` |
| SSH path | N/A (local filesystem) | optional, if worker off-box |
| "Tunnel" | local_vps.py `/apps/{name}` redirect | existing cloudflared tunnel_service |

`docker-compose.override.yml` (dev-only) adds `extra_hosts: host.docker.internal:host-gateway` and a bind mount of the parent dir into the worker container as `/sandbox/projects` (read-write). The `host.projects_base` setting in DB points at `/sandbox/projects` for dev.

Everything else is REAL: real `git pull`, real `npm install`, real `curl`, real log tails, real file edits. Only SSH and hostname metadata are faked.

---

## 10. QA / reading list (user authorized cloning)

Repos to study for patterns worth stealing into TAGH Dev core:

1. **OpenHands** — https://github.com/All-Hands-AI/OpenHands — event-stream architecture, swappable runtime sandbox (maps directly onto simulate-vs-host), agent-controller stuck-detection loop. **Read first:** `openhands/controller/agent_controller.py`, then `openhands/events/stream.py`.
2. **Aider** — https://github.com/Aider-AI/aider — tree-sitter repo map for onboarding, `udiff` edit format for reliable apply, test-loop self-heal. **Read first:** `aider/coders/base_coder.py` (`run_one`, `reflected_message`), then `aider/repomap.py`.
3. **SWE-agent** — https://github.com/SWE-agent/SWE-agent — minimal Agent-Computer Interface (fewer more deterministic tools), trajectory record/replay for QA regressions, hooks for instrumentation. **Read first:** `sweagent/agent/agents.py`, then `config/default.yaml`.
4. **Mastra** — https://github.com/mastra-ai/mastra — evals framework (LLM-judge + deterministic), typed workflow DAGs, clean MCP client patterns. **Read first:** `packages/core/src/agent/index.ts`, then `packages/evals/src/metrics/llm/`.
5. **assistant-ui** — https://github.com/assistant-ui/assistant-ui — you already use it; adopt newer `ContentPartByToolName` + thread persistence primitives + the `examples/with-mcp` folder for the tool→UI card handshake.

Use these as inspiration for: the new host_mcp tool surface (SWE-agent's ACI discipline), the simulate↔host runtime swap (OpenHands runtime), self-healing test loops (Aider), CI evals (Mastra), and the tool_output streaming UI (assistant-ui).

---

## 11. File-by-file execution order

**Phase 1 — foundation (no behavior change; Docker mode still default)**
1. [src/openclow/models/project.py](src/openclow/models/project.py) — add columns
2. `alembic/versions/NNN_host_mode.py` — migration
3. [src/openclow/services/settings_service.py](src/openclow/services/settings_service.py) — register `host.*` keys
4. [src/openclow/providers/llm/claude.py](src/openclow/providers/llm/claude.py) — `_mcp_host()` factory + `_tools_and_mcp_for(project)`

**Phase 2 — host MCP + guard**
5. `src/openclow/services/host_guard.py` (new)
6. `src/openclow/mcp_servers/host_mcp.py` (new)
7. [src/openclow/worker/tasks/_agent_base.py](src/openclow/worker/tasks/_agent_base.py) — extend `describe_tool`
8. `src/openclow/services/project_exec.py` (new) — shared `execute_in_project`

**Phase 3 — onboarding (host mode)**
9. [src/openclow/agents/onboarding.py](src/openclow/agents/onboarding.py) — `HOST_ONBOARDING_PROMPT`, `analyze_repo_host`, extended `ProjectConfig`
10. [src/openclow/worker/tasks/onboarding.py](src/openclow/worker/tasks/onboarding.py) — mode branch + auto-clone
11. [src/openclow/api/routes/actions.py](src/openclow/api/routes/actions.py) — `confirm_project` persists new fields

**Phase 4 — bootstrap (host mode)**
12. [src/openclow/worker/tasks/bootstrap.py](src/openclow/worker/tasks/bootstrap.py) — `HOST_MASTER_BOOTSTRAP_PROMPT`, `_bootstrap_project_host`, top-level mode branch
13. [src/openclow/worker/tasks/health_task.py](src/openclow/worker/tasks/health_task.py) — mode branch; reuses `_run_master_agent` with host prompt

**Phase 5 — task execution + persona**
14. [src/openclow/worker/tasks/orchestrator.py](src/openclow/worker/tasks/orchestrator.py) — swap direct `docker_exec` for `execute_in_project`; agent tool list via `_tools_and_mcp_for`
15. [src/openclow/worker/tasks/_agent_helper.py](src/openclow/worker/tasks/_agent_helper.py) — `_build_system_prompt(mode)` for host-mode repair
16. [src/openclow/api/routes/assistant.py](src/openclow/api/routes/assistant.py) — new DevOps+Chat-Support persona

**Phase 6 — streaming**
17. [src/openclow/providers/chat/web/__init__.py](src/openclow/providers/chat/web/__init__.py) — `send_tool_output`
18. [src/openclow/worker/tasks/agent_session.py](src/openclow/worker/tasks/agent_session.py) + orchestrator streaming helper — surface `mcp_tool_result` as `tool_output`
19. [chat_frontend/src/App.tsx](chat_frontend/src/App.tsx) + thread component — render `tool_output` event

**Phase 7 — admin UI**
20. [chat_frontend/src/components/settings/SettingsProjects.tsx](chat_frontend/src/components/settings/SettingsProjects.tsx) — Host Mode section (base dir, default mode, auto-clone toggle)

**Phase 8 — simulation harness**
21. `dev-sandbox/local_vps.py` (new) — supervisor + FastAPI admin on 8120
22. `dev-sandbox/local_vps.yml` (new) — 3 sample apps
23. `dev-sandbox/seed_sim_projects.py` (new) — insert `mode="host"` rows pointing at `../sim-*`
24. `dev-sandbox/Makefile.sim` + root Makefile include — `sim-*` targets
25. `dev-sandbox/README.md` — dev setup walkthrough
26. [docker-compose.override.yml](docker-compose.override.yml) — bind-mount parent dir, `host.docker.internal` extra_hosts

**Phase 9 — nothing deleted**
Every Docker path stays. `_preflight`, `_step_clone`, `compose_build`, `compose_up`, `MASTER_BOOTSTRAP_PROMPT`, `docker_mcp` continue to serve `mode="docker"` projects unchanged.

---

## 12. Verification

End-to-end smoke test (simulation):
1. `make sim-install` — clones 3 sibling apps, runs install steps
2. `docker compose up -d` — TAGH Dev services
3. `make sim-up` — supervisor starts 3 apps on 8101/8102/8103, admin on 8120
4. Visit `http://localhost:8120/apps` — see healthy list
5. `make sim-seed` — insert 3 `mode="host"` project rows
6. Open web chat → say "add project sim-fastapi" → confirm it onboards via HOST prompt, reads real README, runs real `pip install`, detects already-running app on 8101, streams every `host_*` tool call with narration before/after, health-checks `http://host.docker.internal:8101/`, creates tunnel (or reuses local_vps redirect in dev), finishes BOOTSTRAP_COMPLETE.
7. Kill `sim-fastapi` process → trigger repair → confirm agent tails logs, restarts via `host_start_app`, re-verifies, never surfaces "unfixable".
8. Create a coding task against `sim-next` → confirm coder uses `host_run_command` for `npm run build`, diffs real files, all tool output streams live to the web panel.
9. Run a Docker-mode project in parallel → confirm it still works identically to today.

Manual checks:
- `python -m py_compile` on every edited Python file.
- `docker compose restart bot worker` after code changes (no rebuild needed unless pyproject.toml changed).
- `docker compose logs -f bot worker | grep -E "error|Error|host_"` during a full onboarding.
- Alembic: `alembic upgrade head` applies cleanly; `alembic downgrade -1` reverses.
- Frontend typecheck: `cd chat_frontend && npm run typecheck`.

Production readiness gate (before flipping prod VPS to host mode):
- Every sample app in sim runs a full add-project + coding task without manual intervention.
- All three attempt/failure paths exercised: fresh clone, git-pull-update, app-down-auto-restart.
- Tool output streaming visible in browser for long-running `npm install`.
- Admin settings panel round-trips `host.projects_base` change and new projects honor it.
